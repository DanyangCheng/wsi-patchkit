"use strict";
document.querySelector("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const error = document.querySelector("#error");
  const password = document.querySelector("#password").value;
  const confirmation = document.querySelector("#password-confirm").value;
  error.textContent = "";
  if (password !== confirmation) {
    error.textContent = "两次输入的密码不一致。";
    return;
  }
  button.disabled = true;
  button.textContent = "正在提交…";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch("/api/auth/register", {
      method: "POST", signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.querySelector("#username").value,
        password, password_confirm: confirmation,
      }),
    });
    if (response.status === 202) {
      form.reset();
      form.hidden = true;
      document.querySelector("#result").textContent = "注册申请已提交，请等待管理员审核。审核通过后即可登录。";
      return;
    }
    error.textContent = response.status === 409 ? "该账号已存在，请更换账号或返回登录。"
      : response.status === 429 ? "申请过于频繁，请一分钟后再试。"
      : response.status === 426 ? "当前服务要求 HTTPS，请通过 HTTPS 地址访问。"
      : response.status === 403 ? "请求来源验证失败，请刷新页面或联系管理员。"
      : "提交失败，请检查账号格式、密码长度及确认密码。";
  } catch (failure) {
    error.textContent = failure.name === "AbortError"
      ? "提交超时，请稍后重试。" : "连接失败，请检查网络后重试。";
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
    button.textContent = "提交申请";
  }
});
