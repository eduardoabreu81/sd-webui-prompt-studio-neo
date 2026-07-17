"""
Prompt Studio Neo — Live Tag Colorizer (Stage 1) settings.

Registers options in the shared "Prompt Studio Neo" settings section. The actual
colorizing is done client-side by javascript/promptColorizer.js, which reads these
options via the global `opts` object. Nothing is hardcoded: colors come from the
existing `tac_colormap` setting (Danbooru types) plus the options below.

Master switch defaults OFF, so this is inert until the user opts in.
"""
import gradio as gr

from modules import shared, script_callbacks

SECTION = ("prompt_studio_neo", "Prompt Studio Neo")

WILDCARD_DEFAULT = '["#e6b422", "#8a6300"]'  # [dark, light]; TAC has no wildcard color of its own


def on_ui_settings():
    shared.opts.add_option(
        "psn_colorizer_enabled",
        shared.OptionInfo(
            False,
            "Live Tag Colorizer — colorize prompt text by tag type (LoRA / embedding / "
            "wildcard / Danbooru type). Reuses the TAC color map. Restart or reload UI after enabling.",
            section=SECTION,
        ),
    )
    shared.opts.add_option(
        "psn_colorizer_anima_at",
        shared.OptionInfo(
            "Always",
            "Colorizer: treat a leading @ as an artist (Anima convention)",
            gr.Dropdown,
            {"choices": ["Always", "Off"]},
            section=SECTION,
        ).info("Anima checkpoints write artist tags as @artist"),
    )
    shared.opts.add_option(
        "psn_colorizer_wildcard_color",
        shared.OptionInfo(
            WILDCARD_DEFAULT,
            "Colorizer: wildcard color as JSON [darkThemeColor, lightThemeColor]",
            gr.Code,
            lambda: {"language": "json", "interactive": True},
            section=SECTION,
        ).info("TAC does not color wildcards natively — this is the colorizer's own choice"),
    )


script_callbacks.on_ui_settings(on_ui_settings)


# --- PAIO group colors -> served to the frontend so the colorizer can use them ---
# PAIO group colors live in group_tags/*.yaml (rgba, meant as chip backgrounds). We
# turn them into text colors: opaque, with a light-theme variant darkened for contrast
# on white. Served at /prompt-studio-neo/paio-colors as { tag: [darkColor, lightColor] }.
import os
import re

_GT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "group_tags")


def _gt_read(name):
    try:
        with open(os.path.join(_GT_DIR, name + ".yaml"), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _gt_resolve_text():
    # mirror get_group_tags.py: custom overrides default; prepend/append wrap it
    custom = _gt_read("custom")
    base = custom if custom.strip() else _gt_read("default")
    return _gt_read("prepend") + "\n\n" + base + "\n\n" + _gt_read("append")


def _parse_rgb(c):
    c = str(c).strip()
    m = re.match(r"rgba?\(([^)]+)\)", c)
    if m:
        p = [x.strip() for x in m.group(1).split(",")]
        return (int(float(p[0])), int(float(p[1])), int(float(p[2])))
    m = re.match(r"#([0-9a-fA-F]{6})$", c)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.match(r"#([0-9a-fA-F]{3})$", c)
    if m:
        return tuple(int(ch * 2, 16) for ch in m.group(1))
    return None


def _lum(rgb):
    def f(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _pair(c):
    """[darkThemeColor, lightThemeColor]: dark = original opaque; light = darkened to ~4.5:1 on white."""
    rgb = _parse_rgb(c)
    if rgb is None:
        return [c, c]
    dark = "rgb(%d,%d,%d)" % rgb
    r, g, b = rgb
    for _ in range(24):
        if 1.05 / (_lum((r, g, b)) + 0.05) >= 4.5:
            break
        r, g, b = r * 0.85, g * 0.85, b * 0.85
    return [dark, "rgb(%d,%d,%d)" % (round(r), round(g), round(b))]


_PAIO_COLORS = None


def _build_paio_colors():
    try:
        import yaml
    except Exception as e:
        print("[PSN colorizer] PyYAML unavailable, PAIO colors disabled:", e)
        return {}
    try:
        data = yaml.safe_load(_gt_resolve_text())
    except Exception as e:
        print("[PSN colorizer] group_tags parse error:", e)
        return {}
    tag2color = {}
    for cat in data or []:
        if not isinstance(cat, dict):
            continue
        for g in (cat.get("groups") or []):
            if not isinstance(g, dict):
                continue
            color, tags = g.get("color"), g.get("tags")
            if color and tags:
                for t in tags:
                    # group_tags/*.yaml is inconsistent (spaces vs underscores) — canonicalize
                    key = re.sub(r"\s+", "_", str(t).strip().lower())
                    if key:
                        tag2color[key] = color
    uniq = {c: _pair(c) for c in set(tag2color.values())}
    return {t: uniq[c] for t, c in tag2color.items()}


def _get_paio_colors():
    global _PAIO_COLORS
    if _PAIO_COLORS is None:
        _PAIO_COLORS = _build_paio_colors()
    return _PAIO_COLORS


def on_app_started(_demo, app):
    @app.get("/prompt-studio-neo/paio-colors")
    def _paio_colors():
        return _get_paio_colors()


script_callbacks.on_app_started(on_app_started)
