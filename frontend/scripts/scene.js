// 背景の空と挨拶。実際の時刻に合わせて時間帯・太陽や月の位置・月の満ち欠け・一言を切り替える。
// 星・雲・街並みは起動時に生成し、毎回同じ形になるよう固定の乱数で作る。

const greeting = document.querySelector("#greeting");
const greetingSub = document.querySelector("#greeting-sub");
const themeMeta = document.querySelector('meta[name="theme-color"]');
const skyBody = document.querySelector("#sky-body");
const starCanvas = document.querySelector("#stars");
const moonCanvas = document.querySelector("#moon");
const cloudLayer = document.querySelector("#clouds");
const cloudFilters = document.querySelector("#cloud-filters");
const skyline = document.querySelector("#skyline");

const SVG_NS = "http://www.w3.org/2000/svg";

// 言葉は当日中ずっと同じものが選ばれ、日付が変わると別の候補から選ばれる。
const PERIODS = [
  {
    key: "night",
    ranges: [[19, 24], [0, 5]],
    meta: "#050a1c",
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
    meta: "#4f86c4",
    hello: ["おはようございます", "朝から元気ですね", "いい朝ですね"],
    lines: ["今日もいい一日を。", "最初の一歩は今日もうまくいくはず。", "コーヒーかお茶か、それも大事な選択。"],
  },
  {
    key: "day",
    ranges: [[11, 17]],
    meta: "#2f6fb8",
    hello: ["こんにちは", "おつかれさまです", "お昼すぎですね"],
    lines: ["詰め込みすぎに注意。", "そろそろ水を一杯飲む時間です。", "背筋を伸ばすと調べものもはかどります（体感）。"],
  },
  {
    key: "evening",
    ranges: [[17, 19]],
    meta: "#2a2f5c",
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

// 再読み込みしても星や街並みが変わらないよう、種から決まる乱数を使う
function seededRandom(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
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
  const y = 66 - Math.sin(Math.PI * progress) * 52;
  const root = document.documentElement.style;
  root.setProperty("--body-x", `${x.toFixed(1)}%`);
  root.setProperty("--body-y", `${y.toFixed(1)}%`);
  skyBody.dataset.kind = isDay ? "sun" : "moon";
}

// ---------- 星 ----------

function drawStars() {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = window.innerWidth;
  const height = window.innerHeight;
  starCanvas.width = Math.round(width * ratio);
  starCanvas.height = Math.round(height * ratio);
  const ctx = starCanvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const random = seededRandom(20261008);
  const count = Math.round((width * height) / 2600);
  for (let i = 0; i < count; i += 1) {
    const x = random() * width;
    // 地平線に近いほど大気で見えにくくなるので、上ほど多く明るくする
    const y = Math.pow(random(), 1.6) * height * 0.78;
    const fade = 1 - y / (height * 0.8);
    const size = random() < 0.04 ? 1.3 + random() * 0.6 : 0.35 + random() * 0.75;
    const alpha = (0.25 + random() * 0.75) * fade;
    // わずかに青白い星と黄みの星を混ぜる
    const tint = random();
    const color = tint < 0.15 ? "200,215,255" : tint > 0.9 ? "255,232,200" : "255,255,255";
    ctx.fillStyle = `rgba(${color},${alpha.toFixed(2)})`;
    ctx.beginPath();
    ctx.arc(x, y, size, 0, Math.PI * 2);
    ctx.fill();
    if (size > 1.3) {
      const glow = ctx.createRadialGradient(x, y, 0, x, y, size * 4);
      glow.addColorStop(0, `rgba(${color},${(alpha * 0.35).toFixed(2)})`);
      glow.addColorStop(1, `rgba(${color},0)`);
      ctx.fillStyle = glow;
      ctx.fillRect(x - size * 4, y - size * 4, size * 8, size * 8);
    }
  }
}

// ---------- 月 ----------

// 2000-01-06 18:14 UTC の新月を基準に、朔望月の周期から月齢の割合（0 = 新月、0.5 = 満月）を出す
function moonPhase(now) {
  const synodic = 29.530588853;
  const days = (now.getTime() - Date.UTC(2000, 0, 6, 18, 14)) / 86_400_000;
  return (((days / synodic) % 1) + 1) % 1;
}

function drawMoon(now) {
  const ctx = moonCanvas.getContext("2d");
  const size = moonCanvas.width;
  const r = size / 2 - 2;
  const c = size / 2;
  ctx.clearRect(0, 0, size, size);

  // 縁ほど暗くなる明るさの分布
  const base = ctx.createRadialGradient(c - r * 0.25, c - r * 0.25, r * 0.1, c, c, r);
  base.addColorStop(0, "#f7f5ee");
  base.addColorStop(0.7, "#e4e0d4");
  base.addColorStop(1, "#bdb8aa");
  ctx.fillStyle = base;
  ctx.beginPath();
  ctx.arc(c, c, r, 0, Math.PI * 2);
  ctx.fill();

  // 海（暗い模様）とクレーター。おおまかに地球から見た配置に寄せる
  ctx.save();
  ctx.beginPath();
  ctx.arc(c, c, r, 0, Math.PI * 2);
  ctx.clip();
  ctx.filter = `blur(${(size / 60).toFixed(1)}px)`;
  const maria = [
    [-0.28, -0.32, 0.26, 0.2, 0.3],
    [0.05, -0.38, 0.2, 0.16, 0.26],
    [0.3, -0.1, 0.18, 0.22, 0.24],
    [-0.42, 0.05, 0.22, 0.3, 0.22],
    [-0.08, 0.1, 0.2, 0.16, 0.2],
    [0.18, 0.32, 0.16, 0.12, 0.18],
    [-0.3, 0.42, 0.14, 0.1, 0.16],
  ];
  for (const [dx, dy, rx, ry, alpha] of maria) {
    ctx.fillStyle = `rgba(110,108,100,${alpha})`;
    ctx.beginPath();
    ctx.ellipse(c + dx * r, c + dy * r, rx * r, ry * r, 0.4, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.filter = "none";
  const random = seededRandom(7);
  for (let i = 0; i < 26; i += 1) {
    const angle = random() * Math.PI * 2;
    const dist = Math.sqrt(random()) * r * 0.9;
    const cr = r * (0.015 + random() * 0.04);
    const x = c + Math.cos(angle) * dist;
    const y = c + Math.sin(angle) * dist;
    ctx.fillStyle = "rgba(90,88,80,0.18)";
    ctx.beginPath();
    ctx.arc(x, y, cr, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,250,0.22)";
    ctx.beginPath();
    ctx.arc(x - cr * 0.3, y - cr * 0.3, cr * 0.6, 0, Math.PI * 2);
    ctx.fill();
  }
  // 南の大きなクレーター（ティコ）と光条
  ctx.fillStyle = "rgba(255,255,250,0.5)";
  ctx.beginPath();
  ctx.arc(c - r * 0.08, c + r * 0.62, r * 0.05, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  // 欠けている側を消す。地球照の分だけわずかに残す
  const phase = moonPhase(now);
  const waxing = phase < 0.5;
  const q = waxing ? phase : 1 - phase;
  const k = Math.cos(2 * Math.PI * q);
  ctx.save();
  ctx.globalCompositeOperation = "destination-out";
  ctx.filter = `blur(${(size / 90).toFixed(1)}px)`;
  ctx.fillStyle = "rgba(0,0,0,0.96)";
  ctx.translate(c, c);
  // 北半球では満ちていく月の右側が光る。欠けていく月は左右を反転して描く
  if (!waxing) ctx.scale(-1, 1);
  ctx.beginPath();
  ctx.arc(0, 0, r + 2, -Math.PI / 2, Math.PI / 2, true);
  ctx.ellipse(0, 0, Math.abs(k) * (r + 2), r + 2, 0, Math.PI / 2, -Math.PI / 2, k > 0);
  ctx.fill();
  ctx.restore();
}

// ---------- 雲 ----------

// 雲は輪郭をフラクタルノイズで崩した楕円を3層重ね、光の当たる面と影を作る
const CLOUDS = [
  { top: 9, scale: 1, speed: 260, delay: -40 },
  { top: 22, scale: 0.7, speed: 340, delay: -210 },
  { top: 5, scale: 0.55, speed: 420, delay: -120 },
  { top: 30, scale: 0.45, speed: 480, delay: -380 },
  { top: 15, scale: 0.85, speed: 300, delay: -170 },
];

function buildClouds() {
  CLOUDS.forEach((cloud, index) => {
    const seed = 11 + index * 7;
    const layers = [
      ["lit", 0.011, 4, 150],
      ["shade", 0.011, 3, 120],
      ["base", 0.013, 3, 90],
    ];
    const track = document.createElement("div");
    track.className = "cloud-track";
    track.style.top = `${cloud.top}%`;
    track.style.animationDuration = `${cloud.speed}s`;
    track.style.animationDelay = `${cloud.delay}s`;
    const body = document.createElement("div");
    body.className = "cloud";
    body.style.scale = cloud.scale;
    for (const [name, frequency, octaves, strength] of layers) {
      const id = `cloud-${index}-${name}`;
      const filter = document.createElementNS(SVG_NS, "filter");
      filter.id = id;
      filter.setAttribute("x", "-40%");
      filter.setAttribute("y", "-40%");
      filter.setAttribute("width", "180%");
      filter.setAttribute("height", "180%");
      const noise = document.createElementNS(SVG_NS, "feTurbulence");
      noise.setAttribute("type", "fractalNoise");
      noise.setAttribute("baseFrequency", String(frequency));
      noise.setAttribute("numOctaves", String(octaves));
      noise.setAttribute("seed", String(seed));
      const displace = document.createElementNS(SVG_NS, "feDisplacementMap");
      displace.setAttribute("in", "SourceGraphic");
      displace.setAttribute("scale", String(strength));
      filter.append(noise, displace);
      cloudFilters.append(filter);

      const layer = document.createElement("span");
      layer.className = `cloud-${name}`;
      layer.style.filter = `url(#${id})`;
      body.append(layer);
    }
    track.append(body);
    cloudLayer.append(track);
  });
}

// ---------- 街並み ----------

// 遠いビルほど大気でかすんで見えるよう、奥・中・手前の3層に分けて描く
const LAYERS = [
  { name: "far", min: 40, max: 150, widthMin: 18, widthMax: 54, windows: false },
  { name: "mid", min: 50, max: 215, widthMin: 26, widthMax: 78, windows: true },
  { name: "near", min: 24, max: 110, widthMin: 46, widthMax: 130, windows: true },
];

function buildSkyline() {
  const width = 1600;
  const ground = 300;
  const random = seededRandom(424242);
  const beacons = document.createElementNS(SVG_NS, "g");
  beacons.setAttribute("class", "beacons");

  LAYERS.forEach((layer, depth) => {
    const windows = document.createElementNS(SVG_NS, "g");
    windows.setAttribute("class", "windows");
    let path = `M0 ${ground}`;
    let x = -random() * 20;
    while (x < width) {
      const w = layer.widthMin + random() * (layer.widthMax - layer.widthMin);
      // 中央はブラウザ窓の裏になるので、背の高いビルを左右に寄せる
      const edge = Math.abs(x + w / 2 - width / 2) / (width / 2);
      const h = layer.min + Math.pow(random(), 1.4) * (layer.max - layer.min) * (0.55 + edge * 0.45);
      const top = ground - h;
      path += ` L${x.toFixed(1)} ${top.toFixed(1)}`;
      // 一部のビルは屋上に段や塔屋を載せる
      if (random() < 0.3 && w > 30) {
        const inset = w * (0.2 + random() * 0.2);
        const step = 6 + random() * 14;
        path += ` L${(x + inset).toFixed(1)} ${top.toFixed(1)} L${(x + inset).toFixed(1)} ${(top - step).toFixed(1)}`;
        path += ` L${(x + w - inset).toFixed(1)} ${(top - step).toFixed(1)} L${(x + w - inset).toFixed(1)} ${top.toFixed(1)}`;
      }
      // 高いビルにはアンテナと航空障害灯
      if (h > layer.max * 0.72 && layer.name !== "near") {
        const ax = x + w / 2;
        const tip = top - 14 - random() * 22;
        path += ` L${(ax - 1).toFixed(1)} ${top.toFixed(1)} L${(ax - 1).toFixed(1)} ${tip.toFixed(1)}`;
        path += ` L${(ax + 1).toFixed(1)} ${tip.toFixed(1)} L${(ax + 1).toFixed(1)} ${top.toFixed(1)}`;
        // 灯は手前の層に隠れないよう、中の層のビルにだけ付ける
        if (layer.name === "mid") {
          const lamp = document.createElementNS(SVG_NS, "circle");
          lamp.setAttribute("cx", ax.toFixed(1));
          lamp.setAttribute("cy", tip.toFixed(1));
          lamp.setAttribute("r", "2.2");
          lamp.style.animationDelay = `${(-random() * 2).toFixed(2)}s`;
          beacons.append(lamp);
        }
      }
      path += ` L${(x + w).toFixed(1)} ${top.toFixed(1)}`;

      if (layer.windows) {
        const cell = layer.name === "near" ? 9 : 7;
        for (let row = top + 8; row < ground - 6; row += cell + 4) {
          for (let col = x + 5; col < x + w - cell; col += cell + 3) {
            const chance = random();
            if (chance > 0.42) continue;
            const win = document.createElementNS(SVG_NS, "rect");
            win.setAttribute("x", col.toFixed(1));
            win.setAttribute("y", row.toFixed(1));
            win.setAttribute("width", String(cell - 3));
            win.setAttribute("height", String(cell - 2));
            // 点く時間帯を窓ごとにずらし、夕方は一部だけ、夜はより多く点ける
            win.setAttribute("class", chance < 0.14 ? "early" : "late");
            if (random() < 0.25) win.classList.add("cool");
            windows.append(win);
          }
        }
      }
      x += w + (layer.name === "near" ? random() * 6 : random() * 3);
    }
    path += ` L${width} ${ground} Z`;
    const shape = document.createElementNS(SVG_NS, "path");
    shape.setAttribute("class", `bld ${layer.name}`);
    shape.setAttribute("d", path);
    skyline.append(shape);
    if (layer.windows) skyline.append(windows);
    if (layer.name === "mid") skyline.append(beacons);
    // 奥の層と中の層のあいだに、地表付近のもやを挟む
    if (depth < 2) {
      const haze = document.createElementNS(SVG_NS, "rect");
      haze.setAttribute("class", "haze");
      haze.setAttribute("x", "0");
      haze.setAttribute("y", depth === 0 ? "120" : "200");
      haze.setAttribute("width", String(width));
      haze.setAttribute("height", depth === 0 ? "180" : "100");
      skyline.append(haze);
    }
  });
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
  drawMoon(now);

  const date = now.toLocaleDateString("ja-JP", { month: "long", day: "numeric", weekday: "short" });
  const pick = (candidates, offset) => candidates[(daySeed(now) + offset) % candidates.length];
  greeting.textContent = `${date}　${pick(period.hello, 0)}`;
  greetingSub.textContent = pick(period.lines, 1);
}

export function startScene() {
  buildSkyline();
  buildClouds();
  drawStars();
  applyTime();
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawStars, 200);
  });
  // 1分ごとに太陽の位置・月の形・時間帯を更新する
  setInterval(applyTime, 60_000);
}
