const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "../src/wsi_patchkit/web/static/viewer-crops.js"), "utf8");
class Element {
  constructor() { this.children = []; this.events = {}; this.scrollTop = 0; this.text = ""; }
  set textContent(text) { this.text = String(text); }
  get textContent() { return this.text + this.children.map(n => n.textContent).join(""); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.scrollTop = 0; }
  addEventListener(name, fn) { this.events[name] = fn; }
}
const settle = async () => { for (let i = 0; i < 15; i++) await new Promise(setImmediate); };
function screen(records) {
  const list = new Element(), more = new Element(), requests = [], intervals = [];
  const state = {slide: {id: "a"}, active: true};
  let subscriber, ready;
  const document = {hidden: false, createElement: () => new Element(), querySelector: s => ({"#crop-job-list": list, "#crop-jobs-more": more})[s]};
  const sandbox = {
    document, URLSearchParams, setInterval(fn) { intervals.push(fn); },
    window: {patchkitCrop: {selection: () => state, subscribe(fn) { subscriber = fn; }, status(message) { throw new Error(message); }}, addEventListener(_, fn) { ready = fn; }},
    async fetch(endpoint) {
      requests.push(endpoint);
      if (endpoint.startsWith("/api/personal/jobs?")) {
        const params = new URL(endpoint, "http://testserver").searchParams;
        const items = records.filter(j => j.slide_id === params.get("slide_id")).sort((a,b) => Number(["running", "queued"].includes(b.status)) - Number(["running", "queued"].includes(a.status)) || b.created_at - a.created_at);
        const offset = Number(params.get("cursor") || 0), limit = Number(params.get("limit"));
        return Response.json({items: items.slice(offset, offset + limit), counts: {completed: items.filter(j => j.status === "completed").length, running: items.filter(j => j.status === "running").length}, next_cursor: offset + limit < items.length ? String(offset + limit) : null});
      }
      const job = records.find(j => endpoint.endsWith("/" + j.job_id));
      return Response.json(job || {detail: "unknown job"}, {status: job ? 200 : 404});
    },
  };
  vm.runInNewContext(script, sandbox); ready();
  return {list, more, requests, state, document, sync: () => subscriber(), poll: () => intervals[0]()};
}
function records(count, slide = "a") {
  return Array.from({length: count}, (_, i) => ({job_id: slide + i, slide_id: slide, status: i === 0 ? "running" : "completed", created_at: i + 1, filename: `${i}.png`, region: {width: i + 1, height: 8}}));
}
test("viewer loads 20 tasks, appends history, and preserves it while polling", async () => {
  const jobs = records(26), page = screen(jobs);
  await settle();
  assert.equal(page.list.children.length, 21); // summary and 20 rows
  assert.match(page.list.children[1].textContent, /裁剪中/);
  assert.equal(page.more.hidden, false);
  page.more.events.click(); await settle();
  assert.equal(page.list.children.length, 27);
  page.list.scrollTop = 123;
  jobs[0].status = "completed";
  page.poll(); await settle();
  assert.equal(page.list.children.length, 27);
  assert.equal(page.list.scrollTop, 123);
  assert.doesNotMatch(page.list.children.slice(1).map(n => n.textContent).join(""), /裁剪中/);
  assert.ok(page.requests.every(endpoint => !endpoint.startsWith("/api/personal/jobs?") || new URL(endpoint, "http://testserver").searchParams.get("slide_id") === "a"));
  const before = page.requests.length;
  page.document.hidden = true; page.poll(); await settle();
  assert.equal(page.requests.length, before);
  page.document.hidden = false; page.state.active = false; page.poll(); await settle();
  assert.equal(page.requests.length, before);
});
test("switching slides discards the previous slice's loaded history", async () => {
  const page = screen([...records(26), ...records(2, "b")]);
  await settle(); page.more.events.click(); await settle();
  page.state.slide.id = "b"; page.sync(); await settle();
  assert.equal(page.list.children.length, 3);
  assert.equal(page.more.hidden, true);
  assert.match(page.requests.at(-1), /slide_id=b/);
});
test("a burst of new jobs can be paged without losing the gap before loaded history", async () => {
  const jobs = records(26), page = screen(jobs);
  await settle(); page.more.events.click(); await settle();
  jobs[0].status = "completed";
  jobs.push(...records(30).map((j, i) => ({...j, job_id: "new" + i, status: "completed", created_at: 100 + i})));
  page.poll(); await settle();
  assert.equal(page.list.children.length, 47);
  assert.equal(page.more.hidden, false);
  page.more.events.click(); await settle();
  assert.equal(page.list.children.length, 57);
});
