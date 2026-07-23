"""Shared in-memory catalog for model and extra-network files.

Forge Neo already owns discovery of LoRAs.  Consumers in Prompt Studio can
publish those native entries here and resolve later API requests without
walking the model directory again.  A caller may still publish filesystem
fallback entries when the host catalog is unavailable.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MODEL_EXTENSIONS = frozenset({".safetensors", ".ckpt", ".pt"})
PREVIEW_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".preview.png",
    ".preview.jpg",
    ".preview.jpeg",
    ".preview.webp",
    ".preview.gif",
)


def _key(value: str) -> str:
    return value.strip().replace("\\", "/").casefold()


@dataclass(frozen=True)
class ModelCatalogEntry:
    path: Path
    alias: str = ""
    network_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).absolute())
        object.__setattr__(self, "alias", str(self.alias or "").strip())
        object.__setattr__(self, "network_hash", str(self.network_hash or "").strip())


class ModelFileCatalog:
    """Thread-safe path index keyed by model type and user-facing identifiers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, tuple[ModelCatalogEntry, ...]] = {}
        self._indexes: dict[str, dict[str, ModelCatalogEntry]] = {}

    def replace(
        self,
        model_type: str,
        root: str | Path,
        entries: Iterable[ModelCatalogEntry],
    ) -> tuple[ModelCatalogEntry, ...]:
        root_path = Path(root).absolute()
        normalized_entries = tuple(entries)
        index: dict[str, ModelCatalogEntry] = {}

        for entry in normalized_entries:
            path = entry.path
            candidates = [path.name, path.stem, entry.alias]
            try:
                relative = path.relative_to(root_path).as_posix()
                candidates.extend((relative, str(Path(relative).with_suffix(""))))
            except ValueError:
                pass

            # setdefault makes duplicate filename stems deterministic and keeps
            # the same first-entry behaviour as Forge's ordered catalog.
            for candidate in candidates:
                candidate_key = _key(candidate)
                if candidate_key:
                    index.setdefault(candidate_key, entry)

        type_key = _key(model_type)
        with self._lock:
            self._entries[type_key] = normalized_entries
            self._indexes[type_key] = index
        return normalized_entries

    def entries(self, model_type: str) -> tuple[ModelCatalogEntry, ...]:
        with self._lock:
            return self._entries.get(_key(model_type), ())

    def find(self, model_type: str, identifier: str) -> ModelCatalogEntry | None:
        raw = str(identifier or "").strip().replace("\\", "/")
        if not raw:
            return None

        path = Path(raw)
        candidates = [raw, path.name]
        if path.suffix.casefold() in MODEL_EXTENSIONS:
            candidates.extend((str(path.with_suffix("")), path.stem))

        with self._lock:
            index = self._indexes.get(_key(model_type), {})
            for candidate in candidates:
                entry = index.get(_key(candidate))
                if entry is not None:
                    return entry
        return None

    def clear(self, model_type: str | None = None) -> None:
        with self._lock:
            if model_type is None:
                self._entries.clear()
                self._indexes.clear()
                return
            type_key = _key(model_type)
            self._entries.pop(type_key, None)
            self._indexes.pop(type_key, None)


def find_companion_file(
    model_path: str | Path,
    extensions: Iterable[str],
) -> Path | None:
    """Return the first existing sibling that shares the model filename stem."""
    base_path = Path(model_path).with_suffix("")
    for extension in extensions:
        normalized_extension = str(extension)
        if not normalized_extension.startswith("."):
            normalized_extension = "." + normalized_extension
        candidate = Path(str(base_path) + normalized_extension)
        if candidate.is_file():
            return candidate
    return None


model_file_catalog = ModelFileCatalog()
