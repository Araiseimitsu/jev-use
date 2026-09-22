import { getJson, postJson, postSseStream } from "./api.js";

const systemStatus = document.querySelector("#system-status");
const form = document.querySelector("#task-form");
const taskInput = document.querySelector("#task-input");
const runBtn = document.querySelector("#run-btn");
const stopBtn = document.querySelector("#stop-btn");
const confirm = document.querySelector("#confirm");
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

let runId = "";
let acceptedTask = "";
let abortController = null;

async function init() {
  try {
    const config = await getJson("/config");
    systemStatus.textContent = config.typesafe_enabled ? "準備完了" : "API キーが未設定です";
  } catch {
    systemStatus.textContent = "接続できません";
  }
}

form.querySelector(".presets").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button?.dataset.preset) return;
  taskInput.value = button.dataset.preset;
  acceptedTask = "";
  confirm.hidden = true;
  runBtn.textContent = "実行する";
  taskInput.focus();
});

taskInput.addEventListener("input", () => {
  acceptedTask = "";
  confirm.hidden = true;
  runBtn.textContent = "実行する";
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
        confirm.hidden = false;
        confirm.textContent = (assessment.reasons || []).join(" ");
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
  acceptedTask = "";
  confirm.hidden = true;
  run.hidden = false;
  result.hidden = true;
  approval.hidden = true;
  preview.hidden = true;
  steps.replaceChildren();
  pageLine.textContent = "ページを開いています";
  runBtn.disabled = true;
  runBtn.textContent = "実行中";
  stopBtn.hidden = false;
  systemStatus.textContent = "実行中";

  abortController = new AbortController();
  await postSseStream(
    "/browser/run",
    { task },
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
  } else if (event.event === "complete") {
    showResult(data.result || "");
    systemStatus.textContent = "完了";
  } else if (event.event === "error") {
    const message = data.message || "実行できませんでした。";
    showResult(message);
    formNote.textContent = message;
    systemStatus.textContent = "停止";
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
  systemStatus.textContent = active ? waitingStatus : "実行中";
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
  if (systemStatus.textContent === "実行中") systemStatus.textContent = status;
  abortController = null;
}

stopBtn.addEventListener("click", async () => {
  if (runId) await postJson(`/browser/stop/${runId}`, {}).catch(() => {});
  abortController?.abort();
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
      copyBtn.textContent = "結果をコピー";
    }, 1600);
  } catch {
    formNote.textContent = "コピーできませんでした。";
  }
});

init();
