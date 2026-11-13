/* DockerUpdate v2.0 · 更新中心与名称备注（管理增强视图见 console.js） */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const icon = (id) => `<svg class="ic"><use href="#${id}"/></svg>`;

let state = null;
let filter = 'all';
let query = '';
let sortBy = 'update';
let token = localStorage.getItem('du_token') || sessionStorage.getItem('du_token') || '';
let pendingConfirm = null;
let loadTimer = null;
let lastJobError = '';
let paused = localStorage.getItem('du_paused') === '1';
const expanded = new Set();

/* ---------- 防止浏览器把登录账号自动填进搜索框 ----------
   公网 / 新域名首次登录后，Chrome、Edge 与各类密码管理器会把保存的
   username 自动填到页面上「看起来像输入框」的控件。若这次填充触发了
   input 事件，搜索词就会变成 admin —— 此时任何筛选（含「可更新」）
   都匹配不到容器，列表显示 0 条，看起来就像按钮坏了。
   策略：只有用户真正与搜索框交互过，input 的值才被采纳；其余时刻
   （启动、登录后、bfcache 恢复、延迟自动填充）一律清空并重置筛选。 */
function markSearchTouched(e) { e.target.dataset.duTouched = '1'; }
function bindSearchGuards() {
  $$('input[type="search"]').forEach((el) => {
    ['keydown', 'paste', 'compositionstart', 'pointerdown', 'focus'].forEach((ev) =>
      el.addEventListener(ev, markSearchTouched));
  });
}
function clearSearchAutofill() {
  let cleared = false;
  $$('input[type="search"]').forEach((el) => {
    if (el.dataset.duTouched === '1') return;
    if (el.value) { el.value = ''; cleared = true; }
  });
  if (!cleared) return;
  query = '';
  if (window.duResetConsoleSearch) window.duResetConsoleSearch();
  if (state) render(state);
}
function scheduleAutofillClean() {
  // 浏览器自动填充常在 DOMContentLoaded 之后异步完成，故多点重试
  [0, 120, 400, 1000, 2000].forEach((ms) => setTimeout(clearSearchAutofill, ms));
}
bindSearchGuards();
scheduleAutofillClean();
window.addEventListener('pageshow', scheduleAutofillClean);
window.addEventListener('load', scheduleAutofillClean);

/* ---------- 会话 ---------- */
function setToken(t, remember) {
  token = t || '';
  localStorage.removeItem('du_token');
  sessionStorage.removeItem('du_token');
  if (token) (remember ? localStorage : sessionStorage).setItem('du_token', token);
}
function clearToken() { setToken(''); }
function isRemembered() { return !!localStorage.getItem('du_token'); }

/* ==================== 全局操作进度条 ====================
   任何请求只要超过 1 秒仍未返回，就自动弹出磨砂玻璃进度条：
   - 短操作（<1s）完全无感，不闪屏；
   - 并发请求共用一个浮层，显示「还有 N 个操作」；
   - 进度条平滑推进到 92% 等待服务端，返回后才冲到 100% 淡出；
   - 阶段文案按时间轮播，安装 / 卸载有专属阶段清单。
   说明：服务端是一次请求一次响应，无法回推真实字节进度，
   因此百分比是「处理状态示意」，阶段名标明实际在等的环节，不伪造完成。
   ======================================================== */
const duProgress = (() => {
  const DELAY = 1000;               // 超过 1 秒才显示
  let active = [];                  // 当前未完成的操作
  let el = null, tick = null, shownAt = 0;
  let cur = null;                   // 当前展示的操作
  // v7.0.1：后台运行明细（run trace）
  let traceId = '', traceLines = [], traceSeq = 0, traceTimer = null;
  let traceDone = false, traceStart = 0;

  const GENERIC = ['正在提交请求…', '服务端处理中…', '等待响应…', '仍在等待，请勿关闭页面…'];
  function stagesOf(h) { return (h && h.stages && h.stages.length) ? h.stages : GENERIC; }

  function mount() {
    if (el) return el;
    el = document.createElement('div');
    el.className = 'du-prog';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML = `<div class="du-prog-card glass">
      <div class="du-prog-top">
        <span class="du-prog-spin"><svg class="ic"><use href="#i-refresh"/></svg></span>
        <div class="du-prog-title"><b id="duProgTitle">正在处理…</b>
          <em id="duProgSub">正在执行操作，请勿关闭页面</em></div>
        <span class="du-prog-time" id="duProgTime">0.0s</span>
      </div>
      <div class="du-prog-track"><div class="du-prog-fill" id="duProgFill"></div></div>
      <div class="du-prog-meta"><span id="duProgStage">准备中…</span>
        <span id="duProgPct">0%</span></div>
      <div class="du-prog-detail hidden" id="duProgDetail"></div>
      <ul class="du-prog-stages" id="duProgStages"></ul>
      <div class="du-prog-more hidden" id="duProgMore"></div>
      <div class="du-prog-trace">
        <div class="du-prog-trace-hd">
          <button type="button" class="du-prog-trace-btn" id="duProgTraceToggle" aria-expanded="true">
            后台运行明细 <span class="du-prog-trace-count" id="duProgTraceCount">0</span></button>
          <span class="du-prog-trace-pad"></span>
          <button type="button" class="du-prog-trace-btn ghost" id="duProgTraceCopy">复制</button>
          <button type="button" class="du-prog-trace-btn ghost" id="duProgTraceHide">收起</button>
        </div>
        <ol class="du-prog-lines" id="duProgLines"></ol>
      </div>
      <p class="du-prog-note" id="duProgNote">进度为处理状态示意；实际操作完成后会自动关闭，请勿刷新页面。</p>
    </div>`;
    el.addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b || !el) return;
      if (b.id === 'duProgTraceToggle') {
        el.classList.toggle('trace-collapsed');
        b.setAttribute('aria-expanded', String(!el.classList.contains('trace-collapsed')));
      } else if (b.id === 'duProgTraceCopy') copyTrace();
      else if (b.id === 'duProgTraceHide') forceFinish();
    });
    document.body.appendChild(el);
    return el;
  }

  function paint() {
    if (!el || !cur) return;
    const stages = stagesOf(cur);
    const secs = (Date.now() - shownAt) / 1000;
    const real = cur.real || null;
    // 有 SSE 真实进度时完全采用服务端数值；没有时退回「平滑逼近 92%」的等待示意
    const pct = real ? Math.max(0, Math.min(100, Number(real.pct) || 0))
                     : Math.min(92, 92 * (1 - Math.exp(-secs / 9))) + 4;
    const idx = real ? Math.min(stages.length - 1, Math.floor(pct / 100 * stages.length))
                     : Math.min(stages.length - 1, Math.floor(secs / 3.2));
    $('#duProgTitle', el).textContent = cur._traceKeep
      ? '后台运行明细' : (cur.label || '正在处理…');
    $('#duProgSub', el).textContent = cur._traceKeep
      ? '逐行显示服务端实际执行 · 可随时收起' : (real
      ? '后台任务执行中 · 关闭页面也会继续' : (cur.immediate
      ? '正在执行操作，请勿关闭页面' : '操作超过 1 秒，正在等待服务端响应'));
    $('#duProgTime', el).textContent = secs.toFixed(1) + 's';
    $('#duProgFill', el).style.width = pct.toFixed(1) + '%';
    $('#duProgPct', el).textContent = Math.floor(pct) + '%';
    $('#duProgStage', el).textContent = '· ' + (real ? (real.stage || '处理中') : stages[idx]);
    const detail = $('#duProgDetail', el);
    const bits = real ? [real.detail, real.speed].filter(Boolean) : [];
    detail.classList.toggle('hidden', bits.length === 0);
    detail.textContent = bits.join(' · ');
    $('#duProgNote', el).textContent = cur._traceKeep
      ? '明细来自服务端运行日志（HTTP / Docker API / 业务阶段），可按 seq 增量拉取。'
      : (real
      ? '实时进度由服务端 SSE 推送；反代掐断长连接时自动改轮询，任务在后台继续。'
      : '进度为处理状态示意；实际操作完成后会自动关闭，请勿刷新页面。');
    const list = $('#duProgStages', el);
    // 阶段清单只在内容 / 当前阶段变化时重建；原来每 120ms 重写一次，
    // 浮层里的磨砂层与阴影随之重绘，视觉上会带着周边元素一起闪。
    const stageSig = stages.join('\u0001') + '\u0001' + idx;
    if (list.dataset.sig !== stageSig) {
      list.innerHTML = stages.map((s, i) =>
        `<li class="${i < idx ? 'done' : (i === idx ? 'now' : '')}"><i></i>${esc(s)}</li>`).join('');
      list.dataset.sig = stageSig;
    }
    const more = $('#duProgMore', el);
    const n = active.length - 1;
    more.classList.toggle('hidden', n <= 0);
    if (n > 0) more.textContent = `同时还有 ${n} 个操作在进行中`;
  }

  /* ---------- v7.0.1 · 后台运行明细 ---------- */
  function addLines(lines) {
    const list = el && $('#duProgLines', el);
    if (!list) return;
    let added = 0;
    (lines || []).forEach((l) => {
      if (!l || Number(l.seq || 0) <= traceSeq) return;
      traceSeq = Math.max(traceSeq, Number(l.seq));
      traceLines.push(l); added++;
      const li = document.createElement('li');
      li.className = 'lv-' + String(l.level || 'info');
      const extra = [l.code, l.dur ? (l.dur + 'ms') : ''].filter(Boolean).join(' · ');
      li.innerHTML = '<time>' + esc(l.ts) + '</time><span class="sc">' + esc(l.scope) +
        '</span><span class="ms">' + esc(l.msg) + '</span><em>' + esc(extra) + '</em>';
      list.appendChild(li);                     // 只追加，不重建，避免整页闪
    });
    while (list.children.length > 200) list.removeChild(list.firstChild);
    if (added) {
      list.scrollTop = list.scrollHeight;
      const c = $('#duProgTraceCount', el); if (c) c.textContent = String(traceLines.length);
    }
  }
  function copyTrace() {
    const text = traceLines.map((l) =>
      [l.ts, (l.scope || '') + ' '.repeat(Math.max(1, 12 - String(l.scope || '').length)),
       l.msg, [l.code, l.dur ? (l.dur + 'ms') : ''].filter(Boolean).join(' · ')]
        .join(' ').trimEnd()).join('\n');
    const done = () => { if (typeof toast === 'function') toast('已复制 ' + traceLines.length + ' 行运行明细', 'ok'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, () => {});
    } else {
      const ta = document.createElement('textarea'); ta.value = text;
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (_) {}
      ta.remove();
    }
  }
  function stopTracePoll() { if (traceTimer) { clearInterval(traceTimer); traceTimer = null; } }
  function forceFinish() {
    stopTracePoll(); traceDone = true;
    active.filter((h) => h._traceKeep).forEach((h) => finish(h, true, true));
    if (!active.length && el) { el.classList.remove('on', 'err'); clearInterval(tick); tick = null; cur = null; }
  }
  function startTracePoll() {
    if (traceTimer || !traceId) return;
    traceStart = Date.now();
    traceTimer = setInterval(async () => {
      if (!traceId || traceDone || (Date.now() - traceStart) > 300000) { stopTracePoll(); return; }
      try {
        const headers = {}; if (token) headers['Authorization'] = 'Bearer ' + token;
        const r = await fetch('/api/trace?id=' + encodeURIComponent(traceId) + '&after=' + traceSeq, { headers });
        const d = await r.json();
        if (d && d.run) {
          if (d.run.lines && d.run.lines.length) addLines(d.run.lines);
          if (d.run.finished && !traceDone) {
            traceDone = true; stopTracePoll();
            setTimeout(forceFinish, 2200);
          }
        }
      } catch (_) { /* 网络抖动保留轮询 */ }
    }, 450);
  }
  function attach(runId, o) {
    o = o || {};
    traceId = runId || ''; traceLines = []; traceSeq = 0; traceDone = false;
    if (el) {
      const list = $('#duProgLines', el); if (list) list.innerHTML = '';
      const c = $('#duProgTraceCount', el); if (c) c.textContent = '0';
      el.classList.remove('trace-collapsed');
    }
    if (o.background) {
      let h = active.filter((x) => x._traceKeep)[0];
      if (!h) {
        h = begin('后台任务执行中', ['读取 Docker 状态', '执行后台任务', '写回结果'], true);
        h._traceKeep = true;
      }
      cur = h;
      if (!el || !el.classList.contains('on')) show(h); else paint();
    }
    if (!o.async && ((el && el.classList.contains('on')) || o.background)) startTracePoll();
  }

  function show(h) {
    if (el && el.classList.contains('on')) return;   // 已经显示了就只换标题
    cur = (h && active.includes(h)) ? h : (active[0] || null);
    if (!cur) return;
    mount();
    el.classList.remove('err');
    el.classList.add('on');
    shownAt = Date.now();
    paint();
    clearInterval(tick);
    tick = setInterval(paint, 200);
  }

  function begin(label, stages, immediate) {
    const h = { label: label || '正在处理…', stages: stages || null, immediate: !!immediate,
                id: Math.random().toString(36).slice(2), timer: null,
                started: Date.now() };
    active.push(h);
    // 每个操作各自计时：并发的短请求不会把长操作的进度条提前唤醒。
    // immediate（安装等用户明确要看进度的操作）从点下按钮起就显示。
    h.timer = setTimeout(() => {
      h.timer = null;
      if (active.includes(h)) show(h);
    }, immediate ? 0 : DELAY);
    return h;
  }

  function finish(h, ok, force) {
    if (!h) return;
    // 后台任务还没结束：POST 已经返回，但进度浮层继续显示明细
    if (h._traceKeep && !force && traceId && !traceDone) { h._reqDone = true; paint(); return; }
    const i = active.indexOf(h);
    if (i < 0) return;                  // 已经收尾过
    active.splice(i, 1);
    clearTimeout(h.timer);              // <1s 就完成了：这个操作不显示
    if (active.length === 0) {
      if (!el || !el.classList.contains('on')) return;
      clearInterval(tick); tick = null;
      const fill = $('#duProgFill', el), pct = $('#duProgPct', el);
      fill.style.width = '100%'; pct.textContent = '100%';
      $('#duProgStage', el).textContent = ok ? '· 操作已完成' : '· 操作结束（请查看提示）';
      el.classList.toggle('err', !ok);
      setTimeout(() => { if (el) { el.classList.remove('on', 'err'); } }, 1100);
      cur = null;
    } else if (cur === h) {
      cur = active[0];
      paint();                          // 累计耗时不重置，只换标题与阶段
    }
  }

  // v2.1-r11：服务端 SSE / 轮询推来的真实进度，覆盖「等待示意」百分比
  function applyJob(ev) {
    if (!ev || !active.length) return;
    const h = active[active.length - 1];
    h.real = Object.assign({}, h.real || {}, ev);
    if (ev.trace_id) traceId = ev.trace_id;
    if (cur && cur !== h) cur = h;
    show(h);                                  // 先 mount，再追加明细行
    if (ev.line) addLines([ev.line]);
    if (Array.isArray(ev.trace)) addLines(ev.trace);
    paint();
  }

  return { begin, finish, applyJob, attach, addLines,
           get visible() { return !!(el && el.classList.contains('on')); } };
})();
window.duProgress = duProgress;

/* ==================== v2.1-r10：磨砂玻璃自定义下拉 ====================
   原生 <select> 的 option 弹层由操作系统绘制（Windows 上是灰底白字方块），
   和整套 WebUI 的磨砂玻璃风格完全割裂。这里把每个 select 包装成自定义下拉：
   · 原生 select 仍保留在 DOM 且可读写 value（所有既有 onchange / .value 逻辑不变）
   · 展开层用同一套 glass 变量、圆角、hover 与选中态
   · MutationObserver 自动覆盖所有动态渲染出来的 select（各视图、弹窗、抽屉） */
const duSelect = (() => {
  let openPop = null;

  function close() {
    if (!openPop) return;
    const pop = openPop;
    pop.classList.remove('on');
    const wrap = pop._duWrap || pop.closest('.du-sel');
    if (wrap) {
      wrap.classList.remove('open');
      if (pop.parentNode !== wrap) wrap.appendChild(pop);   // 从 body 收回原容器
    }
    pop.style.position = ''; pop.style.top = ''; pop.style.bottom = '';
    pop.style.left = ''; pop.style.width = ''; pop.style.maxHeight = '';
    pop.style.overflowY = ''; pop.style.zIndex = '';
    openPop = null;
  }

  function sync(sel) {
    const wrap = sel.closest('.du-sel');
    if (!wrap) return;
    const opt = sel.options[sel.selectedIndex];
    const label = $('.du-sel-text', wrap);
    const full = opt ? (opt.textContent || sel.value) : (sel.value || '请选择');
    if (label) { label.textContent = full; label.title = full; }
    // v6.0：触发器也带完整文案，鼠标悬停不会再只看到被截断的一半
    const trig = $('.du-sel-btn', wrap);
    if (trig) trig.title = full;
    // 展开时浮层挂在 body 上，仍要同步选中态
    const pop = wrap.querySelector('.du-sel-pop') ||
      (openPop && openPop._duWrap === wrap ? openPop : null);
    $$('.du-sel-opt', pop || wrap).forEach(b => b.classList.toggle('active', b.dataset.value === sel.value));
  }

  function enhance(sel) {
    if (sel.dataset.duEnhanced === '1' || sel.closest('.du-sel')) return;
    sel.dataset.duEnhanced = '1';
    const wrap = document.createElement('div');
    wrap.className = 'du-sel' + (sel.classList.contains('input') ? ' du-sel-full' : '');
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.classList.add('du-sel-native');

    const opt = sel.options[sel.selectedIndex];
    const trig = document.createElement('button');
    trig.type = 'button';
    trig.className = 'du-sel-btn';
    trig.innerHTML = '<span class="du-sel-text"></span><span class="du-sel-arrow"></span>';
    wrap.appendChild(trig);

    const pop = document.createElement('div');
    pop.className = 'du-sel-pop glass';
    pop._duWrap = wrap;   // v3.2：展开时挂到 body，避免被父级 overflow / 层叠上下文裁剪遮挡
    Array.from(sel.options).forEach(o => {
      if (o.disabled) return;
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'du-sel-opt';
      b.dataset.value = o.value;
      b.innerHTML = '<span class="du-sel-tick"></span><span class="du-sel-label"></span>';
      $('.du-sel-label', b).textContent = o.textContent || o.value;
      b.onclick = (e) => {
        e.stopPropagation();
        sel.value = o.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        sync(sel);
        close();
      };
      pop.appendChild(b);
    });
    wrap.appendChild(pop);
    sync(sel);

    trig.onclick = (e) => {
      e.stopPropagation();
      if (openPop === pop) { close(); return; }
      close();
      // v3.2：浮层移出工具栏（工具栏的 glass / overflow 会裁剪或压住下拉），
      // 用 fixed 定位到触发器下方；下方空间不够时自动向上展开，保证全部选项可见。
      const r = trig.getBoundingClientRect();
      const below = window.innerHeight - r.bottom - 12;
      const above = r.top - 12;
      const h = Math.min(360, Math.max(below, above));
      pop.style.position = 'fixed';
      pop.style.zIndex = '9999';
      pop.style.left = Math.max(8, r.left) + 'px';
      pop.style.width = Math.max(r.width, 300) + 'px';
      pop.style.maxHeight = h + 'px';
      pop.style.overflowY = 'auto';
      if (below < 220 && above > below) {
        pop.style.bottom = (window.innerHeight - r.top + 6) + 'px';
        pop.style.top = 'auto';
      } else {
        pop.style.top = (r.bottom + 6) + 'px';
        pop.style.bottom = 'auto';
      }
      document.body.appendChild(pop);
      pop.classList.add('on');
      wrap.classList.add('open');
      openPop = pop;
      const a = pop.querySelector('.du-sel-opt.active');
      if (a) a.scrollIntoView({ block: 'nearest' });
    };
    sel.addEventListener('change', () => sync(sel));
  }

  let queued = false;
  function scan(root) {
    $$('select', root || document).forEach(enhance);
  }
  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; scan(document); });
  }
  document.addEventListener('pointerdown', (e) => {
    if (openPop && !e.target.closest('.du-sel') && !e.target.closest('.du-sel-pop')) close();
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  // 页面滚动 / 窗口变化后 fixed 浮层位置会失效，直接收起
  window.addEventListener('scroll', (e) => {
    const t = e.target;
    if (openPop && !(t && t.closest && t.closest('.du-sel-pop'))) close();
  }, true);
  window.addEventListener('resize', () => { if (openPop) close(); });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      scan(document);
      new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
    });
  } else {
    scan(document);
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  }
  return { scan, close };
})();
window.duEnhanceSelects = duSelect.scan;

/* 操作名：按接口与 kind 给出人话标签；安装/卸载给专属阶段 */
const PRO_LABELS = {
  'stack-up': '正在启动 Compose 栈', 'stack-down': '正在停止栈容器',
  'stack-delete': '正在删除 Compose 栈', 'template-deploy': '正在部署应用模板',
  'volume-backup': '正在打包数据卷', 'trivy-scan': '正在扫描镜像漏洞',
  'update-preview': '正在拉取镜像（不重启）', 'create-advanced': '正在创建容器',
  'stack-import': '正在导入 Compose 栈', 'stack-save': '正在保存 YAML',
  'image-update-check': '正在检查镜像更新', 'image-update-pull': '正在更新选中的镜像',
  'image-update-rebuild': '正在更新镜像并重建容器',
};
const CONSOLE_LABELS = {
  'check-all': '正在检测全部镜像更新', 'update': '正在更新容器',
  'image-pull': '正在拉取镜像', 'image-export': '正在导出镜像',
  'backup-import': '正在还原备份', 'container-action': '正在执行容器操作',
};
const PATH_LABELS = {
  '/api/login': '正在登录…', '/api/change-password': '正在修改密码',
  '/api/check-all': '正在检测全部镜像更新', '/api/check': '正在检查更新',
  '/api/check-daily': '正在执行每日巡检', '/api/update': '正在更新容器',
  '/api/rollback': '正在回退版本', '/api/backup': '正在创建版本备份',
  '/api/backup-import': '正在还原备份', '/api/policy': '正在保存更新策略',
  '/api/settings': '正在保存设置', '/api/note': '正在保存名称备注',
  '/api/restore-name': '正在恢复原名', '/api/restore-all-names': '正在批量恢复原名',
  '/api/registry-auth': '正在保存仓库认证',
  '/api/registry': '正在处理镜像仓库请求',
  '/api/pro': '正在处理 Docker 管理操作',
  'mounts-apply': '正在应用目录映射并重建容器',
};
const CHECK_STAGES = ['连接 Docker 守护进程', '逐个拉取镜像索引', '比对镜像 ID 与版本', '写回检测结果'];
const UPDATE_STAGES = ['备份当前容器配置', '拉取新版本镜像', '按原配置重建容器', '校验启动状态', '失败时自动回滚'];
/* v3.3：纯查询类 POST（目录树、设置读取、状态汇总）不弹进度条。
   并发时偶发超过 1s 会弹出「0.1s · 操作已完成 100%」闪一下并抢走焦点。 */
const NO_PROGRESS_PRO = new Set(['dirs', 'state', 'settings', 'creds', 'notify-config', 'tasks',
  'hosts', 'roles', 'api-keys', 'audit-list', 'accel', 'desktop-config', 'overview',
  'container-table', 'stacks', 'stack-read', 'templates', 'notify-test-status',
  'ports', 'ports-check', 'netwatch', 'netwatch-probe', 'netwatch-test',
  'image-updates', 'image-updates-clear', 'image-rebuild-plan',
  'mounts-list', 'mounts-status', 'mounts-plan', 'mounts-dirs', 'mounts-precopy-check']);
const NO_PROGRESS_CONSOLE = new Set(['refresh', 'detail', 'logs', 'container-config']);
function opMeta(path, opts) {
  if (opts.progress === false) return null;
  const isPost = (opts.method || 'GET').toUpperCase() === 'POST';
  if (!isPost && opts.progress !== true) return null;
  const b = opts.body || {};
  const kind = String(b.kind || '');
  if (path === '/api/pro' && (NO_PROGRESS_PRO.has(kind) || !kind)) return null;
  if (path === '/api/console' && NO_PROGRESS_CONSOLE.has(kind)) return null;
  let label = opts.progressLabel || '';
  let stages = opts.progressStages || null;
  if (path === '/api/pro') {
    label = label || PRO_LABELS[kind] || '正在处理 Docker 管理操作';
    if (kind === 'stack-up') stages = stages || ['读取并校验 YAML', '拉取镜像', '创建并启动服务', '校验状态'];
    if (kind === 'template-deploy') stages = stages || ['写 Compose 与 .env', '拉取镜像', '创建并启动服务', '同步桌面图标'];
    if (kind === 'volume-backup') stages = stages || ['创建临时打包容器', '打包卷内容', '写入导出文件'];
  } else if (path === '/api/console') {
    label = label || CONSOLE_LABELS[kind] || '正在执行 Docker 操作';
  } else {
    label = label || PATH_LABELS[path] || '正在处理请求';
    if (!stages && (path === '/api/check-all' || path === '/api/check' || path === '/api/check-daily')) stages = CHECK_STAGES;
    if (!stages && (path === '/api/update' || path === '/api/rollback')) stages = UPDATE_STAGES;
    if (!stages && (path === '/api/backup' || path === '/api/backup-import')) stages = ['读取配置与版本快照', '打包文件', '写入备份目录', '校验归档'];
  }
  return { label, stages };
}

/* ---------- 网络 ---------- */
async function api(path, opts = {}) {
  const meta = opMeta(path, opts);
  const ph = meta ? duProgress.begin(meta.label, meta.stages, opts.progressImmediate === true) : null;
  let ok = false;
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const res = await fetch(path, {
      method: opts.method || 'GET',
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (res.status === 401) {
      clearToken();
      showLogin();
      const err = new Error(data.error || '登录已失效，请重新登录');
      err.auth = true;
      throw err;
    }
    if (res.status === 504) {
      throw new Error('网关响应超时（504）：操作可能仍在后台执行，请稍后刷新查看结果');
    }
    if (!res.ok) throw new Error(data.error || data.message || ('请求失败 ' + res.status));
    ok = res.ok && data.ok !== false;
    // v7.0.1：把本次请求的 run_id 交给进度浮层，实时显示后台执行明细。
    if (data && data.run_id) {
      duProgress.attach(data.run_id, { async: !!data.async, background: !!data.background });
    }
    // v2.1-r11：长操作已在服务端转成后台任务，POST 立即返回。
    // 进度条交给 watchJob() 接管，等 SSE / 轮询拿到终态再 finish。
    if (data && data.async && data.job_id) {
      if (ph) ph._deferred = true;
      data._ph = ph;
      return data;
    }
    return data;
  } finally {
    if (!(ph && ph._deferred)) duProgress.finish(ph, ok);
  }
}

async function act(path, body) {
  try {
    const d = await api(path, { method: 'POST', body });
    if (d.ok === false) { toast(d.error || d.message || '操作被拒绝', 'err'); return; }
    toast(d.message || '操作已提交', 'ok');
    await load();
  } catch (e) {
    if (!e.auth) toast(e.message, 'err');
  }
}

/* v7.0.2：同一时刻只允许一次 /api/state 在飞。
   保存成功后 load() 与 5 秒自动轮询常会几乎同时发起两次全量快照，
   容器多或 Docker 暂时不可用时单次可能 1~2 秒，按钮的 async 流程会被
   拖住不返回，看起来像「点了保存没反应」。这里复用在飞的 promise。 */
let _loadFlight = null;
async function load() {
  if (_loadFlight) return _loadFlight;
  _loadFlight = (async () => {
    try {
      const s = await api('/api/state');
      state = s;
      hideLogin();
      render(s);
      return true;
    } catch (e) {
      if (!e.auth) toast(e.message, 'err');
      return false;
    } finally {
      _loadFlight = null;
    }
  })();
  return _loadFlight;
}

/* ---------- 登录 / 退出 / 改密 ---------- */
function showLogin() { $('#loginMask').classList.remove('hidden'); }
function hideLogin() { $('#loginMask').classList.add('hidden'); }

async function login() {
  const username = $('#loginUser').value.trim();
  const password = $('#loginPwd').value;
  $('#loginErr').textContent = '';
  try {
    const res = await fetch('/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    let d = {};
    try { d = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok || !d.ok) {
      $('#loginErr').textContent = d.error || '登录失败，请重试';
      return;
    }
    setToken(d.token, $('#remember').checked);
    $('#loginPwd').value = '';
    hideLogin();
    scheduleAutofillClean();   // 登录框收起后密码管理器可能才填充
    await load();
    toast('登录成功', 'ok');
  } catch (e) {
    $('#loginErr').textContent = e.message || '无法连接服务';
  }
}

async function logout() {
  try { await api('/api/logout', { method: 'POST', body: {} }); } catch (e) { /* ignore */ }
  clearToken();
  closeDrawers();
  showLogin();
  $('#loginPwd').value = '';
  toast('已退出登录', 'info');
}

async function changePassword() {
  const body = {
    old_password: $('#oldPwd').value,
    new_password: $('#newPwd').value,
    confirm: $('#confirmPwd').value,
  };
  try {
    const d = await api('/api/change-password', { method: 'POST', body });
    setToken(d.token, isRemembered());
    $('#oldPwd').value = $('#newPwd').value = $('#confirmPwd').value = '';
    toast(d.message || '密码已修改', 'ok');
    await load();
  } catch (e) {
    if (!e.auth) toast(e.message, 'err');
  }
}

/* ---------- 提示 ---------- */
function toast(msg, type = 'ok', action = null) {
  const use = type === 'err' ? 'i-close' : (type === 'info' ? 'i-info' : 'i-check');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = icon(use) + '<span>' + esc(msg) + '</span>';
  if (action && action.label && typeof action.onClick === 'function') {
    const btn = document.createElement('button');
    btn.className = 'toast-action';
    btn.textContent = action.label;
    btn.onclick = async () => {
      btn.disabled = true;
      try { await action.onClick(); } catch (e) { /* 撤销失败时保留提示 */ }
      el.remove();
    };
    el.appendChild(btn);
  }
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = '.3s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateX(24px)';
    setTimeout(() => el.remove(), 320);
  }, action ? 8000 : 4200);
}

/* ---------- 渲染 ---------- */
function statCard(label, num, ico, color, value, max) {
  const pct = Math.min(100, Math.round((Number(value) || 0) / Math.max(Number(max) || 1, 1) * 100));
  return `<div class="stat glass ${color}">
    <div class="stat-top"><label>${label}</label><span class="stat-ic">${icon(ico)}</span></div>
    <div class="num">${num}</div><div class="bar"><i style="width:${pct}%"></i></div></div>`;
}

function block(title, arr, cls = '') {
  return `<div class="detail-block ${cls}"><h5>${title}</h5>${
    arr.length ? '<p>' + arr.map(esc).join('<br>') + '</p>' : '<p class="none">无</p>'
  }</div>`;
}

/* v3.9：容器详情不再用一堆卡片，改成分组标题 + 一行一行的紧凑明细，省空间 */
function detailRows(c) {
  const backups = c.backups || [];
  const busy = !!(state && state.runtime.busy);
  const group = (t) => `<div class="dr-group">${esc(t)}</div>`;
  const row = (k, vHtml, mono) => `<div class="dr"><span>${esc(k)}</span><div class="dr-val${mono ? ' mono' : ''}">${vHtml}</div></div>`;
  const lines = (arr) => arr.length ? arr.map(esc).join('<br>') : '<span class="none">无</span>';
  const env = c.env.slice(0, 14).concat(c.env.length > 14 ? ['… 共 ' + c.env.length + ' 项'] : []);
  const labels = Object.entries(c.labels || {}).slice(0, 10).map(([k, v]) => k + ' = ' + v);
  const rbToConfirm = (id) => isConfirming(confirmKey('rollback-to', c.name, id));
  const backupRows = backups.length ? backups.map((b) => `
    <div class="backup-item compact">
      <b>${esc(b.image_id)} · ${esc(b.version || '未标注版本')}</b>
      <span>${esc(b.time)} · ${esc(b.reason_label)} · ${esc(b.image_ref || '本地镜像')}</span>
      <button class="mini-btn" data-a="rollback-to" data-id="${esc(b.id)}" ${busy ? 'disabled' : ''}>
        ${icon('i-rollback')} ${rbToConfirm(b.id) ? '再次点击确认' : '回退到此版本'}
      </button>
    </div>`).join('') : '<div class="backup-empty">还没有备份。更新容器时会自动创建，也可以点「立即备份」。</div>';
  return `<div class="detail-rows">
    ${group('名称备注')}
    ${row('原容器名', esc(c.name))}
    ${row('显示名', esc(c.alias || '（原容器名）'))}
    ${row('备注说明', esc(c.memo || '无'))}
    <div class="dr dr-actions"><span>操作</span><div class="dr-val">
      <button class="mini-btn" data-a="edit-note">${icon('i-pencil')} 编辑备注</button>
      ${c.has_note ? `<button class="mini-btn ghost" data-a="restore-name">${icon('i-rollback')} 恢复原名</button>` : ''}
    </div></div>
    ${group('概要')}
    ${row('容器 ID', esc(c.id), true)}
    ${row('镜像引用', esc(c.image_ref || '—'))}
    ${row('镜像 Digest', esc(c.repo_digest || '—'))}
    ${row('重启策略', esc(c.restart_policy || 'no'))}
    ${row('创建时间', esc(c.created || '—'))}
    ${row('最近更新', esc(c.updated_at || '—'))}
    ${group('网络与存储')}
    ${row('端口映射', lines(c.ports))}
    ${row('数据卷', lines(c.volumes))}
    ${row('网络', lines(c.networks))}
    ${row('环境变量', lines(env))}
    ${row('镜像标签', lines(labels))}
    ${group('版本备份 · ' + backups.length + ' 份')}
    <div class="dr dr-actions"><span>操作</span><div class="dr-val">
      <button class="mini-btn ghost" data-a="backup" ${busy || c.excluded ? 'disabled' : ''}>${icon('i-archive')} 立即备份</button>
    </div></div>
    <div class="backup-list compact">${backupRows}</div>
  </div>`;
}

const ERROR_BADGES = {
  timeout: ['连接仓库超时', 'i-wifi-off'],
  network: ['仓库网络不可达', 'i-wifi-off'],
  auth: ['仓库认证失败', 'i-key'],
  notfound: ['镜像不存在', 'i-info'],
  other: ['检测失败', 'i-close'],
};

/* ---------- v3.9-r4：统一风格的自定义时间选择器（替代浏览器原生白底弹层） ---------- */
window.duTimePicker = {
  bind(root) {
    const scope = root || document;
    Array.from(scope.querySelectorAll('input[type="time"][data-du-time]')).forEach(el => {
      if (el.dataset.duBound === '1') return;
      el.dataset.duBound = '1';
      el.type = 'text'; el.readOnly = true;
      el.classList.add('du-time-input');
      el.setAttribute('autocomplete', 'off');
      el.setAttribute('inputmode', 'none');
      el.addEventListener('click', () => this.open(el));
      el.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.open(el); }
      });
    });
  },
  open(input) {
    document.querySelectorAll('.du-tp').forEach(x => x.remove());
    const m0 = /^(\d{1,2}):(\d{2})$/.exec(input.value || '');
    let H = m0 ? Math.min(23, Math.max(0, +m0[1])) : 3;
    let M = m0 ? Math.min(59, Math.max(0, +m0[2])) : 0;
    const el = document.createElement('div');
    el.className = 'du-tp';
    const hours = Array.from({ length: 24 }, (_, h) =>
      `<button type="button" class="du-tp-cell${h === H ? ' on' : ''}" data-h="${h}">${String(h).padStart(2, '0')}</button>`).join('');
    const mins = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map(v =>
      `<button type="button" class="du-tp-cell${v === Math.floor(M / 5) * 5 ? ' on' : ''}" data-m="${v}">${String(v).padStart(2, '0')}</button>`).join('');
    el.innerHTML = `<div class="du-tp-head"><b>${icon('i-clock')} 选择执行时间</b><span>24 小时制</span></div>
      <div class="du-tp-label">小时</div><div class="du-tp-grid">${hours}</div>
      <div class="du-tp-label">分钟 <span class="tiny">（点格子快速选，或精确输入）</span></div>
      <div class="du-tp-grid mins">${mins}</div>
      <div class="du-tp-exact"><span>精确分钟</span><input type="number" min="0" max="59" value="${M}" id="duTpMinute" class="input"></div>
      <div class="du-tp-foot"><button type="button" class="btn ghost slim" data-tp="now">当前时间</button>
        <span class="modal-spacer"></span>
        <button type="button" class="btn ghost slim" data-tp="cancel">取消</button>
        <button type="button" class="btn primary slim" data-tp="ok">确定</button></div>`;
    document.body.appendChild(el);
    const r = input.getBoundingClientRect();
    const w = 306, h = el.getBoundingClientRect().height || 330;
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
    el.style.top = (r.bottom + h + 8 > window.innerHeight)
      ? Math.max(8, r.top - h - 8) + 'px' : (r.bottom + 6) + 'px';
    const paint = () => {
      el.querySelectorAll('[data-h]').forEach(b => b.classList.toggle('on', +b.dataset.h === H));
      el.querySelectorAll('[data-m]').forEach(b => b.classList.toggle('on', +b.dataset.m === Math.floor(M / 5) * 5));
      const mi = el.querySelector('#duTpMinute'); if (mi) mi.value = M;
      el.querySelector('.du-tp-head b').textContent = '选择执行时间 · ' +
        String(H).padStart(2, '0') + ':' + String(M).padStart(2, '0');
    };
    el.querySelectorAll('[data-h]').forEach(b => b.onclick = () => { H = +b.dataset.h; paint(); });
    el.querySelectorAll('[data-m]').forEach(b => b.onclick = () => { M = +b.dataset.m; paint(); });
    el.querySelector('#duTpMinute').oninput = e => {
      M = Math.min(59, Math.max(0, parseInt(e.target.value || '0', 10) || 0)); paint();
    };
    el.querySelectorAll('[data-tp]').forEach(b => b.onclick = () => {
      const k = b.dataset.tp;
      if (k === 'now') { const d = new Date(); H = d.getHours(); M = d.getMinutes(); paint(); return; }
      if (k === 'ok') {
        input.value = String(H).padStart(2, '0') + ':' + String(M).padStart(2, '0');
        input.dispatchEvent(new Event('change', { bubbles: true }));
      }
      el.remove();
    });
    setTimeout(() => {
      const outside = e => {
        if (!el.contains(e.target) && e.target !== input) { el.remove(); document.removeEventListener('mousedown', outside); }
      };
      document.addEventListener('mousedown', outside);
    }, 0);
  },
};

/* ---------- v3.9-r3：仓库连接诊断（检测失败时给用户可操作的修复路径） ---------- */
function duDiagModal(title, sub, html) {
  const old = document.getElementById('duDiagMask');
  if (old) old.remove();
  const el = document.createElement('div');
  el.id = 'duDiagMask';
  el.className = 'modal-mask';
  el.innerHTML = `<div class="modal-card glass pro-modal wide" role="dialog" aria-modal="true">
    <div class="modal-head"><div><h3>${esc(title)}</h3><p>${esc(sub)}</p></div>
      <button class="icon-btn" data-du-close title="关闭">${icon('i-close')}</button></div>
    <div class="modal-body" id="duDiagBody">${html}</div>
    <div class="modal-foot"><span class="modal-spacer"></span>
      <button class="btn ghost slim" data-du-close>关闭</button></div></div>`;
  document.body.appendChild(el);
  const close = () => el.remove();
  el.querySelectorAll('[data-du-close]').forEach(b => { b.onclick = close; });
  el.addEventListener('click', e => { if (e.target === el) close(); });
  return { body: el.querySelector('#duDiagBody'), close };
}
async function openRegistryDiagnose(name) {
  const m = duDiagModal('仓库连接诊断 · ' + name,
    '本次检测未完成，容器没有被动过；已保留上一次检测结果', `
    <div class="notice small-notice">${icon('i-wifi-off')}<span>「连接仓库超时」是 NAS 的 Docker 守护进程拉镜像时连不上仓库，
      <strong>不是容器有故障、也不是没有新版本</strong>。下面实测的是本面板所在容器的网络，
      与守护进程网络基本一致但不完全等价；根治需在 NAS 上给 Docker 配镜像加速并重启。</span></div>
    <div id="diagBody" class="diag-body"><div class="subtle">正在实测各镜像仓库连通性…</div></div>`);
  try {
    const d = await api('/api/registry/diagnose');
    const box = document.getElementById('diagBody');
    if (!box) return;
    const rows = (d.results || []).map(r => `<tr>
      <td data-label="源"><b>${esc(r.name)}</b></td>
      <td class="small mono" data-label="地址">${esc(r.url)}</td>
      <td data-label="状态"><span class="badge ${r.ok ? 'ok' : 'warn'}">${esc(r.status)}</span></td>
      <td class="small" data-label="延迟">${r.ok ? esc(String(r.ms)) + ' ms' : '—'}</td></tr>`).join('');
    box.innerHTML = `
      <div class="diag-summary"><b>${d.direct_ok ? '面板容器网络可直连 Docker Hub' : '面板容器网络也连不上 Docker Hub'}</b>
        <span class="tiny">检测超时 ${esc(String(d.check_timeout))}s · 失败重试 ${esc(String(d.check_retries))} 次${d.precheck && d.precheck.at ? ' · 上次预检 ' + esc(d.precheck.at) : ''}</span></div>
      <div class="tbl-wrap"><table class="tbl diag-tbl"><thead><tr><th>源</th><th>地址</th><th>状态</th><th>延迟</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4">没有可测的源</td></tr>'}</tbody></table></div>
      <div class="field-title">推荐写入 NAS 的 /etc/docker/daemon.json</div>
      <pre class="diag-code" id="diagCode">${esc(d.daemon_json || '{}')}</pre>
      <p class="tiny">把上面内容合并进 NAS 的 <code>/etc/docker/daemon.json</code>（已存在其它键时只补 registry-mirrors），
        然后 <code>systemctl restart docker</code> 或 <code>service docker restart</code>，再回来点「重新检测」。
        面板容器对宿主机根目录是只读挂载，<strong>不会替你改守护进程配置</strong>。</p>
      <div class="diag-acts">
        <button class="btn primary slim" id="diagFix">${icon('i-bolt')} 自动修复并重新检测</button>
        <button class="btn ghost slim" id="diagCopy">${icon('i-copy')} 复制配置</button>
        <button class="btn ghost slim" id="diagRetryOne">${icon('i-search')} 重新检测该容器</button>
        <button class="btn primary slim" id="diagRetryAll">${icon('i-refresh')} 重试全部检测</button></div>
      <p class="tiny">${esc(d.advice || '')}</p>`;
    const fx = document.getElementById('diagFix');
    if (fx) fx.onclick = async () => {
      fx.disabled = true; fx.textContent = '正在后台自动修复…';
      try {
        const r = await api('/api/registry/autofix', {
          method: 'POST', body: {},
          progressLabel: '正在自动修复仓库连接',
          progressStages: ['探测可用加速源', '经加速源代拉镜像', '重新打回原始标签',
                           '重新检测全部容器', '汇总结果'],
          progressImmediate: true,
        });
        const out = window.duWatchJob ? await window.duWatchJob(r.job_id, r._ph)
                                      : { ok: true, result: {} };
        const d2 = out.result || {};
        const items = (d2.items || []).map(x => `<tr>
          <td data-label="容器"><b>${esc(x.name)}</b></td>
          <td data-label="结果"><span class="badge ${x.ok ? 'ok' : 'skip'}">${x.ok ? '已代拉并重新检测' : '中性跳过'}</span></td>
          <td class="small" data-label="说明">${esc(x.ok ? ('经 ' + x.via) : (x.reason || ''))}</td></tr>`).join('');
        box.innerHTML = `<div class="diag-summary"><b>${esc(d2.message || '自动修复已完成')}</b>
            <span class="tiny">加速源代拉 ${esc(String(d2.ok_count || 0))} 个 · 跳过 ${esc(String(d2.skip_count || 0))} 个</span></div>
          <div class="notice small-notice">${icon('i-info')}<span>面板已通过可用加速源代拉镜像并打回原始标签，
            检测不再依赖 Docker Hub 直连；未同步的镜像改为中性「已跳过」，<strong>不会再出现「连接仓库超时」</strong>。
            这不修改 NAS 的 daemon.json，飞牛桌面自身拉镜像仍可能需要你按上面的配置操作。</span></div>
          <div class="tbl-wrap"><table class="tbl diag-tbl"><thead><tr><th>容器</th><th>结果</th><th>说明</th></tr></thead>
            <tbody>${items || '<tr><td colspan="3">没有需要处理的项目</td></tr>'}</tbody></table></div>
          <div class="diag-acts"><button class="btn primary slim" id="diagFixAgain">${icon('i-refresh')} 再修复一次</button>
            <button class="btn ghost slim" id="diagClose2">完成</button></div>`;
        await load();
        const again = document.getElementById('diagFixAgain');
        if (again) again.onclick = () => openRegistryDiagnose(name);
        const cl = document.getElementById('diagClose2');
        if (cl) cl.onclick = () => m.close();
      } catch (e) {
        fx.disabled = false; fx.textContent = '自动修复并重新检测';
        toast('自动修复失败：' + (e.message || '未知错误'), 'err');
      }
    };
    const cp = document.getElementById('diagCopy');
    if (cp) cp.onclick = () => {
      const t = document.getElementById('diagCode').textContent;
      (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(
        () => toast('daemon.json 片段已复制', 'ok'),
        () => { const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta);
          ta.select(); document.execCommand('copy'); ta.remove(); toast('已复制', 'ok'); });
    };
    const one = document.getElementById('diagRetryOne');
    if (one) one.onclick = async () => { m.close(); await act('/api/check', { name }); };
    const all = document.getElementById('diagRetryAll');
    if (all) all.onclick = async () => { m.close(); await act('/api/check-all', {}); };
  } catch (e) {
    const box = document.getElementById('diagBody');
    if (box) box.innerHTML = `<div class="notice small-notice err">${icon('i-close')}<span>诊断接口不可用：${esc(e.message || '未知错误')}</span></div>`;
  }
}

function badgeOf(c) {
  if (c.excluded) return `<span class="badge skip" title="${esc(c.exclude_reason)}">${icon('i-shield')} 不监控</span>`;
  if (c.kind !== 'tag') {
    const t = c.kind === 'pinned' ? '固定 digest' : '本地镜像';
    return `<span class="badge skip" title="无法检测浮动版本">${icon('i-info')} ${t}</span>`;
  }
  if (c.skipped && !c.check_error) {
    return c.has_update
      ? `<span class="badge update" title="${esc(c.skipped)}">${icon('i-upload')} 发现新版本 · 沿用上次结果</span>`
      : `<span class="badge skip" title="${esc(c.skipped)}">${icon('i-info')} 已跳过检测</span>`;
  }
  if (c.check_error) {
    const [label, ico] = ERROR_BADGES[c.error_kind] || ERROR_BADGES.other;
    const tip = (c.error_advice || '') + '\n\n原始错误：' + c.check_error;
    return `<span class="badge warn" title="${esc(tip)}">${icon(ico)} ${label}</span>`;
  }
  if (c.rolled_back) {
    return `<span class="badge rollback" title="已回退到 ${esc(c.rollback_to)}，仓库中的新版仍可再次更新">${icon('i-rollback')} 已回退 · 可再次更新</span>`;
  }
  if (c.has_update) {
    return c.ignored
      ? `<span class="badge ignored">${icon('i-pause')} 新版本 · 已暂不更新</span>`
      : `<span class="badge update"><i class="dot"></i> 发现新版本</span>`;
  }
  if (!c.checked_at) return `<span class="badge wait">${icon('i-clock')} 待检测</span>`;
  return `<span class="badge latest"><i class="dot"></i> 已是最新</span>`;
}

function confirmKey(action, name, id) { return action + ':' + name + ':' + (id || ''); }
function isConfirming(key) {
  return !!(pendingConfirm && pendingConfirm.key === key && Date.now() < pendingConfirm.until);
}

function rowHTML(c) {
  const busy = !!(state && state.runtime.busy);
  const dis = (c.excluded || c.kind !== 'tag' || busy) ? ' disabled' : '';
  const goOK = c.can_update && c.has_update && !busy;
  /* v6.0-r20：只有检测到可更新版本才出现「立即更新」按钮；没有更新连按钮都不渲染，
     不再用 disabled 的灰按钮占位。can_update 兼容后端未下发时的旧数据，
     excluded / busy 一律不显示。 */
  const showUpdate = !!(c.has_update && c.can_update !== false && !c.excluded && !busy);
  const upConfirm = isConfirming(confirmKey('update', c.name));
  const rbConfirm = isConfirming(confirmKey('rollback', c.name));
  const rbToConfirm = (id) => isConfirming(confirmKey('rollback-to', c.name, id));
  const holdOn = c.policy === 'hold';
  const autoOn = c.policy === 'auto';
  const isOpen = expanded.has(c.name);
  const backups = c.backups || [];
  const hasBackup = backups.length > 0;

  const backupRows = backups.length ? backups.map((b) => `
    <div class="backup-item">
      <span class="b-ic">${icon('i-archive')}</span>
      <div class="backup-meta">
        <b>${esc(b.image_id)} · ${esc(b.version || '未标注版本')}</b>
        <span>${esc(b.time)} · ${esc(b.reason_label)} · ${esc(b.image_ref || '本地镜像')}${b.running ? '' : ' · 当时已停止'}</span>
      </div>
      <button class="mini-btn" data-a="rollback-to" data-id="${esc(b.id)}" ${busy ? 'disabled' : ''}>
        ${icon('i-rollback')} ${rbToConfirm(b.id) ? '再次点击确认' : '回退到此版本'}
      </button>
    </div>`).join('') : '<div class="backup-empty">还没有备份。更新容器时会自动创建，也可以点「立即备份」。</div>';

  const detail = detailRows(c);

  return `<article class="row glass ${c.excluded ? 'is-excluded' : ''}" data-name="${esc(c.name)}">
    <div class="row-top">
      <div class="row-main">
        <div class="name-line">
          <i class="dot ${c.running ? 'on' : 'off'}"></i>
          <h3>${esc(c.alias || c.name)}</h3>
          ${c.alias ? `<span class="orig-name" title="原容器名：${esc(c.name)}">原名 ${esc(c.name)}</span>` : ''}
          <button class="name-edit" data-a="edit-note" title="编辑名称备注">${icon('i-pencil')}</button>
          <span class="state-pill ${c.running ? '' : 'off'}">${esc(c.status)}</span>
        </div>
        <div class="sub">${c.project ? '项目: ' + esc(c.project) + (c.service ? ' · ' + esc(c.service) : '') : '独立容器'}
          <span class="sep">·</span> ${esc(c.exclude_reason || (c.restart_policy ? '重启策略 ' + esc(c.restart_policy) : '标准容器'))}</div>
        ${c.memo ? `<div class="note-memo">${icon('i-tag')}<span>${esc(c.memo)}</span></div>` : ''}
        <div class="tags">
          <span class="tag">${icon('i-layers')} ${esc(c.tag || '—')}</span>
          ${c.version_label ? `<span class="tag">v ${esc(c.version_label)}</span>` : ''}
          ${c.policy === 'auto' ? '<span class="tag">自动更新</span>' : ''}
          ${c.policy === 'hold' ? '<span class="tag">暂不更新</span>' : ''}
        </div>
      </div>
      <div class="row-ver">
        <div class="repo" title="${esc(c.image_ref)}">${esc(c.image_ref || '本地构建镜像')}</div>
        <div class="ver-grid">
          <div><span>镜像标签</span><b class="tagpill">${esc(c.tag || '—')}</b></div>
          <div><span>版本号</span><b title="${esc(c.version_label)}">${esc(c.version_label || '未标注')}</b></div>
          <div><span>镜像 ID</span><b class="mono">${esc(c.image_id || '—')}</b></div>
          <div><span>构建时间</span><b>${esc(c.image_created || '—')}</b></div>
        </div>
        ${c.has_update ? `<div class="newline">${icon('i-upload')} 新镜像 ${esc(c.latest_image_id)} · ${esc(c.latest_version || c.latest_created || '新版本已发布')}${c.check_error ? ' · 本次检测未完成，沿用上次结果' : ''}</div>` : ''}
        ${c.auto_rollback ? `<div class="health-line bad" title="已自动回退到更新前的版本">${icon('i-rollback')} 更新后健康检查未通过，已自动回退</div>` : ''}
        ${c.health_ok === true && c.health_detail && !c.health_skipped ? `<div class="health-line good" title="${esc(c.health_detail)}">${icon('i-check')} 更新后健康：${esc(String(c.health_detail).slice(0, 36))}</div>` : ''}
        ${c.health_ok === false && !c.auto_rollback ? `<div class="health-line bad" title="${esc(c.health_detail)}">${icon('i-wifi-off')} 健康检查未通过</div>` : ''}
      </div>
      <div class="row-side">
        ${badgeOf(c)}
        <div class="checked">${c.checked_at ? '检测于 ' + esc(String(c.checked_at).slice(5, 16)) : '尚未检测'}</div>
        ${c.check_error ? `<div class="err-line soft" title="${esc(c.check_error)}">${esc((ERROR_BADGES[c.error_kind] || ERROR_BADGES.other)[0])} · 本次检测未完成，已保留上次结果</div>` : ''}
      </div>
    </div>
    <div class="row-actions">
      <div class="acts">
        <button class="act ${holdOn ? 'active hold' : ''}" data-a="hold" title="再次点击恢复手动"${dis}>${icon('i-pause')} ${holdOn ? '已暂不更新' : '暂不更新'}</button>
        <label class="act-switch ${autoOn ? 'on' : ''}${dis ? ' is-disabled' : ''}" title="开启后每日巡检发现新版本会自动重建更新；再点一次关闭">
          <input type="checkbox" data-pol="auto" ${autoOn ? 'checked' : ''}${dis ? ' disabled' : ''}><span class="sw-track"></span><span>自动更新</span>
        </label>
        <button class="act" data-a="check"${dis}>${icon('i-search')} 检查更新</button>
        ${showUpdate ? `<button class="act go ${upConfirm ? 'confirm' : ''}" data-a="update" ${goOK ? '' : 'disabled'}>${icon('i-upload')} ${upConfirm ? '再次点击确认更新' : '立即更新'}</button>` : ''}
        <button class="act rollback ${rbConfirm ? 'confirm' : ''}" data-a="rollback" ${!hasBackup || busy || c.excluded ? 'disabled' : ''} title="回退到最近一次备份的旧版本">${icon('i-rollback')} ${rbConfirm ? '确认回退' : '快速回退'}${hasBackup ? `<span class="count-badge">${backups.length}</span>` : ''}</button>
        <button class="act restore" data-a="restore-name" ${!c.has_note || busy ? 'disabled' : ''} title="清除备注，恢复显示 Docker 原容器名">${icon('i-rollback')} 恢复原名</button>
        ${c.check_error ? `<button class="act diag" data-a="diagnose" title="实测仓库连通性并给出修复步骤">${icon('i-wifi-off')} 诊断修复</button>` : ''}
      </div>
      <button class="detail-btn ${isOpen ? 'open' : ''}" data-a="detail">详情 ${icon('i-chevron')}</button>
    </div>
    <div class="detail ${isOpen ? 'show' : ''}">${detail}</div>
  </article>`;
}

/* v3.3：自动轮询每 5 秒会整页重绘。用户正在输入 / 弹窗打开 / 下拉展开时重绘，
   会把输入框清空、焦点丢掉、勾选状态冲掉（截图里「点输入框就闪一下又取消」「保存后又恢复」）。
   这里在交互态跳过子视图重绘，等失焦或弹窗关闭后补一次。 */
function userIsInteracting() {
  const a = document.activeElement;
  if (a && !a.closest('#loginMask') && (a.matches('input, textarea, select') || a.isContentEditable)) return true;
  if (document.querySelector('#genericModal:not(.hidden), #regModal, #containerDrawer.open, .modal-mask:not(.hidden)')) return true;
  if (document.querySelector('.du-sel-pop.on')) return true;
  return false;
}
window.duUserInteracting = userIsInteracting;
let duRenderDeferred = false;
function flushDeferredRender() {
  if (!duRenderDeferred || userIsInteracting()) return;
  duRenderDeferred = false;
  if (state && window.duConsoleRender) window.duConsoleRender(state);
}
document.addEventListener('focusout', () => setTimeout(flushDeferredRender, 80), true);

/* v3.9-r5：轮询渲染签名缓存。
   后台检测时轮询从 5s 缩到 1.5s，旧版每次都把统计卡 / 容器列表 / 日志 / 备注
   整块 innerHTML 重建，卡片的过渡、阴影与磨砂层随之重放，看起来就是「整页闪动」。
   数据没变就一个 DOM 都不动，只更新进度条里的文本与宽度。 */
const duRenderSig = { stats: '', list: '', logs: '', notes: '', restore: '' };

function render(s) {
  if (pendingConfirm && Date.now() > pendingConfirm.until) pendingConfirm = null;

  const pill = $('#dockerPill');
  pill.className = 'pill ' + (s.docker_connected ? 'ok' : 'bad');
  pill.querySelector('span').textContent = s.docker_connected
    ? 'Docker 已连接 · v' + (s.docker_version || '') : 'Docker 未连接';
  if (!s.docker_connected && s.docker_error) pill.title = s.docker_error;

  $('#lastCheck').textContent = s.last_full_check ? String(s.last_full_check).slice(5, 16) : '尚未检测';
  $('#nextCheck').textContent = s.next_check || '—';
  $('#curUser').textContent = s.username || 'admin';
  $('#footSchedule').textContent = '每日 ' + esc(s.settings.daily_time) + ' 自动检测 · '
    + String(s.server_time).slice(0, 10) + ' · 已保存 ' + (s.stats.backups || 0) + ' 份备份'
    + ' · 已备注 ' + (s.stats.notes || 0) + ' 个';

  const st = s.stats;
  const statsSig = [st.total, st.running, st.updates, st.auto, st.hold].join('|');
  if (duRenderSig.stats !== statsSig || !$('#stats').firstElementChild) {
    $('#stats').innerHTML = [
      statCard('容器总数', st.total, 'i-box', 'blue', st.total, st.total),
      statCard('运行中', st.running, 'i-check', 'green', st.running, st.total),
      statCard('可更新', st.updates, 'i-upload', 'orange', st.updates, st.total),
      statCard('自动更新', st.auto, 'i-bolt', 'purple', st.auto, st.total),
      statCard('暂不更新', st.hold, 'i-pause', 'gray', st.hold, st.total),
    ].join('');
    duRenderSig.stats = statsSig;
  }

  const busy = !!s.runtime.busy;
  $('#btnCheckAll').disabled = busy;
  $('#btnDaily').disabled = busy;
  $('#progress').classList.toggle('hidden', !busy);
  if (busy) {
    const p = Number(s.runtime.progress) || 0;
    const t = Number(s.runtime.total) || 0;
    $('#progressText').textContent = s.runtime.message || '任务执行中…';
    $('#progressBar').style.width = (t ? Math.max(4, p / t * 100) : 12) + '%';
    $('#progressNum').textContent = t ? p + ' / ' + t : '';
  }
  // 异步任务失败（如仓库连接超时）时给出明确提示，避免只看到进度条消失
  if (!busy && s.runtime.ok === false && s.runtime.message && s.runtime.message !== lastJobError) {
    toast(s.runtime.message, 'err');
  }
  lastJobError = busy ? '' : (s.runtime.ok === false ? (s.runtime.message || '') : '');

  let items = s.containers.slice();
  const q = query.trim().toLowerCase();
  if (q) {
    items = items.filter((c) => [c.name, c.alias, c.memo, c.image_ref, c.project, c.service,
      c.tag, c.version_label].join(' ').toLowerCase().includes(q));
  }
  if (filter === 'update') items = items.filter((c) => c.has_update && !c.excluded);
  if (filter === 'auto') items = items.filter((c) => c.policy === 'auto' && !c.excluded);
  if (filter === 'hold') items = items.filter((c) => c.policy === 'hold' && !c.excluded);
  if (filter === 'stopped') items = items.filter((c) => !c.running);
  if (filter === 'noted') items = items.filter((c) => c.has_note);

  const displayName = (c) => c.alias || c.name;
  const byName = (a, b) => displayName(a).localeCompare(displayName(b), 'zh-CN');
  const byUpdate = (a, b) => (Number(b.has_update) - Number(a.has_update))
    || (Number(b.running) - Number(a.running)) || byName(a, b);
  if (sortBy === 'name') items.sort(byName);
  else if (sortBy === 'note') items.sort((a, b) => (Number(b.has_note) - Number(a.has_note)) || byName(a, b));
  else if (sortBy === 'status') items.sort((a, b) => (Number(b.running) - Number(a.running)) || byName(a, b));
  else if (sortBy === 'checked') items.sort((a, b) => String(b.checked_at).localeCompare(String(a.checked_at)) || byName(a, b));
  else items.sort(byUpdate);

  const listSig = JSON.stringify(items) + '|' + filter + '|' + q + '|' + sortBy
    + '|' + (busy ? 1 : 0) + '|' + (pendingConfirm ? pendingConfirm.key : '')
    // v3.9-r6：展开详情 / 二次确认按钮文案都依赖本地交互态，必须进入签名，
    // 否则点「详情」只改 expanded、数据没变，整行 DOM 被缓存跳过，看起来就是按钮失灵。
    + '|' + Array.from(expanded).sort().join('\u0002');
  const listEl = $('#list');
  if (duRenderSig.list !== listSig || (items.length && !listEl.firstElementChild)) {
    listEl.innerHTML = items.map(rowHTML).join('');
    duRenderSig.list = listSig;
  }
  const emptyEl = $('#empty');
  emptyEl.classList.toggle('hidden', items.length > 0);
  $('#visibleCount').textContent = '显示 ' + items.length + ' / ' + s.containers.length;
  if (!items.length) {
    const emptySig = 'empty|' + q + '|' + filter;
    if (emptyEl.dataset.sig !== emptySig) {
      const label = { all: '全部', update: '可更新', auto: '自动更新', hold: '暂不更新',
                      stopped: '已停止', noted: '已备注' }[filter] || filter;
      emptyEl.innerHTML = `<svg class="ic"><use href="#i-box"/></svg>
        <h3>没有匹配的容器</h3>
        <p>${q ? `搜索词「<b>${esc(query.trim())}</b>」 · 筛选「${esc(label)}」` : `筛选「${esc(label)}」下没有容器`}</p>
        ${q ? '<button class="btn ghost slim" id="btnClearSearch">清除搜索词</button>' : ''}`;
      emptyEl.dataset.sig = emptySig;
      const clr = $('#btnClearSearch');
      if (clr) clr.onclick = () => {
        const el = $('#search');
        el.value = ''; el.dataset.duTouched = '1';
        query = '';
        if (state) render(state);
        toast('已清除搜索词', 'ok');
      };
    }
  }

  const noteMap = s.notes || {};
  const logs = s.history || [];
  const logCount = $('#logCount');
  if (logCount) logCount.textContent = '共 ' + logs.length + ' 条 · 保留 '
    + (s.settings.log_retention_days ?? 7) + ' 天，到期自动清除';
  const logsSig = JSON.stringify(logs) + '|' + JSON.stringify(noteMap) + '|'
    + String(s.settings.log_retention_days ?? 7);
  if (duRenderSig.logs !== logsSig || !$('#logList').firstElementChild) {
    $('#logList').innerHTML = logs.length
      ? logs.map((h) => {
          const note = noteMap[h.container] || {};
          return `<div class="log-row ${h.ok ? '' : 'fail'} ${esc(h.action)}">
            <span class="log-time">${esc(String(h.time).slice(5))}</span>
            <span class="log-tag">${esc(h.action)}</span>
            <span class="log-name" title="原对象：${esc(h.container)}">${esc(note.alias || h.container)}</span>
            <span class="log-msg">${esc(h.message)}</span>
          </div>`;
        }).join('')
      : '<div class="log-empty">暂无记录，面板内的任何动作都会记录在这里</div>';
    duRenderSig.logs = logsSig;
  }

  const noteEntries = Object.entries(noteMap);
  const notesSig = JSON.stringify(noteEntries);
  if (duRenderSig.notes !== notesSig || !$('#noteList').firstElementChild) {
    $('#noteList').innerHTML = noteEntries.length
      ? noteEntries.map(([n, v]) => `<div class="note-row">
          <span class="n-ic">${icon('i-tag')}</span>
          <div class="note-body"><b>${esc(v.alias || n)}</b>
            <span title="原名 ${esc(n)}${v.memo ? ' · ' + esc(v.memo) : ''}">原名 ${esc(n)}${v.memo ? ' · ' + esc(v.memo) : ''}</span></div>
          <button class="mini-btn ghost" data-restore="${esc(n)}">恢复</button>
        </div>`).join('')
      : '<div class="note-empty">还没有备注。点卡片标题旁的铅笔，给容器起个好记的名字吧。</div>';
    duRenderSig.notes = notesSig;
  }

  const allConfirm = isConfirming(confirmKey('restore-all', '', ''));
  const restoreAllBtn = $('#btnRestoreAllNames');
  const restoreSig = (allConfirm ? 1 : 0) + '|' + (noteEntries.length === 0 || busy ? 1 : 0);
  if (duRenderSig.restore !== restoreSig) {
    restoreAllBtn.innerHTML = icon('i-rollback') + (allConfirm ? ' 再次点击确认恢复全部' : ' 恢复全部原名');
    duRenderSig.restore = restoreSig;
  }
  restoreAllBtn.disabled = noteEntries.length === 0 || busy;

  // v2.0：让管理增强视图复用同一次快照，避免重复请求
  // v3.3：用户正在输入 / 弹窗打开时不重绘子视图，避免清空输入与丢失焦点
  if (userIsInteracting()) {
    duRenderDeferred = true;
  } else if (window.duConsoleRender) {
    duRenderDeferred = false;
    try { window.duConsoleRender(s); } catch (e) { console.error('[console]', e); }
  }
}

/* ---------- 名称备注 ---------- */
let editingNoteName = '';

function openNoteModal(name) {
  const c = (state && state.containers.find((x) => x.name === name)) || {};
  editingNoteName = name;
  $('#noteOrig').textContent = name;
  $('#noteAlias').value = c.alias || '';
  $('#noteMemo').value = c.memo || '';
  $('#btnNoteRestore').classList.toggle('hidden', !c.has_note);
  $('#noteModal').classList.remove('hidden');
  setTimeout(() => { try { $('#noteAlias').focus(); } catch (e) { /* ignore */ } }, 60);
}
function closeNoteModal() {
  $('#noteModal').classList.add('hidden');
  editingNoteName = '';
}
async function saveNote() {
  if (!editingNoteName) return;
  const name = editingNoteName;
  const body = { name, alias: $('#noteAlias').value, memo: $('#noteMemo').value };
  try {
    const d = await api('/api/note', { method: 'POST', body });
    closeNoteModal();
    toast(d.message || '备注已保存', 'ok');
    await load();
  } catch (e) { if (!e.auth) toast(e.message, 'err'); }
}
async function restoreName(name, fromModal = false) {
  try {
    const d = await api('/api/restore-name', { method: 'POST', body: { name } });
    if (fromModal) closeNoteModal();
    await load();
    const removed = d.removed || {};
    if (removed.alias || removed.memo) {
      toast(d.message, 'ok', { label: '撤销', onClick: async () => {
        await api('/api/note', { method: 'POST',
          body: { name, alias: removed.alias || '', memo: removed.memo || '' } });
        await load();
      }});
    } else {
      toast(d.message, 'info');
    }
  } catch (e) { if (!e.auth) toast(e.message, 'err'); }
}
async function restoreAllNames() {
  const key = confirmKey('restore-all', '', '');
  if (!isConfirming(key)) {
    pendingConfirm = { key, until: Date.now() + 6000 };
    toast('再次点击「恢复全部原名」确认清除所有备注', 'info');
    if (state) render(state);
    return;
  }
  pendingConfirm = null;
  try {
    const d = await api('/api/restore-all-names', { method: 'POST', body: {} });
    await load();
    const removed = d.removed || {};
    if (Object.keys(removed).length) {
      toast(d.message, 'ok', { label: '撤销', onClick: async () => {
        for (const [n, v] of Object.entries(removed)) {
          await api('/api/note', { method: 'POST',
            body: { name: n, alias: v.alias || '', memo: v.memo || '' } });
        }
        await load();
      }});
    } else {
      toast(d.message, 'info');
    }
  } catch (e) { if (!e.auth) toast(e.message, 'err'); }
}

/* ---------- 抽屉 ---------- */
function openDrawer(id) {
  $$('.drawer').forEach((d) => d.classList.remove('open'));
  $('#' + id).classList.add('open');
  $('#scrim').classList.add('show');
}
function closeDrawers() {
  $$('.drawer').forEach((d) => d.classList.remove('open'));
  $('#scrim').classList.remove('show');
}
async function fillSettings() {
  if (!state) return;
  const s = state.settings;
  $('#setTime').value = s.daily_time || '03:00';
  $('#setTimeout').value = s.check_timeout ?? 300;
  $('#setRetries').value = s.check_retries ?? 1;
  $('#setBackupKeep').value = s.backup_keep ?? 5;
  $('#setSample').value = s.sample_interval ?? 3;
  $('#setRefresh').value = s.refresh_interval ?? 5;
  // v5.0：更新后健康检查
  $('#setHealthProbe').value = s.health_probe || 'running';
  $('#setHealthPort').value = s.health_port ?? 0;
  $('#setHealthPath').value = s.health_path || '/';
  $('#setHealthWait').value = s.health_wait_s ?? 5;
  $('#setHealthTimeout').value = s.health_timeout_s ?? 3;
  $('#setHealthRetries').value = s.health_retries ?? 2;
  $$('input[data-key]').forEach((i) => { i.checked = !!s[i.dataset.key]; });
  $('#curUser').textContent = state.username || 'admin';
  try {
    const d = await api('/api/registry-auth');
    $('#regAuth').value = d.text || '';
  } catch (e) { /* ignore */ }
}

/* ---------- 事件 ---------- */
/* v6.0-r20：自动更新按钮改成开关 —— label 内 checkbox 用 change 事件走 /api/policy，
   避免 label 的 click 冒泡与浏览器转发造成双触发；成功后 load() 用后端状态回填。 */
$('#list').addEventListener('change', async (e) => {
  const cb = e.target;
  if (!cb || !cb.matches || !cb.matches('[data-pol="auto"]')) return;
  const row = cb.closest('[data-name]');
  if (!row || !state) return;
  const name = row.dataset.name;
  const mode = cb.checked ? 'auto' : 'manual';
  const sw = cb.closest('.act-switch');
  if (sw) sw.classList.toggle('on', cb.checked);
  toast(mode === 'auto' ? `已开启「${name}」自动更新` : `已关闭「${name}」自动更新`, 'ok');
  await act('/api/policy', { name, mode });
});
$('#list').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-a]');
  if (!btn || btn.disabled) return;
  const row = btn.closest('[data-name]');
  if (!row || !state) return;
  const name = row.dataset.name;
  const a = btn.dataset.a;

  if (a === 'detail') {
    if (expanded.has(name)) expanded.delete(name); else expanded.add(name);
    render(state);
    return;
  }
  if (a === 'edit-note') { openNoteModal(name); return; }
  if (a === 'restore-name') { await restoreName(name); return; }
  if (a === 'diagnose') { await openRegistryDiagnose(name); return; }
  const c = state.containers.find((x) => x.name === name) || {};
  if (a === 'hold' || a === 'auto') {
    await act('/api/policy', { name, mode: c.policy === a ? 'manual' : a });
  } else if (a === 'check') {
    await act('/api/check', { name });
  } else if (a === 'backup') {
    await act('/api/backup', { name });
  } else if (a === 'update' || a === 'rollback' || a === 'rollback-to') {
    const id = btn.dataset.id || '';
    const key = confirmKey(a, name, id);
    if (!isConfirming(key)) {
      pendingConfirm = { key, until: Date.now() + 6000 };
      toast(a === 'update' ? '再次点击「立即更新」确认重建容器'
        : '再次点击确认回退（会先备份当前版本）', 'info');
      render(state);
      return;
    }
    pendingConfirm = null;
    if (a === 'update') await act('/api/update', { name });
    else await act('/api/rollback', { name, backup_id: id || undefined });
  }
});

$('#btnCheckAll').onclick = () => act('/api/check-all', {});
$('#btnDaily').onclick = () => act('/api/check-daily', {});
$('#btnReload').onclick = () => window.duRefreshWithFeedback($('#btnReload'));
$('#btnLogs').onclick = () => { if (window.duGotoView) window.duGotoView('logs'); };
$('#btnLogsBack').onclick = () => { if (window.duGotoView) window.duGotoView('overview'); };
$('#btnLogsRefresh').onclick = () => window.duRefreshWithFeedback && window.duRefreshWithFeedback($('#btnLogsRefresh'));
/* v3.9：运行日志保留时长（写入 settings，后端按天数自动清除过期日志） */
window.duFillLogRetention = () => {
  const el = $('#logRetention');
  if (el && state) el.value = String(state.settings.log_retention_days ?? 7);
};
$('#btnLogRetentionSave').onclick = async () => {
  const v = Number($('#logRetention').value) || 7;
  try {
    await api('/api/settings', { method: 'POST', body: { settings: { log_retention_days: v } } });
    toast('日志保留时长已保存为 ' + v + ' 天，过期日志已自动清理', 'ok');
    await load();
  } catch (e) { toast(e.message || '保存失败', 'err'); }
};
$('#btnSettings').onclick = () => { if (window.duGotoView) window.duGotoView('settings'); };
$('#btnSettingsBack').onclick = () => { if (window.duGotoView) window.duGotoView('overview'); };
$('#btnLogout').onclick = logout;
$('#btnLogout2').onclick = logout;
$('#btnChangePwd').onclick = changePassword;
$('#btnNoteClose').onclick = closeNoteModal;
$('#btnNoteCancel').onclick = closeNoteModal;
$('#btnNoteSave').onclick = saveNote;
$('#btnNoteRestore').onclick = () => { if (editingNoteName) restoreName(editingNoteName, true); };
$('#noteModal').addEventListener('click', (e) => {
  if (e.target.id === 'noteModal') closeNoteModal();
});
$('#noteAlias').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveNote(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#noteModal').classList.contains('hidden')) closeNoteModal();
});
$('#btnRestoreAllNames').onclick = restoreAllNames;
$('#noteList').addEventListener('click', (e) => {
  const b = e.target.closest('[data-restore]');
  if (b) restoreName(b.dataset.restore);
});
$('#scrim').onclick = closeDrawers;
$$('[data-close]').forEach((b) => { b.onclick = closeDrawers; });

$('#btnSaveSettings').onclick = async () => {
  const settings = {
    daily_time: $('#setTime').value,
    check_timeout: Number($('#setTimeout').value),
    check_retries: Number($('#setRetries').value),
    backup_keep: Number($('#setBackupKeep').value),
    sample_interval: Number($('#setSample').value),
    refresh_interval: Number($('#setRefresh').value),
    // v5.0：更新后健康检查
    health_probe: $('#setHealthProbe').value,
    health_port: Number($('#setHealthPort').value) || 0,
    health_path: $('#setHealthPath').value || '/',
    health_wait_s: Number($('#setHealthWait').value) || 0,
    health_timeout_s: Number($('#setHealthTimeout').value) || 3,
    health_retries: Number($('#setHealthRetries').value) || 0,
  };
  $$('input[data-key]').forEach((i) => { settings[i.dataset.key] = i.checked; });
  try {
    await api('/api/settings', { method: 'POST', body: { settings } });
    await api('/api/registry-auth', { method: 'POST', body: { text: $('#regAuth').value } });
    toast('设置已保存', 'ok');
    closeDrawers();
    await load();
  } catch (e) { toast(e.message, 'err'); }
};

$('#search').addEventListener('input', (e) => {
  if (e.target.dataset.duTouched !== '1') {
    e.target.value = ''; query = '';
    if (state) render(state);
    return;
  }
  query = e.target.value;
  if (state) render(state);
});
$('#filters').addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  // 点击筛选时再兜底一次：用户没碰过搜索框却带值，必然是自动填充
  const s = $('#search');
  if (s.value && s.dataset.duTouched !== '1') {
    s.value = ''; query = '';
    toast('已清除浏览器自动填入的登录账号，筛选恢复正常', 'info');
  }
  filter = b.dataset.filter;
  $$('#filters .chip').forEach((x) => x.classList.toggle('active', x === b));
  if (state) render(state);
});
$('#sort').addEventListener('change', (e) => { sortBy = e.target.value; if (state) render(state); });

$('#loginForm').addEventListener('submit', (e) => { e.preventDefault(); login(); });

const savedTheme = localStorage.getItem('du_theme');
/* v3.8：默认使用深蓝紫企业级科技主题；用户手动切换过则尊重其选择。 */
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
else document.documentElement.dataset.theme = 'dark';
function syncThemeIcon() {
  const u = $('#btnTheme use');
  if (u) u.setAttribute('href', document.documentElement.dataset.theme === 'dark' ? '#i-sun' : '#i-moon');
}
$('#btnTheme').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('du_theme', next);
  syncThemeIcon();
};
syncThemeIcon();

/* ---------- 供 console.js 复用的桥接 ---------- */
window.duApi = api;
window.duToast = toast;
window.duEsc = esc;
window.duIcon = icon;
window.duState = () => state;
window.duLoad = load;
/* v6.0-r15：手动「刷新」统一反馈。
   #btnReload 与各页「刷新」按钮过去直接调 load()，只重读 /api/state：
   数据没变时签名缓存跳过重绘，纯 GET 又不弹进度条/toast，点了零反馈。
   现在给按钮图标一个匀速旋转（至少 420ms）+ 一条带时间的 toast，
   明确写出「页面未重载」，避免误以为它是浏览器刷新 / 能清缓存。
   注意：不强制重绘、不改 DOM 主体，不会重现整页闪动或冲掉正在输入的表单。 */
window.duRefreshWithFeedback = async function (btn) {
  if (btn && btn.dataset.duBusy === '1') return;   // 防连点
  const t0 = Date.now();
  if (btn) { btn.dataset.duBusy = '1'; btn.classList.add('du-reloading'); }
  try {
    const ok = await load();
    const rest = Math.max(0, 420 - (Date.now() - t0));
    if (rest) await new Promise(r => setTimeout(r, rest));
    if (ok) {
      const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      toast('已重新读取面板数据 · ' + now + '（页面未重载）', 'ok');
    }
    /* 失败时 load() 内部已经 toast 了具体错误，这里不重复弹 */
  } finally {
    if (btn) { btn.classList.remove('du-reloading'); btn.dataset.duBusy = ''; }
  }
};
window.duToken = () => token;
window.duConfirmKey = confirmKey;
window.duIsConfirming = isConfirming;
window.duSetConfirm = (key, ms = 6000) => { pendingConfirm = { key, until: Date.now() + ms }; };
window.duClearConfirm = () => { pendingConfirm = null; };
window.duRender = () => { if (state) render(state); };
window.duOpenDrawer = openDrawer;
window.duCloseDrawers = closeDrawers;
window.duOpenNote = openNoteModal;

/* ---------- 轮询 ---------- */
function schedule() {
  clearTimeout(loadTimer);
  const idle = (state && state.settings && state.settings.refresh_interval)
    ? Math.max(3, Number(state.settings.refresh_interval) || 5) * 1000 : 5000;
  loadTimer = setTimeout(tick, (state && state.runtime.busy) ? 1500 : idle);
}
async function tick() {
  if (!token) { showLogin(); schedule(); return; }
  if (!paused && !document.hidden) await load();
  schedule();
}
window.duSetPaused = (v) => { paused = !!v; };
window.duIsPaused = () => paused;
tick();

/* ==================== v3.5：表格自动标注 data-label，配合窄屏堆叠布局 ====================
   任何视图渲染出来的 .tbl 都会自动把表头文字写到每个单元格上；
   窄屏时 CSS 把表格改成卡片式堆叠，不需要横向滚动也能看全所有列。 */
(function () {
  let timer = null;
  function decorate() {
    document.querySelectorAll('table.tbl:not([data-rlabeled])').forEach(tb => {
      const heads = Array.from(tb.querySelectorAll('thead th')).map(th => (th.textContent || '').trim());
      if (!heads.length) return;
      tb.querySelectorAll('tbody tr').forEach(tr => {
        Array.from(tr.children).forEach((td, i) => {
          if (!td.hasAttribute('data-label') && heads[i]) td.setAttribute('data-label', heads[i]);
        });
      });
      tb.setAttribute('data-rlabeled', '1');
    });
  }
  window.duDecorateTables = decorate;
  if (typeof MutationObserver !== 'undefined') {
    const mo = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(decorate, 60); });
    mo.observe(document.body, { childList: true, subtree: true });
  }
  decorate();
})();


/* ==================== v3.6：设置独立页按小节分组（双列，不再挤在窄抽屉里） ==================== */
(function groupSettingsPage() {
  const body = document.getElementById('settingsBody');
  if (!body || body.dataset.grouped === '1') return;
  const kids = Array.from(body.children);
  let sec = null;
  kids.forEach(el => {
    if (el.classList.contains('field-title')) {
      sec = document.createElement('section');
      sec.className = 'settings-sec';
      body.appendChild(sec);
      sec.appendChild(el);
    } else if (sec) {
      sec.appendChild(el);
    }
  });
  body.dataset.grouped = '1';
})();


/* ==========================================================================
   v6.0-r23 · 设置页 S4「控制台单组」
   页面内二级分组导航（不是全站侧栏）：左栏选组，右侧一次只显示一组。
   9 组共用一份 state.json，保存仍是全量写入；「重置本组」只恢复当前组字段，
   不会误清其它组里还没保存的改动。不重建 DOM、不参与 5 秒轮询。
   ========================================================================== */
(function duSettingsConsole() {
  const body = document.getElementById('settingsBody');
  const list = document.getElementById('railList');
  const search = document.getElementById('settingsSearch');
  const railEmpty = document.getElementById('railEmpty');
  const railCount = document.getElementById('railCount');
  const head = document.getElementById('settingsGroupHead');
  const hint = document.getElementById('settingsFootHint');
  if (!body || !list) return;

  const secs = Array.from(body.querySelectorAll(':scope > .settings-sec'));
  if (!secs.length) return;

  const texts = secs.map(s => (s.innerText || s.textContent || '').toLowerCase());
  const links = [];
  let active = 0;

  secs.forEach((sec, i) => {
    const title = sec.querySelector(':scope > .field-title');
    if (!title) return;
    sec.id = 'setSec' + (i + 1);

    // 组名只取标题的直接文本节点，排除编号 / 项数 / 版本徽章
    const name = Array.from(title.childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).filter(Boolean).join(' ')
      .trim() || ('分组 ' + (i + 1));
    const badge = title.querySelector(':scope > .tiny');
    const badgeText = badge ? badge.textContent.trim() : '';

    const num = document.createElement('b');
    num.className = 'sec-num';
    num.textContent = String(i + 1).padStart(2, '0');
    title.insertBefore(num, title.firstChild);
    const cnt = document.createElement('span');
    cnt.className = 'sec-count';
    cnt.textContent = Math.max(1, sec.children.length - 1) + ' 项';
    title.appendChild(cnt);

    const a = document.createElement('a');
    a.href = '#';
    a.dataset.idx = String(i);
    const ni = document.createElement('i');
    ni.textContent = String(i + 1).padStart(2, '0');
    const nm = document.createElement('span');
    nm.textContent = name;
    const em = document.createElement('em');
    em.textContent = cnt.textContent;
    a.append(ni, nm, em);
    a.addEventListener('click', (e) => { e.preventDefault(); select(i); });
    list.appendChild(a);
    links.push(a);
    sec.dataset.name = name;
    sec.dataset.count = cnt.textContent;
    sec.dataset.badge = badgeText;
  });

  function allFields() {
    return Array.from(document.querySelectorAll('#settingsBody input, #settingsBody select, #settingsBody textarea'));
  }
  const getVal = (el) => el.type === 'checkbox' ? el.checked : el.value;
  const setVal = (el, v) => { if (el.type === 'checkbox') el.checked = !!v; else el.value = v; };

  function select(i) {
    if (i < 0 || i >= secs.length) return;
    active = i;
    secs.forEach((sec, k) => sec.classList.toggle('du-grp-active', k === i));
    links.forEach((a, k) => a.classList.toggle('on', k === i));
    const sec = secs[i], name = sec.dataset.name, cnt = sec.dataset.count;
    const badge = sec.dataset.badge ? ' · ' + sec.dataset.badge : '';
    if (head) head.innerHTML =
      '<h3><b>' + String(i + 1).padStart(2, '0') + '</b> ' + name + '</h3>' +
      '<p>' + cnt + ' · 仅显示当前组；9 组共用一份配置，保存即写入全部' + badge + '</p>';
    if (hint) hint.textContent = '当前组：' + name + ' · 配置写入 data/state.json；改密码后其它会话立即失效。';
    window.duSettingsCurrent = name;
    const view = document.getElementById('viewSettings');
    if (view && !view.classList.contains('hidden')) {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }

  // 「重置本组」：先用后端状态全量回填，再把非当前组的字段恢复成用户刚才的输入。
  // 于是只有当前组被重置，不依赖快照时机，也不会误清别的组。
  // 关键：整段同步执行。fillSettings 的同步部分在调用时就把 state 里的值填完了，
  // 不去 await 它后面的 registry-auth 请求——那个异步窗口会让轮询/其它回调有机可乘，
  // 把刚恢复的其它组输入又冲掉（历史上「输入框闪一下就恢复」就是这类竞态）。
  function resetGroup(i) {
    const cur = secs[i];
    const before = allFields().map(el => [el, getVal(el)]);
    let pending = null;
    try {
      if (typeof window.duFillSettings === 'function') pending = window.duFillSettings();
    } catch (e) { /* 网络失败时保留本地值，不假装成功 */ }
    before.forEach(([el, v]) => { if (!cur.contains(el)) setVal(el, v); });
    ['oldPwd', 'newPwd', 'confirmPwd'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    // 异步部分只会回填 #regAuth；若当前组不是「私有仓库认证」，恢复用户原值
    if (pending && typeof pending.then === 'function') {
      const saved = before.find(([el]) => el.id === 'regAuth');
      pending.then(() => {
        const ra = document.getElementById('regAuth');
        if (ra && saved && !cur.contains(ra)) ra.value = saved[1];
      }).catch(() => {});
    }
    if (typeof toast === 'function') toast('已重置「' + cur.dataset.name + '」', 'ok');
  }

  function applyFilter() {
    const q = ((search && search.value) || '').trim().toLowerCase();
    let shown = 0, first = -1;
    secs.forEach((sec, i) => {
      const hit = !q || texts[i].includes(q);
      links[i].classList.toggle('du-sec-hidden', !hit);
      if (hit) { shown++; if (first < 0) first = i; }
    });
    if (railEmpty) railEmpty.classList.toggle('hidden', shown > 0);
    if (railCount) railCount.textContent = String(q ? shown : secs.length);
    if (shown && links[active] && links[active].classList.contains('du-sec-hidden') && first >= 0) {
      select(first);
    }
  }
  if (search) {
    search.addEventListener('input', applyFilter);
    search.addEventListener('search', applyFilter);  // 原生清除按钮
  }

  const resetBtn = document.getElementById('btnSettingsResetGroup');
  if (resetBtn) resetBtn.addEventListener('click', () => resetGroup(active));

  const disc = document.getElementById('btnSettingsDiscard');
  if (disc) disc.addEventListener('click', () => {
    try { if (typeof window.duFillSettings === 'function') window.duFillSettings(); } catch (e) { /* ignore */ }
    ['oldPwd', 'newPwd', 'confirmPwd'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    if (typeof toast === 'function') toast('已放弃未保存的修改', 'ok');
  });

  select(0);
  window.duSettingsRailRefresh = () => {};   // 兼容旧调用：S4 不再滚动定位
  window.duSettingsSelect = select;           // 供外部 / 审计脚本切换分组
})();

window.duFillSettings = fillSettings;
if (window.duTimePicker) window.duTimePicker.bind(document);


/* ==========================================================================
   v6.0-r13 · 纯色背景（动态壁纸已停用）
   配置只存浏览器 localStorage；图片不上传、不写 NAS。背景层独立于内容层，
   只改 transform / opacity，不重建 DOM、不使用 backdrop-filter。
   ========================================================================== */
(function duWallpaper() {
  const KEY = 'du_wall_cfg', IMGKEY = 'du_wall_img';
  /* v6.0-r13：以下图片常量仅为兼容旧配置保留，apply() 会强制不加载 */
  /* r12：内置图改为「线条主题」，浅/深各一张，随包离线可用；
     旧的两张抽象壁纸仍保留为可选项。 */
  const LINES_L = '/static/bg-lines-light.webp', LINES_D = '/static/bg-lines-dark.webp';
  const PHOTO_L = '/static/bg-light.jpg', PHOTO_D = '/static/bg-dark.jpg';
  const DEF = { v: 3, on: false, pick: 'auto', motion: 'flow', speed: 48, strength: 0.40, url: '' };
  const $ = (id) => document.getElementById(id);
  const wall = $('duWall');
  if (!wall) return;

  function isDark() { return document.documentElement.getAttribute('data-theme') === 'dark'; }
  function cfg() {
    let c;
    try { c = Object.assign({}, DEF, JSON.parse(localStorage.getItem(KEY) || '{}')); }
    catch (e) { c = Object.assign({}, DEF); }
    /* 升级迁移：旧配置（抽象壁纸 + 推镜）自动切到线条流动，不打断用户自定义 */
    if (c.v !== 2) {
      if (c.pick === 'light') c.pick = 'lines-light';
      else if (c.pick === 'dark') c.pick = 'lines-dark';
      else if (c.pick === 'auto' || c.pick === 'custom' || c.pick === 'url') c.pick = c.pick;
      c.motion = 'flow'; c.v = 2;
      try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {}
    }
    c.on = false;
    return c;
  }
  function save(c) { try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {} }
  function customImg() { try { return localStorage.getItem(IMGKEY) || ''; } catch (e) { return ''; } }

  function pickUrl(c) {
    if (c.pick === 'custom') return customImg() || (isDark() ? LINES_D : LINES_L);
    if (c.pick === 'url') return (c.url || '').trim() || (isDark() ? LINES_D : LINES_L);
    if (c.pick === 'lines-light' || c.pick === 'light') return LINES_L;
    if (c.pick === 'lines-dark' || c.pick === 'dark') return LINES_D;
    if (c.pick === 'photo-light') return PHOTO_L;
    if (c.pick === 'photo-dark') return PHOTO_D;
    return isDark() ? LINES_D : LINES_L;
  }

  function apply(c) {
    c = c || cfg();
    /* v6.0-r13：恢复上一版纯色底 —— 忽略 localStorage 里任何旧开启动态背景的配置 */
    c.on = false;
    wall.dataset.on = '0';
    wall.dataset.motion = 'still';
    wall.dataset.motion = c.motion || 'flow';
    wall.style.setProperty('--du-wall-op', String(c.strength || 0.40));
    wall.style.setProperty('--du-wall-speed', (Number(c.speed) || 48) + 's');
    const u = '';
    const css = 'none';
    const a = wall.querySelector('.du-wall-a'), b = wall.querySelector('.du-wall-b');
    if (a) a.style.backgroundImage = css;
    if (b) b.style.backgroundImage = css;
    wall.dataset.src = u.indexOf('data:') === 0 ? 'custom' : u;
  }
  window.duWallApply = apply;

  /* 表单 <-> 配置 */
  function fill(c) {
    c = c || cfg();
    const m = { bgOn: c.on, bgPick: c.pick, bgMotion: c.motion,
                bgSpeed: String(c.speed), bgStrength: String(c.strength), bgUrl: c.url || '' };
    Object.keys(m).forEach(id => {
      const el = $(id); if (!el) return;
      if (el.type === 'checkbox') el.checked = !!m[id]; else el.value = m[id];
    });
    const fn = $('bgFileName');
    if (fn) { const has = !!customImg(); fn.textContent = has ? '已使用本地图片（仅存本浏览器）' : '未选择文件'; fn.classList.toggle('has-file', has); }
  }
  function read() {
    return {
      v: 3,
      on: false,
      pick: ($('bgPick') || {}).value || 'auto',
      motion: ($('bgMotion') || {}).value || 'flow',
      speed: Number(($('bgSpeed') || {}).value) || 48,
      strength: Number(($('bgStrength') || {}).value) || 0.40,
      url: (($('bgUrl') || {}).value || '').trim(),
    };
  }
  function commit(showToast) {
    const c = read(); save(c); apply(c);
    if (showToast && typeof window.toast === 'function')
      window.toast(c.on ? '线条背景已应用' : '已回到极简纯色底', 'ok');
  }

  ['bgOn', 'bgPick', 'bgMotion', 'bgSpeed', 'bgStrength'].forEach(id => {
    const el = $(id); if (el) el.addEventListener('change', () => commit(false));
  });
  const urlEl = $('bgUrl');
  if (urlEl) urlEl.addEventListener('change', () => commit(false));
  const btn = $('btnBgSave');
  if (btn) btn.onclick = () => commit(true);
  const pickBtn = $('bgPickFile'), fileEl = $('bgFile');
  if (pickBtn && fileEl) {
    pickBtn.onclick = () => fileEl.click();
    fileEl.onchange = () => {
      const f = fileEl.files && fileEl.files[0];
      if (!f) return;
      const fr = new FileReader();
      fr.onload = () => {
        const im = new Image();
        im.onload = () => {
          const maxW = 1600, sc = Math.min(1, maxW / im.width);
          const cv = document.createElement('canvas');
          cv.width = Math.round(im.width * sc); cv.height = Math.round(im.height * sc);
          cv.getContext('2d').drawImage(im, 0, 0, cv.width, cv.height);
          let data = cv.toDataURL('image/jpeg', 0.78);
          if (data.length > 900000) data = cv.toDataURL('image/jpeg', 0.6);
          try { localStorage.setItem(IMGKEY, data); } catch (e) {
            if (typeof window.toast === 'function') window.toast('图片过大，请换一张小一点的', 'err');
            return;
          }
          const pk = $('bgPick'); if (pk) pk.value = 'custom';
          const fn = $('bgFileName');
          if (fn) { fn.textContent = f.name + '（已压缩，仅存本浏览器）'; fn.classList.add('has-file'); }
          commit(true);
        };
        im.onerror = () => { if (typeof window.toast === 'function') window.toast('无法读取这张图片', 'err'); };
        im.src = String(fr.result);
      };
      fr.readAsDataURL(f);
    };
  }

  /* 明暗主题切换时自动换线条图（自动模式） */
  if (typeof MutationObserver !== 'undefined') {
    new MutationObserver(() => apply()).observe(document.documentElement,
      { attributes: true, attributeFilter: ['data-theme'] });
  }
  fill(); apply();
})();
