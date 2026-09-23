import { getJson, postJson, postSseStream } from "./api.js";

const systemStatus = document.querySelector("#system-status");
const form = document.querySelector("#task-form");
const taskInput = document.querySelector("#task-input");
const clearBtn = document.querySelector("#clear-btn");
const runBtn = document.querySelector("#run-btn");
const stopBtn = document.querySelector("#stop-btn");
const headlessToggle = document.querySelector("#headless-toggle");
const confirmNote = document.querySelector("#confirm");
const formNote = document.querySelector("#form-note");
const run = document.querySelector("#run");
const pageLine = document.querySelector("#page-line");
const humanCheck = document.querySelector("#human-check");
const userInput = document.querySelector("#user-input");
const humanReadyBtn = document.querySelector("#human-ready-btn");
const preview = document.querySelector("#preview");
const steps = document.querySelector("#steps");
const approval = document.querySelector("#approval");
const approvalText = document.querySelector("#approval-text");
const approveBtn = document.querySelector("#approve-btn");
const denyBtn = document.querySelector("#deny-btn");
const result = document.querySelector("#result");
const resultText = document.querySelector("#result-text");
const copyBtn = document.querySelector("#copy-btn");
const recentHistory = document.querySelector("#recent-history");
const historyList = document.querySelector("#history-list");
const HISTORY_KEY = "jev-use:recent-tasks";
const HISTORY_LIMIT = 5;

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

function renderRecentTasks() {
  recentHistory.hidden = recentTasks.length === 0;
  historyList.replaceChildren();
  for (const task of recentTasks) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = task;
    button.addEventListener("click", () => {
      taskInput.value = task;
      resetConfirmation();
      recentHistory.open = false;
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

// 文言から状態を決め、ステータスドットの色・動きを切り替える
const STATUS_STATE = {
  "準備完了": "ready",
  "API キーが未設定です": "warn",
  "接続できません": "error",
  "実行中": "running",
  "確認待ち": "waiting",
  "入力待ち": "waiting",
  "完了": "done",
  "停止": "error",
};

function setStatus(text) {
  systemStatus.dataset.state = STATUS_STATE[text] || "ready";
  systemStatus.textContent = text;
}

async function init() {
  try {
    const config = await getJson("/config");
    headless = Boolean(config.headless);
    headlessToggle.checked = headless;
    setStatus(config.typesafe_enabled ? "準備完了" : "API キーが未設定です");
  } catch {
    setStatus("接続できません");
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
  formNote.textContent = "";

  if (acceptedTask !== task) {
    runBtn.disabled = true;
    runBtn.textContent = "確認しています";
    try {
      const assessment = await postJson("/browser/assess", { task });
      if (assessment.requires_confirmation) {
        confirmNote.hidden = false;
        confirmNote.textContent = (assessment.reasons || []).join(" ");
        acceptedTask = task;
        runBtn.textContent = "確認して実行";
        return;
      }
    } catch (error) {
      formNote.textContent = error.message;
      runBtn.textContent = "実行する";
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
  run.hidden = false;
  result.hidden = true;
  approval.hidden = true;
  preview.hidden = true;
  steps.replaceChildren();
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
      formNote.textContent = error.message;
      if (!steps.childElementCount) run.hidden = true;
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
    pageLine.textContent = data.title ? `${data.title} — ${data.url}` : data.url || "";
  } else if (event.event === "screenshot" && data.image) {
    preview.src = `data:image/jpeg;base64,${data.image}`;
    preview.hidden = false;
  } else if (event.event === "step") {
    appendStep(data);
  } else if (event.event === "human_check") {
    showPause(humanCheck, data, "確認できた", "確認待ち");
  } else if (event.event === "user_input") {
    showPause(userInput, data, "続行", "入力待ち");
  } else if (event.event === "confirm_request") {
    approval.hidden = false;
    approvalText.textContent = data.reason || "この操作を実行しますか？";
    setStatus("確認待ち");
  } else if (event.event === "complete") {
    showResult(data.result || "");
    setStatus("完了");
  } else if (event.event === "error") {
    const message = data.message || "実行できませんでした。";
    showResult(message);
    formNote.textContent = message;
    setStatus("停止");
  }
}

function showResult(text) {
  result.hidden = false;
  resultText.textContent = text;
  result.scrollIntoView({ block: "nearest" });
}

function appendStep(data) {
  const action = data.action || {};
  const jev = data.jev || {};
  const item = document.createElement("li");
  const title = document.createElement("strong");
  title.textContent = action.text
    ? `${action.description}（${action.text}）`
    : action.description || action.id || "操作";
  const meta = document.createElement("span");
  const bits = [];
  if (typeof jev.confidence === "number") bits.push(`確信度 ${Math.round(jev.confidence * 100)}%`);
  if (typeof jev.latency_ms === "number") bits.push(`${jev.latency_ms}ms`);
  if (data.error) bits.push(data.error);
  meta.textContent = bits.join("  ");
  item.append(title, meta);
  steps.append(item);
}

function showPause(messageEl, data, buttonLabel, waitingStatus) {
  const active = Boolean(data.active);
  messageEl.hidden = !active;
  messageEl.textContent = data.message || "";
  humanReadyBtn.hidden = !active;
  humanReadyBtn.textContent = buttonLabel;
  setStatus(active ? waitingStatus : "実行中");
}

function finish(status) {
  runBtn.disabled = false;
  runBtn.textContent = "実行する";
  stopBtn.hidden = true;
  approval.hidden = true;
  humanCheck.hidden = true;
  userInput.hidden = true;
  humanReadyBtn.hidden = true;
  humanReadyBtn.textContent = "確認できた";
  if (result.hidden && !steps.childElementCount) run.hidden = true;
  if (systemStatus.textContent === "実行中") setStatus(status);
  abortController = null;
}

// 停止要求を出してから受信を切る。受信を切っても、サーバー側はブラウザを閉じてから終わる。
stopBtn.addEventListener("click", async () => {
  if (runId) await postJson(`/browser/stop/${runId}`, {}).catch(() => {});
  abortController?.abort();
  showResult("停止しました。");
  setStatus("停止");
});

humanReadyBtn.addEventListener("click", async () => {
  if (!runId) return;
  humanReadyBtn.disabled = true;
  try {
    await postJson(`/browser/human-ready/${runId}`, {});
  } catch (error) {
    formNote.textContent = error.message;
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
    formNote.textContent = error.message;
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
    formNote.textContent = "コピーできませんでした。";
  }
});

renderRecentTasks();
init();
