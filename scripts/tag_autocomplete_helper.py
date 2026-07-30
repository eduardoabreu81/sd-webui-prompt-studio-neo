# This helper script scans folders for wildcards and embeddings and writes them
# to a temporary file to expose it to the javascript side

import glob
import importlib
import json
import sqlite3
import sys
import urllib.parse
from asyncio import sleep
from pathlib import Path

import gradio as gr
import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from modules import hashes, script_callbacks, sd_models, shared
from pydantic import BaseModel

try:
    from scripts.model_keyword_support import (get_lora_simple_hash,
                                               load_hash_cache, update_hash_cache,
                                               write_model_keyword_path)
    from scripts.model_catalog_service import (
        MODEL_EXTENSIONS,
        PREVIEW_EXTENSIONS,
        ModelCatalogEntry,
        find_companion_file,
        model_file_catalog,
    )
    from scripts.model_metadata_service import (
        preferred_network_name,
        resolve_model_metadata,
        resolve_current_checkpoint_metadata,
        trigger_words_text,
    )
    from scripts.shared_paths import *
except ModuleNotFoundError:
    # Forge Neo can expose extension scripts as sibling modules instead of a
    # conventional package, depending on extension load order.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from model_keyword_support import (get_lora_simple_hash,
                                       load_hash_cache, update_hash_cache,
                                       write_model_keyword_path)
    from model_catalog_service import (
        MODEL_EXTENSIONS,
        PREVIEW_EXTENSIONS,
        ModelCatalogEntry,
        find_companion_file,
        model_file_catalog,
    )
    from model_metadata_service import (
        preferred_network_name,
        resolve_model_metadata,
        resolve_current_checkpoint_metadata,
        trigger_words_text,
    )
    from shared_paths import *

try:
    try:
        from scripts import tag_frequency_db as tdb
    except ModuleNotFoundError:
        from inspect import currentframe, getframeinfo
        filename = getframeinfo(currentframe()).filename
        parent = Path(filename).resolve().parent
        sys.path.append(str(parent))
        import tag_frequency_db as tdb

    # Ensure the db dependency is reloaded on script reload
    importlib.reload(tdb)

    db = tdb.TagFrequencyDb()
    if int(db.version) != int(tdb.db_ver):
        raise ValueError("Database version mismatch")
except (ImportError, ValueError, sqlite3.Error) as e:
    print(f"[Tag Autocomplete Neo] Tag frequency database error: {e}")
    db = None

def get_embed_db(sd_model=None):
    """Returns the embedding database from the Forge Neo text processing engine."""
    try:
        forge_model = sd_model if sd_model is not None else sd_models.model_data.get_sd_model()
        if forge_model is None:
            return None
        engine = getattr(
            forge_model,
            "text_processing_engine",
            getattr(forge_model, "text_processing_engine_l", None),
        )
        return getattr(engine, "embeddings", None) if engine is not None else None
    except Exception:
        return None

# Attempt to get embedding load function, using the same call as api.
try:
    embed_db = get_embed_db()
    if embed_db is not None:
        load_textual_inversion_embeddings = embed_db.load_textual_inversion_embeddings
    else:
        load_textual_inversion_embeddings = lambda *args, **kwargs: None
except Exception as e: # Not supported.
    load_textual_inversion_embeddings = lambda *args, **kwargs: None
    print("[Tag Autocomplete Neo] Cannot reload embeddings instantly:", e)

# Sorting functions for extra networks / embeddings stuff
sort_criteria = {
    "Name": lambda path, name, subpath: name.lower() if subpath else path.stem.lower(),
    "Date Modified (newest first)": lambda path, name, subpath: path.stat().st_mtime if path.exists() else name.lower(),
    "Date Modified (oldest first)": lambda path, name, subpath: path.stat().st_mtime if path.exists() else name.lower()
}

def sort_models(model_list, sort_method = None, name_has_subpath = False):
    """Sorts models according to the setting.
    
    Input: list of (full_path, display_name, {hash}) models. 
    Returns models in the format of name, sort key, meta.
    Meta is optional and can be a hash, version string or other required info.
    """
    if len(model_list) == 0:
        return model_list

    if sort_method is None:
        sort_method = getattr(shared.opts, "tac_modelSortOrder", "Name")

    # Get sorting method from dictionary
    sorter = sort_criteria.get(sort_method, sort_criteria["Name"])

    # During merging on the JS side we need to re-sort anyway, so here only the sort criteria are calculated.
    # The list itself doesn't need to get sorted at this point.
    if len(model_list[0]) > 2:
        results = [f'"{name}","{sorter(path, name, name_has_subpath)}",{meta}' for path, name, meta in model_list]
    else:
        results = [f'"{name}","{sorter(path, name, name_has_subpath)}"' for path, name in model_list]
    return results


def get_wildcards():
    """Returns a list of all wildcards. Works on nested folders."""
    wildcard_files = list(WILDCARD_PATH.rglob("*.txt"))
    resolved = [(w, w.relative_to(WILDCARD_PATH).as_posix())
                for w in wildcard_files
                if w.name != "put wildcards here.txt"
                and w.is_file()]
    return sort_models(resolved, name_has_subpath=True)


def get_ext_wildcards():
    """Returns a list of all extension wildcards. Works on nested folders."""
    wildcard_files = []
    excluded_folder_names = [s.strip() for s in getattr(shared.opts, "tac_wildcardExclusionList", "").split(",")]
    for path in WILDCARD_EXT_PATHS:
        wildcard_files.append(path.as_posix())
        resolved = [(w, w.relative_to(path).as_posix())
                    for w in path.rglob("*.txt")
                    if w.name != "put wildcards here.txt"
                    and not any(excluded in w.parts for excluded in excluded_folder_names)
                    and w.is_file()]
        wildcard_files.extend(sort_models(resolved, name_has_subpath=True))
        wildcard_files.append("-----")

    return wildcard_files

def is_umi_format(data):
    """Returns True if the YAML file is in UMI format."""
    issue_found = False
    for item in data:
        try:
            if not (data[item] and 'Tags' in data[item] and isinstance(data[item]['Tags'], list)):
                issue_found = True
                break
        except:
            issue_found = True
            break
    return not issue_found

count = 0
def parse_umi_format(umi_tags, data):
    global count
    for item in data:
        umi_tags[count] = ','.join(data[item]['Tags'])
        count += 1


def parse_dynamic_prompt_format(yaml_wildcards, data, path, root):
    # Recurse subkeys, delete those without string lists as values
    def recurse_dict(d: dict):
        for key, value in d.copy().items():
            if isinstance(value, dict):
                recurse_dict(value)
            elif (
                not isinstance(value, str)
                and not (isinstance(value, list) and all(isinstance(v, str) for v in value))
            ):
                del d[key]

    try:
        recurse_dict(data)
        # Preserve nested Dynamic Prompts folders in the YAML completion tree.
        for part in reversed(path.relative_to(root).parent.parts):
            data = {part: data}
        # Add to yaml_wildcards
        yaml_wildcards[path.name] = data
    except:
        return


def get_yaml_wildcards():
    """Returns a list of all tags found in extension YAML files found under a Tags: key."""
    yaml_files = []
    for path in WILDCARD_EXT_PATHS:
        yaml_files.extend((p, path) for p in path.rglob("*.yml") if p.is_file())
        yaml_files.extend((p, path) for p in path.rglob("*.yaml") if p.is_file())

    yaml_wildcards = {}

    umi_tags = {} # { tag: count }

    for path, root in yaml_files:
        try:
            with open(path, encoding="utf8") as file:
                data = yaml.safe_load(file)
                if (data):
                    if (is_umi_format(data)):
                        parse_umi_format(umi_tags, data)
                    else:
                        parse_dynamic_prompt_format(yaml_wildcards, data, path, root)
                else:
                    print('[Tag Autocomplete Neo] No data found in ' + path.name)
        except (yaml.YAMLError, UnicodeDecodeError, AttributeError, TypeError) as e:
            # YAML file not in wildcard format or couldn't be read
            print(f'[Tag Autocomplete Neo] Issue in parsing YAML file {path.name}: {e}')
            continue
        except Exception as e:
            # Something else went wrong, just skip
            continue

    # Sort by count
    umi_sorted = sorted(umi_tags.items(), key=lambda item: item[1], reverse=True)
    umi_output = []
    for tag, count in umi_sorted:
        umi_output.append(f"{tag},{count}")

    if (len(umi_output) > 0):
        write_to_temp_file('umi_tags.txt', umi_output)

    with open(TEMP_PATH.joinpath("wc_yaml.json"), "w", encoding="utf-8") as file:
        json.dump(yaml_wildcards, file, ensure_ascii=False)


def get_embeddings(sd_model):
    """Write a list of all embeddings with their version"""

    # Version constants
    V1_SHAPE = 768
    V2_SHAPE = 1024
    VXL_SHAPE = 2048
    emb_v1 = []
    emb_v2 = []
    emb_vXL = []
    emb_unknown = []
    results = []

    try:
        embed_db = get_embed_db(sd_model)
        # Re-register callback if needed
        global load_textual_inversion_embeddings
        if embed_db is not None and load_textual_inversion_embeddings != embed_db.load_textual_inversion_embeddings:
            load_textual_inversion_embeddings = embed_db.load_textual_inversion_embeddings
        
        loaded = embed_db.word_embeddings
        skipped = embed_db.skipped_embeddings

        # Add embeddings to the correct list
        for key, emb in (skipped | loaded).items():
            filename = getattr(emb, "filename", None)
            
            if filename is None:
                if emb.shape is None:
                    emb_unknown.append((Path(key), key, ""))
                elif emb.shape == V1_SHAPE:
                    emb_v1.append((Path(key), key, "v1"))
                elif emb.shape == V2_SHAPE:
                    emb_v2.append((Path(key), key, "v2"))
                elif emb.shape == VXL_SHAPE:
                    emb_vXL.append((Path(key), key, "vXL"))
                else:
                    emb_unknown.append((Path(key), key, ""))
            
            else:
                if emb.filename is None:
                    continue

                # Resolve symlinks and make the path absolute before calling
                # relative_to() — guards against #291 (relative/symlinked paths
                # that don't appear to be under EMB_PATH until resolved).
                emb_resolved = Path(emb.filename).resolve()
                emb_base = EMB_PATH.resolve()
                try:
                    rel = emb_resolved.relative_to(emb_base).as_posix()
                except ValueError:
                    # Path is genuinely outside EMB_PATH (e.g. symlink to
                    # another drive). Use just the filename as the display key.
                    rel = emb_resolved.name

                if emb.shape is None:
                    emb_unknown.append((emb_resolved, rel, ""))
                elif emb.shape == V1_SHAPE:
                    emb_v1.append((emb_resolved, rel, "v1"))
                elif emb.shape == V2_SHAPE:
                    emb_v2.append((emb_resolved, rel, "v2"))
                elif emb.shape == VXL_SHAPE:
                    emb_vXL.append((emb_resolved, rel, "vXL"))
                else:
                    emb_unknown.append((emb_resolved, rel, ""))

        results = sort_models(emb_v1) + sort_models(emb_v2) + sort_models(emb_vXL) + sort_models(emb_unknown)
    except AttributeError:
        print("[Tag Autocomplete Neo] Old webui version or unrecognized model shape, using fallback for embedding completion.")
        # Get a list of all embeddings in the folder
        all_embeds = [str(e.relative_to(EMB_PATH)) for e in EMB_PATH.rglob("*") if e.suffix in {".bin", ".pt", ".png",'.webp', '.jxl', '.avif'} and e.is_file()]
        # Remove files with a size of 0
        all_embeds = [e for e in all_embeds if EMB_PATH.joinpath(e).stat().st_size > 0]
        # Remove file extensions
        all_embeds = [e[:e.rfind('.')] for e in all_embeds]
        results = [e + "," for e in all_embeds]

    write_to_temp_file('emb.txt', results)

model_keyword_installed = write_model_keyword_path()


# Try to import the built-in Lora module for alias resolution.
# When available, model.get_alias() respects the user's "lora_preferred_name"
# setting ("Alias from file" = ss_output_name, "Filename" = filename stem).
# Its refresh function also owns the authoritative filesystem scan. A local
# fallback is used only when this host module is missing or returns no entries.
_lora_module = None
try:
    import sys
    from modules import extensions
    for builtin_name in ("Lora", "sd_forge_lora"):
        builtin_path = Path(extensions.extensions_builtin_dir).joinpath(builtin_name)
        if builtin_path.is_dir() and builtin_path.as_posix() not in sys.path:
            sys.path.append(builtin_path.as_posix())
    import lora as _lora_module  # pyright: ignore [reportMissingImports]
except Exception:
    pass


def _native_network_entries(base_path: Path | None) -> list[ModelCatalogEntry]:
    """Return Forge's authoritative LoRA catalog for ``base_path``."""
    if _lora_module is None or base_path is None:
        return []
    try:
        # Forge performs the filesystem walk, reads safetensors metadata and
        # detects duplicate/forbidden aliases in this single refresh.
        refresh = getattr(_lora_module, "list_available_loras", None)
        if callable(refresh):
            refresh()
        return [
            ModelCatalogEntry(
                path=Path(model.filename),
                alias=model.get_alias(),
                network_hash=getattr(model, "hash", "") or "",
            )
            for model in _lora_module.available_loras.values()
        ]
    except Exception:
        return []


def _fallback_network_entries(base_path: Path | None) -> list[ModelCatalogEntry]:
    """Compatibility discovery when the host does not expose a LoRA catalog."""
    if base_path is None or not base_path.exists():
        return []
    return [
        ModelCatalogEntry(path=path)
        for path in sorted(base_path.rglob("*"))
        if path.suffix.casefold() in MODEL_EXTENSIONS and path.is_file()
    ]


def _discover_networks(model_type: str, base_path: Path | None) -> tuple[ModelCatalogEntry, ...]:
    if base_path is None:
        model_file_catalog.clear(model_type)
        return ()
    entries = _native_network_entries(base_path) or _fallback_network_entries(base_path)
    return model_file_catalog.replace(model_type, base_path, entries)


def _forbidden_lora_aliases() -> set[str]:
    if _lora_module is None:
        return {"none", "addams"}
    try:
        return set(getattr(_lora_module, "forbidden_lora_aliases", set()))
    except Exception:
        return {"none", "addams"}


def _get_lora():
    """Publish and return the Forge LoRA catalog, with a filesystem fallback."""
    entries = _discover_networks("lora", LORA_PATH)
    if LYCO_PATH is not None and LORA_PATH is not None and LYCO_PATH.absolute() == LORA_PATH.absolute():
        model_file_catalog.replace("lyco", LYCO_PATH, entries)
    return entries


def _get_lyco():
    """Publish and return a separate legacy LyCORIS catalog when configured."""
    return _discover_networks("lyco", LYCO_PATH)


def _resolve_network_entry(model_type: str, identifier: str) -> ModelCatalogEntry | None:
    """Resolve from the published catalog, refreshing it once on a miss."""
    entry = model_file_catalog.find(model_type, identifier)
    if entry is not None:
        return entry

    if model_type == "lora":
        _get_lora()
    elif model_type == "lyco":
        if LYCO_PATH is not None and LORA_PATH is not None and LYCO_PATH.absolute() == LORA_PATH.absolute():
            _get_lora()
        else:
            _get_lyco()
    return model_file_catalog.find(model_type, identifier)


def is_visible(p: Path) -> bool:
    if getattr(shared.opts, "extra_networks_hidden_models", "When searched") != "Never":
        return True
    for part in p.parts:
        if part.startswith('.'):
            return False
    return True

def get_lora():
    """Write a list of all lora.

    Output format (CSV rows): "rel/path/name.ext","sort_key",hash,"alias"
    The alias column is what the frontend inserts into <lora:ALIAS:weight>.
    It mirrors the behaviour of Forge Neo's get_alias(): prefers ss_output_name
    unless the user has chosen "Filename" in the Extra Networks settings.
    """
    valid_loras = _get_lora()
    sort_method = getattr(shared.opts, "tac_modelSortOrder", "Name")
    sorter = sort_criteria.get(sort_method, sort_criteria["Name"])

    results = []
    for entry in valid_loras:
        l = entry.path
        provided_alias = entry.alias
        if not l.exists() or not l.is_file() or not is_visible(l):
            continue
        try:
            name = l.relative_to(LORA_PATH).as_posix()
        except ValueError:
            # Forge may expose additional --lora-dirs outside the primary root.
            name = l.name
        hash_val = get_lora_simple_hash(l) if model_keyword_installed else ""

        alias = preferred_network_name(
            l,
            provided_alias,
            forbidden_aliases=_forbidden_lora_aliases(),
        )

        sort_key = sorter(l, name, True)
        results.append(f'"{name}","{sort_key}",{hash_val},"{alias}"')

    return results


def get_lyco():
    """Write a list of all LyCORIS/LOHA.

    Output format (CSV rows): "rel/path/name.ext","sort_key",hash,"alias"
    Same alias logic as get_lora().
    """
    valid_lycos = _get_lyco()
    sort_method = getattr(shared.opts, "tac_modelSortOrder", "Name")
    sorter = sort_criteria.get(sort_method, sort_criteria["Name"])

    results = []
    for entry in valid_lycos:
        ly = entry.path
        provided_alias = entry.alias
        if not ly.exists() or not ly.is_file() or not is_visible(ly):
            continue
        try:
            name = ly.relative_to(LYCO_PATH).as_posix()
        except ValueError:
            name = ly.name
        hash_val = get_lora_simple_hash(ly) if model_keyword_installed else ""

        alias = preferred_network_name(
            ly,
            provided_alias,
            forbidden_aliases=_forbidden_lora_aliases(),
        )

        sort_key = sorter(ly, name, True)
        results.append(f'"{name}","{sort_key}",{hash_val},"{alias}"')

    return results

def get_style_names():
    try:
        style_names: list[str] = shared.prompt_styles.styles.keys()
        style_names = sorted(style_names, key=len, reverse=True)
        return style_names
    except Exception:
        return None

def write_tag_base_path():
    """Writes the tag base path to a fixed location temporary file"""
    with open(STATIC_TEMP_PATH.joinpath('tagAutocompletePath.txt'), 'w', encoding="utf-8") as f:
        f.write(TAGS_PATH.as_posix())


def write_to_temp_file(name, data):
    """Writes the given data to a temporary file"""
    with open(TEMP_PATH.joinpath(name), 'w', encoding="utf-8") as f:
        f.write(('\n'.join(data)))


csv_files = []
csv_files_withnone = []
def update_tag_files(*args, **kwargs):
    """Returns a list of all potential tag files"""
    global csv_files, csv_files_withnone
    files = [str(t.relative_to(TAGS_PATH)) for t in TAGS_PATH.glob("*.csv") if t.is_file()]
    csv_files = files
    csv_files_withnone = ["None"] + files

json_files = []
json_files_withnone = []
def update_json_files(*args, **kwargs):
    """Returns a list of all potential json files"""
    global json_files, json_files_withnone
    files = [str(j.relative_to(TAGS_PATH)) for j in TAGS_PATH.glob("*.json") if j.is_file()]
    json_files = files
    json_files_withnone = ["None"] + files


# Write the tag base path to a fixed location temporary file
# to enable the javascript side to find our files regardless of extension folder name
if not STATIC_TEMP_PATH.exists():
    STATIC_TEMP_PATH.mkdir(exist_ok=True)

write_tag_base_path()
update_tag_files()
update_json_files()

# Check if the temp path exists and create it if not
if not TEMP_PATH.exists():
    TEMP_PATH.mkdir(parents=True, exist_ok=True)

# Set up files to ensure the script doesn't fail to load them
# even if no wildcards or embeddings are found
write_to_temp_file('wc.txt', [])
write_to_temp_file('wce.txt', [])
write_to_temp_file('wc_yaml.json', [])
write_to_temp_file('umi_tags.txt', [])
write_to_temp_file('hyp.txt', [])
write_to_temp_file('lora.txt', [])
write_to_temp_file('lyco.txt', [])
write_to_temp_file('styles.txt', [])
# Only reload embeddings if the file doesn't exist, since they are already re-written on model load
if not TEMP_PATH.joinpath("emb.txt").exists():
    write_to_temp_file('emb.txt', [])

# Write embeddings to emb.txt if found
if EMB_PATH.exists():
    # Get embeddings after the model loaded callback
    script_callbacks.on_model_loaded(get_embeddings)

def refresh_embeddings(force: bool, *args, **kwargs):
    try:
        embed_db = get_embed_db()
        if embed_db is None:
            return
        # Call directly through embed_db to avoid the stale global binding that is
        # set to a no-op lambda when no model is loaded at import time.
        # Also removed the "any embeddings loaded?" guard so newly-added embeddings
        # are discovered even when the folder was previously empty (#297).
        # Note: Forge Neo removed the force_reload kwarg from this method.
        embed_db.load_textual_inversion_embeddings()
        get_embeddings(None)
    except Exception as e:
        print(f"[Tag Autocomplete Neo] Error refreshing embeddings: {e}")

def refresh_temp_files(*args, **kwargs):
    global WILDCARD_EXT_PATHS
    skip_wildcard_refresh = getattr(shared.opts, "tac_skipWildcardRefresh", False)
    if skip_wildcard_refresh:
        WILDCARD_EXT_PATHS = find_ext_wildcard_paths()
    write_temp_files(skip_wildcard_refresh)
    force_embed_refresh = getattr(shared.opts, "tac_forceRefreshEmbeddings", False)
    refresh_embeddings(force=force_embed_refresh)

def write_style_names(*args, **kwargs):
    styles = get_style_names()
    if styles:
        write_to_temp_file('styles.txt', styles)

def write_temp_files(skip_wildcard_refresh = False):
    # Write wildcards to wc.txt if found
    if WILDCARD_PATH.exists() and not skip_wildcard_refresh:
        try:
            # Attempt to create a relative path, but fall back to an absolute path if not possible
            relative_wildcard_path = WILDCARD_PATH.relative_to(FILE_DIR).as_posix()
        except ValueError:
            # If the paths are not relative, use the absolute path
            relative_wildcard_path = WILDCARD_PATH.as_posix()

        wildcards = [relative_wildcard_path] + get_wildcards()
        if wildcards:
            write_to_temp_file('wc.txt', wildcards)

    # Write extension wildcards to wce.txt if found
    if WILDCARD_EXT_PATHS is not None and not skip_wildcard_refresh:
        wildcards_ext = get_ext_wildcards()
        if wildcards_ext:
            write_to_temp_file('wce.txt', wildcards_ext)
        # Write yaml extension wildcards to umi_tags.txt and wc_yaml.json if found
        get_yaml_wildcards()

    if model_keyword_installed:
        try:
            load_hash_cache()
        except Exception as e:
            print(f"[Tag Autocomplete Neo] Warning: could not load hash cache: {e}")

    lora_exists = LORA_PATH is not None and LORA_PATH.exists()
    if lora_exists:
        try:
            lora = get_lora()
            if lora:
                write_to_temp_file('lora.txt', lora)
        except Exception as e:
            print(f"[Tag Autocomplete Neo] Warning: could not write lora.txt: {e}")

    # In Forge Neo, LYCO_PATH == LORA_PATH — LoRA and LyCORIS share one directory.
    # lora.txt already covers both; lyco.txt is not written separately.

    if model_keyword_installed:
        try:
            update_hash_cache()
        except Exception as e:
            print(f"[Tag Autocomplete Neo] Warning: could not update hash cache: {e}")

    if shared.prompt_styles is not None:
        write_style_names()

write_temp_files()

# Register autocomplete options
def on_ui_settings():
    TAC_SECTION = ("prompt_studio_neo", "Prompt Studio Neo")

    # Dictionary of function options and their explanations
    frequency_sort_functions = {
        "Logarithmic (weak)": "Will respect the base order and slightly prefer often used tags",
        "Logarithmic (strong)": "Same as Logarithmic (weak), but with a stronger bias",
        "Usage first": "Will list used tags by frequency before all others",
    }

    tac_options = {
        # Main tag file
        "tac_tagFile": shared.OptionInfo("danbooru.csv", "Tag filename", gr.Dropdown, lambda: {"choices": csv_files_withnone}, refresh=update_tag_files),
        # Active in settings
        "tac_active": shared.OptionInfo(True, "Enable Tag Autocompletion"),
        "tac_activeIn.txt2img": shared.OptionInfo(True, "Active in txt2img").needs_restart(),
        "tac_activeIn.img2img": shared.OptionInfo(True, "Active in img2img").needs_restart(),
        "tac_activeIn.negativePrompts": shared.OptionInfo(True, "Active in negative prompts").needs_restart(),
        "tac_activeIn.thirdParty": shared.OptionInfo(True, "Active in third party textboxes").info("See <a href=\"https://github.com/DominikDoom/a1111-sd-webui-tagcomplete#-features\" target=\"_blank\">README</a> for supported extensions").needs_restart(),
        "tac_activeIn.modelList": shared.OptionInfo("", "Black/Whitelist models").info("Model names [with file extension] or their hashes, separated by commas"),
        "tac_activeIn.modelListMode": shared.OptionInfo("Blacklist", "Mode to use for model list", gr.Dropdown, lambda: {"choices": ["Blacklist","Whitelist"]}),
        # Results related settings
        "tac_slidingPopup": shared.OptionInfo(True, "Move completion popup together with text cursor"),
        "tac_maxResults": shared.OptionInfo(5, "Maximum results"),
        "tac_showAllResults": shared.OptionInfo(False, "Show all results"),
        "tac_resultStepLength": shared.OptionInfo(100, "How many results to load at once"),
        "tac_delayTime": shared.OptionInfo(100, "Time in ms to wait before triggering completion again").needs_restart(),
        "tac_useIndexedSearch": shared.OptionInfo(True, "Use indexed search (faster, recommended for slower PCs / mobile)").info("Builds a prefix index for tags. Disable to use legacy full-scan mode if you experience missing results"),
        "tac_useWildcards": shared.OptionInfo(True, "Search for wildcards"),
        "tac_sortWildcardResults": shared.OptionInfo(True, "Sort wildcard file contents alphabetically").info("If your wildcard files have a specific custom order, disable this to keep it"),
        "tac_wildcardExclusionList": shared.OptionInfo("", "Wildcard folder exclusion list").info("Add folder names that shouldn't be searched for wildcards, separated by comma.").needs_restart(),
        "tac_skipWildcardRefresh": shared.OptionInfo(False, "Don't re-scan for wildcard files when pressing the extra networks refresh button").info("Useful to prevent hanging if you use a very large wildcard collection."),
        "tac_useEmbeddings": shared.OptionInfo(True, "Search for embeddings"),
        "tac_forceRefreshEmbeddings": shared.OptionInfo(False, "Force refresh embeddings when pressing the extra networks refresh button").info("Turn this on if you have issues with new embeddings not registering correctly in TAC. Warning: Seems to cause reloading issues in gradio for some users."),
        "tac_includeEmbeddingsInNormalResults": shared.OptionInfo(False, "Include embeddings in normal tag results").info("The 'JumpTo...' keybinds (End & Home key by default) will select the first non-embedding result of their direction on the first press for quick navigation in longer lists."),
        "tac_useLoras": shared.OptionInfo(True, "Search for Loras"),
        "tac_useLycos": shared.OptionInfo(True, "Search for LyCORIS/LoHa"),
        "tac_useLoraPrefixForLycos": shared.OptionInfo(True, "Use the '<lora:' prefix instead of '<lyco:' for models in the LyCORIS folder").info("The lyco prefix is included for backwards compatibility and not used anymore by default. Disable this if you are on an old webui version without built-in lyco support."),
        "tac_showWikiLinks": shared.OptionInfo(False, "Show '?' next to tags, linking to its Danbooru or e621 wiki page").info("Warning: This is an external site and very likely contains NSFW examples!"),
        "tac_showExtraNetworkPreviews": shared.OptionInfo(True, "Show preview thumbnails for extra networks if available"),
        "tac_modelSortOrder": shared.OptionInfo("Name", "Model sort order", gr.Dropdown, lambda: {"choices": list(sort_criteria.keys())}).info("Order for extra network models and wildcards in dropdown"),
        "tac_useStyleVars": shared.OptionInfo(False, "Search for webui style names").info("Suggests style names from the webui dropdown with '$'. Currently requires a secondary extension like <a href=\"https://github.com/SirVeggie/extension-style-vars\" target=\"_blank\">style-vars</a> to actually apply the styles before generating."),
        # Frequency sorting settings
        "tac_frequencySort": shared.OptionInfo(True, "Locally record tag usage and sort frequent tags higher").info("Will also work for extra networks, keeping the specified base order"),
        "tac_frequencyFunction": shared.OptionInfo("Logarithmic (weak)", "Function to use for frequency sorting", gr.Dropdown, lambda: {"choices": list(frequency_sort_functions.keys())}).info("; ".join([f'<b>{key}</b>: {val}' for key, val in frequency_sort_functions.items()])),
        "tac_frequencyMinCount": shared.OptionInfo(3, "Minimum number of uses for a tag to be considered frequent").info("Tags with less uses than this will not be sorted higher, even if the sorting function would normally result in a higher position."),
        "tac_frequencyMaxAge": shared.OptionInfo(30, "Maximum days since last use for a tag to be considered frequent").info("Similar to the above, tags that haven't been used in this many days will not be sorted higher. Set to 0 to disable."),
        "tac_frequencyRecommendCap": shared.OptionInfo(10, "Maximum number of recommended tags").info("Limits the maximum number of recommended tags to not drown out normal results. Set to 0 to disable."),
        "tac_frequencyIncludeAlias": shared.OptionInfo(False, "Frequency sorting matches aliases for frequent tags").info("Tag frequency will be increased for the main tag even if an alias is used for completion. This option can be used to override the default behavior of alias results being ignored for frequency sorting."),
        # Insertion related settings
        "tac_replaceUnderscores": shared.OptionInfo(True, "Replace underscores with spaces on insertion"),
        "tac_undersocreReplacementExclusionList": shared.OptionInfo("0_0,(o)_(o),+_+,+_-,._.,<o>_<o>,<|>_<|>,=_=,>_<,3_3,6_9,>_o,@_@,^_^,o_o,u_u,x_x,|_|,||_||", "Underscore replacement exclusion list").info("Add tags that shouldn't have underscores replaced with spaces, separated by comma."),
        "tac_escapeParentheses": shared.OptionInfo(True, "Escape parentheses on insertion"),
        "tac_appendComma": shared.OptionInfo(True, "Append comma on tag autocompletion"),
        "tac_appendSpace": shared.OptionInfo(True, "Append space on tag autocompletion").info("will append after comma if the above is enabled"),
        "tac_alwaysSpaceAtEnd": shared.OptionInfo(True, "Always append space if inserting at the end of the textbox").info("takes precedence over the regular space setting for that position"),
        "tac_modelKeywordCompletion": shared.OptionInfo("Never", "Try to add known trigger words for LORA/LyCO models", gr.Dropdown, lambda: {"choices": ["Never","Only user list","Always"]}).info("Uses Browser Neo metadata read-only when available, then Prompt Studio's own metadata cache.").needs_restart(),
        "tac_modelKeywordLocation": shared.OptionInfo("Start of prompt", "Where to insert the trigger keyword", gr.Dropdown, lambda: {"choices": ["Start of prompt","End of prompt","Before LORA/LyCO","After LORA/LyCO"]}).info("Only relevant if the above option is enabled"),
        "tac_modelKeywordCivitai": shared.OptionInfo(False, "Fetch trigger words from CivitAI when metadata is unavailable").info("Uses the single 'CivitAI API Key' setting above. Results are stored in Prompt Studio's internal cache; model sidecars are never modified."),
        "tac_artistAtTrigger": shared.OptionInfo(False, "Show Artist tags when typing '@'").info("Filters completions to Artist tags from the loaded tag set. The typed '@' is kept on insertion, so you get Anima's '@artist' syntax without needing the option below."),
        "tac_artistInsertAt": shared.OptionInfo(False, "Always add '@' before inserted Artist tags").info("Applies to all Artist tags, independently of the loaded checkpoint."),
        "tac_animaArtistPrefix": shared.OptionInfo("Off", "Auto-prefix Danbooru Artist tags with '@' for Anima checkpoints", gr.Dropdown, lambda: {"choices": ["Off", "Auto"]}).info("Uses Browser Neo metadata when available, then Prompt Studio's own cache/CivitAI lookup."),
        "tac_wildcardCompletionMode": shared.OptionInfo("To next folder level", "How to complete nested wildcard paths", gr.Dropdown, lambda: {"choices": ["To next folder level","To first difference","Always fully"]}).info("e.g. \"hair/colours/light/...\""),
        # Alias settings
        "tac_alias.searchByAlias": shared.OptionInfo(True, "Search by alias"),
        "tac_alias.onlyShowAlias": shared.OptionInfo(False, "Only show alias"),
        # Translation settings
        "tac_translation.translationFile": shared.OptionInfo("None", "Translation filename", gr.Dropdown, lambda: {"choices": csv_files_withnone}, refresh=update_tag_files),
        "tac_translation.oldFormat": shared.OptionInfo(False, "Translation file uses old 3-column translation format instead of the new 2-column one"),
        "tac_translation.searchByTranslation": shared.OptionInfo(True, "Search by translation"),
        "tac_translation.liveTranslation": shared.OptionInfo(False, "Show live tag translation below prompt ").info("WIP, expect some bugs"),
        # Extra file settings
        "tac_extra.extraFile": shared.OptionInfo("extra-quality-tags.csv", "Extra filename", gr.Dropdown, lambda: {"choices": csv_files_withnone}, refresh=update_tag_files).info("for small sets of custom tags"),
        "tac_extra.addMode": shared.OptionInfo("Insert before", "Mode to add the extra tags to the main tag list", gr.Dropdown, lambda: {"choices": ["Insert before","Insert after"]}),
        # Chant settings
        "tac_chantFile": shared.OptionInfo("demo-chants.json", "Chant filename", gr.Dropdown, lambda: {"choices": json_files_withnone}, refresh=update_json_files).info("Chants are longer prompt presets"),
    }

    # Add normal settings
    for key, opt in tac_options.items():
        opt.section = TAC_SECTION
        shared.opts.add_option(key, opt)

    # Settings that need special treatment
    # Custom mappings
    keymapDefault = """\
{
    "MoveUp": "ArrowUp",
    "MoveDown": "ArrowDown",
    "JumpUp": "PageUp",
    "JumpDown": "PageDown",
    "JumpToStart": "Home",
    "JumpToEnd": "End",
    "ChooseSelected": "Enter",
    "ChooseFirstOrSelected": "Tab",
    "Close": "Escape"
}\
"""
    colorDefault = """\
{
    "danbooru": {
        "-1": ["red", "maroon"],
        "0": ["lightblue", "dodgerblue"],
        "1": ["indianred", "firebrick"],
        "3": ["violet", "darkorchid"],
        "4": ["lightgreen", "darkgreen"],
        "5": ["orange", "darkorange"]
    },
    "e621": {
        "-1": ["red", "maroon"],
        "0": ["lightblue", "dodgerblue"],
        "1": ["gold", "goldenrod"],
        "3": ["violet", "darkorchid"],
        "4": ["lightgreen", "darkgreen"],
        "5": ["tomato", "darksalmon"],
        "6": ["red", "maroon"],
        "7": ["whitesmoke", "black"],
        "8": ["seagreen", "darkseagreen"]
    },
    "derpibooru": {
        "-1": ["red", "maroon"],
        "0": ["#60d160", "#3d9d3d"],
        "1": ["#fff956", "#918e2e"],
        "3": ["#fd9961", "#a14c2e"],
        "4": ["#cf5bbe", "#6c1e6c"],
        "5": ["#3c8ad9", "#1e5e93"],
        "6": ["#a6a6a6", "#555555"],
        "7": ["#47abc1", "#1f6c7c"],
        "8": ["#7871d0", "#392f7d"],
        "9": ["#df3647", "#8e1c2b"],
        "10": ["#c98f2b", "#7b470e"],
        "11": ["#e87ebe", "#a83583"]
    },
    "danbooru_e621_merged": {
        "-1": ["red", "maroon"],
        "0": ["lightblue", "dodgerblue"],
        "1": ["indianred", "firebrick"],
        "3": ["violet", "darkorchid"],
        "4": ["lightgreen", "darkgreen"],
        "5": ["orange", "darkorange"],
        "6": ["red", "maroon"],
        "7": ["lightblue", "dodgerblue"],
        "8": ["gold", "goldenrod"],
        "9": ["gold", "goldenrod"],
        "10": ["violet", "darkorchid"],
        "11": ["lightgreen", "darkgreen"],
        "12": ["tomato", "darksalmon"],
        "14": ["whitesmoke", "black"],
        "15": ["seagreen", "darkseagreen"]
    }
}\
"""
    keymapLabel = "Configure Hotkeys. For possible values, see https://www.w3.org/TR/uievents-key, or leave empty / set to 'None' to disable. Must be valid JSON."
    colorLabel = "Configure colors. See the Settings section in the README for more info. Must be valid JSON."

    try:
        shared.opts.add_option("tac_keymap", shared.OptionInfo(keymapDefault, keymapLabel, gr.Code, lambda: {"language": "json", "interactive": True}, section=TAC_SECTION))
        shared.opts.add_option("tac_colormap", shared.OptionInfo(colorDefault, colorLabel, gr.Code, lambda: {"language": "json", "interactive": True}, section=TAC_SECTION))
    except AttributeError:
        shared.opts.add_option("tac_keymap", shared.OptionInfo(keymapDefault, keymapLabel, gr.Textbox, section=TAC_SECTION))
        shared.opts.add_option("tac_colormap", shared.OptionInfo(colorDefault, colorLabel, gr.Textbox, section=TAC_SECTION))

    shared.opts.add_option("tac_refreshTempFiles", shared.OptionInfo("Refresh TAC temp files", "Refresh internal temp files", gr.HTML, {}, refresh=refresh_temp_files, section=TAC_SECTION))

script_callbacks.on_ui_settings(on_ui_settings)

def get_style_mtime():
    try:
        style_file = getattr(shared, "styles_filename", "styles.csv")
        # Check in case a list is returned
        if isinstance(style_file, list):
            style_file = style_file[0]
        
        style_file = Path(FILE_DIR).joinpath(style_file)
        if Path.exists(style_file):
            return style_file.stat().st_mtime
    except Exception:
        return None

last_style_mtime = get_style_mtime()

def api_tac(_: gr.Blocks, app: FastAPI):
    # Surface a persisted-off master switch loudly: with tac_active False the
    # frontend attaches to nothing and fails silently otherwise.
    try:
        if not getattr(shared.opts, "tac_active", True):
            print("[Tag Autocomplete Neo] WARNING: 'Enable Tag Autocompletion' (tac_active) is OFF — no autocomplete will appear. Re-enable it in Settings > Prompt Studio Neo.")
    except Exception:
        pass

    async def get_json_info(model_type: str, filename: str = None):
        entry = _resolve_network_entry(model_type, filename)
        if entry is None:
            return Response(status_code=404)

        try:
            info_path = find_companion_file(entry.path, (".json",))
            if info_path is not None:
                return FileResponse(info_path)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return Response(status_code=404)

    async def get_preview_thumbnail(model_type: str, filename: str = None, blob: bool = False):
        try:
            image_path = None
            if model_type in {"lora", "lyco"}:
                entry = _resolve_network_entry(model_type, filename)
                if entry is not None:
                    image_path = find_companion_file(entry.path, PREVIEW_EXTENSIONS)
            else:
                # Embeddings do not have a stable Forge catalog until a model is
                # loaded, so retain a compatibility lookup for that type only.
                base_path = get_path_for_type(model_type)
                if base_path is not None and base_path.exists():
                    img_glob = glob.glob(
                        base_path.as_posix() + f"/**/{glob.escape(filename)}.*",
                        recursive=True,
                    )
                    image_path = next(
                        (
                            Path(image)
                            for image in img_glob
                            if Path(image).suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                            and Path(image).is_file()
                        ),
                        None,
                    )

            if image_path is not None:
                if blob:
                    return FileResponse(image_path)
                return JSONResponse({"url": urllib.parse.quote(str(image_path))})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return Response(status_code=404)

    @app.post("/tacapi/v1/refresh-temp-files")
    async def api_refresh_temp_files():
        await sleep(0) # might help with refresh blocking gradio
        refresh_temp_files()

    @app.post("/tacapi/v1/refresh-embeddings")
    async def api_refresh_embeddings():
        refresh_embeddings(force=False)

    @app.get("/tacapi/v1/lora-info/{lora_name}")
    async def get_lora_info(lora_name):
        return await get_json_info("lora", lora_name)

    @app.get("/tacapi/v1/lyco-info/{lyco_name}")
    async def get_lyco_info(lyco_name):
        return await get_json_info("lyco", lyco_name)

    @app.get("/tacapi/v1/civitai-trigger-words/{lora_name}")
    async def get_civitai_trigger_words(lora_name: str):
        """Look up trigger words for a LoRA from CivitAI by-hash API.

        Priority:
          1. Reuse Browser Neo metadata read-only when available.
          2. Return Prompt Studio's own cached result when valid.
          3. Call CivitAI and persist only to Prompt Studio's internal cache.
        """
        if LORA_PATH is None or not LORA_PATH.exists():
            return Response(status_code=404)

        entry = _resolve_network_entry("lora", lora_name)
        if entry is None:
            return Response(status_code=404)

        try:
            metadata = resolve_model_metadata(
                entry.path,
                model_type="lora",
                required_fields=("trigger_words",),
                allow_network=True,
                calculate_hash=True,
            )
            trained_words = trigger_words_text(metadata)
            if not trained_words:
                return Response(status_code=404)
            return JSONResponse({"trainedWords": trained_words})
        except Exception as e:
            print(f"[Tag Autocomplete Neo] CivitAI trigger word lookup failed: {e}")
            return Response(status_code=500)

    @app.get("/tacapi/v1/current-checkpoint-basemodel")
    async def get_current_checkpoint_basemodel():
        """Return the selected checkpoint family through shared metadata."""
        try:
            metadata = resolve_current_checkpoint_metadata(
                required_fields=("base_model",),
                allow_network=True,
            )
            base_model = metadata.get("base_model", "")
            if not base_model:
                return Response(status_code=404)
            return JSONResponse({"baseModel": base_model})
        except Exception as e:
            print(f"[Tag Autocomplete Neo] Checkpoint base model lookup failed: {e}")
            return Response(status_code=500)

    @app.get("/tacapi/v1/lora-cached-hash/{lora_name}")
    async def get_lora_cached_hash(lora_name: str):
        entry = _resolve_network_entry("lora", lora_name)
        if entry is not None:
            if entry.network_hash:
                return entry.network_hash
            path = str(entry.path)
            cached_hash = hashes.sha256_from_cache(
                path,
                f"lora/{lora_name}",
                path.endswith(".safetensors"),
            )
            if cached_hash is not None:
                return cached_hash
        
        return None

    def get_path_for_type(type):
        if type == "lora":
            return LORA_PATH
        elif type == "lyco":
            return LYCO_PATH
        elif type == "embedding":
            return EMB_PATH
        else:
            return None

    @app.get("/tacapi/v1/thumb-preview/{filename}")
    async def get_thumb_preview(filename, type):
        return await get_preview_thumbnail(type, filename, False)

    @app.get("/tacapi/v1/thumb-preview-blob/{filename}")
    async def get_thumb_preview_blob(filename, type):
        return await get_preview_thumbnail(type, filename, True)

    @app.get("/tacapi/v1/wildcard-contents")
    async def get_wildcard_contents(basepath: str, filename: str):
        if basepath is None or basepath == "":
            return Response(status_code=404)

        base = Path(basepath)
        if base is None or (not base.exists()):
            return Response(status_code=404)

        try:
            wildcard_path = base.joinpath(filename)
            if wildcard_path.exists() and wildcard_path.is_file():
                return FileResponse(wildcard_path)
            else:
                return Response(status_code=404)
        except Exception as e:
            return JSONResponse({"error": e}, status_code=500)

    @app.get("/tacapi/v1/refresh-styles-if-changed")
    async def refresh_styles_if_changed():
        global last_style_mtime
        
        mtime = get_style_mtime()
        if mtime is not None and mtime > last_style_mtime:
            last_style_mtime = mtime
            # Update temp file
            if shared.prompt_styles is not None:
                write_style_names()
            
            return Response(status_code=200) # Success
        else:
            return Response(status_code=304) # Not modified
    def db_request(func, get = False):
        if db is not None:
            try:
                if get:
                    ret = func()
                    if ret is list:
                        ret = [{"name": t[0], "type": t[1], "count": t[2], "lastUseDate": t[3]} for t in ret]
                    return JSONResponse({"result": ret})
                else:
                    func()
            except sqlite3.Error as e:
                return JSONResponse({"error": e.__cause__}, status_code=500)
        else:
            return JSONResponse({"error": "Database not initialized"}, status_code=500)

    @app.post("/tacapi/v1/increase-use-count")
    async def increase_use_count(tagname: str, ttype: int, neg: bool):
        db_request(lambda: db.increase_tag_count(tagname, ttype, neg))

    @app.get("/tacapi/v1/get-use-count")
    async def get_use_count(tagname: str, ttype: int, neg: bool):
        return db_request(lambda: db.get_tag_count(tagname, ttype, neg), get=True)
    
    # Small dataholder class
    class UseCountListRequest(BaseModel):
        tagNames: list[str]
        tagTypes: list[int]
        neg: bool = False

    # Semantically weird to use post here, but it's required for the body on js side
    @app.post("/tacapi/v1/get-use-count-list")
    async def get_use_count_list(body: UseCountListRequest):
        # If a date limit is set > 0, pass it to the db
        date_limit = getattr(shared.opts, "tac_frequencyMaxAge", 30)
        date_limit = date_limit if date_limit > 0 else None

        if db:
            count_list = list(db.get_tag_counts(body.tagNames, body.tagTypes, body.neg, date_limit))
        else:
            count_list = None
    
        # If a limit is set, return at max the top n results by count
        if count_list and len(count_list):
            limit = int(min(getattr(shared.opts, "tac_frequencyRecommendCap", 10), len(count_list)))
            # Sort by count and return the top n
            if limit > 0:
                count_list = sorted(count_list, key=lambda x: x[2], reverse=True)[:limit]

        return db_request(lambda: count_list, get=True)

    @app.put("/tacapi/v1/reset-use-count")
    async def reset_use_count(tagname: str, ttype: int, pos: bool, neg: bool):
        db_request(lambda: db.reset_tag_count(tagname, ttype, pos, neg))

    @app.get("/tacapi/v1/get-all-use-counts")
    async def get_all_tag_counts():
        return db_request(lambda: db.get_all_tags(), get=True)

script_callbacks.on_app_started(api_tac)
