const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticDir = path.join(__dirname, "../src/wsi_patchkit/web/static");
const html = fs.readFileSync(path.join(staticDir, "personal.html"), "utf8");
const script = fs.readFileSync(path.join(staticDir, "personal.js"), "utf8");

// Build the controls from the actual page markup, so a renamed element cannot
// be silently supplied by the test when the production script requests it.
class Element {
  constructor(tag = "div", text = "") {
    this.tag = tag; this.text = text; this.children = []; this.events = {};
    this.dataset = {}; this.value = ""; this.checked = false;
    this.classList = { toggle() {} }; this.elements = {};
  }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(""); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get options() { return this.children; }
  append(...children) {
    for (const child of children) {
      const item = typeof child === "string" ? new Element("text", child) : child;
      item.parent = this; this.children.push(item);
    }
  }
  replaceChildren(...children) { this.text = ""; this.children = []; this.append(...children); }
  addEventListener(event, callback) { (this.events[event] ||= []).push(callback); }
  async dispatch(event) {
    for (const callback of this.events[event] || []) {
      await callback({ target: this, currentTarget: this, preventDefault() {} });
    }
  }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  querySelector(selector) {
    return this.children.find(c => c.className === selector.slice(1)) || null;
  }
  remove() { this.parent.children = this.parent.children.filter(c => c !== this); }
}

function screen({ jobRecords = [], source = null, slideRecords = [] } = {}) {
  const controls = new Map(), panels = [], tabs = [], downloads = [], requests = [];
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
    const [, tag, attrs] = match, node = new Element(tag);
    const id = attrs.match(/\bid="([^"]+)"/);
    if (id) controls.set("#" + id[1], node);
    for (const [key, list] of [["panel", panels], ["tab", tabs]]) {
      const value = attrs.match(new RegExp(`data-${key}="([^"]+)"`));
      if (value) { node.dataset[key] = value[1]; list.push(node); }
    }
  }
  let crops = ["alpha.png", "beta.png"].map(filename => ({
    filename, source, size_bytes: 100, modified_at: "2026-10-19T00:00:00Z",
    image_url: `/api/crops/${filename}/image`,
    download_url: `/api/crops/${filename}/download`,
    export_url: `/api/crops/${filename}/export`,
  }));
  const body = new Element("body");
  const document = {
    body, hidden: false,
    querySelector: selector => controls.get(selector) || null,
    querySelectorAll: selector => selector === "[data-panel]" ? panels : tabs,
    createElement(tag) {
      const node = new Element(tag);
      node.click = () => downloads.push({ filename: node.download, href: node.href });
      return node;
    },
    createTextNode: text => new Element("text", text),
  };
  const url = class extends URL {};
  url.createObjectURL = () => "blob:test-download";
  url.revokeObjectURL = () => {};
  const sandbox = {
    document, location: { hash: "#crops" },
    window: { addEventListener() {} },
    Option: class extends Element { constructor(text, value) { super("option", text); this.value = value; } },
    URL: url, URLSearchParams, Response, FormData,
    confirm: () => true, setInterval() {}, setTimeout() {},
    async fetch(endpoint, options = {}) {
      requests.push({ endpoint, options });
      if (endpoint === "/api/crops/batch-download") return new Response("test archive");
      if (endpoint === "/api/crops/batch-delete") {
        const { filenames } = JSON.parse(options.body);
        crops = crops.filter(c => !filenames.includes(c.filename));
        return Response.json({ filenames, deleted: filenames.length });
      }
      if (endpoint.startsWith("/api/personal/jobs?")) {
        const params = new URL(endpoint, "http://testserver").searchParams;
        const filtered = jobRecords.filter(j => (!params.get("status") || j.status === params.get("status")) && (!params.get("slide_id") || j.slide_id === params.get("slide_id")));
        const offset = Number(params.get("cursor") || 0), limit = Number(params.get("limit"));
        return Response.json({ items: filtered.slice(offset, offset + limit), counts: {completed: filtered.length}, next_cursor: offset + limit < filtered.length ? String(offset + limit) : null });
      }
      const data = {
        "/api/crops": crops,
        "/api/crops/exporters": [{id: "indexed-png", name: "类别索引 PNG"}, {id: "none", name: "无"}, {id: "segmentation-mask", name: "Segmentation Mask 1.1"}, {id: "custom", name: "Custom"}],
        "/api/personal/slides": slideRecords, "/api/personal/jobs": [],
        "/api/personal/trash": [], "/api/personal/uploads": [],
        "/api/personal/account": { enabled: false },
        "/api/personal/overview": {
          slides: 0, uploads: 0, crops: crops.length, upload_bytes: 0,
          crop_bytes: 200, pending_bytes: 0, max_upload_bytes: 1024,
          recent_views: [], jobs: { queued: 0, running: 0, failed: 0 },
        },
      };
      assert.ok(endpoint in data, `unexpected request: ${endpoint}`);
      return Response.json(data[endpoint]);
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(staticDir, "crop-exports.js"), "utf8"), sandbox);
  vm.runInNewContext(script, sandbox);
  return { controls, panels, downloads, requests };
}

async function settle() {
  for (let i = 0; i < 10; i++) await new Promise(setImmediate);
}

test("crop management initializes, filters, downloads and deletes selected crops", async () => {
  const page = screen(), get = selector => page.controls.get(selector);
  await settle();
  assert.equal(get("#page-title").textContent, "裁剪管理");
  assert.equal(page.panels.find(p => p.dataset.panel === "crops").hidden, false);
  assert.equal(get("#crops-grid").children.length, 2);
  assert.equal(get("#stats").children.length, 4);
  assert.equal(get("#download-crops").disabled, true);
  assert.equal(get("#download-crop-labels").disabled, true);

  get("#crops-search").value = "alpha";
  await get("#crops-search").dispatch("input");
  assert.equal(get("#crops-grid").children.length, 1);
  get("#select-all").checked = true;
  await get("#select-all").dispatch("change");
  assert.match(get("#selection-count").textContent, /已选 1 张/);
  await get("#download-crops").dispatch("click");
  const download = page.requests.find(r => r.endpoint === "/api/crops/batch-download");
  assert.deepEqual(JSON.parse(download.options.body), { filenames: ["alpha.png"], include_images: true, label_format: "none" });
  assert.equal(page.downloads[0].filename, "wsi-patchkit-images.zip");

  get("#crops-search").value = "";
  await get("#crops-search").dispatch("input");
  await get("#delete-crops").dispatch("click");
  assert.equal(get("#crops-grid").children.length, 1);
  assert.match(get("#crops-grid").textContent, /beta.png/);
  assert.doesNotMatch(get("#crops-grid").textContent, /alpha.png/);
  assert.equal(get("#delete-crops").disabled, true);
});

test("task records paginate on the server and reset the cursor on filter changes", async () => {
  const jobRecords = Array.from({length: 70}, (_, i) => ({job_id: String(i), filename: `crop-${i}.png`, status: "completed", slide_id: "slide", level: 0, created_at: 100, region: {x: 0, y: 0, width: 8, height: 8}}));
  const page = screen({jobRecords}), get = selector => page.controls.get(selector);
  await settle();
  assert.equal(get("#job-list").children.length, 50);
  assert.equal(get("#jobs-previous").disabled, true);
  assert.equal(get("#jobs-next").disabled, false);
  await get("#jobs-next").dispatch("click"); await settle();
  assert.equal(get("#job-list").children.length, 20);
  assert.equal(get("#jobs-previous").disabled, false);
  assert.equal(get("#jobs-next").disabled, true);
  await get("#jobs-previous").dispatch("click"); await settle();
  assert.equal(get("#job-list").children.length, 50);
  get("#job-filter").value = "failed";
  await get("#job-filter").dispatch("change"); await settle();
  assert.match(get("#job-list").textContent, /当前没有/);
  assert.equal(get("#jobs-previous").disabled, true);
  const params = new URL(page.requests.filter(r => r.endpoint.startsWith("/api/personal/jobs?")).at(-1).endpoint, "http://testserver").searchParams;
  assert.equal(params.get("status"), "failed");
  assert.equal(params.get("cursor"), null);
});

test("crop cards retain source coordinates and have no per-image actions", async () => {
  const page = screen({source: {slide_id: "slide", level: 0, region: {x: 1, y: 2, width: 8, height: 8}}});
  await settle();
  const grid = page.controls.get("#crops-grid");
  assert.match(grid.textContent, /Level 0 · \(1, 2\) · 8 × 8/);
  for (const card of grid.children) {
    const body = card.children.find(c => c.className === "card-body");
    assert.equal(body.children.some(c => c.className === "actions" || c.tag === "a" || c.tag === "button"), false);
  }
  assert.doesNotMatch(grid.textContent, /来源坐标未记录/);
});


test("annotation downloads use the selected format without images", async () => {
  const page = screen(), get = selector => page.controls.get(selector);
  await settle();
  assert.deepEqual(get("#crop-label-format").options.map(o => o.value), ["indexed-png", "segmentation-mask", "custom"]);
  assert.equal(get("#crop-label-format").value, "segmentation-mask");
  get("#crop-label-format").value = "custom";
  await get("#crop-label-format").dispatch("change");
  get("#select-all").checked = true;
  await get("#select-all").dispatch("change");
  await get("#download-crop-labels").dispatch("click");
  const request = page.requests.find(r => r.endpoint === "/api/crops/batch-download");
  assert.equal(JSON.parse(request.options.body).label_format, "custom");
  assert.equal(JSON.parse(request.options.body).include_images, false);
  assert.equal(page.downloads[0].filename, "wsi-patchkit-annotations.zip");
});


test("thumbnail opens crop details and a source link with native coordinates", async () => {
  const source = {slide_id: "slide / 测试", level: 2, region: {x: 11, y: 22, width: 333, height: 444}};
  const page = screen({source, slideRecords: [{id: source.slide_id, path: "source.tif", tags: []}]}), get = selector => page.controls.get(selector);
  await settle();
  const slideCard = get("#slide-grid").children[0];
  assert.match(slideCard.textContent, /切片详情/);
  assert.match(slideCard.textContent, /查看与裁剪/);
  const preview = get("#crops-grid").children[0].children[0];
  assert.equal(preview.tag, "button");
  assert.equal(preview["aria-haspopup"], "dialog");
  await preview.dispatch("click");
  assert.equal(get("#crop-detail-dialog").open, true);
  assert.equal(get("#crop-detail-title").textContent, "alpha.png");
  assert.equal(get("#crop-detail-preview").src, "/api/crops/alpha.png/image");
  assert.match(get("#crop-detail-info").textContent, /source.tif/);
  assert.match(get("#crop-detail-info").textContent, /Level 2/);
  assert.equal(get("#crop-locate-source").hidden, false);
  const params = new URL(get("#crop-locate-source").href, "http://testserver").searchParams;
  for (const [key, value] of Object.entries({slide: source.slide_id, crop: "1", level: "2", x: "11", y: "22", width: "333", height: "444"})) assert.equal(params.get(key), value);
  assert.match(get("#selection-count").textContent, /已选 0 张/);
  await get("#close-crop-detail").dispatch("click");
  assert.equal(get("#crop-detail-dialog").open, false);
});

test("details explain missing or unavailable sources without retaining a previous link", async () => {
  for (const source of [null, {slide_id: "removed", level: 0, region: {x: 1, y: 2, width: 8, height: 8}}]) {
    const page = screen({source}), get = selector => page.controls.get(selector);
    await settle();
    get("#crop-locate-source").href = "/?slide=previous";
    await get("#crops-grid").children[0].children[0].dispatch("click");
    assert.equal(get("#crop-detail-dialog").open, true);
    assert.equal(get("#crop-locate-source").hidden, true);
    assert.equal(get("#crop-locate-source").href, undefined);
    assert.match(get("#crop-source-status").textContent, source ? /当前不可访问/ : /未记录来源坐标/);
  }
});
