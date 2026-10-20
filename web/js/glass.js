/* ============================================================================
   glass.js — 液态玻璃的"光学层"
   ----------------------------------------------------------------------------
   传统玻璃拟态只有两个参数：模糊半径、不透明度。Apple 的 Liquid Glass 多了
   两个关键量：**边缘折射（lensing）** 与 **镜面高光（specular）**。
   浏览器里能做出折射的只有一条路：

       backdrop-filter: blur(2px) saturate(180%) url(#svg-filter)
       svg-filter = feImage(位移图) → feDisplacementMap

   feDisplacementMap 用一张图的 R/G 通道当作"每个像素往哪挪多少"：
       R = X 方向，G = Y 方向，128 = 不动
   所以我们为**每个元素**生成一张位移图：内部是中性灰，只有靠近边缘的一圈
   沿法线方向偏移（边缘薄、中间厚 → 就是透镜）。位移图必须和元素同尺寸，
   不然圆角半径和边框厚度对不上，折射会跑到中间去把字motion blur 掉。

   为什么不用现成的 CSS 库：它们要求 npm 构建 / React，而这里只有静态文件。
   实现参考 sohumsuthar/liquid-glass 与 logrocket 的滤镜图思路（见 DESIGN.md）。
   ========================================================================== */
(function () {
  'use strict';

  const SUP = (window.CSS && CSS.supports && CSS.supports('backdrop-filter', 'url(#a)'));
  const MAX_FILTERS = 10;
  const BUCKET = 24;                 // 尺寸按 24px 归档，避免拖动窗口时疯狂重建
  const cache = new Map();
  const made = [];
  let seq = 0;

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  /* 圆角矩形的有符号距离场：负数在内部，0 在边界 */
  function sdf(px, py, w, h, r) {
    const qx = Math.abs(px - w / 2) - (w / 2 - r);
    const qy = Math.abs(py - h / 2) - (h / 2 - r);
    const ax = Math.max(qx, 0), ay = Math.max(qy, 0);
    return Math.sqrt(ax * ax + ay * ay) + Math.min(Math.max(qx, qy), 0) - r;
  }

  /* 生成位移图：rim 内沿法线推像素，内部保持中性 128 */
  function buildMap(w, h, r, bezel, power) {
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    const img = ctx.createImageData(w, h);
    const d = img.data;
    const e = 1.2;                        // 数值微分的步长
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) << 2;
        const s = sdf(x + .5, y + .5, w, h, r);
        if (s < -bezel) {                 // 内部：中性
          d[i] = d[i + 1] = 128; d[i + 2] = 128; d[i + 3] = 255;
          continue;
        }
        const gx = sdf(x + .5 + e, y + .5, w, h, r) - sdf(x + .5 - e, y + .5, w, h, r);
        const gy = sdf(x + .5, y + .5 + e, w, h, r) - sdf(x + .5, y + .5 - e, w, h, r);
        const len = Math.hypot(gx, gy) || 1;
        const t = clamp(1 + s / bezel, 0, 1);      // 0=rim 内沿 1=边界
        const m = Math.pow(t, power) * 127;
        // 法线朝外；折射把背景往里"吸"，所以取反向
        d[i] = clamp(128 - (gx / len) * m, 0, 255);
        d[i + 1] = clamp(128 - (gy / len) * m, 0, 255);
        d[i + 2] = 128;
        d[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    return c.toDataURL('image/png');
  }

  function fallback(el) {
    el.style.backdropFilter = 'blur(2px) saturate(180%) brightness(1.04)';
    el.style.webkitBackdropFilter = 'blur(2px) saturate(180%) brightness(1.04)';
  }

  function apply(el) {
    if (!SUP) return fallback(el);
    const rect = el.getBoundingClientRect();
    const w = Math.round(rect.width), h = Math.round(rect.height);
    if (w < 40 || h < 30) return fallback(el);

    const cs = getComputedStyle(el);
    const radius = parseFloat(cs.borderTopLeftRadius) || 20;
    const bw = Math.round(w / BUCKET) * BUCKET;
    const bh = Math.round(h / BUCKET) * BUCKET;
    const bezel = clamp(Math.min(bw, bh) * 0.10, 14, 34);
    const key = bw + 'x' + bh + ':' + Math.round(radius) + ':' + Math.round(bezel);

    let id = cache.get(key);
    if (!id) {
      const scale = Math.max(1, Math.min(2, 900000 / (bw * bh)));   // 超大面板降采样
      const mw = Math.round(bw / scale), mh = Math.round(bh / scale);
      let href;
      try {
        href = buildMap(mw, mh, Math.max(2, radius / scale), bezel / scale, 1.7);
      } catch (err) {
        return fallback(el);
      }
      id = 'lgf' + (++seq);
      const NS = 'http://www.w3.org/2000/svg';
      const f = document.createElementNS(NS, 'filter');
      f.setAttribute('id', id);
      f.setAttribute('x', '-6%'); f.setAttribute('y', '-6%');
      f.setAttribute('width', '112%'); f.setAttribute('height', '112%');
      f.setAttribute('color-interpolation-filters', 'sRGB');   // 必须！linearRGB 会让 128 漂移
      const im = document.createElementNS(NS, 'feImage');
      im.setAttribute('href', href);
      im.setAttribute('x', '0'); im.setAttribute('y', '0');
      im.setAttribute('width', '100%'); im.setAttribute('height', '100%');
      im.setAttribute('preserveAspectRatio', 'none');
      im.setAttribute('result', 'map');
      const dm = document.createElementNS(NS, 'feDisplacementMap');
      dm.setAttribute('in', 'SourceGraphic');
      dm.setAttribute('in2', 'map');
      dm.setAttribute('scale', String(bezel * 1.15));
      dm.setAttribute('xChannelSelector', 'R');
      dm.setAttribute('yChannelSelector', 'G');
      f.appendChild(im); f.appendChild(dm);
      document.getElementById('lg-defs').appendChild(f);
      made.push(f);
      if (made.length > MAX_FILTERS) {
        const old = made.shift();
        old.remove();
      }
      cache.set(key, id);
    }
    el.style.backdropFilter = 'blur(2px) saturate(185%) brightness(1.04) url(#' + id + ')';
    el.style.webkitBackdropFilter = 'blur(2px) saturate(185%) brightness(1.04) url(#' + id + ')';
  }

  /* 指针位置 → 镜面高光跟着走（这一步让玻璃"活"起来） */
  function wirePointer(el) {
    let raf = 0;
    el.addEventListener('pointermove', (ev) => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const r = el.getBoundingClientRect();
        el.style.setProperty('--mx', ((ev.clientX - r.left) / r.width * 100).toFixed(2) + '%');
        el.style.setProperty('--my', ((ev.clientY - r.top) / r.height * 100).toFixed(2) + '%');
      });
    }, { passive: true });
    el.addEventListener('pointerleave', () => {
      el.style.setProperty('--mx', '50%');
      el.style.setProperty('--my', '0%');
    }, { passive: true });
  }

  const ro = ('ResizeObserver' in window)
    ? new ResizeObserver((entries) => {
        for (const en of entries) {
          const el = en.target;
          clearTimeout(el.__lgT);
          el.__lgT = setTimeout(() => apply(el), 140);
        }
      })
    : null;

  function init() {
    document.querySelectorAll('.glass[data-glass="hero"]').forEach((el) => {
      apply(el);
      wirePointer(el);
      if (ro) ro.observe(el);
    });
  }

  window.Glass = { init, refresh: init, supported: SUP };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
