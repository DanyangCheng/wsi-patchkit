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
  querySelector(selector) {
    return this.children.find(c => c.className === selector.slice(1)) || null;
  }
  remove() { this.parent.children = this.parent.children.filter(c => c !== this); }
}

function screen() {
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
    filename, size_bytes: 100, modified_at: "2026-10-19T00:00:00Z",
    image_url: `/api/crops/${filename}/image`,
    download_url: `/api/crops/${filename}/download`,
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
      const data = {
        "/api/crops": crops,
        "/api/personal/slides": [], "/api/personal/jobs": [],
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

  get("#crops-search").value = "alpha";
  await get("#crops-search").dispatch("input");
  assert.equal(get("#crops-grid").children.length, 1);
  get("#select-all").checked = true;
  await get("#select-all").dispatch("change");
  assert.match(get("#selection-count").textContent, /已选 1 张/);
  await get("#download-crops").dispatch("click");
  const download = page.requests.find(r => r.endpoint === "/api/crops/batch-download");
  assert.deepEqual(JSON.parse(download.options.body), { filenames: ["alpha.png"], include_manifest: true });
  assert.equal(page.downloads[0].filename, "wsi-patchkit-crops.zip");

  get("#crops-search").value = "";
  await get("#crops-search").dispatch("input");
  await get("#delete-crops").dispatch("click");
  assert.equal(get("#crops-grid").children.length, 1);
  assert.match(get("#crops-grid").textContent, /beta.png/);
  assert.doesNotMatch(get("#crops-grid").textContent, /alpha.png/);
  assert.equal(get("#delete-crops").disabled, true);
});
