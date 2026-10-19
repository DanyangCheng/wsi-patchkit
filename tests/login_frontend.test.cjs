const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/wsi_patchkit/web/static/login.js"), "utf8");

function page(fetch, protocol = "https:") {
  let submit;
  let destination;
  const button = { disabled: false, textContent: "登录" };
  const error = { textContent: "" };
  const form = {
    addEventListener: (name, callback) => { submit = callback; },
    querySelector: () => button,
  };
  const fields = {
    "#login-form": form, "#error": error,
    "#username": { value: "admin" }, "#password": { value: "test password" },
  };
  vm.runInNewContext(source, {
    document: { querySelector: (selector) => fields[selector] },
    location: { protocol, replace: (url) => { destination = url; } },
    fetch, AbortController, setTimeout, clearTimeout,
  });
  return { button, error, destination: () => destination,
    submit: () => submit({ preventDefault() {}, currentTarget: form }) };
}

test("submit sends credentials and verifies cookie before navigating", async () => {
  const requests = [];
  const screen = page(async (url, options) => {
    requests.push({ url, options });
    return { ok: true };
  });
  const pending = screen.submit();
  assert.equal(screen.button.disabled, true);
  assert.equal(screen.button.textContent, "正在登录…");
  await pending;
  assert.equal(requests[0].url, "/api/auth/login");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(JSON.parse(requests[0].options.body).username, "admin");
  assert.equal(requests[1].url, "/api/auth/session");
  assert.equal(screen.destination(), "/");
});

test("HTTP cookie failure displays an actionable message", async () => {
  let calls = 0;
  const screen = page(async () => ({ ok: ++calls === 1 }), "http:");
  await screen.submit();
  assert.match(screen.error.textContent, /PATCHKIT_COOKIE_SECURE=false/);
  assert.equal(screen.destination(), undefined);
  assert.equal(screen.button.disabled, false);
});

test("aborted request displays timeout and restores submit button", async () => {
  const screen = page(async () => {
    const error = new Error("timeout");
    error.name = "AbortError";
    throw error;
  });
  await screen.submit();
  assert.match(screen.error.textContent, /超时/);
  assert.equal(screen.button.disabled, false);
  assert.equal(screen.button.textContent, "登录");
});

test("HTTPS requirement is explained before cookie creation", async () => {
  const screen = page(async () => ({ ok: false, status: 426 }), "http:");
  await screen.submit();
  assert.match(screen.error.textContent, /HTTPS/);
  assert.equal(screen.button.disabled, false);
  assert.equal(screen.destination(), undefined);
});

test("pending account displays review status without navigation", async () => {
  const screen = page(async () => ({ ok: false, status: 403, json: async () => ({ detail: "account_pending" }) }));
  await screen.submit();
  assert.match(screen.error.textContent, /等待管理员审核/);
  assert.equal(screen.destination(), undefined);
  assert.equal(screen.button.disabled, false);
});
