/**
 * dashboard.js — TTS Quality Monitor Dashboard
 * All server calls proxied via background.js (MV3 CSP).
 */
'use strict';

const SERVER = 'http://127.0.0.1:5050';

// Maps each issue type to the fix the model applies automatically, plus a plain
// explanation shown live when the user picks an issue. Single source of truth
// for both the preview and submitReport, so they can never disagree.
const ISSUE_ACTION_MAP = {
  PRONUNCIATION: { action: 'ADD_TO_STORE',     desc: 'Saves the correct pronunciation so this word is always read right from now on.' },
  HALLUCINATION: { action: 'BLACKLIST',        desc: 'Blocks the extra word the voice invented so it stops adding it.' },
  SKIP:          { action: 'ADJUST_CHUNK',     desc: 'Adjusts how this sentence is split so the dropped word is no longer skipped.' },
  TOO_FAST:      { action: 'ADJUST_RATE_DOWN', desc: 'Slows the speaking rate slightly for this kind of sentence.' },
  TOO_SLOW:      { action: 'ADJUST_RATE_UP',   desc: 'Speeds the speaking rate slightly for this kind of sentence.' },
  ROBOTIC:       { action: 'ADJUST_TEMP_UP',   desc: 'Raises expressiveness so the voice sounds less flat and monotone.' },
  VOICE:         { action: 'ADJUST_TEMP_DOWN', desc: 'Lowers variability so the voice stays steady and stops drifting.' },
  EQUATION:      { action: 'MATH_OVERRIDE',    desc: 'Teaches how this equation or symbol should be read aloud, used from now on.' },
  PUNCT:         { action: 'PUNCT',            desc: 'Learns the correct pause or break for this punctuation.' },
  CUTOFF:        { action: 'ADJUST_CHUNK',     desc: 'Splits this sentence shorter so it is no longer cut off early.' },
  SUPPRESS:      { action: 'SUPPRESS',         desc: 'Marks this text to be skipped entirely in future reads.' },
  OTHER:         { action: 'FLAG_PATTERN',     desc: 'Flags this for review so a pattern can be spotted over time.' },
};

let _lastChunkId   = null;
let _lastChunkText = '';
// When the user explicitly clicks a chunk to report on, lock the report
// selection so live playback (TTS log lines / chunkReady messages) can't
// overwrite it. Cleared when the report is submitted or cleared.
let _reportLocked  = false;
let _feedSelectMode = false;  // live-feed mass-select mode
let _chunkCount    = 0;
let _aiLastCount   = -1;   // tracks chunk count to auto-refresh AI feed on change
let _aiPollTick    = 0;    // periodic AI refresh so async analysis appears live
let _logLines      = [];
let _lastLoggedRaw = null;   // suppress consecutive duplicate console lines
let _logSince      = 0;
let _firstConsolePoll = true;   // skip rendering the backlog on the first poll
let _allChunks     = [];
let _browserFilter = 'low';
let _browserOffset = 0;

// --- Proxy ---
// --- Toast helper and surfaces API errors to the user ---
// Instead of silently swallowing fetch failures with .catch(()=>{}), show a
// short-lived toast at the bottom of the screen so the user knows something
// actually went wrong.
let _toastEl = null;
function showToast(msg, kind = 'error') {
  if (!_toastEl) {
    _toastEl = document.createElement('div');
    _toastEl.style.cssText =
      'position:fixed;bottom:18px;left:50%;transform:translateX(-50%);' +
      'background:#1a0a0a;color:#fca5a5;border:1px solid #dc2626;' +
      'border-radius:6px;padding:10px 16px;font:12px/1.4 var(--font,sans-serif);' +
      'z-index:99999;max-width:520px;box-shadow:0 6px 24px rgba(0,0,0,0.5);' +
      'opacity:0;transition:opacity 0.2s';
    document.body.appendChild(_toastEl);
  }
  if (kind === 'info') {
    _toastEl.style.background = '#0a1a14';
    _toastEl.style.color      = '#86efac';
    _toastEl.style.borderColor = '#16a34a';
  } else {
    _toastEl.style.background = '#1a0a0a';
    _toastEl.style.color      = '#fca5a5';
    _toastEl.style.borderColor = '#dc2626';
  }
  _toastEl.textContent = msg;
  _toastEl.style.opacity = '1';
  clearTimeout(_toastEl._timer);
  _toastEl._timer = setTimeout(() => { _toastEl.style.opacity = '0'; }, 4500);
}

function api(path, method, body) {
  return new Promise((resolve, reject) => {
    function attempt(n) {
      chrome.runtime.sendMessage(
        { action:'dashboardFetch', path, method:method||'GET', body:body||null },
        resp => {
          if (chrome.runtime.lastError) {
            if (n > 0) { setTimeout(() => attempt(n-1), 400); return; }
            reject(new Error(chrome.runtime.lastError.message)); return;
          }
          if (resp && resp.ok) resolve(resp.data);
          else reject(new Error(resp ? resp.error : 'no response'));
        }
      );
    }
    attempt(2);
  });
}

// --- Tabs ---
const TABS = ['console','chunks','report','rules','stats','ai'];

function showTab(name) {
  TABS.forEach(t => {
    const b = document.querySelector(`[data-tab="${t}"]`);
    const p = document.getElementById('panel-' + t);
    if (b) b.classList.toggle('active', t === name);
    if (p) p.classList.toggle('active', t === name);
  });
  // The active-tab marker on <body> drives the layout overrides, so the AI tab
  // hides the left console and lets the right panel take the full width.
  // See body[data-active-tab="ai"] rules in player.html.
  document.body.setAttribute('data-active-tab', name);
  // Always set the right panel width, since a stale value leaves a gap. Skipped
  // on the AI tab, where the data-active-tab CSS rule forces width:100% anyway.
  const rp = document.getElementById('right-panel');
  if (rp && name !== 'ai' && !rp.style.width) rp.style.width = '320px';
  if (name === 'stats')  refreshStats();
  if (name === 'rules')  refreshRules();
  if (name === 'report') { refreshReports(); refreshChunkSelector(); }
  if (name === 'ai')     refreshAI();
  if (name === 'chunks') loadBrowser('low', 0);
}

// Icons for the content facets shown on each chunk card. Declared at top
// level because both the live feed and the chunk browser render badges.
const _FACET_ICON = { math:'∑', list:'•', definition:'≡', code:'⌘', quote:'❝', citation:'¶' };

// --- Console ---
const ANSI = /\x1b\[[0-9;]*m/g;
const TAG_CLR = {
  TTS:'tag-tts',PROSODY:'tag-prosody',SKIP:'tag-skip',ERROR:'tag-error',
  RETRY:'tag-retry',PRONOUNCE:'tag-pronounce',LEARNER:'tag-learner',
  DEDUP:'tag-dedup',CODE:'tag-code',AUDIO:'tag-audio',
  WARN:'tag-info',FATAL:'tag-error',RECOVER:'tag-retry',MATH:'tag-math'
};
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// --- Plain-English rule explanations ---
// Turns a stored rule into a sentence the user can actually understand, and
// explains what the "hits" number means for that rule type.
function describeRule(r) {
  const p = esc(r.pattern || '');
  const v = esc(r.value || '');
  switch ((r.rule_type || '').toUpperCase()) {
    case 'PRONUNCIATION':
      return `When KAM sees “${p}”, it now says “${v}” instead.`;
    case 'BLACKLIST':
      return `KAM removes “${p}” before speaking (it was crashing or garbling synthesis).`;
    case 'SPLIT':
      return `KAM starts a new sentence at “${p}” so it’s read more clearly.`;
    case 'PUNCT':
      return `KAM adjusts punctuation around “${p}” to fix phrasing.`;
    case 'MONITOR':
      return `KAM is watching “${p}”. If it keeps causing issues, it’ll auto-split it.`;
    case 'SUPPRESS':
      return `KAM skips “${p}” entirely (you marked it irrelevant).`;
    case 'FLAG':
      return `KAM noticed “${p}” but takes no action — just tracking it.`;
    default:
      return `Rule on “${p}”.`;
  }
}

// What the hits counter means depends on whether the rule changes audio.
function hitsLabel(r) {
  const t = (r.rule_type || '').toUpperCase();
  const n = r.hits || 0;
  if (t === 'FLAG' || t === 'MONITOR') {
    return `seen ${n}×`;            // non-mutating: just observed
  }
  return `applied ${n}×`;            // mutating: actually changed audio N times
}

function addLog(raw) {
  raw = (raw||'').replace(ANSI,'').trim();
  if (!raw || raw.startsWith('127.0.0.1')) return;
  const el = document.getElementById('console-log'); if (!el) return;

  // --- Declutter: suppress repetitive, low-signal lines ---
  // These add noise without telling the user anything actionable. The full
  // detail still exists server-side; the dashboard just doesn't echo every one.
  const _noise = [
    /Whisper ready \(CPU\)/i,           // printed before every analysis
    /store reloaded — \d+ entries/i,    // pronunciation store hot-reload
    /\[INFER\]/i,                        // dev timing probe
    /Importing whisper/i,
    /Loading Whisper tiny/i,
  ];
  if (_noise.some(rx => rx.test(raw))) return;
  // Collapse consecutive identical lines (the feed sometimes echoes a line
  // twice); skip if this line is identical to the immediately previous one.
  if (raw === _lastLoggedRaw) return;
  _lastLoggedRaw = raw;

  const now = new Date().toLocaleTimeString('en-GB',{hour12:false});
  const div = document.createElement('div');
  if (raw.startsWith('──')||raw.startsWith('─')) {
    div.className = 'log-line line-sep';
  } else {
    div.className = 'log-line';
    const m = raw.match(/^\s*\[([A-Z_\-]+)\]\s*(.*)/s);
    if (m) {
      const tag = m[1].split('-')[0];
      const lc  = (tag==='ERROR'||tag==='FATAL')?' line-error':(tag==='SKIP'||tag==='DEDUP')?' line-skip':'';
      div.className += lc;
      div.innerHTML = `<span class="log-time">${now}</span>`
        +`<span class="log-tag ${TAG_CLR[tag]||'tag-info'}">[${tag}]</span>`
        +`<span class="log-msg">${esc(m[2])}</span>`;
      if (tag==='TTS' && !_reportLocked) {
        const tm = m[2].match(/\((\d+)c\)\s*(.*)/);
        if (tm) {
          _lastChunkText = tm[2];
          const pr = document.getElementById('report-preview');
          if (pr) pr.textContent = _lastChunkText.substring(0,120);
        }
      }
    } else {
      div.innerHTML = `<span class="log-time">${now}</span><span class="log-msg">${esc(raw)}</span>`;
    }
  }
  // Stick to the bottom only if that is where the reader already was.
  //
  // This used to set scrollTop unconditionally on every single line, so the
  // moment you scrolled up to read something the next log line yanked you back
  // down. Checking first means scrolling up is enough to hold your place, and
  // scrolling back to the bottom resumes the follow. The 40px tolerance covers
  // sub-pixel heights and a fractional scrollTop, which otherwise leave you a
  // pixel short of the bottom and stop the follow for no visible reason.
  const wasAtBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
  el.appendChild(div); _logLines.push(div);
  if (_logLines.length > 500) _logLines.shift().remove();
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}
function clearConsole() {
  const el = document.getElementById('console-log'); if(el) el.innerHTML=''; _logLines=[];
  // Also reset stats DB so the stats page starts from zero
  api('/report/stats/reset','POST').then(()=>{
    _chunkCount=0; _allChunks=[];
    const ctr=document.getElementById('chunk-counter'); if(ctr) ctr.textContent='0 chunks';
    const cb=document.getElementById('chunks-body'); if(cb) cb.innerHTML='';
    const bb=document.getElementById('browser-body'); if(bb) bb.innerHTML='';
    refreshStats();
    addLog('[LEARNER] Console + stats cleared');
  }).catch(()=>{ addLog('[WARN] Could not reset stats. Is the server offline?'); });
}

// --- Poll console ---
function pollConsole() {
  api(`/console?since=${_logSince}`)
    .then(d => {
      if (d.lines && d.lines.length) {
        // On the first successful poll the server returns its whole backlog, but
        // the power-button host has already streamed those startup lines to the
        // console during boot. Rendering them again is the duplication. So on
        // the first poll we just sync the cursor to the end WITHOUT rendering;
        // from then on only genuinely new lines are shown.
        if (_firstConsolePoll) {
          _firstConsolePoll = false;
        } else {
          d.lines.forEach(l => addLog(l));
        }
        _logSince = d.cursor || (_logSince + d.lines.length);
      }
      const el = document.getElementById('server-status');
      if (el) { el.textContent='● online'; el.className='pill online'; }
      if (typeof _markServerOnline === 'function') _markServerOnline(true);
    })
    .catch(()=>{
      const el=document.getElementById('server-status');
      if(el){el.textContent='○ offline';el.className='pill offline';}
      if (typeof _markServerOnline === 'function') _markServerOnline(false);
    });
}


// --- Autonomous self-tuning toggle ---
// The switch previously had no handler and no endpoint: clicking it did nothing
// and the caption was static text. This wires it to /autotune and reports the
// real state, including the honest reason it might not be adjusting anything
// yet, since self-tuning needs a minimum number of observations per fingerprint
// before it will touch a parameter).
function _refreshAutotune() {
  const box = document.getElementById('autotune-toggle');
  const status = document.getElementById('autotune-status');
  if (!box || !status) return;
  api('/autotune').then(d => {
    box.checked = !!d.enabled;
    const ready = d.profiles_ready || 0;
    const obs   = d.observations || 0;
    const min   = d.min_samples || 12;
    if (!d.enabled) {
      status.innerHTML = `<span style="color:var(--dim)">Off.</span> KAM records quality but never changes `
        + `its own settings. Turn on to let it adjust per chunk fingerprint, with a rollback tripwire `
        + `if quality drops. <span style="color:var(--subtext)">${obs} observations recorded, `
        + `${ready} fingerprint${ready===1?'':'s'} ready.</span>`;
    } else if (ready === 0) {
      status.innerHTML = `<span style="color:var(--yellow)">On — gathering evidence.</span> `
        + `No fingerprint has the ${min} observations needed yet, so nothing is being adjusted. `
        + `<span style="color:var(--subtext)">${obs} recorded so far — keep reading and it will begin.</span>`;
    } else {
      status.innerHTML = `<span style="color:var(--green)">On and active.</span> `
        + `${ready} fingerprint${ready===1?'':'s'} have enough evidence to self-tune `
        + `(${min}+ observations each). <span style="color:var(--subtext)">${obs} observations recorded. `
        + `Changes appear under Quality Insights and in the Rules history.</span>`;
    }
  }).catch(() => { status.textContent = 'Server offline — self-tuning state unknown.'; });
}

function _wireAutotune() {
  const box = document.getElementById('autotune-toggle');
  if (!box || box.dataset.wired) return;
  box.dataset.wired = '1';
  box.addEventListener('change', () => {
    const want = box.checked;
    api('/autotune', 'POST', { enabled: want })
      .then(() => {
        showToast(want ? 'Autonomous self-tuning ON' : 'Autonomous self-tuning OFF');
        _refreshAutotune();
      })
      .catch(() => { box.checked = !want; showToast('Could not change self-tuning'); });
  });
  _refreshAutotune();
}

// --- Live chunk feed ---
// RECONCILING renderer. The previous version inserted only "new" chunks and
// relied on a client-side seen-set to dedup. That drifted badly, because
// chunk_id is a CONTENT HASH: re-reading the same text reuses ids, so chunks
// were wrongly suppressed and the feed ended up missing most of a read.
//
// This version instead makes the DOM match the server response every poll:
// one card per returned chunk, in timestamp order, updated in place when the
// data changes. It's idempotent, so it can't drift however the polls interleave.
function _chunkCardHTML(row, q) {
  const fbUp   = row.user_feedback === 'positive' ? ' confirmed' : '';
  const fbDown = row.user_feedback === 'negative' ? ' rejected'  : '';
  return `<div class="chunk-meta">`
    +(row.seq?`<span class="chunk-badge chunk-num">chunk ${row.seq}/${row.total}</span>`:'')
    +`<span class="chunk-badge type-${row.sentence_type||''}">${row.sentence_type||'?'}</span>`
    +(row.primary_facet && row.primary_facet !== 'prose'
        ? `<span class="chunk-badge facet-${esc(row.primary_facet)}" title="Content facets detected in this chunk${row.facet_count>1?` (${row.facet_count} in total)`:''} — these shape how it is read and how it is learned from">${_FACET_ICON[row.primary_facet]||''} ${esc(row.primary_facet)}${row.facet_count>1?' +'+(row.facet_count-1):''}</span>`
        : (row.has_math?`<span class="chunk-badge facet-math" title="Contains an equation">∑ math</span>`:''))
    +`<span class="chunk-badge">${row.char_count||0}c</span>`
    +(q!=null?`<span class="chunk-badge">${(q*100).toFixed(0)}%</span>`:'')
    +(row.quality_flags?`<span class="chunk-badge warn">${esc(row.quality_flags)}</span>`:'')
    +`<button class="chunk-thumb${fbUp}" data-action="positive" data-chunk-id="${esc(row.chunk_id||'')}" title="Perfect chunk. Reinforce it">👍</button>`
    +`<button class="chunk-thumb chunk-thumb-down${fbDown}" data-action="negative" data-chunk-id="${esc(row.chunk_id||'')}" title="Broken chunk. Log it as a hallucination">👎</button>`
    +`</div>`
    +`<div class="chunk-text">${esc(row.chunk_text||'')}</div>`
    +(row.whisper_accuracy!=null?`<div class="chunk-quality">Whisper ${(row.whisper_accuracy*100).toFixed(0)}% · Pitch σ${row.pitch_variance!=null?row.pitch_variance:'—'}</div>`:'');
}

// Mark a card as rated, so the whole row shows its verdict rather than just the
// small button. Applied optimistically on click and rolled back if the request
// fails, so the click always feels immediate but never lies about what stuck.
// state is 'up', 'down', or null to clear.
function _setCardRated(card, state) {
  if (!card) return;
  card.classList.remove('rated-up', 'rated-down', 'just-rated');
  if (!state) return;
  card.classList.add(state === 'up' ? 'rated-up' : 'rated-down');
  // Restart the flash animation. Removing the class and reading offsetWidth
  // forces a reflow, without which re-adding it in the same frame is a no-op
  // and a second rating on the same card would not flash.
  void card.offsetWidth;
  card.classList.add('just-rated');
}

const _FEED_CAP = 40;   // cards kept in the DOM

// Feed pause. While paused the poll still runs and the data is still kept
// current, but nothing is rendered, so cards stay exactly where they are while
// you read and rate them.
//
// Before this the only way to hold the feed still was to scroll away from the
// top, which is fragile: it is easy to drift back to the top by accident, and
// the reconcile re-appends every card each poll, so even scrolled away the
// viewport shifts under you.
let _feedPaused = false;
let _feedPendingRows = null;   // newest data received while paused

function _setFeedPaused(paused) {
  _feedPaused = paused;
  const btn   = document.getElementById('feed-pause');
  const badge = document.getElementById('feed-paused-badge');
  if (btn) {
    btn.textContent = paused ? '▶' : '⏸';
    btn.style.color       = paused ? '#000' : 'var(--subtext)';
    btn.style.background  = paused ? 'var(--accent, #f5c518)' : 'transparent';
    btn.style.borderColor = paused ? 'var(--accent, #f5c518)' : 'var(--border)';
    btn.title = paused
      ? 'Resume the live feed'
      : 'Freeze the feed so you can read and rate without cards moving. New chunks keep arriving and appear when you resume.';
  }
  if (badge) badge.style.display = paused ? '' : 'none';

  // On resume, draw whatever arrived while we were frozen and return to the
  // newest chunk, which is at the top.
  if (!paused) {
    const pending = _feedPendingRows;
    _feedPendingRows = null;
    if (pending) _renderChunkFeed(pending);
    const body = document.getElementById('chunks-body');
    if (body) body.scrollTop = 0;
  }
}

function _updatePausedBadge(n) {
  const badge = document.getElementById('feed-paused-badge');
  if (badge) badge.textContent = n > 0 ? `PAUSED · ${n} NEW` : 'PAUSED';
}

function pollChunks() {
  api('/report/history?limit=' + _FEED_CAP)
    .then(rows => {
      if (!rows || !rows.length) return;
      const prevTop = _allChunks && _allChunks.length ? _allChunks[0].chunk_id : null;
      _allChunks = rows;

      // Paused: keep the data fresh and count what is waiting, but do not touch
      // the DOM. The count is how many chunks are newer than the one currently
      // at the top of the frozen view.
      if (_feedPaused) {
        _feedPendingRows = rows;
        let n = 0;
        const body = document.getElementById('chunks-body');
        const topCard = body && body.querySelector('.chunk-card');
        const frozenTop = topCard ? topCard.dataset.id : prevTop;
        for (const r of rows) { if (r.chunk_id === frozenTop) break; n++; }
        _updatePausedBadge(n);
        return;
      }
      _renderChunkFeed(rows);
    }).catch(err => {
      // Never swallow silently: a render error here empties the feed and looks
      // exactly like "the server sent nothing", which is what hid a scope bug
      // through two rounds of debugging.
      console.error('[KAM] live feed render failed:', err);
      const ctr = document.getElementById('chunk-counter');
      if (ctr) ctr.textContent = 'feed error — see console';
    });
}

function _renderChunkFeed(rows) {
  const body = document.getElementById('chunks-body');
  if (!body) return;
  // Never fight the user mid-interaction: skip reconciling while chunks are
  // selected for mass-rating, or the cards would be rebuilt under them.
  if (_feedSelectMode && body.querySelector('.chunk-card.feed-selected')) return;

  // Reconciling re-appends every card, which moves them in the DOM and can
  // shift what you are looking at. Remember where the reader was and put
  // them back afterwards, unless they were at the very top, where following
  // the newest chunk is the point.
  const prevScroll = body.scrollTop;

  // Server order is newest-first; render oldest-first so the newest ends on
  // top after we append in order.
  const ordered = rows.filter(r => r.chunk_id)
    .slice()
    .sort((a,b) => ((a.ts||0)-(b.ts||0)) || ((a.seq||0)-(b.seq||0)));

  const existing = new Map();
  body.querySelectorAll('.chunk-card').forEach(c => existing.set(c.dataset.id, c));

  const desiredTopDown = [];   // newest → oldest, i.e. final DOM order
  for (let i = ordered.length - 1; i >= 0; i--) desiredTopDown.push(ordered[i]);

  desiredTopDown.forEach(row => {
    const q = row.quality_score;
    const cls = q==null ? '' : q>=0.75 ? 'quality-high' : q>=0.5 ? 'quality-mid' : 'quality-low';
    let card = existing.get(row.chunk_id);
    if (!card) {
      card = document.createElement('div');
      card.dataset.id = row.chunk_id || '';
      card.title = 'Click chunk text to report. Thumbs-up confirms good quality.';
      card.dataset.sig = '';
    }
    // Rebuild the inner markup only when something actually changed, so we
    // never clobber hover/selection state on every 3s poll.
    const sig = [row.ts, q, row.quality_flags, row.user_feedback,
                 row.whisper_accuracy, row.seq, row.total].join('|');
    if (card.dataset.sig !== sig) {
      card.innerHTML = _chunkCardHTML(row, q);
      card.dataset.sig = sig;
    }
    card.dataset.text = row.chunk_text || '';
    // Preserve the pinned marker; refresh only the quality classes.
    // The rated marker comes from the server's stored verdict, so a rating
    // survives a refresh, a restart and the 3s reconcile. Only an explicit
    // thumbs counts here: 'solid' means the chunk was auto-digested as
    // heard-and-fine, which is not the same as the user having judged it.
    const pinned = card.classList.contains('pinned');
    const rated = row.user_feedback === 'positive' ? ' rated-up'
                : row.user_feedback === 'negative' ? ' rated-down' : '';
    card.className = 'chunk-card ' + cls + (pinned ? ' pinned' : '') + rated;
    body.appendChild(card);          // appending in top-down order sorts them
    existing.delete(row.chunk_id);
  });

  // Anything the server no longer returns has aged out of the window.
  existing.forEach(c => c.remove());

  _chunkCount = rows.length ? (rows[0].total || rows.length) : 0;
  const ctr = document.getElementById('chunk-counter');
  if (ctr) ctr.textContent = _chunkCount + ' chunks';
  while (body.children.length > _FEED_CAP) body.lastChild.remove();

  // Put the reader back where they were. At the very top we leave it there
  // so the newest chunk stays in view, which is the whole point of a live
  // feed; anywhere else, restoring the offset stops the list sliding around
  // while they read.
  if (prevScroll > 4) body.scrollTop = prevScroll;
}

// A plain-English explanation of why a chunk was flagged and what KAM does about
// it, which is what makes the Chunk Browser an analysis view rather than just a
// duplicate of the live feed.
// It's compact and colour-coded, mirroring the console's at-a-glance style.
// Each issue is a single coloured tag with a short fix note, so the reader sees
// the TYPE of problem instantly rather than reading a paragraph.
function explainChunkAnalysis(row) {
  if (row.success === 0) {
    return `<span class="an-tag an-error">ERROR</span><span class="an-note">Synthesis failed for this chunk</span>`;
  }
  const flags = (row.quality_flags || '').split('|').filter(Boolean);
  if (!flags.length) {
    return `<span class="an-tag an-clean">CLEAN</span><span class="an-note">Spoken accurately, no issues found</span>`;
  }
  const tags = [];
  for (const f of flags) {
    if (f.startsWith('WHISPER_SKIP')) {
      const words = (f.split(':')[1] || '').replace(/,/g, ', ');
      tags.push(`<span class="an-tag an-skip">SKIP</span><span class="an-note">Whisper did not hear: ${esc(words)}. A spell-out rule was added so it is read clearly next time.</span>`);
    } else if (f.startsWith('HALLUCINATION')) {
      tags.push(`<span class="an-tag an-halluc">HALLUC</span><span class="an-note">Heard words that were not in the text. Temperature was lowered to keep the voice on script.</span>`);
    } else if (f === 'CUTOFF') {
      tags.push(`<span class="an-tag an-cutoff">CUTOFF</span><span class="an-note">Audio ended before the sentence finished. If this repeats, the chunk will be split shorter.</span>`);
    } else if (f === 'VOICE_DRIFT') {
      tags.push(`<span class="an-tag an-drift">DRIFT</span><span class="an-note">The voice wandered from its usual tone. Temperature was nudged down to steady it.</span>`);
    } else {
      tags.push(`<span class="an-tag">${esc(f.replace(/_/g, ' '))}</span>`);
    }
  }
  return tags.join('');
}

// --- Chunk browser → Whisper Analysis view ---
// Shows chunks the analysis flagged (low / flagged / error / skipped) with the
// reasoning above. Distinct from the Live Feed (which is the rolling rate-this
// strip for every chunk as it plays).
function loadBrowser(filter, offset) {
  _browserFilter=filter; _browserOffset=offset;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.toggle('active',b.dataset.filter===filter));
  const info = document.getElementById('browser-info');
  if (info) info.textContent='Loading…';
  api(`/report/chunks?filter=${filter}&limit=50&offset=${offset}`)
    .then(d => {
      const body=document.getElementById('browser-body'); if(!body) return;
      if (offset===0) body.innerHTML='';
      const total = d.total || 0;
      if (info) info.textContent=total===0
        ? `No "${filter}" chunks — nothing needs attention here.`
        : `${total} ${filter} chunk${total===1?'':'s'} — showing ${offset+1}–${Math.min(offset+50,total)}`;
      if (total===0) return;
      const more=document.getElementById('btn-load-more');
      if (more) more.style.display=(offset+50<d.total)?'block':'none';
      (d.chunks||[]).forEach(row => {
        const q=row.quality_score;
        const cls=q==null?'':q>=0.75?'quality-high':q>=0.5?'quality-mid':'quality-low';
        const card=document.createElement('div');
        card.className='chunk-card '+cls+(row.success===0?' quality-low':'');
        card.dataset.text=row.chunk_text||''; card.dataset.id=row.chunk_id||''; card.title='Click to select for report';
        const flags=(row.quality_flags||'').split('|').filter(Boolean);
        card.innerHTML=
          `<div class="chunk-meta">`
          +(row.seq?`<span class="chunk-badge chunk-num">chunk ${row.seq}/${row.total}</span>`:'')
          +`<span class="chunk-badge type-${row.sentence_type||''}">${row.sentence_type||'?'}</span>`
          +(row.primary_facet && row.primary_facet !== 'prose'
              ? `<span class="chunk-badge facet-${esc(row.primary_facet)}" title="Content facets detected in this chunk${row.facet_count>1?` (${row.facet_count} in total)`:''} — these shape how it is read and how it is learned from">${_FACET_ICON[row.primary_facet]||''} ${esc(row.primary_facet)}${row.facet_count>1?' +'+(row.facet_count-1):''}</span>`
              : (row.has_math?`<span class="chunk-badge facet-math" title="Contains an equation">∑ math</span>`:''))
          +`<span class="chunk-badge">${row.char_count||0}c</span>`
          +(q!=null?`<span class="chunk-badge" style="color:${q>=0.75?'var(--green)':q>=0.5?'var(--yellow)':'var(--red)'}">${(q*100).toFixed(0)}%</span>`:'<span class="chunk-badge">not analysed</span>')
          +(row.success===0?'<span class="chunk-badge" style="color:var(--red)">ERROR</span>':'')
          +(row.skipped?'<span class="chunk-badge" style="color:var(--dim)">SKIP</span>':'')
          +`</div>`
          +`<div class="chunk-text">${esc(row.chunk_text||'')}</div>`
          +`<div class="chunk-analysis">${explainChunkAnalysis(row)}</div>`;
        body.appendChild(card);
      });
    }).catch((err)=>{
      const info=document.getElementById('browser-info');
      if(info) info.textContent=`Load failed — try restarting server (${err.message||'offline'})`;
    });
}

// --- Stats ---
function refreshStats() {
  api('/report/stats').then(d => {
    const c=d.chunks||{};
    const set=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=v;};
    set('s-total',   c.total   ??'—');
    set('s-quality', c.avg_quality?(c.avg_quality*100).toFixed(0)+'%':'—');
    set('s-high',    c.high_quality??'—');
    set('s-low',     c.low_quality ??'—');
    set('s-success', c.success  ??'—');
    set('s-errors',  c.errors   ??'—');
    set('s-skipped', c.skipped  ??'—');
    set('s-analysed',c.analysed ??'—');
    set('s-flagged', c.flagged  ??'—');
    set('s-reports', d.reports  ??'—');
    set('s-rules',   d.rules    ??'—');
    set('s-blacklist',d.blacklist??'—');
    const ws=document.getElementById('s-whisper');
    if(ws){ws.textContent=d.whisper_available?'✓ ready':'✗ not installed';ws.className='val '+(d.whisper_available?'ok':'warn');}
    const lb=document.getElementById('s-librosa');
    if(lb){lb.textContent=d.librosa_available?'✓ ready':'✗ not installed';lb.className='val '+(d.librosa_available?'ok':'warn');}
    if(d.baseline){
      set('s-pitch',  d.baseline.pitch_mean   ?d.baseline.pitch_mean.toFixed(1)+' Hz':'—');
      set('s-energy', d.baseline.energy_mean  ?d.baseline.energy_mean.toFixed(3):'—');
      set('s-rate',   d.baseline.speaking_rate?d.baseline.speaking_rate.toFixed(1)+' onsets/s':'—');
      set('s-samples',d.baseline.sample_count??'—');
    }
  }).catch(()=>{});
  refreshHistory();
}

// --- Model learning history (permanent and survives "clear") ---
const _HIST_ICON = { BLACKLIST:'🚫', PRONUNCIATION:'🗣', SPLIT:'✂', PUNCT:'❚',
  SUPPRESS:'🔇', RATE:'⏩', TEMP:'🌡', CONFIRM:'👍', REPORT:'📝', MONITOR:'👁' };
function refreshHistory() {
  api('/report/learning?limit=200').then(d => {
    const s = d.summary || {}; const ev = d.events || [];
    const tot = document.getElementById('hist-total');
    if (tot) tot.textContent = s.total ?? 0;
    const byU = (s.by_source && s.by_source.user) || 0;
    const byA = (s.by_source && s.by_source.auto) || 0;
    const su = document.getElementById('hist-user'); if (su) su.textContent = byU;
    const sa = document.getElementById('hist-auto'); if (sa) sa.textContent = byA;
    const body = document.getElementById('hist-body');
    if (!body) return;
    if (!ev.length) { body.innerHTML = '<div class="ai-empty">No learning events yet. They appear here as the model adapts, and stay permanently.</div>'; return; }
    body.innerHTML = ev.map(e => {
      const d2 = new Date((e.ts||0)*1000);
      const when = d2.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      const ic = _HIST_ICON[e.event_type] || '•';
      const src = e.source==='user' ? '<span class="hist-src user">user</span>' : '<span class="hist-src auto">auto</span>';
      return `<div class="hist-row"><span class="hist-when">${when}</span><span class="hist-ic">${ic}</span><span class="hist-detail">${esc(e.detail||e.event_type)}</span>${src}</div>`;
    }).join('');
  }).catch(()=>{});
}

// --- Rules ---
let _rulesCache = [];
let _rulesSort = { key: 'ts', dir: -1 };  // default: newest first

function _fmtRuleDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined,{month:'short',day:'numeric'}) + ' ' +
         d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
}

function _renderRules() {
  const tbody = document.getElementById('rules-tbody'); if (!tbody) return;
  const count = document.getElementById('rules-count');
  if (count) count.textContent = `(${_rulesCache.length})`;
  if (!_rulesCache.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--dim)">No rules yet — add one above or submit a report</td></tr>';
    return;
  }
  const { key, dir } = _rulesSort;
  const sorted = _rulesCache.slice().sort((a, b) => {
    let va = a[key], vb = b[key];
    if (key === 'ts' || key === 'hits') { va = +va || 0; vb = +vb || 0; return (va - vb) * dir; }
    va = (va || '').toString().toLowerCase(); vb = (vb || '').toString().toLowerCase();
    return va < vb ? -dir : va > vb ? dir : 0;
  });
  const srcClr = { AUTO:'var(--yellow)', REPORT:'var(--green)', MANUAL:'var(--cyan)' };
  // Rule-type badge colours, matching the console tag language. Grouped by what
  // the rule DOES: FLAG is amber (noticed only, no change), mutating rules get
  // their own hue, suppression is red.

const RULE_CLR = {
    PRONUNCIATION: { bg:'#1a0a2e', br:'#7c3aed', tx:'#c4b5fd' },  // purple — say differently
    PUNCT:         { bg:'#1a1200', br:'#ca8a04', tx:'#fde68a' },  // gold   — phrasing
    SPLIT:         { bg:'#0a1a2e', br:'#2563eb', tx:'#93c5fd' },  // blue   — chunk shaping
    BOUNDARY:      { bg:'#0a1a2e', br:'#2563eb', tx:'#93c5fd' },  // blue
    BLACKLIST:     { bg:'#1a0a0a', br:'#dc2626', tx:'#fca5a5' },  // red    — never say
    SUPPRESS:      { bg:'#1a0a0a', br:'#dc2626', tx:'#fca5a5' },  // red
    FLAG:          { bg:'#1a1400', br:'#d97706', tx:'#fcd34d' },  // amber  — watch only
    MATH:          { bg:'#082f3a', br:'#06b6d4', tx:'#67e8f9' },  // cyan   — taught equation reading
  };
  function _ruleBadge(t) {
    if (!t) return '';
    const c = RULE_CLR[t] || { bg:'#111', br:'#555', tx:'#aaa' };
    return `<span style="display:inline-block;padding:1px 6px;border-radius:4px;`
         + `font-size:9px;font-weight:600;letter-spacing:0.3px;`
         + `background:${c.bg};border:1px solid ${c.br};color:${c.tx}">${esc(t)}</span>`;
  }
  tbody.innerHTML = sorted.map(r =>
    `<tr>`
    +`<td style="color:var(--dim);white-space:nowrap">${_fmtRuleDate(r.ts)}</td>`
    +`<td>${esc(r.pattern||'')}</td>`
    +`<td>${_ruleBadge(r.rule_type)}</td>`
    +`<td style="color:var(--subtext);max-width:70px;overflow:hidden;text-overflow:ellipsis">${esc(r.value||r.action||'')}</td>`
    +`<td style="color:${srcClr[r.source]||'var(--dim)'}">${r.source||''}</td>`
    +`<td style="color:var(--dim)">${r.hits||0}</td>`
    +`<td><button class="del-rule" data-id="${r.id}">✕</button></td>`
    +`</tr>`
  ).join('');
  // Reflect sort arrow on headers.
  document.querySelectorAll('.rule-sort').forEach(th => {
    const base = th.textContent.replace(/[▾▴]/g,'').trim();
    th.textContent = th.dataset.sort === key ? `${base} ${dir<0?'▾':'▴'}` : base;
  });
}

function refreshRules() {
  api('/report/rules').then(rules => {
    _rulesCache = rules || [];
    _renderRules();
    _renderRuleOrigins();
  }).catch(()=>{
    const tbody=document.getElementById('rules-tbody');
    if(tbody) tbody.innerHTML='<tr><td colspan="7" style="color:var(--red)">Server offline</td></tr>';
  });
}

// Rule origins, grouped. I moved these off the AI tab, which is for Whisper
// insight rather than rule management. Each group explains what that origin
// means, and the
// sections collapse so the rules table stays the focus.
function _renderRuleOrigins() {
  const host = document.getElementById('rules-origins');
  if (!host) return;
  const all  = _rulesCache || [];
  const auto = all.filter(r => r.source === 'AUTO');
  const rep  = all.filter(r => r.source === 'REPORT');
  const man  = all.filter(r => r.source === 'MANUAL');

  const RULE_CLR = {
    PRONUNCIATION:{bg:'#1a0a2e',br:'#7c3aed',tx:'#c4b5fd'},
    PUNCT:{bg:'#1a1200',br:'#ca8a04',tx:'#fde68a'},
    SPLIT:{bg:'#0a1a2e',br:'#2563eb',tx:'#93c5fd'},
    BOUNDARY:{bg:'#0a1a2e',br:'#2563eb',tx:'#93c5fd'},
    BLACKLIST:{bg:'#1a0a0a',br:'#dc2626',tx:'#fca5a5'},
    SUPPRESS:{bg:'#1a0a0a',br:'#dc2626',tx:'#fca5a5'},
    FLAG:{bg:'#1a1400',br:'#d97706',tx:'#fcd34d'},
    MATH:{bg:'#082f3a',br:'#06b6d4',tx:'#67e8f9'},
  };
  const badge = t => {
    const c = RULE_CLR[t] || {bg:'#111',br:'#555',tx:'#aaa'};
    return `<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:9px;`
         + `font-weight:600;background:${c.bg};border:1px solid ${c.br};color:${c.tx};flex-shrink:0">${esc(t||'')}</span>`;
  };
  const rows = list => list.length
    ? list.map(r =>
        `<div style="display:flex;gap:6px;align-items:baseline;padding:3px 0;border-bottom:1px solid var(--border)">`
        + badge(r.rule_type)
        + `<span style="color:var(--text);font-size:10px">${esc(r.pattern||'')}</span>`
        + (r.value ? `<span style="color:var(--subtext);font-size:10px">→ ${esc(r.value)}</span>` : '')
        + (r.hits ? `<span style="color:var(--dim);font-size:9px;margin-left:auto">${r.hits} hit${r.hits===1?'':'s'}</span>` : '')
        + `</div>`).join('')
    : '<div style="color:var(--dim);font-size:10px;padding:4px 0">None yet.</div>';

  let n = 0;
  const section = (title, desc, list, tip) => {
    const id = 'ro-sec-' + (++n);
    return `<div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:8px 10px">
      <div class="ro-toggle" data-target="${id}" title="${esc(tip)}"
           style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none">
        <strong style="color:var(--text);font-size:10px">${title} (${list.length})</strong>
        <span class="ro-arrow" style="color:var(--dim);font-size:9px">▸</span>
      </div>
      <div id="${id}" style="display:none;margin-top:6px">
        <div style="color:var(--dim);font-size:9px;line-height:1.5;margin-bottom:5px">${desc}</div>
        ${rows(list)}
      </div></div>`;
  };

  host.innerHTML =
      section('🤖 Autonomous decisions',
              'Fixes KAM made on its own from listening back — blocking an invented word, spelling out an acronym, steadying a drifting voice.',
              auto, 'Rules the model created itself from Whisper analysis.')
    + section('✅ Your confirmed rules',
              'Changes made because you reported them. These always override the model\'s own decisions.',
              rep, 'Rules created from your reports.')
    + section('🛠 Manual rules',
              'Rules you added by hand in the form above.',
              man, 'Rules you created manually.');

  host.querySelectorAll('.ro-toggle').forEach(t => {
    t.addEventListener('click', () => {
      const body = document.getElementById(t.dataset.target);
      const arrow = t.querySelector('.ro-arrow');
      if (!body) return;
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : 'block';
      if (arrow) arrow.textContent = open ? '▸' : '▾';
    });
  });
}

function deleteRule(id) {
  api(`/report/rules/${id}`,'DELETE').then(()=>refreshRules()).catch(err=>showToast('Delete failed: '+(err&&err.message||'unknown')));
}

function addRule() {
  const type   = document.getElementById('new-rule-type').value;
  const pattern= document.getElementById('new-rule-pattern').value.trim();
  const value  = document.getElementById('new-rule-value').value.trim();
  const action = document.getElementById('new-rule-action').value.trim();
  if (!pattern) { alert('Pattern is required'); return; }
  api('/report/rules','POST',{rule_type:type,pattern,value,action})
    .then(()=>{
      ['new-rule-pattern','new-rule-value','new-rule-action'].forEach(id=>{
        const el=document.getElementById(id); if(el) el.value='';
      });
      refreshRules();
      addLog(`[LEARNER] Manual rule added: ${type} — ${pattern}${value?' → '+value:''}`);
    }).catch(()=>{ addLog('[ERROR] Failed to add rule. Is the server offline?'); });
}

// --- Reports list ---
function refreshReports() {
  // Unique colour per issue type
  const _ISSUE_COLOURS = {
    'PRONUNCIATION': { bg:'#1a0a2e', border:'#7c3aed', text:'#c4b5fd' },  // purple
    'SKIP':          { bg:'#0a1a2e', border:'#2563eb', text:'#93c5fd' },  // blue
    'HALLUCINATION': { bg:'#1a0a0a', border:'#dc2626', text:'#fca5a5' },  // red
    'PROSODY':       { bg:'#0a1a0a', border:'#16a34a', text:'#86efac' },  // green
    'SPEED':         { bg:'#1a1400', border:'#d97706', text:'#fcd34d' },  // amber
    'VOICE':         { bg:'#1a0a1a', border:'#db2777', text:'#f9a8d4' },  // pink
    'OTHER':         { bg:'#111',    border:'#555',    text:'#aaa'    },  // grey
  };
  function _issueStyle(issue) {
    const c = _ISSUE_COLOURS[issue] || _ISSUE_COLOURS['OTHER'];
    return `background:${c.bg};border-left:3px solid ${c.border};color:${c.text}`;
  }
  function _issueBadge(issue) {
    const c = _ISSUE_COLOURS[issue] || _ISSUE_COLOURS['OTHER'];
    return `style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;`
         + `color:${c.text};background:${c.bg};border:1px solid ${c.border};`
         + `border-radius:3px;padding:1px 6px;flex-shrink:0"`;
  }

  api('/report/history?type=reports&limit=20').then(rows=>{
    const el=document.getElementById('reports-list'); if(!el) return;
    if(!rows||!rows.length){
      el.innerHTML='<div style="color:#999;font-size:10px">No reports yet</div>';
      return;
    }
    el.innerHTML=rows.map(r=>{
      const issue = r.issue || 'OTHER';
      return `<div style="margin-bottom:6px;border-radius:4px;padding:7px 10px;${_issueStyle(issue)}">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
          <span ${_issueBadge(issue)}>${esc(issue)}</span>
          ${r.token?`<span style="color:#fff;font-weight:600">${esc(r.token)}</span>`:''}
          ${r.expected?`<span style="color:#999">→</span><span style="color:#4ade80;font-weight:600">${esc(r.expected)}</span>`:''}
          <span style="margin-left:auto;font-size:9px;color:${r.applied?'#4ade80':'#666'}">${r.applied?'✓ applied':'○ pending'}</span>
        </div>
        <div style="font-size:10px;color:#aaa;line-height:1.4">${esc((r.chunk_text||'').substring(0,70))}${(r.chunk_text||'').length>70?'…':''}</div>
      </div>`;
    }).join('');
  }).catch(()=>{
    const el=document.getElementById('reports-list');
    if(el) el.innerHTML='<div style="color:#666;font-size:10px">Could not load reports</div>';
  });
}

// --- Report and chunk selector ---
function refreshChunkSelector() {
  // Chunk selection happens through the popup's ⚑ button, so no picker here
}
function onChunkSelect() {}

// --- Report submit ---
function submitReport() {
  const get = id => {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  };
  const issue = get('r-issue');
  // Idiot-proof: derive the action from the issue so the user only has to say
  // WHAT went wrong, not also pick the fix. An explicit action override is used
  // only if the user changed the action dropdown away from AUTO.
  let action = get('r-action');
  if (!action || action === 'AUTO') {
    action = (ISSUE_ACTION_MAP[issue] && ISSUE_ACTION_MAP[issue].action) || 'FLAG_PATTERN';
  }
  const body = {
    chunk_text: _lastChunkText,
    chunk_id:   _lastChunkId,
    issue,
    token:      get('r-token'),
    expected:   get('r-expected'),
    heard:      get('r-heard'),
    action,
    confidence: get('r-confidence') || 'HIGH',
  };
  // A client-side pre-check, since pronunciation reports need a token before
  // there's any point hitting the server.
  if (issue === 'PRONUNCIATION' && !body.token) {
    showToast('Pronunciation report needs a problem token.');
    return;
  }
  api('/report', 'POST', body)
    .then(d => {
      // Server may have rejected the submission (e.g. no-op or empty essentials).
      if (d && d.ok === false) {
        showToast(d.error || 'Report rejected.');
        return;
      }
      const el = document.getElementById('submit-result');
      if (el) {
        let msg;
        if (d && d.applied) {
          msg = (d.applied_via ? `Applied (${d.applied_via}). ` : 'Applied. ') + (d.result || '');
        } else if (d && d.pending) {
          msg = d.reason || `Logged. Needs more matching reports before it applies (${d.agreement||0} of ${d.needed||2}).`;
        } else {
          msg = (d && d.result) ? d.result : 'Report submitted.';
        }
        el.textContent = msg;
        el.style.display = 'block';
      }
      addLog(`[LEARNER] Report: ${issue}. ${body.token || body.chunk_text.substring(0, 30)}`);
      refreshReports();
      setTimeout(clearReport, 3000);
    })
    .catch(err => {
      // `api()` rejects with the server's validation message on HTTP 400.
      // Show the actual reason instead of swallowing it silently.
      const msg = (err && err.message) ? err.message : 'Submit failed.';
      showToast(msg);
    });
}
function clearReport() {
  _reportLocked = false;  // release the report selection lock
  ['r-token','r-expected','r-heard'].forEach(id=>{const el=document.getElementById(id);if(el)el.value='';});
  const el=document.getElementById('submit-result'); if(el) el.style.display='none';
}

// --- AI Insights ---
// --- AI explanation generator ---
function toggleAiSection(id) {
  const sec  = document.getElementById(id);
  if (!sec) return;
  const body  = sec.querySelector('.ai-section-body');
  const arrow = sec.querySelector('.ai-collapse-arrow');
  if (!body) return;
  const collapsed = body.classList.toggle('collapsed');
  if (arrow) arrow.classList.toggle('open', !collapsed);
}

function _explainChunk(r) {
  const flags  = (r.quality_flags || '').split('|').filter(Boolean);
  const wa     = r.whisper_accuracy;
  const pv     = r.pitch_variance;
  const vc     = r.voice_consistency;
  const q      = r.quality_score;
  const parts  = [];
  const fixes  = [];

  // What went wrong
  if (flags.some(f=>f.startsWith('WHISPER_SKIP'))) {
    const skipped = flags.find(f=>f.startsWith('WHISPER_SKIP')).replace('WHISPER_SKIP:','');
    parts.push(`Whisper detected skipped words: <em>${esc(skipped)}</em>. XTTS failed to vocalise these tokens.`);
    fixes.push(`Auto-corrected: skipped abbreviation(s) added to pronunciation store with letter-by-letter spelling.`);
  }
  if (flags.includes('HALLUCINATION')) {
    parts.push(`Whisper found words in the audio that weren't in the source text — hallucination detected.`);
    fixes.push(`Pattern flagged. Temperature nudged down to improve precision on similar chunks.`);
  }
  if (flags.includes('CUTOFF')) {
    parts.push(`Audio energy dropped sharply at the end — the chunk was cut off before finishing.`);
    fixes.push(`Chunk pattern flagged for split-point adjustment.`);
  }
  if (flags.includes('VOICE_DRIFT')) {
    parts.push(`Voice characteristics deviated from your baseline profile — drift detected.`);
    fixes.push(`Flagged for monitoring. If recurring, adjust temperature slider up slightly.`);
  }
  if (wa != null && wa < 0.75) {
    parts.push(`Whisper word accuracy was ${(wa*100).toFixed(0)}% — significantly below target.`);
    fixes.push(`Temperature auto-tuned down by 0.02 to increase pronunciation precision.`);
  }
  if (pv != null && pv < 5) {
    parts.push(`Pitch variance was ${pv.toFixed(1)} Hz — delivery was flat and robotic.`);
    fixes.push(`Temperature auto-tuned up by 0.02 to add natural expressiveness.`);
  }
  if (vc != null && vc < 0.5) {
    parts.push(`Voice consistency score was ${(vc*100).toFixed(0)}% — voice sounded different from your cloned baseline.`);
  }
  if (!parts.length) {
    if (q != null) parts.push(`Quality score ${(q*100).toFixed(0)}% — below threshold but no specific flags detected.`);
    else parts.push('Chunk flagged but no analysis data available yet.');
  }
  if (!fixes.length) fixes.push('No automatic fix applied — submit a report if you heard an issue.');

  return { what: parts.join(' '), fix: fixes.join(' ') };
}

// ---
// VERDICT SYSTEM
// Each verdict is a one-click action mapped to a concrete server-side change.
// The user sees a confirmation strip showing exactly what KAM did, so the
// feedback loop stays transparent, rather than the thumbs-up just disappearing
// and you hoping it learned something.
//
// verdict.id         goes to the server as the feedback type
// verdict.label      what you read on the button
// verdict.tone       the colour group: good, partial, pronounce, voice or skip
// verdict.emoji      the visual marker
// verdict.applied    the confirmation shown after a click. {token} and
//                    {expected} get filled in from the mini-form when it's there.
// verdict.needsForm  if set, opens a mini-form to capture a word and its
//                    expected spelling before posting the feedback.
// ---



function _renderAiFeed(chunks) {
  const body = document.getElementById('ai-feed-body');
  if (!body) return;
  // Show every chunk, good and bad, newest first so the most recent are at the
  // top and the older ones below. That way you see the latest activity straight
  // away and scroll down for the history.
  const all = (chunks || []).slice().sort((a, b) => {
    const sa = a.seq != null ? a.seq : (a.ts || 0);
    const sb = b.seq != null ? b.seq : (b.ts || 0);
    return sb - sa;   // descending
  });
  const flaggedCount = all.filter(r => r.quality_score != null && r.quality_score < 0.75).length;
  const cntEl = document.getElementById('ai-feed-count');
  if (cntEl) cntEl.textContent = all.length
    ? `· ${all.length} chunks · ${flaggedCount} need attention`
    : '· no chunks yet';

  if (!all.length) {
    body.innerHTML = '<div class="ai-empty">No chunks yet — analysis builds as you use the model.</div>';
    return;
  }

  body.innerHTML = all.map(r => {
    const { what, fix } = _explainChunk(r);
    const q = r.quality_score;
    const qCls = q == null ? 'quality-none'
               : q >= 0.75 ? 'quality-good'
               : q >= 0.5  ? 'quality-mid'
               : 'quality-low';
    const qPct = q != null ? (q*100).toFixed(0) + '%' : 'n/a';
    const qMetricCls = q == null ? '' : q >= 0.75 ? 'good' : q >= 0.5 ? 'warn' : 'bad';
    const waPct = r.whisper_accuracy != null
      ? `<span class="ai-metric ${r.whisper_accuracy<0.75?'bad':r.whisper_accuracy<0.9?'warn':'good'}">Whisper ${(r.whisper_accuracy*100).toFixed(0)}%</span>` : '';
    const pvStr = r.pitch_variance != null
      ? `<span class="ai-metric ${r.pitch_variance<6?'bad':'warn'}">Pitch σ${r.pitch_variance.toFixed(1)}</span>` : '';
    const flags = (r.quality_flags||'').split('|').filter(Boolean).map(f =>
      `<span class="ai-metric warn">${esc(f.replace(/:.*/,''))}</span>`
    ).join('');
    // The chunk number, which is the location indicator.
    const numBadge = r.seq ? `<span class="ai-chunk-num">chunk ${r.seq}/${r.total}</span>` : '';
    const full = r.chunk_text || '';

    return `
      <div class="ai-chunk-analysis ${qCls}" data-chunk-id="${esc(r.chunk_id||'')}" data-full-text="${esc(full)}">
        <div class="ai-chunk-head">${numBadge}</div>
        <div class="ai-text-preview">${esc(full)}</div>
        <div class="ai-metrics">
          <span class="ai-metric ${qMetricCls}">Q: ${qPct}</span>
          ${waPct}${pvStr}${flags}
        </div>
        <div class="ai-explanation">${what}</div>
        <div class="ai-improvement">✦ ${fix}</div>
      </div>`;
  }).join('');
}



function refreshAI(silent) {
  const el=document.getElementById('ai-body');
  if(el && !silent && !el.querySelector('.ai-section')) el.innerHTML='<div class="ai-empty">Loading…</div>';
  // These calls are independent, so one failure doesn't kill the whole tab
  Promise.all([
    api('/report/rules').catch(()=>[]),
    api('/report/chunks?filter=all&limit=200').catch(()=>({chunks:[]})),
    api('/report/stats').catch(()=>({})),
  ]).then(([rules,flaggedData,stats])=>{
    const el=document.getElementById('ai-body'); if(!el) return;
    const autoRules  =(rules||[]).filter(r=>r.source==='AUTO');
    const repRules   =(rules||[]).filter(r=>r.source==='REPORT');
    const manRules   =(rules||[]).filter(r=>r.source==='MANUAL');
    const flagged    =(flaggedData&&flaggedData.chunks)||[];
    const baseline   =stats.baseline||{};
    const c          =stats.chunks||{};

    // Group flags
    const flagCounts={};
    flagged.forEach(ch=>{
      (ch.quality_flags||'').split('|').filter(Boolean).forEach(f=>{
        const key=f.replace(/:.*/,''); flagCounts[key]=(flagCounts[key]||0)+1;
      });
    });

    el.innerHTML=`
      <div class="ai-guide">
        <strong>What the model has learned.</strong> This page shows how KAM's
        voice has changed over time. After each chunk is spoken, a local Whisper
        model listens back and compares it to the text, measuring word accuracy,
        pitch, energy and voice steadiness. The Report page is where you tell KAM
        what went wrong. This page is where you see what it heard and how it is
        tuning itself. Rules KAM has created or you have confirmed now live on
        the <strong>Rules</strong> page.
      </div>


      <div class="ai-section" id="ai-sec-stats">
        <div class="ai-section-header" onclick="toggleAiSection('ai-sec-stats')">
          <span class="ai-section-title">📊 Whisper Analysis</span>
          <span class="ai-collapse-arrow open">▶</span>
        </div>
        <div class="ai-section-body">
          <div class="ai-stat-row"><span>Total chunks processed</span><span>${c.total??'—'}</span></div>
          <div class="ai-stat-row"><span>Analysed by Whisper</span><span>${c.analysed??'—'}</span></div>
          <div class="ai-stat-row"><span>High quality (≥85%)</span><span class="ok">${c.high_quality??'—'}</span></div>
          <div class="ai-stat-row"><span>Low quality (&lt;60%)</span><span class="err">${c.low_quality??'—'}</span></div>
          <div class="ai-stat-row"><span>Flagged total</span><span class="warn">${c.flagged??'—'}</span></div>
          ${baseline.sample_count?`
          <div class="ai-stat-row"><span>Pitch mean</span><span>${baseline.pitch_mean?baseline.pitch_mean.toFixed(1)+' Hz':'—'}</span></div>
          <div class="ai-stat-row"><span>Speaking rate</span><span>${baseline.speaking_rate?baseline.speaking_rate.toFixed(1)+' onsets/s':'—'}</span></div>
          `:'<div class="ai-empty">Baseline builds after ~10 high quality chunks</div>'}
        </div>
      </div>

      <div class="ai-section" id="ai-sec-insights">
        <div class="ai-section-header" onclick="toggleAiSection('ai-sec-insights')">
          <span class="ai-section-title">🔬 Quality Insights</span>
          <span class="ai-collapse-arrow open">▶</span>
        </div>
        <div class="ai-section-body" id="ai-insights-body">
          <div class="ai-empty">Loading…</div>
        </div>
      </div>

    `;
    // --- Quality Insights (fingerprints, maths, splitter, report outcomes) ---
    _renderAiInsights();
    _wireAutotune();
    // Analysis feed is rendered separately into the left panel (#ai-feed-body)
    // so the user can review chunk-level reasoning while still navigating
    // the other right-panel tabs (Reports, Rules, Stats).
    _renderAiFeed(flagged);

    // The AI rule panels are (re)built on each refresh, so re-attach the
    // drag-to-resize handles to the freshly created elements.
    if (typeof window._kamMakeResizable === 'function') {
    }

    // The AI page is observation only, so there are no verdict or report
    // controls on it.
    el.querySelectorAll('.ai-chunk-row').forEach(row=>{
      row.addEventListener('click',()=>{
        _lastChunkText=row.dataset.text; _lastChunkId=row.dataset.id;
        showTab('report');
        const pr=document.getElementById('report-preview');
        if(pr) pr.textContent=_lastChunkText.substring(0,120);
      });
    });
  }).catch((err)=>{
    const el=document.getElementById('ai-body');
    if(el) el.innerHTML=`<div class="ai-empty" style="color:var(--red)">Load error: ${err.message||'unknown'}</div>`;
  });
}

// --- Message from background ---
chrome.runtime.onMessage.addListener((req)=>{
  // Only handle our own dashboard event. Return false for everything else so we
  // never hold open or hijack the response channel of messages meant for the
  // offscreen audio sink (doing so advanced the playback loop early and made the
  // highlight run ahead of the audio).
  if(req && req.action==='chunkReady'){
    if(!_reportLocked){
      _lastChunkId=req.chunkId; _lastChunkText=req.chunkText||'';
      const pr=document.getElementById('report-preview');
      if(pr) pr.textContent=_lastChunkText.substring(0,120);
    }
    pollChunks();
  }
  return false;
});

// --- Boot ---
// --- AI Quality Insights ---
// Fingerprint trends, the maths split, splitter health, report outcomes and the
// voice comparison. This is insight only, there are no actions on it.
function _renderAiInsights() {
  const host = document.getElementById('ai-insights-body');
  if (!host) return;
  const DIMW = {simple:'simple',normal:'typical',dense:'dense',short:'short',
    medium:'medium-length',long:'long',clean:'clean punctuation',
    commas:'comma-heavy',complex:'complex punctuation',plain:'plain wording',
    technical:'technical wording',symbolic:'numbers/symbols'};
  const readable = p => {
    const parts = (p||'').split('|');
    const isMath = parts[0] === 'math';
    if (isMath) parts.shift();
    const body = parts.map(x=>DIMW[x]||x).join(', ');
    return isMath ? `∑ equations · ${body || 'all'}` : body;
  };
  const arrow = t => t==='up' ? '<span style="color:var(--green)">▲ improving</span>'
               : t==='down' ? '<span style="color:var(--red)">▼ falling</span>'
               : '<span style="color:var(--dim)">— steady</span>';
  api('/ai/insights').then(d => {
    let h = '';
    if (d.profiles && d.profiles.length) {
      h += `<div class="ai-ins-title">Quality by sentence fingerprint</div>`
        + d.profiles.map(p =>
          `<div class="ai-stat-row" title="${p.n} observations · overall ${(p.quality*100).toFixed(0)}% · recent ${(p.recent*100).toFixed(0)}%">`
          + `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(readable(p.profile))}</span>`
          + `<span style="flex-shrink:0">${(p.quality*100).toFixed(0)}% ${arrow(p.trend)}</span></div>`).join('');
    }
    if (d.math_split) {
      const m = d.math_split;
      const gap = m.plain_q != null ? Math.round((m.plain_q - m.math_q)*100) : null;
      h += `<div class="ai-ins-title">Equations vs plain text</div>`
        + `<div class="ai-stat-row"><span>∑ maths chunks (${m.math_n})</span><span>${(m.math_q*100).toFixed(0)}%</span></div>`
        + `<div class="ai-stat-row"><span>Plain chunks (${m.plain_n})</span><span>${m.plain_q!=null?(m.plain_q*100).toFixed(0)+'%':'—'}</span></div>`
        + (gap != null ? `<div style="font-size:9px;color:var(--dim);padding:2px 0">${gap>3?`Maths reads ${gap} points below plain text — the expander has room to improve.`:`Maths tracks plain text closely — the expander is holding up.`}</div>` : '');
    }
    if (d.splitter) {
      h += `<div class="ai-ins-title">Splitter health</div>`
        + `<div class="ai-stat-row"><span>Chunks with 2+ sentences (missed boundary)</span>`
        + `<span class="${d.splitter.pct>5?'warn':'ok'}">${d.splitter.pct}%</span></div>`
        + (d.splitter.examples||[]).map(t =>
            `<div style="font-size:9px;color:var(--dim);padding:1px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t)}">· ${esc(t.substring(0,80))}</div>`).join('');
    }
    if (d.report_outcomes && d.report_outcomes.length) {
      h += `<div class="ai-ins-title">Did your reports work?</div>`
        + d.report_outcomes.map(r => {
            if (r.before == null || r.after == null || r.n_after < 3)
              return `<div class="ai-stat-row"><span>${esc(r.issue)} · ${esc(readable(r.profile))}</span><span style="color:var(--dim)">gathering evidence…</span></div>`;
            const d100 = Math.round((r.after - r.before)*100);
            const clr = d100 > 1 ? 'var(--green)' : d100 < -1 ? 'var(--red)' : 'var(--dim)';
            return `<div class="ai-stat-row" title="${r.n_before} obs before, ${r.n_after} after">`
              + `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.issue)} · ${esc(readable(r.profile))}</span>`
              + `<span style="color:${clr};flex-shrink:0">${(r.before*100).toFixed(0)}% → ${(r.after*100).toFixed(0)}%</span></div>`;
          }).join('');
    }
    if (d.perf_overall) {
      h += `<div class="ai-ins-title">Synthesis speed on this machine</div>`
        + `<div class="ai-stat-row" title="Real-time factor: synthesis time ÷ audio length. Under 1.0 means KAM produces speech faster than it plays, so playback never stalls.">`
        + `<span>Overall (${d.perf_overall.n} chunks)</span>`
        + `<span class="${d.perf_overall.rtf < 1 ? 'ok' : 'warn'}">RTF ${d.perf_overall.rtf}${d.perf_overall.rtf < 1 ? ' — faster than playback' : ' — slower than playback'}</span></div>`
        + (d.perf_by_facet||[]).map(p =>
            `<div class="ai-stat-row" title="${p.n} chunks · average ${p.ms} ms each">`
            + `<span>${_FACET_ICON[p.facet]||''} ${esc(p.facet)}</span>`
            + `<span class="${p.rtf < 1 ? 'ok' : 'warn'}">RTF ${p.rtf}</span></div>`).join('');
    }
    if (d.retries && d.retries.retried > 0) {
      h += `<div class="ai-stat-row" title="Chunks that needed a second attempt — a strong instability signal">`
        + `<span>Chunks needing a retry</span>`
        + `<span class="${d.retries.pct > 5 ? 'warn' : 'ok'}">${d.retries.pct}%</span></div>`;
    }
    if (d.voices && d.voices.length >= 2) {
      h += `<div class="ai-ins-title">Voice comparison</div>`
        + d.voices.map(v =>
          `<div class="ai-stat-row"><span>🎤 ${esc(v.voice)} (${v.n} obs)</span><span>${(v.quality*100).toFixed(0)}%</span></div>`).join('');
    }
    host.innerHTML = h || '<div class="ai-empty">Insights build as chunks are analysed — read something first.</div>';
  }).catch(() => { host.innerHTML = '<div class="ai-empty">Server offline.</div>'; });
}

document.addEventListener('DOMContentLoaded',()=>{

  // --- Voice profiles ---
  const vPill = document.getElementById('voice-pill');
  const vMenu = document.getElementById('voice-menu');
  const vStatus = document.getElementById('voice-status');
  function _vSay(msg){ if (vStatus) vStatus.textContent = msg || ''; }
  function _voiceRefresh() {
    api('/voices').then(d => {
      const name = document.getElementById('voice-pill-name');
      if (name) name.textContent = d.active || 'default';
      const list = document.getElementById('voice-list');
      if (!list) return;
      list.innerHTML = (d.voices || []).map(v =>
        `<div style="display:flex;align-items:center;gap:6px;padding:4px 6px;border:1px solid var(--border);border-radius:5px;${v.active?'background:var(--bg3)':''}">
           <span style="flex:1;color:var(--text);font-size:10px">${esc(v.voice_id)}${v.active?' <span style="color:var(--green)">●</span>':''}</span>
           <span style="color:var(--dim);font-size:9px">${v.clips} clip${v.clips===1?'':'s'}</span>
           ${v.active?'':`<button class="voice-use" data-id="${esc(v.voice_id)}" ${v.clips?'':'disabled'} title="${v.clips?'Switch to this voice':'No clips recorded yet'}" style="background:transparent;border:1px solid var(--border);color:var(--subtext);border-radius:4px;padding:2px 7px;cursor:pointer;font-family:var(--font);font-size:9px">Use</button>`}
           <button class="voice-files" data-id="${esc(v.voice_id)}" title="Open this voice's clip folder to add or edit recordings" style="background:transparent;border:1px solid var(--border);color:var(--subtext);border-radius:4px;padding:2px 6px;cursor:pointer;font-family:var(--font);font-size:9px">📁</button>
         </div>`).join('');
    }).catch(()=>_vSay('Server offline — voices unavailable.'));
  }
  if (vPill) vPill.addEventListener('click', () => {
    const open = vMenu.style.display !== 'none';
    vMenu.style.display = open ? 'none' : 'block';
    if (!open) { _voiceRefresh(); _vSay(''); }
  });
  document.addEventListener('click', e => {
    if (vMenu && vMenu.style.display !== 'none'
        && !e.target.closest('#voice-wrap')) vMenu.style.display = 'none';
  });
  if (vMenu) vMenu.addEventListener('click', e => {
    const use = e.target.closest('.voice-use');
    if (use) {
      _vSay('Switching… (instant if cached, a few seconds otherwise)');
      api('/voices/select','POST',{ voice_id: use.dataset.id }).then(r => {
        _vSay(r.applied ? `Switched — ${r.applied}.` : 'Switched.');
        showToast(`🎤 Voice: ${use.dataset.id}`);
        _voiceRefresh();
      }).catch(err => _vSay('Switch failed: ' + (err && err.message || 'server error')));
      return;
    }
    const files = e.target.closest('.voice-files');
    if (files) {
      api('/voices/open','POST',{ voice_id: files.dataset.id })
        .then(r => _vSay('Opened: ' + (r.path || 'folder')))
        .catch(() => _vSay('Could not open the folder.'));
    }
  });
  const vNew = document.getElementById('voice-new');
  if (vNew) vNew.addEventListener('click', () => {
    const name = prompt('Name for the new voice (letters/numbers, e.g. "sarah" or "narrator"):');
    if (!name) return;
    api('/voices/create','POST',{ name }).then(r => {
      if (!r.ok) { _vSay(r.error || 'Could not create voice.'); return; }
      _voiceRefresh();
      _vSay(`Created '${r.voice_id}'. Next: 📜 record the 12 passages as separate WAVs, `
          + `clean them (python clean_voice_clips.py --in <raw> --out "${r.voice_id}"), `
          + `drop them in via 📁, then press Use.`);
      api('/voices/open','POST',{ voice_id: r.voice_id }).catch(()=>{});
    }).catch(() => _vSay('Server offline.'));
  });
  const vPassBtn = document.getElementById('voice-passages-btn');
  if (vPassBtn) vPassBtn.addEventListener('click', () => {
    const box = document.getElementById('voice-passages');
    if (!box) return;
    const open = box.style.display !== 'none';
    box.style.display = open ? 'none' : 'block';
    if (open || box.dataset.loaded) return;
    api('/voices/passages').then(d => {
      box.dataset.loaded = '1';
      box.innerHTML =
        `<div style="font-size:9px;color:var(--dim);margin-bottom:6px">Record each passage as its own clip (8–15s), same mic distance and pace throughout, no processing. One WAV per passage. 12–20 clips gives the encoder the best coverage — all 16 below, plus any repeats of the ones you read most naturally.</div>`
        + (d.passages || []).map(p =>
        `<div style="margin-bottom:7px">
           <div style="color:var(--indigo);font-size:9px;font-weight:600">${p.n}. ${esc(p.title)}</div>
           <div style="color:var(--subtext);font-size:10px;line-height:1.5">${esc(p.text)}</div>
         </div>`).join('');
    }).catch(() => { box.innerHTML = '<div style="color:var(--dim);font-size:10px">Server offline.</div>'; });
  });
  _voiceRefresh();

  // --- Console Legend: colour customisation + hardware info ---
  // Factory colours are the shipped defaults and are never overwritten, so a
  // full reset is always possible (same three-tier model as tuning/fonts).
  const LG_FACTORY = {
    TTS:'#06b6d4', PROSODY:'#a78bfa', SKIP:'#9a5b4c', CODE:'#f59e0b',
    PUNCT:'#a855f7', PRONOUNCE:'#a855f7', LEARNER:'#22c55e', RETRY:'#f59e0b',
    ERROR:'#ef4444', FATAL:'#ef4444', WARN:'#f59e0b', BOOT:'#2563eb',
    STARTUP:'#86efac', STANDBY:'#f59e0b', HOST:'#15803d', MATH:'#06b6d4',
  };
  const LG_CUR = 'kamLegendColours';      // active colours
  const LG_DEF = 'kamLegendColoursDefault'; // user-saved default

  function _lgLoad(key) {
    try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch { return {}; }
  }
  function _lgApply(colours) {
    const root = document.documentElement;
    Object.keys(LG_FACTORY).forEach(tag => {
      const v = colours[tag];
      root.style.setProperty('--lg-' + tag.toLowerCase(), v || LG_FACTORY[tag]);
    });
  }
  function _lgBuildRows() {
    const host = document.getElementById('legend-colour-rows');
    if (!host) return;
    const cur = { ...LG_FACTORY, ..._lgLoad(LG_CUR) };
    host.innerHTML = Object.keys(LG_FACTORY).map(tag =>
      `<div class="lg-colour-row">
         <input type="color" data-tag="${tag}" value="${cur[tag]}">
         <span style="color:var(--subtext)">[${tag}]</span>
       </div>`).join('');
    host.querySelectorAll('input[type=color]').forEach(inp => {
      inp.addEventListener('input', () => {
        const cols = { ...LG_FACTORY, ..._lgLoad(LG_CUR) };
        cols[inp.dataset.tag] = inp.value;
        try { localStorage.setItem(LG_CUR, JSON.stringify(cols)); } catch {}
        _lgApply(cols);
      });
    });
  }
  // Apply saved colours on load.
  _lgApply({ ...LG_FACTORY, ..._lgLoad(LG_CUR) });

  const lgToggle = document.getElementById('legend-colours-toggle');
  const lgPanel  = document.getElementById('legend-colours');
  if (lgToggle && lgPanel) {
    lgToggle.addEventListener('click', () => {
      const open = lgPanel.style.display !== 'none';
      lgPanel.style.display = open ? 'none' : 'block';
      if (!open) _lgBuildRows();
    });
  }
  const lgSave = document.getElementById('legend-save-default');
  if (lgSave) lgSave.addEventListener('click', () => {
    const cur = { ...LG_FACTORY, ..._lgLoad(LG_CUR) };
    try { localStorage.setItem(LG_DEF, JSON.stringify(cur)); } catch {}
    showToast('Legend colours saved as your default');
  });
  const lgResetDef = document.getElementById('legend-reset-default');
  if (lgResetDef) lgResetDef.addEventListener('click', () => {
    const def = _lgLoad(LG_DEF);
    const cols = Object.keys(def).length ? def : { ...LG_FACTORY };
    try { localStorage.setItem(LG_CUR, JSON.stringify(cols)); } catch {}
    _lgApply(cols); _lgBuildRows();
    showToast('Reset to your default colours');
  });
  const lgResetFac = document.getElementById('legend-reset-factory');
  if (lgResetFac) lgResetFac.addEventListener('click', () => {
    if (!confirm('Restore the original factory legend colours? Your saved colour default will be cleared.')) return;
    try { localStorage.removeItem(LG_CUR); localStorage.removeItem(LG_DEF); } catch {}
    _lgApply({ ...LG_FACTORY }); _lgBuildRows();
    showToast('Factory legend colours restored');
  });




  // The hardware section, with live device info plus the stated requirements.
  (function _lgHardware() {
    const el = document.getElementById('legend-hardware');
    if (!el) return;
    api('/autotune/report').then(r => {
      const dev = r && r.device ? r.device : 'unknown';
      const vram = (r && r.vram_total_mb)
        ? `${(r.vram_total_mb/1024).toFixed(1)} GB` : null;
      // Adaptation status comes from /standby (hw_source, hw_rtf).
      api('/standby').then(sb => {
        const src = sb && sb.hw_source;
        const rtf = sb && sb.hw_rtf;
        let adapt;
        if (src === 'profiler' && rtf != null) {
          adapt = `<span style="color:var(--green)">Measured</span> — RTF ${rtf.toFixed(2)}, `
                + `prefetch ${sb.prefetch_target}, analysis ${sb.analysis_enabled ? 'on' : 'off'}.`;
        } else if (src) {
          adapt = `<span style="color:var(--yellow)">Auto-detected</span> — using safe defaults. `
                + `Run <strong style="color:var(--text)">python hardware_profile.py</strong> to measure speed and tune buffering.`;
        } else {
          adapt = 'Not yet determined.';
        }
        el.innerHTML =
          `<div class="lg-def"><strong style="color:var(--text)">This machine:</strong> `
          + `${esc(dev)}${vram ? ' · ' + vram + ' VRAM' : ''}</div>`
          + `<div class="lg-def" style="margin-top:4px"><strong style="color:var(--text)">Adaptation:</strong> ${adapt}</div>`
          + `<div class="lg-def" style="margin-top:4px"><strong style="color:var(--text)">Minimum:</strong> `
          + `4-core CPU, 8 GB RAM. CPU-only works but synthesis is several times slower than playback.</div>`
          + `<div class="lg-def"><strong style="color:var(--text)">Recommended:</strong> `
          + `NVIDIA GPU with 6 GB+ VRAM and CUDA, 16 GB RAM. Gives real-time or faster synthesis.</div>`
          + `<div class="lg-def" style="margin-top:4px">KAM adjusts standby, buffering and quality analysis to suit this machine.</div>`;
      }).catch(() => {});
    }).catch(() => {
      el.innerHTML = `<div class="lg-def">Server offline — start it to see this machine's hardware.</div>`
        + `<div class="lg-def" style="margin-top:4px"><strong style="color:var(--text)">Minimum:</strong> 4-core CPU, 8 GB RAM.</div>`
        + `<div class="lg-def"><strong style="color:var(--text)">Recommended:</strong> NVIDIA GPU, 6 GB+ VRAM, 16 GB RAM.</div>`;
    });
  })();

  // Chunk reading font + size: the selectors set CSS variables on <body> so
  // every surface showing chunk text (live feed, Whisper analysis, Report
  // preview) matches. Both choices persist independently.
  const CHUNK_FONTS = {
    mono:"var(--font)",
    arial:"Arial, sans-serif",
    arialblack:"'Arial Black', Gadget, sans-serif",
    arialnarrow:"'Arial Narrow', Arial, sans-serif",
    avenir:"Avenir, 'Avenir Next', sans-serif",
    avenirnext:"'Avenir Next', Avenir, sans-serif",
    bahnschrift:"Bahnschrift, 'Segoe UI', sans-serif",
    baskerville:"Baskerville, 'Baskerville Old Face', Georgia, serif",
    bigcaslon:"'Big Caslon', 'Book Antiqua', serif",
    bodoni:"'Bodoni MT', Didot, serif",
    bookantiqua:"'Book Antiqua', Palatino, serif",
    bookman:"'Bookman Old Style', serif",
    brushscript:"'Brush Script MT', cursive",
    calibri:"Calibri, 'Segoe UI', sans-serif",
    californian:"'Californian FB', Georgia, serif",
    cambria:"Cambria, Georgia, serif",
    candara:"Candara, 'Segoe UI', sans-serif",
    centaur:"Centaur, 'Times New Roman', serif",
    century:"'Century Gothic', 'Apple Gothic', sans-serif",
    centuryschool:"'Century Schoolbook', Georgia, serif",
    comic:"'Comic Sans MS', 'Comic Sans', cursive",
    consolas:"Consolas, 'Courier New', monospace",
    constantia:"Constantia, Cambria, serif",
    copperplate:"Copperplate, 'Copperplate Gothic Light', serif",
    corbel:"Corbel, 'Segoe UI', sans-serif",
    courier:"'Courier New', Courier, monospace",
    didot:"Didot, 'Bodoni MT', serif",
    ebrima:"Ebrima, 'Segoe UI', sans-serif",
    franklin:"'Franklin Gothic Medium', 'Arial Narrow', sans-serif",
    futura:"Futura, 'Century Gothic', sans-serif",
    gabriola:"Gabriola, Georgia, serif",
    gadugi:"Gadugi, 'Segoe UI', sans-serif",
    garamond:"Garamond, 'Times New Roman', serif",
    geneva:"Geneva, Tahoma, sans-serif",
    georgia:"Georgia, serif",
    gillsans:"'Gill Sans', 'Gill Sans MT', sans-serif",
    goudy:"'Goudy Old Style', Garamond, serif",
    helvetica:"Helvetica, Arial, sans-serif",
    hoefler:"'Hoefler Text', Georgia, serif",
    impact:"Impact, Charcoal, sans-serif",
    inktrap:"'Ink Free', cursive",
    lato:"Lato, 'Segoe UI', sans-serif",
    lucidabright:"'Lucida Bright', Georgia, serif",
    lucidacon:"'Lucida Console', Monaco, monospace",
    lucidasans:"'Lucida Sans Unicode', 'Lucida Grande', sans-serif",
    malgun:"'Malgun Gothic', 'Segoe UI', sans-serif",
    menlo:"Menlo, Monaco, monospace",
    monaco:"Monaco, 'Lucida Console', monospace",
    optima:"Optima, Candara, sans-serif",
    palatino:"'Palatino Linotype', Palatino, serif",
    papyrus:"Papyrus, fantasy",
    perpetua:"Perpetua, Georgia, serif",
    rockwell:"Rockwell, 'Courier New', serif",
    segoe:"'Segoe UI', system-ui, sans-serif",
    segoeprint:"'Segoe Print', cursive",
    segoescript:"'Segoe Script', cursive",
    sitka:"'Sitka Text', Cambria, serif",
    sylfaen:"Sylfaen, 'Times New Roman', serif",
    tahoma:"Tahoma, Geneva, sans-serif",
    times:"'Times New Roman', Times, serif",
    trebuchet:"'Trebuchet MS', sans-serif",
    twcen:"'Tw Cen MT', 'Century Gothic', sans-serif",
    verdana:"Verdana, Geneva, sans-serif",
  };
  const _chunkFontSel = document.getElementById('chunk-font-select');
  const _chunkSizeSel = document.getElementById('chunk-size-select');
  function applyChunkFont(name, sizePx) {
    document.body.style.setProperty('--chunkfont', CHUNK_FONTS[name] || CHUNK_FONTS.mono);
    document.body.style.setProperty('--chunkfont-size', (parseInt(sizePx) || 14) + 'px');
  }
  if (_chunkFontSel && _chunkSizeSel) {
    // Migrate legacy class-based values (sans/serif/wide) to font keys.
    const MIGRATE = { sans: 'segoe', serif: 'georgia', wide: 'verdana' };
    let savedFont = localStorage.getItem('chunkFont') || 'georgia';
    if (MIGRATE[savedFont]) savedFont = MIGRATE[savedFont];
    if (!CHUNK_FONTS[savedFont]) savedFont = 'georgia';
    const savedSize = localStorage.getItem('chunkFontSize') || '14';
    _chunkFontSel.value = savedFont;
    _chunkSizeSel.value = savedSize;
    applyChunkFont(savedFont, savedSize);
    const _onChunkFontChange = () => {
      applyChunkFont(_chunkFontSel.value, _chunkSizeSel.value);
      localStorage.setItem('chunkFont', _chunkFontSel.value);
      localStorage.setItem('chunkFontSize', _chunkSizeSel.value);
    };
    _chunkFontSel.addEventListener('change', _onChunkFontChange);
    _chunkSizeSel.addEventListener('change', _onChunkFontChange);
  }

  // Tabs
  document.querySelectorAll('[data-tab]').forEach(btn=>{
    btn.addEventListener('click',()=>showTab(btn.dataset.tab));
  });

  // Clean-up noisy auto-flags button (inline onclick removed for MV3 CSP).
  const cleanupBtn = document.getElementById('btn-cleanup-flags');
  if (cleanupBtn) {
    cleanupBtn.addEventListener('click', () => {
      cleanupBtn.disabled = true;
      api('/report/rules/cleanup', 'POST')
        .then(res => {
          const n = (res && res.deleted) || 0;
          showToast(`Removed ${n} noisy auto-flag rule(s)`);
          if (typeof refreshRules === 'function') refreshRules();
        })
        .catch(err => showToast('Cleanup failed: ' + ((err && err.message) || 'unknown')))
        .finally(() => { cleanupBtn.disabled = false; });
    });
  }

  // The AI feed refresh button, for pulling fresh data without leaving the tab.
  const aiRefresh = document.getElementById('ai-feed-refresh');
  if (aiRefresh) {
    aiRefresh.addEventListener('click', () => {
      aiRefresh.disabled = true;
      refreshAI();
      setTimeout(() => { aiRefresh.disabled = false; }, 600);
    });
  }

  // Live auto-action recommendation: when the user picks an issue (and leaves
  // Action on Auto), show exactly which fix will be applied and why, in plain
  // language. Updates on either dropdown changing.
  const _rIssue   = document.getElementById('r-issue');
  const _rAction  = document.getElementById('r-action');
  const _rPreview = document.getElementById('r-action-preview');
  function _updateActionPreview() {
    if (!_rPreview) return;
    const issue = _rIssue ? _rIssue.value : 'OTHER';
    const chosen = _rAction ? _rAction.value : 'AUTO';
    const map = ISSUE_ACTION_MAP[issue] || ISSUE_ACTION_MAP.OTHER;
    if (chosen === 'AUTO') {
      _rPreview.innerHTML = `<strong style="color:var(--text)">Recommended fix:</strong> ${esc(map.desc)}`;
    } else {
      _rPreview.innerHTML = `<strong style="color:var(--text)">Manual override.</strong> You chose a specific action instead of the recommended fix.`;
    }
  }
  if (_rIssue)  _rIssue.addEventListener('change', _updateActionPreview);
  if (_rAction) _rAction.addEventListener('change', _updateActionPreview);
  _updateActionPreview();

  // --- Server power button (native messaging) ---
  // The dashboard talks to a small native host (kam_host.py) that launches and
  // stops server.py and streams its stdout here. Requires one-time setup via
  // register_host.py. If the host isn't registered, connectNative fails and we
  // explain how to set it up rather than silently doing nothing.
  const HOST_NAME = 'com.kam.tts';
  let _hostPort = null;
  let _serverRunning = false;
  let _serverReady = false;

  function _setPower(state) {
    const btn = document.getElementById('power-btn');
    if (!btn) return;
    btn.classList.remove('on', 'off', 'loading');
    // Three visual states:
    //   off      red with no animation, meaning the server isn't running
    //   loading  an animated ring while the server boots and the model loads
    //   on       solid green with no animation, so the model is ready and serving
    btn.classList.add(state === 'on' ? 'on' : state === 'loading' ? 'loading' : 'off');
  }

  // Ground-truth sync: the console poll (which actually talks to the server)
  // calls this. If the server is reachable the button goes green, however it was
  // started. I don't override an in-progress 'loading' animation with off,
  // so a deliberate start still shows the ring until it's actually up.
  let _midToggle = false;
  window._markServerOnline = function (online) {
    if (online) {
      _serverRunning = true; _serverReady = true; _midToggle = false;
      _setPower('on');
    } else if (!_midToggle) {
      _serverRunning = false; _serverReady = false;
      _setPower('off');
    }
  };

  // Startup stages in order → fraction complete. Drives the green arc that fills
  // inside the spinning yellow ring, so the user sees boot progress and (later)
  // exactly which stage failed.
  const _STAGE_ORDER = ['process-started','flask-import','torch-tts-imported',
                        'device-probe','model-loading','model-loaded',
                        'benchmarking','adapting'];
  const _STAGE_LABEL = {
    'process-started':'Process started',
    'flask-import':'Loading framework',
    'torch-tts-imported':'Loaded PyTorch + TTS',
    'device-probe':'Selecting compute device',
    'model-loading':'Loading model',
    'model-loaded':'Model loaded',
    // This only happens on the first boot for a given machine, where the server
    // measures synthesis speed so it can size its buffers to the hardware.
    'benchmarking':'Measuring synthesis speed',
    'adapting':'Adapting to this machine',
  };
  function _setProgressArc(frac) {
    const arc = document.getElementById('power-arc');
    if (!arc) return;
    const C = 2 * Math.PI * 9;            // r=9 circle circumference
    arc.style.strokeDasharray = String(C);
    arc.style.strokeDashoffset = String(C * (1 - Math.max(0, Math.min(1, frac))));
  }
  function _setStageProgress(stage) {
    const i = _STAGE_ORDER.indexOf(stage);
    if (i < 0) return;
    const frac = (i + 1) / (_STAGE_ORDER.length + 1);  // headroom for 'ready'
    _setProgressArc(frac);
    const btn = document.getElementById('power-btn');
    if (btn) btn.title = _STAGE_LABEL[stage] || stage;
  }
  function _resetProgress() { _setProgressArc(0); }
  function _completePower() {
    _setProgressArc(1);                    // arc closes into a full circle
    const btn = document.getElementById('power-btn');
    if (btn) {
      btn.classList.add('complete');       // quick completion pulse
      setTimeout(() => { btn.classList.remove('complete'); _setPower('on'); }, 450);
    } else {
      _setPower('on');
    }
  }

  function _connectHost() {
    try {
      _hostPort = chrome.runtime.connectNative(HOST_NAME);
    } catch (e) {
      addLog('[HOST] Native host unavailable — run register_host.py once to enable the power button.');
      return null;
    }
    _hostPort.onMessage.addListener(msg => {
      if (!msg) return;
      if (msg.type === 'log')    addLog(msg.line || '');
      else if (msg.type === 'stage') { _setStageProgress(msg.stage); }
      else if (msg.type === 'status') {
        if (msg.running) {
          if (!_serverReady) _setPower('loading');
        }
        _serverRunning = !!msg.running;
      }
      else if (msg.type === 'ready') {
        addLog('[HOST] ✓ Model Ready');
        _serverReady = true; _serverRunning = true; _midToggle = false;
        _completePower();   // fill to 100% + completion animation → solid green
      }
      else if (msg.type === 'exit') {
        addLog(`[HOST] Server exited (code ${msg.code})`);
        _serverReady = false; _serverRunning = false; _midToggle = false;
        _setPower('off');
      }
      else if (msg.type === 'error') {
        addLog('[HOST] ' + (msg.message || 'error'));
        // A real failure ends the loading state so the ring doesn't spin forever.
        _midToggle = false;
      }
    });
    _hostPort.onDisconnect.addListener(() => {
      const err = chrome.runtime.lastError;
      addLog('[HOST] Disconnected' + (err ? ': ' + err.message : '') +
             '. If the power button does nothing, run register_host.py and restart Chrome.');
      _hostPort = null;
      _serverReady = false; _midToggle = false;
      _setPower('off');
    });
    return _hostPort;
  }

  function _hostSend(cmd) {
    if (!_hostPort && !_connectHost()) return;
    try { _hostPort.postMessage({ cmd }); }
    catch (e) { addLog('[HOST] send failed: ' + e.message); }
  }

  const powerBtn = document.getElementById('power-btn');
  if (powerBtn) {
    _setPower('off');
    powerBtn.addEventListener('click', () => {
      if (_serverRunning) {
        addLog('[HOST] Stopping server…');
        _midToggle = true;
        _setPower('loading');   // animate during shutdown too
        _hostSend('stop');
      } else {
        addLog('[HOST] Starting server…');
        _serverReady = false;
        _midToggle = true;       // hold the ring until the server answers
        _resetProgress();        // empty the green arc; it fills per stage
        _setPower('loading');
        if (!_hostPort) _connectHost();
        _hostSend('start');
      }
    });
    // Ask the host for current status on load (also establishes the port).
    setTimeout(() => _hostSend('status'), 300);
  }

  // --- Settings cog + theme picker ---
  // Cog toggles the dropdown panel. Theme buttons set body[data-theme] and
  // persist the choice in chrome.storage.local under "playerTheme" so the
  // pre-paint bootstrap in player.html applies it on next load.
  const cog   = document.getElementById('settings-cog');
  const panel = document.getElementById('settings-panel');
  const tuneCog   = document.getElementById('tts-tune-cog');
  const tunePanel = document.getElementById('tts-tune-panel');
  const ufCog     = document.getElementById('ui-font-cog');
  const ufPanel   = document.getElementById('ui-font-panel');

  function _closeMenus(except) {
    if (except !== 'themes' && panel) { panel.classList.remove('open'); cog && cog.classList.remove('active'); }
    if (except !== 'tune'   && tunePanel) { tunePanel.classList.remove('open'); tuneCog && tuneCog.classList.remove('active'); }
    if (except !== 'uifont' && ufPanel) { ufPanel.classList.remove('open'); ufCog && ufCog.classList.remove('active'); }
  }

  // Anchor a dropdown directly beneath its trigger button. Right-aligns the
  // panel to the button's right edge, clamped to the viewport so it never
  // overflows off-screen.
  function _positionPanelUnder(btn, panelEl) {
    if (!btn || !panelEl) return;
    const r = btn.getBoundingClientRect();
    const wasHidden = !panelEl.classList.contains('open');
    if (wasHidden) { panelEl.style.visibility = 'hidden'; panelEl.classList.add('open'); }
    const pw = panelEl.offsetWidth || 320;
    if (wasHidden) { panelEl.classList.remove('open'); panelEl.style.visibility = ''; }
    let left = r.right - pw;                       // right-align to the button
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    panelEl.style.left = left + 'px';
    panelEl.style.top  = (r.bottom + 6) + 'px';     // just below the button
  }

  if (cog && panel) {
    cog.addEventListener('click', e => {
      e.stopPropagation();
      _closeMenus('themes');
      const isOpen = !panel.classList.contains('open');
      if (isOpen) _positionPanelUnder(cog, panel);
      panel.classList.toggle('open', isOpen);
      cog.classList.toggle('active', isOpen);
    });
  }
  // --- UI Fonts: main-text font, small-text font, and global interface zoom ---
  if (ufCog && ufPanel) {
    ufCog.addEventListener('click', e => {
      e.stopPropagation();
      _closeMenus('uifont');
      const isOpen = !ufPanel.classList.contains('open');
      if (isOpen) _positionPanelUnder(ufCog, ufPanel);
      ufPanel.classList.toggle('open', isOpen);
      ufCog.classList.toggle('active', isOpen);
    });
    ufPanel.addEventListener('click', e => e.stopPropagation());

    // Verified system fonts (Windows/macOS present) so every option renders.
    const UI_FONTS = {
      default:null,
      arial:"Arial, sans-serif",
      arialblack:"'Arial Black', Gadget, sans-serif",
      arialnarrow:"'Arial Narrow', Arial, sans-serif",
      avenir:"Avenir, 'Avenir Next', sans-serif",
      avenirnext:"'Avenir Next', Avenir, sans-serif",
      bahnschrift:"Bahnschrift, 'Segoe UI', sans-serif",
      baskerville:"Baskerville, 'Baskerville Old Face', Georgia, serif",
      bigcaslon:"'Big Caslon', 'Book Antiqua', serif",
      bodoni:"'Bodoni MT', Didot, serif",
      bookantiqua:"'Book Antiqua', Palatino, serif",
      bookman:"'Bookman Old Style', serif",
      brushscript:"'Brush Script MT', cursive",
      calibri:"Calibri, 'Segoe UI', sans-serif",
      californian:"'Californian FB', Georgia, serif",
      cambria:"Cambria, Georgia, serif",
      candara:"Candara, 'Segoe UI', sans-serif",
      centaur:"Centaur, 'Times New Roman', serif",
      century:"'Century Gothic', 'Apple Gothic', sans-serif",
      centuryschool:"'Century Schoolbook', Georgia, serif",
      comic:"'Comic Sans MS', 'Comic Sans', cursive",
      consolas:"Consolas, 'Courier New', monospace",
      constantia:"Constantia, Cambria, serif",
      copperplate:"Copperplate, 'Copperplate Gothic Light', serif",
      corbel:"Corbel, 'Segoe UI', sans-serif",
      courier:"'Courier New', Courier, monospace",
      didot:"Didot, 'Bodoni MT', serif",
      ebrima:"Ebrima, 'Segoe UI', sans-serif",
      franklin:"'Franklin Gothic Medium', 'Arial Narrow', sans-serif",
      futura:"Futura, 'Century Gothic', sans-serif",
      gabriola:"Gabriola, Georgia, serif",
      gadugi:"Gadugi, 'Segoe UI', sans-serif",
      garamond:"Garamond, 'Times New Roman', serif",
      geneva:"Geneva, Tahoma, sans-serif",
      georgia:"Georgia, serif",
      gillsans:"'Gill Sans', 'Gill Sans MT', sans-serif",
      goudy:"'Goudy Old Style', Garamond, serif",
      helvetica:"Helvetica, Arial, sans-serif",
      hoefler:"'Hoefler Text', Georgia, serif",
      impact:"Impact, Charcoal, sans-serif",
      inktrap:"'Ink Free', cursive",
      lato:"Lato, 'Segoe UI', sans-serif",
      lucidabright:"'Lucida Bright', Georgia, serif",
      lucidacon:"'Lucida Console', Monaco, monospace",
      lucidasans:"'Lucida Sans Unicode', 'Lucida Grande', sans-serif",
      malgun:"'Malgun Gothic', 'Segoe UI', sans-serif",
      menlo:"Menlo, Monaco, monospace",
      monaco:"Monaco, 'Lucida Console', monospace",
      optima:"Optima, Candara, sans-serif",
      palatino:"'Palatino Linotype', Palatino, serif",
      papyrus:"Papyrus, fantasy",
      perpetua:"Perpetua, Georgia, serif",
      rockwell:"Rockwell, 'Courier New', serif",
      segoe:"'Segoe UI', system-ui, sans-serif",
      segoeprint:"'Segoe Print', cursive",
      segoescript:"'Segoe Script', cursive",
      sitka:"'Sitka Text', Cambria, serif",
      sylfaen:"Sylfaen, 'Times New Roman', serif",
      tahoma:"Tahoma, Geneva, sans-serif",
      times:"'Times New Roman', Times, serif",
      trebuchet:"'Trebuchet MS', sans-serif",
      twcen:"'Tw Cen MT', 'Century Gothic', sans-serif",
      verdana:"Verdana, Geneva, sans-serif",
    };

    const ufSel      = document.getElementById('ui-font-select');
    const ufSmallSel = document.getElementById('ui-small-font-select');
    const ufZoom     = document.getElementById('ui-zoom');
    const ufZoomV    = document.getElementById('ui-zoom-v');
    const ufReset    = document.getElementById('uf-reset');

    // Dedicated <style> so overrides beat the [data-theme] cascade with certainty.
    let _ufStyle = document.getElementById('kam-ui-font-style');
    if (!_ufStyle) { _ufStyle = document.createElement('style'); _ufStyle.id = 'kam-ui-font-style'; document.head.appendChild(_ufStyle); }

    function applyUiFont(largeKey, smallKey, zoom) {
      const rules = [];
      if (UI_FONTS[largeKey]) rules.push(`--font: ${UI_FONTS[largeKey]} !important;`);
      if (UI_FONTS[smallKey]) rules.push(`--ui-small-font: ${UI_FONTS[smallKey]} !important;`);
      _ufStyle.textContent = rules.length ? `:root, body, [data-theme] { ${rules.join(' ')} }` : '';
      // Size = global zoom on #main → scales all elements proportionally, no clipping.
      const main = document.getElementById('main');
      if (main) main.style.zoom = (zoom && +zoom !== 100) ? (zoom / 100) : '';
      // The top bar scales at HALF the rate: it must stay readable for anyone
      // who needs magnification, but a full 1:1 header would swallow the window
      // at 250%. e.g. body 200% → header 150%.
      const bar = document.getElementById('topbar');
      if (bar) {
        const z = (zoom && +zoom !== 100) ? (1 + ((zoom / 100) - 1) / 2) : '';
        bar.style.zoom = z;
      }
    }
    function saveUiFont() {
      try {
        localStorage.setItem('uiFont', ufSel.value);
        if (ufSmallSel) localStorage.setItem('uiSmallFont', ufSmallSel.value);
        if (ufZoom) localStorage.setItem('uiZoom', ufZoom.value);
      } catch {}
    }

    if (ufSel) ufSel.addEventListener('change', () => { applyUiFont(ufSel.value, ufSmallSel && ufSmallSel.value, ufZoom && ufZoom.value); saveUiFont(); });
    if (ufSmallSel) ufSmallSel.addEventListener('change', () => { applyUiFont(ufSel.value, ufSmallSel.value, ufZoom && ufZoom.value); saveUiFont(); });
    // The zoom slider lives INSIDE the element it scales, so applying the zoom
    // on every 'input' event re-scales the slider under the cursor mid-drag, so
    // the control shifts away from the pointer and the drag judders. I split the
    // events instead: 'input' updates only the read-out (live feedback, no
    // re-layout), 'change' applies the zoom once the user releases. Clicking
    // the track or using arrow keys also fires 'change', so those still work.
    if (ufZoom) {
      ufZoom.addEventListener('input', () => {
        ufZoomV.textContent = ufZoom.value + '%';
      });
      ufZoom.addEventListener('change', () => {
        ufZoomV.textContent = ufZoom.value + '%';
        applyUiFont(ufSel.value, ufSmallSel && ufSmallSel.value, ufZoom.value);
        saveUiFont();
      });
    }
    if (ufReset) ufReset.addEventListener('click', () => {
      // Factory = Futura at 100% zoom.
      ufSel.value = 'futura'; if (ufSmallSel) ufSmallSel.value = 'futura';
      if (ufZoom) { ufZoom.value = '100'; ufZoomV.textContent = '100%'; }
      applyUiFont('futura', 'futura', '100');
      const main = document.getElementById('main'); if (main) main.style.zoom = '';
      const bar0 = document.getElementById('topbar'); if (bar0) bar0.style.zoom = '';
      try { localStorage.removeItem('uiFont'); localStorage.removeItem('uiSmallFont'); localStorage.removeItem('uiZoom'); } catch {}
    });

    // Restore on load.
    try {
      const lf = localStorage.getItem('uiFont'), sf = localStorage.getItem('uiSmallFont'), z = localStorage.getItem('uiZoom');
      // The factory system font is Futura, used when nothing has been saved.
      if (lf && ufSel) ufSel.value = lf; else if (ufSel && ufSel.querySelector('option[value="futura"]')) ufSel.value = 'futura';
      if (sf && ufSmallSel) ufSmallSel.value = sf; else if (ufSmallSel && ufSmallSel.querySelector('option[value="futura"]')) ufSmallSel.value = 'futura';
      if (z && ufZoom) { ufZoom.value = z; ufZoomV.textContent = z + '%'; }
      applyUiFont(ufSel ? ufSel.value : 'futura', ufSmallSel ? ufSmallSel.value : 'futura', ufZoom ? ufZoom.value : '100');
    } catch {}
  }

  if (tuneCog && tunePanel) {
    tuneCog.addEventListener('click', e => {
      e.stopPropagation();
      _closeMenus('tune');
      const isOpen = !tunePanel.classList.contains('open');
      if (isOpen) { _positionPanelUnder(tuneCog, tunePanel); }
      tunePanel.classList.toggle('open', isOpen);
      tuneCog.classList.toggle('active', isOpen);
      if (isOpen) _loadTuning();   // refresh values each open
    });
    tunePanel.addEventListener('click', e => e.stopPropagation());
  }
  // Click-outside closes whichever menu is open.
  document.addEventListener('click', e => {
    if (panel && !panel.contains(e.target) && e.target !== cog) {
      panel.classList.remove('open'); cog && cog.classList.remove('active');
    }
    if (tunePanel && !tunePanel.contains(e.target) && e.target !== tuneCog) {
      tunePanel.classList.remove('open'); tuneCog && tuneCog.classList.remove('active');
    }
    if (ufPanel && !ufPanel.contains(e.target) && e.target !== ufCog) {
      ufPanel.classList.remove('open'); ufCog && ufCog.classList.remove('active');
    }
  });

  // --- TTS Tuning logic ---
  const TUNE_FIELDS = [
    { key: 'temperature',        sl: 'tp-temp', lab: 'tp-temp-v', dp: 2 },
    { key: 'top_k',              sl: 'tp-topk', lab: 'tp-topk-v', dp: 0 },
    { key: 'top_p',              sl: 'tp-topp', lab: 'tp-topp-v', dp: 2 },
    { key: 'repetition_penalty', sl: 'tp-rep',  lab: 'tp-rep-v',  dp: 1 },
  ];
  const _tuneStatus = (m, bad) => {
    const s = document.getElementById('tp-status');
    if (!s) return;
    s.textContent = m;
    // Offline/failure messages must not render in the success colour.
    s.classList.toggle('warn', !!bad || /offline|failed|could not/i.test(m || ''));
  };
  const _tuneSetLabel = (f, v) => { const el = document.getElementById(f.lab); if (el) el.textContent = (+v).toFixed(f.dp); };

  function _loadTuning() {
    api('/settings').then(s => {
      TUNE_FIELDS.forEach(f => {
        const sl = document.getElementById(f.sl);
        if (sl && s[f.key] != null) { sl.value = s[f.key]; _tuneSetLabel(f, s[f.key]); }
      });
    }).catch(() => _tuneStatus('Server offline. Start it to tune.'));
    refreshLearned();
  }

  // Learned adjustments display: band rate modifiers + per-type temperatures.
  function refreshLearned() {
    const el = document.getElementById('tp-learned');
    if (!el) return;
    api('/learned').then(d => {
      const bands = d.bands || {};
      const types = d.types || {};

      // Turn a machine profile key into readable English.
      //   "prof:sentence|normal|long|clean|plain"
      //     → { type:'sentence', desc:'long, clean punctuation, plain wording' }
      const DIM_WORDS = {
        simple:'simple', normal:'typical', dense:'dense',
        short:'short', medium:'medium-length', long:'long',
        clean:'clean punctuation', commas:'comma-heavy', complex:'complex punctuation',
        plain:'plain wording', technical:'technical wording', symbolic:'numbers/symbols',
      };
      function _readable(key) {
        const raw = key.replace(/^prof:/, '');
        const parts = raw.split('|');
        // Equation chunks carry a leading "math" facet marker.
        let facet = '';
        if (parts[0] === 'math') { facet = 'equations'; parts.shift(); }
        const stype = parts.shift() || 'sentence';
        const desc = parts.map(p => DIM_WORDS[p] || p).join(', ');
        return { stype: facet ? `${stype} · ${facet}` : stype,
                 desc: desc || (facet ? 'all equation chunks' : ''),
                 isProfile: key.startsWith('prof:') };
      }

      // A collapsible section helper, which keeps the panel compact by default.
      let _secId = 0;
      function _section(title, bodyHtml, hint, openByDefault) {
        const id = 'lrn-sec-' + (++_secId);
        return `<div style="border-top:1px solid var(--border);margin-top:6px;padding-top:6px">
          <div class="lrn-toggle" data-target="${id}" title="${esc(hint||'')}"
               style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none">
            <strong style="color:var(--text);font-size:10px">${esc(title)}</strong>
            <span class="lrn-arrow" style="color:var(--dim);font-size:9px">${openByDefault?'▾':'▸'}</span>
          </div>
          <div id="${id}" style="display:${openByDefault?'block':'none'};margin-top:5px">${bodyHtml}</div>
        </div>`;
      }

      // --- Pacing (always visible: only three lines) ---
      const order = ['simple', 'normal', 'dense'];
      let html = order.map(b => {
        const e = bands[b] || {};
        const mod = e.rate_mod != null ? e.rate_mod : 1.0;
        const pct = Math.round((mod - 1) * 100);
        const word = pct === 0 ? 'unchanged' : (pct < 0 ? `${Math.abs(pct)}% slower` : `${pct}% faster`);
        const tip = `Reading pace KAM learned for ${esc(e.label||b)} chunks, from your "too fast"/"too slow" reports.`;
        return `<div title="${tip}"><strong style="color:var(--text)">${esc(e.label || b)}</strong> chunks: ${word}</div>`;
      }).join('');

      // --- Voice steadiness, grouped by sentence type ---
      const tkeys = Object.keys(types);
      if (tkeys.length) {
        const groups = {};          // sentence type → [{desc, temp}]
        const plain  = [];          // legacy non-profile entries
        tkeys.forEach(k => {
          const r = _readable(k);
          const temp = types[k] && types[k].temperature;
          if (temp == null) return;
          if (r.isProfile && r.desc) {
            (groups[r.stype] = groups[r.stype] || []).push({ desc: r.desc, temp });
          } else {
            plain.push({ desc: r.stype, temp });
          }
        });

        let body = '';
        if (plain.length) {
          body += `<div style="margin-bottom:5px">` + plain.map(p =>
            `<div title="Baseline steadiness for all ${esc(p.desc)} chunks."`
            + ` style="display:flex;justify-content:space-between;gap:8px">`
            + `<span style="color:var(--subtext)">${esc(p.desc)}</span>`
            + `<span style="color:var(--indigo)">${p.temp}</span></div>`).join('') + `</div>`;
        }
        Object.keys(groups).sort().forEach(stype => {
          const rows = groups[stype].sort((a,b) => a.desc.localeCompare(b.desc)).map(r =>
            `<div title="Learned voice steadiness for ${esc(stype)} chunks that are ${esc(r.desc)}. Lower = steadier and more literal; higher = more expressive."`
            + ` style="display:flex;justify-content:space-between;gap:8px;padding:1px 0">`
            + `<span style="color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.desc)}</span>`
            + `<span style="color:var(--indigo);flex-shrink:0">${r.temp}</span></div>`).join('');
          body += `<div style="margin-bottom:6px">`
                + `<div style="color:var(--dim);font-size:9px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px">${esc(stype)}</div>`
                + rows + `</div>`;
        });

        html += _section(
          `Voice steadiness · ${tkeys.length} learned profile${tkeys.length===1?'':'s'}`,
          body,
          'Temperature KAM learned per chunk fingerprint. Each line is a distinct sentence shape it has evidence for.',
          false);
      }

      el.innerHTML = html || 'Nothing learned yet.';
      // Wire the collapsible sections.
      el.querySelectorAll('.lrn-toggle').forEach(t => {
        t.addEventListener('click', () => {
          const body = document.getElementById(t.dataset.target);
          const arrow = t.querySelector('.lrn-arrow');
          if (!body) return;
          const open = body.style.display !== 'none';
          body.style.display = open ? 'none' : 'block';
          if (arrow) arrow.textContent = open ? '▸' : '▾';
        });
      });
    }).catch(() => { el.textContent = 'Server offline.'; });
  }

  const resetRatesBtn = document.getElementById('tp-reset-rates');
  if (resetRatesBtn) {
    resetRatesBtn.addEventListener('click', () => {
      resetRatesBtn.disabled = true;
      api('/learned/reset_rates', 'POST')
        .then(() => { showToast('Learned rates reset.'); refreshLearned(); })
        .catch(() => showToast('Reset failed. Is the server offline?'))
        .finally(() => { resetRatesBtn.disabled = false; });
    });
  }

  let _tuneT = null;
  function _pushTuning(key, value) {
    clearTimeout(_tuneT);
    _tuneT = setTimeout(() => {
      api('/settings', 'POST', { [key]: value })
        .then(() => _tuneStatus(`${key} = ${value} · applies to next chunk`))
        .catch(() => _tuneStatus('Could not reach server.'));
    }, 150);
  }

  TUNE_FIELDS.forEach(f => {
    const sl = document.getElementById(f.sl);
    if (!sl) return;
    sl.addEventListener('input', () => {
      _tuneSetLabel(f, sl.value);
      _pushTuning(f.key, f.dp === 0 ? parseInt(sl.value, 10) : parseFloat(sl.value));
    });
  });

  // Volume is a client-side playback control rather than a server TTS param. It
  // goes to the background worker, which relays it to the offscreen audio sink,
  // and it's persisted
  // locally so it survives reloads.
  const _volSlider = document.getElementById('tp-vol');
  const _volLabel  = document.getElementById('tp-vol-v');
  function _applyVolume(pct, persist) {
    const clamped = Math.max(0, Math.min(600, pct));
    if (_volLabel) _volLabel.textContent = clamped + '%';
    // Send as a 0..6 gain factor; the offscreen boost chain handles >1 safely.
    try { chrome.runtime.sendMessage({ action: 'setVolume', volume: clamped / 100 }).catch(() => {}); } catch (e) {}
    if (persist) { try { localStorage.setItem('kamPlaybackVolume', String(clamped)); } catch (e) {} }
  }
  if (_volSlider) {
    let _stored = 100;
    try { const v = localStorage.getItem('kamPlaybackVolume'); if (v != null) _stored = parseInt(v, 10) || 0; } catch (e) {}
    _volSlider.value = _stored;
    _applyVolume(_stored, false);   // sync offscreen on load
    _volSlider.addEventListener('input', () => _applyVolume(parseInt(_volSlider.value, 10), true));
  }

  // Sync every tuning slider from a settings payload returned by the server.
  function _tuneApplySettings(d) {
    const s = (d && d.settings) || {};
    TUNE_FIELDS.forEach(f => {
      const sl = document.getElementById(f.sl);
      if (sl && s[f.key] != null) { sl.value = s[f.key]; _tuneSetLabel(f, s[f.key]); }
    });
  }

  const tpDefault = document.getElementById('tp-default');
  if (tpDefault) tpDefault.addEventListener('click', () => {
    api('/settings', 'POST', { reset: true }).then(d => {
      _tuneApplySettings(d);
      _tuneStatus('Reset to your saved default.');
    }).catch(() => _tuneStatus('Could not reach server.'));
  });

  // Persist the current sliders as the new default (factory stays untouched).
  const tpSaveDef = document.getElementById('tp-save-default');
  if (tpSaveDef) tpSaveDef.addEventListener('click', () => {
    api('/settings', 'POST', { save_default: true }).then(d => {
      _tuneApplySettings(d);
      _tuneStatus('★ Saved — these are now your default settings.');
      showToast('Speech tuning saved as your default');
    }).catch(() => _tuneStatus('Could not reach server.'));
  });

  // Restore the original KAM values and clear the user-saved default.
  const tpFactory = document.getElementById('tp-factory');
  if (tpFactory) tpFactory.addEventListener('click', () => {
    if (!confirm('Restore the original factory speech settings? Your saved default will be cleared.')) return;
    api('/settings', 'POST', { factory_reset: true }).then(d => {
      _tuneApplySettings(d);
      _tuneStatus('⟲ Restored original factory settings.');
      showToast('Factory speech settings restored');
    }).catch(() => _tuneStatus('Could not reach server.'));
  });

  // Quality-analysis sampling, which is the main speed against learning trade
  // that's under the user's control.
  const tpAnalysis = document.getElementById('tp-analysis');
  if (tpAnalysis) {
    api('/standby').then(d => {
      if (d && d.analysis_every != null) tpAnalysis.value = String(d.analysis_every);
    }).catch(()=>{});
    tpAnalysis.addEventListener('change', () => {
      api('/settings', 'POST', { analysis_every: parseInt(tpAnalysis.value, 10) })
        .then(() => {
          const v = parseInt(tpAnalysis.value, 10);
          _tuneStatus(v === 0 ? 'Quality analysis off — fastest, but KAM stops learning.'
                     : v === 1 ? 'Analysing every chunk — best learning.'
                     : `Analysing every ${v}th chunk — faster, learning continues.`);
        })
        .catch(() => _tuneStatus('Could not change analysis setting.', true));
    });
  }

  const tpBench = document.getElementById('tp-benchmark');
  if (tpBench) tpBench.addEventListener('click', () => {
    _tuneStatus('Fetching benchmark…');
    api('/benchmark').then(d => {
      const sentences = (d && d.sentences) || [];
      if (!sentences.length) { _tuneStatus('No benchmark sentences.'); return; }
      // Play through the extension's normal pipeline so current live settings
      // apply. The dashboard isn't a content-script tab, so we ask the popup's
      // background worker to speak via a fresh tab-less session.
      chrome.runtime.sendMessage(
        { action: 'startSpeaking', chunks: sentences, startIndex: 0, speed: 1.0, tabId: null,
          nonce: Date.now() + '-' + Math.random().toString(36).slice(2) },
        () => {}
      );
      _tuneStatus('Speaking benchmark with current settings…');
    }).catch(() => _tuneStatus('Could not reach server.'));
  });

  // Highlight the currently-applied theme on every theme button render.
  function _syncThemeButtons() {
    const cur = document.body.getAttribute('data-theme') || 'arian';
    document.querySelectorAll('.theme-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.theme === cur);
    });
  }
  _syncThemeButtons();
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.theme;
      document.body.setAttribute('data-theme', t);
      try { chrome.storage.local.set({ playerTheme: t }); } catch {}
      _syncThemeButtons();
      applyThemeCustomisations(t);   // re-apply this theme's overrides
      _syncCustPickers(t);
    });
  });

  // --- Per-theme colour customisation ---
  // The user can override any palette variable per theme. Overrides are stored
  // in localStorage keyed by theme, applied as inline custom properties on
  // <body> (which beat the stylesheet's [data-theme] rules), and reset per
  // theme. The set of variables matches the dashboard palette.
  const CUST_VARS = ['--bg','--bg2','--bg3','--border','--text','--subtext','--dim','--indigo'];

  function _custKey(theme) { return 'custColours:' + theme; }

  function _loadCust(theme) {
    try { return JSON.parse(localStorage.getItem(_custKey(theme)) || '{}'); }
    catch { return {}; }
  }
  function _saveCust(theme, obj) {
    try { localStorage.setItem(_custKey(theme), JSON.stringify(obj)); } catch {}
  }

  // Read the theme's DEFAULT value for a var by temporarily clearing any inline
  // override and reading the computed style. Used to seed pickers and to reset.
  function _defaultVarValue(varName) {
    const had = document.body.style.getPropertyValue(varName);
    document.body.style.removeProperty(varName);
    const v = getComputedStyle(document.body).getPropertyValue(varName).trim();
    if (had) document.body.style.setProperty(varName, had);
    return _toHex(v);
  }

  // Normalise rgb()/#abc to #rrggbb so <input type=color> accepts it.
  function _toHex(c) {
    if (!c) return '#000000';
    c = c.trim();
    if (c[0] === '#') {
      if (c.length === 4) return '#' + c[1]+c[1]+c[2]+c[2]+c[3]+c[3];
      return c.slice(0, 7);
    }
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const [r,g,b] = m[1].split(',').map(n => parseInt(n));
      return '#' + [r,g,b].map(x => Math.max(0,Math.min(255,x)).toString(16).padStart(2,'0')).join('');
    }
    return '#000000';
  }

  // Apply a theme's stored overrides as inline custom properties.
  function applyThemeCustomisations(theme) {
    // Clear any existing inline overrides first so switching themes is clean.
    CUST_VARS.forEach(v => document.body.style.removeProperty(v));
    const ov = _loadCust(theme);
    Object.entries(ov).forEach(([k, val]) => {
      if (CUST_VARS.includes(k) && val) document.body.style.setProperty(k, val);
    });
  }

  // Seed the colour pickers: stored override if present, else theme default.
  function _syncCustPickers(theme) {
    const ov = _loadCust(theme);
    document.querySelectorAll('#cust-colours input[data-var]').forEach(inp => {
      const v = inp.dataset.var;
      inp.value = ov[v] || _defaultVarValue(v);
    });
    const lbl = document.getElementById('cust-theme-label');
    if (lbl) lbl.textContent = theme;
  }

  // Wire up the pickers, applying live and persisting against the active theme.
  document.querySelectorAll('#cust-colours input[data-var]').forEach(inp => {
    inp.addEventListener('input', () => {
      const theme = document.body.getAttribute('data-theme') || 'arian';
      const v = inp.dataset.var;
      document.body.style.setProperty(v, inp.value);   // live
      const ov = _loadCust(theme); ov[v] = inp.value; _saveCust(theme, ov);
    });
  });

  // Reset the active theme to its built-in palette.
  const custReset = document.getElementById('cust-reset');
  if (custReset) {
    custReset.addEventListener('click', () => {
      const theme = document.body.getAttribute('data-theme') || 'arian';
      _saveCust(theme, {});
      CUST_VARS.forEach(v => document.body.style.removeProperty(v));
      _syncCustPickers(theme);
      showToast('Reset ' + theme + ' to default colours', 'ok');
    });
  }

  // Apply + seed for the initially-active theme on load.
  (function initCust() {
    const t = document.body.getAttribute('data-theme') || 'arian';
    applyThemeCustomisations(t);
    _syncCustPickers(t);
  })();
  // React to theme changes from popup or other surfaces.
  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === 'local' && changes.playerTheme) {
        document.body.setAttribute('data-theme', changes.playerTheme.newValue || 'arian');
        _syncThemeButtons();
        applyThemeCustomisations(changes.playerTheme.newValue || 'arian');
        _syncCustPickers(changes.playerTheme.newValue || 'arian');
      }
    });
  } catch {}

  // Console clear
  const cb=document.getElementById('clear-btn'); if(cb) cb.addEventListener('click',clearConsole);

  // Report
  const sub=document.getElementById('btn-submit'); if(sub) sub.addEventListener('click',submitReport);

  // Clear the chunk history, which resets the distinct-chunk count and fixes the
  // inflated counts from before content-hash dedup, without touching the rules,
  // history, or the voice baseline.
  const clrChunks = document.getElementById('btn-clear-chunks');
  if (clrChunks) {
    clrChunks.addEventListener('click', () => {
      clrChunks.disabled = true;
      api('/report/stats/reset', 'POST')
        .then(() => {
          _chunkCount = 0; _allChunks = [];
          const ctr = document.getElementById('chunk-counter'); if (ctr) ctr.textContent = '0 chunks';
          const cb = document.getElementById('chunks-body'); if (cb) cb.innerHTML = '';
          const bb = document.getElementById('browser-body'); if (bb) bb.innerHTML = '';
          refreshStats();
          showToast('Chunk history cleared — rules & learning kept', 'ok');
        })
        .catch(() => showToast('Clear failed — server offline?'))
        .finally(() => { clrChunks.disabled = false; });
    });
  }

  // Sortable Rules table headers, which toggle direction when clicked again.
  document.querySelectorAll('.rule-sort').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (_rulesSort.key === key) _rulesSort.dir *= -1;
      else _rulesSort = { key, dir: (key === 'ts' || key === 'hits') ? -1 : 1 };
      _renderRules();
    });
  });
  const clr=document.getElementById('btn-clear-report'); if(clr) clr.addEventListener('click',clearReport);
  const csel=document.getElementById('chunk-selector'); if(csel) csel.addEventListener('change',onChunkSelect);

  // Rules
  const addBtn=document.getElementById('btn-add-rule'); if(addBtn) addBtn.addEventListener('click',addRule);
  const rtbl=document.getElementById('rules-tbody');
  if(rtbl) rtbl.addEventListener('click',e=>{ if(e.target.classList.contains('del-rule')) deleteRule(Number(e.target.dataset.id)); });

  // Filter buttons in chunks tab
  document.querySelectorAll('.filter-btn').forEach(btn=>{
    btn.addEventListener('click',()=>loadBrowser(btn.dataset.filter,0));
  });

  // Load more button
  const lm=document.getElementById('btn-load-more');
  if(lm) lm.addEventListener('click',()=>loadBrowser(_browserFilter,_browserOffset+50));

  // Live chunk feed click router.
  // Two click targets share the same card:
  //   1. .chunk-thumb  → submit positive feedback for this chunk
  //   2. card body     → select chunk and switch to Report tab
  // Order matters: check thumb first and stop propagation if so.
  const cfeed=document.getElementById('chunks-body');
  if(cfeed) cfeed.addEventListener('click',e=>{
    // Select mode: clicking a card toggles selection, bypassing rate/report.
    if (_feedSelectMode) {
      const selCard = e.target.closest('.chunk-card');
      if (selCard && selCard.dataset.id) {
        _markSelected(selCard, !selCard.classList.contains('feed-selected'));
        _updateSelCount();
      }
      return;
    }
    // Thumb buttons on the same spectrum: 👍 for a perfect chunk, 👎 for a
    // broken one.
    const thumb = e.target.closest('.chunk-thumb');
    if (thumb) {
      e.stopPropagation();
      const chunkId = thumb.dataset.chunkId;
      const action  = thumb.dataset.action;
      if (!chunkId) return;
      const card    = thumb.closest('.chunk-card');

      if (action === 'positive') {
        // Toggle: clicking an already-confirmed thumb reverts it.
        if (thumb.classList.contains('confirmed')) {
          thumb.classList.remove('confirmed');
          _setCardRated(card, null);
          api('/chunk/verdict','POST',{ chunk_id: chunkId, verdict: 'revert',
                chunk_text: (card && card.dataset.text) || '' })
            .then(r=>{ showToast('↩ ' + (r && r.applied_text || 'Reverted')); refreshStats(); })
            .catch(()=>{ thumb.classList.add('confirmed'); _setCardRated(card, 'up');
                         showToast('Revert failed — try again'); });
          return;
        }
        thumb.classList.add('confirmed');
        if (card) { const dn = card.querySelector('.chunk-thumb-down'); if (dn) dn.classList.remove('rejected'); }
        _setCardRated(card, 'up');
        api('/chunk/verdict','POST',{ chunk_id: chunkId, verdict: 'sounded_perfect',
              chunk_text: (card && card.dataset.text) || '' })
          .then(r=>{ showToast('✓ ' + (r && r.applied_text || 'Reinforced — KAM trusts similar chunks more')); refreshStats(); })
          .catch(()=>{ thumb.classList.remove('confirmed'); _setCardRated(card, null);
                       showToast('Feedback failed — try again'); });
        return;
      }

      if (action === 'negative') {
        // Toggle: clicking an already-rejected thumb reverts it.
        if (thumb.classList.contains('rejected')) {
          thumb.classList.remove('rejected');
          _setCardRated(card, null);
          api('/chunk/verdict','POST',{ chunk_id: chunkId, verdict: 'revert',
                chunk_text: (card && card.dataset.text) || '' })
            .then(r=>{ showToast('↩ ' + (r && r.applied_text || 'Reverted')); refreshStats(); })
            .catch(()=>{ thumb.classList.add('rejected'); _setCardRated(card, 'down');
                         showToast('Revert failed — try again'); });
          return;
        }
        // A one-click hallucination report, logged directly with no round trip
        // through the Report tab.
        thumb.classList.add('rejected');
        if (card) { const up = card.querySelector('.chunk-thumb:not(.chunk-thumb-down)'); if (up) up.classList.remove('confirmed'); }
        _setCardRated(card, 'down');
        api('/chunk/verdict','POST',{ chunk_id: chunkId, verdict: 'sounded_wrong',
              chunk_text: (card && card.dataset.text) || '' })
          .then(r=>{ showToast('👎 ' + (r && r.applied_text || 'Hallucination logged — voice steadied for similar chunks')); refreshStats(); })
          .catch(()=>{ thumb.classList.remove('rejected'); _setCardRated(card, null);
                       showToast('Feedback failed — try again'); });
        return;
      }
      return;
    }
    // Otherwise: standard card-body click → report selection
    const card=e.target.closest('.chunk-card'); if(!card||!card.dataset.text) return;
    _lastChunkText=card.dataset.text; _lastChunkId=card.dataset.id||null;
    _reportLocked=true;  // pin this chunk; live playback must not overwrite it
    const pr=document.getElementById('report-preview'); if(pr) pr.textContent=_lastChunkText.substring(0,120);
    document.querySelectorAll('.chunk-card.pinned').forEach(c=>c.classList.remove('pinned'));
    card.classList.add('pinned');
    showTab('report');
  });

  // --- Live-feed mass-select: rate many chunks at once (click or click-drag) ---
  // Pause / resume the live feed.
  const _feedPauseBtn = document.getElementById('feed-pause');
  if (_feedPauseBtn) {
    _feedPauseBtn.addEventListener('click', () => _setFeedPaused(!_feedPaused));
  }
  // Scrolling the feed away from the top is a strong hint that you are reading
  // rather than watching, so pause automatically. Returning to the top resumes.
  // The explicit button still wins: it holds the pause even at the top, which
  // is what you want while rating the newest chunk.
  const _feedBody = document.getElementById('chunks-body');
  if (_feedBody) {
    let _autoPaused = false;
    _feedBody.addEventListener('scroll', () => {
      if (_feedBody.scrollTop > 24 && !_feedPaused) {
        _autoPaused = true; _setFeedPaused(true);
      } else if (_feedBody.scrollTop <= 4 && _feedPaused && _autoPaused) {
        _autoPaused = false; _setFeedPaused(false);
      }
    }, { passive: true });
  }

  const _feedSelToggle = document.getElementById('feed-select-toggle');
  const _feedMassBar   = document.getElementById('feed-mass-bar');
  function _selectedCards() {
    return Array.from(document.querySelectorAll('#chunks-body .chunk-card.feed-selected'));
  }
  function _updateSelCount() {
    const c = document.getElementById('feed-sel-count');
    if (c) c.textContent = _selectedCards().length + ' selected';
  }
  function _markSelected(card, on) {
    card.classList.toggle('feed-selected', on);
    card.style.outline = on ? '2px solid var(--indigo)' : '';
  }
  function _clearSelection() {
    _selectedCards().forEach(c => _markSelected(c, false));
    _updateSelCount();
  }
  function _setSelectMode(on) {
    _feedSelectMode = on;
    if (_feedMassBar) _feedMassBar.style.display = on ? 'flex' : 'none';
    if (_feedSelToggle) _feedSelToggle.style.background = on ? 'var(--indigo)' : 'transparent';
    if (cfeed) cfeed.style.userSelect = on ? 'none' : '';
    if (!on) _clearSelection();
  }
  // Paint thumb state so a mass-rated chunk shows green/red immediately and
  // can't be silently re-rated before the next poll refreshes it.
  function _paintCardVerdict(card, verdict) {
    if (!card) return;
    const up = card.querySelector('.chunk-thumb:not(.chunk-thumb-down)');
    const dn = card.querySelector('.chunk-thumb-down');
    if (verdict === 'sounded_perfect') { up && up.classList.add('confirmed'); dn && dn.classList.remove('rejected'); }
    else if (verdict === 'sounded_wrong') { dn && dn.classList.add('rejected'); up && up.classList.remove('confirmed'); }
  }
  function _massVerdict(verdict) {
    const items = _selectedCards().map(c => ({ card: c, id: c.dataset.id })).filter(x => x.id);
    if (!items.length) { showToast('No chunks selected'); return; }
    Promise.all(items.map(({card,id}) =>
      api('/chunk/verdict','POST',{ chunk_id:id, verdict, chunk_text:(card.dataset.text||'') })
        .then(()=>_paintCardVerdict(card, verdict))
        .catch(()=>null)
    )).then(()=>{
      showToast(`${items.length} chunk${items.length===1?'':'s'} marked `
                + (verdict==='sounded_perfect' ? 'perfect' : 'hallucination'));
      refreshStats();
      _clearSelection();   // keep select mode on so the marks stay visible
    });
  }

  // Click-and-drag rubber-band selection over the feed.
  let _dragging=false, _dragStartY=0, _dragAdditive=true;
  if (cfeed) {
    cfeed.addEventListener('mousedown', e => {
      if (!_feedSelectMode) return;
      const card = e.target.closest('.chunk-card');
      if (!card || !card.dataset.id) return;
      _dragging = true;
      _dragStartY = e.clientY;
      // Starting on a selected card drags to DEselect; otherwise it selects.
      _dragAdditive = !card.classList.contains('feed-selected');
      _markSelected(card, _dragAdditive);
      _updateSelCount();
      e.preventDefault();
    });
    cfeed.addEventListener('mousemove', e => {
      if (!_dragging || !_feedSelectMode) return;
      const top = Math.min(_dragStartY, e.clientY), bot = Math.max(_dragStartY, e.clientY);
      document.querySelectorAll('#chunks-body .chunk-card').forEach(card => {
        if (!card.dataset.id) return;
        const r = card.getBoundingClientRect();
        const mid = r.top + r.height / 2;
        if (mid >= top && mid <= bot) _markSelected(card, _dragAdditive);
      });
      _updateSelCount();
    });
    const _endDrag = () => { if (_dragging) { _dragging = false; _updateSelCount(); } };
    cfeed.addEventListener('mouseup', _endDrag);
    cfeed.addEventListener('mouseleave', _endDrag);
  }

  if (_feedSelToggle) _feedSelToggle.addEventListener('click', () => _setSelectMode(!_feedSelectMode));
  const _fsCancel = document.getElementById('feed-sel-cancel');
  if (_fsCancel) _fsCancel.addEventListener('click', () => _setSelectMode(false));
  const _fsAll = document.getElementById('feed-sel-all');
  if (_fsAll) _fsAll.addEventListener('click', () => {
    document.querySelectorAll('#chunks-body .chunk-card').forEach(c => {
      if (c.dataset.id) _markSelected(c, true);
    });
    _updateSelCount();
  });
  const _fsPerfect = document.getElementById('feed-mass-perfect');
  if (_fsPerfect) _fsPerfect.addEventListener('click', () => _massVerdict('sounded_perfect'));
  const _fsHalluc = document.getElementById('feed-mass-halluc');
  if (_fsHalluc) _fsHalluc.addEventListener('click', () => _massVerdict('sounded_wrong'));

  // Browser body click → report
  const bbody=document.getElementById('browser-body');
  if(bbody) bbody.addEventListener('click',e=>{
    const card=e.target.closest('.chunk-card'); if(!card||!card.dataset.text) return;
    _lastChunkText=card.dataset.text; _lastChunkId=card.dataset.id||null;
    _reportLocked=true;  // pin this chunk; live playback must not overwrite it
    const pr=document.getElementById('report-preview'); if(pr) pr.textContent=_lastChunkText.substring(0,120);
    showTab('report');
  });

  // --- Hash routing from popup ⚑ button ---
  function _handleReportHash() {
    const hash = window.location.hash;
    if (!hash.startsWith('#report:')) return false;
    const txt = decodeURIComponent(hash.slice(8));
    // Set this unconditionally so it overrides whatever came before
    _lastChunkText = txt;
    _lastChunkId   = null;  // popup chunks don't have DB IDs yet
    _reportLocked  = true;  // pin; live playback must not overwrite
    const pr = document.getElementById('report-preview');
    if (pr) {
      pr.textContent = txt;
      pr.style.borderLeftColor = 'var(--indigo)';
    }
    // Pre-fill token field with first all-caps word if present
    const capMatch = txt.match(/\b([A-Z][A-Z0-9]{1,})\b/);
    const tokenEl  = document.getElementById('r-token');
    if (tokenEl && capMatch) tokenEl.value = capMatch[1];
    showTab('report');
    // Clear hash without triggering another hashchange
    history.replaceState(null, '', window.location.pathname);
    return true;
  }

  // On load
  if (!_handleReportHash()) showTab('stats');

  // When page is already open and popup fires ⚑ again
  window.addEventListener('hashchange', _handleReportHash);

  // --- Drag-resize left/right divider ---
  (function(){
    const div = document.getElementById('drag-divider');
    const rp  = document.getElementById('right-panel');
    if (!div || !rp) return;
    let drag=false, startX=0, startW=0;
    div.addEventListener('mousedown', e=>{
      drag=true; startX=e.clientX; startW=rp.offsetWidth;
      document.body.style.cursor='col-resize';
      document.body.style.userSelect='none';
    });
    document.addEventListener('mousemove', e=>{
      if (!drag) return;
      const w = Math.max(220, Math.min(600, startW-(e.clientX-startX)));
      rp.style.width = w+'px';
    });
    document.addEventListener('mouseup', ()=>{
      drag=false;
      document.body.style.cursor='';
      document.body.style.userSelect='';
    });
  })();

  // --- Drag-resize chunks panel ---
  // Reusable drag-to-resize: a handle element resizes the pane above it, with
  // the chosen height persisted. Used by the chunk browser, the report guide,
  // and the AI rule panels so every long section can be sized by the user.
  function makeResizable(handleId, bodyId, storageKey, defaultH) {
    const handle = document.getElementById(handleId);
    const pane   = document.getElementById(bodyId);
    if (!handle || !pane) return;
    const savedH = parseInt(localStorage.getItem(storageKey) || String(defaultH));
    pane.style.height = savedH + 'px';
    pane.style.flex = 'none';
    pane.style.overflowY = 'auto';
    if (handle.dataset.bound === '1') return;  // don't double-bind on re-render
    handle.dataset.bound = '1';
    let dragging = false, startY = 0, startH = 0;
    handle.addEventListener('mousedown', e => {
      e.preventDefault();
      dragging = true; startY = e.clientY; startH = pane.offsetHeight;
      document.body.style.cursor = 'row-resize';
      document.body.style.userSelect = 'none';
    });
    document.addEventListener('mousemove', e => {
      if (!dragging) return;
      const delta = e.clientY - startY;
      const newH = Math.max(80, Math.min(1400, startH + delta));
      pane.style.height = newH + 'px';
    });
    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      localStorage.setItem(storageKey, pane.offsetHeight);
    });
  }
  window._kamMakeResizable = makeResizable;  // so refreshAI can re-attach

  makeResizable('chunk-drag-handle', 'browser-body', 'browserPaneH', 200);
  makeResizable('report-guide-handle', 'report-guide-body', 'reportGuideH', 220);
  makeResizable('report-recent-handle', 'report-recent-body', 'reportRecentH', 200);
  makeResizable('stats-guide-handle', 'stats-guide-body', 'statsGuideH', 240);
  makeResizable('hist-handle', 'hist-body', 'histPaneH', 200);

  // Keep SW alive + poll
  setInterval(()=>chrome.runtime.sendMessage({action:'keepAlive'}).catch(()=>{}),25000);
  function doPoll(){
    pollConsole();
    pollChunks();
    // Live-refresh the AI analysis feed while its tab is open, so new chunks
    // appear without clicking the refresh button (matches the live feed). Only
    // when the chunk count actually changed, and silently (no loading flash).
    const aiPanel = document.getElementById('panel-ai');
    if (aiPanel && aiPanel.classList.contains('active')) {
      // Refresh when the chunk count changes (new chunk) OR every few polls,
      // because Whisper analysis lands ASYNCHRONOUSLY after the count already
      // ticked, so the quality scores and flags appear without a manual refresh.
      _aiPollTick = (_aiPollTick || 0) + 1;
      if (_chunkCount !== _aiLastCount || _aiPollTick % 2 === 0) {
        _aiLastCount = _chunkCount;
        refreshAI(true);
      }
    }
  }
  doPoll();
  setInterval(doPoll,3000);
  addLog('[LEARNER] Dashboard connected — polling every 3s');
});
