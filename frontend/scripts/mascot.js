// マスコット Jev。状態に合わせて表情とセリフを変え、目はマウスの方を向く。

const jev = document.querySelector("#jev");
const bubble = document.querySelector("#jev-bubble");
const eyes = document.querySelector("#jev-eyes");

const LINES = {
  idle: ["何を調べましょう？", "いつでもどうぞ", "ブラウザの準備はできています"],
  thinking: ["依頼を確かめています…"],
  running: ["ページを読んでいます", "次の操作を考え中…", "がんばってます"],
  waiting: ["ちょっと手伝ってください"],
  done: ["できました！"],
  error: ["うまくいきませんでした"],
};

export function setMood(mood, line) {
  jev.dataset.mood = mood;
  const candidates = LINES[mood] || LINES.idle;
  const text = line || candidates[Math.floor(Math.random() * candidates.length)];
  if (bubble.textContent === text) return;
  bubble.textContent = text;
  // 同じ要素のアニメーションをやり直し、セリフが変わったことを見せる
  bubble.style.animation = "none";
  void bubble.offsetWidth;
  bubble.style.animation = "";
}

export function startMascot() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let frame = 0;
  window.addEventListener("pointermove", (event) => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const box = jev.getBoundingClientRect();
      const dx = event.clientX - (box.left + box.width / 2);
      const dy = event.clientY - (box.top + box.height / 2);
      const distance = Math.hypot(dx, dy) || 1;
      const reach = Math.min(distance / 40, 4);
      eyes.style.translate = `${((dx / distance) * reach).toFixed(1)}px ${((dy / distance) * reach).toFixed(1)}px`;
    });
  });
}
