"""ページの画素を撮らず、許可した SVG 図形だけを独立した PNG にする。"""

# 元の SVG の複製や外部参照を使わず、図形の数値属性だけで描き直す。
# 本文・入力値・SVG の文字・埋め込み画像・CSS・イベント属性を持ち出さない。
ICON_PNG_SCRIPT = r"""
async button => {
  const icons = button.querySelectorAll('svg');
  if (icons.length !== 1) return null;
  const source = icons[0];
  const rect = source.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2 || rect.width > 96 || rect.height > 96) return null;
  const vb = source.viewBox.baseVal;
  if (!vb || vb.width <= 0 || vb.height <= 0 || vb.width > 256 || vb.height > 256) return null;
  const nodes = [source, ...source.querySelectorAll('*')];
  if (nodes.length > 40) return null;
  const allowed = new Set(['svg', 'g', 'path', 'circle', 'ellipse', 'rect', 'line', 'polyline', 'polygon']);
  const ignored = new Set(['title', 'desc']);
  const numbers = /^[-+\d.eE,\s]+$/;
  const path = /^[MmZzLlHhVvCcSsQqTtAa\d.eE,+\s-]+$/;
  const geometry = ['x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry',
    'width', 'height', 'points', 'd', 'stroke-width', 'stroke-dasharray', 'stroke-dashoffset'];
  const ns = 'http://www.w3.org/2000/svg';
  const copies = new Map();
  let budget = 0;
  for (const node of nodes) {
    const tag = node.localName;
    if (ignored.has(tag)) continue;
    if (!allowed.has(tag) || (tag === 'svg' && node !== source)) return null;
    const copy = document.createElementNS(ns, tag);
    for (const name of geometry) {
      const value = node.getAttribute(name);
      if (value === null) continue;
      budget += value.length;
      if (budget > 16384 || value.length > 4096 || !(name === 'd' ? path : numbers).test(value)) return null;
      copy.setAttribute(name, value);
    }
    const transform = node.getAttribute('transform');
    if (transform) {
      if (transform.length > 256 || !/^(\s*(matrix|translate|scale|rotate|skewX|skewY)\s*\([-+\d.eE,\s]+\)\s*)+$/.test(transform)) return null;
      copy.setAttribute('transform', transform);
    }
    const style = getComputedStyle(node);
    copy.setAttribute('fill', style.fill === 'none' ? 'none' : 'black');
    copy.setAttribute('stroke', style.stroke === 'none' ? 'none' : 'black');
    if (numbers.test(style.strokeWidth)) copy.setAttribute('stroke-width', style.strokeWidth);
    for (const name of ['stroke-linecap', 'stroke-linejoin', 'fill-rule']) {
      const value = style.getPropertyValue(name);
      if (/^(butt|round|square|miter|bevel|nonzero|evenodd)$/.test(value)) copy.setAttribute(name, value);
    }
    if (node !== source) {
      const parent = copies.get(node.parentElement);
      if (!parent) return null;
      parent.append(copy);
    }
    copies.set(node, copy);
  }
  const svg = copies.get(source);
  svg.setAttribute('xmlns', ns);
  svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.width} ${vb.height}`);
  svg.setAttribute('width', '216');
  svg.setAttribute('height', '216');
  const serialized = new XMLSerializer().serializeToString(svg);
  const image = new Image();
  image.src = 'data:image/svg+xml;base64,' + btoa(serialized);
  try { await image.decode(); } catch { return null; }
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 280;
  const context = canvas.getContext('2d');
  context.fillStyle = 'white';
  context.fillRect(0, 0, 280, 280);
  context.drawImage(image, 32, 32, 216, 216);
  return canvas.toDataURL('image/png').split(',')[1];
}
"""
