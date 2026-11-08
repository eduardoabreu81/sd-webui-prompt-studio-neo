// TagComplete prompt helpers (javascript/_promptTools.js) and their hooks in tagAutocomplete.js.
const assert = require('node:assert/strict')
const {readFileSync} = require('node:fs')
const {resolve} = require('node:path')
const {test} = require('node:test')
const vm = require('node:vm')

const root = resolve(__dirname, '..')
const tools = readFileSync(resolve(root, 'javascript/_promptTools.js'), 'utf8')
const tac = readFileSync(resolve(root, 'javascript/tagAutocomplete.js'), 'utf8')
const context = {}
vm.runInNewContext(tools + ';this.TacPromptTools = TacPromptTools', context)
const T = context.TacPromptTools
const fmt = (text, options) => T.formatPrompt(text, options)

test('formatter tidies commas and spaces per line', () => {
    assert.equal(fmt('  1girl ,solo,,  smile ,  '), '1girl, solo, smile')
    assert.equal(fmt('1girl,solo\n\nsky ,cloud'), '1girl, solo\n\nsky, cloud')
    assert.equal(fmt('1girl , solo', {separator: ','}), '1girl,solo')
    assert.equal(fmt('1girl, solo\nsky', {trailingComma: true}), '1girl, solo,\nsky,')
    assert.equal(fmt('1girl, solo\nBREAK\nsky', {trailingComma: true}), '1girl, solo,\nBREAK\nsky,')
    assert.equal(fmt('long  hair , smile'), 'long  hair, smile')
})

test('formatter keeps bracket, quote and extra network contents as written', () => {
    assert.equal(fmt('(a,b:1.2),c'), '(a,b:1.2), c')
    assert.equal(fmt('{red,blue|green} ,  x'), '{red,blue|green}, x')
    assert.equal(fmt('"a,b" ,c'), '"a,b", c')
    assert.equal(fmt('<lora:my  model:0.8>,a'), '<lora:my  model:0.8>, a')
    assert.equal(fmt('nami \\(one piece\\),a'), 'nami \\(one piece\\), a')
    assert.equal(fmt('>_<,a'), '>_<, a')
})

test('formatter leaves unbalanced text and data lists untouched', () => {
    assert.equal(fmt('(a,b ,c'), '(a,b ,c')
    assert.equal(fmt('a) ,b'), 'a) ,b')
    assert.equal(fmt('1,0, 0,A'), '1,0, 0,A')
    assert.equal(fmt('   '), '')
    assert.equal(fmt(''), '')
})

test('normalizeTag compares tags across emphasis, weights and escapes', () => {
    assert.equal(T.normalizeTag(' (long hair:1.2) '), 'long_hair')
    assert.equal(T.normalizeTag('((long_hair))'), 'long_hair')
    assert.equal(T.normalizeTag('[Long Hair]'), 'long_hair')
    assert.equal(T.normalizeTag('nami \\(one piece\\)'), 'nami_(one_piece)')
    assert.equal(T.normalizeTag('@artist name'), 'artist_name')
})

test('findTag skips the segment under the caret', () => {
    const text = 'long hair, smile, long_h'
    assert.deepEqual({...T.findTag(text, 'long_hair', text.length)}, {start: 0, end: 9})
    assert.equal(T.findTag(text, 'smile', 13), null)
    assert.equal(T.findTag('long_hair', 'long_hair', 4), null)
    assert.deepEqual({...T.findTag('a :( b, (smile:1.1)', 'smile', -1)}, {start: 8, end: 19})
})

test('removalRange drops one adjacent comma', () => {
    const cut = (text, pos) => {
        const r = T.removalRange(text, T.segmentAt(text, pos))
        return text.slice(0, r.start) + text.slice(r.end)
    }
    assert.equal(cut('a, long_h', 8), 'a')
    assert.equal(cut('long_h, a', 2), 'a')
    assert.equal(cut('a, long_h, b', 6), 'a, b')
    assert.equal(cut('a\nlong_h\nb', 4), 'a\n\nb')
})

const hooks = tac.slice(tac.indexOf('function setPromptText'), tac.indexOf('// On click, insert the tag'))

function setup(value, caret, cfg = {}, window = {}) {
    const area = {
        value, selectionStart: caret, selectionEnd: caret,
        setSelectionRange(s, e) { this.selectionStart = s; this.selectionEnd = e },
    }
    const ctx = {
        TAC_CFG: {appendSpace: true, formatTrailingComma: false, ...cfg},
        updates: 0, hidden: 0, tagword: 'x', window,
    }
    ctx.updateInput = () => ctx.updates++
    ctx.hideResults = () => ctx.hidden++
    vm.runInNewContext(tools + '\n' + hooks, ctx)
    return {area, ctx}
}

test('choosing a used tag removes the partial word and selects the existing tag', () => {
    const {area, ctx} = setup('long hair, smile, long_h', 24)
    assert.equal(ctx.selectUsedTag(area, 'long_hair'), true)
    assert.equal(area.value, 'long hair, smile')
    assert.equal(area.value.slice(area.selectionStart, area.selectionEnd), 'long hair')
    assert.equal(ctx.updates, 1)
    assert.equal(ctx.hidden, 1)
})

test('the existing tag after the partial word keeps its selection after the removal', () => {
    const {area, ctx} = setup('smi, 1girl, (smile:1.2)', 3)
    assert.equal(ctx.selectUsedTag(area, 'smile'), true)
    assert.equal(area.value, '1girl, (smile:1.2)')
    assert.equal(area.value.slice(area.selectionStart, area.selectionEnd), '(smile:1.2)')
})

test('a new tag, or a partial word inside special syntax, falls through to normal insertion', () => {
    let {area, ctx} = setup('1girl, smi', 10)
    assert.equal(ctx.selectUsedTag(area, 'smile'), false)
    assert.equal(area.value, '1girl, smi')
    ;({area, ctx} = setup('smile, (smi:1.2)', 11))
    assert.equal(ctx.selectUsedTag(area, 'smile'), false)
    assert.equal(ctx.updates, 0)
})

test('formatPromptArea follows the append-space and trailing-comma settings', () => {
    let {area, ctx} = setup('1girl ,solo,,smile', 5)
    ctx.formatPromptArea(area)
    assert.equal(area.value, '1girl, solo, smile')
    assert.equal(ctx.updates, 1)
    ;({area, ctx} = setup('1girl , solo', 0, {appendSpace: false, formatTrailingComma: true}))
    ctx.formatPromptArea(area)
    assert.equal(area.value, '1girl,solo,')
    ;({area, ctx} = setup('1girl, solo', 0))
    ctx.formatPromptArea(area)
    assert.equal(ctx.updates, 0)
})

test('with Prompt Studio formatting published, the formatter matches its layout', () => {
    const cases = [
        [{removeSpace: true, removeLastComma: false}, '1girl,solo,\nsky,cloud,'],
        [{removeSpace: false, removeLastComma: false}, '1girl, solo, \nsky, cloud, '],
        [{removeSpace: true, removeLastComma: true}, '1girl,solo,\nsky,cloud'],
    ]
    for (const [prefs, expected] of cases) {
        const {area, ctx} = setup('1girl ,solo\nsky,, cloud', 0, {}, {phystonPromptFormat: prefs})
        ctx.formatPromptArea(area)
        assert.equal(area.value, expected)
    }
})

// Multi-word completion ("long hai" -> long_hair)
const utils = readFileSync(resolve(root, 'javascript/_utils.js'), 'utf8')
const slice = (from, to) => tac.slice(tac.indexOf(from), tac.indexOf(to, tac.indexOf(from) + 1))
const hasTagSrc = slice('// True when a tag name contains `word`', 'async function autocomplete(')

test('spacedTagQueries returns the last plain words joined, longest first', () => {
    assert.deepEqual([...T.spacedTagQueries('1girl, very long hai')], ['very_long_hai', 'long_hai'])
    assert.deepEqual([...T.spacedTagQueries('a girl standing in the ra')], ['in_the_ra', 'the_ra'])
    assert.deepEqual([...T.spacedTagQueries('smile, Long Hai')], ['long_hai'])
    assert.deepEqual([...T.spacedTagQueries('smile, hai')], [])
    assert.deepEqual([...T.spacedTagQueries('long hai ')], [])
    assert.deepEqual([...T.spacedTagQueries('long  hai')], [])
    assert.deepEqual([...T.spacedTagQueries('(long hai')], [])
    assert.deepEqual([...T.spacedTagQueries('<lora:my lo')], [])
    assert.deepEqual([...T.spacedTagQueries('__my wild')], [])
    assert.deepEqual([...T.spacedTagQueries('a|long hai')], ['long_hai'])
})

const insertFn = tac.slice(tac.indexOf('async function insertTextAtCursor'), tac.indexOf('function addResultsToList('))
const constants = ['POINTY_REGEX', 'COMPLETED_WILDCARD_REGEX', 'STYLE_VAR_REGEX', 'NORMAL_TAG_REGEX', 'WEIGHT_REGEX']
    .map(name => tac.match(new RegExp(`const ${name} = .*;`))[0]).join('\n')
const tagRegexFn = tac.slice(tac.indexOf('let cachedTagRegex'), tac.indexOf('function setPromptText'))

async function insert(value, caret, text, word, cfg = {}) {
    const area = {value, selectionStart: caret, selectionEnd: caret, setSelectionRange() {}}
    const ctx = {
        TAC_CFG: {appendComma: true, appendSpace: true, alwaysSpaceAtEnd: true, replaceUnderscores: true,
            escapeParentheses: true, animaArtistPrefix: 'Off', modelKeywordCompletion: 'Never', wcWrap: '__', ...cfg},
        ResultType: {tag: 1, extra: 2, lora: 3, lyco: 4, wildcardFile: 5, yamlWildcard: 6, umiWildcard: 7, wildcardTag: 8, chant: 9},
        QUEUE_SANITIZE: [], QUEUE_AFTER_INSERT: [], previousTags: [], tagword: '', tacSelfTrigger: false, hideBlocked: false,
        updateInput() {}, hideResults() {}, isVisible: () => false, checkpointIsAnima: () => false,
        requestAnimationFrame: fn => fn(), setTimeout: fn => fn(),
        getTextAreaIdentifier: () => '.txt2img.p', gradioApp: () => ({querySelector: () => null}),
    }
    vm.runInNewContext(utils + '\n' + constants + '\n' + tagRegexFn + '\n' + insertFn
        + '\nthis.run = (a, r, w) => insertTextAtCursor(a, r, w)', ctx)
    await ctx.run(area, {text, type: 1}, word)
    return area.value
}

test('a tag found from spaced words replaces all of them', async () => {
    assert.equal(await insert('1girl, long hai', 15, 'long_hair', 'long_hai'), '1girl, long hair, ')
    assert.equal(await insert('long hai, smile', 8, 'long_hair', 'long_hai'), 'long hair, smile')
    assert.equal(await insert('1girl, very long hai', 20, 'very_long_hair', 'very_long_hai'), '1girl, very long hair, ')
    assert.equal(await insert('1girl, long_hai', 15, 'long_hair', 'long_hai'), '1girl, long hair, ')
    assert.equal(await insert('1girl, rai', 10, 'rain', 'rai'), '1girl, rain, ')
})

test('hasTagMatch finds multi-word tags through the prefix index and without it', () => {
    const allTags = [['long_hair'], ['very_long_hair'], ['rain']]
    allTags.forEach(t => t._lower = t[0])
    const tagIndex = new Map([['lon', [allTags[0], allTags[1]]], ['hai', [allTags[0], allTags[1]]], ['ver', [allTags[1]]], ['rai', [allTags[2]]]])
    for (const useIndexedSearch of [true, false]) {
        const ctx = {allTags, tagIndex, TAC_CFG: {useIndexedSearch}}
        vm.runInNewContext(utils + '\n' + hasTagSrc, ctx)
        assert.equal(ctx.hasTagMatch('long_hai'), true)
        assert.equal(ctx.hasTagMatch('the_ra'), false)
        assert.equal(ctx.hasTagMatch('ng_hai'), false)
    }
})
