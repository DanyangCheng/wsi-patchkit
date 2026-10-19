"use strict";
document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  const error = document.querySelector("#error");
  button.disabled = true;
  button.textContent = "正在登录…";
  error.textContent = "";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch("/api/auth/login", {
      signal: controller.signal,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.querySelector("#username").value,
        password: document.querySelector("#password").value,
      }),
    });
    if (response.ok) {
      const session = await fetch("/api/auth/session", {
        cache: "no-store", signal: controller.signal,
      });
      if (session.ok) { location.replace("/"); return; }
      error.textContent = location.protocol === "http:"
        ? "登录会话未生效。请使用 HTTPS；内网 HTTP 测试需设置 PATCHKIT_COOKIE_SECURE=false 并重启服务。"
        : "登录会话未生效，请检查浏览器 Cookie 设置。";
      return;
    }
    if (response.status === 403) {
      const result = await response.json();
      if (result.detail === "account_pending") {
        error.textContent = "账号正在等待管理员审核，请审核通过后再登录。";
        return;
      }
      if (result.detail === "account_rejected") {
        error.textContent = "注册申请未通过审核，请联系管理员。";
        return;
      }
    }
    error.textContent = response.status === 429
      ? "登录尝试过于频繁，请一分钟后重试。"
      : [409, 426].includes(response.status) ? "当前服务要求 HTTPS，请通过 HTTPS 地址访问。"
      : response.status === 403 ? "登录请求被拒绝，请检查反向代理的域名和 HTTPS 配置。"
      : "登录失败，请检查账号和密码。";
  } catch (failure) {
    error.textContent = failure.name === "AbortError"
      ? "登录请求超时，请检查服务状态后重试。" : "连接失败，请检查网络后重试。";
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
    button.textContent = "登录";
  }
});
