// The right-click menu reads with no popup open, so the worker has to do what
// the popup's Speak button does. This runs the worker's real menu code and the
// real reader-core.js against a fake Chrome: the reading code must load where
// there is no page, the three entries must exist, "read from here" must find
// the chunk that was clicked, and every way it can fail must be said in the
// page, since there is nowhere else to say it.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = process.env.KAM_EXT_DIR || path.join(HERE, '..', '..', 'extension');
const read = f => fs.readFileSync(path.join(EXT, f), 'utf8');
const has = f => fs.existsSync(path.join(EXT, f));
const bg = read('background.js');
const core = has('reader-core.js') ? read('reader-core.js') : '';
const { kamSpeakable, kamMostlyNonLatin } = require(path.join(EXT, 'textnorm.js'));

let PASS = 0, FAIL = 0;
const check = (l, g, w) => {
  const ok = JSON.stringify(g) === JSON.stringify(w);
  ok ? (PASS++, console.log('  ok   ' + l))
     : (FAIL++, console.log(`  FAIL ${l}\n         got  ${JSON.stringify(g)}\n         want ${JSON.stringify(w)}`));
};
const section = async (title, fn) => {
  console.log(`\n=== ${title} ===`);
  try { await fn(); } catch (e) { FAIL++; console.log(`  FAIL ${title} threw: ${e && e.message}`); }
};

// A function lifted whole, with `async` kept, stepping over the parameter list
// first since a default like `opts = {}` would end the brace match early.
const grab = (src, name) => {
  let i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`function ${name} not found`);
  let p = 0, j = src.indexOf('(', i);
  for (; j < src.length; j++) { if (src[j] === '(') p++; else if (src[j] === ')') { p--; if (!p) { j++; break; } } }
  let d = 0;
  for (; j < src.length; j++) { if (src[j] === '{') d++; else if (src[j] === '}') { d--; if (!d) { j++; break; } } }
  if (src.slice(Math.max(0, i - 6), i) === 'async ') i -= 6;
  return src.slice(i, j);
};
const MARKER_RE = /\|\/?(?:H1|H2|H3|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST|BREAK)\|/g;

// --- A fake Chrome with one page in it ---------------------------------------
function world({ pageText = '', selection = '', serverUp = true, anchor = '', stored = {}, speed = 1.25 } = {}) {
  const w = { notices: [], preparing: [], started: [], menus: [], removedAll: 0, clicked: null, scripted: [] };
  w.chrome = {
    runtime: { lastError: undefined, onInstalled: { addListener() {} }, onStartup: { addListener() {} } },
    tabs: {
      query: (q, cb) => cb([{ id: 99, url: 'https://active.example/' }]),
      sendMessage: (tabId, msg, opts) => {
        if (msg.action === 'overlayNotice') w.notices.push(msg.text);
        if (msg.action === 'overlayPreparing') w.preparing.push(msg.text);
        if (msg.action === 'getMenuAnchor') { w.anchorAsked = opts || null; return Promise.resolve({ text: anchor }); }
        return Promise.resolve({ status: 'ok' });
      },
    },
    scripting: {
      // The injected walker cannot run here, so the page hands back what the
      // walker would have: one result per frame.
      executeScript: (details, cb) => {
        w.scripted.push(details.target);
        const src = String(details.func);
        const isSelection = src.includes('getSelection');
        cb([{ result: isSelection ? selection : pageText }]);
      },
    },
    storage: { local: { get: async () => stored } },
    contextMenus: {
      removeAll: cb => { w.removedAll++; w.menus.length = 0; cb && cb(); },
      create: (m, cb) => { w.menus.push(m); cb && cb(); },
      onClicked: { addListener: f => { w.clicked = f; } },
    },
  };
  w.kamFetch = async url => ({
    ok: true,
    json: async () => url.endsWith('/settings') ? { speed } : { rules: [] },
  });
  return w;
}

function loadWorker(w, { speedKnown = false } = {}) {
  const consts = ['MENU_SELECTION', 'MENU_FROM_HERE', 'MENU_PAGE']
    .map(n => (bg.match(new RegExp(`const ${n}\\s*=\\s*"[^"]*";`)) || [''])[0]).join('\n');
  const code = `
    ${core}
    ${consts}
    let _readerLoaded = true, _readerProblem = "", _speedKnown = ${speedKnown}, currentSpeed = 1.5;
    const startRead = r => { started.push(r); return { status: "started" }; };
    const _serverUp = async () => serverUp;
    ${grab(bg, 'showError')}
    ${grab(bg, '_plainForMatch')}
    ${grab(bg, 'anchorIndex')}
    ${grab(bg, '_menuNotice')}
    ${grab(bg, '_menuAnchorText')}
    ${grab(bg, '_menuSpeed')}
    ${grab(bg, '_menuPrivate')}
    ${grab(bg, 'readFromMenu')}
    ${grab(bg, '_createMenus')}
    return { readFromMenu, anchorIndex, _createMenus, getPageText, getSelectedText, splitIntoChunks,
             set loaded(v) { _readerLoaded = v; } };`;
  return new Function('chrome', 'kamFetch', 'SERVER', 'KAM_MARKER_RE', 'kamSpeakable', 'kamMostlyNonLatin',
                      'started', 'serverUp', code)(
    w.chrome, w.kamFetch, 'http://s', MARKER_RE, kamSpeakable, kamMostlyNonLatin, w.started, w.serverUp);
}

const PAGE = '|H1|The Garden|/H1| |BREAK| The first paragraph introduces the garden and the people who keep it. '
           + '|BREAK| The second paragraph is about the roses, which were planted in 1921 and still flower. '
           + '|BREAK| The third paragraph covers the café by the gate, where tea costs £3.';
const TAB = { id: 7, title: 'The Garden', url: 'https://example.org/garden' };

await section('the reading code loads where there is no page', async () => {
  check('reader-core.js exists', !!core, true);
  let err = '';
  try {
    new Function('chrome', 'self', 'kamFetch', 'SERVER', 'kamSpeakable', 'kamMostlyNonLatin', core)(
      { tabs: {}, scripting: {} }, {}, async () => ({}), 'http://s', kamSpeakable, kamMostlyNonLatin);
  } catch (e) { err = e.message; }
  check('it evaluates with no document or window', err, '');
  check('the worker imports it', /importScripts\("reader-core\.js"\)/.test(bg), true);
  const html = read('popup.html');
  check('the popup loads it before popup.js',
        html.indexOf('reader-core.js') > 0 && html.indexOf('reader-core.js') < html.indexOf('src="popup.js"'), true);
  check('the reading code is in one place, not two',
        /function splitIntoChunks\(/.test(read('popup.js')), false);
  check('the manifest asks for the menu permission',
        JSON.parse(read('manifest.json')).permissions.includes('contextMenus'), true);
});

await section('three entries, each where it makes sense', async () => {
  const w = world(); const k = loadWorker({ ...w, serverUp: true });
  k._createMenus();
  check('old entries are cleared first, so a reload does not double them', w.removedAll, 1);
  check('the three entries', w.menus.map(m => [m.id, m.contexts]),
        [['kam-read-selection', ['selection']], ['kam-read-from-here', ['page', 'link']], ['kam-read-page', ['page', 'link']]]);
  check('each says what it does', w.menus.every(m => /KAM TTS/.test(m.title)), true);
});

await section('read this page', async () => {
  const w = world({ pageText: PAGE }); const k = loadWorker({ ...w, serverUp: true });
  const r = await k.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
  check('a read starts', [r.status, w.started.length], ['started', 1]);
  const req = w.started[0];
  check('on the tab that was clicked, not the active one', [req.tabId, w.scripted[0].tabId], [7, 7]);
  check('from the top', req.startIndex, 0);
  check('labelled for resume', [req.title, req.url], ['The Garden', 'https://example.org/garden']);
  check('at the speed the server has', req.speed, 1.25);
  check('digestible, like the popup\'s Read Page', req.digestible, true);
  check('with real chunks from the real chunker', req.chunks.length >= 4 && req.chunks.every(c => typeof c === 'string' && c.length <= 200), true);
  check('the bar says it is working while it reads', w.preparing, ['Reading the page…']);
  check('and nothing is reported wrong', w.notices, []);
});

await section('read from here', async () => {
  const w = world({ pageText: PAGE, anchor: 'The second paragraph is about the roses, which were planted in 1921 and still flower.' });
  const k = loadWorker({ ...w, serverUp: true });
  await k.readFromMenu({ menuItemId: 'kam-read-from-here', frameId: 3 }, TAB);
  const req = w.started[0];
  check('the clicked frame is asked where the click landed', w.anchorAsked, { frameId: 3 });
  check('the read starts at the clicked paragraph', /second paragraph/.test(req.chunks[req.startIndex]), true);
  check('with the whole page still in the list', /The Garden/.test(req.chunks[0]), true);

  const chunks = ['The Garden', 'The first paragraph introduces the garden', 'and the people who keep it.',
                  'Tea at the cafe costs 3 pounds.', 'Overview'];
  check('an opening that runs across two chunks is found in the first of them',
        k.anchorIndex(chunks, 'The first paragraph introduces the garden and the people who keep it.'), 1);
  check('accents and symbols do not stop a match', k.anchorIndex(chunks, 'Tea at the café costs £3.'), 3);
  check('a short heading is found', k.anchorIndex(chunks, 'Overview'), 4);
  check('text that is not on the page starts at the top', k.anchorIndex(chunks, 'Completely different words entirely here'), 0);
  check('no anchor starts at the top', k.anchorIndex(chunks, ''), 0);
});

await section('read selection', async () => {
  const w = world({ selection: 'Just this sentence, please. And this one too.' });
  const k = loadWorker({ ...w, serverUp: true }, { speedKnown: true });
  await k.readFromMenu({ menuItemId: 'kam-read-selection' }, TAB);
  check('the selection is what gets read', w.started[0].chunks.join(' '), 'Just this sentence, please. And this one too.');
  check('at the speed already chosen here', w.started[0].speed, 1.5);
  check('the bar says so', w.preparing, ['Reading your selection…']);
});

await section('a private read stays private', async () => {
  const w = world({ pageText: PAGE, stored: { kamPrivateRead: true } }); const k = loadWorker({ ...w, serverUp: true });
  await k.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
  check('the popup\'s private setting is honoured', w.started[0].private, true);
  const w2 = world({ pageText: PAGE }); const k2 = loadWorker({ ...w2, serverUp: true });
  await k2.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
  check('and absent when it is off', 'private' in w2.started[0], false);
});

await section('what goes wrong is said in the page', async () => {
  {
    const w = world({ pageText: PAGE, serverUp: false }); const k = loadWorker({ ...w, serverUp: false });
    const r = await k.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
    check('server off: no read', [r.status, w.started.length], ['server_down', 0]);
    check('and the page says how to start it', /power button/.test(w.notices[0] || ''), true);
    check('without touching the page', w.scripted.length, 0);
  }
  {
    const w = world({ pageText: '' }); const k = loadWorker({ ...w, serverUp: true });
    const r = await k.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
    check('nothing to read: no read', [r.status, w.started.length], ['no_text', 0]);
    check('and it says so', w.notices.length, 1);
  }
  {
    const w = world({ pageText: '__KAM_PDF__' }); const k = loadWorker({ ...w, serverUp: true });
    await k.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
    check('a PDF gets the PDF advice', /PDF/.test(w.notices[0] || ''), true);
  }
  {
    const w = world({ pageText: 'これは日本語の文章です。英語ではありませんので読めません。これは日本語の文章です。' });
    const k = loadWorker({ ...w, serverUp: true });
    const r = await k.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
    check('another language: no silent read', [r.status, w.started.length], ['not_english', 0]);
    check('and it says English only', /English only/.test(w.notices[0] || ''), true);
  }
  {
    const w = world({ pageText: PAGE }); const k = loadWorker({ ...w, serverUp: true });
    k.loaded = false;
    const r = await k.readFromMenu({ menuItemId: 'kam-read-page' }, TAB);
    check('reader missing: says to reload', [r.status, /Reload the extension/.test(w.notices[0] || '')], ['no_reader', true]);
  }
  check('no tab, nothing happens', (await loadWorker(world()).readFromMenu({ menuItemId: 'kam-read-page' }, null)).status, 'no_tab');
});

await section('the popup still reads the active tab', async () => {
  const w = world({ pageText: PAGE }); const k = loadWorker({ ...w, serverUp: true });
  await k.getPageText();
  check('no tab handed in means the active tab, as before', w.scripted[0].tabId, 99);
});

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL ? 1 : 0);
