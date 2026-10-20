/* ============================================================================
   app.js — 状态机 / 渲染 / 交互
   数据来源只有一条：每 1 秒拉一次 /api/snapshot（后端把所有探测合并好了）。
   本地编辑放进 draft，轮询不会覆盖你正在输入的框。
   ========================================================================== */
(function () {
  'use strict';

  const qs = new URLSearchParams(location.search);
  const PORT = '__PORT__'.startsWith('__') ? (qs.get('port') || 8770) : '__PORT__';
  const TOKEN = '__TOKEN__'.startsWith('__') ? (qs.get('t') || '') : '__TOKEN__';
  const DEMO = qs.get('demo') === '1' || location.protocol === 'file:';
  const MINI = qs.get('mini') === '1';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtGB = (b) => (b / 1073741824).toFixed(1) + ' GB';
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  let S = null;
  let draft = { launch: {}, sampling: {} };
  let builtFor = '';
  let tab = 'dashboard';
  let activePreset = 'gen';
  let openSel = null;

  if (MINI) {
    document.body.classList.add('mini');
    document.title = 'LLMSwitch Mini';
  }

  /* ---------------- API ---------------- */
  async function api(path, opt) {
    const o = Object.assign({ headers: {} }, opt || {});
    o.headers['X-Token'] = TOKEN;
    if (o.json !== undefined) {
      o.method = o.method || 'POST';
      o.headers['Content-Type'] = 'application/json';
      o.body = JSON.stringify(o.json);
      delete o.json;
    }
    const r = await fetch(path + (path.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TOKEN), o);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function toast(msg, kind) {
    const box = $('#toasts');
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .3s, transform .3s';
      el.style.opacity = '0'; el.style.transform = 'translateY(6px)';
      setTimeout(() => el.remove(), 320);
    }, 3200);
  }

  /* ---------------- 轮询 ---------------- */
  async function tick() {
    if (DEMO) {
      S = demoSnapshot();
    } else {
      try {
        S = await api('/api/snapshot');
      } catch (e) {
        if (!S) {
          $('#stageText').textContent = '连接失败';
          $('#stageSub').textContent = '后端未响应 · 正在重试';
          document.body.dataset.stage = 'error';
        }
        return;
      }
    }
    try { render(); } catch (err) { if (window.console) console.error('[render]', err); }
  }

  /* ---------------- 渲染 ---------------- */
  function stageText(st) {
    return { stopped: '已停止', starting: '启动中', running: '运行中', stopping: '停止中', error: '启动失败' }[st] || st;
  }

  function render() {
    renderStatus();
    renderTop();
    renderSide();
    if (tab === 'dashboard') { renderStats(); renderTables(); drawMainChart(); }
    if (tab === 'models') renderModelGrid();
    if (tab === 'runtime') { renderRuntime(); renderBudget(); }
    if (tab === 'sampling') renderSampling();
  }

  function renderStatus() {
    const st = S.status.stage;
    document.body.dataset.stage = st;
    if (MINI) document.title = 'LLMSwitch Mini · ' + stageText(st);
    $('#stageText').textContent = stageText(st);
    const sub = {
      stopped: (S.setup && S.setup.needs_llama_dir) ? '未配置 llama.cpp 目录' : '等待启动',
      starting: '加载权重与 KV · ' + (S.status.ctx || ''),
      running: (S.status.pid ? 'PID ' + S.status.pid + ' · ' : '') + (S.live.health === 'ok' ? '接口就绪' : '预热中'),
      stopping: '正在释放显存',
      error: S.status.error || '未知原因',
    }[st] || '';
    $('#stageSub').textContent = sub;
    $('#stageSub').title = sub;

    const btn = $('#btnToggle');
    const running = st === 'running' || st === 'starting';
    btn.querySelector('span').textContent = running ? '停止' : (st === 'error' ? '重试' : '启动');
    btn.querySelector('svg').innerHTML = running
      ? '<rect x="7" y="5" width="10" height="14" rx="2" fill="currentColor" stroke="none"/>'
      : '<path d="M8 5.5v13l11-6.5z"/>';
    btn.classList.toggle('danger', running);
    $('#btnRestart').disabled = (st === 'stopped');
    const mt = $('#btnMiniToggle');
    if (mt) {
      mt.textContent = running ? '停止' : '启动';
      mt.classList.toggle('danger', running);
    }

    $('#pillStage').querySelector('span').textContent = stageText(st);
    $('#tbTps').textContent = S.live.tps ? S.live.tps.toFixed(1) : '—';

    const needDir = !!(S.setup && S.setup.needs_llama_dir);
    $('#setupStrip').hidden = !needDir;
    const need = S.plan.need_mib, free = S.plan.vram_free;
    const over = !needDir && need > free && S.plan.vram_total;
    $('#warnStrip').hidden = !over;
    if (over) {
      $('#warnText').textContent =
        '按当前配置预计需要 ' + need + ' MiB，可用仅 ' + free + ' MiB，可能 OOM。'
        + '可以把上下文降到 ' + suggestCtx(free) + 'K，或改用 q8_0 KV，或先关掉占用显存的程序。';
    }
  }

  function renderTop() {
    const c = S.current;
    $('#tbModel').textContent = c ? c.label
      : ((S.setup && S.setup.needs_llama_dir) ? '未配置模型目录' : '没有检测到模型');
    const tags = [];
    if (c) {
      if (c.spec) tags.push('<span class="tag">' + esc(c.spec) + '</span>');
      if (c.quant) tags.push('<span class="tag acc">' + esc(c.quant) + '</span>');
      if (c.has_nextn) tags.push('<span class="tag run">MTP</span>');
      if (c.has_mmproj) tags.push('<span class="tag">Vision</span>');
      if (c.n_layers) tags.push('<span class="tag">' + c.n_layers + ' L</span>');
      if (c.n_expert) tags.push('<span class="tag">' + c.n_expert + ' E</span>');
    }
    $('#tbTags').innerHTML = tags.join('');

    const g = S.live.gpu || {};
    const total = g.total || 0, used = g.used || 0;
    const pct = total ? clamp(used / total * 100, 0, 100) : 0;
    const fill = $('#vramFill');
    fill.style.width = pct.toFixed(1) + '%';
    fill.classList.toggle('hot', pct > 85);
    $('#vramTxt').textContent = total ? ((used / 1024).toFixed(1) + ' / ' + (total / 1024).toFixed(1) + ' GB') : '未检测到';
    $('#footVram').textContent = total ? ((used / 1024).toFixed(1) + ' / ' + (total / 1024).toFixed(1) + ' GB') : '—';
    const fp = $('#footPort');
    fp.textContent = S.paths.llama_port || S.paths.port;
    fp.title = 'llama-server: ' + (S.paths.llama_port || '') + ' · 控制台: ' + S.paths.port;
    $('#modelCount').textContent = S.models.length;
    $('#verTag').textContent = 'v' + S.version;
  }

  function renderSide() {
    const box = $('#modelList');
    const html = S.models.map((m) => {
      const cls = ['mitem'];
      if (m.selected) cls.push('on');
      if (m.running) cls.push('run');
      return '<div class="' + cls.join(' ') + '" data-mid="' + esc(m.id) + '" title="' + esc(m.id) + '">'
        + '<div class="mt"><i></i>' + esc(m.label) + '</div>'
        + '<div class="ms"><b>' + esc(m.quant || '—') + '</b><span>·</span><span>' + fmtGB(m.size) + '</span>'
        + (m.has_nextn ? '<span>·</span><span>MTP</span>' : '') + '</div></div>';
    }).join('');
    if (box.dataset.sig !== html) { box.innerHTML = html; box.dataset.sig = html; }
  }

  /* ---------------- 概览 ---------------- */
  function renderStats() {
    const L = S.live;
    $('#sTps').textContent = (L.tps || 0).toFixed(1);
    $('#sTpsFoot').textContent = L.prefill_tps
      ? ('prefill ' + L.prefill_tps + ' tok/s · 处理中 ' + L.req_processing)
      : '等待启动';
    const capK = (L.ctx_cap || 0) / 1024;
    const usedK = (L.ctx_used || 0) / 1024;
    $('#sCtx').textContent = usedK >= 10 ? Math.round(usedK) : usedK.toFixed(1);
    $('#sCtxCap').textContent = capK ? '/ ' + Math.round(capK) + 'K' : '/ —';
    const cf = $('#ctxFill');
    const pct = capK ? clamp(usedK / capK * 100, 0, 100) : 0;
    cf.style.width = pct + '%';
    cf.classList.toggle('hot', pct > 85);
    $('#sCtxFoot').textContent = capK ? ('剩余 ' + Math.max(0, Math.round(capK - usedK)) + 'K') : '来自 /slots 的实时值';
    $('#sVram').textContent = (S.live.gpu.used || 0);
    $('#sVramFoot').textContent = (S.live.gpu.name || 'GPU') + ' · ' + (S.live.gpu.util || 0) + '%';
    $('#sBudget').textContent = S.cfg.sampling.budget;
    $('#sBudgetFoot').textContent = '当前模型：' + (S.current ? S.current.label : '—');
    drawSpark($('[data-spark="tps"]'), S.live.history.map((r) => r[2]), '#7dd3fc');
    drawSpark($('[data-spark="vram"]'), S.live.history.map((r) => r[1]), '#a78bfa');
  }

  function renderTables() {
    const L = S.live, c = S.current || {};
    const rows = [
      ['模型', esc(c.label || '—')],
      ['上下文 / KV', (S.status.ctx || '—') + ' / ' + (S.status.kv || '—')],
      ['GPU 层数', '<code>' + esc(S.cfg.launch.ngl) + '</code>'],
      ['张量覆盖', '<code>' + esc(S.cfg.launch.ot || '无') + '</code>'],
      ['Flash Attention', esc(S.cfg.launch.fa)],
      ['投机解码', S.cfg.launch.spec === 'draft-mtp' ? ('MTP ×' + S.cfg.launch.spec_n_max) : '关闭'],
      ['思考预算', S.cfg.sampling.budget + ' tok'],
      ['预热耗时', S.status.warmup_ms ? (S.status.warmup_ms / 1000).toFixed(1) + ' s' : '—'],
    ];
    $('#launchTable').innerHTML = rows.map(([k, v]) =>
      '<div class="k">' + k + '</div><div class="v">' + v + '</div>').join('');

    const env = [
      ['显卡', esc(L.gpu.name || '未检测到')],
      ['驱动', esc(L.gpu.driver || '—')],
      ['构建', esc(L.build_info || '—')],
      ['槽位', (L.total_slots || '—') + ' · 处理中 ' + (L.req_processing || 0)],
      ['投机接受率', L.spec_draft ? (L.spec_rate + '%') : '—'],
      ['llama.cpp', '<code>' + esc(S.paths.llama_dir || '未设置') + '</code>'],
    ];
    $('#envTable').innerHTML = env.map(([k, v]) =>
      '<div class="k">' + k + '</div><div class="v">' + v + '</div>').join('');
  }

  /* ---------------- 模型 ---------------- */
  function renderModelGrid() {
    const box = $('#modelGrid');
    const dir = $('#modelsDir');
    if (dir) dir.textContent = S.paths.models_dir || '—';
    const cnt = $('#modelsCount');
    if (cnt) cnt.textContent = S.models.length;
    const sz = $('#modelsSize');
    if (sz) sz.textContent = fmtGB(S.models.reduce((a, m) => a + (m.size || 0), 0));
    const maxSize = Math.max.apply(null, S.models.map((m) => m.size).concat([1]));
    const html = S.models.length ? S.models.map((m) => {
      const badges = [
        m.has_nextn ? '<span class="tag run">MTP</span>' : '',
        m.has_mmproj ? '<span class="tag">mmproj</span>' : '',
        m.selected ? '<span class="tag acc">当前</span>' : '',
        m.running ? '<span class="tag run">已载入</span>' : '',
      ].join('');
      return '<div class="card hover mcard ' + (m.selected ? 'on' : '') + '" data-mid="' + esc(m.id) + '">'
        + '<div class="mh"><div class="mh-t"><h3>' + esc(m.label) + '</h3>'
        + '<div class="sub">' + esc(m.dir) + ' · ' + esc(m.quant || '?') + '</div></div>'
        + '<div class="mh-b">' + badges + '</div></div>'
        + '<div class="meta">'
        + '<div><span>规格</span><b>' + esc(m.spec || '—') + '</b></div>'
        + '<div><span>体积</span><b>' + fmtGB(m.size) + '</b></div>'
        + '<div><span>架构</span><b>' + esc(m.arch || '—') + '</b></div>'
        + '<div><span>层 / 专家</span><b>' + (m.n_layers || '?') + ' / ' + (m.n_expert || '?') + '</b></div>'
        + '<div><span>原生上下文</span><b>' + (m.n_ctx_train ? (m.n_ctx_train / 1024) + 'K' : '—') + '</b></div>'
        + '<div><span>量化</span><b>' + esc(m.quant || '—') + '</b></div>'
        + '</div>'
        + '<div class="size-bar"><i style="width:' + (m.size / maxSize * 100).toFixed(1) + '%"></i></div>'
        + '<div class="mfoot"><button class="btn mini ghost" data-act="select" data-mid="' + esc(m.id) + '">设为当前</button>'
        + '<span class="grow"></span>'
        + '<button class="btn mini primary" data-act="swap" data-mid="' + esc(m.id) + '">载入此模型</button>'
        + '</div></div>';
    }).join('') : '<div class="card"><div class="hint">models/ 下没有 .gguf 文件。把模型放进 '
      + esc(S.paths.models_dir || 'llama.cpp/models') + ' 后点右上角「重新扫描」。</div></div>';
    if (box.dataset.sig !== html) { box.innerHTML = html; box.dataset.sig = html; }
  }

  /* ---------------- 表单控件 ---------------- */
  function optLabel(spec, v) {
    for (const o of (spec.options || [])) if (String(o[0]) === String(v)) return o[1];
    return String(v);
  }

  function fieldHtml(spec, val, ns) {
    const id = ns + '_' + spec.key;
    const attrs = 'data-ns="' + ns + '" data-k="' + spec.key + '"';
    let ctl = '';

    if (spec.kind === 'enum') {
      ctl = '<div class="sel" ' + attrs + ' data-v="' + esc(val) + '" id="' + id + '">'
        + '<button type="button" class="sel-btn"><span class="sel-val">' + esc(optLabel(spec, val))
        + '</span><svg viewBox="0 0 12 12"><path d="M2 4l4 4 4-4"/></svg></button></div>';
    } else if (spec.kind === 'bool') {
      ctl = '<label class="switch"><input type="checkbox" ' + attrs + (val ? ' checked' : '')
        + '><span></span><i class="sw-txt">' + (val ? '开启' : '关闭') + '</i></label>';
    } else if (spec.kind === 'int' || spec.kind === 'float') {
      const hasRange = spec.lo !== undefined && spec.hi !== undefined;
      const step = spec.step || (spec.kind === 'int' ? 1 : 0.01);
      const pct = hasRange ? (clamp((parseFloat(val) - spec.lo) / (spec.hi - spec.lo), 0, 1) * 100).toFixed(1) + '%' : '50%';
      ctl = (hasRange ? '<div class="range"><input type="range" ' + attrs + ' min="' + spec.lo + '" max="' + spec.hi
        + '" step="' + step + '" value="' + esc(val) + '" style="--p:' + pct + '"></div>' : '')
        + '<div class="step"><button type="button" data-step="-1" aria-label="减少">−</button>'
        + '<input type="number" ' + attrs + ' id="' + id + '" value="' + esc(val) + '" step="' + step + '">'
        + '<button type="button" data-step="1" aria-label="增加">+</button></div>';
    } else {
      ctl = '<input type="text" ' + attrs + ' id="' + id + '" value="' + esc(val) + '"'
        + (spec.placeholder ? ' placeholder="' + esc(spec.placeholder) + '"' : '') + '>';
    }

    // 启动参数卡片较窄 → 带滑条的项占整行；采样页三列排布更紧凑，不强制整行
    const wide = ns === 'launch' && (spec.kind === 'int' || spec.kind === 'float') && spec.lo !== undefined;
    return '<div class="field' + (wide ? ' wide' : '') + '" data-fk="' + ns + ':' + spec.key + '">'
      + '<label for="' + id + '"><span>' + esc(spec.label) + '</span><em class="key">' + esc(spec.key) + '</em></label>'
      + '<div class="ctl">' + ctl + '</div>'
      + (spec.hint ? '<div class="hint">' + esc(spec.hint) + '</div>' : '')
      + '</div>';
  }

  const GROUP_OF = {
    ctx: 'fieldsCtx', kv: 'fieldsCtx', ngl: 'fieldsCtx', ot: 'fieldsCtx',
    fa: 'fieldsPerf', fit: 'fieldsPerf', fitt: 'fieldsPerf', threads: 'fieldsPerf',
    batch: 'fieldsPerf', ubatch: 'fieldsPerf',
    cache_ram: 'fieldsAdv', keep: 'fieldsAdv', kv_offload: 'fieldsAdv', moe_cache: 'fieldsAdv',
    spec: 'fieldsAdv', spec_n_max: 'fieldsAdv', mmproj: 'fieldsAdv', metrics: 'fieldsAdv',
    alias: 'fieldsAdv', np: 'fieldsAdv',
  };

  function buildForms() {
    if (!S) return;
    const chosen = S.current ? S.current.id : '';
    if (builtFor === chosen && $('#fieldsCtx').childElementCount) return;
    builtFor = chosen;
    draft.launch = Object.assign({}, S.cfg.launch);
    draft.sampling = Object.assign({}, S.cfg.sampling);

    const buckets = { fieldsCtx: [], fieldsPerf: [], fieldsAdv: [] };
    S.specs.launch.forEach((sp) => {
      const box = GROUP_OF[sp.key] || 'fieldsAdv';
      buckets[box].push(fieldHtml(sp, draft.launch[sp.key], 'launch'));
    });
    Object.keys(buckets).forEach((k) => { $('#' + k).innerHTML = buckets[k].join(''); });

    const DIVS = {
      dry_mult: '复读抑制 · DRY',
      xtc_prob: '多样性 · XTC / Typical',
      budget: '思考',
      sampler_order: '采样链',
    };
    $('#samplingFields').innerHTML = S.specs.sampling.map((sp) =>
      (DIVS[sp.key] ? '<div class="fdiv"><span>' + DIVS[sp.key] + '</span></div>' : '')
      + fieldHtml(sp, draft.sampling[sp.key], 'sampling')).join('');

    renderPresets();
    markDirty();
  }

  function renderRuntime() { buildForms(); refreshValues('launch'); }
  function renderSampling() { buildForms(); refreshValues('sampling'); }

  function setValue(ns, k, v) {
    draft[ns][k] = v;
    markDirty();
  }

  function refreshValues(ns) {
    $$('[data-ns="' + ns + '"]').forEach((el) => {
      const k = el.dataset.k;
      const v = draft[ns][k];
      if (el.classList.contains('sel')) {
        el.dataset.v = v;
        const lab = el.querySelector('.sel-val');
        const spec = specOf(ns, k);
        if (lab && spec) lab.textContent = optLabel(spec, v);
      } else if (el.type === 'checkbox') {
        el.checked = !!v;
        const t = el.closest('.switch') && el.closest('.switch').querySelector('.sw-txt');
        if (t) t.textContent = el.checked ? '开启' : '关闭';
      } else if (el.type === 'range') {
        if (document.activeElement !== el) el.value = v;
        paintRange(el);
      } else if (document.activeElement !== el) {
        el.value = v;
      }
    });
  }

  function paintRange(el) {
    const lo = parseFloat(el.min), hi = parseFloat(el.max);
    const p = clamp((parseFloat(el.value) - lo) / (hi - lo || 1), 0, 1) * 100;
    el.style.setProperty('--p', p.toFixed(1) + '%');
  }

  function specOf(ns, k) {
    const list = ns === 'launch' ? S.specs.launch : S.specs.sampling;
    return list.find((x) => x.key === k);
  }

  /* ---------------- 自定义下拉 ---------------- */
  function closeSel() {
    if (!openSel) return;
    openSel.classList.remove('open');
    const pop = openSel.querySelector('.sel-pop');
    if (pop && pop.parentNode === document.body) pop.remove();
    openSel = null;
  }

  function openSelect(sel) {
    const spec = specOf(sel.dataset.ns, sel.dataset.k);
    if (!spec) return;
    if (openSel === sel) { closeSel(); return; }
    closeSel();
    const btn = sel.querySelector('.sel-btn');
    const pop = document.createElement('div');
    pop.className = 'sel-pop';
    (spec.options || []).forEach(([v, label]) => {
      const o = document.createElement('div');
      o.className = 'sel-opt' + (String(v) === String(sel.dataset.v) ? ' on' : '');
      o.dataset.v = v;
      o.textContent = label;
      pop.appendChild(o);
    });
    document.body.appendChild(pop);
    const r = btn.getBoundingClientRect();
    pop.style.minWidth = Math.max(120, r.width) + 'px';
    const ph = pop.offsetHeight;
    const below = window.innerHeight - r.bottom;
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)) + 'px';
    pop.style.top = (below < ph + 12 ? Math.max(8, r.top - ph - 6) : r.bottom + 6) + 'px';
    sel.classList.add('open');
    openSel = sel;
  }

  /* ---------------- 图表 ---------------- */
  function sizeCanvas(cv) {
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || cv.parentElement.clientWidth;
    const h = cv.clientHeight || parseInt(cv.getAttribute('height'), 10) || 40;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  function drawSpark(cv, vals, color) {
    if (!cv) return;
    const { ctx, w, h } = sizeCanvas(cv);
    ctx.clearRect(0, 0, w, h);
    if (!vals || vals.length < 2) return;
    const max = Math.max.apply(null, vals) || 1;
    const X = (i) => (i / (vals.length - 1)) * w;
    const Y = (v) => h - 3 - (v / max) * (h - 6);
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, color + '55');
    grad.addColorStop(1, color + '00');
    ctx.beginPath();
    ctx.moveTo(0, h);
    vals.forEach((v, i) => ctx.lineTo(X(i), Y(v)));
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    vals.forEach((v, i) => (i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.stroke();
  }

  function drawMainChart() {
    const cv = $('#mainChart');
    if (!cv) return;
    const { ctx, w, h } = sizeCanvas(cv);
    ctx.clearRect(0, 0, w, h);
    const rows = (S.live.history || []).slice(-180);
    const padL = 36, padR = 38, padT = 10, padB = 22;
    const iw = Math.max(1, w - padL - padR), ih = Math.max(1, h - padT - padB);
    ctx.strokeStyle = 'rgba(255,255,255,.06)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = padT + ih * i / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
    }
    ctx.fillStyle = 'rgba(178,187,204,.6)';
    ctx.font = '10.5px "Segoe UI", sans-serif';
    if (rows.length < 2) {
      ctx.textAlign = 'center';
      ctx.fillText('启动后开始采样', w / 2, h / 2);
      return;
    }
    const maxTps = Math.max(10, Math.max.apply(null, rows.map((r) => r[2] || 0)) * 1.15);
    const cap = S.live.ctx_cap || (parseInt(S.status.ctx, 10) * 1024) || 131072;
    const X = (i) => padL + (i / (rows.length - 1)) * iw;
    const Y1 = (v) => padT + ih - (v / maxTps) * ih;
    const Y2 = (v) => padT + ih - (v / cap) * ih;

    const g = ctx.createLinearGradient(0, padT, 0, padT + ih);
    g.addColorStop(0, 'rgba(125,211,252,.40)');
    g.addColorStop(1, 'rgba(125,211,252,0)');
    ctx.beginPath();
    ctx.moveTo(X(0), padT + ih);
    rows.forEach((r, i) => ctx.lineTo(X(i), Y1(r[2] || 0)));
    ctx.lineTo(X(rows.length - 1), padT + ih);
    ctx.closePath();
    ctx.fillStyle = g;
    ctx.fill();

    ctx.beginPath();
    rows.forEach((r, i) => (i ? ctx.lineTo(X(i), Y1(r[2] || 0)) : ctx.moveTo(X(i), Y1(r[2] || 0))));
    ctx.strokeStyle = '#7dd3fc';
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    ctx.stroke();

    ctx.beginPath();
    rows.forEach((r, i) => (i ? ctx.lineTo(X(i), Y2(r[3] || 0)) : ctx.moveTo(X(i), Y2(r[3] || 0))));
    ctx.strokeStyle = 'rgba(167,139,250,.85)';
    ctx.lineWidth = 1.4;
    ctx.setLineDash([4, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = 'rgba(178,187,204,.6)';
    ctx.textAlign = 'right';
    ctx.fillText(maxTps.toFixed(0), padL - 6, padT + 8);
    ctx.fillText('0', padL - 6, padT + ih);
    ctx.textAlign = 'left';
    ctx.fillText(Math.round(cap / 1024) + 'K', padL + iw + 6, padT + 8);
    ctx.fillText('0', padL + iw + 6, padT + ih);
    const age = Math.max(0, Math.round(Date.now() / 1000 - rows[rows.length - 1][0]));
    $('#chartAge').textContent = age <= 2 ? '刚刚' : age + ' 秒前';
  }

  /* ---------------- 显存预算 ---------------- */
  function needOf(ctxK, kv) {
    const per = (kv === 'f16' || kv === 'bf16') ? 30 : 15;
    return Math.round(2680 + ctxK * per);
  }

  function suggestCtx(free) {
    for (const c of [128, 64, 32, 16]) if (free >= 2680 + c * 30 + 400) return c;
    return 8;
  }

  function renderBudget() {
    const l = draft.launch;
    if (!l.ctx) return;
    const ctxK = l.ctx === 'auto' ? clamp(Math.floor((S.plan.vram_free - 3080) / 30), 8, 128) : parseInt(l.ctx, 10);
    const eff = l.kv === 'auto' ? (ctxK >= 128 ? 'q8_0' : 'f16') : l.kv;
    const need = needOf(ctxK, eff);
    const total = S.plan.vram_total || 0;
    const used = S.plan.vram_used || 0;
    const free = S.plan.vram_free || 0;
    const base = total || (used + free) || 1;
    const pUsed = clamp(used / base * 100, 0, 100);
    const pNeed = clamp(need / base * 100, 0, 100 - pUsed);
    $('#segModel').style.width = pUsed + '%';
    $('#segKv').style.width = pNeed + '%';
    $('#segFree').style.width = clamp(100 - pUsed - pNeed, 0, 100) + '%';
    $('#bdUsed').textContent = used + ' MiB';
    $('#bdNeed').textContent = need + ' MiB';
    $('#bdFree').textContent = free + ' MiB';

    const hint = $('#budgetHint');
    if (!total) {
      hint.textContent = '没有检测到 NVIDIA 显卡信息，这里只能按公式估算。';
      hint.style.color = '';
    } else if (need > free) {
      hint.innerHTML = '预计超出 <b>' + (need - free) + ' MiB</b>。建议把上下文降到 '
        + suggestCtx(free) + 'K，或改用 q8_0 KV。';
      hint.style.color = 'var(--warn)';
    } else {
      hint.innerHTML = '预计还剩 <b>' + (free - need) + ' MiB</b> 余量（KV '
        + (l.kv === 'auto' ? '自动→' + eff : eff) + ' · ' + (l.ctx === 'auto' ? '自动 ' + ctxK + 'K' : l.ctx + 'K') + '）。';
      hint.style.color = '';
    }
  }

  /* ---------------- 预设 ---------------- */
  function renderPresets() {
    const box = $('#presetChips');
    const labels = (S.specs && S.specs.style_labels) || {};
    let html = (S.specs.styles || []).map((k) =>
      '<button class="chip ' + (activePreset === k ? 'on' : '') + '" data-preset-builtin="' + k + '">'
      + esc(labels[k] || k) + '</button>').join('');
    const names = Object.keys(S.presets || {});
    if (names.length) {
      html += names.map((n) =>
        '<button class="chip ' + (activePreset === n ? 'on' : '') + '" data-preset="' + esc(n) + '">'
        + esc(n) + '<span class="x" data-preset-del="' + esc(n) + '" title="删除">×</span></button>').join('');
    }
    box.innerHTML = html;
  }

  async function applyBuiltin(style) {
    activePreset = style;
    if (DEMO) { applyStyleLocal(style); return; }
    try {
      const d = await api('/api/style', { json: { id: S.current.id, style: style } });
      S.cfg = d.cfg;
      draft.sampling = Object.assign({}, d.cfg.sampling);
      refreshValues('sampling');
      renderPresets();
      markDirty();
      toast('已套用「' + ((S.specs.style_labels || {})[style] || style) + '」', 'ok');
    } catch (e) { toast('套用失败：' + e.message, 'err'); }
  }

  function applyStyleLocal(style) {
    Object.assign(draft.sampling, DEMO_STYLES[style] || {}, { sty: style });
    refreshValues('sampling');
    renderPresets();
    markDirty();
  }

  function applyPreset(name) {
    const p = (S.presets || {})[name];
    if (!p) return;
    draft.sampling = Object.assign({}, p.sampling || {});
    draft.launch = Object.assign({}, p.launch || {});
    activePreset = name;
    refreshValues('sampling');
    refreshValues('launch');
    renderPresets();
    renderBudget();
    markDirty();
    toast('已载入预设「' + name + '」，保存后生效', 'ok');
  }

  /* ---------------- 日志 ---------------- */
  async function loadLogs() {
    if (DEMO) { $('#logBox').innerHTML = demoLog(); return; }
    try {
      const d = await api('/api/logs?n=400');
      const lines = d.lines || [];
      $('#logBox').innerHTML = lines.map((ln) => {
        const e = esc(ln);
        if (/\bE\b|error|failed/i.test(ln)) return '<span class="e">' + e + '</span>';
        if (/\bW\b|warn/i.test(ln)) return '<span class="w">' + e + '</span>';
        return e;
      }).join('\n') || '（暂无日志）';
      const box = $('#logBox');
      box.scrollTop = box.scrollHeight;
    } catch (e) { /* 静默 */ }
  }

  /* ---------------- 保存 / 动作 ---------------- */
  function markDirty() {
    if (!S) return;
    const cl = JSON.stringify(draft.launch) !== JSON.stringify(S.cfg.launch);
    const cs = JSON.stringify(draft.sampling) !== JSON.stringify(S.cfg.sampling);
    const bl = $('#btnSaveLaunch');
    bl.textContent = cl ? '保存修改' : '已保存';
    bl.classList.toggle('primary', cl);
    const bs = $('#btnSaveSampling');
    bs.textContent = cs ? '保存修改' : '已保存';
    bs.classList.toggle('primary', cs);
  }

  async function save(ns, restart) {
    if (DEMO) { toast('演示模式不会真的保存', 'ok'); return; }
    const cfg = {};
    cfg[ns] = draft[ns];
    try {
      const d = await api('/api/config', { json: { id: S.current.id, cfg: cfg } });
      S.cfg = d.cfg;
      draft[ns] = Object.assign({}, d.cfg[ns]);
      markDirty();
      const running = ['running', 'starting'].includes(S.status.stage);
      if (running) {
        if (restart) {
          await api('/api/server', { json: { action: 'restart' } });
          toast('已保存，正在按新参数重启…', 'ok');
        } else {
          toast('已保存。当前进程仍用旧参数，点「保存并重启」立即生效', 'ok');
        }
      } else {
        toast('已保存', 'ok');
      }
      tick();
    } catch (e) { toast('保存失败：' + e.message, 'err'); }
  }

  async function serverAction(act, mid) {
    if (DEMO) { toast('演示模式不会真的启动服务', 'ok'); return; }
    try {
      const d = await api('/api/server', { json: { action: act, model: mid } });
      if (!d.ok) toast(d.msg || '操作未执行', 'err');
      setTimeout(tick, 300);
    } catch (e) { toast('操作失败：' + e.message, 'err'); }
  }

  async function selectModel(mid, andSwap) {
    if (!mid) return;
    if (DEMO) { toast('演示模式', 'ok'); return; }
    try {
      const d = await api('/api/select', { json: { id: mid } });
      S = d.snapshot;
      builtFor = '';
      render();
      if (andSwap && ['running', 'starting'].includes(S.status.stage)) serverAction('swap', mid);
    } catch (e) { toast('切换失败：' + e.message, 'err'); }
  }

  /* ---------------- 弹窗 ---------------- */
  function modal(title, text) {
    $('#modalTitle').textContent = title;
    $('#modalBody').textContent = text;
    $('#modalInput').hidden = true;
    $('#modalCancel').hidden = true;
    $('#modalOk').hidden = false;
    $('#modalOk').textContent = '复制';
    const m = $('#modal');
    m.hidden = false;
    m.dataset.mode = 'text';
    m.dataset.copy = text;
  }

  function askName(title, placeholder, value) {
    return new Promise((resolve) => {
      const m = $('#modal');
      $('#modalTitle').textContent = title;
      $('#modalBody').textContent = '';
      const inp = $('#modalInput');
      inp.hidden = false;
      inp.placeholder = placeholder || '';
      inp.value = value || '';
      $('#modalCancel').hidden = false;
      $('#modalOk').hidden = false;
      $('#modalOk').textContent = '保存';
      m.hidden = false;
      m.dataset.mode = 'name';
      setTimeout(() => { inp.focus(); inp.select(); }, 30);
      m._resolve = resolve;
    });
  }

  function aboutModal() {
    const lines = [
      'LLMSwitch Studio v' + (S ? S.version : '?'),
      '',
      '把 llama.cpp 的 llama-server 包装成可切换、可调参、可观测的桌面控制台。',
      '',
      '快捷键',
      '  Alt+S   启动 / 停止',
      '  Alt+R   重启',
      '  Esc     关闭弹窗',
      '',
      '目录',
      '  llama.cpp   ' + (S ? (S.paths.llama_dir || '未设置') : ''),
      '  模型        ' + (S ? (S.paths.models_dir || '—') : ''),
      '  日志        ' + (S ? (S.paths.log || '—') : ''),
      '',
      '界面由本地 HTTP 服务（仅 127.0.0.1）驱动，窗口只是它的浏览器外壳。',
      '所有数据都留在本机。',
    ].join('\n');
    modal('关于', lines);
  }

  /* ---------------- 事件 ---------------- */
  function bind() {
    $('#tabs').addEventListener('click', (ev) => {
      const b = ev.target.closest('button[data-tab]');
      if (b) setTab(b.dataset.tab);
    });

    $('#modelList').addEventListener('click', (ev) => {
      const it = ev.target.closest('.mitem');
      if (it) selectModel(it.dataset.mid, true);
    });

    $('#modelGrid').addEventListener('click', (ev) => {
      const b = ev.target.closest('button[data-act]');
      if (!b) return;
      if (b.dataset.act === 'select') selectModel(b.dataset.mid, false);
      if (b.dataset.act === 'swap') {
        selectModel(b.dataset.mid, false).then(() => serverAction('swap', b.dataset.mid));
      }
    });

    $('#btnToggle').addEventListener('click', toggleRun);
    $('#btnRestart').addEventListener('click', () => serverAction('restart', S.current && S.current.id));
    $('#btnFloat').addEventListener('click', () => openFloat(true));

    $('#content').addEventListener('input', onInput);
    $('#content').addEventListener('change', onInput);
    $('#content').addEventListener('click', onClickControls);
    $('#btnSaveLaunch').addEventListener('click', () => save('launch', false));
    $('#btnApplyRestart').addEventListener('click', () => save('launch', true));
    $('#btnSaveSampling').addEventListener('click', () => save('sampling', false));

    $('#btnAutoCtx').addEventListener('click', () => {
      const c = suggestCtx(S.plan.vram_free);
      draft.launch.ctx = String(c);
      draft.launch.kv = 'auto';
      refreshValues('launch');
      renderBudget();
      markDirty();
      toast('按可用显存选了 ' + c + 'K / 自动 KV', 'ok');
    });

    const om = $('#btnOpenModels');
    if (om) om.addEventListener('click', () => api('/api/reveal', { json: { target: 'models' } }).catch(() => {}));

    $('#btnRescan').addEventListener('click', async () => {
      if (DEMO) { toast('演示模式', 'ok'); return; }
      try {
        await api('/api/models?rescan=1');
        toast('已重新扫描 models/', 'ok');
        tick();
      } catch (e) { toast('扫描失败：' + e.message, 'err'); }
    });

    $('#presetChips').addEventListener('click', (ev) => {
      const del = ev.target.closest('[data-preset-del]');
      if (del) {
        ev.stopPropagation();
        const name = del.dataset.presetDel;
        if (!confirm('删除预设「' + name + '」？')) return;
        api('/api/presets', { json: { action: 'delete', name: name } })
          .then((d) => { S.presets = d.presets; renderPresets(); toast('已删除', 'ok'); })
          .catch((e) => toast('删除失败：' + e.message, 'err'));
        return;
      }
      const bi = ev.target.closest('[data-preset-builtin]');
      if (bi) { applyBuiltin(bi.dataset.presetBuiltin); return; }
      const cu = ev.target.closest('[data-preset]');
      if (cu) applyPreset(cu.dataset.preset);
    });

    $('#btnSavePreset').addEventListener('click', async () => {
      if (DEMO) { toast('演示模式', 'ok'); return; }
      const name = await askName('保存为预设', '例如：长文 128K', '');
      if (!name) return;
      try {
        const d = await api('/api/presets', {
          json: { action: 'save', name: name, cfg: { sampling: draft.sampling, launch: draft.launch } },
        });
        if (!d.ok) { toast(d.name || '保存失败', 'err'); return; }
        S.presets = d.presets;
        activePreset = d.name;
        renderPresets();
        toast('已保存预设「' + d.name + '」', 'ok');
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
    });

    $('#btnReset').addEventListener('click', async () => {
      if (DEMO) { toast('演示模式', 'ok'); return; }
      try {
        const d = await api('/api/reset', { json: { id: S.current.id } });
        S.cfg = d.cfg;
        draft.sampling = Object.assign({}, d.cfg.sampling);
        draft.launch = Object.assign({}, d.cfg.launch);
        builtFor = '';
        renderRuntime(); renderSampling();
        renderPresets(); markDirty();
        toast('已恢复默认值', 'ok');
      } catch (e) { toast('恢复失败：' + e.message, 'err'); }
    });

    $('#btnExport').addEventListener('click', async () => {
      if (DEMO) { toast('演示模式', 'ok'); return; }
      try {
        const d = await api('/api/export?format=json');
        modal('配置 JSON', d.text);
      } catch (e) { toast('导出失败：' + e.message, 'err'); }
    });

    $('#btnCopyCmd').addEventListener('click', () => {
      modal('llama-server 命令行', S.status.cmdline || '（还没有启动过，先点「启动」）');
    });

    $('#btnCopyLog').addEventListener('click', () => { copy($('#logBox').textContent); toast('已复制', 'ok'); });
    $('#btnOpenLog').addEventListener('click', () => {
      api('/api/reveal', { json: { target: 'log' } }).catch(() => {});
    });
    $('#logAuto').addEventListener('change', (e) => { if (e.target.checked) loadLogs(); });

    $('#setupSave').addEventListener('click', async () => {
      const p = $('#setupInput').value.trim();
      if (!p) { toast('请输入文件夹路径', 'err'); return; }
      if (DEMO) { toast('演示模式', 'ok'); return; }
      try {
        const d = await api('/api/settings', { json: { llama_dir: p } });
        if (!d.ok) { toast(d.error || '路径无效', 'err'); return; }
        S = d.snapshot;
        builtFor = '';
        toast('已设置 llama.cpp 目录', 'ok');
        render();
      } catch (e) { toast('设置失败：' + e.message, 'err'); }
    });

    const brand = $('#brandBtn');
    brand.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const menu = $('#brandMenu');
      const willOpen = menu.hidden;
      menu.hidden = !willOpen;
      brand.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });
    document.addEventListener('click', (ev) => {
      if (!$('#brandMenu').hidden && !ev.target.closest('#brandMenu') && !ev.target.closest('#brandBtn')) {
        $('#brandMenu').hidden = true;
        brand.setAttribute('aria-expanded', 'false');
      }
      if (openSel && !ev.target.closest('.sel') && !ev.target.closest('.sel-pop')) closeSel();
    });
    $('#brandMenu').addEventListener('click', (ev) => {
      const b = ev.target.closest('button[data-cmd]');
      if (!b) return;
      $('#brandMenu').hidden = true;
      brand.setAttribute('aria-expanded', 'false');
      const cmd = b.dataset.cmd;
      if (cmd === 'home') setTab('dashboard');
      if (cmd === 'float') openFloat(true);
      if (cmd === 'logs') setTab('logs');
      if (cmd === 'folder') api('/api/reveal', { json: { target: 'work' } }).catch(() => {});
      if (cmd === 'about') aboutModal();
    });

    $('#modalClose').addEventListener('click', closeModal);
    $('#modalCancel').addEventListener('click', closeModal);
    $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
    $('#modalOk').addEventListener('click', () => {
      const m = $('#modal');
      if (m.dataset.mode === 'name') {
        const v = $('#modalInput').value.trim();
        if (!v) { toast('名称不能为空', 'err'); return; }
        const r = m._resolve;
        m._resolve = null;
        m.hidden = true;
        if (r) r(v);
      } else {
        copy(m.dataset.copy || '');
        toast('已复制', 'ok');
        closeModal();
      }
    });
    $('#modalInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#modalOk').click(); });

    document.addEventListener('keydown', (e) => {
      if (e.altKey && e.key.toLowerCase() === 's') { e.preventDefault(); toggleRun(); }
      if (e.altKey && e.key.toLowerCase() === 'r') { e.preventDefault(); $('#btnRestart').click(); }
      if (e.key === 'Escape') { closeModal(); closeSel(); $('#brandMenu').hidden = true; }
    });

    window.addEventListener('resize', () => {
      moveInk();
      closeSel();
      if (tab === 'dashboard') drawMainChart();
    });
    $('#content').addEventListener('scroll', closeSel, true);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  }

  function closeModal() {
    const m = $('#modal');
    m.hidden = true;
    if (m._resolve) { const r = m._resolve; m._resolve = null; r(null); }
  }

  function toggleRun() {
    if (!S) return;
    const st = S.status.stage;
    if (st === 'running' || st === 'starting') serverAction('stop');
    else serverAction('start', S.current && S.current.id);
  }

  async function openFloat(mini) {
    if (DEMO) { toast('演示模式', 'ok'); return; }
    try {
      const d = await api('/api/window', { json: { mini: mini !== false, topmost: true } });
      toast(d.ok ? '已打开悬浮窗（置顶）' : '无法打开窗口', d.ok ? 'ok' : 'err');
    } catch (e) { toast('打开失败：' + e.message, 'err'); }
  }

  /* 表单事件：滑块 / 步进 / 文本 / 开关 */
  function onInput(ev) {
    const el = ev.target;
    const holder = el.closest('[data-ns]');
    if (!holder) return;
    const ns = holder.dataset.ns;
    const k = el.dataset.k;
    if (!ns || !k) return;
    let v = el.type === 'checkbox' ? el.checked : el.value;

    if (el.type === 'range') {
      paintRange(el);
      $$('[data-ns="' + ns + '"][data-k="' + k + '"]').forEach((sib) => {
        if (sib !== el && sib.type === 'number') sib.value = v;
      });
    } else if (el.type === 'number') {
      const spec = specOf(ns, k);
      if (spec && v !== '' && !isNaN(parseFloat(v))) {
        const step = spec.step || (spec.kind === 'int' ? 1 : 0.01);
        const dec = String(step).split('.')[1];
        v = dec ? parseFloat(v).toFixed(dec.length) : String(Math.round(parseFloat(v)));
      }
      $$('[data-ns="' + ns + '"][data-k="' + k + '"]').forEach((sib) => {
        if (sib !== el && sib.type === 'range') { sib.value = v; paintRange(sib); }
      });
    } else if (el.type === 'checkbox') {
      const t = el.closest('.switch') && el.closest('.switch').querySelector('.sw-txt');
      if (t) t.textContent = v ? '开启' : '关闭';
    }

    setValue(ns, k, v);
    if (ns === 'launch') renderBudget();
  }

  function onClickControls(ev) {
    const selBtn = ev.target.closest('.sel-btn');
    if (selBtn) { openSelect(selBtn.closest('.sel')); return; }

    const opt = ev.target.closest('.sel-opt');
    if (opt && openSel) {
      const ns = openSel.dataset.ns, k = openSel.dataset.k;
      setValue(ns, k, opt.dataset.v);
      openSel.dataset.v = opt.dataset.v;
      const spec = specOf(ns, k);
      openSel.querySelector('.sel-val').textContent = spec ? optLabel(spec, opt.dataset.v) : opt.textContent;
      closeSel();
      if (ns === 'launch') renderBudget();
      return;
    }

    const stepBtn = ev.target.closest('.step button');
    if (stepBtn) {
      const holder = stepBtn.closest('[data-ns]');
      const ns = holder.dataset.ns, k = holder.dataset.k;
      const spec = specOf(ns, k);
      const input = holder.querySelector('input[type="number"]');
      const st = parseFloat(spec.step || (spec.kind === 'int' ? 1 : 0.01));
      let v = parseFloat(input.value);
      if (isNaN(v)) v = parseFloat(spec.default || '0');
      v += st * parseInt(stepBtn.dataset.step, 10);
      if (spec.lo !== undefined) v = Math.max(spec.lo, v);
      if (spec.hi !== undefined) v = Math.min(spec.hi, v);
      const dec = String(st).split('.')[1];
      v = dec ? v.toFixed(dec.length) : String(Math.round(v));
      input.value = v;
      $$('[data-ns="' + ns + '"][data-k="' + k + '"]').forEach((sib) => {
        if (sib.type === 'range') { sib.value = v; paintRange(sib); }
      });
      setValue(ns, k, v);
      if (ns === 'launch') renderBudget();
    }
  }

  function setTab(name) {
    tab = name;
    $$('#tabs button').forEach((x) => x.classList.toggle('on', x.dataset.tab === name));
    $$('.view').forEach((v) => v.classList.toggle('on', v.dataset.view === name));
    moveInk();
    if (name === 'logs') loadLogs();
    if (name === 'sampling' || name === 'runtime') buildForms();
    if (S) render();
  }

  function moveInk() {
    const b = $('#tabs button.on');
    const ink = $('#tabInk');
    if (!b || !ink) return;
    ink.style.width = b.offsetWidth + 'px';
    ink.style.transform = 'translateX(' + (b.offsetLeft - 4) + 'px)';
  }

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
    } else fallbackCopy(text);
  }
  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) { /* ignore */ }
    ta.remove();
  }

  /* ---------------- 演示数据（?demo=1 或直接打开文件时用） ---------------- */
  const DEMO_STYLES = {
    gen: { temp: '0.7', top_p: '0.95', top_k: '20', min_p: '0.05', presence: '0.5', repeat: '1.08', dry_mult: '0.8' },
    code: { temp: '0.6', top_p: '0.95', top_k: '20', min_p: '0.05', presence: '0.3', repeat: '1.05', dry_mult: '0.5' },
    prompt: { temp: '1.0', top_p: '0.97', top_k: '40', min_p: '0.03', presence: '0.4', repeat: '1.06', dry_mult: '0.8' },
    strict: { temp: '0.6', top_p: '0.80', top_k: '20', min_p: '0.05', presence: '0.3', repeat: '1.05', dry_mult: '0.5' },
  };

  function demoSnapshot() {
    const now = Math.floor(Date.now() / 1000);
    const hist = [];
    for (let i = 160; i >= 0; i--) {
      const tps = Math.max(0, 34 + Math.sin(i / 9) * 6 + Math.sin(i / 3.1) * 2.5 - (i < 5 ? i * 6 : 0));
      hist.push([now - i, 3120 + Math.sin(i / 22) * 90, tps, Math.round(18000 + (160 - i) * 46)]);
    }
    const models = [
      { id: 'models\\big\\Qwen3-35B-A3B-Q6_K.gguf', label: 'Qwen3·35B-A3B', spec: '35B-A3B', quant: 'Q6_K',
        size: 23514746272, dir: 'big', has_mmproj: true, has_nextn: true, arch: 'qwen3moe',
        n_layers: 41, n_expert: 256, n_ctx_train: 262144, selected: true, running: true },
      { id: 'models\\ud-q4km\\Qwen3-35B-A3B-UD-Q4_K_M.gguf', label: 'Qwen3·UD·Q4_K_M', spec: '35B-A3B', quant: 'Q4_K_M',
        size: 22663387424, dir: 'ud-q4km', has_mmproj: true, has_nextn: true, arch: 'qwen3moe',
        n_layers: 41, n_expert: 256, n_ctx_train: 262144, selected: false, running: false },
    ];
    const launch = { ctx: '128', kv: 'auto', ngl: '999', ot: 'exps=CPU', fa: 'on', fit: 'off', fitt: '1024',
      threads: '16', batch: '2048', ubatch: '512', cache_ram: '8192', keep: '4096', kv_offload: true,
      moe_cache: '0', spec: 'draft-mtp', spec_n_max: '2', mmproj: true, metrics: true, alias: 'local-model', np: 'auto' };
    const sampling = { temp: '0.7', top_p: '0.95', top_k: '20', min_p: '0.05', presence: '0.5', repeat: '1.08',
      repeat_n: '256', dry_mult: '0.8', dry_base: '1.75', dry_len: '2', dry_n: '512', xtc_prob: '0',
      xtc_thresh: '0.1', typical_p: '1', budget: '512', seed: '-1', sampler_order: 'rec', sty: 'gen' };
    return {
      ok: true, version: '2.0.0', ts: now,
      status: { stage: 'running', pid: 21488, model: models[0].id, ctx: '128K', kv: 'q8_0',
        started_at: now - 640, ready_at: now - 615, stage_at: now - 640, error: '', warmup_ms: 23800, cmd: [],
        cmdline: 'llama-server.exe -m models\\big\\Qwen3-35B-A3B-Q6_K.gguf --mmproj ... -c 131072 -ngl 999 -ot exps=CPU -fa on -fit off -t 16 -b 2048 -ub 512 --cache-type-k q8_0 --cache-type-v q8_0 --cache-ram 8192 --spec-type draft-mtp --spec-draft-n-max 2 --reasoning on --reasoning-budget 512 --temp 0.7 --top-p 0.95 --top-k 20 --min-p 0.05 --presence-penalty 0.5 --repeat-penalty 1.08 --repeat-last-n 256 --dry-multiplier 0.8 --dry-base 1.75 --dry-allowed-length 2 --dry-penalty-last-n 512 --jinja --host 127.0.0.1 --port 8080 --metrics' },
      current: models[0], models: models,
      cfg: { sampling: sampling, launch: launch, kv_effective: 'q8_0', id: models[0].id },
      plan: { ctx_tokens: 131072, kv: 'q8_0', need_mib: 4600, vram_total: 8151, vram_free: 5031, vram_used: 3120, vram_util: 22 },
      live: { health: 'ok', tps: 33.4, prefill_tps: 612, ctx_used: 24180, ctx_cap: 131072, req_processing: 1,
        req_deferred: 0, spec_draft: 4120, spec_accepted: 3088, spec_rate: 75, prompt_tokens: 182400,
        predicted_tokens: 42100, build_info: 'b10775', n_ctx: 131072, total_slots: 4, history: hist,
        gpu: { name: 'NVIDIA GeForce RTX 5060 Laptop GPU', driver: '617.42', total: 8151, used: 3120, util: 22 } },
      specs: DEMO_SPECS(),
      paths: { llama_dir: 'D:\\llama.cpp', models_dir: 'D:\\llama.cpp\\models', log: 'state\\llama-server.log',
        port: 8770, llama_port: 8080, exe: 'D:\\llama.cpp\\llama-server.exe', frozen: false },
      presets: { '长文 128K': { sampling: sampling, launch: launch } },
      setup: { needs_llama_dir: false },
      ui: {},
    };
  }

  function DEMO_SPECS() {
    return {
      sampling: [
        { key: 'temp', label: '温度', kind: 'float', lo: 0, hi: 3, step: .05, hint: '越高越发散。对话与写作常用 0.6–0.8' },
        { key: 'top_p', label: 'Top-P', kind: 'float', lo: 0, hi: 1, step: .01, hint: '核采样：只保留累计概率前 P 的候选' },
        { key: 'top_k', label: 'Top-K', kind: 'int', lo: 0, hi: 200, step: 1, hint: '每步只在概率最高的 K 个里挑，0 = 不限' },
        { key: 'min_p', label: 'Min-P', kind: 'float', lo: 0, hi: 1, step: .01, hint: '低于「最大概率 × Min-P」的候选直接丢弃' },
        { key: 'presence', label: '存在惩罚', kind: 'float', lo: -2, hi: 2, step: .05, hint: '对已出现过的词追加惩罚，抑制复读' },
        { key: 'repeat', label: '重复惩罚', kind: 'float', lo: 1, hi: 2, step: .01, hint: '> 1 才生效；越大越强' },
        { key: 'repeat_n', label: '惩罚窗口', kind: 'int', lo: 1, hi: 8192, step: 64, hint: '回看多少个 token' },
        { key: 'dry_mult', label: 'DRY 倍率', kind: 'float', lo: 0, hi: 4, step: .05, hint: 'DRY 专门抑制成句复读。0 = 关闭' },
        { key: 'dry_base', label: 'DRY base', kind: 'float', lo: 1, hi: 3, step: .05, hint: '惩罚随重复长度指数增长的底数' },
        { key: 'dry_len', label: 'DRY 允许长度', kind: 'int', lo: 1, hi: 16, step: 1, hint: '不超过该长度的重复视为正常' },
        { key: 'dry_n', label: 'DRY 窗口', kind: 'int', lo: 0, hi: 8192, step: 64, hint: 'DRY 回看窗口；不能为负' },
        { key: 'xtc_prob', label: 'XTC 概率', kind: 'float', lo: 0, hi: 1, step: .05, hint: 'XTC 主动避开最高概率 token' },
        { key: 'xtc_thresh', label: 'XTC 阈值', kind: 'float', lo: 0, hi: 1, step: .01, hint: '只对概率低于该值的候选生效' },
        { key: 'typical_p', label: 'Typical-P', kind: 'float', lo: 0, hi: 1, step: .01, hint: '局部典型性采样；1.0 = 关闭' },
        { key: 'budget', label: '思考预算', kind: 'int', lo: 0, hi: 8192, step: 1, hint: '思考链的 token 上限；0 = 不限' },
        { key: 'seed', label: '随机种子', kind: 'int', lo: -1, hi: 2147483647, step: 1, hint: '-1 = 每次随机' },
        { key: 'sampler_order', label: '采样器顺序', kind: 'enum', options: [['rec', '推荐'], ['def', 'llama 默认']], hint: '推荐：惩罚与 DRY 放在温度之后' },
        { key: 'sty', label: '采样风格', kind: 'enum', options: [['gen', '通用'], ['code', '代码'], ['prompt', '写提示词'], ['strict', '严谨']], hint: '一次套用一组采样值' },
      ],
      launch: [
        { key: 'ctx', label: '上下文长度', kind: 'enum', options: [['auto', '自动'], ['8', '8K'], ['16', '16K'], ['32', '32K'], ['64', '64K'], ['128', '128K'], ['256', '256K']], hint: '启动时一次性预留 KV 显存' },
        { key: 'kv', label: 'KV 缓存精度', kind: 'enum', options: [['auto', '自动'], ['f16', 'f16'], ['q8_0', 'q8_0'], ['bf16', 'bf16']], hint: '自动：≥128K 用 q8_0，以下 f16' },
        { key: 'ngl', label: 'GPU 层数', kind: 'int', lo: 0, hi: 1000, step: 1, hint: '999 = 全部' },
        { key: 'ot', label: '张量覆盖', kind: 'text', placeholder: '例如 exps=CPU', hint: '按张量名覆盖存放位置；留空 = 不覆盖' },
        { key: 'fa', label: 'Flash Attention', kind: 'enum', options: [['on', '开'], ['auto', '自动'], ['off', '关']], hint: '开启可省显存、加速长上下文' },
        { key: 'fit', label: '显存自动适配', kind: 'enum', options: [['off', '关'], ['on', '开']], hint: '自动下调未指定的参数以塞进显存' },
        { key: 'fitt', label: '适配保留余量', kind: 'int', lo: 0, hi: 8192, step: 256, hint: '自动适配时留出多少 MiB' },
        { key: 'threads', label: 'CPU 线程', kind: 'int', lo: 1, hi: 128, step: 1, hint: '一般取物理核心数' },
        { key: 'batch', label: '批大小', kind: 'int', lo: 64, hi: 8192, step: 64, hint: '影响 prompt 处理速度与显存峰值' },
        { key: 'ubatch', label: '微批大小', kind: 'int', lo: 32, hi: 2048, step: 32, hint: '显存不足时优先降它' },
        { key: 'cache_ram', label: '缓存内存', kind: 'int', lo: 0, hi: 65536, step: 512, hint: 'prompt 缓存上限（MiB）' },
        { key: 'keep', label: '保留起始 token', kind: 'int', lo: 0, hi: 32768, step: 512, hint: '截断时保留开头的系统提示词' },
        { key: 'kv_offload', label: 'KV 放显存', kind: 'bool', hint: '关闭后 KV 放到内存' },
        { key: 'moe_cache', label: '专家显存缓存', kind: 'int', lo: 0, hi: 8192, step: 128, hint: '0 = 关闭' },
        { key: 'spec', label: '投机解码', kind: 'enum', options: [['draft-mtp', 'MTP 自投机'], ['none', '关闭']], hint: '模型自带 MTP/EAGLE 头时才有收益' },
        { key: 'spec_n_max', label: '投机草稿数', kind: 'int', lo: 1, hi: 8, step: 1, hint: '每次预猜的 token 数' },
        { key: 'mmproj', label: '多模态投影', kind: 'bool', hint: '有 mmproj 时支持图片输入' },
        { key: 'metrics', label: '暴露监控指标', kind: 'bool', hint: '开启 /metrics' },
        { key: 'alias', label: '模型别名', kind: 'text', placeholder: 'local-model', hint: 'OpenAI 接口里 model 字段的名字' },
        { key: 'np', label: '并发槽位', kind: 'enum', options: [['auto', '自动'], ['1', '1'], ['2', '2'], ['4', '4']], hint: '建议保持自动' },
      ],
      styles: ['gen', 'code', 'prompt', 'strict'],
      style_labels: { gen: '通用', code: '代码', prompt: '写提示词', strict: '严谨' },
    };
  }

  function demoLog() {
    return [
      'llama_model_loader: loaded meta data with 32 key-value pairs',
      'llama_model_loader: - type q6_K : 40 tensors',
      'print_info: arch = qwen3moe',
      'print_info: n_layer = 41',
      'print_info: n_expert = 256',
      'load_tensors: offloading 40 repeating layers to GPU',
      'llama_kv_cache: CUDA0 KV buffer size = 1024.00 MiB',
      'main: model loaded',
      'main: server is listening on 127.0.0.1:8080',
      'srv  update_slots: all slots are idle',
    ].map(esc).join('\n');
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    bind();
    if (MINI) {
      const bar = document.createElement('div');
      bar.className = 'mini-actions';
      bar.innerHTML = '<button class="btn primary" id="btnMiniToggle">启动</button>'
        + '<button class="btn ghost" id="btnExpand">展开</button>';
      $('.main').appendChild(bar);
      $('#btnMiniToggle').addEventListener('click', toggleRun);
      $('#btnExpand').addEventListener('click', () => openFloat(false));
    }
    const qt = qs.get('tab');
    if (qt && !MINI) setTab(qt); else moveInk();
    tick();
    setInterval(() => { if (!document.hidden) tick(); }, 1000);
    setInterval(() => {
      if (tab === 'logs' && $('#logAuto').checked && !document.hidden) loadLogs();
    }, 2500);
    window.addEventListener('resize', () => Glass && Glass.refresh && Glass.refresh());
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
