# Discussão: Prompt Builder para Ideogram e Anima no Forge Neo

> Data: 2026-06-12
> Repositório de trabalho: `sd-webui-prompt-studio-neo`
> Participantes: Eduardo (usuário) + Kimi Code CLI

---

## 1. Resumo executivo

A partir da issue [#1218 do sd-webui-forge-classic](https://github.com/Haoming02/sd-webui-forge-classic/issues/1218), que solicita suporte ao modelo **Ideogram 4** no Forge Neo, investigamos como o **Ideogram Prompt Builder** do `ComfyUI-KJNodes` funciona e como adaptar o conceito para uma extensão nativa do Forge Neo.

Durante a conversa, descobrimos que:

1. O **Ideogram 4** já tem uma extensão não-oficial para Forge Neo (`Whatwhatio/forge-neo-ideogram4`), funcional mas limitada.
2. O mantenedor do Forge Neo (**Haoming02**) vai integrar o Ideogram 4 oficialmente em breve.
3. O modelo **Anima** já funciona nativamente no Forge Neo e, por usar o **Qwen LLM** como text encoder, se beneficia muito de prompts estruturados (key-value/JSON-like).
4. A melhor oportunidade no curto prazo é um **Anima Prompt Builder inline** dentro do `sd-webui-prompt-studio-neo`, nativo no txt2img/img2img.
5. Um **Ideogram Prompt Builder** só deve ser adicionado depois que a integração oficial do Ideogram 4 no Forge Neo estiver madura.
6. **LLM Helper** (gerar prompts automaticamente com Qwen/ToriiGate) fica fora do escopo — é uma feature separada e aumentaria muito a complexidade.

Foi implementado um MVP do **Anima Prompt Builder** no `sd-webui-prompt-studio-neo`.

---

## 2. Contexto e motivação

### 2.1 Ideogram 4 chegando ao Forge Neo

- Modelo open-weight de **9.3B parâmetros**, treinado do zero pela Ideogram.
- Diferenciais: renderização de texto, controle de layout por **bounding boxes**, paleta de cores, saída 2K.
- Versões: `ideogram-ai/ideogram-4-nf4` (~10 GB VRAM) e `ideogram-ai/ideogram-4-fp8` (~13 GB VRAM).
- O modelo consome preferencialmente **JSON captions estruturadas** com ordem fixa de chaves e bbox no grid 0–1000.

### 2.2 Issue #1218 do Forge Neo

- Issue: [Feature] Ideogram 4 — https://github.com/Haoming02/sd-webui-forge-classic/issues/1218
- Usuário recomendou criar um built-in extension no estilo do **Ideogram Prompt Builder** do KJNodes.
- Resposta do Haoming02: "I'll probably work on **Ideogram** before **HiDream**, after **PiD**".
- Eduardo comentou que pode trabalhar em uma extensão externa depois que a feature base for finalizada.

### 2.3 Anima já nativo no Forge Neo

- Modelo **2B parâmetros** da CircleStone Labs + Comfy Org.
- Focado em anime/illustração.
- Usa **Qwen-3 0.6B** como text encoder (não CLIP).
- Por usar LLM, entende muito bem prompts estruturados (key-value, JSON, YAML).

---

## 3. Links e referências coletadas

### Ideogram 4

- Modelo FP8: https://huggingface.co/ideogram-ai/ideogram-4-fp8
- Documentação de prompting: https://github.com/ideogram-oss/ideogram4/blob/main/docs/prompting.md
- Código oficial: https://github.com/ideogram-oss/ideogram4
- Issue Forge Neo: https://github.com/Haoming02/sd-webui-forge-classic/issues/1218

### ComfyUI-KJNodes (referência do Ideogram Prompt Builder)

- Repo: https://github.com/kijai/ComfyUI-KJNodes
- Arquivo principal: `nodes/ideogram4_nodes.py`
- Implementação: nó `Ideogram4PromptBuilderKJ` com canvas visual de bounding boxes, preview PIL, import/export JSON.

### Extensão existente para Forge Neo

- `Whatwhatio/forge-neo-ideogram4`: https://github.com/Whatwhatio/forge-neo-ideogram4
- Extensão funcional com aba separada, Visual JSON Builder, inferência em ambiente isolado.

### Anima

- Modelo: https://huggingface.co/circlestone-labs/Anima
- ComfyUI-AnimaTool: https://github.com/Moeblack/ComfyUI-AnimaTool
- comfyui-ollama-image-to-prompt: https://github.com/jluo-github/comfyui-ollama-image-to-prompt
- ToriiGate-0.5 (image captioning para anime): https://huggingface.co/Minthy/ToriiGate-0.5
- comfyui_toriigate: https://github.com/litch230/comfyui_toriigate

### Reddit (extraído via Playwright)

- "Anima seems to do impressively well on json formatted prompt":
  https://www.reddit.com/r/StableDiffusion/comments/1t0p2au/anima_seems_to_do_impressively_well_on_json/
- "LLM focused on circlestone-labs Anima(NL, JSON and Danbooru) as prompt helper":
  https://www.reddit.com/r/StableDiffusion/comments/1t92wev/llm_focused_on_circlestonelabs_animanl_json_and/

### Danbooru tags

- Tag groups: https://danbooru.donmai.us/wiki_pages/tag_groups

### Forge Neo

- Repo: https://github.com/Haoming02/sd-webui-forge-classic/tree/neo
- Estrutura de extensões documentada em `modules/scripts.py`, `modules/script_callbacks.py`, `modules/extensions.py`.

### Projeto base do usuário

- `sd-webui-prompt-studio-neo`: extensão unificada para Forge Neo (Prompt All-in-One + Tag Autocomplete).

---

## 4. Levantamento técnico

### 4.1 Schema JSON do Ideogram 4

```json
{
  "high_level_description": "string",
  "style_description": {
    "aesthetics": "string",
    "lighting": "string",
    "photo": "string (ou art_style)",
    "medium": "string",
    "color_palette": ["#RRGGBB"]
  },
  "compositional_deconstruction": {
    "background": "string",
    "elements": [
      {
        "type": "obj | text",
        "bbox": [y_min, x_min, y_max, x_max],
        "text": "literal text (só type=text)",
        "desc": "string",
        "color_palette": ["#RRGGBB"]
      }
    ]
  }
}
```

Regras importantes:
- `compositional_deconstruction` é obrigatório.
- Ordem das chaves importa.
- `bbox` usa grid 0–1000 no formato `[ymin, xmin, ymax, xmax]`.
- Cores em hex maiúsculo `#RRGGBB`.

### 4.2 Como o KJNodes implementa

- Classe `Ideogram4PromptBuilderKJ` em `nodes/ideogram4_nodes.py`.
- UI em JavaScript do ComfyUI (frontend custom node).
- Backend Python recebe `elements_data` e `style_palette_data` serializados.
- Gera preview com PIL/Pillow (retângulos, paletas, labels).
- Exporta bboxes em pixel-space para outros nós (SAM3, crop).

### 4.3 Extensão `forge-neo-ideogram4` existente

**Pontos fortes:**
- Roda o pacote oficial do Ideogram em ambiente isolado.
- Visual JSON Builder funcional.
- Importa JSON do KJNodes.
- PNG Info com round-trip.

**Limitações:**
- Aba separada, não inline no txt2img.
- Não integra com pipeline do Forge (sem LoRAs, ControlNet etc.).
- Preview fraco (só boxes, não renderiza preview estilo KJNodes).
- Sem atalhos de teclado.
- Setup Windows-only em partes.

### 4.4 Formato estruturado para Anima

Comunidade descobriu que Anima entende bem key-value:

```text
tags: @eiichiro_oda, score_9, score_8, score_7, highres, masterpiece, safe
girl1: Nami (One Piece), woman, orange hair tied to a ponytail, white tanktop...
girl2: Nico Robin (One Piece), woman, long black hair, blue bomber jacket...
boy1: Chopper (One Piece), small boy, brown fur, brown horns...
background: bright beach scene, sunny day, blue sky, ocean waves
```

Vantagens:
- Fácil editar partes específicas sem reescrever o prompt inteiro.
- LLM (Qwen) entende a estrutura claramente.
- Pode ser serializado como JSON/YAML/key-value.

### 4.5 Estrutura de extensões do Forge Neo

- Extensões em `extensions/` ou `extensions-builtin/`.
- Arquivos em `scripts/*.py` carregados automaticamente.
- JS em `javascript/*.js` injetado automaticamente.
- CSS em `style.css` injetado automaticamente.
- Para UI inline: subclass `modules.scripts.Script` com `scripts.AlwaysVisible`.
- Para nova aba: `script_callbacks.on_ui_tabs(...)`.

---

## 5. Decisões tomadas

1. **Não criar extensão separada.** Integrar no `sd-webui-prompt-studio-neo`.
2. **Começar por Anima**, não por Ideogram.
   - Ideogram terá integração oficial em breve.
   - Extensão não-oficial já existe.
   - Anima é um nicho claro e ainda sem builder nativo no Forge Neo.
3. **MVP sem build complexo.** Usar vanilla JS (padrão TAC) em vez de Vue/Vite (padrão PAIO).
4. **Inline no txt2img/img2img.** Botão ao lado do prompt nativo, painel com campos estruturados.
5. **Dois modos de output:** key-value (legível) e flatten (compatível direto com Anima).
6. **Fora do escopo inicial:** LLM helper, image-to-prompt, canvas visual (reservado para Ideogram no futuro).
7. **Reaproveitar lógica do KJNodes quando for Ideogram**, respeitando GPL-3.0.

---

## 6. Sugestões para seguir

### Curto prazo (Anima)

1. **Testar o MVP implementado** no Forge Neo com o modelo Anima.
2. **Coletar feedback** sobre:
   - Usabilidade dos campos estruturados.
   - Qualidade dos prompts gerados em key-value vs flatten.
   - Necessidade de presets/salvamentos.
3. **Adicionar presets** comuns (ex: "1girl portrait", "2girls scene", "1boy action").
4. **Adicionar import/export** de prompts Anima estruturados.
5. **Melhorar preview** com renderização simples do layout (sem canvas complexo).

### Médio prazo (Ideogram)

1. **Aguardar integração oficial do Ideogram 4 no Forge Neo.**
2. Quando estiver maduro, adicionar ao `sd-webui-prompt-studio-neo` um módulo de **Ideogram Prompt Builder** com:
   - Canvas visual de bounding boxes.
   - Paleta de cores por elemento e global.
   - Preview PIL renderizado.
   - Import/export compatível com KJNodes.
   - Integração inline no txt2img.
3. Reaproveitar/adaptar código do KJNodes (`nodes/ideogram4_nodes.py`), mantendo créditos e licença GPL-3.0.

### Longo prazo (LLM Helper)

1. Avaliar um módulo separado de **LLM Prompt Helper**:
   - Expandir descrição natural para prompt estruturado.
   - Usar Qwen local ou API (Ollama, LM Studio).
   - Integrar ToriiGate-0.5 para image-to-prompt de referência.
2. Este módulo deve ser **opcional** e desacoplado do builder manual.

### Melhorias técnicas

1. **Cross-platform:** garantir que paths e setup funcionem no Linux também.
2. **Settings no Forge:** adicionar opções na seção "Prompt Studio Neo" para ativar/desativar o builder.
3. **Persistência:** salvar presets/favoritos de prompts Anima.
4. **Validação:** usar o backend Python para validar tags obrigatórias (safety, quality).
5. **Documentação pública:** adicionar README explicando o Anima Prompt Builder.

---

## 7. Implementação atual (MVP Anima)

### Arquivos criados

- `javascript/animaPromptBuilder.js` — UI do builder.
- `scripts/physton_prompt/anima_builder.py` — serialização/parse/validação.

### Arquivos alterados

- `scripts/on_app_started.py` — endpoints FastAPI.
- `javascript/.gitignore` — exceção para o novo JS.
- `AGENTS.local.md` e `docs/PROJECT_LOG.md` — docs internos (ignorados pelo git).

### Endpoints adicionados

- `POST /physton_prompt/anima_build`
- `POST /physton_prompt/anima_parse`
- `GET /physton_prompt/anima_defaults`

### Como testar

1. Recarregar a UI do Forge Neo (`Reload UI` ou restart).
2. No txt2img/img2img, clicar no botão 🎨 ao lado do prompt.
3. Preencher campos e clicar "Apply to prompt".
4. Selecionar modelo Anima e gerar.

---

## 8. Notas

- A documentação interna (`AGENTS.local.md`, `docs/PROJECT_LOG.md`) é mantida em português e ignorada pelo git conforme convenção do projeto.
- Este documento de pesquisa (`docs/research/ideogram-anima-prompt-builder-discussion.md`) é público e pode ser compartilhado.
- O levantamento do Reddit foi feito via Playwright localmente instalado no workspace.
