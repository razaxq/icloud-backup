"use strict";

const $ = (id) => document.getElementById(id);

const ACTIVE = new Set(["running", "waiting_password", "waiting_code", "cancelling", "pausing"]);
const PAUSABLE = new Set(["running", "waiting_password", "waiting_code"]);
const SETTINGS_SECTIONS = ["notify", "about"];
const MAX_ACCOUNTS = 10;
const statusLabels = {
  idle: "就绪",
  running: "下载中",
  waiting_password: "等待账户密码",
  waiting_code: "等待验证码",
  cancelling: "正在取消",
  pausing: "正在暂停",
  paused: "已暂停",
  success: "下载完成",
  partial: "完成（有提醒）",
  error: "未能完成",
  cancelled: "已取消",
  interrupted: "已中断",
  unverified: "旧版未校验"
};
const triggerLabels = { manual: "手动", schedule: "定时", resume: "自动继续", watch: "实时" };
const triggerContext = { manual: "手动开始", schedule: "定时运行", resume: "自动继续", watch: "实时检测" };
const statusTones = {
  running: "tone-live", cancelling: "tone-live", pausing: "tone-live", paused: "tone-warn", success: "tone-ok",
  waiting_password: "tone-warn", waiting_code: "tone-warn", partial: "tone-warn",
  interrupted: "tone-warn", unverified: "tone-warn", error: "tone-err"
};
const channelLabels = {
  webhook: "通用 Webhook", bark: "Bark", ntfy: "ntfy", telegram: "Telegram 机器人", smtp: "邮件（SMTP）"
};
const eventLabels = { auth: "需要验证", error: "未能完成", partial: "完成但有提醒", success: "每次成功" };
const SVG_NS = "http://www.w3.org/2000/svg";
const verification = {
  waiting_password: {
    title: "登录 Apple 账户",
    prompt: "本次下载需要验证 Apple 账户。请在此输入该账户的密码。",
    label: "Apple 账户密码",
    type: "password",
    autocomplete: "current-password",
    inputMode: "text"
  },
  waiting_code: {
    title: "Apple 双重认证",
    prompt: "请查看 Apple 受信任设备，输入收到的验证码。",
    label: "验证码",
    type: "text",
    autocomplete: "one-time-code",
    inputMode: "text"
  }
};
const SECRET_FIELDS = ["webhook_url", "bark_url", "ntfy_url", "ntfy_token", "telegram_token", "smtp_password"];
const NOTIFY_TEXT_FIELDS = ["telegram_chat", "smtp_host", "smtp_user", "smtp_from", "smtp_to"];
const EVENTS = ["auth", "error", "partial", "success"];

let state = null;
let busy = false;
let refreshTimer = null;
let requestVersion = 0;
let challengeId = null;
let editing = null;
let suggestDestination = false;
let notifyDirty = false;
let noticeTimer;
let renderedAccounts = "";
let renderedHistory = "";
let renderedLog = [];
let browseState = { path: "", parent: "" };
let browseRequest = 0;
let pendingDelete = null;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function radioValue(name) {
  const checked = document.querySelector(`input[name="${name}"]:checked`);
  return checked ? checked.value : "";
}

function setRadio(name, value) {
  for (const input of document.getElementsByName(name)) input.checked = input.value === value;
}

function showNotice(message, kind = "error") {
  clearTimeout(noticeTimer);
  const notice = $("notice");
  notice.textContent = message;
  notice.dataset.kind = kind;
  notice.hidden = !message;
  if (message && kind === "ok") {
    noticeTimer = setTimeout(() => showNotice(""), 4000);
  }
}

function hidePrivateState() {
  state = null;
  closeBrowser();
  suggestDestination = notifyDirty = false;
  renderedAccounts = renderedHistory = "";
  renderedLog = [];
  $("dashboard").hidden = true;
  $("settings-view").hidden = true;
  $("open-settings").hidden = true;
  $("add-account").hidden = true;
  setPauseMenu(false);
  $("challenge-value").value = "";
  $("account-form").reset();
  $("notify-form").reset();
  $("account-list").replaceChildren();
  $("log").replaceChildren();
  $("hero-title").textContent = "";
  $("hero-sub").textContent = "";
  $("challenge-account").textContent = "";
  challengeId = null;
}

async function api(name, payload) {
  const post = payload !== undefined;
  const headers = { "X-Requested-With": "XMLHttpRequest" };
  if (post) headers["Content-Type"] = "application/json";
  const response = await fetch("./api/" + name, {
    method: post ? "POST" : "GET",
    mode: "same-origin",
    headers,
    credentials: "same-origin",
    cache: "no-store",
    body: post ? JSON.stringify({ ...payload, _csrf: state?.csrf_token }) : undefined
  });
  let result = {};
  try {
    result = await response.json();
  } catch {
    if (response.ok) throw new Error("无法读取应用数据，请从飞牛桌面重新打开应用。");
  }
  if (response.status === 401 || (response.status === 403 && name === "state")) {
    hidePrivateState();
    throw new Error(result.error || "请使用飞牛管理员账户重新打开应用。");
  }
  if (!response.ok) {
    const error = new Error(result.error || "请求失败（HTTP " + response.status + "）。");
    error.code = result.code;
    throw error;
  }
  return result;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleString("zh-CN", {
    year: sameYear ? undefined : "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false
  });
}

function intervalLabel(minutes) {
  if (!minutes) return "仅手动";
  if (minutes < 60) return "每 " + minutes + " 分钟";
  const hours = minutes / 60;
  if (hours === 24) return "每天";
  if (hours % 24 === 0) return "每 " + hours / 24 + " 天";
  return "每 " + hours + " 小时";
}

function statusChip(status, text) {
  return el("span", { class: "chip " + (statusTones[status] || "") }, el("i", { class: "dot" }),
    text || statusLabels[status] || status);
}

function elapsedLabel(value) {
  const start = new Date(value).getTime();
  if (Number.isNaN(start)) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - start) / 60000));
  if (minutes < 1) return "刚刚开始";
  if (minutes < 60) return "已运行 " + minutes + " 分钟";
  return "已运行 " + Math.floor(minutes / 60) + " 小时 " + (minutes % 60) + " 分钟";
}

function edgeTime(values, pick) {
  const times = values.filter(Boolean).map((value) => new Date(value)).filter((date) => !Number.isNaN(date.getTime()));
  return times.length ? new Date(pick(...times.map((date) => date.getTime()))).toISOString() : null;
}

/* ---------- summary, challenge and log ---------- */

function renderSummary() {
  const job = state.job || {};
  const status = job.status || "idle";
  const label = statusLabels[status] || "正在处理";
  const accounts = state.accounts || [];
  const active = ACTIVE.has(status);
  const paused = status === "paused";
  $("job-status").textContent = label;
  $("status-dot").dataset.status = status;
  $("job-chip").className = "chip " + (statusTones[status] || "");

  const context = [];
  if (status !== "idle" && job.trigger) {
    context.push(triggerContext[job.trigger] || job.trigger);
    context.push(job.full_scan ? "完整扫描" : "快速检查");
  }
  if (active && job.started_at) context.push(elapsedLabel(job.started_at));
  $("job-context").textContent = context.filter(Boolean).join(" · ");

  let title;
  let sub = job.message || "";
  if (status === "idle") {
    title = accounts.length ? "照片归档已就绪" : "添加 Apple 账户以开始";
    sub = accounts.length ? "" : "原图、视频与实况照片会以 Apple 提供的原始格式下载到 NAS。";
  } else if (verification[status]) {
    title = "等待验证 · " + (job.account || "Apple 账户");
    sub = "Apple 要求验证此账户，完成验证后继续下载。";
  } else if (status === "running" || status === "cancelling" || status === "pausing") {
    title = { running: "正在归档 ", cancelling: "正在取消 · ", pausing: "正在暂停 · " }[status] + (job.account || "");
  } else if (paused) {
    title = "已暂停 · " + (job.account || "");
    sub = (job.pause_until ? "将于 " + formatTime(job.pause_until) + " 自动继续。" : "点击「继续」后接着下载。")
      + "已下载的文件和未完成的传输都会保留，其他账户的计划任务也会等到继续或取消后再运行。";
  } else {
    title = label + (job.account ? " · " + job.account : "");
  }
  $("hero-title").textContent = title;
  $("hero-sub").textContent = sub;
  $("job-bar").hidden = !(status === "running" || status === "cancelling" || status === "pausing");

  const counting = active || paused || (status !== "idle" && Number.isInteger(job.downloaded));
  $("stat-first-label").textContent = counting ? (active || paused ? "本次新增" : "上次任务新增") : "账户";
  $("stat-first-icon").setAttribute("href", counting ? "#i-download" : "#i-users");
  $("stat-first").textContent = counting ? String(job.downloaded || 0) : accounts.length + " / " + MAX_ACCOUNTS;

  const next = edgeTime(accounts.map((item) => item.next_run), Math.min);
  const last = edgeTime(accounts.map((item) => item.last_success), Math.max);
  $("next-run").textContent = next ? formatTime(next) : "尚未安排";
  $("last-success").textContent = last ? formatTime(last) : "暂无记录";
  $("version").textContent = state.version ? "v" + state.version : "";
  const pausable = PAUSABLE.has(status);
  $("hero-actions").hidden = !(active || paused);
  $("resume").hidden = !paused;
  $("resume").disabled = busy;
  $("pause-wrap").hidden = !(pausable || status === "pausing");
  $("pause").disabled = busy || !pausable;
  $("pause-label").textContent = status === "pausing" ? "正在暂停…" : "暂停";
  if (!pausable || busy) setPauseMenu(false);
  $("cancel").hidden = !(active || paused) || status === "pausing";
  $("cancel").disabled = busy || status === "cancelling";
  $("cancel").textContent = status === "cancelling" ? "正在取消…" : "取消任务";
  updateAddButton();
}

function updateAddButton() {
  // Unavailable while the editor is open: it would replace the account being edited.
  const count = state ? (state.accounts || []).length : 0;
  $("add-account").disabled = busy || !state || count >= MAX_ACCOUNTS || !$("editor").hidden;
}

function renderChallenge(job) {
  const challenge = verification[job.status];
  const promptId = challenge ? job.prompt_id : null;
  if (challengeId !== promptId) {
    $("challenge-value").value = "";
    challengeId = promptId;
  }
  const appearing = Boolean(challenge) && $("challenge").hidden;
  $("challenge").hidden = !challenge;
  if (!challenge) return;
  if (appearing) {
    $("challenge").scrollIntoView({ block: "nearest" });
    setTimeout(() => $("challenge-value").focus({ preventScroll: true }), 0);
  }
  $("challenge-chip").textContent = statusLabels[job.status];
  $("challenge-title").textContent = challenge.title;
  $("challenge-account").textContent = job.account || "";
  $("challenge-prompt").textContent = job.message || challenge.prompt;
  $("challenge-note").textContent = location.protocol === "https:"
    ? "密码和验证码不会写入运行记录。"
    : "当前连接使用 HTTP。请通过飞牛的 HTTPS 地址打开应用，再输入验证信息。";
  $("challenge-label").textContent = challenge.label;
  const input = $("challenge-value");
  input.type = challenge.type;
  input.autocomplete = challenge.autocomplete;
  input.inputMode = challenge.inputMode;
  input.disabled = busy;
  $("challenge-form").querySelector("button").disabled = busy;
}

function logLine(text) {
  const match = /^(\d{2}:\d{2}:\d{2})\s+([\s\S]*)$/.exec(text);
  return el("div", { class: "log-line" },
    match ? el("time", {}, match[1]) : null,
    el("span", {}, match ? match[2] : text));
}

function patchLog(log, previous, lines) {
  // The server keeps a bounded buffer: lines drop off the front, new ones are
  // appended, and download progress rewrites the last line. Find how many old
  // lines dropped off so that everything but the old last line still lines up.
  const aligned = (drop) => {
    const keep = previous.length - drop - 1;
    if (keep > lines.length) return false;
    for (let i = 0; i < keep; i++) if (previous[drop + i] !== lines[i]) return false;
    return true;
  };
  if (!previous.length || log.children.length !== previous.length) {
    log.replaceChildren(...lines.map(logLine));
    return;
  }
  let drop = 0;
  while (!aligned(drop)) drop++;  // terminates: drop = length - 1 always aligns
  for (let i = 0; i < drop; i++) log.firstElementChild.remove();
  const keep = previous.length - drop - 1;
  const oldLast = log.children[keep];
  if (keep >= lines.length) oldLast.remove();
  else if (previous[previous.length - 1] !== lines[keep]) oldLast.replaceWith(logLine(lines[keep]));
  log.append(...lines.slice(keep + 1).map(logLine));
}

function renderLog(job, logs) {
  const log = $("log");
  const lines = Array.isArray(logs) ? logs.map(String) : [];
  if (lines.length !== renderedLog.length || lines.some((line, i) => line !== renderedLog[i])) {
    const nearBottom = log.hidden || log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    patchLog(log, renderedLog, lines);
    renderedLog = lines;
    log.hidden = !lines.length;
    if (nearBottom) log.scrollTop = log.scrollHeight;
  }
  log.hidden = !lines.length;
  $("empty-log").hidden = lines.length > 0;
  $("log-live").hidden = !ACTIVE.has(job.status);
  const update = $("last-update");
  update.textContent = job.updated_at ? "更新于 " + formatTime(job.updated_at) : "";
  update.hidden = !update.textContent;
}

/* ---------- accounts ---------- */

function accountCard(account, job, index) {
  const held = ACTIVE.has(job.status) || job.status === "paused";
  const active = held;
  const runningHere = held && job.account_id === account.id;
  const pausedHere = runningHere && job.status === "paused";
  const last = account.last_run;
  const confirming = pendingDelete === account.id;
  const cleanup = account.cleanup || {};
  const retentionLabel = { immediate: "预览全部已备份项目", days: "预览保留最近 " + cleanup.value + " 天",
    latest: "预览保留最新 " + cleanup.value + " 项" }[cleanup.mode];
  const initial = [...String(account.account).trim()][0] || "?";
  return el("article", { class: "account" + (runningHere ? " is-running" : "") },
    el("div", { class: "account-main" },
      el("span", { class: "avatar av-" + (index % 4), "aria-hidden": "true" }, initial),
      el("div", { class: "account-body" },
        el("div", { class: "account-title" },
          el("strong", {}, account.account),
          el("span", { class: "chip" }, account.domain === "cn" ? "中国大陆" : "全球"),
          runningHere ? statusChip(job.status) : (last ? statusChip(last.status) : null),
          account.resume_pending && !runningHere ? el("span", { class: "chip tone-warn" }, "待继续") : null,
          account.needs_full_scan && !runningHere && !account.resume_pending
            ? el("span", { class: "chip tone-warn" }, "待完整检查") : null),
        el("div", { class: "account-path", title: account.destination },
          icon("folder"), el("span", {}, account.destination)),
        el("div", { class: "account-meta" },
          retentionLabel ? el("span", {}, retentionLabel) : null, retentionLabel ? el("i") : null,
          el("span", {}, intervalLabel(account.interval_minutes)), el("i"),
          el("span", {}, "上次成功 " + (account.last_success ? formatTime(account.last_success) : "暂无")), el("i"),
          el("span", {}, "下次 " + (account.next_run ? formatTime(account.next_run) : "未安排"))),
        last && last.message && last.status !== "success" && !runningHere
          ? el("p", { class: "account-note" }, last.message) : null)),
    el("div", { class: "account-actions" },
      el("button", {
        type: "button", class: "btn ghost sm", disabled: busy || active, onclick: () => startAccount(account.id),
        title: held && !runningHere ? (job.status === "paused" ? "请先继续或取消已暂停的任务" : "其他账户的任务正在运行") : ""
      }, icon(pausedHere ? "pause" : "play"),
      runningHere ? (pausedHere ? "已暂停" : "备份中…") : "立即备份"),
      el("button", {
        type: "button", class: "btn ghost sm icon-only", title: "编辑", "aria-label": "编辑 " + account.account,
        disabled: busy || runningHere, onclick: () => openEditor(account)
      }, icon("edit")),
      confirming
        ? el("button", {
          type: "button", class: "btn sm confirming", disabled: busy || runningHere, onclick: () => deleteAccount(account.id)
        }, "确认删除")
        : el("button", {
          type: "button", class: "btn ghost sm icon-only", title: "删除", "aria-label": "删除 " + account.account,
          disabled: busy || runningHere, onclick: () => deleteAccount(account.id)
        }, icon("trash"))));
}

function renderAccounts() {
  const accounts = state.accounts || [];
  const job = state.job || {};
  const key = JSON.stringify([accounts, job.status, job.account_id, busy, pendingDelete]);
  $("account-count").textContent = accounts.length ? accounts.length + " / " + MAX_ACCOUNTS : "";
  $("no-accounts").hidden = accounts.length > 0 || !$("editor").hidden;
  if (key === renderedAccounts) return;
  renderedAccounts = key;
  $("account-list").replaceChildren(...accounts.map((account, index) => accountCard(account, job, index)));
}

function updateSuggestedDestination() {
  if (!suggestDestination) return;
  const root = state?.default_dir || "";
  const name = [...$("account").value.trim().normalize("NFC").toLowerCase()
    .replace(/[^\p{L}\p{M}\p{N}@.+_-]/gu, "_")].slice(0, 48).join("");
  $("destination").value = root && name
    ? root.replace(/\/$/, "") + "/账户-" + name + "-" + radioValue("domain") : "";
}

function resetSharedDestination() {
  $("shared-destination").hidden = true;
  $("allow-shared-destination").checked = false;
}

function openEditor(account) {
  editing = account ? account.id : null;
  $("editor-title").textContent = account ? "编辑账户" : "添加账户";
  $("account").value = account ? account.account : "";
  setRadio("domain", account ? account.domain : "com");
  $("destination").value = account ? account.destination : (state.default_dir || "");
  $("interval").value = String(account ? account.interval_minutes : 60);
  $("include-shared").checked = account ? account.include_shared : true;
  const cleanup = account?.cleanup || { mode: "off", value: 0, shared: false };
  $("cleanup-mode").value = cleanup.mode;
  $("cleanup-value").value = cleanup.value || "";
  $("cleanup-shared").checked = cleanup.shared;
  updateCleanupFields();
  suggestDestination = !account && state.accounts.length > 0;
  updateSuggestedDestination();
  resetSharedDestination();
  closeBrowser();
  updatePathHelp();
  $("editor").hidden = false;
  $("accounts-card").hidden = true;
  $("no-accounts").hidden = true;
  updateAddButton();
  $("editor-title").scrollIntoView({ block: "nearest" });
  $("account").focus({ preventScroll: true });
}

function closeEditor() {
  editing = null;
  suggestDestination = false;
  resetSharedDestination();
  closeBrowser();
  $("editor").hidden = true;
  $("accounts-card").hidden = false;
  $("no-accounts").hidden = Boolean(state && (state.accounts || []).length);
  updateAddButton();
}

function updatePathHelp() {
  const roots = state?.roots || [];
  $("destination").placeholder = state?.default_dir || roots[0] || "输入 NAS 上的完整路径";
  $("account-fields").disabled = busy;
}

function updateCleanupFields() {
  const mode = $("cleanup-mode").value;
  const numeric = mode === "days" || mode === "latest";
  $("cleanup-amount").hidden = !numeric;
  $("cleanup-value").disabled = !numeric;
  $("cleanup-label").textContent = mode === "days" ? "保留最近多少天" : "保留最新多少项";
  if (numeric && !$("cleanup-value").value) $("cleanup-value").value = mode === "days" ? 30 : 100;
  $("cleanup-details").hidden = mode === "off";
  $("cleanup-shared-label").hidden = !$("include-shared").checked;
  if (mode === "off" || !$("include-shared").checked) $("cleanup-shared").checked = false;
}

function editorPayload() {
  const payload = {
    account: $("account").value.trim(),
    domain: radioValue("domain"),
    destination: $("destination").value.trim(),
    interval_minutes: Number($("interval").value),
    include_shared: $("include-shared").checked,
    cleanup: { mode: $("cleanup-mode").value,
      value: $("cleanup-value").disabled ? 0 : $("cleanup-value").valueAsNumber,
      shared: $("cleanup-shared").checked },
    allow_shared_destination: !$("shared-destination").hidden && $("allow-shared-destination").checked
  };
  if (editing) payload.id = editing;
  return payload;
}

function editorValid() {
  return $("account").reportValidity() && $("destination").reportValidity() && $("cleanup-value").reportValidity();
}

async function saveAccount() {
  let result;
  try {
    result = await api("accounts/save", editorPayload());
  } catch (error) {
    if (error.code === "destination_in_use") {
      $("shared-destination").hidden = false;
      $("shared-destination").scrollIntoView({ block: "nearest" });
    }
    throw error;
  }
  closeEditor();
  return result.id;
}

function startAccount(id) {
  act(() => api("start", { id }));
}

function deleteAccount(id) {
  if (pendingDelete !== id) {
    pendingDelete = id;
    renderAccounts();
    setTimeout(() => {
      if (pendingDelete === id) {
        pendingDelete = null;
        if (state) renderAccounts();
      }
    }, 4000);
    return;
  }
  pendingDelete = null;
  act(async () => {
    await api("accounts/delete", { id });
    if (editing === id) closeEditor();
    showNotice("已删除该账户及其登录会话，已下载的照片仍保留在原目录。", "ok");
  });
}

/* ---------- folder browser ---------- */

async function openBrowser(path) {
  const request = ++browseRequest;
  $("browse-pick").disabled = true;
  try {
    const listing = await api("browse" + (path ? "?path=" + encodeURIComponent(path) : ""));
    if (request === browseRequest) renderBrowser(listing);
  } catch (error) {
    if (request !== browseRequest) return;
    if (path) return openBrowser("");
    closeBrowser();
    showNotice(error.message);
  }
}

function renderBrowser(listing) {
  browseState = { path: listing.path, parent: listing.parent || "" };
  $("browser").hidden = false;
  $("browse-path").textContent = listing.path || "已授权给本应用的目录";
  $("browse-up").hidden = !listing.path;
  $("browse-pick").disabled = !listing.path;
  const items = listing.entries.map((entry) => el("li", {},
    el("button", { type: "button", class: "folder", onclick: () => openBrowser(entry.path) },
      icon("folder"), el("span", {}, entry.name), icon("right"))));
  if (listing.truncated) items.push(el("li", { class: "hint" }, "仅显示前 500 个文件夹。"));
  $("browse-list").replaceChildren(...items);
  $("browse-empty").hidden = listing.entries.length > 0;
}

function closeBrowser() {
  ++browseRequest;
  $("browser").hidden = true;
  $("browse-list").replaceChildren();
}

/* ---------- history ---------- */

function renderHistory(history) {
  const items = Array.isArray(history) ? history : [];
  const key = JSON.stringify(items);
  if (key === renderedHistory) return;
  renderedHistory = key;
  $("history-wrap").hidden = !items.length;
  $("no-history").hidden = items.length > 0;
  $("history").tBodies[0].replaceChildren(...items.map((item) => el("tr", {},
    el("td", {}, formatTime(item.finished_at)),
    el("td", { class: "ellipsis", title: item.account }, item.account),
    el("td", { class: "mode" }, (triggerLabels[item.trigger] || "—") + " · " + (item.full_scan ? "完整" : "快速")),
    el("td", { title: item.message || "" }, statusChip(item.status)),
    el("td", { class: "num" }, (item.downloaded_estimate ? "≥" : "") + String(item.downloaded ?? 0)),
    el("td", { class: "num" }, (item.downloaded_estimate && item.removed ? "≥" : "") + String(item.removed ?? 0)))));
}

/* ---------- notifications ---------- */

function updateChannelFields() {
  const channel = $("channel").value;
  document.querySelectorAll("[data-channel]").forEach((node) => {
    node.hidden = node.dataset.channel !== channel;
  });
  $("notify-events").hidden = channel === "none";
  $("notify-test").hidden = channel === "none";
  const secrets = state?.notify?.secrets || {};
  $("secret-help").hidden = !Object.values(secrets).some(Boolean);
}

function renderNotify(notify) {
  const last = notify.last;
  const lastChip = $("notify-last");
  lastChip.hidden = !last;
  if (last) {
    lastChip.className = "chip " + (last.ok ? "tone-ok" : "tone-err");
    lastChip.textContent = (last.ok ? "上次通知已送达" : "上次通知失败：" + last.message) + " · " + formatTime(last.at);
  }
  const channelName = channelLabels[notify.channel];
  const events = EVENTS.filter((event) => notify.events?.[event]).map((event) => eventLabels[event]);
  $("notify-summary").textContent = !channelName
    ? "当前未开启"
    : "当前：" + channelName + " · " + (events.length ? events.join("、") + "时提醒" : "未选择提醒事件");
  if (notifyDirty) return;
  $("channel").value = notify.channel;
  for (const field of NOTIFY_TEXT_FIELDS) $(field).value = notify[field] || "";
  $("smtp_port").value = notify.smtp_port || 465;
  $("smtp_security").value = notify.smtp_security || "ssl";
  for (const field of SECRET_FIELDS) {
    const input = $(field);
    input.value = "";
    const preview = notify.previews?.[field];
    input.placeholder = notify.secrets?.[field]
      ? "已设置" + (preview ? "（" + preview + "）" : "") + "，留空保持不变"
      : input.dataset.placeholder || "";
  }
  for (const box of document.querySelectorAll("[data-clear-secret]")) {
    box.checked = false;
    box.closest("label").hidden = !notify.secrets?.[box.dataset.clearSecret];
  }
  for (const event of EVENTS) {
    document.querySelector(`[data-event="${event}"]`).checked = Boolean(notify.events?.[event]);
  }
  updateChannelFields();
}

async function saveNotify() {
  const channel = $("channel").value;
  const payload = { channel, events: {}, clear: channel === "none" ? SECRET_FIELDS : [] };
  for (const event of EVENTS) payload.events[event] = document.querySelector(`[data-event="${event}"]`).checked;
  const group = document.querySelector(`[data-channel="${channel}"]`);
  if (group) {
    for (const input of group.querySelectorAll("input[id], select[id]")) {
      payload[input.id] = input.type === "number" ? Number(input.value) : input.value.trim();
    }
    for (const box of group.querySelectorAll("[data-clear-secret]:checked")) {
      payload.clear.push(box.dataset.clearSecret);
      payload[box.dataset.clearSecret] = "";
    }
  }
  await api("notify/save", payload);
  notifyDirty = false;
  for (const field of SECRET_FIELDS) $(field).value = "";
}

/* ---------- settings ---------- */

function renderSettings() {
  $("about-version").textContent = state.version ? "v" + state.version : "—";
  $("about-downloader").textContent = state.downloader || "—";
  $("about-roots").textContent = (state.roots || []).join("\n") || "—";
}

function setPauseMenu(open) {
  $("pause-menu").hidden = !open;
  $("pause").setAttribute("aria-expanded", String(open));
}

function currentView() {
  const match = /^#settings(?:\/([a-z]+))?$/.exec(location.hash);
  if (!match) return { view: "dashboard" };
  return { view: "settings", section: SETTINGS_SECTIONS.includes(match[1]) ? match[1] : SETTINGS_SECTIONS[0] };
}

function applyView() {
  const { view, section } = currentView();
  const ready = Boolean(state);
  $("dashboard").hidden = !ready || view !== "dashboard";
  $("settings-view").hidden = !ready || view !== "settings";
  $("open-settings").setAttribute("aria-pressed", String(view === "settings"));
  $("add-account").hidden = !ready || view === "settings";
  for (const link of document.querySelectorAll(".settings-nav a")) {
    if (link.dataset.section === section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll(".settings-panel")) panel.hidden = panel.dataset.section !== section;
  const job = state?.job || {};
  const waiting = view === "settings" && Boolean(verification[job.status]);
  $("settings-alert").hidden = !waiting;
  if (waiting) $("settings-alert-text").textContent = (job.account || "Apple 账户") + " 正在等待验证，完成后才能继续下载。";
}

/* ---------- refresh loop ---------- */

function render(snapshot) {
  if (snapshot.auth !== "ok") {
    hidePrivateState();
    throw new Error("请使用飞牛管理员账户重新打开应用。");
  }
  state = snapshot;
  $("open-settings").hidden = false;
  applyView();
  const job = snapshot.job || {};
  renderSummary();
  renderChallenge(job);
  renderAccounts();
  renderLog(job, snapshot.logs);
  renderHistory(snapshot.history);
  renderNotify(snapshot.notify || {});
  renderSettings();
  updatePathHelp();
  $("notify-fields").disabled = busy;
}

async function refresh() {
  const version = ++requestVersion;
  try {
    const snapshot = await api("state");
    if (version !== requestVersion) return;
    render(snapshot);
    if ($("notice").dataset.kind === "connection") showNotice("");
  } catch (error) {
    if (version === requestVersion) showNotice(error.message, "connection");
  } finally {
    if (version === requestVersion) $("connecting").hidden = true;
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    if (!busy && !document.hidden) await refresh();
    scheduleRefresh();
  }, 2500);
}

async function act(task) {
  if (busy) return;
  busy = true;
  ++requestVersion;
  clearTimeout(refreshTimer);
  if (state) render(state);
  showNotice("");
  try {
    await task();
  } catch (error) {
    showNotice(error.message);
  } finally {
    busy = false;
    await refresh();
    scheduleRefresh();
  }
}

/* ---------- events ---------- */

for (const field of SECRET_FIELDS) $(field).dataset.placeholder = $(field).placeholder;

$("add-account").addEventListener("click", () => openEditor(null));
for (const id of ["editor-close", "editor-back"]) {
  $(id).addEventListener("click", () => {
    closeEditor();
    $("add-account").focus({ preventScroll: true });
  });
}
$("open-settings").addEventListener("click", () => {
  location.hash = currentView().view === "settings" ? "" : "#settings";
});
$("settings-back").addEventListener("click", () => { location.hash = ""; });
$("settings-alert").querySelector("a").addEventListener("click", (event) => {
  event.preventDefault();
  location.hash = "";
});
window.addEventListener("hashchange", () => {
  applyView();
  window.scrollTo(0, 0);
});
$("cancel").addEventListener("click", () => act(() => api("cancel", {})));
$("resume").addEventListener("click", () => act(() => api("resume", {})));
$("pause").addEventListener("click", (event) => {
  event.stopPropagation();
  const open = $("pause-menu").hidden;
  setPauseMenu(open);
  if (open) $("pause-menu").querySelector("button").focus();
});
for (const item of $("pause-menu").querySelectorAll("[data-minutes]")) {
  item.addEventListener("click", () => {
    setPauseMenu(false);
    act(() => api("pause", { minutes: Number(item.dataset.minutes) }));
  });
}
$("pause-menu").addEventListener("keydown", (event) => {
  const items = [...$("pause-menu").querySelectorAll("button")];
  const index = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(index + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
  }
});
document.addEventListener("click", (event) => {
  if (!$("pause-wrap").contains(event.target)) setPauseMenu(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("pause-menu").hidden) {
    setPauseMenu(false);
    $("pause").focus();
  }
});
$("account").addEventListener("input", () => { resetSharedDestination(); updateSuggestedDestination(); });
for (const input of document.getElementsByName("domain")) {
  input.addEventListener("change", () => { resetSharedDestination(); updateSuggestedDestination(); });
}
$("destination").addEventListener("input", () => { suggestDestination = false; resetSharedDestination(); });
$("cleanup-mode").addEventListener("change", updateCleanupFields);
$("include-shared").addEventListener("change", updateCleanupFields);
$("account-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!editorValid()) return;
  act(async () => {
    await saveAccount();
    showNotice("账户设置已保存。", "ok");
  });
});
$("save-start").addEventListener("click", () => {
  if (!editorValid()) return;
  act(async () => {
    const id = await saveAccount();
    await api("start", { id });
  });
});
$("browse").addEventListener("click", () => openBrowser($("destination").value.trim()));
$("browse-up").addEventListener("click", () => openBrowser(browseState.parent));
$("browse-close").addEventListener("click", closeBrowser);
$("browse-pick").addEventListener("click", () => {
  $("destination").value = browseState.path;
  suggestDestination = false;
  resetSharedDestination();
  closeBrowser();
  $("destination").focus();
});

$("notify-form").addEventListener("input", () => { notifyDirty = true; });
$("channel").addEventListener("change", updateChannelFields);
$("notify-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    await saveNotify();
    $("notify-status").textContent = "通知设置已保存";
  });
});
$("notify-test").addEventListener("click", () => {
  act(async () => {
    if (notifyDirty) await saveNotify();
    $("notify-status").textContent = "正在发送…";
    try {
      await api("notify/test", {});
      $("notify-status").textContent = "测试通知已发送，请检查是否收到。";
    } catch (error) {
      $("notify-status").textContent = "";
      throw error;
    }
  });
});

$("challenge-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("challenge-value").value;
  const promptId = challengeId;
  $("challenge-value").value = "";
  act(() => api("input", { value, prompt_id: promptId }));
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !busy) refresh();
});
refresh().finally(scheduleRefresh);
