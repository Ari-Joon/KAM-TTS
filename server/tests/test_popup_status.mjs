// What the popup's status bar says, from the real statusView() in popup.js.
// Two things went wrong here: there was no way to stop the server from the
// popup, and the start-up progress sometimes never appeared, since it waited
// for the first stage to arrive and ignored the stretch between "ready" and
// the server answering.
import fs from 'node:fs';
const src = fs.readFileSync(new URL('../../extension/popup.js', import.meta.url), 'utf8');

let PASS=0, FAIL=0;
const check=(l,g,w)=>{ const ok=JSON.stringify(g)===JSON.stringify(w);
  ok?(PASS++,console.log('  ok   '+l)):(FAIL++,console.log(`  FAIL ${l}\n         got ${JSON.stringify(g)} want ${JSON.stringify(w)}`)); };

const grab = name => {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`function ${name} not found`);
  let d=0, j=i;
  for (; j<src.length; j++){ if(src[j]==='{')d++; else if(src[j]==='}'){d--; if(!d){j++;break;} } }
  return src.slice(i,j);
};
const boot = src.slice(src.indexOf('const BOOT_STEPS'), src.indexOf('let _serverOnline'));
const grace = src.match(/const START_GRACE_MS[^;]*;/)[0];
const { statusView, START_GRACE_MS } = new Function(`
  ${boot}
  ${grace}
  ${grab('_stepFor')}
  ${grab('statusView')}
  return { statusView, START_GRACE_MS };`)();

const NOW = 1_000_000_000;
const pick = v => ({ text: v.text, step: v.step, button: v.button, disabled: v.disabled });

console.log('\n=== offline and online ===');
check('idle and off offers start', pick(statusView(false, {}, {}, NOW)),
      { text: 'Server offline', step: '', button: 'start', disabled: false });
check('online offers stop', pick(statusView(true, {}, {}, NOW)),
      { text: 'Server online', step: '', button: 'stop', disabled: false });
check('online dot', statusView(true, {}, {}, NOW).dot, 'online');

console.log('\n=== starting ===');
{
  const v = statusView(false, {}, { startAt: NOW - 400 }, NOW);
  check('progress shows at once after a click, before any stage',
        pick(v), { text: 'Starting the server…', step: 'Starting up', button: 'start', disabled: true });
  check('with the rail drawn', typeof v.pct, 'number');
}
{
  const host = { connected: true, stage: 'model-loading', startedAt: NOW - 20000 };
  check('a popup opened mid-boot picks up the stage',
        statusView(false, host, {}, NOW).step, 'Loading the voice model');
}
{
  const host = { connected: true, ready: true, stage: 'ready', startedAt: NOW - 20000 };
  const v = statusView(false, host, {}, NOW);
  check('ready but not yet answering is warming up, not offline',
        [v.text, v.step, v.pct], ['Starting the server…', 'Warming up the voice model', 100]);
}
{
  const host = { connected: true, stage: 'model-loading', startedAt: NOW - START_GRACE_MS - 1 };
  check('a stage left behind by a dead server does not spin forever',
        pick(statusView(false, host, {}, NOW)).button + '/' + statusView(false, host, {}, NOW).disabled, 'start/false');
}
{
  const host = { connected: false, stage: 'model-loading', startedAt: NOW - 1000 };
  check('nor does one from a host that has gone', statusView(false, host, {}, NOW).text, 'Server offline');
}
{
  const host = { connected: true, stage: 'flask-import', startedAt: NOW - 1000, error: 'Server stopped before ready (exit 1).' };
  check('a failed boot says why and offers start again',
        pick(statusView(false, host, { startAt: NOW - 1000 }, NOW)),
        { text: 'Server stopped before ready (exit 1).', step: '', button: 'start', disabled: false });
}

console.log('\n=== stopping ===');
check('stop pressed, still answering', pick(statusView(true, {}, { stopAt: NOW - 1000 }, NOW)),
      { text: 'Stopping the server…', step: '', button: 'stop', disabled: true });
check('a stop that never took effect gives the button back',
      pick(statusView(true, {}, { stopAt: NOW - 60000 }, NOW)).disabled, false);
check('a failed stop explains itself for a few seconds',
      statusView(true, {}, { note: 'Could not reach the server to stop it.', noteAt: NOW - 2000 }, NOW).step,
      'Could not reach the server to stop it.');
check('and then goes quiet',
      statusView(true, {}, { note: 'Could not reach the server to stop it.', noteAt: NOW - 60000 }, NOW).step, '');

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL?1:0);
