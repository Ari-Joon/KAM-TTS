// Exercise the pause logic and the "how many new while paused" count by
// lifting the real functions out of dashboard.js and giving them a tiny DOM.
import fs from 'node:fs';
const src = fs.readFileSync(
  new URL('../../extension/dashboard.js', import.meta.url),'utf8');

let PASS=0, FAIL=0;
const check=(l,g,w)=>{ const ok=JSON.stringify(g)===JSON.stringify(w);
  ok?(PASS++,console.log('  ok   '+l)):(FAIL++,console.log(`  FAIL ${l}\n         got ${JSON.stringify(g)} want ${JSON.stringify(w)}`)); };

// minimal DOM
const els = {};
const mk = id => ({ id, textContent:'', title:'', scrollTop:0, style:{},
  classList:{ _s:new Set(), add(...c){c.forEach(x=>this._s.add(x))},
    remove(...c){c.forEach(x=>this._s.delete(x))},
    contains(c){return this._s.has(c)}, toggle(c,on){on?this.add(c):this.remove(c)} },
  querySelector(){ return els._topCard || null; },
  querySelectorAll(){ return []; }, appendChild(){}, offsetWidth:0 });
for (const id of ['feed-pause','feed-paused-badge','chunks-body','chunk-counter']) els[id]=mk(id);
globalThis.document = { getElementById:id=>els[id]||null, querySelectorAll:()=>[] };

// pull in just the pause machinery
const grab = name => {
  const i = src.indexOf(`function ${name}(`);
  let d=0, j=i;
  for (; j<src.length; j++){ if(src[j]==='{')d++; else if(src[j]==='}'){d--; if(!d){j++;break;} } }
  return src.slice(i,j);
};
const ctx = { _feedPaused:false, _feedPendingRows:null };
const code = `
  let _feedPaused=false, _feedPendingRows=null, _allChunks=[], _feedSelectMode=false, _chunkCount=0;
  ${grab('_setFeedPaused')}
  ${grab('_updatePausedBadge')}
  const _renderChunkFeed = () => { rendered++; };
  let rendered = 0;
  return { get paused(){return _feedPaused}, set paused(v){_feedPaused=v},
           get pending(){return _feedPendingRows}, set pending(v){_feedPendingRows=v},
           get rendered(){return rendered},
           setPaused:_setFeedPaused, badge:_updatePausedBadge };
`;
const api = new Function(code)();

console.log('\n=== pause toggling ===');
api.setPaused(true);
check('button shows resume glyph', els['feed-pause'].textContent, '▶');
check('PAUSED badge visible', els['feed-paused-badge'].style.display, '');
check('flag set', api.paused, true);

api.setPaused(false);
check('button back to pause glyph', els['feed-pause'].textContent, '⏸');
check('badge hidden', els['feed-paused-badge'].style.display, 'none');
check('resume scrolls back to newest', els['chunks-body'].scrollTop, 0);

console.log('\n=== pending count while paused ===');
api.badge(0);
check('no new chunks reads plain', els['feed-paused-badge'].textContent, 'PAUSED');
api.badge(7);
check('seven waiting is shown', els['feed-paused-badge'].textContent, 'PAUSED · 7 NEW');

console.log('\n=== resume renders what arrived ===');
api.paused = true; api.pending = [{chunk_id:'a'}];
const before = api.rendered;
api.setPaused(false);
check('buffered rows were rendered on resume', api.rendered, before+1);
check('buffer cleared', api.pending, null);

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL?1:0);
