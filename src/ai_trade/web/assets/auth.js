"use strict";

const form = document.getElementById("login-form");
const username = document.getElementById("username");
const password = document.getElementById("password");
const passwordToggle = document.getElementById("password-toggle");
const loginButton = document.getElementById("login-button");
const formMessage = document.getElementById("form-message");
const lockStorageKey = "ai-trade-auth-lock-until";

let lockTimer = 0;
let lockUntil = readStoredLock();

function setMessage(message, kind = "info", assertive = false, announce = true) {
  formMessage.dataset.kind = kind;
  formMessage.classList.add("is-visible");
  formMessage.setAttribute("aria-hidden", "false");
  formMessage.setAttribute("role", assertive ? "alert" : "status");
  formMessage.setAttribute(
    "aria-live",
    announce ? (assertive ? "assertive" : "polite") : "off",
  );
  formMessage.textContent = message;
}

function clearMessage() {
  formMessage.textContent = "";
  formMessage.removeAttribute("data-kind");
  formMessage.classList.remove("is-visible");
  formMessage.setAttribute("aria-hidden", "true");
  formMessage.setAttribute("role", "status");
  formMessage.setAttribute("aria-live", "polite");
}

function setBusy(busy) {
  form.setAttribute("aria-busy", String(busy));
  username.disabled = busy;
  password.disabled = busy;
  passwordToggle.disabled = busy;
  loginButton.disabled = busy;
  loginButton.textContent = busy ? "正在验证…" : "登录";
}

function setLocked(locked) {
  form.setAttribute("aria-busy", "false");
  username.disabled = locked;
  password.disabled = locked;
  passwordToggle.disabled = locked;
  loginButton.disabled = locked;
  loginButton.textContent = locked ? "暂时锁定" : "登录";
}

function markInvalid(field, invalid) {
  field.setAttribute("aria-invalid", String(invalid));
}

function validate() {
  const nameMissing = username.value.trim() === "";
  const passwordMissing = password.value === "";
  markInvalid(username, nameMissing);
  markInvalid(password, passwordMissing);

  if (nameMissing) {
    setMessage("请输入用户名。", "error", true);
    username.focus();
    return false;
  }
  if (passwordMissing) {
    setMessage("请输入密码。", "error", true);
    password.focus();
    return false;
  }
  return true;
}

function formatRetry(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes > 0) {
    return `${minutes} 分 ${String(remainder).padStart(2, "0")} 秒`;
  }
  return `${remainder} 秒`;
}

function updateLockState() {
  window.clearTimeout(lockTimer);
  const remaining = Math.max(0, Math.ceil((lockUntil - Date.now()) / 1000));
  if (remaining <= 0) {
    lockUntil = 0;
    storeLock(0);
    setLocked(false);
    setMessage("现在可以重新登录。", "info");
    username.focus();
    return;
  }

  setLocked(true);
  const announceLock = formMessage.dataset.kind !== "lock";
  setMessage(
    `登录尝试已暂时锁定。请在 ${formatRetry(remaining)}后重试。`,
    "lock",
    announceLock,
    announceLock,
  );
  lockTimer = window.setTimeout(updateLockState, 1000);
}

function applyLock(retryAfter) {
  const seconds = Math.min(86400, Math.max(1, Math.ceil(Number(retryAfter) || 60)));
  lockUntil = Date.now() + seconds * 1000;
  storeLock(lockUntil);
  updateLockState();
}

function readStoredLock() {
  try {
    const value = Number(window.sessionStorage.getItem(lockStorageKey));
    return Number.isFinite(value) && value > Date.now() ? value : 0;
  } catch {
    return 0;
  }
}

function storeLock(value) {
  try {
    if (value > 0) {
      window.sessionStorage.setItem(lockStorageKey, String(value));
    } else {
      window.sessionStorage.removeItem(lockStorageKey);
    }
  } catch {
    // Session storage may be unavailable under strict browser policies.
  }
}

async function readJson(response) {
  const raw = await response.text();
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

async function submitLogin(event) {
  event.preventDefault();
  if (lockUntil > Date.now()) {
    updateLockState();
    return;
  }
  if (!validate()) return;

  clearMessage();
  setBusy(true);
  setMessage("正在验证内测权限…", "info");

  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
      }),
    });
    const payload = await readJson(response);

    if (response.ok) {
      setMessage("登录成功，正在打开工作台…", "info");
      window.location.href = "/";
      return;
    }

    if (response.status === 429 || Number(payload.retry_after) > 0) {
      applyLock(payload.retry_after);
      return;
    }

    setBusy(false);
    const message =
      typeof payload.error === "string" && payload.error.trim()
        ? payload.error.trim()
        : "登录未完成。请检查用户名和密码后重试。";
    setMessage(message, "error", true);
    if (response.status === 401 || response.status === 403) {
      password.value = "";
      markInvalid(password, true);
      password.focus();
    } else {
      loginButton.focus();
    }
  } catch {
    setBusy(false);
    setMessage("无法连接本机登录服务。请确认工作台服务正在运行，然后重试。", "error", true);
    loginButton.focus();
  }
}

passwordToggle.addEventListener("click", () => {
  const show = password.type === "password";
  password.type = show ? "text" : "password";
  passwordToggle.textContent = show ? "隐藏密码" : "显示密码";
  passwordToggle.setAttribute("aria-pressed", String(show));
  password.focus();
});

username.addEventListener("input", () => markInvalid(username, false));
password.addEventListener("input", () => markInvalid(password, false));
form.addEventListener("submit", submitLogin);

if (lockUntil > Date.now()) updateLockState();
