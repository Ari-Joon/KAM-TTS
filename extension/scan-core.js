/* KAM PDFs scanner core: document edge detection, perspective correction, clean-up.
   Pure image maths on canvases. Shared by the desktop app and the phone page (scan.html). */
'use strict';
const KamScan = (() => {

  /* ---------- loading ---------- */
  async function loadToCanvas(file, maxDim = 1800) {
    let bmp;
    try { bmp = await createImageBitmap(file, { imageOrientation: 'from-image' }); }
    catch (e) {
      bmp = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = URL.createObjectURL(file); });
    }
    const w = bmp.width || bmp.naturalWidth, h = bmp.height || bmp.naturalHeight;
    const sc = Math.min(1, maxDim / Math.max(w, h));
    const c = document.createElement('canvas'); c.width = Math.max(1, Math.round(w * sc)); c.height = Math.max(1, Math.round(h * sc));
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    if (bmp.close) bmp.close();
    return c;
  }

  /* ---------- helpers ---------- */
  function grayOf(data, n) { const g = new Float32Array(n); for (let i = 0, j = 0; i < n; i++, j += 4) g[i] = data[j] * 0.299 + data[j + 1] * 0.587 + data[j + 2] * 0.114; return g; }
  // Summed-area table (Uint32 is enough: 255 * 16M pixels < 2^32)
  function integral(src, W, H) {
    const I = new Uint32Array((W + 1) * (H + 1)), S = W + 1;
    for (let y = 1; y <= H; y++) { let row = 0; for (let x = 1; x <= W; x++) { row += src[(y - 1) * W + (x - 1)]; I[y * S + x] = I[(y - 1) * S + x] + row; } }
    return I;
  }
  function boxMean(I, W, H, x, y, r) {
    const S = W + 1, x0 = Math.max(0, x - r), y0 = Math.max(0, y - r), x1 = Math.min(W, x + r + 1), y1 = Math.min(H, y + r + 1);
    return (I[y1 * S + x1] - I[y0 * S + x1] - I[y1 * S + x0] + I[y0 * S + x0]) / ((x1 - x0) * (y1 - y0));
  }
  function otsu(g, n) {
    const hist = new Uint32Array(256); for (let i = 0; i < n; i++) hist[Math.max(0, Math.min(255, g[i] | 0))]++;
    let sum = 0; for (let t = 0; t < 256; t++) sum += t * hist[t];
    let sumB = 0, wB = 0, best = 0, thr = 128;
    for (let t = 0; t < 256; t++) {
      wB += hist[t]; if (!wB) continue; const wF = n - wB; if (!wF) break;
      sumB += t * hist[t]; const mB = sumB / wB, mF = (sum - sumB) / wF, v = wB * wF * (mB - mF) * (mB - mF);
      if (v > best) { best = v; thr = t; }
    }
    return thr;
  }

  /* ---------- document detection ----------
     Paper is normally the brightest large region in a photo. Threshold (Otsu), keep the
     largest bright blob, and take its four extreme points as the corners.
     Returns [[x,y] TL, TR, BR, BL] in source pixels, or null if nothing convincing was found. */
  function detectCorners(canvas) {
    const W = 320, sc = W / canvas.width, H = Math.max(2, Math.round(canvas.height * sc));
    const c = document.createElement('canvas'); c.width = W; c.height = H;
    const ctx = c.getContext('2d'); ctx.drawImage(canvas, 0, 0, W, H);
    const n = W * H, g = grayOf(ctx.getImageData(0, 0, W, H).data, n);
    // light blur
    const bl = new Uint8Array(n), I = integral(Uint8Array.from(g, v => v | 0), W, H);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) bl[y * W + x] = boxMean(I, W, H, x, y, 2);
    const thr = otsu(bl, n);
    const mask = new Uint8Array(n); for (let i = 0; i < n; i++) mask[i] = bl[i] > thr ? 1 : 0;
    // largest connected bright component (iterative flood fill)
    const label = new Int32Array(n).fill(-1); let bestArea = 0, bestId = -1, id = 0;
    const stack = new Int32Array(n);
    for (let s = 0; s < n; s++) {
      if (!mask[s] || label[s] >= 0) continue;
      let sp = 0, area = 0; stack[sp++] = s; label[s] = id;
      while (sp) {
        const p = stack[--sp]; area++;
        const x = p % W, y = (p - x) / W;
        if (x > 0 && mask[p - 1] && label[p - 1] < 0) { label[p - 1] = id; stack[sp++] = p - 1; }
        if (x < W - 1 && mask[p + 1] && label[p + 1] < 0) { label[p + 1] = id; stack[sp++] = p + 1; }
        if (y > 0 && mask[p - W] && label[p - W] < 0) { label[p - W] = id; stack[sp++] = p - W; }
        if (y < H - 1 && mask[p + W] && label[p + W] < 0) { label[p + W] = id; stack[sp++] = p + W; }
      }
      if (area > bestArea) { bestArea = area; bestId = id; }
      id++;
    }
    if (bestId < 0 || bestArea < n * 0.12) return null;
    let tl = null, tr = null, br = null, bl2 = null, mTL = Infinity, mTR = -Infinity, mBR = -Infinity, mBL = Infinity;
    for (let p = 0; p < n; p++) {
      if (label[p] !== bestId) continue;
      const x = p % W, y = (p - x) / W, s = x + y, d = x - y;
      if (s < mTL) { mTL = s; tl = [x, y]; }
      if (s > mBR) { mBR = s; br = [x, y]; }
      if (d > mTR) { mTR = d; tr = [x, y]; }
      if (d < mBL) { mBL = d; bl2 = [x, y]; }
    }
    const quad = [tl, tr, br, bl2];
    // sanity: the quad should cover most of the blob and not be a sliver
    const qa = Math.abs(polyArea(quad));
    if (qa < bestArea * 0.6 || qa > bestArea * 1.6) return null;
    const minSide = Math.min(dist(tl, tr), dist(tr, br), dist(br, bl2), dist(bl2, tl));
    if (minSide < Math.min(W, H) * 0.15) return null;
    return quad.map(([x, y]) => [x / sc, y / sc]);
  }
  function polyArea(p) { let a = 0; for (let i = 0; i < p.length; i++) { const [x1, y1] = p[i], [x2, y2] = p[(i + 1) % p.length]; a += x1 * y2 - x2 * y1; } return a / 2; }
  function dist(a, b) { return Math.hypot(a[0] - b[0], a[1] - b[1]); }
  function fullCorners(canvas) { return [[0, 0], [canvas.width, 0], [canvas.width, canvas.height], [0, canvas.height]]; }

  /* ---------- perspective correction ---------- */
  // Homography mapping dst points -> src points (8 unknowns, h33 = 1), solved by Gaussian elimination.
  function homography(dst, src) {
    const A = [], b = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = dst[i], [u, v] = src[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    }
    const n = 8;
    for (let c = 0; c < n; c++) {
      let piv = c; for (let r = c + 1; r < n; r++) if (Math.abs(A[r][c]) > Math.abs(A[piv][c])) piv = r;
      [A[c], A[piv]] = [A[piv], A[c]]; [b[c], b[piv]] = [b[piv], b[c]];
      const d = A[c][c] || 1e-12;
      for (let r = 0; r < n; r++) { if (r === c) continue; const f = A[r][c] / d; if (!f) continue; for (let k = c; k < n; k++) A[r][k] -= f * A[c][k]; b[r] -= f * b[c]; }
    }
    const h = b.map((v, i) => v / (A[i][i] || 1e-12)); h.push(1);
    return h;
  }
  function warp(canvas, corners, maxDim = 2200) {
    const [tl, tr, br, bl] = corners;
    let outW = Math.round(Math.max(dist(tl, tr), dist(bl, br))), outH = Math.round(Math.max(dist(tl, bl), dist(tr, br)));
    const sc = Math.min(1, maxDim / Math.max(outW, outH)); outW = Math.max(2, Math.round(outW * sc)); outH = Math.max(2, Math.round(outH * sc));
    const h = homography([[0, 0], [outW, 0], [outW, outH], [0, outH]], corners);
    const sctx = canvas.getContext('2d'), sw = canvas.width, sh = canvas.height;
    const sd = sctx.getImageData(0, 0, sw, sh).data;
    const out = document.createElement('canvas'); out.width = outW; out.height = outH;
    const octx = out.getContext('2d'), oimg = octx.createImageData(outW, outH), od = oimg.data;
    for (let y = 0; y < outH; y++) {
      for (let x = 0; x < outW; x++) {
        const den = h[6] * x + h[7] * y + h[8];
        const sx = (h[0] * x + h[1] * y + h[2]) / den, sy = (h[3] * x + h[4] * y + h[5]) / den;
        const x0 = Math.floor(sx), y0 = Math.floor(sy), fx = sx - x0, fy = sy - y0;
        const o = (y * outW + x) * 4;
        if (x0 < 0 || y0 < 0 || x0 >= sw - 1 || y0 >= sh - 1) { od[o] = od[o + 1] = od[o + 2] = 255; od[o + 3] = 255; continue; }
        const i00 = (y0 * sw + x0) * 4, i10 = i00 + 4, i01 = i00 + sw * 4, i11 = i01 + 4;
        const w00 = (1 - fx) * (1 - fy), w10 = fx * (1 - fy), w01 = (1 - fx) * fy, w11 = fx * fy;
        od[o] = sd[i00] * w00 + sd[i10] * w10 + sd[i01] * w01 + sd[i11] * w11;
        od[o + 1] = sd[i00 + 1] * w00 + sd[i10 + 1] * w10 + sd[i01 + 1] * w01 + sd[i11 + 1] * w11;
        od[o + 2] = sd[i00 + 2] * w00 + sd[i10 + 2] * w10 + sd[i01 + 2] * w01 + sd[i11 + 2] * w11;
        od[o + 3] = 255;
      }
    }
    octx.putImageData(oimg, 0, 0);
    return out;
  }

  /* ---------- clean-up ----------
     'color' : flatten lighting (divide by local background) and boost contrast
     'gray'  : same, greyscale
     'bw'    : adaptive threshold, pure black text on white
     'none'  : leave as photographed */
  function enhance(canvas, mode = 'color') {
    if (mode === 'none') return canvas;
    const ctx = canvas.getContext('2d'), W = canvas.width, H = canvas.height, n = W * H;
    const img = ctx.getImageData(0, 0, W, H), d = img.data;
    if (mode === 'bw') {
      const g = Uint8Array.from(grayOf(d, n), v => v | 0), I = integral(g, W, H), r = Math.max(8, Math.round(Math.max(W, H) / 40));
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const i = y * W + x, v = g[i] < boxMean(I, W, H, x, y, r) - 9 ? 0 : 255, o = i * 4;
        d[o] = d[o + 1] = d[o + 2] = v;
      }
    } else {
      const r = Math.max(10, Math.round(Math.max(W, H) / 10));
      const chans = mode === 'gray' ? [Uint8Array.from(grayOf(d, n), v => v | 0)] : [0, 1, 2].map(c => { const a = new Uint8Array(n); for (let i = 0; i < n; i++) a[i] = d[i * 4 + c]; return a; });
      const outs = chans.map(ch => {
        const I = integral(ch, W, H), o = new Uint8ClampedArray(n);
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
          const i = y * W + x, bg = Math.max(40, boxMean(I, W, H, x, y, r));
          let v = ch[i] / bg * 245;            // flatten: background -> ~245
          v = (v - 128) * 1.25 + 128;          // a little more contrast
          o[i] = v;
        }
        return o;
      });
      for (let i = 0, o = 0; i < n; i++, o += 4) {
        if (outs.length === 1) d[o] = d[o + 1] = d[o + 2] = outs[0][i];
        else { d[o] = outs[0][i]; d[o + 1] = outs[1][i]; d[o + 2] = outs[2][i]; }
      }
    }
    ctx.putImageData(img, 0, 0);
    return canvas;
  }

  function rotate90(canvas, times = 1) {
    times = ((times % 4) + 4) % 4; if (!times) return canvas;
    const swap = times % 2 === 1, out = document.createElement('canvas');
    out.width = swap ? canvas.height : canvas.width; out.height = swap ? canvas.width : canvas.height;
    const ctx = out.getContext('2d'); ctx.translate(out.width / 2, out.height / 2); ctx.rotate(times * Math.PI / 2); ctx.drawImage(canvas, -canvas.width / 2, -canvas.height / 2);
    return out;
  }
  function toJpeg(canvas, quality = 0.88) { return new Promise(res => canvas.toBlob(res, 'image/jpeg', quality)); }

  return { loadToCanvas, detectCorners, fullCorners, warp, enhance, rotate90, toJpeg, homography };
})();
