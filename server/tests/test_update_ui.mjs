// The update button and bar, from the real code at the end of dashboard.js,
// and the worker's resume after the restart, from the real background.js.
// Run against a stand-in page, a stand-in chrome and a stand-in GitHub.
import fs from 'node:fs';
const dash = fs.readFileSync(new URL('../../extension/dashboard.js', import.meta.url), 'utf8');
const bg = fs.readFileSync(new URL('../../extension/background.js', import.meta.url), 'utf8');

let PASS=0, FAIL=0;
const check=(l,g,w)=>{ const ok=JSON.stringify(g)===JSON.stringify(w);
  ok?(PASS++,console.log('  ok   '+l)):(FAIL++,console.log(`  FAIL ${l}\n         got ${JSON.stringify(g)} want ${JSON.stringify(w)}`)); };
const tick = () => new Promise(r => setTimeout(r, 0));

// --- a page with just the elements the update code touches ---
function element(id) {
  const classes = new Set(), listeners = {};
  const el = { id, hidden: id === 'upd-bar' || id === 'upd-progress', textContent: '', title: '', href: '', dataset: {}, attrs: {},
    classList: { toggle: (c, on) => on ? classes.add(c) : classes.delete(c), contains: c => classes.has(c) },
    setAttribute: (k, v) => { el.attrs[k] = v; },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    click: () => listeners.click && listeners.click(),
    get classes() { return [...classes].sort(); } };
  return el;
}
function page({ release, stored = {}, api, confirmAnswer = true, version = '0.10.0' }) {
  const els = {}; for (const id of ['upd-btn', 'upd-bar', 'upd-msg', 'upd-progress', 'upd-notes', 'upd-now', 'upd-later']) els[id] = element(id);
  els.label = element('label'); els['upd-btn'].querySelector = () => els.label;
  let ready = null; const sent = [], toasts = [], local = { ...stored }, ls = {};
  const env = {
    document: { getElementById: id => els[id], addEventListener: (t, fn) => { if (t === 'DOMContentLoaded') ready = fn; } },
    chrome: { runtime: { getManifest: () => ({ version }), sendMessage: (m, cb) => { sent.push(m); cb && cb(); }, lastError: null },
              storage: { local: { get: (k, cb) => cb({ [k]: local[k] }), remove: k => { delete local[k]; } } } },
    localStorage: { getItem: k => (k in ls ? ls[k] : null), setItem: (k, v) => { ls[k] = String(v); } },
    fetch: async () => release === 404 ? { ok: false, status: 404 } : { ok: true, status: 200, json: async () => release },
    showToast: (m, kind) => toasts.push([kind || 'error', m]),
    api: api || (async () => ({ ok: true, from: version, to: '0.11.0' })),
    confirm: () => confirmAnswer,
    setTimeout: () => 0, setInterval: () => 0, clearTimeout: () => {},
  };
  const section = dash.slice(dash.indexOf('// Updates. The dashboard asks GitHub itself'));
  const fns = new Function(...Object.keys(env), `${section}
    return { updCheck, updInstall, updLater, updIsNewer };`)(...Object.values(env));
  ready();
  const bar = () => els['upd-bar'].hidden ? 'hidden' : els['upd-bar'].dataset.kind;
  return { ...fns, els, sent, toasts, ls, local, bar, label: () => els.label.textContent, button: () => els['upd-btn'].classes };
}
const release = v => ({ tag_name: 'v' + v, name: `KAM TTS ${v} - a change`, html_url: 'https://example.invalid/rel', draft: false, prerelease: false });

console.log('\n=== the button ===');
{
  const p = page({ release: release('0.10.0') });
  check('quiet while nothing newer is known', [p.button(), p.label(), p.bar()], [[], '', 'hidden']);
  await p.updCheck(true);
  check('a check you asked for answers on the button', p.label(), 'Up to date');
  check('and says so', p.toasts[0], ['info', 'You are on the latest version, 0.10.0.']);
}
{
  const p = page({ release: 404 });
  await p.updCheck(true);
  check('nothing released yet is not an error', [p.label(), p.bar()], ['Up to date', 'hidden']);
}

console.log('\n=== a newer version ===');
{
  const p = page({ release: release('0.11.0') });
  await p.updCheck(false);
  check('turns the button gold and names it', [p.button(), p.label()], [['available'], 'Update to 0.11.0']);
  check('and raises the bar', p.bar(), 'offer');
  check('which says what changed', p.els['upd-msg'].textContent, 'KAM TTS 0.11.0 is available — a change. It is free, as always.');
  check('and links to the release', p.els['upd-notes'].href, 'https://example.invalid/rel');

  p.els['upd-later'].click();
  check('Not now hides the bar', p.bar(), 'hidden');
  check('but not the gold button', p.button(), ['available']);
  await p.updCheck(false);
  check('a later automatic check leaves the bar down for that version', p.bar(), 'hidden');
  p.els['upd-btn'].click();
  check('clicking the button brings it back', p.bar(), 'offer');
  check('versions compare number by number', [p.updIsNewer('0.10.0', '0.9.9'), p.updIsNewer('0.9.9', '0.10.0')], [true, false]);
}

console.log('\n=== installing ===');
{
  const p = page({ release: release('0.11.0') });
  await p.updCheck(false);
  await p.updInstall();
  check('a finished install hands the restart to the worker', p.sent, [{ action: 'updateRestart', from: '0.10.0', to: '0.11.0' }]);
  check('and says it is restarting', [p.bar(), p.els['upd-msg'].textContent], ['working', 'Installed 0.11.0. Restarting KAM TTS…']);
}
{
  const p = page({ release: release('0.11.0'), confirmAnswer: false });
  await p.updCheck(false); await p.updInstall();
  check('saying no to the question changes nothing', [p.sent, p.bar()], [[], 'offer']);
}
{
  const p = page({ release: release('0.11.0'), api: async () => ({ ok: false, reason: 'git could not fast-forward this checkout.' }) });
  await p.updCheck(false); await p.updInstall();
  check('a refusal is shown, with the reason', [p.bar(), p.els['upd-msg'].textContent], ['problem', 'git could not fast-forward this checkout.']);
  check('and it can be tried again', [p.els['upd-now'].hidden, p.els['upd-now'].textContent], [false, 'Try again']);
  check('without restarting anything', p.sent, []);
}
{
  const p = page({ release: release('0.11.0'), api: async () => { throw new Error('no response'); } });
  await p.updCheck(false); await p.updInstall();
  check('a server that is off is named as the reason', p.els['upd-msg'].textContent.includes('power button'), true);
}

console.log('\n=== after the restart ===');
{
  const p = page({ release: release('0.11.0'), version: '0.11.0', stored: { kamUpdated: { at: Date.now(), from: '0.10.0', to: '0.11.0' } } });
  check('the dashboard says what it was updated from', [p.bar(), p.els['upd-msg'].textContent], ['done', 'KAM TTS is now 0.11.0, updated from 0.10.0.']);
  check('once', p.local.kamUpdated, undefined);
}
{
  const p = page({ release: release('0.11.0'), stored: { kamUpdated: { at: Date.now() - 3600e3, from: '0.9.0', to: '0.10.0' } } });
  check('an old note is not shown', p.bar(), 'hidden');
}

// --- the worker's side ---
const grabAsync = name => {
  const i = bg.indexOf(`async function ${name}(`); if (i < 0) throw new Error(`${name} not found`);
  let d = 0, j = i; for (; j < bg.length; j++) { if (bg[j] === '{') d++; else if (bg[j] === '}') { d--; if (!d) { j++; break; } } }
  return bg.slice(i, j);
};
async function resume(stored) {
  const calls = [], local = { ...stored };
  const chrome = { storage: { local: { get: async k => ({ [k]: local[k] }), remove: async k => { delete local[k]; }, set: async o => Object.assign(local, o) } },
                   tabs: { update: async (id, o) => calls.push(['focus', id, o.active]) } };
  // playerTabId is the worker's record of the dashboard tab, set by ensureDashboardTab.
  const fn = new Function('chrome', 'hostStart', 'ensureDashboardTab', 'playerTabId', `
    ${grabAsync('_resumeAfterUpdate')}
    return _resumeAfterUpdate;`)(chrome, () => calls.push(['start']), async () => { calls.push(['dashboard']); }, 7);
  await fn();
  return { calls, local };
}
console.log('\n=== the worker, after the reload ===');
{
  const r = await resume({ kamResume: { at: Date.now(), from: '0.10.0', to: '0.11.0' } });
  check('starts the server and brings the dashboard back to the front', r.calls, [['start'], ['dashboard'], ['focus', 7, true]]);
  check('leaves the dashboard its note', [r.local.kamUpdated && r.local.kamUpdated.to, r.local.kamResume], ['0.11.0', undefined]);
}
{
  const r = await resume({ kamResume: { at: Date.now() - 3600e3, from: '0.9.0', to: '0.10.0' } });
  check('a stale request from an old restart starts nothing', [r.calls, r.local.kamResume], [[], undefined]);
}
{
  const r = await resume({});
  check('an ordinary start is left alone', r.calls, []);
}

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL ? 1 : 0);
