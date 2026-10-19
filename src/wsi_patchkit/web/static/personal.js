"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  const names = { overview: "工作台", slides: "切片库", uploads: "上传管理", jobs: "任务记录", crops: "裁剪管理", trash: "回收站", account: "账号设置" };
  const states = { queued: "等待中", running: "裁剪中", completed: "已完成", failed: "失败", cancelled: "已取消" };
  let slides = [], jobs = [], crops = [], uploads = [], selected = new Set();
  let detail = null;
  let detailSequence = 0, busyRefresh = false;
  const activeUploads = new Map();
  const uploadMessages = new Map();
  function node(tag, content, className) {
    const n = document.createElement(tag);
    if (content !== undefined) n.textContent = content;
    if (className) n.className = className;
    return n;
  }
  function action(label, callback, className = "secondary") {
    const b = node("button", label, className); b.type = "button";
    b.addEventListener("click", () => run(callback)); return b;
  }
  function link(label, href, className = "secondary") {
    const n = node("a", label, className); n.href = href; return n;
  }
  function status(message, error = false, target = "#status") {
    $(target).textContent = message; $(target).classList.toggle("error", error);
  }
  async function run(callback, target = "#status") {
    try { return await callback(); } catch (e) { status(e.message, true, target); }
  }
  async function api(path, method = "GET", payload) {
    const response = await fetch(path, {
      method, cache: "no-store", headers: payload === undefined ? {} : { "Content-Type": "application/json" },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `请求失败（${response.status}）`);
    return result;
  }
  function bytes(n) {
    if (!n) return "0 B";
    const i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${["B", "KB", "MB", "GB", "TB"][i]}`;
  }
  function date(n) { return n ? new Date(typeof n === "number" ? n * 1000 : n).toLocaleString("zh-CN") : "时间未知"; }
  function title(s) { return s.path || s.id; }
  function thumb(id) { return `/iiif/3/${encodeURIComponent(id)}/full/384,/0/default.jpg`; }
  function viewerLink(id, job) {
    const params = new URLSearchParams({ slide: id, crop: "1" });
    if (job) {
      params.set("level", job.level);
      for (const k of ["x", "y", "width", "height"]) params.set(k, job.region[k]);
    }
    return `/?${params}`;
  }
  function empty(container, message) { container.replaceChildren(node("p", message, "empty")); }
  function row(label, description, actions = []) {
    const r = node("div", undefined, "row"), body = node("div", undefined, "grow");
    body.append(node("strong", label), node("p", description)); r.append(body, ...actions); return r;
  }
  function info(target, items) {
    $(target).replaceChildren(...items.flatMap(([k, v]) => [node("dt", k), node("dd", String(v))]));
  }
  function activeTab() { return location.hash.slice(1) in names ? location.hash.slice(1) : "overview"; }
  function route() {
    const tab = activeTab(); $("#page-title").textContent = names[tab];
    document.querySelectorAll("[data-panel]").forEach(n => { n.hidden = n.dataset.panel !== tab; });
    document.querySelectorAll("[data-tab]").forEach(n => { n.classList.toggle("active", n.dataset.tab === tab); n.setAttribute("aria-current", n.dataset.tab === tab ? "page" : "false"); });
  }
  function renderOverview(data) {
    const stats = [["可访问切片", data.slides, `个人上传 ${data.uploads} 张`], ["裁剪结果", data.crops, `占用 ${bytes(data.crop_bytes)}`], ["进行中的任务", data.jobs.queued + data.jobs.running, `失败 ${data.jobs.failed} 个`], ["个人文件占用", bytes(data.upload_bytes + data.crop_bytes + data.pending_bytes), "含回收站和未完成上传"]];
    $("#stats").replaceChildren(...stats.map(([label, value, sub]) => {
      const n = node("article", undefined, "stat"); n.append(node("span", label), node("strong", value), node("small", sub)); return n;
    }));
    $("#upload-limit").textContent = `单张最大 ${bytes(data.max_upload_bytes)}；每个分块最多 8 MB。`;
    $("#recent-views").replaceChildren(...data.recent_views.map(v => {
      const s = slides.find(s => s.id === v.id);
      return row(title(s), date(v.viewed_at), [action("继续查看", () => openDetail(s))]);
    }));
    if (!data.recent_views.length) empty($("#recent-views"), "打开切片后，最近查看的记录会出现在这里。");
    $("#recent-slides").replaceChildren(...slides.filter(s => s.uploaded).slice(0, 5).map(s => row(title(s), `${bytes(s.size_bytes)} · ${date(s.modified_at)}`, [action("打开", () => openDetail(s))])));
    if (!$("#recent-slides").children.length) empty($("#recent-slides"), "上传第一张切片，开始整理你的工作空间。");
    $("#recent-jobs").replaceChildren(...jobs.slice(0, 5).map(j => row(j.filename, `${states[j.status]} · ${date(j.created_at)}`, [link("查看任务", "#jobs")])));
    if (!jobs.length) empty($("#recent-jobs"), "还没有裁剪任务。可从切片库选择区域。");
  }
  function filteredSlides() {
    const query = $("#slide-search").value.toLowerCase(), project = $("#project-filter").value;
    return slides.filter(s => (!project || s.project === project) && [title(s), s.project, s.notes, ...s.tags].join(" ").toLowerCase().includes(query)).sort((a, b) => $("#slide-sort").value === "name" ? title(a).localeCompare(title(b)) : $("#slide-sort").value === "size" ? b.size_bytes-a.size_bytes : b.modified_at-a.modified_at);
  }
  function renderSlides() {
    const cards = filteredSlides().map(s => {
      const c = node("article", undefined, "card"), image = node("img", undefined, "thumbnail");
      image.src = thumb(s.id); image.loading = "lazy"; image.alt = title(s);
      const body = node("div", undefined, "card-body");
      body.append(node("h2", title(s)), node("p", `${bytes(s.size_bytes)} · ${s.uploaded ? "个人上传" : "共享切片"}`));
      for (const t of [s.project, ...s.tags].filter(Boolean)) body.append(node("span", t, "badge"));
      const actions = node("div", undefined, "actions"); actions.append(action("切片详情", () => openDetail(s)), link("查看与裁剪", viewerLink(s.id)));
      body.append(actions); c.append(image, body); return c;
    });
    $("#slide-grid").replaceChildren(...cards);
    if (!cards.length) empty($("#slide-grid"), "没有匹配的切片。可以调整搜索或上传切片。");
  }
  function renderJobs() {
    const filtered = jobs.filter(j => !$("#job-filter").value || j.status === $("#job-filter").value);
    $("#job-list").replaceChildren(...filtered.map(j => {
      const actions = [link("定位区域", viewerLink(j.slide_id, j))];
      if (j.status === "queued") actions.push(action("取消", async () => { await api(`/api/personal/jobs/${j.job_id}/cancel`, "POST"); await refresh(); }));
      if (["failed", "cancelled"].includes(j.status)) actions.push(action("重试", async () => { await api(`/api/personal/jobs/${j.job_id}/retry`, "POST"); await refresh(); }));
      if (j.status === "completed" && crops.some(r => r.filename === j.filename)) actions.push(link("下载", `/api/crops/${encodeURIComponent(j.filename)}/download`));
      const r = row(j.filename, `${states[j.status]} · Level ${j.level} · ${j.region.width} × ${j.region.height} · ${date(j.created_at)}${j.error ? ` · ${j.error}` : ""}`, actions);
      r.classList.add("panel"); return r;
    }));
    if (!filtered.length) empty($("#job-list"), "当前没有此状态的任务。");
  }
  function visibleCrops() { return crops.filter(r => r.filename.toLowerCase().includes($("#crops-search").value.toLowerCase())); }
  function renderCrops() {
    const available = new Set(crops.map(r => r.filename));
    selected = new Set([...selected].filter(s => available.has(s)));
    const filtered = visibleCrops();
    $("#crops-grid").replaceChildren(...filtered.map(r => {
      const c = node("article", undefined, "card"), image = node("img", undefined, "thumbnail");
      image.src = r.image_url; image.alt = r.filename; image.loading = "lazy";
      const body = node("div", undefined, "card-body"), label = node("label"), checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(r.filename); checkbox.setAttribute("aria-label", `选择 ${r.filename}`);
      checkbox.addEventListener("change", () => { checkbox.checked ? selected.add(r.filename) : selected.delete(r.filename); selectionInfo(); }); label.append(checkbox, document.createTextNode(" 选择"));
      body.append(label, node("h2", r.filename), node("p", `${bytes(r.size_bytes)} · ${date(r.modified_at)}`));
      const j = jobs.find(j => j.filename === r.filename && j.status === "completed");
      const actions = node("div", undefined, "actions"); actions.append(link("下载", r.download_url));
      if (j) { body.append(node("p", `Level ${j.level} · (${j.region.x}, ${j.region.y}) · ${j.region.width} × ${j.region.height}`)); actions.append(link("定位来源", viewerLink(j.slide_id, j))); }
      else body.append(node("p", "历史图像：来源坐标未记录"));
      body.append(actions); c.append(image, body); return c;
    }));
    if (!filtered.length) empty($("#crops-grid"), "还没有匹配的裁剪结果。");
    selectionInfo();
  }
  function selectionInfo() {
    const visible = visibleCrops(), count = visible.filter(r => selected.has(r.filename)).length;
    $("#selection-count").textContent = `共 ${crops.length} 张 · 已选 ${selected.size} 张`;
    $("#select-all").checked = visible.length > 0 && count === visible.length;
    $("#select-all").indeterminate = count > 0 && count < visible.length;
    $("#download-crops").disabled = $("#delete-crops").disabled = selected.size === 0;
  }
  async function downloadResponse(response, filename) {
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || "下载失败"); }
    const url = URL.createObjectURL(await response.blob()); const a = link("", url); a.download = filename; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  async function refresh() {
    if (busyRefresh) return;
    busyRefresh = true; $("#refresh").disabled = true;
    try {
      const data = await Promise.all([api("/api/personal/slides"), api("/api/personal/jobs"), api("/api/crops"), api("/api/personal/overview"), api("/api/personal/trash"), api("/api/personal/uploads")]);
      [slides, jobs, crops] = data; uploads = data[5].map(r => activeUploads.has(r.id) ? (uploads.find(u => u.id === r.id) || r) : r);
      const current = $("#project-filter").value;
      $("#project-filter").replaceChildren(new Option("所有项目", ""), ...[...new Set(slides.map(s => s.project).filter(Boolean))].sort().map(p => new Option(p, p)));
      $("#project-filter").value = [...$("#project-filter").options].some(o => o.value === current) ? current : "";
      renderSlides(); renderJobs(); renderCrops(); renderOverview(data[3]); renderUploads();
      $("#trash-list").replaceChildren(...data[4].map(r => row(r.name, `移入时间 ${date(r.deleted_at)} · 关联任务 ${r.related_jobs}`, [action("恢复", async () => { await api(`/api/personal/slides/${encodeURIComponent(r.id)}/restore`, "POST"); status("切片已恢复"); await refresh(); })])));
      if (!data[4].length) empty($("#trash-list"), "回收站为空。");
    } finally { busyRefresh = false; $("#refresh").disabled = false; }
  }
  async function loadAccount() {
    const result = await api("/api/personal/account");
    $("#account-disabled").hidden = result.enabled; $("#account-content").hidden = !result.enabled;
    if (!result.enabled) return;
    const user = result.user;
    info("#account-info", [["用户名", user.username], ["账号角色", user.is_admin ? "管理员" : "普通用户"], ["账号状态", user.is_active ? "正常" : "已停用"], ["创建时间", date(user.created_at)]]);
    $("#profile-form").elements.display_name.value = result.profile.display_name || "";
    $("#profile-form").elements.notes.value = result.profile.notes || "";
    $("#greeting").textContent = `${result.profile.display_name || user.username}，开始今天的探索。`;
    $("#admin-link").hidden = !user.is_admin;
  }
  async function loadRegions() {
    const id = detail.id;
    const regions = await api(`/api/personal/slides/${encodeURIComponent(id)}/regions`);
    if (detail?.id !== id) return;
    $("#region-list").replaceChildren(...regions.map(r => {
      const href = viewerLink(id, {level: r.level, region: r}) + `&region=${encodeURIComponent(r.id)}`;
      return row(r.name, `L${r.level}: ${r.width} × ${r.height}${r.notes ? " · " + r.notes : ""}`, [link("在查看器中使用", href), action("删除", async () => { await api(`/api/personal/slides/${encodeURIComponent(id)}/regions/${r.id}`, "DELETE"); await loadRegions(); }, "danger")]);
    }));
    if (!regions.length) empty($("#region-list"), "在查看器中保存选区后，可从这里继续使用。");
  }
  async function openDetail(s) {
    const sequence = ++detailSequence; detail = s;
    $("#detail-title").textContent = title(s); $("#slide-preview").src = thumb(s.id);
    $("#open-viewer").href = viewerLink(s.id); $("#trash-slide").hidden = !s.uploaded;
    for (const k of ["project", "notes"]) $("#slide-form").elements[k].value = s[k] || "";
    $("#slide-form").elements.tags.value = s.tags.join(", ");
    status("正在读取切片信息…", false, "#detail-status");
    $("#slide-info").replaceChildren(); $("#region-list").replaceChildren();
    if (!$("#slide-dialog").open) $("#slide-dialog").showModal();
    try {
      const data = await api(`/api/slides/${encodeURIComponent(s.id)}`);
      if (sequence !== detailSequence) return;
      info("#slide-info", [["尺寸", `${data.width} × ${data.height} px`], ["MPP", data.mpp ? data.mpp.join(" × ") + " μm/px" : "未知"], ["文件大小", bytes(s.size_bytes)], ["格式", title(s).split(".").pop().toUpperCase()], ["来源", s.uploaded ? "个人上传" : "共享切片"]]);
      await Promise.all([loadRegions(), api(`/api/personal/slides/${encodeURIComponent(s.id)}/view`, "POST")]);
      if (sequence === detailSequence) status("", false, "#detail-status");
    } catch (e) { if (sequence === detailSequence) status(e.message, true, "#detail-status"); }
  }
  function renderUploads() {
    $("#upload-list").replaceChildren(...uploads.map(u => {
      const running = activeUploads.has(u.id), message = uploadMessages.get(u.id);
      const actions = running ? [action("暂停", () => { activeUploads.get(u.id).abort(); })] : [action("选择原文件继续", () => $("#upload-files").click()), action("移除", async () => {
        if (!confirm(`移除“${u.filename}”的未完成上传？`)) return;
        await api(`/api/personal/uploads/${u.id}`, "DELETE"); uploadMessages.delete(u.id); uploads = uploads.filter(x => x.id !== u.id); renderUploads();
      }, "danger")];
      const r = row(u.filename, message || `${bytes(u.offset)} / ${bytes(u.size)} · ${running ? "传输中" : "待继续"}`, actions);
      const progress = node("progress"); progress.max = u.size; progress.value = u.offset; progress.setAttribute("aria-label", `${u.filename} 上传进度`); r.querySelector(".grow").append(progress); return r;
    }));
    if (!uploads.length) empty($("#upload-list"), "暂无未完成上传。");
  }
  async function upload(file) {
    let u = uploads.find(u => u.filename === file.name && u.size === file.size);
    if (u && !confirm(`发现“${file.name}”的未完成上传。确认选择的是同一个原文件并继续？`)) return;
    if (!u && slides.some(s => s.uploaded && title(s).split("/").pop() === file.name && s.size_bytes === file.size) && !confirm(`切片库中已有同名同大小文件“${file.name}”。是否仍然上传一份副本？`)) return;
    if (!u) { u = await api("/api/personal/uploads", "POST", { filename: file.name, size: file.size }); uploads.push(u); }
    if (activeUploads.has(u.id)) return;
    const controller = new AbortController(); activeUploads.set(u.id, controller); uploadMessages.delete(u.id); renderUploads();
    try {
      while (u.offset < file.size) {
        const chunk = file.slice(u.offset, Math.min(u.offset+8*1024*1024, file.size));
        const response = await fetch(`/api/personal/uploads/${u.id}?offset=${u.offset}`, { method: "PUT", body: chunk, signal: controller.signal });
        const result = await response.json(); if (!response.ok) throw new Error(result.detail || "上传失败");
        u.offset = result.offset; renderUploads();
      }
      if (controller.signal.aborted) throw new DOMException("暂停", "AbortError");
      uploadMessages.set(u.id, "传输完成，正在校验切片…"); renderUploads();
      await api(`/api/personal/uploads/${u.id}/complete`, "POST");
      uploads = uploads.filter(x => x.id !== u.id); status(`“${file.name}”上传成功`);
    } catch (e) {
      uploadMessages.set(u.id, e.name === "AbortError" ? "已暂停，重新选择原文件可继续" : `上传失败：${e.message}`);
      // A lost response may follow a successful append; always refresh server offset.
      try { const pending = await api("/api/personal/uploads"); const fresh = pending.find(x => x.id === u.id); if (fresh) u.offset = fresh.offset; } catch (_) { /* Resync again on refresh. */ }
    } finally { activeUploads.delete(u.id); renderUploads(); }
  }
  let uploadingBatch = false;
  async function uploadFiles(files) {
    if (uploadingBatch) { status("请等待当前批次完成后再选择文件", true); return; }
    uploadingBatch = true; $("#upload-files").disabled = true;
    try { for (const file of files) await run(() => upload(file)); }
    finally { uploadingBatch = false; $("#upload-files").disabled = false; $("#upload-files").value = ""; await run(refresh); }
  }
  window.addEventListener("hashchange", route);
  $("#refresh").addEventListener("click", () => run(refresh));
  for (const selector of ["#slide-search", "#project-filter", "#slide-sort"]) $(selector).addEventListener("input", renderSlides);
  $("#job-filter").addEventListener("change", renderJobs);
  $("#crops-search").addEventListener("input", renderCrops);
  $("#select-all").addEventListener("change", e => { for (const r of visibleCrops()) e.target.checked ? selected.add(r.filename) : selected.delete(r.filename); renderCrops(); });
  $("#download-crops").addEventListener("click", () => run(async () => { await downloadResponse(await fetch("/api/crops/batch-download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filenames: [...selected], include_manifest: true }) }), "wsi-patchkit-crops.zip"); }));
  $("#download-manifest").addEventListener("click", () => run(async () => {
    const manifest = await api("/api/personal/manifest");
    if (selected.size) manifest.crops = manifest.crops.filter(j => selected.has(j.filename));
    await downloadResponse(new Response(JSON.stringify(manifest, null, 2), { headers: { "Content-Type": "application/json" } }), "crop-manifest.json");
  }));
  $("#delete-crops").addEventListener("click", () => run(async () => {
    if (!selected.size || !confirm(`永久删除所选 ${selected.size} 张裁剪图？`)) return;
    await api("/api/crops/batch-delete", "POST", { filenames: [...selected] }); selected.clear(); await refresh();
  }));
  $("#close-detail").addEventListener("click", () => { ++detailSequence; $("#slide-dialog").close(); });
  $("#slide-form").addEventListener("submit", e => { e.preventDefault(); run(async () => {
    const data = Object.fromEntries(new FormData(e.target)); data.tags = data.tags.split(/[,，]/).map(t => t.trim()).filter(Boolean);
    await api(`/api/personal/slides/${encodeURIComponent(detail.id)}`, "PUT", data); status("切片资料已保存", false, "#detail-status"); await refresh();
  }, "#detail-status"); });
  $("#trash-slide").addEventListener("click", () => run(async () => {
    const count = jobs.filter(j => j.slide_id === detail.id).length;
    if (!confirm(`将“${title(detail)}”移入回收站？关联 ${count} 个裁剪任务；裁剪结果保留。`)) return;
    await api(`/api/personal/slides/${encodeURIComponent(detail.id)}/trash`, "POST"); $("#slide-dialog").close(); status("已移入回收站，可随时恢复"); await refresh();
  }, "#detail-status"));
  $("#upload-files").addEventListener("change", e => uploadFiles([...e.target.files]));
  const drop = $("#dropzone");
  for (const event of ["dragenter", "dragover"]) drop.addEventListener(event, e => { e.preventDefault(); drop.classList.add("dragover"); });
  for (const event of ["dragleave", "drop"]) drop.addEventListener(event, e => { e.preventDefault(); drop.classList.remove("dragover"); });
  drop.addEventListener("drop", e => { location.hash = "uploads"; uploadFiles([...e.dataTransfer.files]); });
  $("#profile-form").addEventListener("submit", e => { e.preventDefault(); run(async () => {
    const button = e.submitter; button.disabled = true;
    try { await api("/api/personal/account", "PUT", Object.fromEntries(new FormData(e.target))); status("资料已保存", false, "#profile-status"); await loadAccount(); } finally { button.disabled = false; }
  }, "#profile-status"); });
  $("#password-form").addEventListener("submit", e => { e.preventDefault(); run(async () => {
    const p = Object.fromEntries(new FormData(e.target));
    if (p.new_password !== p.confirm_password) throw new Error("两次输入的新密码不一致");
    if (p.new_password === p.current_password) throw new Error("新密码应与当前密码不同");
    const button = e.submitter; button.disabled = true;
    status("正在更新密码…", false, "#password-status");
    try { await api("/api/auth/password", "POST", { current_password: p.current_password, new_password: p.new_password }); e.target.reset(); status("密码已更新，请重新登录", false, "#password-status"); location.replace("/login"); }
    catch (error) { const messages = { "incorrect current password": "当前密码不正确", "too many password attempts": "尝试过于频繁，请稍后再试", "account changed; log in again": "账号已发生变化，请重新登录" }; throw new Error(messages[error.message] || error.message); }
    finally { button.disabled = false; }
  }, "#password-status"); });
  $("#logout-all").addEventListener("click", () => run(async () => { if (!confirm("退出所有设备上的登录会话？")) return; await api("/api/auth/logout-all", "POST"); location.replace("/login"); }));
  route(); run(async () => { await Promise.all([refresh(), loadAccount()]); });
  setInterval(() => { if (!document.hidden && !activeUploads.size && !$("#slide-dialog").open && ["overview", "jobs"].includes(activeTab())) run(refresh); }, 5000);
})();
