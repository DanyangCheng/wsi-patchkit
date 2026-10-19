const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const registerSource = fs.readFileSync(path.join(__dirname, "../src/wsi_patchkit/web/static/register.js"), "utf8");
const usersSource = fs.readFileSync(path.join(__dirname, "../src/wsi_patchkit/web/static/users.js"), "utf8");

function registration(fetch) {
  let submit;
  const button = { disabled: false, textContent: "提交申请" };
  const form = { hidden: false, reset() { this.resetCalled = true; },
    addEventListener: (_, callback) => { submit = callback; }, querySelector: () => button };
  const fields = {
    "#register-form": form, "#error": { textContent: "" }, "#result": { textContent: "" },
    "#username": { value: "alice" }, "#password": { value: "a good long password" },
    "#password-confirm": { value: "a good long password" },
  };
  vm.runInNewContext(registerSource, {
    document: { querySelector: selector => fields[selector] },
    fetch, AbortController, setTimeout, clearTimeout,
  });
  return { fields, button, submit: () => submit({ preventDefault() {}, currentTarget: form }) };
}

test("registration submits confirmation, clears credentials and shows pending status", async () => {
  let payload;
  const page = registration(async (url, options) => {
    assert.equal(url, "/api/auth/register");
    payload = JSON.parse(options.body);
    return { status: 202 };
  });
  await page.submit();
  assert.equal(payload.username, "alice");
  assert.equal(payload.password_confirm, payload.password);
  assert.equal(page.fields["#register-form"].resetCalled, true);
  assert.equal(page.fields["#register-form"].hidden, true);
  assert.match(page.fields["#result"].textContent, /等待管理员审核/);
});

test("confirmation mismatch does not submit a registration", async () => {
  const page = registration(() => assert.fail("must not request"));
  page.fields["#password-confirm"].value = "different password";
  await page.submit();
  assert.match(page.fields["#error"].textContent, /不一致/);
  assert.equal(page.button.disabled, false);
});

test("duplicate registration is actionable and restores the form", async () => {
  const page = registration(async () => ({ status: 409 }));
  await page.submit();
  assert.match(page.fields["#error"].textContent, /已存在/);
  assert.equal(page.fields["#register-form"].hidden, false);
  assert.equal(page.button.disabled, false);
});

class Element {
  constructor(tag, text = "") {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.textContent = text;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll(tag) {
    return this.children.flatMap(child => child instanceof Element
      ? [...(child.tagName === tag ? [child] : []), ...child.querySelectorAll(tag)] : []);
  }
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("administrator approves a pending account with selected group IDs", async () => {
  const fields = { "#users": new Element("div"), "#status": new Element("p"), "#refresh": new Element("button") };
  const user = { id: "user-id", username: "alice", status: "pending", is_admin: false, group_ids: [], created_at: 1 };
  let update;
  vm.runInNewContext(usersSource, {
    document: { querySelector: selector => fields[selector], createElement: tag => new Element(tag),
      createTextNode: text => new Element("text", text) },
    fetch: async (url, options) => {
      if (options?.method === "PATCH") {
        assert.equal(url, "/api/users/user-id");
        update = JSON.parse(options.body);
        user.status = update.status;
        return { ok: true };
      }
      return { ok: true, json: async () => url === "/api/users"
        ? { users: [user] } : { groups: [{ id: "group-a", name: "Group A" }] } };
    },
  });
  await settle();
  fields["#users"].querySelectorAll("input")[0].checked = true;
  const approve = fields["#users"].querySelectorAll("button").find(button => button.textContent === "通过审核并保存授权");
  assert.ok(approve);
  await approve.listeners.click();
  await settle();
  assert.deepEqual(update, { status: "active", group_ids: ["group-a"] });
  assert.match(fields["#status"].textContent, /已保存 alice/);
});
