"use strict";
(() => {
  const container = document.querySelector("#users");
  const status = document.querySelector("#status");
  const refresh = document.querySelector("#refresh");
  const labels = { pending: "待审核", active: "已启用", rejected: "未通过", disabled: "已停用" };
  function notice(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
  }
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function card(user, groups) {
    const article = element("section", undefined, "account");
    const heading = element("div", undefined, "account-heading");
    heading.append(element("h2", user.username), element("span", user.is_admin ? "管理员" : labels[user.status], "badge"));
    article.append(heading);
    const checks = [];
    if (user.is_admin) {
      article.append(element("p", "管理员可访问全部共享文件组。"));
    } else {
      const fieldset = element("fieldset");
      fieldset.append(element("legend", "可访问的文件组"));
      const list = element("div", undefined, "groups");
      for (const group of groups) {
        const label = element("label");
        const input = element("input");
        input.type = "checkbox";
        input.value = group.id;
        input.checked = user.group_ids.includes(group.id);
        checks.push(input);
        label.append(input, document.createTextNode(group.name));
        list.append(label);
      }
      if (!groups.length) list.append(element("p", "暂无可分配的文件组。"));
      fieldset.append(list);
      article.append(fieldset);
    }
    const actions = element("div", undefined, "actions");
    const label = element("label", "账号状态 ");
    const select = element("select");
    for (const [value, text] of Object.entries(labels)) {
      const option = element("option", text);
      option.value = value;
      select.append(option);
    }
    select.value = user.status;
    label.append(select);
    actions.append(label);
    const save = element("button", "保存设置", "primary");
    save.type = "button";
    async function update(accountStatus) {
      article.querySelectorAll("button").forEach(button => { button.disabled = true; });
      try {
        const payload = { status: accountStatus };
        if (!user.is_admin) payload.group_ids = checks.filter(input => input.checked).map(input => input.value);
        const response = await fetch(`/api/users/${encodeURIComponent(user.id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
        if (!response.ok) {
          notice(response.status === 409 ? "无法保存：至少需要保留一个启用的管理员。"
            : "保存失败，请刷新后重试。", true);
          return;
        }
        await load();
        notice(`已保存 ${user.username} 的账号状态和权限。`);
      } catch (_) { notice("连接失败，请重试。", true); }
      finally { article.querySelectorAll("button").forEach(button => { button.disabled = false; }); }
    }
    if (user.status === "pending") {
      const approve = element("button", "通过审核并保存授权", "primary");
      approve.type = "button";
      approve.addEventListener("click", () => update("active"));
      const reject = element("button", "拒绝申请");
      reject.type = "button";
      reject.addEventListener("click", () => update("rejected"));
      actions.append(reject, approve);
    }
    save.addEventListener("click", () => update(select.value));
    actions.append(save);
    article.append(actions);
    return article;
  }
  async function load() {
    refresh.disabled = true;
    try {
      const [usersResponse, groupsResponse] = await Promise.all([fetch("/api/users"), fetch("/api/groups")]);
      if (!usersResponse.ok || !groupsResponse.ok) {
        notice(usersResponse.status === 403 ? "当前账号没有管理权限。" : "读取失败，请重试。", true);
        return;
      }
      const [{ users }, { groups }] = await Promise.all([usersResponse.json(), groupsResponse.json()]);
      users.sort((a, b) => Number(b.status === "pending") - Number(a.status === "pending") || a.created_at - b.created_at);
      container.replaceChildren(...users.map(user => card(user, groups)));
      notice(`${users.length} 个账号，${users.filter(user => user.status === "pending").length} 个待审核申请。`);
    } catch (_) { notice("连接失败，请重试。", true); }
    finally { refresh.disabled = false; }
  }
  refresh.addEventListener("click", load);
  load();
})();
