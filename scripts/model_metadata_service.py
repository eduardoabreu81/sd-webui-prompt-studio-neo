"""Shared model metadata resolver for Prompt Studio Neo.

Prompt Studio owns the cache in this module. CivitAI Browser Neo artifacts are
optional, read-only inputs: this module never creates or mutates ``.json`` or
``.api_info.json`` files next to models. ``.civitai.info`` is handled as a
separate external/legacy format because it is not created by Browser Neo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import requests


CACHE_VERSION = 1
CACHE_PATH = Path(__file__).resolve().parents[1] / "storage" / "model_metadata_cache.json"
NETWORK_RETRY_SECONDS = 6 * 60 * 60

_CACHE_LOCK = threading.RLock()
_CACHE_MEMORY: dict[str, tuple[dict[str, int], dict[str, Any]]] = {}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MODEL_FIELDS = (
    "sha256",
    "base_model",
    "trigger_words",
    "model_id",
    "model_version_id",
)


def normalize_sha256(value: Any) -> str:
    value = str(value or "").strip()
    return value.upper() if _SHA256_RE.fullmatch(value) else ""


def _normalize_id(value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _same_id(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return True
    return str(left) == str(right)


def normalize_trigger_words(value: Any) -> list[str]:
    """Return a stable, case-insensitively deduplicated list of trigger words."""
    values: list[Any]
    if isinstance(value, (list, tuple, set)):
        values = list(value)
    elif value is None:
        values = []
    else:
        values = [value]

    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, (list, tuple, set)):
            parts: Iterable[Any] = item
        else:
            parts = re.split(r"[,;\r\n]+", str(item))
        for part in parts:
            word = str(part).strip()
            key = word.casefold()
            if word and key not in seen:
                seen.add(key)
                result.append(word)
    return result


def trigger_words_text(metadata: dict[str, Any]) -> str:
    return ", ".join(normalize_trigger_words(metadata.get("trigger_words")))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _fingerprint(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
        return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    except OSError:
        return {"mtime_ns": 0, "size": 0}


def _cache_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _empty_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "models": {}}


def _load_cache(path: Path) -> dict[str, Any]:
    memory_key = _cache_key(path)
    fingerprint = _fingerprint(path)
    cached_memory = _CACHE_MEMORY.get(memory_key)
    if cached_memory and cached_memory[0] == fingerprint:
        return cached_memory[1]

    data = _read_json(path)
    if data.get("version") != CACHE_VERSION or not isinstance(data.get("models"), dict):
        data = _empty_cache()
    _CACHE_MEMORY[memory_key] = (fingerprint, data)
    return data


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temp_path, path)
    _CACHE_MEMORY[_cache_key(path)] = (_fingerprint(path), data)


def _new_record(path: Path, model_type: str) -> dict[str, Any]:
    return {
        "file_path": str(path),
        "model_type": model_type,
        "fingerprint": _fingerprint(path),
        "sha256": "",
        "base_model": "",
        "trigger_words": [],
        "model_id": None,
        "model_version_id": None,
        "sources": [],
        "civitai_checked_at": 0,
    }


def _merge_record(target: dict[str, Any], source: dict[str, Any], *, overwrite: bool) -> None:
    for field in _MODEL_FIELDS:
        value = source.get(field)
        if field == "trigger_words":
            value = normalize_trigger_words(value)
        if value in (None, "", []):
            continue
        if overwrite or target.get(field) in (None, "", []):
            target[field] = value

    for source_name in source.get("sources", []):
        if source_name and source_name not in target["sources"]:
            target["sources"].append(source_name)

    checked_at = source.get("civitai_checked_at", 0)
    if checked_at:
        target["civitai_checked_at"] = max(target.get("civitai_checked_at", 0), checked_at)


def _sidecar_trigger_words(data: dict[str, Any]) -> tuple[list[str], str]:
    groups = data.get("activation text groups")
    if groups is None:
        groups = data.get("activation_text_groups")
    words = normalize_trigger_words(groups)
    if words:
        return words, "browser_neo_json"

    words = normalize_trigger_words(data.get("activation text"))
    if words:
        return words, "browser_neo_json"

    # Compatibility with TAC versions that wrote their private cache fields into
    # the Browser/Forge metadata sidecar. Read once; never write them again.
    words = normalize_trigger_words(data.get("civitai_trained_words"))
    return (words, "legacy_tac_json") if words else ([], "")


def _version_sha256(version: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for file_info in version.get("files", []) or []:
        file_hashes = file_info.get("hashes", {}) if isinstance(file_info, dict) else {}
        if not isinstance(file_hashes, dict):
            continue
        for key in ("SHA256", "sha256"):
            value = normalize_sha256(file_hashes.get(key))
            if value:
                hashes.add(value)
    return hashes


def _select_api_version(data: dict[str, Any], sidecar: dict[str, Any]) -> dict[str, Any]:
    versions = data.get("modelVersions")
    if not isinstance(versions, list):
        return data

    wanted_id = _normalize_id(sidecar.get("modelVersionId"))
    if wanted_id is not None:
        for version in versions:
            if isinstance(version, dict) and _same_id(version.get("id"), wanted_id):
                return version

    wanted_sha = normalize_sha256(sidecar.get("sha256"))
    if wanted_sha:
        for version in versions:
            if isinstance(version, dict) and wanted_sha in _version_sha256(version):
                return version

    return versions[0] if len(versions) == 1 and isinstance(versions[0], dict) else {}


def _record_from_api_data(
    data: dict[str, Any],
    sidecar: dict[str, Any] | None = None,
    *,
    source_name: str,
) -> dict[str, Any]:
    sidecar = sidecar or {}
    if not isinstance(data, dict):
        return {}

    is_model_response = isinstance(data.get("modelVersions"), list)
    api_model_id = data.get("id") if is_model_response else data.get("modelId")
    if api_model_id is None and isinstance(data.get("model"), dict):
        api_model_id = data["model"].get("id")

    sidecar_model_id = sidecar.get("modelId")
    if not _same_id(api_model_id, sidecar_model_id):
        return {}

    version = _select_api_version(data, sidecar)
    base_model = version.get("baseModel", "") if version else ""
    if not base_model:
        base_model = data.get("baseModel", "")
    if not base_model and isinstance(data.get("model"), dict):
        base_model = data["model"].get("baseModel", "")

    words = normalize_trigger_words(version.get("trainedWords")) if version else []
    if not words:
        words = normalize_trigger_words(data.get("trainedWords"))

    version_id = version.get("id") if version else data.get("id")
    if is_model_response and not version:
        version_id = None

    return {
        "base_model": str(base_model or "").strip(),
        "trigger_words": words,
        "model_id": _normalize_id(api_model_id),
        "model_version_id": _normalize_id(version_id),
        "sources": [source_name],
    }


def _artifact_paths(path: Path) -> tuple[Path, Path, Path]:
    base = path.with_suffix("")
    browser_json_path = Path(str(base) + ".json")
    api_info_path = Path(str(base) + ".api_info.json")
    external_info_path = Path(str(base) + ".civitai.info")
    if not external_info_path.is_file():
        appended_external_path = Path(str(path) + ".civitai.info")
        if appended_external_path.is_file():
            external_info_path = appended_external_path
    return browser_json_path, api_info_path, external_info_path


def read_model_artifacts(file_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read Browser Neo and external sidecars without modifying them."""
    path = Path(file_path)
    browser_json_path, api_info_path, external_info_path = _artifact_paths(path)

    result = _new_record(path, "")
    sidecar = _read_json(browser_json_path) if browser_json_path.is_file() else {}
    if sidecar:
        words, word_source = _sidecar_trigger_words(sidecar)
        canonical_fields_present = any(
            key in sidecar
            for key in (
                "sha256",
                "modelId",
                "modelVersionId",
                "baseModel",
                "sd version",
                "activation text",
                "activation text groups",
            )
        )
        result.update(
            {
                "sha256": normalize_sha256(sidecar.get("sha256") or sidecar.get("civitai_sha256")),
                "base_model": str(sidecar.get("baseModel") or sidecar.get("sd version") or "").strip(),
                "trigger_words": words,
                "model_id": _normalize_id(sidecar.get("modelId")),
                "model_version_id": _normalize_id(sidecar.get("modelVersionId")),
            }
        )
        if canonical_fields_present:
            result["sources"].append("browser_neo_json")
        if word_source and word_source not in result["sources"]:
            result["sources"].append(word_source)

    api_info = _read_json(api_info_path) if api_info_path.is_file() else {}
    if api_info:
        _merge_record(
            result,
            _record_from_api_data(api_info, sidecar, source_name="browser_neo_api_info"),
            overwrite=True,
        )

    # This format is not owned by Browser Neo. Keep it as an independent,
    # lower-priority compatibility source.
    external_info = _read_json(external_info_path) if external_info_path.is_file() else {}
    if external_info:
        external_model_id = external_info.get("modelId")
        if external_model_id is None and isinstance(external_info.get("model"), dict):
            external_model_id = external_info["model"].get("id")

        # A sidecar for another model must not leak metadata into this file.
        # Missing IDs remain compatible with older .civitai.info variants.
        if _same_id(external_model_id, sidecar.get("modelId")):
            external_record = _record_from_api_data(
                external_info, sidecar, source_name="external_civitai_info"
            )
            if not external_record:
                external_record = {
                    "base_model": str(external_info.get("baseModel") or "").strip(),
                    "trigger_words": normalize_trigger_words(
                        external_info.get("trainedWords") or external_info.get("activation text")
                    ),
                    "model_id": _normalize_id(external_model_id),
                    "model_version_id": _normalize_id(external_info.get("modelVersionId")),
                    "sources": ["external_civitai_info"],
                }
            _merge_record(result, external_record, overwrite=False)

    return result


def _image_urls(value: Any) -> list[str]:
    urls: list[str] = []
    for image in value if isinstance(value, list) else []:
        url = image.get("url") if isinstance(image, dict) else image
        url = str(url or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _display_from_api_data(
    data: dict[str, Any],
    sidecar: dict[str, Any],
    *,
    source_name: str,
) -> dict[str, Any]:
    normalized = _record_from_api_data(data, sidecar, source_name=source_name)
    if not normalized:
        return {}

    is_model_response = isinstance(data.get("modelVersions"), list)
    version = _select_api_version(data, sidecar)
    if is_model_response:
        model = {
            key: data.get(key)
            for key in ("id", "name", "type", "nsfw", "poi")
            if data.get(key) is not None
        }
    else:
        model = data.get("model", {}) if isinstance(data.get("model"), dict) else {}

    return {
        "modelId": normalized.get("model_id"),
        "modelVersionId": normalized.get("model_version_id"),
        "name": str((version or {}).get("name") or "").strip(),
        "description": str(data.get("description") or (version or {}).get("description") or ""),
        "baseModel": normalized.get("base_model", ""),
        "model": model,
        "trainedWords": normalized.get("trigger_words", []),
        "images": _image_urls((version or {}).get("images") or data.get("images")),
        "sources": [source_name],
    }


def _merge_display(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in (
        "modelId",
        "modelVersionId",
        "name",
        "description",
        "baseModel",
        "model",
        "trainedWords",
        "images",
    ):
        value = source.get(key)
        if value not in (None, "", [], {}):
            target[key] = value
    for source_name in source.get("sources", []):
        if source_name not in target["sources"]:
            target["sources"].append(source_name)


def read_model_display_info(file_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return UI metadata from Browser Neo and external sidecars, read-only."""
    path = Path(file_path)
    browser_json_path, api_info_path, external_info_path = _artifact_paths(path)
    browser_json = _read_json(browser_json_path)
    browser_api = _read_json(api_info_path)
    external_info = _read_json(external_info_path)

    result = {
        "modelId": None,
        "modelVersionId": None,
        "name": "",
        "description": "",
        "baseModel": "",
        "model": {},
        "trainedWords": [],
        "images": [],
        "sources": [],
    }

    # External/legacy info remains a valid fallback, but Browser artifacts win
    # whenever both products are installed.
    if external_info:
        _merge_display(
            result,
            _display_from_api_data(
                external_info,
                browser_json,
                source_name="external_civitai_info",
            ),
        )
    if browser_api:
        _merge_display(
            result,
            _display_from_api_data(
                browser_api,
                browser_json,
                source_name="browser_neo_api_info",
            ),
        )

    if browser_json:
        _merge_display(
            result,
            {
                "modelId": _normalize_id(browser_json.get("modelId")),
                "modelVersionId": _normalize_id(browser_json.get("modelVersionId")),
                "description": str(browser_json.get("description") or ""),
                "baseModel": str(
                    browser_json.get("baseModel") or browser_json.get("sd version") or ""
                ).strip(),
                "sources": ["browser_neo_json"],
            },
        )

    normalized = read_model_artifacts(path)
    _merge_display(
        result,
        {
            "modelId": normalized.get("model_id"),
            "modelVersionId": normalized.get("model_version_id"),
            "baseModel": normalized.get("base_model", ""),
            "trainedWords": normalized.get("trigger_words", []),
            "sources": normalized.get("sources", []),
        },
    )
    return result


def read_safetensors_metadata(file_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read only the compact safetensors JSON header metadata."""
    path = Path(file_path)
    if path.suffix.lower() != ".safetensors":
        return {}
    try:
        with path.open("rb") as handle:
            raw_length = handle.read(8)
            if len(raw_length) != 8:
                return {}
            header_length = struct.unpack("<Q", raw_length)[0]
            if header_length > 100 * 1024 * 1024:
                return {}
            header = json.loads(handle.read(header_length).decode("utf-8"))
        metadata = header.get("__metadata__", {}) if isinstance(header, dict) else {}
        return metadata if isinstance(metadata, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError, struct.error):
        return {}


def preferred_network_name(
    file_path: str | os.PathLike[str],
    alias: str | None = None,
    *,
    preferred_name: str | None = None,
    forbidden_aliases: Iterable[str] = (),
) -> str:
    """Return the LoRA name Forge will put in an extra-network prompt tag."""
    path = Path(file_path)
    filename = path.stem
    candidate = str(alias or read_safetensors_metadata(path).get("ss_output_name") or "").strip()

    if preferred_name is None:
        try:
            from modules import shared

            preferred_name = getattr(shared.opts, "lora_preferred_name", "Alias from file")
        except Exception:
            preferred_name = "Alias from file"

    forbidden = {str(value).casefold() for value in forbidden_aliases}
    if preferred_name == "Filename" or not candidate or candidate.casefold() in forbidden:
        return filename
    return candidate


def _read_safetensors_metadata_sha(path: Path) -> str:
    metadata = read_safetensors_metadata(path)
    if not metadata:
        return ""
    return normalize_sha256(metadata.get("modelspec.hash_sha256"))


def _read_sha256_sidecar(path: Path) -> str:
    sidecar_path = Path(str(path.with_suffix("")) + ".sha256")
    try:
        return normalize_sha256(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return ""


def _browser_hash_db_sha(path: Path) -> str:
    """Read Browser Neo's checkpoint hash index when the extension is available."""
    try:
        from modules import extensions

        for extension in extensions.extensions:
            extension_path = Path(getattr(extension, "path", ""))
            if extension_path.name.lower() != "sd-civitai-browser-neo":
                continue
            db_path = extension_path / "lib" / "models" / "checkpoint_hashes.json"
            db = _read_json(db_path)
            checkpoints = db.get("checkpoints", {}) if isinstance(db, dict) else {}
            if not isinstance(checkpoints, dict):
                return ""
            wanted = os.path.normcase(os.path.abspath(str(path)))
            for stored_path, entry in checkpoints.items():
                if os.path.normcase(os.path.abspath(stored_path)) != wanted:
                    continue
                if not isinstance(entry, dict):
                    return ""
                try:
                    if float(entry.get("mtime", 0)) < path.stat().st_mtime:
                        return ""
                except (OSError, TypeError, ValueError):
                    return ""
                return normalize_sha256(entry.get("sha256"))
    except Exception:
        return ""
    return ""


def _forge_hash(path: Path, model_type: str, *, calculate: bool) -> str:
    key_hash = hashlib.sha1(_cache_key(path).encode("utf-8")).hexdigest()
    forge_key = f"prompt-studio/{model_type or 'model'}/{key_hash}"
    try:
        from modules import hashes

        value = hashes.sha256_from_cache(str(path), forge_key, use_addnet_hash=False)
        if not value and calculate:
            value = hashes.sha256(str(path), forge_key, use_addnet_hash=False)
        return normalize_sha256(value)
    except Exception:
        if not calculate:
            return ""
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest().upper()
        except OSError:
            return ""


def get_prompt_studio_api_key() -> str:
    """Use Prompt Studio's PAIO key, with TAC's old key as migration fallback."""
    try:
        from modules import shared

        primary = getattr(shared.opts, "paio_neo_civitai_api_key", "") or ""
        legacy = getattr(shared.opts, "tac_civitaiApiKey", "") or ""
        return str(primary or legacy).strip()
    except Exception:
        return ""


def get_current_checkpoint_path() -> str:
    """Return the absolute path of the checkpoint currently selected by Forge."""
    try:
        from modules import sd_models, shared

        name = getattr(shared.opts, "sd_model_checkpoint", "")
        if not name:
            return ""
        info = sd_models.get_closet_checkpoint_match(name)
        return str(getattr(info, "filename", "") or "")
    except Exception:
        return ""


def resolve_current_checkpoint_metadata(
    *,
    required_fields: Iterable[str] = (),
    allow_network: bool = True,
) -> dict[str, Any]:
    """Resolve the selected checkpoint while reusing Forge's known full hash."""
    path = get_current_checkpoint_path()
    if not path:
        return _new_record(Path(""), "checkpoint")

    metadata = resolve_model_metadata(
        path,
        model_type="checkpoint",
        allow_network=False,
        calculate_hash=False,
    )
    if not metadata.get("sha256"):
        try:
            from modules import sd_models, shared

            info = sd_models.get_closet_checkpoint_match(
                getattr(shared.opts, "sd_model_checkpoint", "")
            )
            known_sha = normalize_sha256(getattr(info, "sha256", ""))
            if known_sha:
                metadata = seed_model_metadata(
                    path,
                    {"sha256": known_sha},
                    model_type="checkpoint",
                    source_name="forge_checkpoint_info",
                )
        except Exception:
            pass

    if allow_network and _needs_network(metadata, required_fields):
        metadata = resolve_model_metadata(
            path,
            model_type="checkpoint",
            required_fields=required_fields,
            allow_network=True,
            calculate_hash=not bool(metadata.get("sha256")),
        )
    return metadata


def fetch_civitai_version(sha256: str, api_key: str | None = None) -> dict[str, Any]:
    sha256 = normalize_sha256(sha256)
    if not sha256:
        return {}
    headers = {
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
    }
    key = get_prompt_studio_api_key() if api_key is None else api_key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = requests.get(
            f"https://civitai.com/api/v1/model-versions/by-hash/{sha256}",
            headers=headers,
            timeout=(15, 30),
        )
        if response.status_code != 200:
            return {}
        data = response.json()
        return data if isinstance(data, dict) and "error" not in data else {}
    except (requests.RequestException, ValueError):
        return {}


def _needs_network(record: dict[str, Any], required_fields: Iterable[str]) -> bool:
    for field in required_fields:
        if record.get(field) in (None, "", []):
            return True
    return False


def _cache_record(record: dict[str, Any], *, updated_at: int | None = None) -> dict[str, Any]:
    return {
        "file_path": record.get("file_path", ""),
        "model_type": record.get("model_type", ""),
        "fingerprint": record.get("fingerprint", {}),
        "sha256": normalize_sha256(record.get("sha256")),
        "base_model": record.get("base_model", ""),
        "trigger_words": normalize_trigger_words(record.get("trigger_words")),
        "model_id": record.get("model_id"),
        "model_version_id": record.get("model_version_id"),
        "sources": list(dict.fromkeys(record.get("sources", []))),
        "civitai_checked_at": int(record.get("civitai_checked_at", 0) or 0),
        "updated_at": int(time.time()) if updated_at is None else updated_at,
    }


def _cache_record_changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_without_time = {key: value for key, value in previous.items() if key != "updated_at"}
    current_without_time = {key: value for key, value in current.items() if key != "updated_at"}
    return previous_without_time != current_without_time


def resolve_model_metadata(
    file_path: str | os.PathLike[str],
    *,
    model_type: str = "model",
    required_fields: Iterable[str] = (),
    allow_network: bool = True,
    calculate_hash: bool = True,
    cache_path: str | os.PathLike[str] | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve normalized metadata from cache, read-only artifacts, Forge and CivitAI."""
    path = Path(file_path)
    record = _new_record(path, model_type)
    if not path.is_file():
        return record

    resolved_cache_path = Path(cache_path) if cache_path is not None else CACHE_PATH
    key = _cache_key(path)

    with _CACHE_LOCK:
        cache = _load_cache(resolved_cache_path)
        cached = cache["models"].get(key, {})
        if cached.get("fingerprint") == record["fingerprint"]:
            _merge_record(record, cached, overwrite=True)

        artifacts = read_model_artifacts(path)
        _merge_record(record, artifacts, overwrite=True)

        if not record["sha256"]:
            record["sha256"] = _read_sha256_sidecar(path)
            if record["sha256"]:
                record["sources"].append("sha256_sidecar")
        if not record["sha256"]:
            record["sha256"] = _browser_hash_db_sha(path)
            if record["sha256"]:
                record["sources"].append("browser_neo_hash_db")
        if not record["sha256"]:
            record["sha256"] = _read_safetensors_metadata_sha(path)
            if record["sha256"]:
                record["sources"].append("safetensors_metadata")
        if not record["sha256"]:
            record["sha256"] = _forge_hash(path, model_type, calculate=calculate_hash)
            if record["sha256"]:
                record["sources"].append("forge_hash_cache")

        now = int(time.time())
        recently_checked = now - int(record.get("civitai_checked_at", 0) or 0) < NETWORK_RETRY_SECONDS
        if (
            allow_network
            and record["sha256"]
            and _needs_network(record, required_fields)
            and not recently_checked
        ):
            api_data = (fetcher or fetch_civitai_version)(record["sha256"])
            if api_data:
                _merge_record(
                    record,
                    _record_from_api_data(api_data, source_name="civitai"),
                    overwrite=False,
                )
            record["civitai_checked_at"] = now

        record["sources"] = list(dict.fromkeys(record["sources"]))
        previous = cache["models"].get(key, {})
        cached_record = _cache_record(record, updated_at=previous.get("updated_at", now))
        if _cache_record_changed(previous, cached_record):
            cached_record["updated_at"] = now
            cache["models"][key] = cached_record
            _save_cache(resolved_cache_path, cache)

    return record


def seed_model_metadata(
    file_path: str | os.PathLike[str],
    values: dict[str, Any],
    *,
    model_type: str = "model",
    cache_path: str | os.PathLike[str] | None = None,
    source_name: str = "legacy_prompt_studio_cache",
) -> dict[str, Any]:
    """Import an older Prompt Studio cache entry into the shared cache."""
    record = resolve_model_metadata(
        file_path,
        model_type=model_type,
        allow_network=False,
        calculate_hash=False,
        cache_path=cache_path,
    )
    incoming = dict(values)
    incoming["sources"] = [source_name]
    _merge_record(record, incoming, overwrite=False)

    path = Path(file_path)
    resolved_cache_path = Path(cache_path) if cache_path is not None else CACHE_PATH
    with _CACHE_LOCK:
        cache = _load_cache(resolved_cache_path)
        key = _cache_key(path)
        previous = cache["models"].get(key, {})
        cached_record = _cache_record(
            record,
            updated_at=previous.get("updated_at", int(time.time())),
        )
        if _cache_record_changed(previous, cached_record):
            cached_record["updated_at"] = int(time.time())
            cache["models"][key] = cached_record
            _save_cache(resolved_cache_path, cache)
    return record
