# SD WebUI Prompt Studio Neo

**All-in-one prompt workstation for [Stable Diffusion WebUI Forge - Neo](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo).**

Prompt Studio Neo unifies two extensions into a single install:

| Module | Origin | What it does |
|---|---|---|
| **Prompt All-in-One** | fork of [physton/sd-webui-prompt-all-in-one](https://github.com/Physton/sd-webui-prompt-all-in-one) | Interactive tag-chip prompt editor: translation (16+ services + offline mBART-50), history, full-prompt favorites with export/import, LoRA/embedding detection, quality presets with CivitAI checkpoint detection, suggested tag groups, ChatGPT prompt generation |
| **Tag Autocomplete** | fork of [DominikDoom/a1111-sd-webui-tagcomplete](https://github.com/DominikDoom/a1111-sd-webui-tagcomplete) | Danbooru/e621 tag autocompletion while typing, LoRA/LyCORIS/embedding/wildcard/style completion, CivitAI trigger words, usage-frequency sorting |

> **Forge Neo only.** Not compatible with Automatic1111, Forge Classic, or SD.Next.

## Installation

Extensions → Install from URL:

```
https://github.com/eduardoabreu81/sd-webui-prompt-studio-neo
```

> **Important:** uninstall `sd-webui-prompt-all-in-one-neo` and `sd-webui-tagcomplete-neo` if you have them installed separately — Prompt Studio Neo replaces both, and running them together will duplicate functionality.

## Documentation

Full feature documentation for each module (pre-merge READMEs, still accurate):

- [Prompt All-in-One module](docs/README-prompt-all-in-one.md)
- [Tag Autocomplete module](docs/README-tagcomplete.md)

## Credits

- [Physton](https://github.com/Physton) — original sd-webui-prompt-all-in-one
- [Dominik Reh (DominikDoom)](https://github.com/DominikDoom) — original a1111-sd-webui-tagcomplete
- [Haoming02](https://github.com/Haoming02) — Forge Classic / Neo

## License

[MIT](LICENSE) — preserves the original copyright notices of both upstream projects.
