import os

from scripts.model_metadata_service import (
    get_current_checkpoint_path,
    resolve_model_metadata,
    seed_model_metadata,
)
from scripts.physton_prompt.storage import Storage

# ---------------------------------------------------------------------------
# Storage key
# ---------------------------------------------------------------------------
STORAGE_KEY = 'qualityPresets'

def _default_builtin_enabled() -> dict:
    """Return a dict with all BUILTIN_TEMPLATES keys set to True."""
    # Evaluated lazily (after BUILTIN_TEMPLATES is defined) via load_presets().
    return {k: True for k in BUILTIN_TEMPLATES}

DEFAULT_PRESETS = {
    # NOTE: CivitAI API key is stored in WebUI settings and resolved by the
    # shared model metadata service.
    # builtin_enabled: per-family toggle (True = use this family's template by default)
    # Populated at runtime from BUILTIN_TEMPLATES keys; missing keys default to True.
    'builtin_enabled': {},
    # builtin_overrides: user-edited tags per family (values follow same schema as BUILTIN_TEMPLATES)
    'builtin_overrides': {},
    # Legacy PAIO cache, retained only so the shared service can migrate older
    # installations lazily. New metadata is stored in model_metadata_cache.json.
    'checkpoint_cache': {},
    'presets': [],
}

# ---------------------------------------------------------------------------
# Built-in template mapping  CivitAI baseModel  →  quality tag blocks
#
# Sources (research summary):
#
#  SD 1.5 / SDXL generic
#    - "masterpiece, best quality" are Danbooru aesthetic-tier tags; useful but
#      less impactful in SDXL. Negative: classic "worst quality, low quality, lowres"
#    https://civitai.com/articles/19069
#
#  NoobAI-XL
#    - Team explicitly mapped percentile tiers in the dataset:
#        masterpiece (>95th), best quality (85-95th), good quality (60-85th),
#        normal quality (30-60th), worst quality (<30th)
#    - Official recommended prefix: "masterpiece, best quality, newest, absurdres, highres"
#    - Official negative: "worst quality, old, early, low quality, lowres"
#    https://civitai.com/models/833294/noobai-xl-nai-xl
#
#  Pony Diffusion V6 XL
#    - Uses score_* tags from the Derpibooru rating system (not Danbooru aesthetic tiers)
#    - Full chain: score_9 > score_8_up > score_7_up > score_6_up > score_5_up > score_4_up
#    - Best practice: score_9, score_8_up, score_7_up (top 3 is sufficient)
#    https://civitai.com/models/257749/pony-diffusion-v6-xl
#
#  Illustrious XL (v0.1 / 1.0 / 2.0)
#    - "Raw" base model without strong aesthetic curation; masterpiece still works
#      in tag-style prompts. Most visual quality comes from LoRA and prompt content.
#    https://civitai.com/models/1232765/illustrious-xl-10
#
#  Flux.1 (dev / schnell / pro)
#    - Trained on natural-language captions, not Danbooru tag-style data.
#    - Quality tags have minimal effect; quality comes from descriptive prose prompts.
#    - Use concrete defect terms in negative (e.g. "extra fingers") rather than
#      generic quality tags.
#    https://education.civitai.com/quickstart-guide-to-flux-1/
# ---------------------------------------------------------------------------
BUILTIN_TEMPLATES = {
    # --- Pony ---
    'Pony': {
        'positive_prefix': ['score_9', 'score_8_up', 'score_7_up'],
        'negative_prefix': ['score_1', 'score_2', 'score_3'],
    },

    # --- NoobAI (NoobAI-XL) ---
    'NoobAI': {
        'positive_prefix': ['masterpiece', 'best quality', 'newest', 'absurdres', 'highres'],
        'negative_prefix': ['worst quality', 'old', 'early', 'low quality', 'lowres'],
    },

    # --- Illustrious XL ---
    'Illustrious': {
        'positive_prefix': ['masterpiece', 'best quality'],
        'negative_prefix': ['worst quality', 'low quality', 'lowres'],
    },

    # --- SDXL variants (generic, no dataset-specific quality tags) ---
    'SDXL 1.0': {
        'positive_prefix': ['masterpiece', 'best quality'],
        'negative_prefix': ['worst quality', 'low quality', 'lowres'],
    },
    'SDXL Turbo': {
        'positive_prefix': ['masterpiece', 'best quality'],
        'negative_prefix': ['worst quality', 'low quality', 'lowres'],
    },
    'SDXL Lightning': {
        'positive_prefix': ['masterpiece', 'best quality'],
        'negative_prefix': ['worst quality', 'low quality', 'lowres'],
    },

    # --- SD 1.5 / 2.x ---
    'SD 1.5': {
        'positive_prefix': ['masterpiece', 'best quality'],
        'negative_prefix': ['worst quality', 'low quality', 'lowres'],
    },
    'SD 2.1': {
        'positive_prefix': ['masterpiece', 'best quality'],
        'negative_prefix': ['worst quality', 'low quality', 'lowres'],
    },

    # --- Flux.1 — no quality tags; natural language captions preferred ---
    'Flux.1 D': {
        'positive_prefix': [],
        'negative_prefix': [],
    },
    'Flux.1 S': {
        'positive_prefix': [],
        'negative_prefix': [],
    },

    # --- Anima (CircleStone Labs + Comfy Org, Qwen-3 text encoder) ---
    #   Official README recommendation:
    #     positive prefix: "masterpiece, best quality, score_7, safe, "
    #     negative: "worst quality, low quality, score_1, score_2, score_3, artist name"
    #   https://huggingface.co/circlestone-labs/Anima
    'Anima': {
        'positive_prefix': ['masterpiece', 'best quality', 'score_7', 'safe'],
        'negative_prefix': ['worst quality', 'low quality', 'score_1', 'score_2', 'score_3', 'artist name'],
    },
}

# ---------------------------------------------------------------------------
# Filename-based family detection (fallback when CivitAI has no answer,
# e.g. local merges or models not hosted on CivitAI).
#
# Matching is token-based (split on non-alphanumeric) instead of substring,
# so 'anima-base-v1.0' matches Anima but 'animagine-xl' does not.
# ---------------------------------------------------------------------------
_FILENAME_FAMILY_TOKENS = {
    'anima': 'Anima',
}


def detect_family_by_filename(filename_stem: str) -> str:
    """Return a BUILTIN_TEMPLATES family for a checkpoint filename stem, or ''."""
    import re as _re
    tokens = _re.split(r'[^a-z0-9]+', filename_stem.lower())
    for token in tokens:
        family = _FILENAME_FAMILY_TOKENS.get(token)
        if family:
            return family
    return ''

# ---------------------------------------------------------------------------
# Shared metadata backend compatibility wrapper
# ---------------------------------------------------------------------------

def get_sha256_from_file(filepath: str, calculate: bool = False) -> str:
    """Return the shared resolver's SHA-256 value for a checkpoint."""
    metadata = resolve_model_metadata(
        filepath,
        model_type='checkpoint',
        allow_network=False,
        calculate_hash=calculate,
    )
    return metadata.get('sha256', '')


# ---------------------------------------------------------------------------
# Preset storage helpers
# ---------------------------------------------------------------------------

def load_presets() -> dict:
    data = Storage.get(STORAGE_KEY)
    if not isinstance(data, dict):
        data = {}
    data.setdefault('presets', [])
    data.setdefault('builtin_overrides', {})
    data.setdefault('checkpoint_cache', {})
    # Ensure every known family is present in builtin_enabled (default True)
    enabled = data.get('builtin_enabled', {})
    for key in BUILTIN_TEMPLATES:
        enabled.setdefault(key, True)
    data['builtin_enabled'] = enabled
    return data


def save_presets(data: dict) -> None:
    Storage.set(STORAGE_KEY, data)


# ---------------------------------------------------------------------------
# Core detection: given a checkpoint filepath, resolve the matching preset
# or fall back to a built-in template.
# ---------------------------------------------------------------------------

def detect_preset_for_checkpoint(filepath: str) -> dict:
    """
    Examine the checkpoint at `filepath` and return the best-matching quality
    preset as a dict:

        {
            'source':           'civitai' | 'preset' | 'builtin' | 'none',
            'base_model':       str,        # raw CivitAI baseModel value or ''
            'preset_name':      str,        # name of matched user preset or ''
            'positive_prefix':  [str, ...],
            'negative_prefix':  [str, ...],
            'positive_embeddings': [str, ...],
            'negative_embeddings': [str, ...],
            'auto_insert':      bool,
        }

    Detection order:
      1. Shared metadata cache/sidecars, then CivitAI by-hash → built-in template
      2. Filename substring  →  user preset match_substr
      3. No match → source='none', empty tag lists
    """
    result = {
        'source': 'none',
        'base_model': '',
        'preset_name': '',
        'positive_prefix': [],
        'negative_prefix': [],
        'positive_embeddings': [],
        'negative_embeddings': [],
        'auto_insert': False,
    }

    if not filepath or not os.path.exists(filepath):
        return result

    filename_stem = os.path.splitext(os.path.basename(filepath))[0].lower()

    storage = load_presets()
    user_presets = storage.get('presets', [])

    # -- Step 1a: exact filename match in user presets ----------------------
    for preset in user_presets:
        exact_list = [e.lower() for e in preset.get('match_exact', [])]
        if filename_stem in exact_list:
            result.update({
                'source': 'preset',
                'preset_name': preset.get('name', ''),
                'positive_prefix': preset.get('positive_prefix', []),
                'negative_prefix': preset.get('negative_prefix', []),
                'positive_embeddings': preset.get('positive_embeddings', []),
                'negative_embeddings': preset.get('negative_embeddings', []),
                'auto_insert': preset.get('auto_insert', False),
            })
            return result

    # -- Step 1b: shared metadata lookup -------------------------------------
    metadata = resolve_model_metadata(
        filepath,
        model_type='checkpoint',
        allow_network=False,
        calculate_hash=False,
    )

    # Import the previous PAIO cache lazily so existing users do not lose a
    # completed checkpoint scan during the backend migration.
    sha256 = metadata.get('sha256', '')
    legacy_cached = storage.get('checkpoint_cache', {}).get(sha256, {}) if sha256 else {}
    if not metadata.get('base_model') and legacy_cached.get('base_model'):
        metadata = seed_model_metadata(
            filepath,
            {
                'sha256': sha256,
                'base_model': legacy_cached.get('base_model', ''),
            },
            model_type='checkpoint',
        )

    if not metadata.get('base_model'):
        metadata = resolve_model_metadata(
            filepath,
            model_type='checkpoint',
            required_fields=('base_model',),
            allow_network=True,
            calculate_hash=True,
        )

    base_model = metadata.get('base_model', '')

    if base_model:
        result['base_model'] = base_model
        # Check if any user preset overrides this base model family
        for preset in user_presets:
            substr_list = [s.lower() for s in preset.get('match_substr', [])]
            if any(s in filename_stem for s in substr_list):
                result.update({
                    'source': 'preset',
                    'preset_name': preset.get('name', ''),
                    'positive_prefix': preset.get('positive_prefix', []),
                    'negative_prefix': preset.get('negative_prefix', []),
                    'positive_embeddings': preset.get('positive_embeddings', []),
                    'negative_embeddings': preset.get('negative_embeddings', []),
                    'auto_insert': preset.get('auto_insert', False),
                })
                return result

        # Fall back to built-in template for this baseModel (if enabled)
        enabled = storage.get('builtin_enabled', {})
        if enabled.get(base_model, True):
            overrides = storage.get('builtin_overrides', {})
            template = overrides.get(base_model) or BUILTIN_TEMPLATES.get(base_model)
            if template:
                result.update({
                    'source': 'civitai',
                    'positive_prefix': list(template.get('positive_prefix', [])),
                    'negative_prefix': list(template.get('negative_prefix', [])),
                    'auto_insert': False,   # built-in templates are suggested, not auto-inserted
                })
                return result

    # -- Step 2: filename substring match against user presets --------------
    for preset in user_presets:
        substr_list = [s.lower() for s in preset.get('match_substr', [])]
        if any(s in filename_stem for s in substr_list):
            result.update({
                'source': 'preset',
                'preset_name': preset.get('name', ''),
                'positive_prefix': preset.get('positive_prefix', []),
                'negative_prefix': preset.get('negative_prefix', []),
                'positive_embeddings': preset.get('positive_embeddings', []),
                'negative_embeddings': preset.get('negative_embeddings', []),
                'auto_insert': preset.get('auto_insert', False),
            })
            return result

    # -- Step 3: filename-token family detection (no CivitAI answer) --------
    family = detect_family_by_filename(filename_stem)
    if family:
        result['base_model'] = result['base_model'] or family
        enabled = storage.get('builtin_enabled', {})
        if enabled.get(family, True):
            overrides = storage.get('builtin_overrides', {})
            template = overrides.get(family) or BUILTIN_TEMPLATES.get(family)
            if template:
                result.update({
                    'source': 'builtin',
                    'positive_prefix': list(template.get('positive_prefix', [])),
                    'negative_prefix': list(template.get('negative_prefix', [])),
                    'auto_insert': False,
                })
                return result

    # -- No match -----------------------------------------------------------
    return result


# ---------------------------------------------------------------------------
# WebUI integration: installed checkpoints list
# ---------------------------------------------------------------------------

def get_installed_checkpoints() -> list:
    """
    Return a list of dicts for every checkpoint known to the WebUI:
        [
            {
                'filename':   str,   # basename with extension
                'filepath':   str,   # absolute path
                'title':      str,   # display name (model title or filename stem)
                'sha256':     str,   # from shared metadata sources, or ''
                'base_model': str,   # from shared metadata sources, or ''
            },
            ...
        ]
    """
    storage = load_presets()
    legacy_cache = storage.get('checkpoint_cache', {})

    checkpoints = []
    try:
        from modules import sd_models
        for info in sd_models.checkpoints_list.values():
            filepath = getattr(info, 'filename', '')
            filename = os.path.basename(filepath)
            title    = getattr(info, 'title', '') or os.path.splitext(filename)[0]
            metadata = resolve_model_metadata(
                filepath,
                model_type='checkpoint',
                allow_network=False,
                calculate_hash=False,
            ) if filepath else {}
            sha256 = metadata.get('sha256', '')
            legacy_cached = legacy_cache.get(sha256, {}) if sha256 else {}
            if not metadata.get('base_model') and legacy_cached.get('base_model'):
                metadata = seed_model_metadata(
                    filepath,
                    {
                        'sha256': sha256,
                        'base_model': legacy_cached.get('base_model', ''),
                    },
                    model_type='checkpoint',
                )
            checkpoints.append({
                'filename':   filename,
                'filepath':   filepath,
                'title':      title,
                'sha256':     sha256,
                'base_model': metadata.get('base_model', ''),
            })
    except Exception:
        pass
    return checkpoints


def scan_checkpoint(filepath: str) -> dict:
    """
    Resolve a checkpoint through shared metadata sources and, if necessary,
    CivitAI. Only Prompt Studio's own metadata cache is updated.
    Returns {'filename', 'sha256', 'base_model'} — empty strings on failure.
    """
    metadata = resolve_model_metadata(
        filepath,
        model_type='checkpoint',
        required_fields=('base_model',),
        allow_network=True,
        calculate_hash=True,
    )

    return {
        'filename':   os.path.basename(filepath),
        'sha256':     metadata.get('sha256', ''),
        'base_model': metadata.get('base_model', ''),
    }
