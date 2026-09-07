/* The Scan panel: pair a phone, take the photos it sends, read the text off them.

   The listener the phone reaches is opened by the server only while a session is
   open here, on this machine's wifi address and nowhere else, and closing the
   panel takes it down again. Everything after the photo arrives happens in this
   page: straightening, cleaning and recognition all run locally, so a scanned
   page never leaves the machine any more than a typed one does. */
'use strict';

(() => {
  const $ = id => document.getElementById(id);
  let poll = null;      // status poller while a session is open
  let seen = 0;         // pages already read, so each is only recognised once
  let busy = false;     // one page at a time; the recogniser is not reentrant
  let token = null;     // server API token, for the binary page fetch

  function say(msg) { const el = $('scan-status'); if (el) el.textContent = msg || ''; }

  // The page images come back as bytes, not JSON, so they cannot go through the
  // worker's dashboardFetch proxy. This page is the extension origin, which CORS
  // already allows; it just needs the token, the same way the recorder does.
  async function apiToken() {
    if (token) return token;
    const r = await fetch(SERVER + '/token');
    token = (await r.json()).token;
    return token;
  }

  async function fetchPage(i) {
    const t = await apiToken();
    const r = await fetch(`${SERVER}/scan/page/${i}`, { headers: { 'X-KAM-Token': t } });
    if (!r.ok) throw new Error('page ' + i + ' is no longer there');
    return await r.blob();
  }

  function showPairing(st) {
    $('scan-pair').style.display = 'block';
    $('scan-start').style.display = 'none';
    $('scan-stop').style.display = 'block';
    const qr = $('scan-qr');
    // Rebuilt only when the URL changes, since QRCode appends a fresh canvas
    // every time and would otherwise stack them up on every poll.
    if (qr.dataset.url !== st.url) {
      qr.innerHTML = '';
      qr.dataset.url = st.url;
      try {
        new QRCode(qr, { text: st.url, width: 132, height: 132,
                         correctLevel: QRCode.CorrectLevel.M });
      } catch (e) {
        qr.textContent = 'QR unavailable';
      }
    }
    $('scan-url').textContent = st.url.replace(/\?k=.*$/, '');
    const mins = Math.round((st.seconds_left || 0) / 60);
    $('scan-expiry').textContent = `Code ${st.code} · closes in ${mins} min if unused`;
  }

  function showClosed() {
    $('scan-pair').style.display = 'none';
    $('scan-start').style.display = 'block';
    $('scan-stop').style.display = 'none';
    const qr = $('scan-qr'); if (qr) { qr.innerHTML = ''; delete qr.dataset.url; }
  }

  function appendText(text) {
    const box = $('scan-text');
    box.style.display = 'block';
    $('scan-actions').style.display = 'flex';
    box.value = (box.value ? box.value.replace(/\s*$/, '') + '\n\n' : '') + text;
    box.scrollTop = box.scrollHeight;
  }

  async function readNewPages(total) {
    if (busy) return;
    busy = true;
    try {
      while (seen < total) {
        const i = seen;
        say(`Fetching page ${i + 1}…`);
        let blob;
        try { blob = await fetchPage(i); }
        catch (e) { say(e.message); seen++; continue; }

        const cols = parseInt($('scan-cols').value, 10) || 1;
        const { text } = await KamOcr.readImage(blob, cols, say);
        const cleaned = KamScanText.clean(text);
        if (cleaned.text) {
          appendText(cleaned.text);
          const v = cleaned.versesRemoved
            ? `, ${cleaned.versesRemoved} verse number${cleaned.versesRemoved > 1 ? 's' : ''} taken out`
            : '';
          say(`Read page ${i + 1}${v}.`);
          showToast(`📷 Page ${i + 1} read`);
        } else {
          say(`Nothing readable on page ${i + 1}. Try more light, or a squarer shot.`);
        }
        seen++;
        // The photo has given up its text, which was the point of it, so the
        // server drops it rather than keeping a picture of what I was reading.
        api(`/scan/page/${i}/done`, 'POST', {}).catch(() => {});
      }
    } catch (e) {
      say('Could not read that page: ' + (e && e.message || e));
    } finally {
      busy = false;
    }
  }

  async function refresh() {
    let st;
    try { st = await api('/scan/status'); }
    catch (e) { say('Server offline.'); return; }
    if (!st || !st.open) {
      showClosed();
      if (st && st.expired) say('The session timed out and the phone link is closed.');
      stopPolling();
      return;
    }
    showPairing(st);
    if (st.pages > seen) readNewPages(st.pages);
    else if (!busy && !seen) say('Waiting for the first photo…');
  }

  function startPolling() {
    stopPolling();
    poll = setInterval(refresh, 2000);
  }
  function stopPolling() { if (poll) { clearInterval(poll); poll = null; } }

  // --- Wiring ---------------------------------------------------------------

  const pill = $('scan-pill');
  if (pill) pill.addEventListener('click', () => {
    const p = $('scan-panel');
    const open = p.style.display !== 'none';
    p.style.display = open ? 'none' : 'block';
    if (!open) refresh();
  });

  document.addEventListener('click', e => {
    const p = $('scan-panel');
    if (p && p.style.display !== 'none' && !e.target.closest('#scan-wrap')) {
      p.style.display = 'none';
    }
  });

  const start = $('scan-start');
  if (start) start.addEventListener('click', async () => {
    say('Opening a session…');
    try {
      const r = await api('/scan/open', 'POST', {});
      if (!r.ok) { say(r.error || 'Could not open a session.'); return; }
      seen = 0;
      showPairing(Object.assign({ seconds_left: r.expires_in }, r));
      say('Scan the code with your phone.');
      startPolling();
    } catch (e) { say('Server offline.'); }
  });

  const stop = $('scan-stop');
  if (stop) stop.addEventListener('click', async () => {
    stopPolling();
    try { await api('/scan/close', 'POST', {}); } catch (e) { /* already gone */ }
    // The recogniser holds a worker and a lot of memory, and the dashboard stays
    // open for hours, so it goes when the session does.
    KamOcr.release().catch(() => {});
    showClosed();
    seen = 0;
    say('Session closed. The phone link is down.');
  });

  const send = $('scan-send');
  if (send) send.addEventListener('click', () => {
    const text = ($('scan-text').value || '').trim();
    if (!text) { say('Nothing to send yet.'); return; }
    // Handed to the popup rather than spoken from here, since the point is that
    // it lands in Custom Text where it can be edited and played like anything
    // else typed in.
    chrome.storage.local.set({ kamScanText: text }, () => {
      say('Sent. Open the KAM TTS popup and press Custom Text.');
      showToast('📷 Text sent to Custom Text');
    });
  });

  const clear = $('scan-clear');
  if (clear) clear.addEventListener('click', () => {
    $('scan-text').value = '';
    $('scan-text').style.display = 'none';
    $('scan-actions').style.display = 'none';
    say('');
  });
})();
