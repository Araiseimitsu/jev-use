import { getJson, postJson, postSseStream } from "./api.js";
import { setMood, startMascot } from "./mascot.js";
import { startScene } from "./scene.js";

const $ = (selector) => document.querySelector(selector);
const systemStatus = $("#system-status");
const browser = $("#browser");
const form = $("#task-form");
const taskInput = $("#task-input");
const clearBtn = $("#clear-btn");
const runBtn = $("#run-btn");
const stopBtn = $("#stop-btn");
const headlessToggle = $("#headless-toggle");
const confirmNote = $("#confirm");
const formNote = $("#form-note");
const tabTitle = $("#tab-title");
const home = $("#home");
const homeEmpty = $("#home-empty");
const historyList = $("#history-list");
const run = $("#run");
const pageLine = $("#page-line");
const pause = $("#pause");
const humanCheck = $("#human-check");
const userInput = $("#user-input");
const humanReadyBtn = $("#human-ready-btn");
const preview = $("#preview");
const previewOpen = $("#preview-open");
const previewDialog = $("#preview-dialog");
const previewFull = $("#preview-full");
const previewClose = $("#preview-close");
const log = $("#log");
const steps = $("#steps");
const approval = $("#approval");
const approvalText = $("#approval-text");
const approveBtn = $("#approve-btn");
const denyBtn = $("#deny-btn");
const result = $("#result");
const resultText = $("#result-text");
const copyBtn = $("#copy-btn");
const newBtn = $("#new-btn");
const HISTORY_KEY = "jev-use:recent-tasks";
const HISTORY_LIMIT = 6;

let runId = "";
let acceptedTask = "";
let abortController = null;
// 設定を読めなかったときは画面の初期値に従う
let headless = headlessToggle.checked;
let recentTasks = loadRecentTasks();

function loadRecentTasks() {
  try {
    const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(saved)
      ? saved.filter((task) => typeof task === "string" && task.trim()).slice(0, HISTORY_LIMIT)
      : [];
  } catch {
    return [];
  }
}

// 文字列から色相を決め、履歴のサムネイルを依頼ごとに見分けやすくする
function hueOf(text) {
  let hash = 0;
  for (const ch of text) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash % 360;
}

function renderRecentTasks() {
  homeEmpty.hidden = recentTasks.length > 0;
  historyList.replaceChildren();
  for (const task of recentTasks) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.title = "この依頼を入力欄に入れる";
    const thumb = document.createElement("span");
    thumb.className = "thumb";
    thumb.style.setProperty("--h", hueOf(task));
    const text = document.createElement("span");
    text.className = "text";
    text.textContent = task;
    button.append(thumb, text);
    button.addEventListener("click", () => {
      taskInput.value = task;
      resetConfirmation();
      taskInput.focus();
    });
    item.append(button);
    historyList.append(item);
  }
}

function saveRecentTask(task) {
  recentTasks = [task, ...recentTasks.filter((saved) => saved !== task)].slice(0, HISTORY_LIMIT);
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(recentTasks));
  } catch {
    // 保存できない環境でも、現在の画面では履歴を使えるようにする。
  }
  renderRecentTasks();
}

// 文言から状態を決め、ステータス表示とマスコットの表情を切り替える
const STATUS_STATE = {
  "準備完了": ["ready", "idle"],
  "API キーが未設定です": ["warn", "error"],
  "接続できません": ["error", "error"],
  "実行中": ["running", "running"],
  "確認待ち": ["waiting", "waiting"],
  "入力待ち": ["waiting", "waiting"],
  "完了": ["done", "done"],
  "停止": ["error", "error"],
};

function setStatus(text, line) {
  const [state, mood] = STATUS_STATE[text] || ["ready", "idle"];
  systemStatus.dataset.state = state;
  systemStatus.textContent = text;
  browser.dataset.mode = state === "running" ? "running" : state;
  setMood(mood, line);
}

function showNote(message) {
  formNote.textContent = message;
  formNote.hidden = !message;
}

async function init() {
  try {
    const config = await getJson("/config");
    headless = Boolean(config.headless);
    headlessToggle.checked = headless;
    if (config.typesafe_enabled && config.text_enabled) {
      setStatus("準備完了");
    } else {
      setStatus("API キーが未設定です", "backend/.env に API キーを入れてください");
    }
  } catch {
    setStatus("接続できません", "サーバーが起動しているか確かめてください");
  }
}

headlessToggle.addEventListener("change", () => {
  headless = headlessToggle.checked;
});

// 指示が変わったら、前の指示への確認は使わない
function resetConfirmation() {
  clearBtn.hidden = !taskInput.value;
  acceptedTask = "";
  confirmNote.hidden = true;
  runBtn.textContent = "実行する";
}

taskInput.addEventListener("input", resetConfirmation);

// Enter で送信（IME 変換中と Shift+Enter は除外）
taskInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  form.requestSubmit();
});

clearBtn.addEventListener("click", () => {
  taskInput.value = "";
  resetConfirmation();
  taskInput.focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = taskInput.value.trim();
  if (!task || runBtn.disabled) return;
  showNote("");

  if (acceptedTask !== task) {
    runBtn.disabled = true;
    runBtn.textContent = "確認しています";
    setMood("thinking");
    try {
      const assessment = await postJson("/browser/assess", { task });
      if (assessment.requires_confirmation) {
        confirmNote.hidden = false;
        confirmNote.textContent = `${(assessment.reasons || []).join(" ")}（このまま Enter で実行）`;
        acceptedTask = task;
        runBtn.textContent = "確認して実行";
        setMood("waiting", "念のため確認してください");
        // ボタン無効化でフォーカスが外れても、続けて Enter で実行できるよう入力欄へ戻す
        runBtn.disabled = false;
        taskInput.focus();
        return;
      }
    } catch (error) {
      showNote(error.message);
      runBtn.textContent = "実行する";
      setMood("error");
      return;
    } finally {
      runBtn.disabled = false;
    }
  }

  await startRun(task);
});

async function startRun(task) {
  saveRecentTask(task);
  taskInput.value = "";
  resetConfirmation();
  document.body.classList.add("running");
  home.hidden = true;
  run.hidden = false;
  log.hidden = false;
  result.hidden = true;
  approval.hidden = true;
  pause.hidden = true;
  previewOpen.hidden = true;
  if (previewDialog.open) previewDialog.close();
  steps.replaceChildren();
  tabTitle.textContent = task;
  pageLine.textContent = "ページを開いています";
  runBtn.disabled = true;
  runBtn.textContent = "実行中";
  stopBtn.hidden = false;
  setStatus("実行中");

  abortController = new AbortController();
  await postSseStream(
    "/browser/run",
    { task, headless },
    onEvent,
    (error) => {
      showNote(error.message);
      if (!steps.childElementCount) backToHome();
      finish("準備完了");
    },
    abortController.signal,
  );
  finish(systemStatus.textContent === "実行中" ? "準備完了" : systemStatus.textContent);
}

function onEvent(event) {
  const data = event.data || {};
  if (event.event === "start") {
    runId = data.run_id || "";
    pageLine.textContent = data.start_url || "";
  } else if (event.event === "observation") {
    pageLine.textContent = data.url || "";
    if (data.title) tabTitle.textContent = data.title;
  } else if (event.event === "screenshot" && data.image) {
    preview.src = `data:image/jpeg;base64,${data.image}`;
    previewFull.src = preview.src;
    previewOpen.hidden = false;
  } else if (event.event === "step") {
    appendStep(data);
  } else if (event.event === "human_check") {
    showPause(humanCheck, data, "確認できた", "確認待ち");
  } else if (event.event === "user_input") {
    showPause(userInput, data, "続行", "入力待ち");
  } else if (event.event === "confirm_request") {
    approval.hidden = false;
    approvalText.textContent = data.reason || "この操作を実行しますか？";
    setStatus("確認待ち", "この操作、進めていいですか？");
  } else if (event.event === "complete") {
    showResult(data.result || "");
    setStatus("完了");
  } else if (event.event === "error") {
    const message = data.message || "実行できませんでした。";
    showResult(message);
    showNote(message);
    setStatus("停止");
  }
}

previewOpen.addEventListener("click", () => previewDialog.showModal());
previewClose.addEventListener("click", () => previewDialog.close());
previewDialog.addEventListener("click", (event) => {
  if (event.target === previewDialog) previewDialog.close();
});

function showResult(text) {
  result.hidden = false;
  resultText.textContent = text;
}

function appendStep(data) {
  const action = data.action || {};
  const jev = data.jev || {};
  const item = document.createElement("li");
  if (data.error) item.className = "failed";
  const title = document.createElement("strong");
  title.textContent = action.text
    ? `${action.description}（${action.text}）`
    : action.description || action.id || "操作";
  const meta = document.createElement("span");
  const bits = [];
  // 次の操作は Gemini が選び、選んだ理由を返す。Jev は危険度だけを判定する。
  if (jev.reason) bits.push(jev.reason);
  if (typeof jev.latency_ms === "number") bits.push(`${jev.latency_ms}ms`);
  if (data.error) bits.push(data.error);
  meta.textContent = bits.join("  ");
  item.append(title, meta);

  if (typeof jev.risk_probability === "number") {
    const percent = Math.round(jev.risk_probability * 100);
    const risk = document.createElement("span");
    risk.className = "risk";
    risk.dataset.level = percent >= 70 ? "high" : percent >= 35 ? "mid" : "low";
    risk.title = `危険度 ${percent}%`;
    risk.setAttribute("aria-label", risk.title);
    const bar = document.createElement("i");
    bar.style.width = `${Math.max(percent, 3)}%`;
    risk.append(bar);
    item.append(risk);
  }

  steps.append(item);
  item.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function showPause(messageEl, data, buttonLabel, waitingStatus) {
  const active = Boolean(data.active);
  pause.hidden = !active;
  humanCheck.hidden = true;
  userInput.hidden = true;
  messageEl.hidden = !active;
  messageEl.textContent = data.message || "";
  humanReadyBtn.textContent = buttonLabel;
  setStatus(active ? waitingStatus : "実行中");
}

function finish(status) {
  runBtn.disabled = false;
  runBtn.textContent = "実行する";
  stopBtn.hidden = true;
  approval.hidden = true;
  pause.hidden = true;
  humanReadyBtn.textContent = "確認できた";
  if (result.hidden && !steps.childElementCount) backToHome();
  if (systemStatus.textContent === "実行中") setStatus(status);
  abortController = null;
}

// 結果を閉じて、新しいタブの画面に戻す
function backToHome() {
  document.body.classList.remove("running");
  run.hidden = true;
  log.hidden = true;
  home.hidden = false;
  tabTitle.textContent = "新しいタブ";
  pageLine.textContent = "待機中";
}

newBtn.addEventListener("click", () => {
  backToHome();
  showNote("");
  if (!abortController) setStatus("準備完了");
  taskInput.focus();
});

// 停止要求を出してから受信を切る。受信を切っても、サーバー側はブラウザを閉じてから終わる。
stopBtn.addEventListener("click", async () => {
  if (runId) await postJson(`/browser/stop/${runId}`, {}).catch(() => {});
  abortController?.abort();
  showResult("停止しました。");
  setStatus("停止", "止めました");
});

humanReadyBtn.addEventListener("click", async () => {
  if (!runId) return;
  humanReadyBtn.disabled = true;
  try {
    await postJson(`/browser/human-ready/${runId}`, {});
  } catch (error) {
    showNote(error.message);
  } finally {
    humanReadyBtn.disabled = false;
  }
});

approveBtn.addEventListener("click", () => respondApproval(true));
denyBtn.addEventListener("click", () => respondApproval(false));

async function respondApproval(granted) {
  if (!runId) return;
  approveBtn.disabled = true;
  denyBtn.disabled = true;
  try {
    await postJson(`/browser/approve/${runId}`, { granted });
    approval.hidden = true;
    setStatus("実行中");
  } catch (error) {
    showNote(error.message);
  } finally {
    approveBtn.disabled = false;
    denyBtn.disabled = false;
  }
}

copyBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(resultText.textContent || "");
    copyBtn.textContent = "コピーしました";
    setTimeout(() => {
      copyBtn.textContent = "コピー";
    }, 1600);
  } catch {
    showNote("コピーできませんでした。");
  }
});

startScene();
startMascot();
renderRecentTasks();
init();
