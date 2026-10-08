// 背景の空と挨拶。実際の時刻に合わせて時間帯・太陽や月の位置・一言を切り替える。

const greeting = document.querySelector("#greeting");
const greetingSub = document.querySelector("#greeting-sub");
const themeMeta = document.querySelector('meta[name="theme-color"]');
const skyBody = document.querySelector("#sky-body");
const townWindows = document.querySelector("#town-windows");

// 言葉は当日中ずっと同じものが選ばれ、日付が変わると別の候補から選ばれる。
const PERIODS = [
  {
    key: "night",
    ranges: [[19, 24], [0, 5]],
    meta: "#070b24",
    hello: ["こんばんは", "まだ起きてるんですね", "静かな時間ですね", "今日もおつかれさまでした"],
    lines: [
      "深追いはほどほどに、目は大事に。",
      "眠る前の調べものは、明日の自分への手紙。",
      "夜のほうが進む、という人も多いですね。",
    ],
  },
  {
    key: "morning",
    ranges: [[5, 11]],
    meta: "#7cc6f0",
    hello: ["おはようございます", "朝から元気ですね", "いい朝ですね"],
    lines: ["今日もいい一日を。", "最初の一歩は今日もうまくいくはず。", "コーヒーかお茶か、それも大事な選択。"],
  },
  {
    key: "day",
    ranges: [[11, 17]],
    meta: "#6cc0ef",
    hello: ["こんにちは", "おつかれさまです", "お昼すぎですね"],
    lines: ["詰め込みすぎに注意。", "そろそろ水を一杯飲む時間です。", "背筋を伸ばすと調べものもはかどります（体感）。"],
  },
  {
    key: "evening",
    ranges: [[17, 19]],
    meta: "#3d4f8f",
    hello: ["おつかれさまです", "いい夕方ですね", "そろそろ帰りの時間"],
    lines: ["夕飯はちゃんと食べますよね？", "今日やれた分だけで、今日は満点。", "日中のノイズを捨てる時間です。"],
  },
];

// 日付ごとに異なる候補を選ぶだけの軽いハッシュ
function daySeed(now) {
  const key = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}`;
  let hash = 0;
  for (const ch of key) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash;
}

function periodOf(hour) {
  return PERIODS.find(({ ranges }) => ranges.some(([start, end]) => hour >= start && hour < end));
}

// 太陽は 5〜19 時、月は 19〜翌 5 時に、画面の左から右へ弧を描いて進む
function placeSkyBody(now) {
  const hours = now.getHours() + now.getMinutes() / 60;
  const isDay = hours >= 5 && hours < 19;
  const progress = isDay ? (hours - 5) / 14 : ((hours - 19 + 24) % 24) / 10;
  const x = 8 + progress * 84;
  const y = 62 - Math.sin(Math.PI * progress) * 48;
  const root = document.documentElement.style;
  root.setProperty("--body-x", `${x.toFixed(1)}%`);
  root.setProperty("--body-y", `${y.toFixed(1)}%`);
  skyBody.dataset.kind = isDay ? "sun" : "moon";
}

// ビルの窓を並べる。夕方以降に CSS で明かりが点く
function buildWindows() {
  const svgNs = "http://www.w3.org/2000/svg";
  for (const rect of document.querySelectorAll(".town rect")) {
    const x = Number(rect.getAttribute("x"));
    const y = Number(rect.getAttribute("y"));
    const width = Number(rect.getAttribute("width"));
    for (let row = y + 12; row < 236; row += 18) {
      for (let col = x + 7; col < x + width - 8; col += 13) {
        // 全部点くと不自然なので、位置から決まる一部だけ窓にする
        if ((row * 7 + col * 3) % 5 === 0) continue;
        const win = document.createElementNS(svgNs, "rect");
        win.setAttribute("x", col);
        win.setAttribute("y", row);
        win.setAttribute("width", 6);
        win.setAttribute("height", 8);
        townWindows.append(win);
      }
    }
  }
}

let currentPeriod = null;

function applyTime() {
  const now = new Date();
  const period = periodOf(now.getHours());
  if (period.key !== currentPeriod) {
    document.documentElement.dataset.time = period.key;
    themeMeta.setAttribute("content", period.meta);
    currentPeriod = period.key;
  }
  placeSkyBody(now);

  const date = now.toLocaleDateString("ja-JP", { month: "long", day: "numeric", weekday: "short" });
  const pick = (candidates, offset) => candidates[(daySeed(now) + offset) % candidates.length];
  greeting.textContent = `${date}　${pick(period.hello, 0)}`;
  greetingSub.textContent = pick(period.lines, 1);
}

export function startScene() {
  buildWindows();
  applyTime();
  // 1分ごとに太陽の位置と時間帯を更新する
  setInterval(applyTime, 60_000);
}
