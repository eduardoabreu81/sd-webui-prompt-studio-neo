// Prompt Studio Neo — Live Tag Colorizer (Stage 1)
// Colorizes prompt text in the native Forge textareas by tag type, live, without
// breaking Forge. Data-driven: colors come from TAC (allTags + TAC_CFG.colorMap +
// TAC's CSS color vars) and the psn_colorizer_* settings. Nothing hardcoded except
// fallbacks. Master switch (opts.psn_colorizer_enabled) defaults OFF.
//
// Invariants (learned in Stage 0): read-only overlay — never write to the textarea
// or dispatch input/change (protects the TAC Tab caret fix); progressive transparency
// with -webkit-text-fill-color; no document-wide MutationObserver; align to the
// padding box with no mirror border.

(function () {
    "use strict";

    const BOX_IDS = ["txt2img_prompt", "txt2img_neg_prompt", "img2img_prompt", "img2img_neg_prompt"];
    const attached = new WeakSet();
    const attachedTAs = new Set();       // for re-rendering when the tag map grows
    let danMap = null, danMapLen = -1;   // name(lower) -> danbooru type; rebuilt when allTags loads
    let paioColors = null;               // name(lower) -> [darkColor, lightColor], fetched from backend

    function reRenderAll() { attachedTAs.forEach((ta) => { if (ta._psnTc && document.contains(ta)) ta._psnTc.render(); }); }

    // --- config / data helpers (all read live) -------------------------------
    function enabled() {
        return typeof opts !== "undefined" && !!opts.psn_colorizer_enabled;
    }
    function themeIdx() {
        // TAC color arrays are [darkColor, lightColor]. Forge Neo doesn't reliably use a
        // .dark class, so detect from the page background luminance (works with any theme).
        try {
            for (const el of [document.body, document.documentElement]) {
                const m = getComputedStyle(el).backgroundColor.match(/rgba?\(([^)]+)\)/);
                if (m) {
                    const [r, g, b, a] = m[1].split(",").map(Number);
                    if (a === 0) continue; // transparent — try next element
                    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128 ? 0 : 1;
                }
            }
        } catch (e) { /* fall through */ }
        return 0; // default dark
    }
    function cssVar(name, fallback) {
        const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return v || fallback;
    }
    function tagFileBase() {
        return (typeof TAC_CFG !== "undefined" && TAC_CFG && TAC_CFG.tagFile)
            ? TAC_CFG.tagFile.split(".")[0] : "danbooru";
    }
    function buildDanMap() {
        const sources = [];
        if (typeof allTags !== "undefined" && allTags) sources.push(allTags);
        if (typeof extras !== "undefined" && extras) sources.push(extras);   // quality tags etc.
        if (sources.length === 0) return null;
        const m = new Map();
        for (const arr of sources) {
            for (const t of arr) {
                if (t && t[0] != null && t[1] !== undefined && t[1] !== "") {
                    const type = String(t[1]);
                    if (/^-?\d+$/.test(type)) m.set(String(t[0]).trim().toLowerCase(), type);
                }
            }
        }
        return m.size ? m : null;
    }
    // Rebuild the map when allTags grows (TAC loads tags lazily, after we may have already
    // built a map from `extras` alone). Re-render attached boxes so new colors apply.
    function ensureDanMap() {
        const len = (typeof allTags !== "undefined" && allTags) ? allTags.length : 0;
        if (danMap !== null && len === danMapLen) return;
        danMap = buildDanMap();
        danMapLen = len;
        attachedTAs.forEach((ta) => { if (ta._psnTc && document.contains(ta)) ta._psnTc.render(); });
    }
    function danbooruColor(type) {
        const cm = (typeof TAC_CFG !== "undefined" && TAC_CFG) ? TAC_CFG.colorMap : null;
        if (!cm) return null;
        const group = cm[tagFileBase()] || cm["danbooru"];
        if (!group) return null;
        const pair = group[type] || group["-1"];
        return pair ? pair[themeIdx()] : null;
    }
    function embeddingSet() {
        if (typeof embeddings === "undefined" || !embeddings) return null;
        const s = new Set();
        for (const e of embeddings) s.add(String(Array.isArray(e) ? e[0] : e).trim().toLowerCase());
        return s;
    }
    function wildcardColor() {
        try {
            const arr = JSON.parse(opts.psn_colorizer_wildcard_color);
            return arr[themeIdx()] || arr[0];
        } catch (e) { return themeIdx() ? "#8a6300" : "#e6b422"; }
    }
    function animaAt() {
        return typeof opts !== "undefined" && opts.psn_colorizer_anima_at !== "Off";
    }
    // Forge disables the negative prompt textarea outright when cfg_scale <= 1 (it has no
    // effect at CFG 1) — see modules/ui.py: `cfg_scale.change(fn=use_cfg, ...)` where
    // `use_cfg` returns `gr.update(interactive=(val > 1.0))`. The gray look the user sees
    // is the browser's native `:disabled` textarea style. We override the textarea's own
    // color to transparent, which hides that, so the overlay has to reproduce the dimming
    // itself — driven directly off `ta.disabled`, which Forge sets for us.

    // --- tokenizer (proven in Stage 0) ---------------------------------------
    // <...> may not contain < or newline: an unpaired < earlier in the prompt (e.g. the
    // <3 emoticon tag, or a second lora being typed) must not swallow a following
    // <lora:...>. < and > are also NOT punctuation (group 4) so a stray ( or ) glued to
    // < can't eat it either — this makes weighted loras like (<lora:x:1>:1.2) work.
    // __wildcard__ names routinely carry single underscores (__hair_style__, __2_girls__),
    // so the body allows a lone _ via `_(?!_)` and only stops at the closing __. A plain
    // [^_]* body would break every such name AND mis-match across two wildcards — in
    // "__hair_style__, __color__" it matched the "__, __" in the middle instead.
    const TOKEN_RE = /(<[^<>\n]*>)|(__(?![\s_])(?:[^_\n]|_(?!_))*__)|((?:(?!__)[^,()\[\]{}<>:|\n])+)|([,()\[\]{}:|\n]+)|([\s\S])/g;
    function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

    function classifyPhrase(chunk) {
        const t = chunk.trim();
        if (!t) return null;
        if (t === "BREAK" || t === "AND" || t === "NOT") return null;
        const norm = t.toLowerCase().replace(/\s+/g, "_");
        // Anima's '@' artist marker. A leading '@' is not evidence that what follows is
        // an artist, so the tag is colored by its *actual* category instead of assuming
        // category 1. The full token is looked up first, because a few real tags carry
        // the '@' in the name itself (@_@ is a general tag, @shun / @est@ are artists);
        // only then the name without the prefix. Unknown names stay uncolored.
        if (t[0] === "@" && animaAt()) {
            if (!danMap) return null;
            if (danMap.has(norm)) return danbooruColor(danMap.get(norm));
            const bare = norm.slice(1);
            return bare && danMap.has(bare) ? danbooruColor(danMap.get(bare)) : null;
        }
        if (paioColors) {                                                              // 1) PAIO group (already user-configured)
            // group_tags/*.yaml keys are inconsistent (some spaced, some underscored) — try both
            const pc = paioColors[norm] || paioColors[t.toLowerCase()];
            if (pc) return pc[themeIdx()];
        }
        const emb = embeddingSet();
        if (emb && emb.has(norm)) return cssVar("--embedding-v1-color", "#8ea9d0");  // 2) TAC special (bare embedding)
        if (danMap && danMap.has(norm)) return danbooruColor(danMap.get(norm));       // 3) Danbooru type
        return null;
    }
    function classifyToken(m) {
        if (m[1]) {
            const inner = m[1].slice(1, -1).toLowerCase();
            if (inner.startsWith("lora:") || inner.startsWith("lyco:") || inner.startsWith("hypernet:"))
                return cssVar("--lora-color", "#597ef7");
            if (inner.startsWith("embedding:")) return cssVar("--embedding-v1-color", "#8ea9d0");
            return null;
        }
        if (m[2]) return wildcardColor();
        if (m[3]) return classifyPhrase(m[3]);
        return null;
    }
    function tokensToHtml(v) {
        let html = "", m;
        TOKEN_RE.lastIndex = 0;
        while ((m = TOKEN_RE.exec(v))) {
            const color = classifyToken(m);
            const safe = esc(m[0]);
            html += color ? '<span style="color:' + color + '">' + safe + "</span>" : safe;
        }
        return html;
    }

    // --- overlay -------------------------------------------------------------
    function initColorizer(ta) {
        const host = ta.parentElement;
        if (getComputedStyle(host).position === "static") host.style.position = "relative";
        host.querySelectorAll(":scope > .psn-tc-backdrop").forEach((e) => e.remove());

        const backdrop = document.createElement("div");
        backdrop.className = "psn-tc-backdrop";
        const hl = document.createElement("div");
        hl.className = "psn-tc-highlights";
        Object.assign(backdrop.style, { position: "absolute", zIndex: "0", overflow: "hidden", pointerEvents: "none", margin: "0" });
        Object.assign(hl.style, { margin: "0", border: "0", whiteSpace: "pre-wrap", wordWrap: "break-word", overflowWrap: "break-word", boxSizing: "border-box", pointerEvents: "none" });
        backdrop.appendChild(hl);
        host.appendChild(backdrop);

        const COPY = ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing",
            "textTransform", "wordSpacing", "textIndent", "textAlign",
            "paddingTop", "paddingRight", "paddingBottom", "paddingLeft"];
        // neutral tokens use the textarea's real text color (theme-correct, incl. custom themes)
        const baseColor = getComputedStyle(ta).webkitTextFillColor || getComputedStyle(ta).color;
        hl.style.color = baseColor;

        function copyStyles() { const cs = getComputedStyle(ta); for (const p of COPY) hl.style[p] = cs[p]; hl.style.width = "100%"; hl.style.height = "auto"; }
        function place() {
            backdrop.style.top = (ta.offsetTop + ta.clientTop) + "px";
            backdrop.style.left = (ta.offsetLeft + ta.clientLeft) + "px";
            backdrop.style.width = ta.clientWidth + "px";
            backdrop.style.height = ta.clientHeight + "px";
        }
        function syncScroll() { hl.style.transform = "translate(" + (-ta.scrollLeft) + "px," + (-ta.scrollTop) + "px)"; }
        let raf = 0;
        // remember what we rendered: the periodic scan uses this to catch value changes
        // that arrive without an input event (Gradio backend updates, paste arrow, PAIO)
        function render() {
            copyStyles(); place();
            const v = ta.value;
            hl.innerHTML = tokensToHtml(v);
            // mirror Forge's own `disabled` dimming on the overlay (colored spans included)
            // since our override of the textarea's color already hid the native gray look
            const dim = ta.disabled;
            hl.style.opacity = dim ? "0.5" : "1";
            ta._psnTcLast = v;
            ta._psnTcDim = dim;
            syncScroll();
        }
        function schedule() { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; ensureDanMap(); render(); }); }

        ta.addEventListener("input", schedule);       // read-only: never dispatch input ourselves
        ta.addEventListener("scroll", syncScroll);
        new ResizeObserver(() => { place(); syncScroll(); }).observe(ta);

        // hide real text only after attaching (progressive; theme may use -webkit-text-fill-color).
        // caret-color MUST be set to a visible color, else it inherits the transparent text color.
        ta.style.setProperty("color", "transparent", "important");
        ta.style.setProperty("-webkit-text-fill-color", "transparent", "important");
        ta.style.setProperty("caret-color", baseColor, "important");
        ta.style.setProperty("background", "transparent", "important");
        ta._psnTc = { backdrop, hl, ta, render, schedule };
        attachedTAs.add(ta);
        render();

        // narrow observer: reattach if Gradio replaces this textarea node
        const ho = new MutationObserver(() => { if (!document.contains(ta)) { attached.delete(ta); attachedTAs.delete(ta); ho.disconnect(); scan(); } });
        ho.observe(host, { childList: true });
    }

    function detach(ta) {
        if (ta._psnTc && ta._psnTc.backdrop) ta._psnTc.backdrop.remove();
        ta.style.removeProperty("color");
        ta.style.removeProperty("-webkit-text-fill-color");
        ta.style.removeProperty("caret-color");
        ta.style.removeProperty("background");
        attached.delete(ta);
        attachedTAs.delete(ta);
        delete ta._psnTc;
    }

    function scan() {
        if (!enabled()) return;
        ensureDanMap();
        for (const id of BOX_IDS) {
            const root = document.getElementById(id);
            if (!root) continue;
            const ta = root.querySelector("textarea");
            if (!ta) continue;
            if (attached.has(ta)) {
                const tc = ta._psnTc;
                if (!tc || !document.contains(tc.backdrop)) {
                    detach(ta);            // Gradio removed our backdrop but kept the textarea — reattach below
                } else if (ta._psnTcLast !== ta.value) {
                    tc.schedule();         // value changed with no input event — resync the overlay
                    continue;
                } else if (ta._psnTcDim !== ta.disabled) {
                    tc.schedule();         // CFG scale toggled `disabled` — doesn't touch ta.value, so resync explicitly
                    continue;
                } else {
                    continue;
                }
            }
            if (ta.offsetParent === null) continue;
            attached.add(ta);
            try { initColorizer(ta); } catch (e) { console.error("[PSN colorizer] attach failed:", e); attached.delete(ta); }
        }
    }

    function disableAll() {
        document.querySelectorAll("#txt2img_prompt textarea, #txt2img_neg_prompt textarea, #img2img_prompt textarea, #img2img_neg_prompt textarea")
            .forEach((ta) => { if (ta._psnTc) detach(ta); });
    }

    // --- lifecycle (Forge globals) -------------------------------------------
    function loadPaioColors() {
        fetch("/prompt-studio-neo/paio-colors")
            .then((r) => (r.ok ? r.json() : null))
            .then((j) => { if (j && typeof j === "object") { paioColors = j; reRenderAll(); } })
            .catch(() => { /* PAIO colors optional */ });
    }
    if (typeof onUiLoaded === "function") {
        onUiLoaded(() => {
            loadPaioColors();
            // Permanent watchdog: attaches new boxes (img2img tab), reattaches when Gradio
            // replaces the textarea or removes our backdrop, and resyncs when the value
            // changed without an input event. scan() is a few getElementById calls when
            // nothing changed — cheap enough to run forever.
            setInterval(() => { if (enabled()) scan(); }, 500);
        });
    }
    if (typeof onOptionsChanged === "function") {
        onOptionsChanged(() => {
            if (enabled()) scan();
            else disableAll();
        });
    }
})();
