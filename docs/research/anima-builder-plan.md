# Plan: Prompt Profiles + Anima Builder (v2)

> Date: 2026-06-12 · Supersedes the MVP described in `ideogram-anima-prompt-builder-discussion.md` §7.
> Status: Stage B0 partially shipped (Anima quality template + filename detection + per-checkpoint pinning).

## Key research corrections to the original discussion

1. **Anima's official prompt grammar** ([README](https://huggingface.co/circlestone-labs/Anima)) is an *ordered tag structure* — `[quality/meta/year/safety] [count] [character] [series] [@artist] [general]` — with official prefix `masterpiece, best quality, score_7, safe, `. The key-value/JSON style is a community technique, not the documented baseline. A builder must support the official format first.
2. **Artist tags require `@` prefix** (weak effect without it); tags are lowercase with spaces (underscores only in `score_*`); prompt weighting needs higher values than SDXL (e.g. `(chibi:2)`); multi-character prompts must *describe each character's appearance*, not just name them.
3. **Licensing**: KJNodes is GPL-3.0 and ComfyUI-AnimaTool is AGPL-3.0 — no code can be copied into this MIT repo. The Ideogram 4 JSON schema is officially documented under Apache-2.0 (`ideogram-oss/ideogram4/docs/prompting.md`) and will be **reimplemented from spec**.
4. **Ideogram 4 in Forge Neo**: issue #1218 still open, official integration planned by the maintainer ("after PiD"). Wait for it; the unofficial isolated-env extension is not a foundation to build on.

## Core concept: Prompt Profiles

A thin layer that changes *grammar and defaults*, never data:

| Profile | TAC | PAIO | Builder |
|---|---|---|---|
| **Classic** (default) | as today | as today | — |
| **Anima** | same `danbooru.csv`, insertion uses spaces, `@` helper for artists | group tags unchanged; Quality Presets Anima template | structured panel (fields mirror official taxonomy) |
| **Ideogram** (future) | active only inside builder desc fields | chip UI collapses for JSON prompts | bbox canvas, reimplemented from Apache-2.0 spec, KJNodes-compatible import/export |

Profile selection: auto-detect from loaded checkpoint (filename token / CivitAI family) with manual override. Default = Classic ⇒ zero behavior change unless opted in.

## Stages

- **B0 — Foundations (shipped 2026-06-12):** `Anima` family in Quality Presets BUILTIN_TEMPLATES (official tags); token-based filename family detection fallback (`anima` token, avoids "animagine" false positive); 📌 **Pin tags** — save positive/negative pinned to an exact checkpoint (`match_exact`, highest detection priority), pre-filled from the family template.
- **A — Validation (user):** test Anima template detection + pinning locally; test how PAIO chips handle multiline prompts (decides key-value output availability).
- **B — Anima Builder core:** inline panel (txt2img *and* img2img), semantic fields (quality/year/safety dropdowns, count, character blocks with name+series+appearance, auto-`@` artist, general tags, environment, negative), TAC autocomplete attached to builder fields via `thirdParty` registry, serialization in Python only (single source of truth) with rule-based validation warnings, settings toggle.
- **C — Persistence:** builder presets via PAIO Storage; import/export; auto-show builder when Anima checkpoint detected.
- **D — Ideogram adapter:** when official Forge Neo support lands; builder becomes a framework with per-model adapters.

Out of scope (unchanged): LLM prompt helper — design leaves an endpoint hook only.

## Per-checkpoint pinned tags (user idea, shipped in B0)

Quality Presets already supported `match_exact` in the backend; the UI only exposed substring matching. Added: a 📌 Pin tags button per checkpoint (Checkpoints tab) that pre-fills a custom preset pinned to that exact file, with tags from the detected family template as starting point. Pinned presets win over CivitAI/built-in detection.
