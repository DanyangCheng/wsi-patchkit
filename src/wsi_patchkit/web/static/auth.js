"use strict";
(() => {
  const originalFetch = window.fetch.bind(window);
  function expired() { location.replace("/login"); }
  const session = originalFetch("/api/auth/session", { cache: "no-store" })
    .then(async (response) => {
      if (response.status === 401) { expired(); throw new Error("登录已过期"); }
      if (!response.ok) throw new Error("无法获取登录状态");
      return response.json();
    });
  window.patchkitSession = session;
  window.fetch = async (input, init = {}) => {
    const state = await session;
    const request = new Request(input, init);
    if (new URL(request.url).origin === location.origin) {
      if (state.enabled && !["GET", "HEAD", "OPTIONS"].includes(request.method)) {
        request.headers.set("X-CSRF-Token", state.csrf_token);
      }
      const response = await originalFetch(request);
      if (state.enabled && response.status === 401) expired();
      return response;
    }
    return originalFetch(request);
  };
  session.then((state) => {
    if (!state.enabled) return;
    const header = document.querySelector("header");
    const headerActions = header?.querySelector("[data-header-actions]") || header;
    if (state.user?.is_admin && location.pathname !== "/admin/users") {
      const link = document.createElement("a");
      link.href = "/admin/users";
      link.textContent = "用户与授权";
      link.className = "toolbar-link";
      headerActions?.append(link);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "退出登录";
    button.title = `当前账号：${state.username}`;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await window.fetch("/api/auth/logout", { method: "POST" });
        if (!response.ok) throw new Error("退出失败");
        location.replace("/login");
      } catch (_) { button.disabled = false; alert("退出失败，请重试。"); }
    });
    const personalLink = header?.querySelector('a[href="/personal"]:not(.brand)');
    if (personalLink) {
      const actions = document.createElement("div");
      actions.className = "account-actions";
      actions.append(personalLink, button);
      header.append(actions);
    } else {
      headerActions?.append(button);
    }
  }).catch((error) => console.warn(error));
  // Revalidate sessions on history restoration and while the viewer is idle.
  // This also redirects after a server restart without waiting for an API call.
  async function checkSession() {
    try {
      const response = await originalFetch("/api/auth/session", { cache: "no-store" });
      if (response.status === 401) expired();
    } catch (_) { /* A temporary network failure does not imply logout. */ }
  }
  window.addEventListener("pageshow", checkSession);
  setInterval(checkSession, 60000);
})();
