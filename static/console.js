/* DockerUpdate v2.0 · Docker 管理增强视图（概览 / 应用 / 容器 / 镜像 / 网络 / 备份 / 告警） */
(function () {
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = window.duEsc;
const icon = window.duIcon;
const api = window.duApi;
const toast = window.duToast;

let S = null;
let view = 'updates';
let cFilter = 'all', cQuery = '', iQuery = '', danglingOnly = false, unusedOnly = false;
/* v6.0-r15：统一手动刷新入口（带按钮反馈）；app.js 尚未加载时兜底 duLoad */
function duRefreshUI(btn) {
  return window.duRefreshWithFeedback
    ? window.duRefreshWithFeedback(btn) : window.duLoad();
}
let selected = new Set();
let detailName = '', detailTab = 'perf', detailData = null, detailSeries = [];
let modalSubmit = null;
const confirmMap = new Map();

/* ---------- v6.0：容器 / 应用卡片排列方式（3 档纯图案按钮） ---------- */
const LAYOUT_KEY = (t) => 'du_layout_' + t;
function applyLayout(target) {
  // v6.0：大卡片档已移除；旧浏览器里存的 large 自动回退到中卡片
  const saved = localStorage.getItem(LAYOUT_KEY(target));
  const mode = ['list', 'small', 'medium'].includes(saved) ? saved : 'medium';
  const body = $('#' + (target === 'apps' ? 'appsBody' : 'containersBody'));
  if (body) body.dataset.layout = mode;
  $$('[data-layout-btn][data-target="' + target + '"]').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === mode));
}
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-layout-btn]');
  if (!b) return;
  localStorage.setItem(LAYOUT_KEY(b.dataset.target), b.dataset.mode);
  applyLayout(b.dataset.target);
});

/* ---------- 工具 ---------- */
const nz = (v) => Number(v) || 0;
function bytes(n) {
  n = Number(n) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return Math.round(n) + ' B';
}
function speed(n) {
  n = Number(n) || 0;
  return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB/s'
    : n >= 1024 ? (n / 1024).toFixed(1) + ' KB/s' : Math.round(n) + ' B/s';
}
function dur(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return d ? `${d}天${h}小时` : (h ? `${h}小时${m}分` : `${m}分钟`);
}
function aliasOf(name) {
  const c = (S && S.containers || []).find(x => x.name === name);
  return c ? (c.alias || name) : name;
}
function confirm2(key, hint) {
  const now = Date.now();
  const until = confirmMap.get(key) || 0;
  if (now < until) { confirmMap.delete(key); return true; }
  confirmMap.set(key, now + 6000);
  toast(hint, 'info');
  return false;
}
async function act(kind, body, okMsg) {
  try {
    const d = await api('/api/console', { method: 'POST', body: { kind, body } });
    if (d.ok === false) { toast(d.error || d.message || '操作被拒绝', 'err'); return; }
    toast(d.message || okMsg || '操作完成', 'ok');
    await window.duLoad();
  } catch (e) { if (!e.auth) toast(e.message, 'err'); }
}
async function download(url, filename) {
  try {
    const res = await fetch(url, { headers: { Authorization: 'Bearer ' + window.duToken() } });
    if (!res.ok) { toast('下载失败：' + res.status, 'err'); return; }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    toast('已开始下载 ' + filename, 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------- SVG 折线（按容器实际像素生成 viewBox） ----------
   之前写死 viewBox="0 0 620 170" + preserveAspectRatio="none"，容器一拉伸，
   文字也跟着被横向压扁（刻度 100/75/50 变形）。现在先量出容器宽高，再让
   viewBox 与像素 1:1，文字用固定 px 字号，缩放窗口也不变形。 */
let chartSeq = 0;
const chartRegistry = new Map();
const chartColors = { primary: 'var(--primary)', green: 'var(--green)',
  orange: 'var(--orange)', purple: '#8a8a8a', cyan: '#b8b8b8' };

function chartMarkup(points, keys, opts = {}) {
  const id = 'c' + (++chartSeq);
  chartRegistry.set(id, { points, keys, opts });
  return `<div class="chart-wrap du-chart" data-chart-id="${id}"></div>`;
}

function chartSVG(spec, w, h, gradientId) {
  const { points, keys, opts } = spec;
  const n = points.length;
  if (!n) {
    return `<div class="chart-empty">还没有足够的采样数据<br>开启资源监控后，每 ${opts.interval || 3} 秒采集一次，按小时汇总</div>`;
  }
  const padL = 42, padR = 12, padT = 12, padB = 26;
  const vals = keys.map(k => points.map(p => nz(p[k.key])));
  const maxes = keys.map((k, i) => k.max || Math.max(1, ...vals[i]) * 1.15);
  const x = (i) => padL + (n === 1 ? (w - padL - padR) / 2 : i / (n - 1) * (w - padL - padR));
  const y = (v, i) => h - padB - Math.min(1, Math.max(0, v / maxes[i])) * (h - padT - padB);
  const top = maxes[0];
  const fmt = (v) => {
    if (v <= 0) return '0';
    if (top >= 100) return String(Math.round(v));
    if (top >= 10) return v.toFixed(1).replace(/\.0$/, '');
    return v.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
  };
  let grid = '';
  for (let g = 0; g <= 4; g++) {
    const gy = padT + g / 4 * (h - padT - padB);
    grid += `<line class="chart-grid" x1="${padL}" y1="${gy.toFixed(1)}" x2="${w - padR}" y2="${gy.toFixed(1)}"/>`;
    grid += `<text class="chart-tick" x="${padL - 7}" y="${(gy + 3.5).toFixed(1)}" text-anchor="end">${fmt((4 - g) / 4 * top)}</text>`;
  }
  let lines = '';
  const c0 = chartColors[keys[0].color || 'primary'];
  keys.forEach((k, i) => {
    const arr = vals[i].map((v, j) => `${x(j).toFixed(1)},${y(v, i).toFixed(1)}`).join(' ');
    if (k.area !== false && i === 0) {
      lines += `<polygon class="chart-area" fill="url(#${gradientId})" points="${padL},${(h - padB).toFixed(1)} ${arr} ${x(n - 1).toFixed(1)},${(h - padB).toFixed(1)}"/>`;
    }
    lines += `<polyline class="chart-line ${k.cls || ''}" ${k.dash ? 'stroke-dasharray="5 4"' : ''}
      points="${arr}" style="stroke:${chartColors[k.color] || (k.cls === 'g2' ? chartColors.green : k.cls === 'g3' ? chartColors.orange : c0)}"/>`;
  });
  const labels = [0, Math.floor((n - 1) / 2), n - 1].filter((v, i, a) => a.indexOf(v) === i);
  let xlab = '';
  labels.forEach(i => {
    const raw = String(points[i].t || '');
    const t = opts.longRange ? raw.slice(5, 10).replace('-', '/') : raw.slice(-5);
    xlab += `<text class="chart-tick" x="${x(i).toFixed(1)}" y="${(h - 6).toFixed(1)}" text-anchor="middle">${esc(t)}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc(opts.aria || '资源趋势')}">
    <defs><linearGradient id="${gradientId}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${c0}" stop-opacity=".42"/>
      <stop offset="100%" stop-color="${c0}" stop-opacity="0"/></linearGradient></defs>
    ${grid}${lines}${xlab}</svg>`;
}

function renderCharts() {
  const live = new Set($$('.du-chart[data-chart-id]').map(el => el.dataset.chartId));
  for (const k of chartRegistry.keys()) if (!live.has(k)) chartRegistry.delete(k);
  $$('.du-chart[data-chart-id]').forEach((el) => {
    const spec = chartRegistry.get(el.dataset.chartId);
    if (!spec) return;
    const w = Math.max(220, Math.round(el.clientWidth || 600));
    const h = Math.max(110, Math.round(el.clientHeight || 170));
    el.innerHTML = chartSVG(spec, w, h, 'duGrad_' + el.dataset.chartId);
  });
}
let chartResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(chartResizeTimer);
  chartResizeTimer = setTimeout(renderCharts, 120);
});

/* ---------- 单一分区：Docker 管理 ---------- */
let zone = 'docker';
let lastDockerView = 'updates';
/* v3.0：应用商店分区整体移除，界面只保留 Docker 管理一个分区。 */
function switchZone() {
  zone = 'docker';
  $$('.zone-btn').forEach(b => b.classList.toggle('active', b.dataset.zone === 'docker'));
  $$('.vtab').forEach(b => b.classList.toggle('hidden', b.dataset.zone !== 'docker'));
  const side = $('#zoneSide');
  if (side) side.textContent = '当前分区 · Docker 管理';
  switchView(lastDockerView || 'updates');
}
window.duSwitchZone = switchZone;
const zoneBar = $('#zoneBar');   // v3.8：侧边栏已移除，元素可能不存在
if (zoneBar) zoneBar.addEventListener('click', (e) => {
  const b = e.target.closest('[data-zone]');
  if (b) switchZone();
});

/* ---------- v3.9：容器大模块下的四个独立子页面 ---------- */
const CONTAINER_SUBS = ['containers', 'container-links', 'container-snapshots',
  'container-details', 'container-ports', 'container-mounts', 'container-symlinks'];
function isContainerSub(v) { return CONTAINER_SUBS.indexOf(v) >= 0; }
/* v6.0-r18：更新大模块的两个独立子页面（容器更新 / 镜像更新） */
const UPDATE_SUBS = ['updates', 'image-updates'];
function isUpdateSub(v) { return UPDATE_SUBS.indexOf(v) >= 0; }

/* ---------- 视图切换 ---------- */
function switchView(v) {
  view = v;
  lastDockerView = v;
  // 子页面仍高亮顶部「容器」大模块；设置 / 运行日志不高亮任何模块
  const navView = isContainerSub(v) ? 'containers' : (isUpdateSub(v) ? 'updates' : v);
  $$('.vtab').forEach(b => b.classList.toggle('active',
    (v !== 'settings' && v !== 'logs') && b.dataset.view === navView));
  const setBtn = $('#btnSettings');
  if (setBtn) setBtn.classList.toggle('active', v === 'settings');
  const logBtn = $('#btnLogs');
  if (logBtn) logBtn.classList.toggle('active', v === 'logs');
  const ids = { overview: 'viewOverview', apps: 'viewApps', containers: 'viewContainers',
    'container-links': 'viewContainerLinks',
    'container-snapshots': 'viewContainerSnapshots',
    'container-details': 'viewContainerDetails',
    'container-ports': 'viewContainerPorts',
    'container-mounts': 'viewContainerMounts',
    'container-symlinks': 'viewContainerSymlinks',
    updates: 'viewUpdates', 'image-updates': 'viewImageUpdates', images: 'viewImages',
    alerts: 'viewAlerts', system: 'viewSystem', settings: 'viewSettings',
    logs: 'viewLogs' };
  Object.entries(ids).forEach(([k, id]) => {
    const el = $('#' + id);
    if (el) el.classList.toggle('hidden', k !== v);
  });
  const st = $('#duSubtabs');
  if (st) {
    st.classList.toggle('hidden', !isContainerSub(v));
    $$('.subtab', st).forEach(b => b.classList.toggle('active', b.dataset.sub === v));
  }
  const ust = $('#duUpdateSubtabs');
  if (ust) {
    ust.classList.toggle('hidden', !isUpdateSub(v));
    $$('.subtab', ust).forEach(b => b.classList.toggle('active', b.dataset.sub === v));
  }
  if (v === 'logs' && window.duFillLogRetention) window.duFillLogRetention();
  if (v === 'settings' && window.duFillSettings) window.duFillSettings();
  if (S) renderView();
  window.scrollTo(0, 0);
}
$('#viewTabs').addEventListener('click', (e) => {
  const b = e.target.closest('[data-view]');
  if (b) switchView(b.dataset.view);
});
const duSubtabs = $('#duSubtabs');
if (duSubtabs) duSubtabs.addEventListener('click', (e) => {
  const b = e.target.closest('[data-sub]');
  if (b) switchView(b.dataset.sub);
});
const duUpdateSubtabs = $('#duUpdateSubtabs');
if (duUpdateSubtabs) duUpdateSubtabs.addEventListener('click', (e) => {
  const b = e.target.closest('[data-sub]');
  if (b) switchView(b.dataset.sub);
});
/* 本地镜像页「镜像更新」按钮：跳到更新大模块下的独立镜像更新页 */
document.addEventListener('click', (e) => {
  if (e.target.closest('#btnGotoImageUpdates')) switchView('image-updates');
});

/* ---------- 主渲染 ---------- */
function renderStatusBar() {
  if (!S) return;
  const st = S.stats || {}, df = (S.console || {}).docker_df || {};
  const put = (id, val) => { const el = $('#' + id); if (el) el.textContent = val; };
  put('sbEngine', S.docker_connected ? ('v' + (S.docker_version || '').replace(/^v/, '')) : '离线');
  put('sbContainers', st.total != null ? st.total : '—');
  put('sbRunning', st.running != null ? st.running : '—');
  put('sbUpdates', st.update != null ? st.update : '—');
  put('sbImages', df.images != null ? df.images : '—');
  put('sbVolNet', (df.volumes != null && df.networks != null)
      ? (df.volumes + ' / ' + df.networks) : ((df.volumes != null) ? String(df.volumes) : '—'));
  put('sbLast', S.last_full_check ? String(S.last_full_check).slice(5, 16) : '—');
  put('sbNext', S.next_check ? String(S.next_check).slice(5, 16) : '—');
}
function renderView() {
  renderStatusBar();
  if (!S || !S.console) return;
  applyLayout('containers'); applyLayout('apps');
  if (view === 'overview') renderOverview();
  else if (view === 'apps') renderApps();
  else if (view === 'containers') renderContainers();
  else if (view === 'container-links') renderContainerLinks();
  else if (view === 'container-snapshots') renderSnapshots();
  else if (view === 'container-details' && window.duProContainers) window.duProContainers();
  else if (view === 'container-ports') renderPorts();
  else if (view === 'container-mounts' && window.duMounts) window.duMounts.render();
  else if (view === 'container-symlinks' && window.duSymlinks) window.duSymlinks.render();
  else if (view === 'image-updates') renderImageUpdates();
  else if (view === 'images') renderImages();
  else if (view === 'alerts') { renderAlerts(); renderNetWatch(); }
  else if (view === 'system' && window.duProRender) window.duProRender(view);
  /* v6.0-r23：设置页只在「切入该页」时回填一次。以前放在 renderView 里，
     5 秒自动轮询每次都会把输入框冲回后端值，表现为「输入闪一下就恢复」
     「保存后又变回去」「重置本组误清其它组草稿」。回填改由 switchView 触发，
     「重置本组 / 放弃修改」仍显式调用 duFillSettings，不受影响。 */
  if (view === 'overview' && window.duProOverview) window.duProOverview();
  renderLinks();
  renderCharts();
  if (window.duRenderTrendStrip) window.duRenderTrendStrip(view);
  if (detailName && !$('#containerDrawer').classList.contains('open')) detailName = '';
}
/* 供其它模块（概览快捷入口）跳转到指定页面 */
window.duGotoView = (v) => switchView(v);
/* v6.0-r24：目录映射页在独立脚本里，需要通过桥调用本 IIFE 内的弹窗 / 二次确认。
   直接写 openModal() 会抛 ReferenceError（console.js 整体包在 IIFE 中）。 */
window.duOpenModal = openModal;
window.duCloseModal = closeModal;
window.duConfirm2 = confirm2;

function renderLinks() {
  if (!S || !S.console) return;
  const l = S.console.links || {};
  // v2.1-r9：左下角「开源地址」已按用户要求移除，这里只保留「我的导航站」
  const nav = $('#btnNav');
  if (nav) {
    nav.classList.toggle('hidden', !(l.show_nav && l.nav));
    if (l.nav) nav.href = l.nav;
  }
}


/* ---------- v3.5：备份能力迁入设置抽屉 ---------- */
(function bindBackupSettings() {
  const ex = document.getElementById('bkExportSetting');
  const im = document.getElementById('bkImportSetting');
  const br = document.getElementById('bkRestoreBrowse');
  const dir = document.getElementById('bkRestoreDirSetting');
  const chk = document.getElementById('bkRestoreComposeSetting');
  const file = document.getElementById('bkRestoreFile');
  const fname = document.getElementById('bkRestoreFileName');
  if (file && fname) file.onchange = () => {
    fname.textContent = file.files && file.files[0] ? file.files[0].name : '未选择文件';
    fname.classList.toggle('has-file', !!(file.files && file.files[0]));
  };
  if (br) br.onclick = () => {
    if (window.duOpenDirPicker) window.duOpenDirPicker(p => {
      if (dir) { dir.value = p.replace(/\/$/, ''); toast('已填入目录 ' + dir.value, 'ok'); }
    });
    else toast('目录选择器未就绪，请手动输入路径', 'err');
  };
  if (ex) ex.onclick = () => download('/api/backup-export', 'dockerupdate-backup.zip');
  if (im) im.onclick = async () => {
    const f = file && file.files[0];
    if (!f) { toast('请先选择备份 zip 文件', 'err'); return; }
    const restore = !!(chk && chk.checked);
    const d = dir ? dir.value.trim() : '';
    if (restore && !d) { toast('勾选写回后请填写或选择恢复目录', 'err'); return; }
    const bytes = new Uint8Array(await f.arrayBuffer());
    let b64 = '';
    for (let i = 0; i < bytes.length; i += 0x8000)
      b64 += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    try {
      const r = await api('/api/backup-import', { method: 'POST', body: {
        data: btoa(b64), restore_compose: restore, compose_dir: d } });
      toast(r.message || '恢复完成', 'ok');
      await window.duLoad();
    } catch (e) { if (!e.auth) toast(e.message || '恢复失败', 'err'); }
  };
})();

/* v3.9-r6：稳定的「结构签名」。
   r5 直接 JSON.stringify 整个 state，真实服务里 server_time 每秒变、
   runtime.message/progress 每次轮询变、metrics/trend 每 3 秒采样也变，
   签名几乎永远不同，等于没有缓存，检测时整页还是会闪。
   这里只取会改变页面结构的字段；采样类易变字段在 busy 期间不触发重建，
   检测结束（busy false）、容器状态变化、链接设置变化或手动切页时再补绘。 */
let duConsoleSig = '';
function duConsoleStableSig(s, busy) {
  const c = (s && s.console) || {};
  const g = (s && s.settings) || {};
  const cs = ((s && s.containers) || []).map(x => ({
    name: x.name, status: x.status, running: x.running, created: x.created,
    started_at: x.started_at, ports: x.ports, image_ref: x.image_ref,
    image_id: x.image_id, tag: x.tag, version_label: x.version_label,
    alias: x.alias, memo: x.memo, has_note: x.has_note,
    has_update: x.has_update, can_update: x.can_update, policy: x.policy,
    excluded: x.excluded, checked_at: x.checked_at, error: x.error,
    error_kind: x.error_kind, skipped: x.skipped,
    backups: (x.backups || []).map(b => [b.id, b.time, b.image_id])
  }));
  // 只纳入会影响链接/顶栏的设置，不把整份 settings 放进签名，避免表单字段抖动触发重绘
  const linkSet = {
    container_links: g.container_links || {}, app_links: g.app_links || {},
    lan_base: g.lan_base || '', wan_base: g.wan_base || '',
    link_mode: g.link_mode || '', link_probe: !!g.link_probe,
    show_nav: !!g.show_nav, nav: g.nav || ''
  };
  return JSON.stringify({
    busy: !!busy,
    docker_connected: s && s.docker_connected,
    docker_version: s && s.docker_version,
    docker_error: s && s.docker_error,
    stats: s && s.stats,
    settings: linkSet,
    containers: cs,
    last_full_check: s && s.last_full_check,
    next_check: s && s.next_check,
    console: {
      // 故意不含 host / metrics / trend / monitor：它们是采样数据，不该让整页重建
      docker_df: c.docker_df, apps: c.apps, app_stats: c.app_stats,
      images: c.images, networks: c.networks, volumes: c.volumes,
      compose_files: c.compose_files, compose_root: c.compose_root,
      top: c.top, suggestions: c.suggestions, alerts: c.alerts,
      alert_history: c.alert_history, alert_summary: c.alert_summary,
      links: c.links
    }
  });
}
window.duConsoleRender = function (s) {
  S = s;
  window.__duState = s;
  const busy = !!(s && s.runtime && s.runtime.busy);
  let sig = '';
  try { sig = duConsoleStableSig(s, busy); } catch (e) { sig = String(Date.now()); }
  const changed = sig !== duConsoleSig;
  // busy 期间：只有「页面结构真的变了」才重绘；否则只更新状态带文本。
  // 非 busy（或检测刚开始/刚结束造成 busy 翻转）一律允许补绘一次。
  if (!busy || changed) {
    duConsoleSig = sig;
    renderView();
  } else {
    renderStatusBar();
  }
  if (detailName && $('#containerDrawer').classList.contains('open')) {
    const live = ((s.console || {}).metrics || {})[detailName] || {};
    if (detailData) { detailData.live = live; if (detailTab === 'perf') renderDetailTab(); }
  }
};
/* 用户主动改视图 / 筛选 / 排列后，允许下一次轮询强制补绘一次 */
window.duConsoleForceRender = function () { duConsoleSig = ''; };

/* ---------- 概览 ---------- */
let trendRange = 'today';
let trendDate = '';
let paused = localStorage.getItem('du_paused') === '1';

function dayShift(day, delta) {
  const d = new Date(day + 'T00:00:00');
  d.setDate(d.getDate() + delta);
  return d.toISOString().slice(0, 10);
}
function filterTrend(all) {
  if (!all.length) return all;
  const lastDay = String(all[all.length - 1].t || '').slice(0, 10);
  if (trendRange === '7d') return all.slice(-168);
  if (trendRange === '30d') return all.slice(-720);
  if (trendRange === 'date') return trendDate ? all.filter(p => String(p.t).slice(0, 10) === trendDate) : all;
  if (trendRange === 'yesterday') {
    const y = dayShift(lastDay, -1);
    return all.filter(p => String(p.t).slice(0, 10) === y);
  }
  return all.filter(p => String(p.t).slice(0, 10) === lastDay);
}
function trendStats(pts, h) {
  const down = pts.map(p => nz(p.net_down ?? p.net));
  const up = pts.map(p => nz(p.net_up ?? 0));
  const cpu = pts.map(p => nz(p.cpu));
  const gb = pts.map(p => nz(p.mem_gb ?? (nz(p.mem) * nz(h.mem_total) / 100 / 1073741824)));
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const avg = (a) => a.length ? sum(a) / a.length : 0;
  return {
    netPeak: Math.max(0, ...pts.map((p, i) => down[i] + up[i])),
    netDown: sum(down), netUp: sum(up),
    cpuPeak: Math.max(0, ...cpu), cpuAvg: avg(cpu),
    memPeak: Math.max(0, ...gb), memAvg: avg(gb),
  };
}
function miniCard(title, nowHtml, subHtml, footHtml, points, keys, opts) {
  return `<div class="trend-mini">
    <div class="tm-head"><span class="tm-title">${title}</span><span class="tm-now">${nowHtml}</span></div>
    <div class="tm-sub">${subHtml}</div>
    <div class="tm-chart">${chartMarkup(points, keys, opts)}</div>
    <div class="tm-foot">${footHtml}</div>
  </div>`;
}
function rankCard(title, rows, unit, color, scaleNote) {
  const max = Math.max(0.0001, ...rows.map(r => nz(r.value)));
  const fmt = (v) => unit === 'uptime' ? dur(v)
    : unit === 'net' ? speed(v) : unit === 'mem' ? v + ' MB' : v + '%';
  return `<div class="panel glass rank-panel">
    <div class="panel-head"><h3>${icon('i-chart')} ${title}</h3><span class="sub">Top 5 · ${scaleNote}</span></div>
    ${rows.length ? `<div class="rank-list">${rows.map((r, i) => `<div class="rank-item">
      <span class="rank-no">${i + 1}</span>
      <div class="rank-main"><span class="rank-name" title="原名 ${esc(r.name)}">${esc(r.alias)}</span>
        <div class="rank-bar"><i class="bar-${color}" style="width:${(nz(r.value) / max * 100).toFixed(1)}%"></i></div></div>
      <span class="rank-val">${fmt(r.value)}</span>
    </div>`).join('')}</div>` : '<div class="empty-state">暂无运行中容器</div>'}
    <div class="rank-note">进度条长度按各榜第一名的值等比缩放，不是占整机比例</div>
  </div>`;
}

function renderOverview() {
  const c = S.console, h = c.host || {}, df = c.docker_df || {}, st = c.app_stats || {};
  const mon = c.monitor || {}, allTrend = c.trend || [];
  // 兼容升级前保存的 metrics.json（只有 mem% / net MB/s，没有 net_down / mem_gb）
  const memGBNow = nz(h.mem_used) / 1073741824;
  const pts = filterTrend(allTrend).map(p => ({
    ...p,
    net_down: nz(p.net_down ?? p.net),
    net_up: nz(p.net_up ?? 0),
    mem_gb: nz(p.mem_gb ?? (nz(p.mem) * memGBNow / 100)),
  }));
  const stat = trendStats(pts, h);
  const memGB = memGBNow.toFixed(2);
  const diskFree = bytes(nz(h.disk_free));
  const longRange = trendRange === '7d' || trendRange === '30d';
  const kpi = (label, num, sub, cls, ico) => `<div class="kpi glass ${cls}">
    <div class="k-label"><span>${label}</span>${icon(ico)}</div>
    <div class="k-num">${num}</div><div class="k-sub">${sub}</div></div>`;
  const sug = (c.suggestions || []).map(x => `<div class="sug ${x.level}">
    <span class="sug-ic">${icon(x.level === 'high' ? 'i-shield' : 'i-info')}</span>
    <div class="sug-body"><b>${esc(x.title)} · ${esc(aliasOf(x.name))}</b><p>${esc(x.detail)}</p></div>
    <span class="sug-level">${{high:'高',mid:'中',low:'低'}[x.level] || '低'}</span></div>`).join('');
  const top = c.top || {};
  const ranges = [['today','今天'],['yesterday','昨天'],['7d','近 7 天'],['30d','近 30 天']];

  $('#overviewBody').innerHTML = `
    <div class="page-head"><div><h2>概览</h2>
      <p>${esc(h.hostname || 'NAS')} · ${esc(h.ip || '')} · ${esc(h.cpu_model || '')} · ${h.cores || 0} 核 · 运行 ${dur(h.uptime)}</p></div>
      <div class="head-actions">
        <button class="btn ghost" id="btnPause">${icon(paused ? 'i-play' : 'i-pause')} ${paused ? '继续刷新' : '暂停'}</button>
        <button class="btn ghost" id="btnOverviewRefresh">${icon('i-refresh')} 刷新</button>
      </div></div>
    <div class="grid-4">
      ${kpi('应用总数', st.total || 0, `${S.stats.total} 个容器 · ${st.running_apps || 0} 个全运行`, 'info', 'i-apps')}
      ${kpi('异常', st.abnormal || 0, '需要立即处理', st.abnormal ? 'bad' : 'ok', 'i-shield')}
      ${kpi('注意', st.attention || 0, '建议关注', st.attention ? 'warn' : 'ok', 'i-info')}
      ${kpi('资源占用', memGB + '<small>GB</small>', `CPU ${h.cpu_percent || 0}% · 磁盘可用 ${diskFree}`, 'info', 'i-cpu')}
    </div>

    <div class="panel glass trend-panel" style="margin-top:16px">
      <div class="panel-head"><h3>${icon('i-chart')} 性能趋势</h3>
        <span class="sub">${mon.sample_interval || 3} 秒采样 · 按小时汇总 · 自 ${esc(String(mon.started_at || '').slice(0,10))} 起 · 共 ${mon.points || 0} 个点</span>
        <div class="trend-ranges">
          ${ranges.map(([k, label]) => `<button class="chip ${trendRange === k ? 'active' : ''}" data-range="${k}">${label}</button>`).join('')}
          <input type="date" id="trendDate" class="input" value="${esc(trendDate)}" title="选择某一天">
        </div></div>
      <div class="trend-trio ${pts.length ? '' : 'trend-empty'}">
        ${miniCard('流量',
          `<span class="tm-now-down">↓ ${speed(h.net_rx_bps)}</span> · <span class="tm-now-up">↑ ${speed(h.net_tx_bps)}</span>`,
          `峰值 ${stat.netPeak.toFixed(2)} MB/小时`,
          `总 ${(stat.netDown + stat.netUp).toFixed(1)} MB · 下 ${stat.netDown.toFixed(1)} MB · 上 ${stat.netUp.toFixed(1)} MB`,
          pts, [
            { key: 'net_down', color: 'primary' },
            { key: 'net_up', color: 'orange', dash: true, area: false },
          ], { interval: mon.sample_interval, longRange, aria: '每小时上下行流量' })}
        ${miniCard('CPU',
          `<b>${h.cpu_percent || 0}%</b>`,
          `峰值 ${stat.cpuPeak.toFixed(1)}% · 均值 ${stat.cpuAvg.toFixed(1)}% · ${h.cores || 0} 核`,
          `当前 ${h.cpu_percent || 0}% · 负载 ${(h.loadavg || []).join(' / ')}`,
          pts, [{ key: 'cpu', max: 100, color: 'green' }],
          { interval: mon.sample_interval, longRange, aria: 'CPU 使用率趋势' })}
        ${miniCard('内存',
          `<b>${memGB} GB</b>`,
          `峰值 ${stat.memPeak.toFixed(2)} GB · 均值 ${stat.memAvg.toFixed(2)} GB`,
          `共 ${bytes(h.mem_total)} · 占用 ${h.mem_percent || 0}% · SWAP ${bytes(h.swap_used)}`,
          pts, [{ key: 'mem_gb', color: 'purple' }],
          { interval: mon.sample_interval, longRange, aria: '内存占用趋势' })}
      </div>
      <div class="chart-legend trend-legend">
        <span><i></i> 下行流量</span><span><i class="g3"></i> 上行流量</span>
        <span><i class="g2"></i> CPU %</span><span><i class="g4"></i> 内存 GB</span>
        <span>当前范围 <b>${pts.length}</b> 个小时点${trendRange === 'date' && trendDate ? ' · ' + esc(trendDate) : ''}</span>
      </div>
    </div>

    <div class="grid-2">
      ${rankCard('CPU 占用 Top 5', top.cpu || [], 'cpu', 'blue', '占整机 ' + (h.cores || 0) + ' 核')}
      ${rankCard('内存占用 Top 5', top.mem || [], 'mem', 'purple', '占整机 ' + (nz(h.mem_total) / 1073741824).toFixed(1) + ' GB')}
    </div>
    <div class="grid-2">
      ${rankCard('网络占用 Top 5', top.net || [], 'net', 'orange', '上下行合计')}
      ${rankCard('运行时长 Top 5', top.uptime || [], 'uptime', 'cyan', '连续运行')}
    </div>
    <div class="panel glass">
      <div class="panel-head"><h3>${icon('i-shield')} 优化建议</h3><span class="sub">根据当前运行状态自动分析 · 共 ${(c.suggestions || []).length} 条</span></div>
      <div class="sug-list">${sug || '<div class="empty-state">当前没有发现明显问题 👍</div>'}</div>
    </div>
    <div class="panel glass">
      <div class="panel-head"><h3>${icon('i-hdd')} 宿主机与 Docker</h3></div>
      <dl class="kv">
        <dt>处理器</dt><dd>${esc(h.cpu_model || '—')} · ${h.cores || 0} 核</dd>
        <dt>内存</dt><dd>${bytes(h.mem_used)} / ${bytes(h.mem_total)}（${h.mem_percent || 0}%）· SWAP ${bytes(h.swap_used)} / ${bytes(h.swap_total)}</dd>
        <dt>磁盘</dt><dd>${(h.disks || []).map(d => `${esc(d.path)} ${d.percent}% · 可用 ${bytes(d.free)}`).join('；') || '—'}</dd>
        <dt>负载</dt><dd>${(h.loadavg || []).join(' / ')}</dd>
        <dt>网络</dt><dd>↓ ${speed(h.net_rx_bps)} · ↑ ${speed(h.net_tx_bps)}</dd>
        <dt>Docker</dt><dd>v${esc(S.docker_version || '—')} · 镜像 ${df.images || 0} 个（${df.images_size || 0} MB）· 可回收 ${df.reclaimable || 0} MB · 卷 ${df.volumes || 0} 个</dd>
      </dl>
    </div>`;

  // 时间范围 / 日期 / 暂停 / 刷新 / 添加容器
  $$('#overviewBody [data-range]').forEach(b => b.onclick = () => {
    trendRange = b.dataset.range; trendDate = '';
    renderOverview(); renderCharts();
  });
  const dp = $('#trendDate');
  if (dp) dp.onchange = (e) => {
    if (!e.target.value) { trendRange = 'today'; trendDate = ''; }
    else { trendRange = 'date'; trendDate = e.target.value; }
    renderOverview(); renderCharts();
  };
  const pauseBtn = $('#btnPause');
  if (pauseBtn) pauseBtn.onclick = () => {
    paused = !paused;
    localStorage.setItem('du_paused', paused ? '1' : '0');
    if (window.duSetPaused) window.duSetPaused(paused);
    renderOverview(); renderCharts();
    toast(paused ? '已暂停自动刷新，点「继续刷新」恢复' : '已恢复自动刷新', 'info');
  };
  const br = $('#btnOverviewRefresh');
  if (br) br.onclick = () => duRefreshUI(br);
  if (window.duSetPaused) window.duSetPaused(paused);
}

/* ---------- 应用 ---------- */
function renderApps() {
  const apps = S.console.apps || [], st = S.console.app_stats || {};
  const health = { ok: ['正常','ok'], warn: ['注意','warn'], error: ['异常','bad'] };
  $('#appsBody').innerHTML = `
    <div class="grid-4" style="margin-bottom:16px">
      <div class="kpi glass info"><div class="k-label">应用总数</div><div class="k-num">${st.total || 0}</div><div class="k-sub">按 Compose 项目聚合</div></div>
      <div class="kpi glass ok"><div class="k-label">运行正常</div><div class="k-num">${st.running_apps || 0}</div><div class="k-sub">所有成员均在运行</div></div>
      <div class="kpi glass warn"><div class="k-label">注意</div><div class="k-num">${st.attention || 0}</div><div class="k-sub">部分容器未运行</div></div>
      <div class="kpi glass bad"><div class="k-label">异常</div><div class="k-num">${st.abnormal || 0}</div><div class="k-sub">全部成员已停止</div></div>
    </div>
    <div class="app-grid">${apps.length ? apps.map(a => {
      const [label, cls] = health[a.health] || health.ok;
      return `<div class="app-card glass">
        <div class="app-top"><div style="min-width:0"><div class="app-name">${esc(a.display)}</div>
          <div class="app-sub">${a.project ? '项目 ' + esc(a.project) : '独立容器'} · ${a.count} 个容器 · 运行 ${a.running}/${a.count}</div></div>
          <div class="app-quick">
            <button class="mini-act icon-only app-mini" data-app="app-link" data-key="${esc(a.key)}"
              title="设置该应用的内网 / 外网地址" aria-label="设置访问地址">${icon('i-link')}</button>
          </div>
          <span class="lvl ${cls}">${label}</span></div>
        <div class="app-metrics">
          <div><span>CPU</span><b>${a.cpu}%</b></div><div><span>内存</span><b>${a.mem} MB</b></div>
          <div><span>网络</span><b>${speed(a.net)}</b></div></div>
        <div class="app-members">${a.members.map(n => `<span class="m ${(S.containers.find(c=>c.name===n)||{}).running ? '' : 'off'}" title="${esc(n)}">${esc(a.aliases[n] || n)}</span>`).join('')}</div>
        ${a.updates ? `<div class="subtle" style="margin-bottom:10px">${icon('i-upload')} ${a.updates} 个容器发现新版本</div>` : ''}
        <div class="app-acts">
          <button class="mini-act act-start" data-app="start" data-key="${esc(a.key)}">${icon('i-play')} 启动</button>
          <button class="mini-act act-stop" data-app="stop" data-key="${esc(a.key)}">${icon('i-stop')} 停止</button>
          <button class="mini-act act-restart" data-app="restart" data-key="${esc(a.key)}">${icon('i-refresh')} 重启</button>
          <button class="mini-act act-open" data-app="app-open" data-key="${esc(a.key)}"
            title="检测当前网络，自动选内网 / 外网地址打开">${icon('i-external')} 打开 WebUI</button>
          <button class="mini-act" data-app="detail" data-key="${esc(a.key)}">${icon('i-info')} 成员详情</button>
        </div></div>`;
    }).join('') : '<div class="empty-state glass">暂无应用</div>'}</div>`;
  $('#appsBody').querySelectorAll('[data-app]').forEach(b => {
    b.onclick = async () => {
      const action = b.dataset.app, key = b.dataset.key;
      const a = apps.find(x => x.key === key) || { members: [] };
      if (action === 'detail') {
        if (a.members.length) openDetail(a.members[0]);
        return;
      }
      if (action === 'app-link') return openAppLinkSettings(key, a);
      if (action === 'app-open') return openAppWeb(key, a.members);
      if (!confirm2('app:' + action + ':' + key, `再次点击确认${action === 'start' ? '启动' : action === 'stop' ? '停止' : '重启'}整个应用`)) return;
      await act('app', { key, action });
    };
  });
  const br = $('#btnAppsRefresh'); if (br) br.onclick = () => duRefreshUI(br);
}

/* ---------- v3.9-r3：应用聚合访问地址（内网自动带端口 / 外网逐应用自填 / 正确跳转） ---------- */
function appOwnLink(key) { return ((allLinkSettings().app_links || {})[key]) || {}; }
/* 取该应用第一个成员的宿主机映射端口，用来自动填内网地址 */
function appFirstPort(members) {
  const selfPort = String(location.port || '');
  for (const n of members || []) {
    const c = (S.containers || []).find(x => x.name === n);
    for (const p of ((c && c.ports) || [])) {
      const m = String(p).match(/^(\d+)/);
      if (m && m[1] !== selfPort) return m[1];   // 面板自身端口不能当应用端口
    }
  }
  return '';
}
function appAutoLan(port) {
  const proto = location.protocol === 'https:' ? 'https://' : 'http://';
  return proto + (location.hostname || '192.168.1.110') + (port ? ':' + port : '');
}
/* 把用户输入补成可真正打开的绝对 URL：补协议、替换 {port}、仅在缺端口时补端口、拒绝指向面板自身 */
function finalizeWebUrl(raw, port, allowPort) {
  let s = String(raw || '').trim();
  if (!s) return '';
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(s)) s = 'http://' + s;
  if (s.indexOf('{port}') >= 0) {
    if (!port) return '';
    s = s.replace(/\{port\}/g, port);
  }
  try {
    const u = new URL(s);
    if (allowPort && port && !u.port) u.port = port;
    if (u.origin === location.origin && (u.pathname === '/' || u.pathname === '')) return '';
    return u.toString();
  } catch (e) { return ''; }
}
function appLinkBase(key, port) {
  const g = allLinkSettings(), own = appOwnLink(key);
  return {
    lan: own.lan_base || appAutoLan(port),
    wan: own.wan_base || g.wan_base || '',
    mode: own.link_mode || g.link_mode || 'auto',
  };
}
function openAppLinkSettings(key, a) {
  const g = allLinkSettings(), own = appOwnLink(key);
  const members = (a && a.members) || [];
  const port = appFirstPort(members), autoLan = appAutoLan(port);
  const lanVal = (own.auto_lan !== false || !own.lan_base) ? autoLan : own.lan_base;
  openModal('应用访问地址设置', (a.display || key) + ' · 只对该应用生效，不影响其它应用', `
    <div class="notice small-notice">${icon('i-link')}<span>「打开 WebUI」会先判断当前访问面板的是内网还是外网，再选用对应地址。
      <strong>内网地址已按容器端口自动填好</strong>${port ? '（检测到 ' + esc(port) + '）' : '（该应用暂无映射端口）'}；
      <strong>外网地址请自己填</strong>，每个应用域名不同，支持 <code>{port}</code> 占位符。</span></div>
    <div class="pro-form">
      <label>内网地址 <span class="tiny">（自动检测端口，可改）</span></label>
      <input class="input" id="apLan" value="${esc(lanVal)}" placeholder="${esc(autoLan)}"
        autocomplete="off" data-lpignore="true" data-form-type="other">
      <label>外网 / 公网地址 <span class="tiny">（每个应用单独填完整链接）</span></label>
      <input class="input" id="apWan" value="${esc(own.wan_base || '')}" placeholder="例如 https://adguard.example.com:55555/"
        autocomplete="off" data-lpignore="true" data-form-type="other">
      <label>选择方式</label><select class="input" id="apMode">
        <option value="auto" ${!own.link_mode || own.link_mode === 'auto' ? 'selected' : ''}>自动检测（推荐）</option>
        <option value="lan" ${own.link_mode === 'lan' ? 'selected' : ''}>始终使用内网地址</option>
        <option value="wan" ${own.link_mode === 'wan' ? 'selected' : ''}>始终使用外网地址</option></select>
      <p class="store-hint">保存后点卡片上的「打开 WebUI」即按所选地址新开页面；外网留空时从公网访问会提示先填写。</p>
    </div>`, async () => {
    const lan = ($('#apLan').value || '').trim();
    const links = Object.assign({}, g.app_links || {});
    links[key] = { lan_base: lan, auto_lan: (!lan || lan === autoLan),
                   wan_base: ($('#apWan').value || '').trim(),
                   link_mode: $('#apMode').value };
    try {
      await api('/api/settings', { method: 'POST', body: { settings: { app_links: links } } });
      toast((a.display || key) + ' 的访问地址已保存', 'ok');
      await window.duLoad();
    } catch (e) { if (!e.auth) toast(e.message || '保存失败', 'err'); }
  });
}
function openAppWeb(key, members) {
  const port = appFirstPort(members);
  const own = appOwnLink(key), g = allLinkSettings();
  const s = appLinkBase(key, port);
  let mode = linkOverride() || s.mode;
  if (mode === 'auto') {
    if (isPrivateHost(location.hostname)) mode = 'lan';
    else if (lanReachable === true) mode = 'lan';
    else mode = 'wan';
  }
  const raw = mode === 'lan' ? s.lan : (own.wan_base || g.wan_base || '');
  if (!raw) {
    toast('请先点链接图标填写该应用的外网地址', 'err');
    openAppLinkSettings(key, { display: key, members: members || [] });
    return;
  }
  const url = finalizeWebUrl(raw, port, mode === 'lan');
  if (!url) {
    toast('地址无法解析或指向面板自身，请点链接图标检查端口', 'err');
    openAppLinkSettings(key, { display: key, members: members || [] });
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
  toast('已按' + (mode === 'lan' ? '内网' : '外网') + '地址打开 · ' + url, 'ok');
}

/* ---------- v3.1：容器端口访问链接（内网 / 外网自动选择） ---------- */
let linkProbeAt = 0, lanReachable = null;

function isPrivateHost(h) {
  h = String(h || '').toLowerCase();
  if (!h || h === 'localhost' || h.endsWith('.local') || h === '::1') return true;
  if (/^(\d{1,3}\.){3}\d{1,3}$/.test(h)) {
    const p = h.split('.').map(Number);
    return p[0] === 10 || p[0] === 127 ||
      (p[0] === 192 && p[1] === 168) || (p[0] === 172 && p[1] >= 16 && p[1] <= 31);
  }
  return false;
}
/* v3.2：每个容器的外网网址可能都不同，地址改为「容器单独配置 > 全局默认」。
   全局设置里的两个地址只作为没有单独配置的容器的兜底默认值。 */
function allLinkSettings() { return (S && S.settings) || {}; }
function ownLink(name) { return (allLinkSettings().container_links || {})[name] || null; }
function linkSettings(name) {
  const g = allLinkSettings(), own = ownLink(name) || {};
  return Object.assign({}, g, {
    lan_base: own.lan_base || g.lan_base || '',
    wan_base: own.wan_base || g.wan_base || '',
    link_mode: own.link_mode || g.link_mode || 'auto',
  });
}
function linkOverride() { return localStorage.getItem('du_link_override') || ''; }
function linkBase(name) {
  const s = linkSettings(name);
  const lan = String(s.lan_base || '').trim() || (location.protocol + '//' + location.hostname);
  const wan = String(s.wan_base || '').trim();
  let mode = linkOverride() || s.link_mode || 'auto';
  if (mode === 'auto') {
    if (isPrivateHost(location.hostname)) mode = 'lan';
    else if (lanReachable === true) mode = 'lan';   // 公网域名但内网探测可达
    else mode = 'wan';
  }
  return { base: mode === 'lan' ? lan : (wan || lan), mode, lan, wan };
}
function portUrl(hostPort, name) {
  const base = linkBase(name).base;
  if (!base) return '';
  // v3.9-r3：与应用 WebUI 同一套归一化，裸地址补协议、已有端口不再重复追加
  const u = finalizeWebUrl(base, String(hostPort), true);
  if (u) return u;
  return base.indexOf('{port}') >= 0 ? base.replace('{port}', String(hostPort)) : base;
}
/* 每个容器卡片上的「链接设置」弹窗 */
function openLinkSettings(name) {
  const g = allLinkSettings(), own = ownLink(name) || {};
  openModal('容器访问链接设置', name + ' · 该容器单独生效，不影响其它容器', `
    <div class="notice small-notice">${icon('i-link')}<span>每个容器可以填自己的内网 / 外网地址。内网留空会自动用当前访问主机加该容器端口；外网请按每个容器自己的域名填写。
      外网支持 <code>{port}</code> 占位符（如 <code>https://jellyfin.example.com</code> 或 <code>https://nas.example.com/app/{port}</code>）；不写占位符会自动追加 <code>:端口</code>。</span></div>
    <div class="pro-form">
      <label>这个容器的内网地址</label>
      <input class="input" id="lkLan" value="${esc(own.lan_base || '')}" placeholder="留空用全局默认 ${esc(g.lan_base || '当前访问主机')}"
        autocomplete="off" data-lpignore="true" data-form-type="other">
      <label>这个容器的外网 / 公网地址</label>
      <input class="input" id="lkWan" value="${esc(own.wan_base || '')}" placeholder="例如 https://jellyfin.example.com 或留空用全局默认"
        autocomplete="off" data-lpignore="true" data-form-type="other">
      <label>选择方式</label><select class="input" id="lkMode">
        <option value="" ${!own.link_mode ? 'selected' : ''}>跟随全局设置（${esc(g.link_mode === 'lan' ? '始终内网' : g.link_mode === 'wan' ? '始终外网' : '自动检测')}）</option>
        <option value="auto" ${own.link_mode === 'auto' ? 'selected' : ''}>自动检测（推荐）</option>
        <option value="lan" ${own.link_mode === 'lan' ? 'selected' : ''}>始终使用内网地址</option>
        <option value="wan" ${own.link_mode === 'wan' ? 'selected' : ''}>始终使用外网地址</option></select>
      <p class="store-hint">容器卡片上的端口按钮会按这里的地址生成链接；当前容器的端口：${esc(((S.containers.find(x => x.name === name) || {}).ports || []).join('、') || '无映射端口')}。</p>
    </div>`, async () => {
    const links = Object.assign({}, g.container_links || {});
    links[name] = {
      lan_base: ($('#lkLan').value || '').trim(),
      wan_base: ($('#lkWan').value || '').trim(),
      link_mode: $('#lkMode').value,
    };
    try {
      await api('/api/settings', { method: 'POST', body: { settings: { container_links: links } } });
      toast(name + ' 的访问链接已单独保存', 'ok');
      await window.duLoad();
    } catch (e) { if (!e.auth) toast(e.message || '保存失败', 'err'); }
  });
}
/* 自动模式：从公网访问面板时，短探测一次内网地址是否可达（设备其实在家的情况） */
function probeLan() {
  const s = allLinkSettings();
  const lan = String(s.lan_base || '').trim();
  if (!s.link_probe || linkOverride() || !lan || isPrivateHost(location.hostname)) return;
  if (Date.now() - linkProbeAt < 60000) return;
  linkProbeAt = Date.now();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 1200);
  fetch(lan, { mode: 'no-cors', signal: ctrl.signal, cache: 'no-store' })
    .then(() => { lanReachable = true; })
    .catch(() => { lanReachable = false; })
    .finally(() => {
      clearTimeout(timer);
      if (S && (view === 'containers' || view === 'container-links')) renderView();
    });
}
function renderLinkBar() {
  const bar = $('#linkModeBar');
  if (!bar) return;
  const ov = linkOverride();
  const modes = [['auto', '自动'], ['lan', '内网'], ['wan', '外网']];
  // v3.2：地址不在工具条里设置。每个容器可能对应不同的外网域名，
  // 点容器卡片上的「链接设置」单独填写；这里只切换本浏览器的默认模式。
  bar.innerHTML = `<span class="lmb-label">${icon('i-link')} 容器访问链接</span>
    <span class="lmb-hint">每个容器的内网 / 外网地址在卡片上的「链接设置」里单独填写；这里只切换默认选择方式${ov ? '（本浏览器手动覆盖）' : ''}</span>
    <span class="lmb-btns">${modes.map(([k, t]) =>
      `<button class="chip ${((ov || allLinkSettings().link_mode || 'auto') === k) ? 'active' : ''}" data-link-mode="${k}">${t}</button>`).join('')}</span>`;
  $$('[data-link-mode]', bar).forEach(b => b.onclick = () => {
    const k = b.dataset.linkMode;
    if (k === 'auto') localStorage.removeItem('du_link_override');
    else localStorage.setItem('du_link_override', k);
    lanReachable = null;
    renderView();
  });
}
function portsHtml(ports, name) {
  const list = Array.isArray(ports) ? ports : [];
  if (!list.length) return '<div class="c-ports"><span class="port-none">未映射端口</span></div>';
  return `<div class="c-ports">${list.map(p => {
    const m = String(p).match(/^(\d+)(?:,\s*\d+)*\s*→\s*(\d+)\/(tcp|udp)$/);
    if (!m) return `<span class="port-none" title="${esc(p)}">${esc(p)}</span>`;
    const url = portUrl(m[1], name);
    return `<a class="port-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer"
      title="打开 ${esc(url)} · 宿主机 ${m[1]} → 容器 ${m[2]}/${m[3]}">${icon('i-link')} ${esc(m[1])}<small>→${esc(m[2])}</small></a>`;
  }).join('')}</div>`;
}

/* ---------- 容器 ---------- */
/* v3.5：原「备份恢复」模块里的容器明细表移到这里，窄屏自动堆叠、不横向滚动 */
function deployTable(rows) {
  const pt = { manual: '手动', auto: '自动更新', hold: '暂不更新' };
  return `<div class="panel glass deploy-detail"><div class="panel-head"><h3>${icon('i-archive')} 所有容器 · 部署与快照明细</h3>
    <span class="sub">共 ${rows.length} 个 · 部署日期 / 运行日期 / 上次快照备份日期</span></div>
    <div class="tbl-wrap"><table class="tbl deploy-tbl"><thead><tr>
      <th>容器</th><th>状态</th><th>镜像</th><th>部署日期</th><th>运行日期</th>
      <th>上次快照备份</th><th>快照数</th><th>更新策略</th><th>端口</th></tr></thead>
      <tbody>${rows.length ? rows.map(x => {
        const bks = x.backups || [];
        return `<tr>
          <td class="strong" data-label="容器">${esc(x.alias || x.name)}${x.alias ? `<div class="tiny">${esc(x.name)}</div>` : ''}</td>
          <td data-label="状态"><span class="badge ${x.running ? 'ok' : 'warn'}">${esc(x.status)}</span></td>
          <td class="small mono" data-label="镜像">${esc(x.image_ref || '')}</td>
          <td class="small" data-label="部署日期">${esc(x.created || '—')}</td>
          <td class="small" data-label="运行日期">${esc(x.started_at || '—')}</td>
          <td class="small" data-label="上次快照备份">${esc(bks.length ? (bks[0].time || '') : '尚无备份')}</td>
          <td data-label="快照数">${bks.length}</td>
          <td class="small" data-label="更新策略">${esc(pt[x.policy] || x.policy || '')}</td>
          <td class="small" data-label="端口">${esc((x.ports || []).join('、') || '—')}</td></tr>`;
      }).join('') : '<tr><td colspan="9"><div class="empty-state">暂无容器</div></td></tr>'}</tbody></table></div>
    <p class="store-hint">部署日期取自 Docker 容器 Created，运行日期取自 State.StartedAt，上次快照来自「更新前备份」记录；手动「立即备份」也会记入。</p>
  </div>`;
}

/* v3.9：三个附加区域各自独立页面，不再堆在容器列表下面 */
function renderContainerLinks() {
  renderLinkBar();
  probeLan();
  const box = $('#linkSettingsList'), cnt = $('#linkListCount');
  if (!box || !S) return;
  const items = S.containers.slice();
  if (cnt) cnt.textContent = '共 ' + items.length + ' 个容器';
  box.innerHTML = `<div class="link-list">${items.length ? items.map(x => {
    const own = (allLinkSettings().container_links || {})[x.name] || {};
    const ports = (x.ports || []).join('、') || '未映射端口';
    return `<div class="link-row">
      <div class="link-row-main"><b>${esc(x.alias || x.name)}</b>
        <span class="tiny">${esc(x.name)} · ${esc(ports)}</span></div>
      <div class="link-row-addr"><span>内网 ${esc(own.lan_base || '跟随全局')}</span>
        <span>外网 ${esc(own.wan_base || '跟随全局')}</span></div>
      <button class="btn ghost slim" data-lk="${esc(x.name)}">${icon('i-sliders')} 设置</button>
    </div>`;
  }).join('') : '<div class="empty-state">暂无容器</div>'}</div>`;
  $$('[data-lk]', box).forEach(b => b.onclick = () => openLinkSettings(b.dataset.lk));
  const br = $('#btnLinksRefresh'); if (br) br.onclick = () => duRefreshUI(br);
}
function renderSnapshots() {
  const box = $('#snapshotsBody');
  if (!box || !S) return;
  box.innerHTML = deployTable(S.containers);
  const br = $('#btnSnapshotsRefresh'); if (br) br.onclick = () => duRefreshUI(br);
}

function renderContainers() {
  probeLan();
  const c = S.console, metrics = c.metrics || {};
  let items = S.containers.slice();
  const q = cQuery.trim().toLowerCase();
  if (q) items = items.filter(x => [x.name, x.alias, x.memo, x.image_ref, x.project].join(' ').toLowerCase().includes(q));
  if (cFilter === 'running') items = items.filter(x => x.running);
  if (cFilter === 'stopped') items = items.filter(x => !x.running);
  if (cFilter === 'abnormal') items.sort((a, b) => Number(a.running) - Number(b.running)
    || (nz(metrics[b.name]?.cpu) - nz(metrics[a.name]?.cpu)));
  $('#containersBody').innerHTML = `<div class="c-grid">${items.length ? items.map(x => {
    const m = metrics[x.name] || {};
    const self = x.excluded && x.name === 'docker-update';
    return `<div class="c-card glass">
      <div class="c-head"><i class="dot ${x.running ? 'on' : ''}"></i><h4 title="原名 ${esc(x.name)}">${esc(x.alias || x.name)}</h4>
        <span class="lvl ${x.running ? 'ok' : 'gray'}">${esc(x.status)}</span></div>
      ${x.alias ? `<div class="c-alias">原名 ${esc(x.name)}</div>` : ''}
      ${x.memo ? `<div class="c-note">${esc(x.memo)}</div>` : ''}
      <div class="c-metrics">
        <div>CPU<b>${m.cpu ?? 0}%</b></div><div>内存<b>${m.mem ?? 0} MB</b></div>
        <div>网络<b>${speed(nz(m.net_rx) + nz(m.net_tx))}</b></div><div>运行时长<b>${dur(m.uptime)}</b></div></div>
      <div class="c-image" title="${esc(x.image_ref)}">${esc(x.image_ref || '本地镜像')}${x.mem_limit_mb ? ` · 限 ${x.mem_limit_mb}MB` : ''}</div>
      ${portsHtml(x.ports, x.name)}
      <div class="c-acts">
        <button class="mini-act primary" data-ca="detail" data-name="${esc(x.name)}">${icon('i-info')} 详情</button>
        <button class="mini-act ${x.running ? 'act-stop' : 'act-start'}" data-ca="${x.running ? 'stop' : 'start'}" data-name="${esc(x.name)}" ${self ? 'disabled' : ''}>${icon(x.running ? 'i-stop' : 'i-play')} ${x.running ? '停止' : '启动'}</button>
        <button class="mini-act act-restart" data-ca="restart" data-name="${esc(x.name)}" ${self ? 'disabled' : ''}>${icon('i-refresh')} 重启</button>
        <button class="mini-act" data-ca="pause" data-name="${esc(x.name)}" ${self ? 'disabled' : ''}>${icon('i-pause')} ${String(x.status).includes('paused') ? '恢复' : '暂停'}</button>
        <button class="mini-act" data-ca="link" data-name="${esc(x.name)}">${icon('i-link')} 链接设置</button>
        <button class="mini-act" data-ca="mounts" data-name="${esc(x.name)}">${icon('i-folder')} 目录映射</button>
        <button class="mini-act" data-ca="note" data-name="${esc(x.name)}">${icon('i-pencil')} 备注</button>
        <button class="mini-act" data-ca="limits" data-name="${esc(x.name)}">${icon('i-cpu')} 资源限制</button>
        <button class="mini-act danger" data-ca="remove" data-name="${esc(x.name)}" ${self ? 'disabled' : ''}>${icon('i-trash')} 删除</button>
      </div></div>`;
  }).join('') : '<div class="empty-state glass">没有匹配的容器</div>'}</div>`;
  $('#containersBody').querySelectorAll('[data-ca]').forEach(b => {
    b.onclick = async () => {
      const a = b.dataset.ca, name = b.dataset.name;
      if (a === 'detail') return openDetail(name);
      if (a === 'link') return openLinkSettings(name);
      if (a === 'mounts' && window.duMounts) return window.duMounts.openFor(name);
      if (a === 'note') return window.duOpenNote(name);
      if (a === 'limits') return openLimits(name);
      if (a === 'remove' && !confirm2('remove:' + name, '再次点击确认删除容器（不可撤销）')) return;
      if ((a === 'stop' || a === 'restart') && !confirm2(a + ':' + name, '再次点击确认该操作')) return;
      await act('container', { name, action: a });
    };
  });
  $('#btnContainersRefresh').onclick = () => duRefreshUI($('#btnContainersRefresh'));
  $('#containerFilters').onclick = (e) => {
    const b = e.target.closest('[data-cf]'); if (!b) return;
    cFilter = b.dataset.cf;
    $$('#containerFilters .chip').forEach(x => x.classList.toggle('active', x === b));
    renderContainers();
  };
  if (!$('#containerSearch').dataset.bound) {
    $('#containerSearch').dataset.bound = '1';
    $('#containerSearch').addEventListener('input', (e) => {
      if (e.target.dataset.duTouched !== '1') { e.target.value = ''; cQuery = ''; renderContainers(); return; }
      cQuery = e.target.value; renderContainers();
    });
  }
}

/* ---------- 镜像 ---------- */
/* ---------- v6.0-r18 · 镜像更新（更新大模块下的独立页面） ----------
   与「容器更新」彻底分开：只比对 digest / 只拉取刷新本地 tag，绝不重建容器。
   数据走 /api/pro image-updates；检测与拉取走后台任务，SSE 真实进度条。 */
let iuData = null, iuSelected = new Set(), iuFilter = 'all', iuQuery = '';
let iuSig = '', iuBusy = false, iuLoading = false;
const IU_LABELS = { unchecked: '未检测', outdated: '可更新', latest: '已是最新',
  unknown_digest: '可拉取刷新', skipped: '中性跳过', pull_failed: '更新失败', pulled: '已更新' };
/* v6.0-r20：只有真正检测到可更新版本（远端 digest 变化 / 本地无 digest 可拉取刷新）
   才出现「更新」按钮；已是最新 / 中性跳过 / 未检测 / 更新失败一律只保留「检测」。 */
const IU_UPDATABLE = new Set(['outdated', 'unknown_digest']);
function iuApi(kind, body, opts) {
  return api('/api/pro', Object.assign({ method: 'POST', body: { kind, body: body || {} } }, opts || {}));
}
async function loadImageUpdates(force) {
  if (iuLoading) return;
  iuLoading = true;
  try {
    const d = await iuApi('image-updates', {}, { progress: false });
    if (d && d.ok !== false) { iuData = d; renderImageUpdates(force === true); }
    else toast((d && d.message) || '读取镜像更新失败', 'err');
  } catch (e) { if (!e.auth) toast(e.message || '读取镜像更新失败', 'err'); }
  finally { iuLoading = false; }
}
function iuVisible() {
  const rows = (iuData && iuData.rows) || [];
  const q = iuQuery.trim().toLowerCase();
  return rows.filter(x => {
    if (iuFilter === 'used') { if (!(nz(x.containers) > 0)) return false; }
    else if (iuFilter !== 'all' && x.status !== iuFilter) return false;
    if (q && ![x.ref, x.id, x.status_label, x.reason, x.remote_via]
      .join(' ').toLowerCase().includes(q)) return false;
    return true;
  });
}
function iuBadge(x) {
  const s = x.status || 'unchecked';
  const cls = (s === 'outdated' || s === 'unknown_digest') ? 'warn'
    : (s === 'latest' || s === 'pulled') ? 'ok'
    : (s === 'pull_failed') ? 'bad' : 'gray';
  return `<span class="lvl ${cls}" title="${esc(x.reason || '')}">${esc(x.status_label || IU_LABELS[s] || s)}</span>`;
}
function iuShort(d) { return d ? esc(String(d).replace('sha256:', '').slice(0, 16)) + '…' : '—'; }
function renderImageUpdates(force) {
  if (view !== 'image-updates') return;
  const box = $('#iuBody'); if (!box) return;
  const ae = document.activeElement;
  /* 输入中不重绘，避免历史「刚填完就被自动刷新恢复」 */
  if (!force && ae && ae.id === 'iuSearch') return;
  if (!iuData) {
    box.innerHTML = '<div class="panel glass"><div class="empty-state">正在读取本地镜像…</div></div>';
    loadImageUpdates(false); return;
  }
  const d = iuData, items = iuVisible(), sum = d.summary || {};
  /* 没有任何可更新镜像时，顶部两个更新按钮都不显示（v6.0-r20 要求，r7.0.4 沿用） */
  const canAny = (d.rows || []).some(x => IU_UPDATABLE.has(x.status));
  const pullBtn = $('#btnIuPullSel'), rebuildBtn = $('#btnIuRebuildSel');
  if (pullBtn) {
    pullBtn.classList.toggle('hidden', !canAny);
    pullBtn.title = canAny ? '只拉取检测到新版本的镜像，不重建容器'
      : '当前没有检测到可更新的镜像，请先「检查选中」或「检查全部镜像」';
  }
  if (rebuildBtn) {
    rebuildBtn.classList.toggle('hidden', !canAny);
    rebuildBtn.title = canAny ? '拉取后按原配置重建使用该镜像的容器（会短暂中断，自动备份 / 失败回退）'
      : '当前没有检测到可更新的镜像，请先「检查选中」或「检查全部镜像」';
  }
  const sig = JSON.stringify([d.rows, d.summary, d.at, d.docker_ok,
    iuFilter, iuQuery, Array.from(iuSelected).sort()]);
  if (!force && sig === iuSig) return;
  iuSig = sig;
  const lim = $('#iuLimits span');
  if (lim && d.limits) lim.textContent = d.limits;
  const sEl = $('#iuSearch'); if (sEl && ae !== sEl) sEl.value = iuQuery;
  $$('#iuFilters [data-iu-filter]').forEach(b => b.classList.toggle('active', b.dataset.iuFilter === iuFilter));
  const cnt = $('#iuCount'); if (cnt) cnt.textContent = `${items.length} 个 · 已选 ${iuSelected.size}`;
  const kpi = $('#iuKpi');
  if (kpi) kpi.innerHTML = `<div class="grid-4" style="margin-bottom:16px">
    <div class="kpi glass info"><div class="k-label">带标签本地镜像</div><div class="k-num">${nz(sum.total)}</div><div class="k-sub">被容器使用 ${nz(sum.used)} 个</div></div>
    <div class="kpi glass warn"><div class="k-label">可更新 / 可刷新</div><div class="k-num">${nz(sum.outdated) + nz(sum.unknown_digest)}</div><div class="k-sub">digest 有变化 ${nz(sum.outdated)} · 无本地 digest ${nz(sum.unknown_digest)}</div></div>
    <div class="kpi glass ok"><div class="k-label">已是最新</div><div class="k-num">${nz(sum.latest)}</div><div class="k-sub">未检测 ${nz(sum.unchecked)} 个</div></div>
    <div class="kpi glass"><div class="k-label">中性跳过 / 失败</div><div class="k-num">${nz(sum.skipped) + (d.rows || []).filter(x => x.status === 'pull_failed').length}</div><div class="k-sub">${d.at ? '上次检测 ' + esc(d.at) : '尚未检测过'}</div></div>
  </div>${d.docker_ok ? '' : `<div class="notice small-notice">${esc(d.error || 'Docker 暂时不可用')}</div>`}`;
  box.innerHTML = `<div class="panel glass"><div class="panel-head"><h3>${icon('i-image')} 镜像更新列表</h3>
    <span class="sub">${d.sources ? '候选源 ' + d.sources.length + ' 个 · ' : ''}两种模式：只更新 / 更新并重建容器</span></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr>
      <th style="width:34px"><input type="checkbox" id="iuAll" title="全选 / 取消全选"></th>
      <th>仓库 : 标签</th><th>状态</th><th>镜像 ID</th><th>本地 digest</th><th>远端 digest</th>
      <th>大小</th><th>创建时间</th><th>容器</th><th>操作</th>
    </tr></thead><tbody>${items.length ? items.map(x => {
      const brand = (x.ref || '').split(':')[0];
      const logo = brand ? `<img class="img-brand" src="/api/registry/icon?name=${encodeURIComponent(brand)}" alt="" loading="lazy">` : '';
      return `<tr>
      <td data-label="选择"><input type="checkbox" data-iu-ref="${esc(x.ref)}" ${iuSelected.has(x.ref) ? 'checked' : ''}></td>
      <td data-label="仓库 : 标签" class="strong">${logo}<span class="mono small">${esc(x.ref)}</span>
        <div class="tiny">${esc(x.reason || '尚未检测')}${x.remote_via ? ' · ' + esc(x.remote_via) : ''}</div></td>
      <td data-label="状态">${iuBadge(x)}</td>
      <td data-label="镜像 ID" class="mono small">${esc(x.id || '—')}</td>
      <td data-label="本地 digest" class="mono small">${iuShort(x.local_digest)}</td>
      <td data-label="远端 digest" class="mono small">${iuShort(x.remote_digest)}</td>
      <td data-label="大小">${nz(x.size_mb).toFixed(1)} MB</td>
      <td data-label="创建时间" class="small">${esc(x.created || '—')}</td>
      <td data-label="容器">${nz(x.containers) > 0 ? `<span class="lvl ok">${x.containers} 个</span>` : '<span class="lvl gray">0</span>'}</td>
      <td data-label="操作"><button class="mini-act" data-iu-act="check" data-ref="${esc(x.ref)}">${icon('i-search')} 检测</button>
        ${IU_UPDATABLE.has(x.status) ? `<button class="mini-act" data-iu-act="pull" data-ref="${esc(x.ref)}">${icon('i-download')} 更新</button>
        <button class="mini-act" data-iu-act="rebuild" data-ref="${esc(x.ref)}" title="拉取后重建使用该镜像的容器">${icon('i-refresh')} 更新并重建</button>` : ''}</td>
    </tr>`; }).join('') : '<tr><td colspan="10"><div class="empty-state">没有匹配的镜像</div></td></tr>'}</tbody></table></div></div>`;
  /* 三态回填：这是 v3.2 / v3.4 反复修过的「点一下全选、再点没反应」根因 */
  const all = $('#iuAll');
  if (all) {
    const keys = items.map(x => x.ref);
    const on = keys.filter(k => iuSelected.has(k)).length;
    all.checked = keys.length > 0 && on === keys.length;
    all.indeterminate = on > 0 && on < keys.length;
    all.title = all.checked ? '取消全选' : '全选';
  }
}
async function iuRun(kind, refs, label, stages, extra) {
  if (iuBusy) { toast('已有镜像任务在执行，请等它结束', 'info'); return; }
  iuBusy = true;
  try {
    const r = await iuApi(kind, Object.assign({ refs }, extra || {}), {
      progressLabel: label, progressStages: stages, progressImmediate: true });
    let out = r;
    if (r && r.async && r.job_id && window.duWatchJob) out = await window.duWatchJob(r.job_id, r._ph);
    if (out && out.ok === false) toast(out.message || '任务失败', 'err');
    iuSig = ''; await loadImageUpdates(true);
  } catch (e) { if (!e.auth) toast(e.message || '任务失败', 'err'); }
  finally { iuBusy = false; }
}
function iuRefs() {
  const refs = Array.from(iuSelected);
  if (!refs.length) { toast('请先勾选镜像', 'info'); return null; }
  return refs;
}
/* v7.0.4：重建前先问后端「会牵连几个容器」，二次确认里给出真实数量；
   计划接口只算不写，读取失败也不阻断，文案退回保守提示。 */
async function iuRebuildConfirm(refs) {
  let p = null;
  try { p = await iuApi('image-rebuild-plan', { refs }, { progress: false }); } catch (e) { p = null; }
  const n = (p && p.ok !== false && typeof p.containers === 'number') ? p.containers : null;
  const hint = n === null
    ? `再次点击确认：更新 ${refs.length} 个镜像并重建关联容器（会短暂中断服务；自动备份、失败自动回退）`
    : `再次点击确认：更新 ${refs.length} 个镜像，重建 ${n} 个容器（会短暂中断服务；自动备份、健康检查不通过自动回退）`;
  return confirm2('iu-rebuild', hint);
}
function iuForceRebuild() {
  const el = $('#iuForceRebuild');
  return !!(el && el.checked);
}
/* 所有事件委托到 document：自动轮询重绘表格不会冲掉监听，也不会清掉筛选/勾选 */
document.addEventListener('input', (e) => {
  if (e.target && e.target.id === 'iuSearch') { iuQuery = String(e.target.value || ''); renderImageUpdates(true); }
});
document.addEventListener('change', (e) => {
  const t = e.target;
  if (!t) return;
  if (t.id === 'iuAll') {
    const keys = iuVisible().map(x => x.ref);
    if (t.checked) keys.forEach(k => iuSelected.add(k)); else keys.forEach(k => iuSelected.delete(k));
    $$('#iuBody [data-iu-ref]').forEach(cb => { cb.checked = t.checked; });
    renderImageUpdates(true);
  } else if (t.matches && t.matches('[data-iu-ref]')) {
    if (t.checked) iuSelected.add(t.dataset.iuRef); else iuSelected.delete(t.dataset.iuRef);
    renderImageUpdates(true);
  }
});
document.addEventListener('click', async (e) => {
  const el = e.target.closest('button'); if (!el) return;
  if (el.matches('#iuFilters [data-iu-filter]')) { iuFilter = el.dataset.iuFilter; renderImageUpdates(true); return; }
  if (el.id === 'btnIuRefresh') { await duRefreshUI(el); iuSig = ''; await loadImageUpdates(true); return; }
  if (el.id === 'btnIuCheckAll') {
    return iuRun('image-update-check', [], '正在检查全部镜像更新',
      ['读取本地镜像与候选源', '比对远端 digest', '写回检测结果']);
  }
  if (el.id === 'btnIuCheckSel') {
    const refs = iuRefs(); if (!refs) return;
    return iuRun('image-update-check', refs, '正在检查选中的镜像',
      ['读取本地镜像与候选源', '比对远端 digest', '写回检测结果']);
  }
  if (el.id === 'btnIuPullSel') {
    let refs = iuRefs(); if (!refs) return;
    const allowed = new Set(((iuData && iuData.rows) || []).filter(x => IU_UPDATABLE.has(x.status)).map(x => x.ref));
    refs = refs.filter(r => allowed.has(r));
    if (!refs.length) { toast('所选镜像没有检测到可更新版本，请先点「检查选中」或「检查全部镜像」', 'info'); return; }
    if (!confirm2('iu-pull', `再次点击确认更新 ${refs.length} 个镜像（只拉取，不会重建容器）`)) return;
    return iuRun('image-update-pull', refs, '正在更新选中的镜像',
      ['准备拉取', '拉取并校验镜像', '汇总结果']);
  }
  if (el.id === 'btnIuRebuildSel') {
    let refs = iuRefs(); if (!refs) return;
    const allowed = new Set(((iuData && iuData.rows) || []).filter(x => IU_UPDATABLE.has(x.status)).map(x => x.ref));
    refs = refs.filter(r => allowed.has(r));
    if (!refs.length) { toast('所选镜像没有检测到可更新版本，请先点「检查选中」或「检查全部镜像」', 'info'); return; }
    if (!(await iuRebuildConfirm(refs))) return;
    return iuRun('image-update-rebuild', refs, '正在更新镜像并重建容器',
      ['校验镜像与关联容器', '拉取并校验新镜像', '生成版本备份', '按原配置重建容器', '核对启动状态'],
      { force: iuForceRebuild() });
  }
  if (el.id === 'btnIuClear') {
    try {
      const d = await iuApi('image-updates-clear', {}, { progress: false });
      toast(d.message || '已清空', 'ok'); iuSig = ''; await loadImageUpdates(true);
    } catch (err) { if (!err.auth) toast(err.message || '清空失败', 'err'); }
    return;
  }
  if (el.matches('[data-iu-act]')) {
    const ref = el.dataset.ref;
    if (el.dataset.iuAct === 'check') return iuRun('image-update-check', [ref], `正在检查 ${ref}`,
      ['读取本地镜像与候选源', '比对远端 digest', '写回检测结果']);
    const row = ((iuData && iuData.rows) || []).find(x => x.ref === ref);
    if (!row || !IU_UPDATABLE.has(row.status)) {
      toast('该镜像尚未检测到可更新版本，请先点「检测」', 'info'); return;
    }
    if (el.dataset.iuAct === 'rebuild') {
      if (!(await iuRebuildConfirm([ref]))) return;
      return iuRun('image-update-rebuild', [ref], `正在更新并重建 ${ref}`,
        ['校验镜像与关联容器', '拉取并校验新镜像', '生成版本备份', '按原配置重建容器', '核对启动状态'],
        { force: iuForceRebuild() });
    }
    if (!confirm2('iu-pull-one', `再次点击确认更新 ${ref}（只拉取，不会重建容器）`)) return;
    return iuRun('image-update-pull', [ref], `正在更新 ${ref}`,
      ['准备拉取', '拉取并校验镜像', '汇总结果']);
  }
});

function renderImages() {
  const c = S.console, all = c.images || [];
  let items = all.filter(x => (!danglingOnly || x.dangling) && (!unusedOnly || !nz(x.containers)));
  const q = iQuery.trim().toLowerCase();
  if (q) items = items.filter(x => [x.tags.join(' '), x.id, x.full_id].join(' ').toLowerCase().includes(q));
  const size = all.reduce((s, x) => s + nz(x.size), 0);
  const dang = all.filter(x => x.dangling).length;
  const used = all.filter(x => nz(x.containers) > 0).length;
  $('#imagesBody').innerHTML = `
    <div class="grid-4" style="margin-bottom:16px">
      <div class="kpi glass info"><div class="k-label">本地镜像</div><div class="k-num">${all.length}</div><div class="k-sub">使用中 ${used} · 未使用 ${all.length - used} · 悬空 ${dang}</div></div>
      <div class="kpi glass"><div class="k-label">镜像占用</div><div class="k-num">${(size/1024).toFixed(2)}<small>GB</small></div><div class="k-sub">Docker system df 可回收 ${c.docker_df?.reclaimable || 0} MB</div></div>
      <div class="kpi glass"><div class="k-label">已选择</div><div class="k-num">${selected.size}</div><div class="k-sub">可批量导出或删除</div></div>
      <div class="kpi glass"><div class="k-label">运行中容器</div><div class="k-num">${S.stats.running}</div><div class="k-sub">共 ${S.stats.total} 个容器</div></div>
    </div>
    <div class="panel glass"><div class="panel-head"><h3>${icon('i-image')} 镜像列表</h3>
      <span class="sub">${items.length} 个 · 勾选后可批量导出 / 删除</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th style="width:34px"><input type="checkbox" id="imgAll" title="全选 / 取消全选"></th>
        <th>仓库 : 标签</th><th>镜像 ID</th><th>大小</th><th>创建时间</th><th>使用状态</th><th>操作</th>
      </tr></thead><tbody>${items.length ? items.map(x => {
        const inUse = nz(x.containers) > 0;
        // v3.2：每行显示按镜像名生成的品牌矢量图标，悬空镜像显示灰色占位
        const tag0 = (x.tags && x.tags[0]) || '';
        const brand = tag0 && !tag0.startsWith('<none>') ? tag0.split(':')[0] : '';
        const logo = brand
          ? `<img class="img-brand" src="/api/registry/icon?name=${encodeURIComponent(brand)}" alt="" loading="lazy">`
          : `<span class="img-brand img-brand-none">${icon('i-image')}</span>`;
        return `<tr>
        <td><input type="checkbox" data-img="${esc(x.id)}" ${selected.has(x.id) ? 'checked' : ''}></td>
        <td class="strong">${logo}<i class="use-dot ${inUse ? 'on' : ''}" title="${inUse ? '正在被容器使用' : '未被任何容器使用'}"></i>
          ${x.tags.length ? x.tags.map(t => `<span class="m">${esc(t)}</span>`).join(' ') : '<span class="lvl gray">悬空 &lt;none&gt;</span>'}
          <span class="use-tag ${inUse ? 'ok' : 'gray'}">${inUse ? '使用中 · ' + x.containers + ' 个容器' : '未使用'}</span></td>
        <td class="mono small">${esc(x.id)}</td><td>${x.size} MB</td><td class="small">${esc(x.created || '—')}</td>
        <td>${inUse ? `<span class="lvl ok">${x.containers} 个容器</span>` : '<span class="lvl gray">0</span>'}</td>
        <td><button class="mini-act" data-ia="export" data-id="${esc(x.id)}">${icon('i-download')} 导出</button>
          <button class="mini-act" data-ia="tag" data-id="${esc(x.id)}">${icon('i-tag')} 打标签</button>
          <button class="mini-act danger" data-ia="remove" data-id="${esc(x.id)}">${icon('i-trash')} 删除</button></td>
      </tr>`; }).join('') : '<tr><td colspan="7"><div class="empty-state">没有匹配的镜像</div></td></tr>'}</tbody></table></div></div>`;
  const allBox = $('#imgAll');
  const syncAll = () => {
    if (!allBox) return;
    const ids = items.map(x => x.id);
    const on = ids.filter(id => selected.has(id)).length;
    // 关键修复：重绘后必须回填三态，否则第二次点击仍会被当成「全选」，看起来没反应
    allBox.checked = ids.length > 0 && on === ids.length;
    allBox.indeterminate = on > 0 && on < ids.length;
    allBox.title = allBox.checked ? '取消全选' : '全选';
    const kpi = $('#imagesBody .kpi:nth-child(3) .k-num');
    if (kpi) kpi.textContent = selected.size;
  };
  if (allBox) allBox.onchange = (e) => {
    if (e.target.checked) items.forEach(x => selected.add(x.id)); else items.forEach(x => selected.delete(x.id));
    $$('#imagesBody [data-img]').forEach(cb => { cb.checked = e.target.checked; });
    syncAll();
  };
  $$('#imagesBody [data-img]').forEach(cb => cb.onchange = (e) => {
    const id = cb.dataset.img;
    if (e.target.checked) selected.add(id); else selected.delete(id);
    syncAll();
  });
  syncAll();
  $$('#imagesBody [data-ia]').forEach(b => b.onclick = async () => {
    const id = b.dataset.id, a = b.dataset.ia;
    if (a === 'export') return download('/api/image-export?names=' + encodeURIComponent(id), 'docker-image-' + id + '.tar');
    if (a === 'tag') return openTag(id);
    if (!confirm2('imgdel:' + id, '再次点击确认删除该镜像')) return;
    await act('image', { action: 'remove', ids: [id] });
  });
  $('#btnImagesRefresh').onclick = () => act('refresh', {});
  $('#btnPullImage').onclick = openPull;
  $('#btnPruneImages').onclick = async () => {
    if (!confirm2('prune', '再次点击确认清理所有悬空镜像')) return;
    await act('image', { action: 'prune' });
  };
  $('#btnExportImages').onclick = () => {
    if (!selected.size) { toast('请先勾选要导出的镜像', 'err'); return; }
    download('/api/image-export?names=' + encodeURIComponent([...selected].join(',')), 'docker-images.tar');
  };
  /* v6.0-r14：勾选后批量删除；二次确认，删除后清空选中集合 */
  const delBtn = $('#btnDeleteImages');
  if (delBtn) delBtn.onclick = async () => {
    if (!selected.size) { toast('请先勾选要删除的镜像', 'err'); return; }
    if (!confirm2('imgdel-batch', '再次点击确认删除选中的 ' + selected.size + ' 个镜像（强制删除）')) return;
    const ids = [...selected];
    await act('image', { action: 'remove', ids, force: true });
    selected.clear();
    renderImages();
  };
  const unusedBtn = $('#btnRemoveUnusedImages');
  if (unusedBtn) unusedBtn.onclick = async () => {
    const n = all.filter(x => !nz(x.containers)).length;
    if (!confirm2('imgdel-unused', '再次点击确认删除全部 ' + n + ' 个未使用镜像（使用中镜像会跳过）')) return;
    await act('image', { action: 'remove-unused' });
    selected.clear();
  };
  $('#imageDanglingOnly').onchange = (e) => { danglingOnly = e.target.checked; renderImages(); };
  const unusedBox = $('#imageUnusedOnly');
  if (unusedBox) unusedBox.onchange = (e) => { unusedOnly = e.target.checked; renderImages(); };
  if (!$('#imageSearch').dataset.bound) {
    $('#imageSearch').dataset.bound = '1';
    $('#imageSearch').addEventListener('input', (e) => {
      if (e.target.dataset.duTouched !== '1') { e.target.value = ''; iQuery = ''; renderImages(); return; }
      iQuery = e.target.value; renderImages();
    });
  }
}

/* ---------- 网络 / 卷 / Compose ---------- */
function renderNetworks() {
  const body = $('#networksBody');
  if (!body) return;              // v3.9-r6：网络/卷模块已移除，历史调用不应空引用
  const c = (S && S.console) || {}, nets = c.networks || [], vols = c.volumes || [];
  body.innerHTML = `
    <div class="grid-2">
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-network')} 网络 · ${nets.length} 个</h3>
        <button class="mini-act" id="btnNetModal">${icon('i-plus')} 新建网络</button></div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr><th>名称</th><th>驱动</th><th>子网</th><th>容器</th><th>操作</th></tr></thead>
        <tbody>${nets.map(n => `<tr><td class="strong">${esc(n.name)}<div class="small mono">${esc(n.id)}</div></td>
          <td>${esc(n.driver)}</td><td class="small">${n.subnets.map(esc).join('<br>') || '—'}</td><td>${n.count}</td>
          <td><button class="mini-act" data-na="connect" data-id="${esc(n.id)}">${icon('i-plus')} 连接</button>
            <button class="mini-act danger" data-na="remove" data-id="${esc(n.id)}">${icon('i-trash')} 删除</button></td></tr>`).join('')}</tbody></table></div>
        ${nets.length ? `<div class="chip-list" style="margin-top:12px">${nets.slice(0,6).map(n => `<span class="m"><b>${esc(n.name)}</b> · ${(n.containers||[]).map(x=>esc(x.name)).join(', ') || '无容器'}</span>`).join('')}</div>` : ''}
      </div>
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-volume')} 数据卷 · ${vols.length} 个</h3>
        <button class="mini-act" id="btnVolCreate">${icon('i-plus')} 新建卷</button>
        <button class="mini-act danger" id="btnVolPrune">清理未使用</button></div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr><th>名称</th><th>驱动</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody>${vols.slice(0, 60).map(v => `<tr><td class="strong">${esc(v.name)}<div class="small">${esc(v.mountpoint)}</div></td>
          <td>${esc(v.driver)}</td><td class="small">${esc(v.created || '—')}</td>
          <td><button class="mini-act danger" data-va="remove" data-name="${esc(v.name)}">${icon('i-trash')} 删除</button></td></tr>`).join('') || '<tr><td colspan="4"><div class="empty-state">暂无数据卷</div></td></tr>'}</tbody></table></div>
      </div></div>`;
  const netBtn = $('#btnNetModal'); if (netBtn) netBtn.onclick = openNetwork;
  const volCreate = $('#btnVolCreate'); if (volCreate) volCreate.onclick = () => openModal('新建数据卷', '创建一个 local 驱动的命名卷', field('卷名', 'volName', 'my-data'), async () => {
    const name = $('#volName').value.trim();
    if (!name) { toast('请填写卷名', 'err'); return false; }
    await act('volume', { action: 'create', name });
  });
  const volPrune = $('#btnVolPrune'); if (volPrune) volPrune.onclick = async () => {
    if (!confirm2('volprune', '再次点击确认清理未使用数据卷')) return;
    await act('volume', { action: 'prune' });
  };
  $$('#networksBody [data-na]').forEach(b => b.onclick = async () => {
    const id = b.dataset.id, a = b.dataset.na;
    if (a === 'remove') {
      if (!confirm2('netdel:' + id, '再次点击确认删除网络')) return;
      return act('network', { action: 'remove', id });
    }
    const options = ((S && S.containers) || []).map(x => `<option value="${esc(x.name)}">${esc(x.alias || x.name)}</option>`).join('');
    openModal('连接容器到网络', '选择要加入该网络的容器',
      `<div class="field"><label>容器</label><select id="netContainer" class="input">${options}</select></div>`,
      async () => act('network', { action: 'connect', id, container: $('#netContainer').value }));
  });
  $$('#networksBody [data-va]').forEach(b => b.onclick = async () => {
    if (!confirm2('voldel:' + b.dataset.name, '再次点击确认删除数据卷')) return;
    await act('volume', { action: 'remove', name: b.dataset.name });
  });
  const refreshBtn = $('#btnNetworksRefresh');
  if (refreshBtn) refreshBtn.onclick = () => act('refresh', {});
}

/* ---------- 告警 · v5.1 消息推送内容 ---------- */
function renderAlerts() {
  const c = S.console, a = c.alerts || {}, hist = c.alert_history || [], sum = c.alert_summary || {};
  const catalog = a.event_catalog || [];
  const pe = a.push_events || {};
  /* v7.0.2：未保存的开关草稿提升到 window，跨 5 秒轮询重绘保留。
     以前用户勾了「面板与系统」里的开关，5 秒后整页按后端值重建，
     其它分组也跟着跳回原值，看起来就像「操作这一组、别的组也跟着变」。 */
  /* v7.0.2：草稿统一从 window 现取，不用闭包缓存对象。renderAlerts 每次
     轮询都会重绘，若把对象缓存在闭包里，保存时清掉的可能是旧闭包的对象，
     新闭包仍指向另一份数据，表现为「保存了却还有未保存修改」。 */
  const D = () => window.__duEvDraft || (window.__duEvDraft = {});
  const evChecked = (id) => Object.prototype.hasOwnProperty.call(D(), id)
    ? !!D()[id] : (pe[id] !== false);
  /* 脏状态按「与后端已保存值不同」计算：保存成功后 duLoad 重绘会带上新的 pe，
     即使草稿对象还留着旧键，差异也自然归零，不会误报「未保存」。 */
  const dirtyCount = () => Object.keys(D()).filter(k => !!D()[k] !== (pe[k] !== false)).length;
  const groups = a.event_groups || ['更新与版本', '容器生命周期', '资源与阈值', '面板与系统'];
  const modes = a.channel_modes || [{ id: 'auto', label: '自动 · 多渠道优先' }];
  const mode = a.channel_mode || 'auto';
  const estats = a.event_stats || {};
  const enabledCount = catalog.filter(x => (Object.prototype.hasOwnProperty.call(D(), x.id) ? !!D()[x.id] : (pe[x.id] !== false))).length;
  const histLv = window.__duHistLv || 'all', histEv = window.__duHistEv || 'all';
  /* v6.0-r9 修复：「仅看已触发」与事件关键词原先是 renderAlerts 的局部变量，
     5 秒自动轮询重建告警面板后状态被清空，筛选看着像「点了没反应 / 过一会自己恢复」。
     与 __duHistLv / __duHistEv 一样提升到 window，重绘后按持久值还原。 */
  let onlyTriggered = !!window.__duOnlyTriggered;
  const evQuery = String(window.__duEvQuery || '');
  const lvLabel = { high: '高危', mid: '注意', info: '信息' };

  const sc = window.__duSelfcheck || null;
  const scHtml = sc ? `
    <div class="panel glass du-selfcheck" id="duSelfcheck">
      <div class="panel-head"><h3>${icon('i-shield')} 通知自检 · 实际发送载荷</h3>
        <span class="lvl ${sc.ok ? 'ok' : 'gray'}">${sc.ok ? '纯 text · 微信端可显示' : '需要检查'}</span>
        <button class="mini-act" id="btnScClose">关闭</button></div>
      <p class="tiny">面板模块 <b>${esc(sc.module || '—')}</b> · 界面版本 <b>${esc(sc.app_version || '—')}</b> ·
        ${sc.ok ? '所有渠道均为 <code>msgtype=text</code>，不含 markdown 字段。升级后请点一次「测试推送」，微信里应直接看到文字。'
                : '如果模块版本与界面版本不一致，说明容器仍在运行旧代码：请执行 <code>sh deploy.sh</code> 重建容器，并在浏览器按 Ctrl+F5。'}</p>
      <pre class="du-sc-json">${esc(JSON.stringify(sc.channels, null, 2))}</pre>
    </div>` : '';
  $('#alertsBody').innerHTML = scHtml + `
    <div class="grid-2">
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-bell')} 告警规则</h3>
        <span class="lvl ${sum.enabled ? 'ok' : 'gray'}">${sum.enabled ? '已启用' : '未启用'}</span></div>
        <label class="switch"><input type="checkbox" id="alEnabled" ${a.enabled ? 'checked' : ''}><span class="track"></span><span>启用告警与 Webhook 推送</span></label>
        <div class="field" style="margin-top:14px"><label>Webhook 地址</label>
          <input id="alUrl" class="input mono" value="${esc(a.webhook_url || '')}" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…">
          <small>通用 JSON POST：兼容企业微信 / 钉钉 / 飞书机器人与自建接口；留空则只记录不推送。</small></div>
        <div class="form-grid">
          <div><label>CPU 阈值（%）</label><input id="alCpu" type="number" class="input" min="1" max="100" value="${a.cpu_threshold ?? 85}"></div>
          <div><label>内存阈值（%）</label><input id="alMem" type="number" class="input" min="1" max="100" value="${a.mem_threshold ?? 85}"></div>
          <div><label>磁盘阈值（%）</label><input id="alDisk" type="number" class="input" min="1" max="100" value="${a.disk_threshold ?? 90}"></div>
          <div><label>去重间隔（秒）</label><input id="alDedupe" type="number" class="input" min="30" max="86400" value="${a.dedupe_interval ?? 300}"></div>
          <div><label>网络阈值（KB/s）</label><input id="alNet" type="number" class="input" min="64" max="1048576" value="${a.net_threshold ?? 20480}"></div>
        </div>
        <p class="tiny">网络阈值按单容器上下行合计速率计算，20480 KB/s ≈ 20 MB/s；与 CPU / 内存一样需连续 3 次采样超限才推送，可在下方关闭「网络流量异常」。</p>
        <div class="notice small-notice"><svg class="ic"><use href="#i-bell"/></svg><span>容器新增 / 删除 / 启动 / 停止等生命周期开关，以及全部消息推送内容，都在下方「消息推送内容」里逐项设置。</span></div>
        <div class="head-actions" style="margin-top:16px">
          <button class="btn primary" id="btnAlertSave">${icon('i-check')} 保存设置</button>
          <button class="btn ghost" id="btnAlertTest">${icon('i-bell')} 测试推送</button>
          <button class="btn ghost" id="btnAlertSelfcheck">${icon('i-shield')} 通知自检</button>
        </div>
        <p class="tiny">CPU / 内存需连续 3 次采样超阈值才触发，避免瞬时抖动；同一对象在去重间隔内不重复推送。</p>
      </div>
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-list')} 告警历史</h3>
        <span class="sub">最近 ${hist.length} 条 · 高危 ${sum.high || 0} 条 · 上次检查 ${esc(String(sum.last_check || '—').slice(5))}</span>
        <button class="mini-act" id="btnAlertExport">导出 CSV</button>
        <button class="mini-act danger" id="btnAlertClear">清空</button></div>
      <div class="hist-tools">
        <label>级别 <select class="input slim-input" id="alHistLv">
          <option value="all">全部</option><option value="high">高危</option>
          <option value="mid">注意</option><option value="info">信息</option></select></label>
        <label>事件 <select class="input slim-input" id="alHistEv"><option value="all">全部事件</option>
          ${catalog.map(x => `<option value="${esc(x.id)}">${esc(x.label)}</option>`).join('')}</select></label>
        <span class="sub" id="alHistCount">${hist.length} 条</span></div>
        <div style="max-height:430px;overflow-y:auto" id="alHistList">${hist.length ? hist.map(h => `<div class="alert-row" data-hist-lv="${esc(h.level || 'info')}" data-hist-ev="${esc(h.kind || '')}">
          <span class="lvl ${h.level === 'high' ? 'bad' : h.level === 'mid' ? 'warn' : 'info'}">${h.level === 'high' ? '高危' : h.level === 'mid' ? '注意' : '信息'}</span>
          <span class="a-msg"><b>${esc(aliasOf(h.name))}</b> · ${esc(h.message)} ${h.sent ? '' : '<span class="subtle" title="' + esc(h.detail || '') + '">(未推送)</span>'}</span>
          <span class="a-time">${esc(String(h.time).slice(5))}</span></div>`).join('') : '<div class="empty-state">暂无告警记录</div>'}</div>
      <div class="empty-state hidden" id="alHistEmpty">没有符合筛选条件的记录</div>
      </div></div>

    <div class="panel glass alerts-events" id="alEvents">
      <div class="panel-head"><h3>${icon('i-sliders')} 消息推送内容</h3>
        <span class="sub">共 ${catalog.length} 项 · 已启用 <b id="evEnabledCount">${enabledCount}</b> 项 · 关闭的事件不推送也不写入告警历史 · <b id="evDirty">${dirtyCount() ? '有 ' + dirtyCount() + ' 项未保存的修改' : '已与后端同步'}</b></span></div>
      <div class="ev-toolbar">
        <div class="field ev-mode"><label>推送通道</label>
          <select class="input" id="evMode">${modes.map(m => `<option value="${esc(m.id)}" ${m.id === mode ? 'selected' : ''}>${esc(m.label)}</option>`).join('')}</select></div>
        <div class="ev-actions">
          <button class="btn ghost slim" id="evAll" title="作用于所有分组的全部事件">全部启用</button>
          <button class="btn ghost slim" id="evNone" title="作用于所有分组的全部事件">全部停用</button>
          <button class="btn ghost slim" id="evDefault">恢复默认</button>
          <button class="btn ghost slim" id="evCommon">仅常用</button>
          <button class="btn ghost slim${onlyTriggered ? ' active' : ''}" id="evTriggered">仅看已触发</button>
        </div>
        <div class="field ev-search"><label>筛选事件</label>
          <input class="input" id="evSearch" value="${esc(evQuery)}" placeholder="输入关键词，如「更新」「容器」" autocomplete="off" data-lpignore="true" data-form-type="other"></div>
        <div class="ev-actions ev-save">
          <select class="input" id="evTestEvent">${catalog.map(x => `<option value="${esc(x.id)}">${esc(x.label)}</option>`).join('')}</select>
          <button class="btn ghost slim" id="evTest">${icon('i-bell')} 测试此事件</button>
          <button class="btn primary slim" id="evSave">${icon('i-check')} 保存推送设置</button>
        </div>
      </div>
      <p class="tiny ev-hint">多渠道通知在「系统联动 → 通知中心」配置（企业微信 / 钉钉 / 飞书 / Telegram / Bark / Server 酱 / 自定义 Webhook）；本页 Webhook 是另一条独立通道。默认「自动」优先走多渠道，没配置才用本页 Webhook，避免重复收两条。</p>
      <p class="tiny ev-hint">每项右下角显示「最近触发时间 · 累计次数」，用于判断哪些事件值得开启；「仅看已触发」只做本地筛选，不改开关。</p>
      ${groups.map(g => {
        const items = catalog.filter(x => x.group === g);
        if (!items.length) return '';
        return `<div class="ev-group" data-ev-group="${esc(g)}">
          <div class="ev-group-head"><b>${esc(g)}</b><span class="sub">${items.length} 项 ·
            <button class="mini-act" data-ev-group-toggle="${esc(g)}" title="只作用于「${esc(g)}」这一组">本组全选 / 全不选</button></span></div>
          <div class="ev-grid">${items.map(x => `<label class="ev-item" data-ev-text="${esc(x.label + ' ' + x.id + ' ' + x.desc)}" data-ev-group-item="${esc(g)}">
            <input type="checkbox" data-ev="${esc(x.id)}" ${evChecked(x.id) ? 'checked' : ''} ${x.id === 'container_down' ? 'id="alDown"' : ''}>
            <span class="ev-body"><b>${esc(x.label)}</b><em>${esc(x.desc)}</em>
              <span class="ev-meta"><code>${esc(x.id)}</code>${estats[x.id] && estats[x.id].count
                ? `<span class="ev-stat">最近 ${esc(String(estats[x.id].last || '').slice(5, 16))} · 共 ${Number(estats[x.id].count)} 次</span>`
                : '<span class="ev-stat ev-stat-none">尚未触发</span>'}</span></span></label>`).join('')}</div></div>`;
      }).join('')}
      <div id="evEmpty" class="empty-state hidden">没有匹配的事件，换个关键词试试</div>
    </div>`;

  const cfg = () => {
    const push_events = {};
    $$('[data-ev]').forEach(i => { push_events[i.dataset.ev] = i.checked; });
    return {
      enabled: $('#alEnabled').checked, webhook_url: $('#alUrl').value.trim(),
      cpu_threshold: Number($('#alCpu').value), mem_threshold: Number($('#alMem').value),
      disk_threshold: Number($('#alDisk').value), dedupe_interval: Number($('#alDedupe').value),
      net_threshold: Number($('#alNet').value),
      container_down: !!push_events.container_down,
      channel_mode: $('#evMode').value, push_events,
    };
  };
  const syncCount = () => {
    const n = $$('[data-ev]').filter(i => i.checked).length;
    $('#evEnabledCount').textContent = n;
  };
  /* v7.0.2：所有改动同时写入草稿；分组按钮只在自己的 .ev-group 内查找，
     从 DOM 结构上杜绝「其它分组跟着变」。 */
  const markDirty = () => {
    const n = dirtyCount();
    const el = $('#evDirty');
    if (el) el.textContent = n ? `有 ${n} 项未保存的修改（点「保存推送设置」生效）` : '已与后端同步';
  };
  const setAll = (fn) => {
    $$('[data-ev]').forEach(i => {
      i.checked = fn(i.dataset.ev);
      D()[i.dataset.ev] = i.checked;
    });
    syncCount(); markDirty();
  };
  $$('[data-ev]').forEach(i => i.addEventListener('change', () => {
    D()[i.dataset.ev] = i.checked;
    markDirty();
  }));
  $('#evAll').onclick = () => setAll(() => true);
  $('#evNone').onclick = () => setAll(() => false);
  $('#evDefault').onclick = () => setAll(k => {
    const x = catalog.find(y => y.id === k); return x ? !!x.default : true;
  });
  $('#evCommon').onclick = () => setAll(k => [
    'found_update', 'update_ok', 'update_fail', 'auto_rollback', 'container_added',
    'container_removed', 'container_stopped', 'container_down', 'disk_high', 'login_fail'
  ].includes(k));
  $('#evTriggered').onclick = () => {
    onlyTriggered = !onlyTriggered;
    window.__duOnlyTriggered = onlyTriggered;
    $('#evTriggered').classList.toggle('active', onlyTriggered);
    applyFilter();
  };
  $$('[data-ev-group-toggle]').forEach(btn => btn.onclick = () => {
    const box = btn.closest('[data-ev-group]');
    if (!box) return;
    const items = $$('[data-ev]', box);          // 只在本组内取，绝不跨组
    const allOn = items.length > 0 && items.every(i => i.checked);
    items.forEach(i => { i.checked = !allOn; D()[i.dataset.ev] = i.checked; });
    syncCount(); markDirty();
  });
  const applyFilter = () => {
    const q = ($('#evSearch').value || '').trim().toLowerCase();
    let shown = 0;
    $$('[data-ev-group]').forEach(g => {
      let n = 0;
      $$('[data-ev-text]', g).forEach(item => {
        const id = item.querySelector('[data-ev]')?.dataset.ev;
        const triggered = !!(estats[id] && estats[id].count);
        const vis = (!q || String(item.dataset.evText).toLowerCase().includes(q))
                    && (!onlyTriggered || triggered);
        item.classList.toggle('ev-hidden', !vis);
        if (vis) n++;
      });
      g.classList.toggle('ev-hidden', n === 0);
      shown += n;
    });
    $('#evEmpty').classList.toggle('hidden', shown > 0);
  };
  $('#evSearch').addEventListener('input', e => {
    window.__duEvQuery = e.target.value; applyFilter();
  });
  $('#evSearch').addEventListener('keydown', e => { e.target.dataset.duTouched = '1'; });
  /* v6.0-r9：自动轮询重绘后按持久状态还原按钮 active 与筛选结果，不再被 5 秒刷新冲掉 */
  if (onlyTriggered || evQuery) applyFilter();

  const saveAll = async () => {
    const d = await api('/api/console', { method: 'POST', body: { kind: 'alerts-config', body: cfg() } });
    window.__duEvDraft = {};   // 已落盘，整份草稿丢弃（不依赖任何闭包）
    markDirty();
    toast(d.message || '已保存', 'ok');
    /* v7.0.2：全量刷新放到后台，不再 await。保存已落盘、草稿已清、提示已弹，
       按钮的 async 流程立刻结束；/api/state 慢或 Docker 暂时不可用时不会把按钮拖住。 */
    Promise.resolve(window.duLoad()).catch(() => {});
    return d;
  };
  $('#btnAlertSave').onclick = async () => { try { await saveAll(); } catch (e) { toast(e.message, 'err'); } };
  $('#evSave').onclick = async () => { try { await saveAll(); } catch (e) { toast(e.message, 'err'); } };
  $('#btnAlertTest').onclick = async () => {
    try {
      await api('/api/console', { method: 'POST', body: { kind: 'alerts-config', body: cfg() } });
      const t = await api('/api/console', { method: 'POST', body: { kind: 'alerts-test', body: {} } });
      toast(t.message || '测试推送成功', t.skipped ? 'info' : 'ok');
      Promise.resolve(window.duLoad()).catch(() => {});
    } catch (e) { toast(e.message, 'err'); }
  };
  $('#evTest').onclick = async () => {
    try {
      await api('/api/console', { method: 'POST', body: { kind: 'alerts-config', body: cfg() } });
      const t = await api('/api/console', { method: 'POST', body: {
        kind: 'alerts-test', body: { event: $('#evTestEvent').value } } });
      toast(t.message || '测试完成', t.skipped ? 'info' : 'ok');
      Promise.resolve(window.duLoad()).catch(() => {});
    } catch (e) { toast(e.message, 'err'); }
  };
  $('#btnAlertSelfcheck').onclick = async () => {
    try {
      const r = await api('/api/console', { method: 'POST', body: { kind: 'alerts-selfcheck', body: {} } });
      const d = r.selfcheck || {};
      const chs = (d.channels && d.channels.channels) || [];
      const aw = d.alert_webhook || {};
      const bad = [aw, ...chs].filter(x => x && x.has_markdown);
      /* v6.0-r15：版本以界面徽章为准（静态 HTML 来自当前升级包），后端 module
         由 notice.py 提供；不再硬编码版本号，避免每次改版都漏改，也避免
         「界面已是新版、容器里还跑旧代码」却被误判为自检通过。 */
      const verEl = document.querySelector('h1 .ver');
      const uiVer = (verEl ? verEl.textContent : '').trim() || ('v' + (d.app_version || ''));
      const sameVer = !!uiVer && d.module === uiVer;
      window.__duSelfcheck = {
        module: d.module, app_version: d.app_version, ui_version: uiVer,
        ok: sameVer && bad.length === 0,
        channels: { alert_webhook: aw, channels: chs }
      };
      toast(sameVer
              ? (bad.length ? '自检完成：发现载荷仍含 markdown 字段，请查看下方明细' : '自检完成：载荷已展示在告警页顶部')
              : '检测到旧代码，请执行 sh deploy.sh 重建容器',
            sameVer && !bad.length ? 'ok' : (sameVer ? 'info' : 'err'));
      renderAlerts();
    } catch (e) { toast(e.message, 'err'); }
  };
  if ($('#btnScClose')) $('#btnScClose').onclick = () => {
    window.__duSelfcheck = null; renderAlerts();
  };
  $('#btnAlertClear').onclick = async () => {
    if (!confirm2('alertclear', '再次点击确认清空告警历史')) return;
    await act('alerts-clear', {});
  };

  // 告警历史：级别 / 事件本地筛选（不重建 DOM，不会被自动刷新冲掉）
  $('#alHistLv').value = histLv; $('#alHistEv').value = histEv;
  const applyHistFilter = () => {
    const lv = $('#alHistLv').value, ev = $('#alHistEv').value;
    window.__duHistLv = lv; window.__duHistEv = ev;
    let n = 0;
    $$('#alHistList .alert-row').forEach(row => {
      const vis = (lv === 'all' || row.dataset.histLv === lv)
                  && (ev === 'all' || row.dataset.histEv === ev);
      row.classList.toggle('ev-hidden', !vis);
      if (vis) n++;
    });
    $('#alHistCount').textContent = n + ' / ' + hist.length + ' 条';
    $('#alHistEmpty').classList.toggle('hidden', n > 0 || !hist.length);
  };
  $('#alHistLv').onchange = applyHistFilter;
  $('#alHistEv').onchange = applyHistFilter;
  if (histLv !== 'all' || histEv !== 'all') applyHistFilter();

  $('#btnAlertExport').onclick = () => {
    const rows = hist.filter(h =>
      (histLv === 'all' || h.level === histLv) && (histEv === 'all' || h.kind === histEv));
    if (!rows.length) { toast('当前筛选下没有可导出的记录', 'info'); return; }
    const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
    const csv = '\uFEFF' + ['时间', '级别', '事件', '对象', '内容', '已推送', '推送结果'].join(',') + '\n'
      + rows.map(h => [h.time, lvLabel[h.level] || h.level,
                       (catalog.find(x => x.id === h.kind) || {}).label || h.kind,
                       h.name, h.message, h.sent ? '是' : '否', h.detail || '']
                       .map(q).join(',')).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    a.download = 'DockerUpdate-告警历史-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast('已导出 ' + rows.length + ' 条告警记录', 'ok');
  };
  /* v6.0-r2：通知中心已合并进告警页，只加载一次，自动轮询不重绘（避免冲掉正在填的表单） */
  const nc = $('#alertChannels');
  if (window.duRenderNotifyChannels && nc && !nc.dataset.bound) {
    nc.dataset.bound = '1';
    window.duRenderNotifyChannels(nc);
  }

}

/* ---------- 容器详情抽屉 ---------- */
async function openDetail(name) {
  detailName = name; detailTab = 'perf'; detailData = null; detailSeries = [];
  window.duOpenDrawer('containerDrawer');
  $('#cdTitle').textContent = aliasOf(name);
  $('#cdSub').textContent = '原容器名 ' + name + ' · 正在加载…';
  $('#cdBody').innerHTML = '<div class="empty-state">加载中…</div>';
  try {
    const [d, m] = await Promise.all([
      api('/api/console-detail?name=' + encodeURIComponent(name)),
      api('/api/container-metrics?name=' + encodeURIComponent(name)),
    ]);
    detailData = d; detailSeries = m.series || []; detailData.live = m.latest || {};
    $('#cdSub').textContent = `原容器名 ${name} · ${d.status} · ${d.image}`;
    renderDetailTab();
    renderDetailFoot();
  } catch (e) {
    $('#cdBody').innerHTML = '<div class="empty-state">' + esc(e.message) + '</div>';
  }

}
function renderDetailTab() {
  if (!detailData) return;
  $$('#cdTabs .dtab').forEach(b => b.classList.toggle('active', b.dataset.dtab === detailTab));
  if (['terminal', 'process', 'inspect'].includes(detailTab) && window.duProDetail) {
    window.duProDetail(detailTab, detailName, $('#cdBody'), detailData);
    return;
  }
  const d = detailData, live = d.live || {};
  if (detailTab === 'perf') {
    $('#cdBody').innerHTML = `
      <div class="grid-4" style="margin-bottom:14px">
        <div class="kpi glass info"><div class="k-label">CPU</div><div class="k-num">${live.cpu ?? 0}<small>%</small></div></div>
        <div class="kpi glass"><div class="k-label">内存</div><div class="k-num">${live.mem ?? 0}<small>MB</small></div><div class="k-sub">${live.mem_percent ?? 0}%${live.mem_limit ? ' / 限 ' + live.mem_limit + 'MB' : ''}</div></div>
        <div class="kpi glass"><div class="k-label">网络</div><div class="k-num" style="font-size:17px">↓${speed(live.net_rx)}</div><div class="k-sub">↑ ${speed(live.net_tx)}</div></div>
        <div class="kpi glass"><div class="k-label">进程 / 磁盘 IO</div><div class="k-num">${live.pids ?? 0}</div><div class="k-sub">读 ${speed(live.io_read)} · 写 ${speed(live.io_write)}</div></div>
      </div>
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-chart')} 最近 ${detailSeries.length} 次采样</h3>
        <span class="sub">每 ${S?.console?.monitor?.sample_interval || 3} 秒 · 当前内存 ${live.mem ?? 0} MB</span></div>
        ${chartMarkup(detailSeries, [
          { key: 'cpu', max: 100, color: 'primary' }, { key: 'mem', color: 'green', area: false }
        ], { interval: S?.console?.monitor?.sample_interval, aria: '容器 CPU 与内存采样' })}
        <div class="chart-legend"><span><i></i> CPU %（0-100）</span><span><i class="g2"></i> 内存（相对峰值归一化）</span></div></div>
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-info')} 运行状态</h3></div>
        <dl class="kv"><dt>状态</dt><dd>${esc(d.status)} · ${d.running ? '运行中' : '未运行'}</dd>
          <dt>健康检查</dt><dd>${esc(d.health || '未配置')}</dd>
          <dt>启动时间</dt><dd>${esc(d.started_at || '—')}</dd>
          <dt>重启次数</dt><dd>${d.restart_count}</dd>
          <dt>运行时长</dt><dd>${dur(live.uptime)}</dd>
          <dt>资源限制</dt><dd>CPU ${d.cpu_cores || '不限'} 核 · 内存 ${d.memory_mb ? d.memory_mb + ' MB' : '不限'}</dd></dl></div>`;
    renderCharts();
    return;
  }
  if (detailTab === 'logs') {
    $('#cdBody').innerHTML = `<div class="panel-head"><h3>${icon('i-list')} 容器日志</h3>
      <div class="tools"><select id="logTail" class="select"><option value="100">最近 100 行</option><option value="300" selected>最近 300 行</option><option value="1000">最近 1000 行</option></select>
      <button class="mini-act" id="btnLogRefresh">${icon('i-refresh')} 刷新</button></div></div>
      <div class="logbox" id="logBox">加载中…</div>`;
    const load = async () => {
      try {
        const t = await api('/api/console-logs?name=' + encodeURIComponent(detailName) + '&tail=' + $('#logTail').value);
        $('#logBox').textContent = t.lines || '（无日志输出）';
        $('#logBox').scrollTop = $('#logBox').scrollHeight;
      } catch (e) { $('#logBox').textContent = e.message; }
    };
    $('#btnLogRefresh').onclick = load;
    $('#logTail').onchange = load;
    if (window.duProEnhanceLogs) window.duProEnhanceLogs(detailName, $('#cdBody'), load);
    load();
    return;
  }
  if (detailTab === 'config') {
    const kv = (label, value) => `<dt>${label}</dt><dd>${esc(value || '—')}</dd>`;
    $('#cdBody').innerHTML = `
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-sliders')} 配置</h3></div>
        <dl class="kv">${kv('镜像', d.image)}${kv('镜像 ID', d.image_id)}${kv('创建时间', d.created)}${kv('重启策略', d.restart_policy)}
          ${kv('网络模式', d.network_mode)}${kv('工作目录', d.working_dir)}${kv('用户', d.user)}${kv('特权模式', d.privileged ? '是 ⚠️' : '否')}
          ${kv('入口命令', (d.entrypoint || []).join(' '))}${kv('启动命令', (d.cmd || []).join(' '))}${kv('日志路径', d.log_path)}</dl></div>
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-list')} 环境变量</h3></div>
        <div class="chip-list">${(d.env || []).map(e => `<span class="m mono">${esc(e)}</span>`).join('') || '<span class="subtle">无</span>'}</div></div>
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-tag')} 标签</h3></div>
        <div class="chip-list">${Object.entries(d.labels || {}).map(([k,v]) => `<span class="m mono">${esc(k)} = ${esc(v)}</span>`).join('') || '<span class="subtle">无</span>'}</div></div>
      <div class="panel glass"><div class="panel-head"><h3>${icon('i-cpu')} 进程</h3></div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr>${(d.top?.Titles || []).map(t => `<th>${esc(t)}</th>`).join('')}</tr></thead>
        <tbody>${(d.top?.Processes || []).slice(0,20).map(row => `<tr>${row.map(x => `<td class="small">${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>`;
    return;
  }
  // mounts
  $('#cdBody').innerHTML = `
    <div class="panel glass"><div class="panel-head"><h3>${icon('i-volume')} 挂载</h3></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>类型</th><th>宿主机 / 源</th><th>容器内</th><th>读写</th></tr></thead>
      <tbody>${(d.mounts || []).map(m => `<tr><td>${esc(m.type)}</td><td class="small">${esc(m.source)}</td><td class="small">${esc(m.destination)}</td><td>${m.rw ? '读写' : '只读'}</td></tr>`).join('') || '<tr><td colspan="4"><div class="empty-state">无挂载</div></td></tr>'}</tbody></table></div>
      <div class="chip-list" style="margin-top:12px">${(d.binds || []).map(b => `<span class="m mono">${esc(b)}</span>`).join('')}</div></div>
    <div class="panel glass"><div class="panel-head"><h3>${icon('i-network')} 网络</h3></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>网络</th><th>IP</th><th>MAC</th><th>别名</th></tr></thead>
      <tbody>${(d.networks || []).map(n => `<tr><td class="strong">${esc(n.name)}</td><td class="mono small">${esc(n.ip)}</td><td class="mono small">${esc(n.mac)}</td><td class="small">${(n.aliases||[]).map(esc).join(', ')}</td></tr>`).join('')}</tbody></table></div></div>
    <div class="panel glass"><div class="panel-head"><h3>${icon('i-box')} 端口映射</h3></div>
      <div class="chip-list">${(d.ports || []).map(p => `<span class="m">${esc(p.host)} → ${esc(p.container)}</span>`).join('') || '<span class="subtle">无端口映射</span>'}</div></div>`;
}
function renderDetailFoot() {
  const c = S.containers.find(x => x.name === detailName) || {};
  const self = detailName === 'docker-update';
  $('#cdFoot').innerHTML = `
    <button class="mini-act" data-da="start" ${self?'disabled':''}>${icon('i-play')} 启动</button>
    <button class="mini-act" data-da="stop" ${self?'disabled':''}>${icon('i-stop')} 停止</button>
    <button class="mini-act" data-da="restart" ${self?'disabled':''}>${icon('i-refresh')} 重启</button>
    <button class="mini-act" data-da="pause" ${self?'disabled':''}>${icon('i-pause')} 暂停/恢复</button>
    <button class="mini-act" data-da="limits">${icon('i-cpu')} 资源限制</button>
    <button class="mini-act" data-da="note">${icon('i-pencil')} 备注</button>
    <button class="mini-act" data-da="config">${icon('i-download')} 导出配置</button>
    <button class="mini-act danger" data-da="remove" ${self?'disabled':''}>${icon('i-trash')} 删除</button>`;
  $$('#cdFoot [data-da]').forEach(b => b.onclick = async () => {
    const a = b.dataset.da;
    if (a === 'note') return window.duOpenNote(detailName);
    if (a === 'limits') return openLimits(detailName);
    if (a === 'config') return download('/api/container-config?name=' + encodeURIComponent(detailName), 'container-' + detailName + '.json');
    if (a === 'remove' && !confirm2('cdremove:' + detailName, '再次点击确认删除容器')) return;
    await act('container', { name: detailName, action: a });
    if (a === 'remove') { window.duCloseDrawers(); detailName = ''; }
    else setTimeout(() => openDetail(detailName), 900);
  });
}
$('#cdTabs').addEventListener('click', (e) => {
  const b = e.target.closest('[data-dtab]');
  if (b) { detailTab = b.dataset.dtab; renderDetailTab(); }
});

/* ---------- 通用弹窗 ---------- */
function field(label, id, placeholder, type = 'text') {
  return `<div class="field"><label>${label}</label><input id="${id}" type="${type}" class="input" placeholder="${esc(placeholder || '')}"></div>`;
}
function openModal(title, sub, html, onSubmit) {
  $('#gmTitle').textContent = title;
  $('#gmSub').textContent = sub;
  $('#gmBody').innerHTML = html;
  modalSubmit = onSubmit;
  $('#genericModal').classList.remove('hidden');
}
function closeModal() { $('#genericModal').classList.add('hidden'); modalSubmit = null; }
$('#btnGmClose').onclick = closeModal;
$('#btnGmCancel').onclick = closeModal;
$('#genericModal').addEventListener('click', (e) => { if (e.target.id === 'genericModal') closeModal(); });
$('#btnGmSave').onclick = async () => {
  if (modalSubmit) {
    const keep = await modalSubmit();
    if (keep === false) return;
  }
  closeModal();
};
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

function openLimits(name) {
  const c = S.containers.find(x => x.name === name) || {};
  openModal('资源限制', '在线修改 cgroup 硬限制，无需重建容器 · ' + name, `
    <div class="notice small-notice">${icon('i-info')}<span>CPU 以「核」为单位（0.5 = 半个核）；内存以 MB 为单位。留空表示不修改该项。</span></div>
    <div class="form-grid">
      <div><label>CPU 核数（当前 ${c.cpu_cores || '不限'}）</label><input id="limCpu" type="number" class="input" min="0" step="0.1" placeholder="留空不改"></div>
      <div><label>内存 MB（当前 ${c.mem_limit_mb ? c.mem_limit_mb + ' MB' : '不限'}）</label><input id="limMem" type="number" class="input" min="0" step="16" placeholder="留空不改"></div>
    </div>`, async () => {
    const cpu = $('#limCpu').value.trim(), mem = $('#limMem').value.trim();
    if (!cpu && !mem) { toast('请至少填写一项', 'err'); return; }
    await act('limits', { name, cpu_cores: cpu || null, mem_mb: mem || null });
  });
}
function openTag(id) {
  openModal('给镜像打标签', '镜像 ID ' + id, `
    ${field('仓库名', 'tagRepo', 'myregistry.example.com/myapp')}
    ${field('标签', 'tagTag', 'latest')}`, async () => {
    await act('image', { action: 'tag', id, repo: $('#tagRepo').value.trim(), tag: $('#tagTag').value.trim() || 'latest' });
  });
}
function openPull() {
  openModal('拉取镜像', '从公开仓库或已配置认证的私有仓库拉取', `
    ${field('镜像地址', 'pullRef', 'nginx:latest 或 ghcr.io/user/app:v1')}
    <div class="notice small-notice">${icon('i-wifi-off')}<span>后台异步执行，可看顶部进度条。若提示连接仓库超时，需在 NAS 上给 Docker 配置代理或镜像加速。</span></div>`,
    async () => {
      const ref = $('#pullRef').value.trim();
      if (!ref) { toast('请填写镜像地址', 'err'); return false; }
      await act('image', { action: 'pull', ref });
    });
}
function openNetwork() {
  openModal('新建网络', '创建一个 bridge 网络，可选指定子网', `
    ${field('网络名', 'netName', 'my-net')}
    <div class="field"><label>驱动</label><select id="netDriver" class="input"><option value="bridge">bridge</option><option value="overlay">overlay（Swarm）</option></select></div>
    ${field('子网（可选）', 'netSubnet', '172.20.0.0/16')}
    <label class="checkline" style="margin-top:8px"><input type="checkbox" id="netInternal"> 内部网络（禁止外部访问）</label>`, async () => {
    const name = $('#netName').value.trim();
    if (!name) { toast('请填写网络名', 'err'); return false; }
    await act('network', { action: 'create', name,
      driver: $('#netDriver').value, subnet: $('#netSubnet').value.trim(), internal: $('#netInternal').checked });
  });
}
function openAddContainer() {
  const nets = (S.console.networks || []).map(n => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join('');
  openModal('添加容器', '填写基本配置即可创建；如本机没有该镜像会自动拉取', `
    <div class="form-grid">
      <div><label>容器名 *</label><input id="acName" class="input" placeholder="my-app"></div>
      <div><label>镜像 *</label><input id="acImage" class="input" placeholder="nginx:latest"></div>
      <div><label>CPU 核数</label><input id="acCpu" type="number" class="input" min="0" step="0.1" placeholder="不限"></div>
      <div><label>内存 MB</label><input id="acMem" type="number" class="input" min="0" step="16" placeholder="不限"></div>
      <div class="full"><label>启动命令（可选）</label><input id="acCmd" class="input" placeholder="nginx -g daemon off;"></div>
      <div class="full"><label>网络</label><select id="acNet" class="input"><option value="bridge">bridge</option>${nets}<option value="host">host</option></select></div>
      <div class="full"><label>重启策略</label><select id="acRestart" class="input">
        <option value="unless-stopped">unless-stopped</option><option value="always">always</option>
        <option value="on-failure">on-failure</option><option value="no">no</option></select></div>
    </div>
    <div style="margin-top:14px"><label style="font-size:11.5px;font-weight:650;color:var(--text2)">端口映射</label>
      <div id="acPorts"></div><button class="mini-act" id="acAddPort">${icon('i-plus')} 添加端口</button></div>
    <div style="margin-top:14px"><label style="font-size:11.5px;font-weight:650;color:var(--text2)">数据卷（宿主机:容器内）</label>
      <div id="acVols"></div><button class="mini-act" id="acAddVol">${icon('i-plus')} 添加卷</button></div>
    <div style="margin-top:14px"><label style="font-size:11.5px;font-weight:650;color:var(--text2)">环境变量（KEY=VALUE）</label>
      <div id="acEnvs"></div><button class="mini-act" id="acAddEnv">${icon('i-plus')} 添加变量</button></div>
    <label class="checkline" style="margin-top:14px"><input type="checkbox" id="acStart" checked> 创建后立即启动</label>`, async () => {
    const rows = (sel, cls) => $$('#' + sel + ' .' + cls).map(r => ({
      host: r.querySelector('[data-f=host]')?.value || '', container: r.querySelector('[data-f=container]')?.value || '',
      proto: r.querySelector('[data-f=proto]')?.value || 'tcp', value: r.querySelector('[data-f=value]')?.value || '',
    }));
    const body = {
      name: $('#acName').value.trim(), image: $('#acImage').value.trim(),
      command: $('#acCmd').value.trim(), network: $('#acNet').value,
      restart: $('#acRestart').value, cpu_cores: $('#acCpu').value.trim(),
      mem_mb: $('#acMem').value.trim(), start: $('#acStart').checked,
      ports: rows('acPorts', 'dyn-port'), volumes: rows('acVols', 'dyn-vol').map(x => x.value),
      env: rows('acEnvs', 'dyn-env').map(x => x.value),
    };
    if (!body.name || !body.image) { toast('容器名和镜像为必填项', 'err'); return false; }
    await act('create-container', body);
  });
  const addRow = (host, cls, fields) => {
    const div = document.createElement('div');
    div.className = 'dynamic-row ' + cls;
    div.innerHTML = fields + '<button class="icon-btn" data-del title="删除">' + icon('i-close') + '</button>';
    host.appendChild(div);
    div.querySelector('[data-del]').onclick = () => div.remove();
  };
  $('#acAddPort').onclick = () => addRow($('#acPorts'), 'dyn-port',
    '<input class="input" data-f="host" placeholder="宿主机 8080"><input class="input" data-f="container" placeholder="容器 80"><select class="input" data-f="proto" style="max-width:90px"><option>tcp</option><option>udp</option></select>');
  $('#acAddVol').onclick = () => addRow($('#acVols'), 'dyn-vol',
    '<input class="input" data-f="value" placeholder="/vol1/1000/docker/app:/data">');
  $('#acAddEnv').onclick = () => addRow($('#acEnvs'), 'dyn-env',
    '<input class="input" data-f="value" placeholder="TZ=Asia/Shanghai">');
}


/* ==================================================================
   v6.0-r17 · ① 整机端口占用（容器大模块下的独立子页面）
   数据来自宿主机 /proc/net，后端只读扫描；这里用签名缓存避免 5 秒轮询
   反复重建表格（历史「输入框被自动刷新冲掉」的成因）。
   ================================================================== */
let portsData = null, portsAt = 0, portsLoading = false, portsSig = '';
let pFilter = 'all', pQuery = '';
const PORTS_CACHE_MS = 30000;

async function loadPorts(force) {
  if (portsLoading) return portsData;
  if (!force && portsData && Date.now() - portsAt < PORTS_CACHE_MS) return portsData;
  portsLoading = true;
  try {
    const d = await api('/api/pro', {
      method: 'POST', body: { kind: 'ports', body: { force: !!force } }, progress: false
    });
    if (d && d.ok !== false && Array.isArray(d.rows)) { portsData = d; portsAt = Date.now(); }
  } catch (e) { /* 失败保留上一次结果，不白屏 */ }
  portsLoading = false;
  return portsData;
}

function renderPorts() {
  if (!portsData) {
    const box = $('#portsBody');
    if (box) box.innerHTML = '<div class="ports-empty">正在读取整机端口表…</div>';
    loadPorts(false).then(() => { if (view === 'container-ports') paintPorts(); });
    return;
  }
  paintPorts();
}

/* v6.0-r19：rows 已是「每个端口一行」，TIME_WAIT / ESTABLISHED 折叠到 connections。
   归属列直接写容器名 + 镜像名 + 容器内端口，宿主进程写进程名 + PID。 */
function paintPorts() {
  const box = $('#portsBody');
  if (!box || !portsData) return;
  const legacy = portsData.rows || [];
  const listeners = portsData.listeners || legacy.filter(r => r.listen);
  const conns = portsData.connections || legacy.filter(r => !r.listen);
  const showConn = pFilter === 'connections';
  const base = showConn ? conns : listeners;
  const q = pQuery.trim().toLowerCase();
  let rows = base.filter(r => {
    if (!showConn) {
      if (pFilter === 'container' && !(r.containers && r.containers.length)) return false;
      if (pFilter === 'host' && (r.containers && r.containers.length)) return false;
      if (pFilter === 'conflict' && !r.conflict) return false;
    }
    if (q && ![r.port, r.proto, r.state, r.local, r.owner, r.container,
               r.image, r.process, r.pid].join(' ').toLowerCase().includes(q)) return false;
    return true;
  });
  const sig = JSON.stringify([portsData.time, listeners.length, conns.length,
    portsData.summary, pFilter, q,
    rows.map(r => [r.port, r.proto, r.state, r.local, r.owner, r.image,
                   r.process, r.connections, r.conflict])]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;

  const sm = portsData.summary || {};
  const kpi = (label, v, cls) => `<div class="pk ${cls || ''}"><b>${esc(String(v))}</b><span>${esc(label)}</span></div>`;
  const kbox = $('#portsKpi');
  if (kbox) kbox.innerHTML = kpi('监听端口', sm.listen ?? 0) + kpi('TCP', sm.tcp ?? 0)
    + kpi('UDP', sm.udp ?? 0) + kpi('容器映射', sm.container_ports ?? 0)
    + kpi('宿主进程占用', sm.host_ports ?? 0)
    + kpi('已识别归属', sm.identified ?? 0)
    + kpi('连接态已折叠', sm.connections ?? 0)
    + kpi('疑似冲突', sm.conflicts ?? 0, (sm.conflicts ? 'warn' : ''));
  const cnt = $('#portsCount');
  if (cnt) cnt.textContent = showConn
    ? `${rows.length} 条连接态 · 扫描时间 ${portsData.time || '—'}`
    : `${rows.length} 个端口（${conns.length} 条连接态已折叠） · 扫描时间 ${portsData.time || '—'}`;
  const note = $('#portsNote');
  if (note) note.innerHTML = `<b>${esc(portsData.source || '')}</b> · ${esc(portsData.note || '')}`
    + (portsData.conflicts && portsData.conflicts.length
        ? ` · 疑似冲突：${portsData.conflicts.map(x => esc(x.proto + '/' + x.port)).join('、')}` : '');

  if (!rows.length) { box.innerHTML = '<div class="ports-empty">没有符合条件的端口</div>'; return; }
  const stateText = r => Object.keys(r.states || {}).length
    ? `${r.connections || 0} 条 · ` + Object.entries(r.states).map(([k, v]) => `${k}×${v}`).join('、')
    : (r.connections ? `${r.connections} 条` : '—');
  box.innerHTML = `<div style="max-height:560px;overflow-y:auto"><table class="tbl"><thead><tr>
      <th>端口</th><th>协议</th><th>状态</th><th>监听地址</th><th>归属（容器 / 镜像 / 进程）</th><th>进程</th><th>连接</th></tr></thead><tbody>
    ${rows.map(r => `<tr>
      <td data-label="端口"><b class="mono">${esc(String(r.port))}</b>${r.conflict ? ' <span class="ports-badge conflict">疑似冲突</span>' : ''}</td>
      <td data-label="协议"><span class="mono">${esc(String(r.proto).toUpperCase())}${r.ip_version === 6 ? '6' : ''}</span></td>
      <td data-label="状态"><span class="ports-badge ${r.listen ? 'listen' : 'idle'}">${esc(r.state || '')}</span></td>
      <td data-label="监听地址"><span class="mono">${esc(r.local || '—')}</span>${(r.addresses || []).length > 1 ? `<div class="sub">共 ${r.addresses.length} 个地址：${esc((r.addresses || []).join('、'))}</div>` : ''}</td>
      <td data-label="归属"><span class="ports-owner">${esc(r.owner || (r.containers && r.containers.length ? r.container : '系统 / 宿主进程'))}</span>${r.image ? `<div class="sub">镜像 ${esc(r.image)}</div>` : ''}</td>
      <td data-label="进程"><span class="ports-owner">${esc(r.process || (r.pid ? 'PID ' + r.pid : '—'))}${r.pid ? ' <span class="sub">#' + esc(String(r.pid)) + '</span>' : ''}</span></td>
      <td data-label="连接"><span class="ports-owner">${esc(r.listen ? stateText(r) : '连接态')}</span></td>
    </tr>`).join('')}</tbody></table></div>`;
}

async function doPortsCheck() {
  const port = ($('#portsCheckPort') || {}).value;
  const proto = ($('#portsCheckProto') || {}).value || 'tcp';
  const out = $('#portsCheckResult');
  if (!out) return;
  if (!port) { out.innerHTML = `<div class="notice small-notice">请先填写要检查的端口号</div>`; return; }
  try {
    const d = await api('/api/pro', {
      method: 'POST', body: { kind: 'ports-check', body: { port, proto } }, progress: false
    });
    const owners = (d.owners || []).map(x => `${esc(x.local)} → ${esc(x.owner)}`).join('；');
    out.innerHTML = `<div class="notice small-notice ${d.used ? '' : 'ok-notice'}">
      ${icon(d.used ? 'i-info' : 'i-check')}<span><b>${esc(d.message || '')}</b>
      ${owners ? '<br>占用者：' + owners : ''}${d.connections ? `<br>另有 ${Number(d.connections)} 条连接记录` : ''}
      <br><span class="tiny">来源：${esc(d.source || '')} · ${esc(d.time || '')}</span></span></div>`;
    if (d.used) toast(d.message, 'info'); else toast(d.message, 'ok');
    portsData = null; // 让下一次刷新重新扫描
  } catch (e) { out.innerHTML = `<div class="notice small-notice danger-notice">${esc(e.message || '检查失败')}</div>`; }
}

/* ==================================================================
   v6.0-r17 · ② 外网连通性监测（告警页）
   断网当下只有「内网 / 局域网 Webhook」可能送达；云端通道没送达的，
   恢复后由后端自动补发。面板如实写明这个限制，不假装断网也能推到微信。
   ================================================================== */
let nwData = null, nwAt = 0, nwLoading = false;
const NW_CACHE_MS = 30000;

async function loadNetWatch(force) {
  if (nwLoading) return nwData;
  if (!force && nwData && Date.now() - nwAt < NW_CACHE_MS) return nwData;
  nwLoading = true;
  try {
    const d = await api('/api/pro', { method: 'POST', body: { kind: 'netwatch', body: {} }, progress: false });
    if (d && d.ok !== false && d.config) { nwData = d; nwAt = Date.now(); }
  } catch (e) { /* 保留上一次状态 */ }
  nwLoading = false;
  return nwData;
}

function renderNetWatch() {
  const box = $('#netWatchBody');
  if (!box) return;
  if (!nwData) {
    box.innerHTML = '<div class="panel glass"><div class="panel-head"><h3>外网连通性监测</h3></div><div class="ports-empty">正在读取监测状态…</div></div>';
    loadNetWatch(false).then(() => { if (view === 'alerts') paintNetWatch(); });
    return;
  }
  paintNetWatch();
}

function paintNetWatch() {
  const box = $('#netWatchBody');
  if (!box || !nwData) return;
  /* 正在输入时不重建，避免历史「刚填完就被自动刷新恢复」 */
  if (window.duUserInteracting && window.duUserInteracting()) return;
  const c = nwData.config || {}, st = nwData.status || {};
  const sig = JSON.stringify([c, st, nwData.targets, (nwData.pending || []).length, (nwData.events || []).slice(0, 5)]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;

  const stateLabel = { online: '外网在线', offline: '外网断网', paused: '监测已暂停', unknown: '等待首次探测' }[st.state] || '未知';
  const sw = (id, label, on) => `<label class="switch"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span class="track"></span><span>${esc(label)}</span></label>`;
  const fld = (label, id, val, type, extra) => `<div class="field"><label>${esc(label)}</label><input id="${id}" class="input" type="${type || 'text'}" value="${esc(String(val ?? ''))}" ${extra || ''}></div>`;
  const targets = (nwData.targets || []).map(t => `<span class="nw-target"><i class="${t.ok ? 'ok' : 'bad'}"></i>
      <span class="mono">${esc(t.target)}</span><span class="sub">${t.ok ? esc(t.ms + ' ms') : esc(String(t.error || '').slice(0, 26))}</span></span>`).join('') || '<span class="sub">尚未探测</span>';

  box.innerHTML = `<div class="panel glass" id="nwPanel">
    <div class="panel-head nw-head"><h3>${icon('i-wifi-off')} 外网连通性监测</h3>
      <span class="nw-state ${esc(st.state || 'unknown')}"><i></i>${esc(stateLabel)}</span>
      <span class="sub">可达 ${Number(st.reachable || 0)}/${Number(st.target_total || (c.targets || []).length)} ·
        上次探测 ${esc(st.last_probe || '—')} · 上次切换 ${esc(st.last_change || '—')}</span></div>
    <div class="nw-targets">${targets}</div>
    <div class="nw-grid" style="margin-top:14px">
      ${sw('nwEnabled', '启用外网连通性监测', c.enabled)}
      ${sw('nwQueue', '恢复后补发未送达的断网通知', c.queue_offline)}
      <div class="field" style="grid-column:1/-1"><label>探测目标（一行一个，host:port；全部不可达才判定断网）</label>
        <textarea id="nwTargets" class="input mono" rows="3" spellcheck="false">${esc((c.targets || []).join('\n'))}</textarea></div>
      ${fld('探测间隔（秒）', 'nwInterval', c.interval_s, 'number', 'min="10" max="3600"')}
      ${fld('连续失败几次判定断网', 'nwDownAfter', c.down_after, 'number', 'min="1" max="10"')}
      ${fld('连续成功几次判定恢复', 'nwUpAfter', c.up_after, 'number', 'min="1" max="10"')}
      ${fld('单次连接超时（秒）', 'nwTimeout', c.timeout_s, 'number', 'min="1" max="15"')}
      <div class="field" style="grid-column:1/-1"><label>内网 / 局域网通知地址（可选，断网当下仍可达）</label>
        <input id="nwLan" class="input mono" value="${esc(c.lan_webhook || '')}" placeholder="http://192.168.1.x:8123/api/webhook/…">
        <small>企业微信 / 钉钉等云端 Webhook 在真正断网时也发不出去；这里填局域网内的 HA、自建接收端或本地 Bark，才能在断网当下收到消息。</small></div>
    </div>
    <div class="head-actions" style="margin-top:12px;flex-wrap:wrap">
      <button class="btn primary slim" id="nwSave">${icon('i-check')} 保存设置</button>
      <button class="btn ghost slim" id="nwProbe">${icon('i-refresh')} 立即探测</button>
      <button class="btn ghost slim" id="nwTestDown">${icon('i-bell')} 测试断网通知</button>
      <button class="btn ghost slim" id="nwTestUp">${icon('i-bell')} 测试恢复通知</button>
    </div>
    <div class="nw-limits">${esc(nwData.limits || '')}<br>
      断网 / 恢复是否推送，由下方「消息推送内容」里的「外网断网 / 网络异常」「外网恢复」两项开关控制；
      关闭后仍会写运行日志与监测状态，只是不发消息。</div>
    ${(nwData.events || []).length ? `<div class="panel-head" style="margin-top:12px"><h3>最近监测事件</h3>
      <span class="sub">${(nwData.pending || []).length ? '有 ' + nwData.pending.length + ' 条断网消息待恢复后补发' : '队列为空'}</span></div>
      <div class="nw-events">${(nwData.events || []).slice(0, 12).map(e => `<div class="nw-event">
        <time>${esc(String(e.time || '').slice(5))}</time><b>${esc(e.kind || '')}</b><span>${esc(e.message || '')}</span></div>`).join('')}</div>` : ''}
  </div>`;

  const $n = (id) => $('#' + id, box);
  const patch = () => ({
    enabled: $n('nwEnabled').checked,
    queue_offline: $n('nwQueue').checked,
    targets: $n('nwTargets').value,
    interval_s: $n('nwInterval').value,
    down_after: $n('nwDownAfter').value,
    up_after: $n('nwUpAfter').value,
    timeout_s: $n('nwTimeout').value,
    lan_webhook: $n('nwLan').value,
  });
  $n('nwSave').onclick = async () => {
    try {
      const d = await api('/api/pro', { method: 'POST', body: { kind: 'netwatch-save', body: patch() }, progress: false });
      toast(d.message || '已保存', 'ok');
      box.dataset.sig = ''; await loadNetWatch(true); paintNetWatch();
    } catch (e) { toast(e.message || '保存失败', 'err'); }
  };
  $n('nwProbe').onclick = async () => {
    try {
      const d = await api('/api/pro', { method: 'POST', body: { kind: 'netwatch-probe', body: {} }, progress: false });
      const pr = d.probe || {};
      toast(`探测完成：${Number(pr.reachable || 0)}/${Number(pr.total || 0)} 个目标可达 · 当前 ${esc(d.state || '')}`, 'ok');
      box.dataset.sig = ''; await loadNetWatch(true); paintNetWatch();
    } catch (e) { toast(e.message || '探测失败', 'err'); }
  };
  const test = (ev) => async () => {
    try {
      const d = await api('/api/pro', { method: 'POST', body: { kind: 'netwatch-test', body: { event: ev } }, progress: false });
      toast(d.message || '测试消息已提交', d.ok === false ? 'err' : 'ok');
      box.dataset.sig = ''; await loadNetWatch(true); paintNetWatch();
    } catch (e) { toast(e.message || '测试失败', 'err'); }
  };
  $n('nwTestDown').onclick = test('net_down');
  $n('nwTestUp').onclick = test('net_up');
}

/* ---------- v6.0-r17 静态控件事件（委托，表格重绘不会冲掉输入） ---------- */
document.addEventListener('click', (e) => {
  const f = e.target.closest('[data-pf]');
  if (f) {
    pFilter = f.dataset.pf || 'all';
    $$('#portsFilters [data-pf]').forEach(b => b.classList.toggle('active', b.dataset.pf === pFilter));
    paintPorts();
    return;
  }
  if (e.target.closest('#btnPortsCheck')) { doPortsCheck(); return; }
  if (e.target.closest('#btnPortsRefresh')) {
    portsData = null; portsSig = '';
    loadPorts(true).then(() => { if (view === 'container-ports') { const b = $('#portsBody'); if (b) b.dataset.sig = ''; paintPorts(); } });
    toast('正在重新扫描整机端口…', 'info');
  }
});
document.addEventListener('input', (e) => {
  if (e.target && e.target.id === 'portsSearch') {
    pQuery = String(e.target.value || '').toLowerCase(); paintPorts();
  }
});

/* ---------- 供 app.js 清理自动填充时调用 ---------- */
window.duResetConsoleSearch = () => {
  cQuery = ''; iQuery = '';
  if (window.duResetProSearch) window.duResetProSearch();
};
window.duResetRegistrySearch = () => { if (window.duRegistryReset) window.duRegistryReset(); };

/* ---------- 初始 ---------- */
renderLinks();
})();
