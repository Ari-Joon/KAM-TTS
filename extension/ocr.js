/* Reading the text off a photographed page.

   Tesseract runs here, in the dashboard, as WebAssembly. Nothing is uploaded and
   no OCR service is called: the recogniser and its English data sit in lib/ocr/
   and the whole thing works with the network unplugged, which is the point of
   the project.

   It runs on the computer rather than the phone on purpose. The phone stays a
   camera, so it downloads none of the nine megabytes of recogniser, and the text
   lands where it is going to be read from anyway.

   The columns are declared by the caller rather than detected. Tesseract's own
   layout analysis is good until it is wrong, and when it is wrong on two narrow
   columns it interleaves them line by line and produces something that reads
   perfectly smoothly and means nothing. A silent failure that sounds right is
   the worst kind here, so each column is cropped and read separately, in order. */
'use strict';

const KamOcr = (() => {
  const BASE = 'lib/ocr/';
  let worker = null, loading = null;

  function loadScript(src) {
    return new Promise((res, rej) => {
      if (document.querySelector(`script[src="${src}"]`)) return res();
      const s = document.createElement('script');
      s.src = src;
      s.onload = () => res();
      s.onerror = () => rej(new Error('could not load ' + src));
      document.head.appendChild(s);
    });
  }

  async function ready(onStatus) {
    if (worker) return worker;
    if (loading) return loading;
    loading = (async () => {
      onStatus && onStatus('Loading the text recogniser…');
      await loadScript(BASE + 'tesseract.min.js');
      onStatus && onStatus('Starting the text recogniser…');
      worker = await Tesseract.createWorker('eng', 1, {
        workerPath: BASE + 'worker.min.js',
        corePath:   BASE + 'tesseract-core-simd.wasm.js',
        langPath:   BASE,
        gzip: false,             // the .traineddata here is plain, not gzipped
        workerBlobURL: false,    // an extension page cannot run a blob: worker
        logger: m => {
          if (!onStatus || !m) return;
          if (m.status === 'recognizing text') {
            onStatus(`Reading the page… ${Math.round((m.progress || 0) * 100)}%`);
          } else if (m.status) {
            onStatus(m.status.charAt(0).toUpperCase() + m.status.slice(1) + '…');
          }
        },
      });
      return worker;
    })();
    try { return await loading; }
    catch (e) { loading = null; worker = null; throw e; }
  }

  /* Straighten and clean one photo before the recogniser sees it.

     A photo of a page is never square to the camera, so the corners are found
     and the page is warped flat. When the corners cannot be found the whole
     frame is used instead, which is right rather than a fallback: it means the
     photo was already a flat crop. Greyscale, not black and white, since
     Tesseract does its own thresholding and does it better than a hard cut does
     on a page lit unevenly. */
  async function prepare(blob) {
    const raw = await KamScan.loadToCanvas(blob, 2400);
    const corners = KamScan.detectCorners(raw) || KamScan.fullCorners(raw);
    const flat = KamScan.warp(raw, corners, 2400);
    return KamScan.enhance(flat, 'gray');
  }

  function crop(canvas, x0, x1) {
    if (x0 === 0 && x1 === canvas.width) return canvas;
    const c = document.createElement('canvas');
    c.width = Math.max(1, x1 - x0);
    c.height = canvas.height;
    c.getContext('2d').drawImage(canvas, x0, 0, c.width, c.height, 0, 0, c.width, c.height);
    return c;
  }

  /* Read one photo. Returns { text, columns } with the columns joined in order. */
  async function readImage(blob, columns, onStatus) {
    const w = await ready(onStatus);
    onStatus && onStatus('Straightening the page…');
    const page = await prepare(blob);
    const slices = KamScanText.columnSlices(page.width, columns || 1);
    const parts = [];
    for (let i = 0; i < slices.length; i++) {
      if (slices.length > 1) {
        onStatus && onStatus(`Reading column ${i + 1} of ${slices.length}…`);
      }
      const [x0, x1] = slices[i];
      const { data } = await w.recognize(crop(page, x0, x1));
      parts.push((data && data.text ? data.text : '').trim());
    }
    return { text: parts.filter(Boolean).join('\n'), columns: slices.length };
  }

  /* Let the recogniser go once a batch is done. It holds a worker and a few tens
     of megabytes, and the dashboard stays open for hours. */
  async function release() {
    const w = worker;
    worker = null; loading = null;
    if (w) { try { await w.terminate(); } catch (e) { /* already gone */ } }
  }

  return { ready, readImage, prepare, release };
})();
