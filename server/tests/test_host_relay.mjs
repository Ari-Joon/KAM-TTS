// The server's lifetime follows whoever holds the native port, because
// kam_host.py stops the server when that port closes. The dashboard once held
// its own, so closing its tab stopped the server. This lifts the real host
// functions out of background.js and dashboard.js, gives them a fake Chrome, and
// checks that only the worker ever connects and that the dashboard still hears
// everything the host says.
import fs from 'node:fs';
const read = f => fs.readFileSync(new URL(`../../extension/${f}`, import.meta.url), 'utf8');
const bg = read('background.js');
const dash = read('dashboard.js');

let PASS=0, FAIL=0;
const check=(l,g,w)=>{ const ok=JSON.stringify(g)===JSON.stringify(w);
  ok?(PASS++,console.log('  ok   '+l)):(FAIL++,console.log(`  FAIL ${l}\n         got ${JSON.stringify(g)} want ${JSON.stringify(w)}`)); };

const grab = (src, name) => {
  let i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`function ${name} not found`);
  if (src.slice(Math.max(0, i - 6), i) === 'async ') i -= 6;   // keep it async
  let d=0, j=i;
  for (; j<src.length; j++){ if(src[j]==='{')d++; else if(src[j]==='}'){d--; if(!d){j++;break;} } }
  return src.slice(i,j);
};

// --- A fake Chrome with one native host ---
function fakeChrome({ refuse = false } = {}) {
  const c = { connects: 0, sent: [], broadcasts: [], port: null, lastError: undefined };
  c.runtime = {
    get lastError() { return c.lastError; },
    connectNative(name) {
      c.connects++;
      if (refuse) throw new Error('Specified native messaging host not found.');
      const port = { name, msg: [], disc: [], closed: false,
        onMessage: { addListener: f => port.msg.push(f) },
        onDisconnect: { addListener: f => port.disc.push(f) },
        postMessage(m) { if (port.closed) throw new Error('closed'); c.sent.push(m.cmd); },
        say(m) { port.msg.forEach(f => f(m)); },
        close(err) { port.closed = true; c.lastError = err ? { message: err } : undefined;
                     port.disc.forEach(f => f()); c.lastError = undefined; } };
      c.port = port;
      return port;
    },
    sendMessage(m) { c.broadcasts.push(m); return Promise.resolve(); },
  };
  return c;
}

const SERVER_URL = 'http://127.0.0.1:5050';
const noFetch = async () => { throw new Error('unexpected HTTP call'); };

function loadWorker(c, kamFetch = noFetch) {
  const consts = bg.slice(bg.indexOf('const HOST_NAME'), bg.indexOf('function hostSnapshot('));
  const code = `
    ${consts}
    ${grab(bg, 'hostSnapshot')}
    ${grab(bg, '_hostBroadcast')}
    ${grab(bg, '_hostConnect')}
    ${grab(bg, 'hostStart')}
    ${grab(bg, 'hostStop')}
    ${grab(bg, 'hostStatus')}
    return { hostStart, hostStop, hostStatus, hostSnapshot, get port(){ return _hostPort; } };`;
  return new Function('chrome', 'kamFetch', 'SERVER', code)({ runtime: c.runtime }, kamFetch, SERVER_URL);
}

const events = c => c.broadcasts.filter(b => b.event).map(b => b.event.type);

console.log('\n=== asking for state never starts a host ===');
{
  const c = fakeChrome(), w = loadWorker(c);
  w.hostStatus();
  check('no port opened by a status request', c.connects, 0);
  check('nothing sent', c.sent, []);
}

console.log('\n=== start, boot, ready: every host message is relayed ===');
{
  const c = fakeChrome(), w = loadWorker(c);
  w.hostStart();
  check('one port', c.connects, 1);
  check('start sent', c.sent, ['start']);
  check('state says booting', w.hostSnapshot().stage, 'process-started');
  c.port.say({ type: 'log', line: '[HOST] Launched server.py (pid 42)' });
  c.port.say({ type: 'stage', stage: 'model-loading' });
  c.port.say({ type: 'pong' });
  c.port.say({ type: 'ready' });
  check('log, stage and ready reach the pages, pong does not', events(c), ['log', 'stage', 'ready']);
  const logEv = c.broadcasts.find(b => b.event && b.event.type === 'log');
  check('log line arrives whole', logEv.event.line, '[HOST] Launched server.py (pid 42)');
  check('every broadcast carries state for the popup', c.broadcasts.every(b => b.state && 'ready' in b.state), true);
  check('ready recorded', w.hostSnapshot().ready, true);
  w.hostStatus();
  check('status asked over the open port', c.sent, ['start', 'status']);
  w.hostStart();
  check('a second start reuses the port', c.connects, 1);
}

console.log('\n=== stop ===');
{
  const calls = [];
  const ok = async (url, opts) => { calls.push([url, opts.method, opts.headers['Content-Type']]); return { ok: true, status: 200 }; };
  const c = fakeChrome(), w = loadWorker(c, ok);
  await w.hostStop();
  check('stop with no port opens no host', c.connects, 0);
  check('it asks the server directly', calls, [[SERVER_URL + '/shutdown', 'POST', 'application/json']]);
  check('and tells the pages', events(c), ['log', 'stopping']);
}
{
  const calls = [];
  const ok = async url => { calls.push(url); return { ok: true, status: 200 }; };
  const c = fakeChrome(), w = loadWorker(c, ok);
  w.hostStart();
  c.port.say({ type: 'status', running: true, pid: 42 });
  check('the pid of a launched server is kept', w.hostSnapshot().pid, 42);
  await w.hostStop();
  check('a server the host launched is stopped through the host', c.sent, ['start', 'stop']);
  check('without an HTTP call', calls, []);
  c.port.say({ type: 'exit', code: 0 });
  check('exit clears running', w.hostSnapshot().running, false);
  check('and the pid', w.hostSnapshot().pid, null);
}
{
  const calls = [];
  const ok = async url => { calls.push(url); return { ok: true, status: 200 }; };
  const c = fakeChrome(), w = loadWorker(c, ok);
  w.hostStart();
  c.port.say({ type: 'log', line: '[HOST] A server is already running on 5050 — using it.' });
  c.port.say({ type: 'ready' });
  await w.hostStop();
  check('a server the host only found is asked directly', calls, [SERVER_URL + '/shutdown']);
  check('the host is not sent a stop it cannot carry out', c.sent, ['start']);
  check('state no longer says ready', [w.hostSnapshot().ready, w.hostSnapshot().stage], [false, '']);
}
{
  const c = fakeChrome(), w = loadWorker(c, async () => ({ ok: false, status: 403 }));
  await w.hostStop();
  check('a refused stop is an error', events(c), ['error']);
  check('which says the token is why', /token/.test(c.broadcasts.at(-1).event.message), true);
}
{
  const c = fakeChrome(), w = loadWorker(c, async () => { throw new TypeError('Failed to fetch'); });
  await w.hostStop();
  check('an unreachable server is an error too', events(c), ['error']);
  check('which says what to do', /close its window/.test(c.broadcasts.at(-1).event.message), true);
}

console.log('\n=== host missing or dropped ===');
{
  const c = fakeChrome({ refuse: true }), w = loadWorker(c);
  w.hostStart();
  check('refused connect leaves no port', w.port, null);
  check('error broadcast', events(c), ['error']);
  check('error explains the repair', /Start KAM TTS\.bat/.test(w.hostSnapshot().error), true);
}
{
  const c = fakeChrome(), w = loadWorker(c);
  w.hostStart();
  const first = c.port;
  first.close('Native host has exited.');
  check('port forgotten on disconnect', w.port, null);
  check('disconnect relayed with Chrome\'s reason',
        c.broadcasts.at(-1).event, { type: 'disconnect', message: 'Native host has exited.' });
  w.hostStart();
  check('next start opens a fresh port', c.connects, 2);
  first.close();
  check('a late close of the old port does not drop the new one', w.port === c.port, true);
}

console.log('\n=== the dashboard only talks to the worker ===');
check('dashboard.js never calls connectNative', /connectNative\s*\(/.test(dash), false);
{
  const log = [], power = [];
  const code = `
    let _serverRunning = false, _serverReady = false, _midToggle = true;
    const addLog = l => log.push(l);
    const _setPower = s => power.push(s);
    const _setStageProgress = s => power.push('stage:' + s);
    const _completePower = () => power.push('on');
    ${grab(dash, '_onHostEvent')}
    return { on: _onHostEvent, get mid(){ return _midToggle; }, get running(){ return _serverRunning; },
             setReady(v){ _serverReady = v; } };`;
  const d = new Function('log', 'power', code)(log, power);
  d.on({ type: 'log', line: 'hello' });
  d.on({ type: 'stage', stage: 'model-loading' });
  d.on({ type: 'ready' });
  check('log line printed, a stage starts the ring and draws, ready turns it on', [log, power],
        [['hello', '[HOST] ✓ Model Ready'], ['loading', 'stage:model-loading', 'on']]);
  check('ready means running', d.running, true);
  power.length = 0;
  d.on({ type: 'stage', stage: 'standby' });
  check('a stage after ready leaves the button alone', power, ['stage:standby']);
  d.on({ type: 'stopping' });
  check('stopping ends the toggle so the poll can turn it off', d.mid, false);
  log.length = 0; power.length = 0;
  d.on({ type: 'error', message: 'This server was not started from Chrome' });
  check('error while serving is logged but leaves the button alone', [log.length, power], [1, []]);
  check('error ends the toggle', d.mid, false);
  d.setReady(false); power.length = 0;
  d.on({ type: 'error', message: 'Native host not found' });
  check('error while not serving stops the spinner', power, ['off']);
  log.length = 0;
  d.on({ type: 'disconnect', message: '' });
  check('a quiet disconnect adds no line', log, []);
}

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL?1:0);
