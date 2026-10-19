"use strict";
window.addEventListener("DOMContentLoaded", () => {
  const bridge = window.patchkitCrop;
  if (!bridge) return;
  const $ = selector => document.querySelector(selector);
  const ns = "http://www.w3.org/2000/svg";
  const overlay = document.createElementNS(ns, "svg");
  overlay.id = "crop-planning-overlay";
  overlay.setAttribute("aria-hidden", "true");
  bridge.viewer.canvas.append(overlay);
  let polygon = [], pending = [], regions = [], templates = [], plan = null;
  let planSignature = null, lastSignature = null, slideId = null;
  let selecting = false, jobsLoading = false, appliedLinkedRegion = null;
  const stateNames = {queued: "等待中", running: "裁剪中", completed: "已完成", failed: "失败", cancelled: "已取消"};

  function randomId() {
    return [...crypto.getRandomValues(new Uint8Array(16))]
      .map(n => n.toString(16).padStart(2, "0")).join("");
  }
  function element(tag, text) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  async function api(path, method = "GET", body) {
    const response = await fetch(path, {
      method, cache: "no-store",
      ...(body === undefined ? {} : {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `请求失败（${response.status}）`);
    return result;
  }
  async function run(callback) {
    try { await callback(); } catch (error) { bridge.status(error.message, "error"); }
  }
  function button(text, callback) {
    const node = element("button", text); node.type = "button";
    node.addEventListener("click", () => run(callback)); return node;
  }
  function invalidate() {
    plan = null; planSignature = null;
    $("#crop-submit-grid").disabled = true;
    $("#crop-grid-summary").textContent = "";
  }
  function hint() {
    bridge.incomplete = pending.length > 0;
    const mode = $("#crop-selection-mode").value;
    bridge.drawing = mode !== "move";
    $("#crop-overlay").style.pointerEvents = bridge.drawing ? "none" : "auto";
    $("#crop-finish-polygon").disabled = mode !== "polygon" || pending.length < 3;
    $("#crop-selection-hint").textContent = mode === "move"
      ? "拖动裁剪框，或点击切片以移动矩形中心。"
      : mode === "rectangle"
        ? (pending.length ? "点击切片中的对角，完成矩形选区。" : "在切片中依次点击两个角，选择矩形区域。")
        : pending.length ? `已选择 ${pending.length} 个点，点击“完成多边形”结束选区。`
        : polygon.length ? "多边形已选定。批量裁剪按图块中心筛选，单张裁剪输出外接矩形。"
        : "在切片中逐点点击，选择至少三个点后完成多边形。";
  }
  function draw() {
    const state = bridge.selection(); overlay.replaceChildren();
    overlay.style.display = state.active && state.slide ? "block" : "none";
    if (!state.active || !state.slide) return;
    function shape(points, level, color, closed = true) {
      const mapped = points.map(p => bridge.screenPoint(p[0], p[1], level));
      if (mapped.some(p => !p)) return;
      const node = document.createElementNS(ns, closed ? "polygon" : "polyline");
      node.setAttribute("points", mapped.map(p => `${p.x},${p.y}`).join(" "));
      node.setAttribute("stroke", color); node.setAttribute("stroke-width", "1.5");
      node.setAttribute("fill", closed ? `${color}18` : "none"); overlay.append(node);
    }
    function rectangle(r, color, level = state.level) {
      shape([[r.x,r.y],[r.x+r.width,r.y],[r.x+r.width,r.y+r.height],[r.x,r.y+r.height]], level, color);
    }
    for (const r of regions.filter(r => r.level === state.level)) {
      if (r.polygon?.length) shape(r.polygon, r.level, "#91baff");
      else rectangle(r, "#91baff", r.level);
    }
    if (polygon.length) shape(polygon, state.level, "#8ce3c4");
    if (pending.length) {
      shape(pending, state.level, "#ffce89", false);
      for (const point of pending) {
        const p = bridge.screenPoint(...point, state.level); if (!p) continue;
        const circle = document.createElementNS(ns, "circle");
        circle.setAttribute("cx", p.x); circle.setAttribute("cy", p.y);
        circle.setAttribute("r", "4"); circle.setAttribute("fill", "#ffce89"); overlay.append(circle);
      }
    }
    if (plan) for (const r of plan.regions) rectangle(r, "#f5cb76");
  }
  function payload() {
    const s = bridge.selection();
    if (!s.slide || !s.active) throw new Error("请先打开切片并选择区域");
    if (pending.length) throw new Error("请先完成选区");
    for (const input of $("#crop-panel").querySelectorAll("input[type=number]")) {
      if (!input.reportValidity()) throw new Error("请检查裁剪参数");
    }
    const result = {x: s.x, y: s.y, width: s.width, height: s.height, level: s.level,
      format: $("#crop-format").value, edge: $("#crop-edge").value, polygon};
    for (const [key, selector] of Object.entries({target_mpp: "#crop-target-mpp", tile_width: "#crop-tile-width", tile_height: "#crop-tile-height", stride: "#crop-stride", minimum_tissue: "#crop-minimum-tissue"})) {
      result[key] = Number($(selector).value);
      if (!Number.isFinite(result[key])) throw new Error("请输入有效的数值参数");
    }
    return result;
  }
  function useRegion(region, fit = true) {
    selecting = true;
    try { bridge.select(region, fit); } finally { selecting = false; }
    polygon = region.polygon || []; pending = [];
    $("#crop-selection-mode").value = polygon.length ? "polygon" : "move";
    invalidate(); hint(); draw();
  }
  async function loadRegions() {
    const id = slideId; if (!id) return;
    const data = await api(`/api/personal/slides/${encodeURIComponent(id)}/regions`);
    if (slideId !== id) return;
    regions = data;
    $("#crop-region-list").replaceChildren(...data.map(r => {
      const row = element("div"); row.className = "crop-record";
      const name = element("span", `${r.name} · L${r.level}`); name.title = r.notes || r.name;
      row.append(name, button("使用", () => useRegion(r)), button("删除", async () => {
        await api(`/api/personal/slides/${encodeURIComponent(id)}/regions/${r.id}`, "DELETE");
        await loadRegions();
      })); return row;
    }));
    if (!data.length) $("#crop-region-list").textContent = "还没有保存的选区。";
    useLinkedRegion();
    draw();
  }
  function useLinkedRegion() {
    const requested = new URLSearchParams(location.search), state = bridge.selection();
    const region = regions.find(r => r.id === requested.get("region"));
    if (!region || !state.active || !bridge.viewer.world.getItemCount() || requested.get("slide") !== slideId) return;
    const key = `${slideId}/${region.id}`;
    if (appliedLinkedRegion === key) return;
    appliedLinkedRegion = key; useRegion(region);
  }
  async function loadTemplates() {
    templates = await api("/api/personal/templates");
    $("#crop-template-select").replaceChildren(new Option("选择模板", ""), ...templates.map(t => new Option(t.name, t.id)));
  }
  async function loadJobs() {
    const id = slideId; if (!id || jobsLoading) return;
    jobsLoading = true;
    try {
      const data = await api("/api/personal/jobs"); if (slideId !== id) return;
      const jobs = data.filter(j => j.slide_id === id);
      const summary = element("p", Object.entries(stateNames).map(([s, name]) => `${name} ${jobs.filter(j => j.status === s).length}`).join(" · "));
      summary.className = "crop-hint";
      $("#crop-job-list").replaceChildren(summary, ...jobs.map(j => {
        const row = element("div"); row.className = "crop-record";
        const label = element("span", `${stateNames[j.status]} · ${j.region.width} × ${j.region.height}`);
        label.title = `${j.filename}${j.error ? " · " + j.error : ""}`; row.append(label);
        if (j.status === "queued") row.append(button("取消", async () => { await api(`/api/personal/jobs/${j.job_id}/cancel`, "POST"); await loadJobs(); }));
        if (["failed", "cancelled"].includes(j.status)) row.append(button("重试", async () => { await api(`/api/personal/jobs/${j.job_id}/retry`, "POST"); await loadJobs(); }));
        return row;
      }));
    } finally { jobsLoading = false; }
  }
  function sync() {
    const state = bridge.selection(), id = state.slide?.id || null;
    const signature = JSON.stringify([id, state.level, state.x, state.y, state.width, state.height]);
    if (signature !== lastSignature) {
      invalidate();
      if (!selecting) { polygon = []; pending = []; }
      lastSignature = signature;
    }
    if (id !== slideId) {
      slideId = id; regions = []; $("#crop-region-list").replaceChildren(); $("#crop-job-list").replaceChildren();
      if (id) run(async () => { await Promise.all([loadRegions(), loadTemplates(), loadJobs()]); });
    }
    hint(); draw();
  }
  bridge.subscribe(sync);
  bridge.viewer.addHandler("open", () => { useLinkedRegion(); draw(); });
  bridge.viewer.addHandler("viewport-change", draw);
  bridge.viewer.addHandler("resize", draw);
  bridge.viewer.addHandler("canvas-click", event => {
    const state = bridge.selection();
    if (!state.active || !bridge.drawing || !event.quick || !event.position || !state.slide) return;
    const point = bridge.imagePoint(event.position), level = state.slide.levels.find(l => l.level === state.level);
    if (!point || !level) return;
    const x = Math.round(point.x / level.downsample[0]), y = Math.round(point.y / level.downsample[1]);
    if (x < 0 || y < 0 || x > level.width || y > level.height) return;
    if (pending.length >= 128) { bridge.status("多边形最多支持 128 个点", "error"); return; }
    pending.push([x, y]); polygon = []; invalidate();
    if ($("#crop-selection-mode").value === "rectangle" && pending.length === 2) finish(false);
    event.preventDefaultAction = true; hint(); draw();
  });
  function finish(isPolygon) {
    if (pending.length < (isPolygon ? 3 : 2)) return;
    const xs = pending.map(p => p[0]), ys = pending.map(p => p[1]);
    const x = Math.min(...xs), y = Math.min(...ys), width = Math.max(...xs)-x, height = Math.max(...ys)-y;
    if (!width || !height) { pending.pop(); hint(); draw(); bridge.status("请选择有面积的区域", "error"); return; }
    const region = {x, y, width, height, level: bridge.selection().level, polygon: isPolygon ? [...pending] : []};
    useRegion(region, false);
  }
  $("#crop-finish-polygon").addEventListener("click", () => finish(true));
  $("#crop-selection-mode").addEventListener("change", () => { pending = []; polygon = []; invalidate(); hint(); draw(); });
  $("#crop-panel").addEventListener("input", event => {
    if (["crop-x", "crop-y", "crop-width", "crop-height"].includes(event.target.id)) { pending = []; polygon = []; hint(); }
    invalidate(); draw();
  });
  $("#crop-panel").addEventListener("change", () => { invalidate(); draw(); });
  $("#crop-save-region").addEventListener("click", () => run(async () => {
    const params = payload(), id = slideId;
    const name = prompt("选区名称", "感兴趣区域"); if (!name) return;
    const notes = prompt("选区备注（可留空）", ""); if (notes === null) return;
    await api(`/api/personal/slides/${encodeURIComponent(id)}/regions/${randomId()}`, "PUT", {...params, name, notes});
    bridge.status("选区已保存", "success"); await loadRegions();
  }));
  $("#crop-preview-grid").addEventListener("click", () => run(async () => {
    const params = payload(), signature = JSON.stringify(params), id = slideId, control = $("#crop-preview-grid");
    control.disabled = true;
    try {
      const result = await api(`/api/personal/slides/${encodeURIComponent(id)}/grid-preview`, "POST", params);
      if (slideId !== id || signature !== JSON.stringify(payload())) return;
      plan = result; planSignature = signature;
      $("#crop-grid-summary").textContent = `${result.count} 个图块 · 未压缩像素约 ${(result.estimated_raw_bytes / 1024**2).toLocaleString("zh-CN", {maximumFractionDigits: 2})} MB`;
      $("#crop-submit-grid").disabled = !result.count; draw();
    } finally { control.disabled = false; }
  }));
  $("#crop-submit-grid").addEventListener("click", () => run(async () => {
    const params = payload(), id = slideId;
    if (!plan || JSON.stringify(params) !== planSignature) throw new Error("请先预览当前裁剪网格");
    $("#crop-submit-grid").disabled = true;
    try {
      const result = await api(`/api/personal/slides/${encodeURIComponent(id)}/grid`, "POST", params);
      invalidate(); draw(); bridge.status(`已提交 ${result.count} 个图块任务`, "success"); await loadJobs();
    } finally { $("#crop-submit-grid").disabled = true; }
  }));
  $("#crop-save-template").addEventListener("click", () => run(async () => {
    const name = $("#crop-template-name").value.trim(); if (!name) throw new Error("请输入模板名称");
    await api(`/api/personal/templates/${randomId()}`, "PUT", {...payload(), name});
    await loadTemplates(); bridge.status("参数模板已保存", "success");
  }));
  $("#crop-template-select").addEventListener("change", () => run(() => {
    const t = templates.find(t => t.id === $("#crop-template-select").value); if (!t) return;
    const state = bridge.selection();
    if (state.slide?.levels.some(l => l.level === t.level) && state.level !== t.level) {
      const level = state.slide.levels.find(l => l.level === t.level);
      useRegion({x: 0, y: 0, width: level.width, height: level.height, level: t.level}, false);
    }
    for (const [key, id] of Object.entries({tile_width: "crop-tile-width", tile_height: "crop-tile-height", stride: "crop-stride", format: "crop-format", edge: "crop-edge", minimum_tissue: "crop-minimum-tissue", target_mpp: "crop-target-mpp"})) $("#"+id).value = t[key] ?? 0;
    invalidate(); draw();
  }));
  $("#crop-delete-template").addEventListener("click", () => run(async () => {
    const id = $("#crop-template-select").value; if (!id) return;
    await api(`/api/personal/templates/${id}`, "DELETE"); await loadTemplates();
  }));
  setInterval(() => { if (!document.hidden && bridge.selection().active) run(loadJobs); }, 3000);
  sync();
});
