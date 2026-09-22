// API 呼び出しの共通処理。URL と共通エラー処理をここに集約する。
const API_BASE = "/api";

export async function getJson(path) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    const msg = errorData?.detail || `API error: ${response.status} ${response.statusText}`;
    throw new Error(msg);
  }

  return response.json();
}

/**
 * POST（JSON 応答）を送信するヘルパー関数。停止 API などに使う。
 * @param {string} path APIパス
 * @param {object} [payload] 送信JSONボディ
 */
export async function postJson(path, payload = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    const msg = errorData?.detail || `API error: ${response.status} ${response.statusText}`;
    throw new Error(msg);
  }

  return response.json();
}

/**
 * POST による SSE ストリーミングを受信するヘルパー関数。
 * @param {string} path APIパス
 * @param {object} payload 送信JSONボディ
 * @param {(event: {event: string, data: any}) => void} onEvent イベント受信コールバック
 * @param {(error: Error) => void} onError エラーコールバック
 * @param {AbortSignal} [signal] 中断用シグナル
 */
export async function postSseStream(path, payload, onEvent, onError, signal) {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(payload),
      signal,
    });

    if (!response.ok) {
      const errJson = await response.json().catch(() => null);
      const msg = errJson?.detail || `エラー: HTTP ${response.status} ${response.statusText}`;
      throw new Error(msg);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      // sse-starlette は \r\n 区切りで送るため、\n 単独にも対応して分割する
      const blocks = buffer.split(/\r?\n\r?\n/);
      // 最後の要素は未完了ブロックの可能性があるためバッファに残す
      buffer = blocks.pop() || "";

      for (const block of blocks) {
        if (!block.trim()) continue;

        let eventType = "message";
        let dataStr = "";

        const lines = block.split("\n");
        for (const line of lines) {
          if (line.startsWith("event:")) {
            eventType = line.replace(/^event:\s*/, "").trim();
          } else if (line.startsWith("data:")) {
            dataStr += line.replace(/^data:\s*/, "").trim();
          }
        }

        if (dataStr) {
          try {
            const parsed = JSON.parse(dataStr);
            onEvent({ event: eventType, data: parsed });
          } catch {
            onEvent({ event: eventType, data: dataStr });
          }
        }
      }
    }
  } catch (err) {
    if (signal?.aborted) return;
    if (onError) {
      onError(err);
    } else {
      throw err;
    }
  }
}