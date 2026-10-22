"use strict";
window.addEventListener("DOMContentLoaded", () => {
  const bridge = window.patchkitCrop;
  if (!bridge) return;
  const $ = selector => document.querySelector(selector);
  const moreButton = $("#crop-jobs-more");
  let slideId = null, jobsLoading = false, jobs = [], nextCursor = null, expanded = false;
  const stateNames = {queued: "等待中", running: "裁剪中", completed: "已完成", failed: "失败", cancelled: "已取消"};
  const active = job => ["queued", "running"].includes(job.status);

  async function api(path, method = "GET") {
    const response = await fetch(path, {method, cache: "no-store"});
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(typeof result.detail === "string" ? result.detail : `请求失败（${response.status}）`);
      error.status = response.status; throw error;
    }
    return result;
  }
  async function run(callback) {
    try { await callback(); } catch (error) { bridge.status(error.message, "error"); }
  }
  function button(text, callback) {
    const node = document.createElement("button"); node.type = "button"; node.textContent = text;
    node.addEventListener("click", () => run(callback)); return node;
  }
  async function loadJobs(more = false) {
    const id = slideId;
    if (!id || jobsLoading || (more && !nextCursor)) return;
    jobsLoading = true; moreButton.disabled = true;
    try {
      const params = new URLSearchParams({slide_id: id, limit: "20", active_first: "true"});
      if (more) params.set("cursor", nextCursor);
      const data = await api(`/api/personal/jobs?${params}`);
      if (slideId !== id) return;
      // Refresh active rows outside the newest page, retaining earlier history.
      const incoming = new Set(data.items.map(job => job.job_id));
      const pending = !more ? jobs.filter(job => active(job) && !incoming.has(job.job_id)) : [];
      const updates = await Promise.all(pending.map(async job => {
        try { return await api(`/api/slides/${encodeURIComponent(id)}/crops/${job.job_id}`); }
        catch (error) { if (error.status === 404) return {job_id: job.job_id, removed: true}; throw error; }
      }));
      if (slideId !== id) return;
      const merged = new Map((more || expanded ? jobs : []).map(job => [job.job_id, job]));
      const overlaps = data.items.some(job => merged.has(job.job_id));
      for (const job of [...updates, ...data.items]) {
        if (job.removed) merged.delete(job.job_id);
        else merged.set(job.job_id, {...merged.get(job.job_id), ...job});
      }
      jobs = [...merged.values()].sort((a, b) => Number(active(b)) - Number(active(a)) || b.created_at - a.created_at || b.job_id.localeCompare(a.job_id));
      if (more || !expanded || !overlaps) nextCursor = data.next_cursor;
      if (more) expanded = true;
      const summary = document.createElement("p"); summary.className = "crop-hint";
      summary.textContent = Object.entries(stateNames).map(([status, name]) => `${name} ${data.counts[status] || 0}`).join(" · ");
      const list = $("#crop-job-list"), scrollTop = list.scrollTop;
      list.replaceChildren(summary, ...jobs.map(job => {
        const row = document.createElement("div"); row.className = "crop-record";
        const label = document.createElement("span"); label.textContent = `${stateNames[job.status]} · ${job.region.width} × ${job.region.height}`;
        label.title = `${job.filename}${job.error ? " · " + job.error : ""}`; row.append(label);
        if (job.status === "queued") row.append(button("取消", async () => { await api(`/api/personal/jobs/${job.job_id}/cancel`, "POST"); await loadJobs(); }));
        if (["failed", "cancelled"].includes(job.status)) row.append(button("重试", async () => { await api(`/api/personal/jobs/${job.job_id}/retry`, "POST"); await loadJobs(); }));
        return row;
      }));
      list.scrollTop = scrollTop;
      moreButton.hidden = !nextCursor;
    } finally {
      jobsLoading = false; moreButton.disabled = false;
      if (slideId !== id && slideId) run(loadJobs);
    }
  }
  function sync() {
    const id = bridge.selection().slide?.id || null;
    if (id !== slideId) {
      slideId = id; jobs = []; nextCursor = null; expanded = false;
      $("#crop-job-list").replaceChildren(); moreButton.hidden = true;
      if (id && bridge.selection().active) run(loadJobs);
    }
  }
  bridge.drawing = false;
  bridge.subscribe(sync);
  moreButton.addEventListener("click", () => run(() => loadJobs(true)));
  setInterval(() => { if (!document.hidden && bridge.selection().active) run(loadJobs); }, 3000);
  sync();
});
