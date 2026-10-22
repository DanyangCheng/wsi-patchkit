// Exercise the actual viewer code with a deterministic DOM/Canvas and viewport.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.style = {}; this.dataset = {};
    this.events = {}; this.value = ''; this.checked = false;
    this.context = { arcs: [], fills: 0, strokes: 0, setTransform() {},
      beginPath() { this.arcs = []; }, moveTo() {},
      arc(...args) { this.arcs.push(args); },
      fill() { this.fills++; }, stroke() { this.strokes++; } };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelector() { return null; }
  setAttribute() {}
  addEventListener(event, callback) { this.events[event] = callback; }
  getContext() { this.context.arcs = []; return this.context; }
  remove() { this.removed = true; }
}
const elements = new Map();
const document = {
  querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  querySelectorAll() { return []; },
  createElement(tag) { return new Element(tag); },
  createTextNode(text) { return text; },
};
let timer;
let zoom = 0.1;
let requestCount = 0;
let fetchImpl;
const item = {
  viewportToImageZoom() { return zoom; },
  imageToViewerElementCoordinates(p) { return {x: p.x * zoom, y: p.y * zoom}; },
  viewportToImageRectangle() { return {x: 0, y: 0, width: 100, height: 100}; },
};
const viewer = {
  container: {clientWidth: 100, clientHeight: 100}, canvas: new Element(),
  viewport: {getZoom() { return zoom; }, getBounds() { return {}; }},
  world: {getItemCount() { return 1; }, getItemAt() { return item; }},
};
const slide = {
  id: 'case', width: 100, height: 100, mpp: [0.25, 0.5],
  levels: [{downsample: [1, 1]}, {downsample: [4, 4]}],
  overlays: [{id: 'nuclei', display_name: 'Nuclei', encoding: 'objects',
    revision: 'r1', initially_visible: true, default_opacity: 0.5,
    classes: [{id: 0, name: 'Nucleus', rgba: [255, 0, 0, 128]}]}],
};
const sandbox = {
  document, AbortController, URLSearchParams,
  localStorage: {getItem() { return null; }},
  OpenSeadragon: {Point: class {constructor(x,y) {this.x=x; this.y=y;}}},
  window: {devicePixelRatio: 2, addEventListener() {},
    clearTimeout() { timer = null; }, setTimeout(fn) {timer = fn; return 1;}},
  fetch: async (...args) => {requestCount++; return fetchImpl(...args);},
};
vm.createContext(sandbox);
let source = fs.readFileSync('src/wsi_patchkit/web/static/app.js', 'utf8');
source = source.replace('window.addEventListener("DOMContentLoaded", initialize);', `
  globalThis.testAPI = {
    setup(v,s) { viewer=v; currentSlide=s; populateOverlayControls(); },
    controls: () => overlayControls, maskSelection: cropMaskSelection,
    entries: detectionItems, drawDetections, updateDetections, fetchDetections, clearDetections
  };
`);
vm.runInContext(source, sandbox);
const api = sandbox.testAPI;

(async () => {
  api.setup(viewer, slide);
  const entry = api.entries.get('nuclei');
  assert.ok(entry, 'detection controls created');
  assert.equal(entry.radius.value, '4');
  entry.shape.value = 'filled';
  entry.points = [[0, 2.5, 10]];
  api.updateDetections();
  await timer();
  assert.equal(requestCount, 0, 'low zoom performs no point queries');
  assert.equal(entry.points.length, 0);

  zoom = 0.25;
  entry.points = [[0, 2.5, 10]];
  api.drawDetections(entry);
  const ctx = entry.canvas.context;
  assert.deepEqual(ctx.arcs[0].slice(0,3), [2.5, 5, 4]);
  assert.equal(ctx.fillStyle, 'rgb(255,0,0)');
  assert.equal(ctx.globalAlpha, 0.5 * 128 / 255);
  assert.equal(entry.canvas.width, 200, 'canvas supports device pixel ratio');

  entry.radius.value = '7';
  entry.shape.value = 'hollow';
  entry.settings[0].color = [0, 80, 255];
  api.drawDetections(entry);
  assert.equal(ctx.arcs[0][2], 7);
  assert.equal(ctx.strokes, 1);
  assert.equal(ctx.strokeStyle, 'rgb(0,80,255)');
  zoom = 1;
  api.drawDetections(entry);
  assert.deepEqual(ctx.arcs[0].slice(0,3), [10,20,7], 'radius stays fixed across zoom');
  entry.settings[0].visible = false;
  api.drawDetections(entry);
  assert.equal(ctx.arcs.length, 0);
  entry.settings[0].visible = true;

  let page = 0;
  fetchImpl = async (url) => {
    const params = new URL(url, 'http://test').searchParams;
    assert.equal(params.get('downsample'), '1');
    assert.equal(params.get('revision'), 'r1');
    assert.equal(params.get('after'), page === 0 ? '0' : '1');
    const data = page++ === 0 ? {points: [[0,2.5,10]], next_cursor: 1}
      : {points: [[0,5,20]], next_cursor: null};
    return {ok: true, json: async () => data};
  };
  await api.fetchDetections(entry);
  assert.equal(entry.points.length, 2, 'all pages rendered without truncation');

  let release;
  fetchImpl = () => new Promise(resolve => {release = resolve;});
  const pending = api.fetchDetections(entry);
  const signal = entry.controller.signal;
  zoom = 0.1;
  api.updateDetections();
  assert.ok(signal.aborted, 'old request cancelled after zooming out');
  release({ok:true, json:async()=>({points:[[0,2.5,10]],next_cursor:null})});
  await pending;
  assert.equal(entry.points.length, 0, 'stale response cannot restore hidden points');
  api.clearDetections();
  assert.ok(entry.canvas.removed);
  assert.equal(api.entries.size, 0);
  const prediction = {id: 'prediction', encoding: 'indexed', initially_visible: true, default_opacity: 0.7,
    classes: [{id: 0, name: 'Normal', rgba: [0, 0, 0, 0]}, {id: 300, name: 'Tumor', rgba: [255, 0, 0, 255]}, {id: 7, name: 'Other', rgba: [0, 255, 0, 255]}]};
  api.setup(viewer, {...slide, overlays: [...slide.overlays, prediction, {...prediction, id: 'hidden', initially_visible: false}]});
  const masks = elements.get('#crop-masks') || document.querySelector('#crop-masks');
  const mode = document.querySelector('#crop-mask-mode');
  masks.checked = true; mode.value = 'all';
  const plain = value => JSON.parse(JSON.stringify(value));
  assert.deepEqual(plain(api.maskSelection()), {}, 'all mode ignores display settings');
  mode.value = 'visible';
  const controls = api.controls().get('prediction');
  controls.classSettings[2].visible = false;
  const snapshot = api.maskSelection();
  assert.deepEqual(plain(snapshot), {overlay_ids: ['prediction'], overlay_class_ids: {prediction: [300]}}, 'visible mode excludes hidden layers, unchecked classes and detections');
  controls.classSettings[0].visible = true;
  assert.deepEqual(plain(api.maskSelection()).overlay_class_ids.prediction, [0, 300], 'class zero can be explicitly selected');
  assert.deepEqual(plain(snapshot).overlay_class_ids.prediction, [300], 'submission snapshots are independent of later changes');
  controls.opacity.value = '0';
  assert.deepEqual(plain(api.maskSelection()), {overlay_ids: [], overlay_class_ids: {}}, 'zero-opacity layers are excluded');
  masks.checked = false;
  assert.deepEqual(plain(api.maskSelection()), {overlay_ids: []}, 'mask saving remains optional');
  api.setup(viewer, {...slide, overlays: []});
  masks.checked = true;
  assert.deepEqual(plain(api.maskSelection()), {overlay_ids: [], overlay_class_ids: {}}, 'switching slides clears previous controls');
  console.log('Detection controls and visible mask selection passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
