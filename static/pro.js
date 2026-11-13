/* DockerUpdate v3.0 · Docker 管理能力补齐（对照 docker要求.txt P0 / P1 / P2）
   · Compose 栈 · 应用模板 · 系统联动 三个独立页面
   · 概览补充统计 / 容器明细表 / 容器详情新标签 / 日志增强 / 完整创建表单
   依赖 app.js 的 duApi / duToast / duEsc / duIcon 与 job.js 的 duWatchJob。 */
(function () {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = window.duEsc, icon = window.duIcon, api = window.duApi, toast = window.duToast;

let P = null, curView = 'system', sysTab = 'accel';
let stacks = [], templates = [], sysData = {};
let modalStack = [], term = null;

async function pro(kind, body, opts) {
  opts = opts || {};
  return await api('/api/pro', {
    method: 'POST', body: { kind, body: body || {} },
    progressLabel: opts.label, progressStages: opts.stages,
    progressImmediate: !!opts.immediate,
  });
}
async function follow(d) {
  if (d && d.async && d.job_id && window.duWatchJob) return await window.duWatchJob(d.job_id, d._ph);
  return d;
}
function val(el, sel) { const n = $(sel, el); return n ? String(n.value || '').trim() : ''; }
function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function b64save(data, filename) {
  const bin = atob(data || '');
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([arr]));
  a.download = filename || 'download.bin';
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
}
function txtsave(text, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  a.download = filename; document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
}
function guardSearch(root) {
  $$('input[type="search"]', root || document).forEach((el) => {
    ['keydown', 'paste', 'pointerdown', 'focus'].forEach((ev) =>
      el.addEventListener(ev, () => { el.dataset.duTouched = '1'; }));
  });
}
window.duResetProSearch = () => {
  $$('#viewSystem input[type="search"], #proContainers input[type="search"]')
    .forEach((el) => { if (el.dataset.duTouched !== '1' && el.value) el.value = ''; });
};

/* ---------------- 弹窗（支持嵌套：目录选择器 / 模板部署） ---------------- */
function openModal(title, sub, body, buttons, wide) {
  const el = document.createElement('div');
  el.className = 'modal-mask';
  el.innerHTML = `<div class="modal-card glass ${wide ? 'pro-modal wide' : 'pro-modal'}" role="dialog" aria-modal="true">
    <div class="modal-head"><div><h3>${esc(title)}</h3><p>${esc(sub || '')}</p></div>
      <button class="icon-btn" data-pro-close title="关闭">${icon('i-close')}</button></div>
    <div class="modal-body">${body}</div>
    <div class="modal-foot"><span class="modal-spacer"></span>
      ${(buttons || []).map((b, i) => `<button class="btn ${b.cls || 'ghost'} slim" data-pro-btn="${i}">${b.ico ? icon(b.ico) : ''} ${esc(b.label)}</button>`).join('')}</div></div>`;
  document.body.appendChild(el);
  modalStack.push(el);
  $('[data-pro-close]', el).onclick = () => closeModal(el);
  el.addEventListener('click', (e) => { if (e.target === el) closeModal(el); });
  (buttons || []).forEach((b, i) => {
    const btn = $('[data-pro-btn="' + i + '"]', el);
    if (btn) btn.onclick = () => b.onClick(el);
  });
  guardSearch(el);
  return el;
}
function closeModal(el) {
  const target = el || modalStack[modalStack.length - 1];
  if (!target) return;
  const i = modalStack.indexOf(target);
  if (i >= 0) modalStack.splice(i, 1);
  target.remove();
}
function confirm2(msg, onYes) {
  const el = openModal('请确认', '高危操作需要二次确认', `<p class="pro-hint">${esc(msg)}</p>`, [
    { label: '取消', onClick: (m) => closeModal(m) },
    { label: '确认执行', cls: 'primary', ico: 'i-check', onClick: async (m) => { closeModal(m); await onYes(); } },
  ]);
  return el;
}

/* ---------------- 状态 ---------------- */
async function loadState(force) {
  try { P = await api('/api/pro/state'); } catch (e) { if (!e.auth) toast(e.message, 'err'); }
  if (force || !P) { /* already tried */ }
  return P;
}
function switchTo(v) {
  const b = document.querySelector('[data-view="' + v + '"]');
  if (b) b.click();
}

/* ==================== 概览补充 ==================== */
async function renderOverviewExtra() {
  const box = $('#proOverview');
  if (!box) return;
  let o;
  try { o = await pro('overview'); } catch (e) { box.innerHTML = ''; return; }
  if (!o || !o.ok) { box.innerHTML = ''; return; }
  const kpi = (label, v, sub, cls) => `<div class="kpi glass ${cls || ''}"><div class="k-label">${label}</div>
    <div class="k-num">${v}</div><div class="k-sub">${sub || ''}</div></div>`;
  box.innerHTML = `<div class="panel glass pro-extra"><div class="panel-head"><h3>${icon('i-chart')} 全局统计与 Docker 资源占用</h3>
      <span class="sub">P0 · 容器 / 镜像 / 卷 / 网络 与告警红点</span></div>
    ${o.docker_error ? `<div class="notice small-notice danger-notice" style="margin-bottom:12px">${icon('i-info')}<span>${esc(o.docker_error)}</span></div>` : ''}
    <div class="grid-4">
      ${kpi('容器总数', o.total, '运行 ' + o.running + ' · 停止 ' + o.stopped)}
      ${kpi('镜像数量', o.images, '本地镜像库')}
      ${kpi('卷 / 网络', o.volumes + ' / ' + o.networks, '命名卷与自定义网络')}
      ${kpi('Docker 磁盘', o.docker_disk_gb + ' GB', '镜像层合计')}
    </div>
    <div class="grid-3" style="margin-top:12px">
      ${kpi('Docker 整体 CPU', o.docker_cpu + ' %', '所有容器采样合计')}
      ${kpi('Docker 整体内存', o.docker_mem_mb + ' MB', '所有容器采样合计')}
      ${kpi('异常 / 高占用', (o.flags || []).length, '红点标记', (o.flags || []).length ? 'warn' : 'ok')}
    </div>
    <div class="panel-head" style="margin-top:16px"><h3>${icon('i-bell')} 快捷入口与红点告警</h3></div>
    <div class="pro-actions">
      <button class="btn ghost slim" id="poImages">${icon('i-image')} 本地镜像</button>
      <button class="btn ghost slim" id="poPrune">${icon('i-trash')} 清理垃圾</button>
      <button class="btn ghost slim" id="poCreate">${icon('i-plus')} 新建容器</button>
    </div>
    ${(!o.docker_error && !Number(o.docker_cpu) && !Number(o.docker_mem_mb))
      ? `<p class="store-hint">CPU / 内存合计为 0：通常是资源采样刚启动、或「设置 → 资源监控」未开启。开启后等待一次采样（默认 3 秒）即会显示实时合计。</p>` : ''}
    ${(o.flags || []).length ? `<div class="pro-flags">${o.flags.map(f =>
      `<span class="chip ${f.level === 'danger' ? 'danger' : 'warn'}"><i class="dot ${f.level}"></i>
        ${esc(f.name)} · ${esc(f.reason)}</span>`).join('')}</div>`
      : '<p class="store-hint">当前没有异常退出或占用过高的容器。</p>'}
  </div>`;
  const goto = window.duGotoView;
  $('#poImages').onclick = () => { if (goto) goto('images'); };
  $('#poCreate').onclick = () => openCreateContainer();
  $('#poPrune').onclick = () => confirm2('将清理悬空镜像与未被使用的孤立卷，数据不会误删。确认继续？', async () => {
    try {
      const r = await pro('volumes-prune', { confirm: true }, { label: '正在清理孤立卷' });
      toast(r.message || '孤立卷已清理', 'ok');
    } catch (e) { if (!e.auth) toast(e.message, 'err'); }
  });
}

/* ==================== 容器明细表 ==================== */
let cRows = [], cTagFilter = '', cQuery = '';
async function renderContainerExtra() {
  const box = $('#proContainers');
  if (!box) return;
  let r;
  try { r = await pro('container-table'); } catch (e) { box.innerHTML = ''; return; }
  cRows = (r && r.rows) || [];
  const tags = [...new Set(cRows.flatMap(x => x.labels || []).map(x => x.split('=')[0]))].sort();
  const draw = () => {
    const rows = cRows.filter(x =>
      (!cTagFilter || (x.labels || []).some(l => l.startsWith(cTagFilter + '='))) &&
      (!cQuery || [x.name, x.image, x.state, (x.labels || []).join(' ')].join(' ').toLowerCase().includes(cQuery.toLowerCase())));
    box.innerHTML = `<div class="panel glass pro-extra"><div class="panel-head"><h3>${icon('i-box')} 容器明细（端口 / 标签 / 创建时间 / 红点）</h3>
        <span class="sub">显示 ${rows.length} / ${cRows.length}</span></div>
      <div class="toolbar glass compact">
        <div class="search"><svg class="ic"><use href="#i-search"/></svg>
          <input id="pcSearch" name="du-pro-q" type="search" placeholder="搜索名称 / 镜像 / 标签…" value="${esc(cQuery)}"
            autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-form-type="other"></div>
        <select id="pcTag" class="select"><option value="">全部标签分组</option>
          ${tags.map(t => `<option value="${esc(t)}" ${t === cTagFilter ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
        <button class="btn ghost slim" id="pcCreate">${icon('i-plus')} 新建容器（完整表单）</button>
      </div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th>容器</th><th>状态</th><th>镜像</th><th>端口</th><th>CPU / 内存</th><th>标签</th><th>创建时间</th></tr></thead>
        <tbody>${rows.length ? rows.map(x => `<tr>
          <td class="strong">${x.dot ? `<i class="dot ${x.dot}" title="${x.dot === 'danger' ? '异常退出' : '占用过高'}"></i> ` : ''}${esc(x.name)}</td>
          <td><span class="badge ${x.state === 'running' ? 'ok' : 'warn'}">${esc(x.state)}</span></td>
          <td class="small mono">${esc(x.image)}</td>
          <td class="small">${(x.ports || []).map(esc).join('<br>') || '—'}</td>
          <td class="small">${x.cpu.toFixed(1)}% / ${x.mem.toFixed(0)} MB</td>
          <td class="small">${(x.labels || []).slice(0, 3).map(esc).join('<br>') || '—'}</td>
          <td class="small">${esc(x.created)}</td></tr>`).join('')
          : '<tr><td colspan="7"><div class="empty-state">没有匹配的容器</div></td></tr>'}</tbody></table></div>
      <p class="store-hint">红点：红色＝异常退出（非零退出码），橙色＝CPU 或内存占用超过 85%。标签分组取自容器 Labels。</p>
    </div>`;
    guardSearch(box);
    $('#pcSearch').oninput = (e) => { cQuery = e.target.value; draw(); };
    $('#pcTag').onchange = (e) => { cTagFilter = e.target.value; draw(); };
    $('#pcCreate').onclick = () => openCreateContainer();
  };
  draw();
}

/* ==================== 完整创建容器表单 ==================== */
function openDirPicker(onPick) {
  let path = (P && P.settings && P.settings.template_root) || '/vol1/1000/docker';
  const el = openModal('选择 fnOS 共享文件夹', '目录树来自宿主机只读挂载 /host，点目录进入，点「选这个目录」填入挂载路径',
    `<div class="pro-dirnav"><button class="btn ghost slim" id="pdUp">↑ 上一级</button>
      <span class="mono small" id="pdPath">${esc(path)}</span></div>
     <div id="pdList" class="pro-dirlist">加载中…</div>
     <p class="store-hint">可写根 ${esc((P && P.settings && P.settings.template_root) || '/vol1/1000/docker')} 下的目录可作为容器数据目录；只读目录可挂载为 :ro。</p>`, [
      { label: '取消', onClick: (m) => closeModal(m) },
      { label: '选这个目录', cls: 'primary', ico: 'i-check', onClick: (m) => { onPick($('#pdPath', m).textContent.trim()); closeModal(m); } },
    ], true);
  const load = async () => {
    const list = $('#pdList', el);
    try {
      const d = await pro('dirs', { path });
      $('#pdPath', el).textContent = d.path || path;
      path = d.path || path;
      list.innerHTML = (d.dirs || []).length
        ? d.dirs.map(x => `<button class="chip pro-dir" data-p="${esc(x.path)}">${icon('i-archive')} ${esc(x.name)}</button>`).join('')
        : '<span class="sub">该目录下没有子目录</span>';
      $$('[data-p]', list).forEach(b => b.onclick = async () => { path = b.dataset.p; await load(); });
      if (d.hint) list.insertAdjacentHTML('beforeend', `<p class="store-hint">${esc(d.hint)}</p>`);
    } catch (e) { list.innerHTML = '<span class="sub">' + esc(e.message) + '</span>'; }
  };
  $('#pdUp', el).onclick = async () => { path = path.split('/').slice(0, -1).join('/') || '/'; await load(); };
  load();
}

async function localImages() {
  try { const s = await api('/api/state'); return (s.console && s.console.images) || []; }
  catch (_) { return []; }
}
async function openCreateContainer() {
  const imgs = await localImages();
  const el = openModal('新建容器（完整表单）', 'P0 · 镜像选择 / 端口 / 环境变量 / fnOS 目录挂载 / 高级选项 / 原始命令',
    `<div class="pro-form">
      <label>镜像来源</label>
      <select class="input" id="ccSource"><option value="custom">自定义输入 / 镜像仓库</option>
        ${imgs.slice(0, 80).map(i => `<option value="${esc((i.tags && i.tags[0]) || i.id || '')}">本地：${esc((i.tags && i.tags[0]) || i.id || '')}</option>`).join('')}</select>
      <label>镜像（repo:tag）</label><input class="input" id="ccImage" placeholder="nginx:latest">
      <label>容器名称</label><input class="input" id="ccName" placeholder="my-nginx">
      <label>端口映射 <span class="sub">一行一个 宿主机:容器[/udp]</span></label>
      <textarea class="input" id="ccPorts" rows="2" placeholder="8080:80"></textarea>
      <label>环境变量 <span class="sub">一行一个 KEY=VALUE</span></label>
      <textarea class="input" id="ccEnv" rows="2" placeholder="TZ=Asia/Shanghai"></textarea>
      <label>挂载 / 数据卷 <span class="sub">一行一个 /宿主机路径:/容器路径[:ro]</span></label>
      <div class="pro-inline"><textarea class="input" id="ccVols" rows="2" placeholder="/vol1/1000/docker/app:/data"></textarea>
        <button class="btn ghost slim" id="ccPick">${icon('i-archive')} 浏览 fnOS 目录</button></div>
      <label>网络 / 重启策略</label>
      <div class="pro-inline"><input class="input" id="ccNetwork" value="bridge">
        <select class="input" id="ccRestart"><option>unless-stopped</option><option>always</option><option>on-failure</option><option>no</option></select></div>
      <label>资源限制（可选）</label>
      <div class="pro-inline"><input class="input" id="ccCpu" placeholder="CPU 核数"><input class="input" id="ccMem" placeholder="内存 MB"></div>
      <label class="checkline"><input type="checkbox" id="ccAdvanced"> 显示高级选项（高危，默认隐藏）</label>
      <div id="ccAdvBox" class="hidden pro-adv">
        <label>UID / GID（User 字段）</label><input class="input" id="ccUser" placeholder="1000:1000">
        <label>额外命令（Cmd，空格分隔）</label><input class="input" id="ccCmd" placeholder="/bin/sh -c 'echo hi'">
        <label class="checkline"><input type="checkbox" id="ccPrivileged"> 特权模式 privileged（高危）</label>
        <label class="checkline"><input type="checkbox" id="ccReadOnly"> 只读根文件系统 read-only</label>
        <label class="checkline"><input type="checkbox" id="ccConfirm"> 我已确认上述高级选项的风险</label>
      </div>
      <label>原始 docker run 命令（实时生成，可复制）</label>
      <pre class="pro-code" id="ccCmdOut">—</pre>
      <p class="store-hint">安全约束：高级项默认隐藏且必须二次勾选；特权模式会放宽容器隔离，请只在可信镜像上开启。</p>
    </div>`, [
      { label: '取消', onClick: (m) => closeModal(m) },
      { label: '复制原始命令', ico: 'i-external', onClick: (m) => {
        navigator.clipboard && navigator.clipboard.writeText($('#ccCmdOut', m).textContent);
        toast('docker run 命令已复制', 'ok'); } },
      { label: '创建并启动', cls: 'primary', ico: 'i-plus', onClick: async (m) => {
        const adv = !!$('#ccAdvanced', m).checked;
        const body = {
          name: val(m, '#ccName'), image: val(m, '#ccImage') || val(m, '#ccSource'),
          ports: val(m, '#ccPorts').split(/\n|,/).map(x => x.trim()).filter(Boolean),
          env: val(m, '#ccEnv').split('\n').map(x => x.trim()).filter(Boolean),
          volumes: val(m, '#ccVols').split('\n').map(x => x.trim()).filter(Boolean),
          network: val(m, '#ccNetwork') || 'bridge', restart: val(m, '#ccRestart'),
          cpu_cores: val(m, '#ccCpu'), mem_mb: val(m, '#ccMem'),
          advanced: adv, advanced_confirmed: adv && !!$('#ccConfirm', m).checked,
          user: val(m, '#ccUser'), command: val(m, '#ccCmd'),
          privileged: adv && !!$('#ccPrivileged', m).checked,
          read_only: adv && !!$('#ccReadOnly', m).checked,
        };
        if (!body.name || !body.image) { toast('请填容器名与镜像', 'err'); return; }
        closeModal(m);
        try {
          const r = await pro('create-advanced', body, { label: '正在创建容器「' + body.name + '」', immediate: true });
          toast((r && r.message) || '容器已创建', 'ok');
          if (window.duReload) window.duReload();
        } catch (e) { if (!e.auth) toast(e.message, 'err'); }
      } },
    ], true);
  $('#ccSource', el).onchange = (e) => { if (e.target.value !== 'custom') $('#ccImage', el).value = e.target.value; };
  $('#ccAdvanced', el).onchange = (e) => $('#ccAdvBox', el).classList.toggle('hidden', !e.target.checked);
  $('#ccPick', el).onclick = () => openDirPicker((p) => {
    const t = $('#ccVols', el);
    t.value = (t.value ? t.value + '\n' : '') + p + ':/data';
    refreshCmd();
  });
  const refreshCmd = async () => {
    try {
      const r = await pro('run-command', {
        name: val(el, '#ccName'), image: val(el, '#ccImage') || val(el, '#ccSource'),
        ports: val(el, '#ccPorts').split(/\n|,/).map(x => x.trim()).filter(Boolean),
        env: val(el, '#ccEnv').split('\n').map(x => x.trim()).filter(Boolean),
        volumes: val(el, '#ccVols').split('\n').map(x => x.trim()).filter(Boolean),
        restart: val(el, '#ccRestart'), user: val(el, '#ccUser'), command: val(el, '#ccCmd'),
        privileged: !!$('#ccPrivileged', el).checked, read_only: !!$('#ccReadOnly', el).checked });
      $('#ccCmdOut', el).textContent = r.command || '—';
    } catch (_) { /* 输入未完成时忽略 */ }
  };
  $$('input, textarea, select', el).forEach(n => n.addEventListener('input', refreshCmd));
  refreshCmd();
}

/* ==================== 容器详情新标签 ==================== */
function renderDetail(tab, name, el, data) {
  if (tab === 'process') renderProcess(name, el);
  else if (tab === 'inspect') renderInspect(name, el);
  else if (tab === 'terminal') renderTerminal(name, el);
}
async function renderProcess(name, el) {
  el.innerHTML = '<div class="empty-state">正在读取进程…</div>';
  try {
    const r = await pro('process', { name });
    el.innerHTML = `<div class="panel glass"><div class="panel-head"><h3>${icon('i-cpu')} ${esc(name)} 进程列表</h3>
      <span class="sub">Docker top · ${(r.processes || []).length} 个进程</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr>${(r.columns || []).map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${(r.processes || []).map(row => `<tr>${row.map(x => `<td class="small mono">${esc(x)}</td>`).join('')}</tr>`).join('') || '<tr><td colspan="9"><div class="empty-state">容器未运行或没有进程</div></td></tr>'}</tbody></table></div></div>`;
  } catch (e) { el.innerHTML = '<div class="empty-state">' + esc(e.message) + '</div>'; }
}
async function renderInspect(name, el) {
  el.innerHTML = '<div class="empty-state">正在读取 Inspect…</div>';
  try {
    const r = await pro('inspect', { name });
    el.innerHTML = `<div class="panel glass"><div class="panel-head"><h3>${icon('i-list')} Inspect 原始 JSON</h3>
      <button class="btn ghost slim" id="piDownload">${icon('i-download')} 下载 JSON</button></div>
      <pre class="pro-code tall">${esc(r.json)}</pre></div>`;
    $('#piDownload').onclick = () => txtsave(r.json, 'inspect-' + name + '.json');
  } catch (e) { el.innerHTML = '<div class="empty-state">' + esc(e.message) + '</div>'; }
}
function renderTerminal(name, el) {
  el.innerHTML = `<div class="panel glass"><div class="panel-head"><h3>${icon('i-box')} Web 终端 · ${esc(name)}</h3>
      <div class="tools"><label class="sub">字体 <input type="range" id="ptFont" min="11" max="22" value="13" style="width:90px"></label>
        <button class="btn ghost slim" id="ptOpen">${icon('i-play')} 连接</button>
        <button class="btn ghost slim" id="ptClose" disabled>${icon('i-close')} 断开</button></div></div>
    <pre class="pro-term" id="ptOut">未连接。点「连接」进入容器 /bin/sh（HTTP 轮询桥接，非 WebSocket）。</pre>
    <div class="pro-inline"><input class="input" id="ptInput" placeholder="输入命令后回车；Ctrl+C 发送中断" disabled>
      <button class="btn primary slim" id="ptSend" disabled>发送</button></div>
    <p class="store-hint">终端通过 Docker exec attach 建立，输出按 300ms 轮询追加。支持字体大小调节；
      vim / top 等全屏程序受轮询延迟影响，复杂交互建议用 SSH。</p></div>`;
  const out = $('#ptOut', el), input = $('#ptInput', el);
  $('#ptFont', el).oninput = (e) => out.style.fontSize = e.target.value + 'px';
  const stopTerm = async () => {
    if (term && term.id) { try { await pro('term-close', { id: term.id }); } catch (_) {} }
    if (term && term.timer) clearInterval(term.timer);
    term = null;
    $('#ptOpen', el).disabled = false; $('#ptClose', el).disabled = true;
    input.disabled = true; $('#ptSend', el).disabled = true;
  };
  $('#ptOpen', el).onclick = async () => {
    try {
      const r = await pro('term-open', { name, cmd: '/bin/sh', cols: 100, rows: 28 },
        { label: '正在打开 Web 终端' });
      term = { id: r.id, buf: '' };
      out.textContent = '';
      $('#ptOpen', el).disabled = true; $('#ptClose', el).disabled = false;
      input.disabled = false; $('#ptSend', el).disabled = false;
      term.timer = setInterval(async () => {
        if (!term) return;
        try {
          const d = await pro('term-read', { id: term.id });
          if (d.data) {
            term.buf += d.data;
            if (term.buf.length > 200000) term.buf = term.buf.slice(-100000);
            out.textContent = stripAnsi(term.buf);
            out.scrollTop = out.scrollHeight;
          }
          if (!d.alive) stopTerm();
        } catch (_) { /* 保留会话 */ }
      }, 300);
      input.focus();
      toast('终端已连接', 'ok');
    } catch (e) { if (!e.auth) toast(e.message, 'err'); }
  };
  $('#ptClose', el).onclick = stopTerm;
  const send = async () => {
    if (!term) return;
    const t = input.value + '\n';
    input.value = '';
    try { await pro('term-write', { id: term.id, data: btoa(unescape(encodeURIComponent(t))) }); } catch (_) {}
  };
  $('#ptSend', el).onclick = send;
  input.onkeydown = (e) => { if (e.key === 'Enter') send(); };
}
function stripAnsi(s) { return String(s || '').replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\r/g, ''); }

function enhanceLogs(name, el, reload) {
  const tools = $('.tools', el);
  if (!tools || $('#plSearch', el)) return;
  tools.insertAdjacentHTML('beforeend', `<input id="plSearch" class="input" style="max-width:150px" placeholder="关键词过滤">
    <label class="checkline"><input type="checkbox" id="plFollow" checked> 跟随滚动</label>
    <button class="btn ghost slim" id="plDownload">${icon('i-download')} 下载日志</button>`);
  const box = $('#logBox', el);
  $('#plSearch', el).oninput = () => {
    const q = $('#plSearch', el).value.toLowerCase();
    const raw = box.dataset.raw || box.textContent;
    box.dataset.raw = box.dataset.raw || raw;
    box.textContent = q ? box.dataset.raw.split('\n').filter(l => l.toLowerCase().includes(q)).join('\n') : box.dataset.raw;
  };
  $('#plFollow', el).onchange = (e) => { window.__duLogFollow = e.target.checked; };
  $('#plDownload', el).onclick = () => txtsave(box.dataset.raw || box.textContent, 'logs-' + name + '.log');
  const orig = reload;
  // 原始 load() 会写 textContent，这里用 MutationObserver 同步 raw 并维持跟随
  const obs = new MutationObserver(() => {
    box.dataset.raw = box.textContent;
    if (window.__duLogFollow !== false) box.scrollTop = box.scrollHeight;
  });
  obs.observe(box, { childList: true, characterData: true, subtree: true });
}

/* ==================== Compose 栈 ==================== */
async function loadStacks() {
  try { stacks = (await pro('stacks')).stacks || []; } catch (e) { stacks = []; }
}
function openNewStack() {
  const el = openModal('新建 Compose 栈', '写 YAML 与 .env，保存后即可 up；支持受限制语法校验与格式化',
    `<div class="pro-form"><label>栈名称</label><input class="input" id="nsName" placeholder="my-stack">
      <label>docker-compose.yml</label><textarea class="input mono" id="nsYaml" rows="12">services:
  web:
    image: nginx:latest
    container_name: my-web
    restart: unless-stopped
    ports:
      - "8080:80"
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - ./data:/data
</textarea>
      <label>.env 环境变量文件</label><textarea class="input mono" id="nsEnv" rows="3" placeholder="TZ=Asia/Shanghai"></textarea>
      <div class="pro-inline"><button class="btn ghost slim" id="nsCheck">${icon('i-check')} 语法校验</button>
        <button class="btn ghost slim" id="nsFormat">${icon('i-list')} 自动格式化</button></div>
      <div id="nsResult" class="sub"></div>
      <p class="store-hint">受限解析：支持 services / image / ports / volumes / environment / restart /
        command / labels / network_mode / user / privileged / read_only / mem_limit；
        不支持 build / anchors / extends / 多行块，遇到会明确报错。</p></div>`, [
      { label: '取消', onClick: (m) => closeModal(m) },
      { label: '保存配置', cls: 'primary', ico: 'i-check', onClick: async (m) => {
        try {
          const r = await pro('stack-save', { name: val(m, '#nsName'), yaml: $('#nsYaml', m).value,
                                              env: $('#nsEnv', m).value }, { label: '正在保存 Compose 配置' });
          closeModal(m); toast(r.message || '已保存', 'ok'); await loadStacks(); render(curView);
        } catch (e) { if (!e.auth) { $('#nsResult', m).textContent = e.message; toast(e.message, 'err'); } }
      } },
    ], true);
  $('#nsCheck', el).onclick = async () => {
    const r = await pro('yaml-check', { text: $('#nsYaml', el).value });
    $('#nsResult', el).textContent = (r.ok ? '✓ ' : '✗ ') + r.message;
  };
  $('#nsFormat', el).onclick = async () => {
    const r = await pro('yaml-format', { text: $('#nsYaml', el).value });
    $('#nsYaml', el).value = r.text; $('#nsResult', el).textContent = '已按 2 空格重排缩进';
  };
}
function openStackEdit(name) {
  pro('stack-read', { name }).then((d) => {
    const el = openModal('编辑栈 · ' + name, d.dir || '',
      `<div class="pro-form"><label>docker-compose.yml</label>
        <textarea class="input mono" id="seYaml" rows="14">${esc(d.yaml || '')}</textarea>
        <label>.env</label><textarea class="input mono" id="seEnv" rows="4">${esc(d.env || '')}</textarea>
        <div class="pro-inline"><button class="btn ghost slim" id="seCheck">${icon('i-check')} 校验</button>
          <button class="btn ghost slim" id="seFormat">${icon('i-list')} 格式化</button>
          <button class="btn ghost slim" id="seDownload">${icon('i-download')} 导出 yml</button></div>
        <div id="seResult" class="sub"></div>
        <p class="store-hint">${d.read_only ? '该 YAML 来自运行中的栈（只读挂载），可复制后另存到可写根。' : '配置保存在可写根，up 时按此文件创建容器。'}</p></div>`, [
        { label: '关闭', onClick: (m) => closeModal(m) },
        { label: '校验并保存', cls: 'primary', ico: 'i-check', onClick: async (m) => {
          try {
            const r = await pro('stack-save', { name, yaml: $('#seYaml', m).value, env: $('#seEnv', m).value },
              { label: '正在保存栈配置' });
            closeModal(m); toast(r.message, 'ok'); await loadStacks(); render(curView);
          } catch (e) { if (!e.auth) $('#seResult', m).textContent = e.message; }
        } },
      ], true);
    $('#seCheck', el).onclick = async () => {
      const r = await pro('yaml-check', { text: $('#seYaml', el).value });
      $('#seResult', el).textContent = (r.ok ? '✓ ' : '✗ ') + r.message;
    };
    $('#seFormat', el).onclick = async () => {
      const r = await pro('yaml-format', { text: $('#seYaml', el).value });
      $('#seYaml', el).value = r.text;
    };
    $('#seDownload', el).onclick = () => txtsave($('#seYaml', el).value, name + '-docker-compose.yml');
  }).catch(e => toast(e.message, 'err'));
}
function openStackLogs(name) {
  const el = openModal('栈日志 · ' + name, '全局日志 / 单服务日志切换',
    `<div class="pro-inline"><select class="input" id="slService"><option value="">全部服务（全局）</option></select>
      <button class="btn ghost slim" id="slRefresh">${icon('i-refresh')} 刷新</button>
      <button class="btn ghost slim" id="slDownload">${icon('i-download')} 下载</button></div>
      <pre class="pro-code tall" id="slOut">加载中…</pre>`, [
      { label: '关闭', onClick: (m) => closeModal(m) },
    ], true);
  const load = async () => {
    const r = await pro('stack-logs', { name, service: val(el, '#slService'), tail: 80 });
    const sel = $('#slService', el);
    if (sel.options.length <= 1) (r.services || []).forEach(s => sel.insertAdjacentHTML('beforeend', `<option>${esc(s)}</option>`));
    $('#slOut', el).textContent = (r.lines || []).join('\n') || '（无日志）';
  };
  $('#slRefresh', el).onclick = load;
  $('#slService', el).onchange = load;
  $('#slDownload', el).onclick = () => txtsave($('#slOut', el).textContent, name + '-logs.log');
  load();
}
async function renderCompose() {
  const box = $('#proCompose');
  if (!box) return;
  if (!stacks.length) await loadStacks();
  box.innerHTML = `<div class="page-head"><div><h2>Compose 栈</h2>
    <p>栈列表 / up·down·重启 / 单服务重启 / 删除栈 / YAML 编辑器 / .env 独立管理 / 导入导出 / 栈日志</p></div>
    <div class="head-actions">
      <button class="btn ghost" id="cpRefresh">${icon('i-refresh')} 刷新</button>
      <button class="btn ghost" id="cpExport">${icon('i-download')} 导出全部</button>
      <label class="btn ghost file-btn">${icon('i-upload')} 导入 Compose<input type="file" id="cpFile" accept=".yml,.yaml,.zip,.json" hidden></label>
      <button class="btn primary" id="cpNew">${icon('i-plus')} 新建栈</button></div></div>
    <div class="notice small-notice">${icon('i-info')}<span>P0 最高优先级：按 Compose 项目标签聚合运行中的栈，
      也列出可写根里手工导入但尚未 up 的栈。up 通过 Docker Engine API 逐服务创建（容器内没有 docker compose 二进制），
      受限语法会明确报错，不会静默丢配置。</span></div>
    <div class="panel glass"><div class="panel-head"><h3>${icon('i-layers')} 栈列表</h3>
      <span class="sub">${stacks.length} 个栈 · 配置根 ${esc((P && P.settings.compose_root) || '')}</span></div>
      <div class="reg-list">${stacks.length ? stacks.map(s => `<div class="reg-row">
        <span class="reg-ic small"><i class="badge ${s.state === 'running' ? 'ok' : (s.state === 'partial' ? 'warn' : 'info')}">${s.count}</i></span>
        <span class="reg-body"><b>${esc(s.name)} <i class="badge ${s.state === 'running' ? 'ok' : 'warn'}">${esc(s.state)}</i></b>
          <em>${esc(s.dir || s.config || '目录未知')} · ${esc(s.created_text)} · ${s.running}/${s.count} 运行中</em>
          <span class="reg-meta">${(s.services || []).map(x => esc(x.name)).join(' · ') || '尚未创建服务'}</span></span>
        <span class="reg-acts">
          <button class="btn ghost slim" data-cp-edit="${esc(s.name)}">${icon('i-list')} YAML / .env</button>
          <button class="btn ghost slim" data-cp-logs="${esc(s.name)}">${icon('i-list')} 日志</button>
          <button class="btn ghost slim" data-cp-restart="${esc(s.name)}">${icon('i-refresh')} 重启栈</button>
          <button class="btn primary slim" data-cp-up="${esc(s.name)}">${icon('i-play')} up</button>
          <button class="btn ghost slim" data-cp-down="${esc(s.name)}">down</button>
          <button class="btn ghost slim" data-cp-del="${esc(s.name)}">${icon('i-trash')}</button>
        </span></div>`).join('') : '<div class="empty-state">还没有 Compose 栈，点「新建栈」或导入 docker-compose.yml</div>'}</div>
    </div>`;
  $('#cpRefresh').onclick = async () => { await loadStacks(); render('compose'); };
  $('#cpNew').onclick = () => openNewStack();
  $('#cpExport').onclick = async () => {
    try { const r = await pro('stack-export', {}, { label: '正在导出 Compose 备份' }); b64save(r.data, r.filename); }
    catch (e) { if (!e.auth) toast(e.message, 'err'); }
  };
  $('#cpFile').onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const data = await new Promise((res) => { const rd = new FileReader(); rd.onload = () => res(String(rd.result).split(',')[1] || ''); rd.readAsDataURL(f); });
    const name = f.name.replace(/\.(yml|yaml|zip|json)$/i, '').replace(/[^a-zA-Z0-9_.-]/g, '-');
    try { const r = await pro('stack-import', { name, data }, { label: '正在导入 Compose 栈' });
      toast(r.message, 'ok'); await loadStacks(); render('compose'); }
    catch (err) { if (!err.auth) toast(err.message, 'err'); }
  };
  $$('[data-cp-edit]', box).forEach(b => b.onclick = () => openStackEdit(b.dataset.cpEdit));
  $$('[data-cp-logs]', box).forEach(b => b.onclick = () => openStackLogs(b.dataset.cpLogs));
  $$('[data-cp-up]', box).forEach(b => b.onclick = async () => {
    try { await follow(await pro('stack-up', { name: b.dataset.cpUp },
      { label: '正在启动 Compose 栈', immediate: true })); await loadStacks(); render('compose'); }
    catch (e) { if (!e.auth) toast(e.message, 'err'); }
  });
  $$('[data-cp-restart]', box).forEach(b => b.onclick = async () => {
    try { const r = await pro('stack-restart', { name: b.dataset.cpRestart }, { label: '正在重启栈' }); toast(r.message, 'ok'); }
    catch (e) { if (!e.auth) toast(e.message, 'err'); }
  });
  $$('[data-cp-down]', box).forEach(b => b.onclick = () => confirm2(
    '将停止并移除栈「' + b.dataset.cpDown + '」的所有容器（配置文件保留）。确认？', async () => {
      try { await follow(await pro('stack-down', { name: b.dataset.cpDown, confirm: true },
        { label: '正在停止栈', immediate: true })); await loadStacks(); render('compose'); }
      catch (e) { if (!e.auth) toast(e.message, 'err'); }
    }));
  $$('[data-cp-del]', box).forEach(b => b.onclick = () => confirm2(
    '删除栈「' + b.dataset.cpDel + '」会移除容器并删除配置目录，不可恢复。确认？', async () => {
      try { await follow(await pro('stack-delete', { name: b.dataset.cpDel, confirm: true },
        { label: '正在删除栈', immediate: true })); await loadStacks(); render('compose'); }
      catch (e) { if (!e.auth) toast(e.message, 'err'); }
    }));
}

/* ==================== 应用模板 ==================== */
async function loadTemplates() {
  try { templates = (await pro('templates')).templates || []; } catch (e) { templates = []; }
}
function highlightYaml(t) {
  return esc(t).split('\n').map(l => l.replace(/^(\s*)([\w.-]+)(:)/, '$1<span class="yk">$2</span>$3')
    .replace(/(#.*)$/, '<span class="yc">$1</span>')).join('\n');
}
function openDeploy(tpl) {
  const vars = (tpl.variables || []).map(v => `<label>${esc(v.label)}（${esc(v.key)}）</label>
    <input class="input" data-var="${esc(v.key)}" value="${esc(v.default || '')}">`).join('');
  const el = openModal('部署 · ' + tpl.name, tpl.desc,
    `<div class="pro-form"><div class="pro-tpl-head"><span class="pro-tpl-ic">${esc(tpl.icon || '🧩')}</span>
      <div><b>${esc(tpl.name)}</b><p>${esc(tpl.category)} · ${esc(tpl.desc)}</p></div></div>
      <label>应用名称 / 安装目录</label><input class="input" id="dpName" value="${esc(tpl.id)}">
      <label>版本（Tag）</label><select class="input" id="dpTag">${(tpl.tags || ['latest']).map(t =>
        `<option>${esc(t)}</option>`).join('')}</select>
      <label>访问端口</label><input class="input" id="dpPort" value="${tpl.port}">
      ${vars}
      <label>预置 Compose（渲染后）</label><pre class="pro-code" id="dpYaml">${highlightYaml(tpl.compose)}</pre>
      <label>挂载提示</label><div class="chip-list">${(tpl.mounts || []).map(m => `<span class="m mono">${esc(m)}</span>`).join('') || '<span class="sub">无</span>'}</div>
      <p class="store-hint">安装位置：<code>${esc((P && P.settings.template_root) || '')}/&lt;应用名&gt;</code>；
        部署后自动写 Compose、.env、README，并尝试同步飞牛桌面图标。访问地址 http://192.168.1.110:${tpl.port}</p></div>`, [
      { label: '取消', onClick: (m) => closeModal(m) },
      { label: '一键部署', cls: 'primary', ico: 'i-download', onClick: async (m) => {
        const variables = {};
        $$('[data-var]', m).forEach(n => variables[n.dataset.var] = n.value);
        const body = { id: tpl.id, name: val(m, '#dpName'), tag: val(m, '#dpTag'),
                       port: val(m, '#dpPort'), variables };
        closeModal(m);
        try { await follow(await pro('template-deploy', body,
          { label: '正在部署「' + tpl.name + '」', immediate: true }));
          toast('部署完成', 'ok'); await loadTemplates(); render('templates'); }
        catch (e) { if (!e.auth) toast(e.message, 'err'); }
      } },
    ], true);
  $('#dpYaml', el).innerHTML = highlightYaml(tpl.compose);
}
function openTemplateAdd() {
  const el = openModal('导入自定义模板', '粘贴 Compose YAML，校验通过后加入模板库（支持版本与默认变量）',
    `<div class="pro-form"><label>模板名称</label><input class="input" id="taName" placeholder="My App">
      <label>分类 / 端口 / 版本（逗号分隔）</label>
      <div class="pro-inline"><input class="input" id="taCat" placeholder="自定义"><input class="input" id="taPort" placeholder="8080">
        <input class="input" id="taTags" placeholder="latest,1.0.0"></div>
      <label>简介 / 挂载提示</label><input class="input" id="taDesc" placeholder="一句话介绍">
      <label>Compose YAML</label><textarea class="input mono" id="taYaml" rows="10">services:
  app:
    image: nginx:latest
    container_name: my-app
    restart: unless-stopped
    ports:
      - "8080:80"
</textarea><div id="taResult" class="sub"></div></div>`, [
      { label: '取消', onClick: (m) => closeModal(m) },
      { label: '校验并导入', cls: 'primary', ico: 'i-check', onClick: async (m) => {
        try {
          const r = await pro('template-add', { name: val(m, '#taName'), category: val(m, '#taCat'),
            port: val(m, '#taPort'), tags: val(m, '#taTags'), desc: val(m, '#taDesc'),
            compose: $('#taYaml', m).value }, { label: '正在导入模板' });
          closeModal(m); toast(r.message, 'ok'); await loadTemplates(); render('templates');
        } catch (e) { if (!e.auth) $('#taResult', m).textContent = e.message; }
      } },
    ], true);
}
async function renderTemplates() {
  const box = $('#proTemplates');
  if (!box) return;
  if (!templates.length) await loadTemplates();
  box.innerHTML = `<div class="page-head"><div><h2>应用模板</h2>
    <p>内置主流自托管服务 · 预置 Compose / 默认变量 / 挂载提示 / 版本选择 · 一键部署 · 自定义模板导入</p></div>
    <div class="head-actions"><button class="btn ghost" id="tpRefresh">${icon('i-refresh')} 刷新</button>
      <button class="btn primary" id="tpAdd">${icon('i-plus')} 导入自定义模板</button></div></div>
    <div class="notice small-notice">${icon('i-info')}<span>模板一键部署为 Docker 容器（Compose 文件落盘），
      不是飞牛 FPK 原生应用；已移除的应用商店分区不会恢复。部署走后台任务 + 真实进度条。</span></div>
    <div class="pro-tpl-grid">${templates.map(t => `<div class="panel glass pro-tpl">
      <div class="pro-tpl-head"><span class="pro-tpl-ic">${esc(t.icon || '🧩')}</span>
        <div><b>${esc(t.name)} ${t.builtin ? '<i class="badge ok">内置</i>' : '<i class="badge info">自定义</i>'}</b>
        <p>${esc(t.category)}</p></div></div>
      <p class="pro-tpl-desc">${esc(t.desc)}</p>
      <div class="chip-list"><span class="m">:${t.port}</span>${(t.tags || []).slice(0, 3).map(x => `<span class="m mono">${esc(x)}</span>`).join('')}</div>
      <div class="pro-tpl-foot"><button class="btn primary slim" data-tp-deploy="${esc(t.id)}">${icon('i-download')} 一键部署</button>
        ${t.custom ? `<button class="btn ghost slim" data-tp-del="${esc(t.id)}">${icon('i-trash')}</button>` : ''}</div>
    </div>`).join('')}</div>`;
  $('#tpRefresh').onclick = async () => { await loadTemplates(); render('templates'); };
  $('#tpAdd').onclick = () => openTemplateAdd();
  $$('[data-tp-deploy]', box).forEach(b => b.onclick = () => {
    const t = templates.find(x => x.id === b.dataset.tpDeploy); if (t) openDeploy(t);
  });
  $$('[data-tp-del]', box).forEach(b => b.onclick = async () => {
    try { const r = await pro('template-delete', { id: b.dataset.tpDel }); toast(r.message, 'ok');
      await loadTemplates(); render('templates'); } catch (e) { if (!e.auth) toast(e.message, 'err'); }
  });
}

/* v6.0-r2：通知渠道渲染抽成可复用函数，告警页与系统联动共用一份配置。 */
async function renderNotifyChannels(body, rerender) {
  await loadSys(false);
  const D = sysData || {};
  const go = rerender || (async () => {
    await loadSys(true);
    await renderNotifyChannels(body, null);
  });
  const n = D.notify || {}, c = n.config || {};
  const types = n.types || [
    { id: 'webhook', label: '自定义 Webhook', hint: 'https://…/notify' },
    { id: 'wecom', label: '企业微信机器人', hint: 'https://qyapi.weixin.qq.com/…' },
    { id: 'dingtalk', label: '钉钉机器人', hint: 'https://oapi.dingtalk.com/…' },
    { id: 'feishu', label: '飞书机器人', hint: 'https://open.feishu.cn/…' },
    { id: 'telegram', label: 'Telegram Bot', hint: 'https://api.telegram.org/bot…/sendMessage' },
    { id: 'bark', label: 'Bark', hint: 'https://bark.example.com/<KEY>' },
    { id: 'serverchan', label: 'Server 酱', hint: 'https://sctapi.ftqq.com/<KEY>.send' },
  ];
  const chans = c.channels || [], evs = c.events || {};
  const EVENTS = [['update_ok', '容器更新成功'], ['update_fail', '更新失败'],
    ['auto_rollback', '健康检查失败自动回退'], ['found_update', '发现新版本'],
    ['container_down', '容器意外退出'], ['check_fail', '检测失败']];
  const eff = n.effective || {};
  const effText = eff.fallback_active
    ? `当前是「自动 · 多渠道优先」：通知中心没有启用任何渠道，消息会自动回退到 <b>告警页「告警规则」里配置的 Webhook</b>（${esc(eff.alert_webhook_masked || '未配置')}）发出。所以即使这里没添加企业微信机器人，微信里仍然收得到消息。`
    : (eff.alert_in_use
      ? `当前实际会用到的通道：通知中心 ${esc(String(eff.channels_enabled || 0))} 个渠道${esc(eff.alert_webhook_masked ? '，以及告警页 Webhook（' + eff.alert_webhook_masked + '）' : '')}。`
      : `当前没有可用的推送通道：通知中心 ${esc(String(eff.channels_enabled || 0))} 个渠道，告警页 Webhook ${eff.alert_webhook_configured ? '已配置但未启用' : '未配置'}。`);
  const effHtml = `<div class="notice eff-banner ${eff.fallback_active ? 'warn' : ''}">
    <svg class="ic"><use href="#i-bell"/></svg>
    <div class="eff-body"><b>当前实际生效的推送通道</b><span>${effText}</span></div>
    ${eff.alert_webhook_configured ? '<button class="btn ghost slim" id="nfDisableAlert">停用告警页 Webhook</button>' : ''}
  </div>`;
  body.innerHTML = `<div class="panel glass"><div class="panel-head"><h3>${icon('i-bell')} 通知中心 · 多渠道</h3>
    <span class="sub">${chans.length} 个渠道 · 按事件开关推送</span></div>
    <p class="store-hint">${esc(n.hint || '')}</p>
    ${effHtml}
    <label class="checkline v50-check"><input type="checkbox" id="nfEnabled" ${c.enabled ? 'checked' : ''}> 启用通知推送</label>
    <h4>已配置的渠道</h4>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>启用</th><th>类型</th><th>名称</th><th>地址</th><th>令牌 / 加签</th><th>操作</th></tr></thead>
      <tbody>${chans.length ? chans.map((x, i) => `<tr data-nf-row="${i}">
        <td><input type="checkbox" data-nf-en="${i}" ${x.enabled ? 'checked' : ''}></td>
        <td><select class="input slim" data-nf-type="${i}">${types.map(t =>
          `<option value="${esc(t.id)}" ${x.type === t.id ? 'selected' : ''}>${esc(t.label)}</option>`).join('')}</select></td>
        <td><input class="input slim" data-nf-name="${i}" value="${esc(x.name || '')}"></td>
        <td><input class="input slim mono" data-nf-url="${i}" value="${esc(x.url || '')}"></td>
        <td class="nf-two"><input class="input slim" data-nf-token="${i}" value="${esc(x.token || '')}" placeholder="令牌 / chat_id">
          <input class="input slim" data-nf-secret="${i}" value="${esc(x.secret || '')}" placeholder="加签密钥"></td>
        <td><button class="btn ghost slim" data-nf-del="${i}">删除</button></td></tr>`).join('')
        : '<tr><td colspan="6" class="sub">还没有渠道，在下面添加一条；面板不内置任何地址或凭据。</td></tr>'}</tbody></table></div>
    <h4>添加渠道</h4>
    <div class="nf-add"><select class="input" id="nfNewType">${types.map(t =>
      `<option value="${esc(t.id)}">${esc(t.label)}</option>`).join('')}</select>
      <input class="input" id="nfNewName" placeholder="名称（可留空）">
      <input class="input mono" id="nfNewUrl" placeholder="完整 http(s) 地址">
      <input class="input" id="nfNewToken" placeholder="令牌 / chat_id（可选）">
      <input class="input" id="nfNewSecret" placeholder="加签密钥（可选）">
      <button class="btn primary slim" id="nfAdd">${icon('i-plus')} 添加</button></div>
    <div class="nf-hint sub">${esc((types[0] || {}).hint || '')}；Telegram 的「令牌」栏填 chat_id；钉钉 / 飞书可选填加签密钥。</div>
    <h4>推送事件</h4>
    <div class="nf-events">${EVENTS.map(([k, label]) =>
      `<label class="checkline"><input type="checkbox" data-nf-ev="${k}" ${evs[k] ? 'checked' : ''}> ${label}</label>`).join('')}</div>
    <div class="pro-inline"><button class="btn primary slim" id="nfSave">${icon('i-check')} 保存</button>
      <button class="btn ghost slim" id="nfTest">${icon('i-refresh')} 测试推送</button></div>
    <div id="nfResult" class="sub"></div></div>`;
  const collect = () => ({
    enabled: $('#nfEnabled', body).checked,
    channels: $$('[data-nf-row]', body).map((row) => {
      const i = row.dataset.nfRow;
      return { id: (chans[i] || {}).id || '', type: val(body, `[data-nf-type="${i}"]`),
        name: val(body, `[data-nf-name="${i}"]`), url: val(body, `[data-nf-url="${i}"]`),
        token: val(body, `[data-nf-token="${i}"]`), secret: val(body, `[data-nf-secret="${i}"]`),
        enabled: !!$(`[data-nf-en="${i}"]`, body).checked };
    }),
    events: Object.fromEntries($$('[data-nf-ev]', body).map(x => [x.dataset.nfEv, x.checked])),
  });
  $$('[data-nf-del]', body).forEach(btn => btn.onclick = () => {
    const d = collect(); d.channels.splice(+btn.dataset.nfDel, 1);
    pro('notify-save', d).then(r => { toast(r.message, 'ok'); go(); })
      .catch(e => toast(e.message, 'err'));
  });
  const disBtn = $('#nfDisableAlert', body);
  if (disBtn) disBtn.onclick = async () => {
    try { const r = await pro('notify-disable-alert', {}); toast(r.message || '已停用', 'ok'); go(); }
    catch (e) { if (!e.auth) toast(e.message, 'err'); }
  };
  $('#nfNewType', body).onchange = (e) => {
    const t = types.find(x => x.id === e.target.value) || {};
    $('.nf-hint', body).textContent = (t.hint || '') + (t.desc ? ' · ' + t.desc : '');
  };
  $('#nfAdd', body).onclick = async () => {
    const url = val(body, '#nfNewUrl').trim();
    if (!/^https?:\/\//i.test(url)) { toast('请填完整的 http(s) 地址', 'err'); return; }
    const d = collect();
    d.channels.push({ type: val(body, '#nfNewType'), name: val(body, '#nfNewName'),
      url, token: val(body, '#nfNewToken'), secret: val(body, '#nfNewSecret'), enabled: true });
    try { const r = await pro('notify-save', d); toast(r.message, 'ok'); go(); }
    catch (e) { if (!e.auth) toast(e.message, 'err'); }
  };
  $('#nfSave', body).onclick = async () => {
    try { const r = await pro('notify-save', collect()); toast(r.message, 'ok'); go(); }
    catch (e) { if (!e.auth) toast(e.message, 'err'); } };
  $('#nfTest', body).onclick = async () => {
    try {
      await pro('notify-save', collect());
      const r = await pro('notify-test', {});
      $('#nfResult', body).textContent = r.message || r.error || '';
      toast(r.message || '测试完成', r.skipped ? 'info' : 'ok');
    } catch (e) { if (!e.auth) toast(e.message, 'err'); } };
}
window.duRenderNotifyChannels = (body) => renderNotifyChannels(body, null);


/* ==================== 系统联动 ==================== */
/* v6.0-r2：通知中心已合并进「告警」模块，系统联动只保留计划任务 / 加速源。 */
const SYS_TABS = [
  ['tasks', '计划任务'], ['accel', '加速源'],
];
/* v3.3：tab 切换原来每次都发 10 个 POST，才「卡一小下」。
   改成首次进入 / 手动刷新 / 保存后强制加载，切 tab 只用缓存重绘 DOM。 */
let sysLoaded = false;
/* v3.9：落盘位置已删除，草稿逻辑随之移除。 */
async function loadSys(force) {
  if (sysLoaded && !force) return;
  try {
    const [notify, tasks, accel] = await Promise.all([
      pro('notify-config'), pro('tasks'), pro('accel'),
    ]);
    sysData = { notify, tasks, accel };
    sysLoaded = true;
  } catch (e) { /* 单项失败不影响其它 */ }
}
window.duProInvalidate = () => { sysLoaded = false; };
function sysHead(active) {
  return `<div class="pro-sys-tabs">${SYS_TABS.map(([k, label]) =>
    `<button class="chip ${k === active ? 'on' : ''}" data-sys="${k}">${esc(label)}</button>`).join('')}</div>`;
}
async function renderSystem(force) {
  const box = $('#proSystem');
  if (!box) return;
  if (!force && window.duUserInteracting && window.duUserInteracting()) return;
  /* v3.9：系统联动是静态配置页，5 秒自动轮询不应整页重建
     （旧版每 5 秒 box.innerHTML 重绘一次，看起来「一直在自己刷新」，还会清掉刚填的表单）。
     只有首次进入 / 切 tab / 手动刷新 / 保存后强制加载才重绘。 */
  if (!force && sysLoaded && box.dataset.rendered === sysTab) return;
  await loadSys(force);
  box.dataset.rendered = sysTab;
  box.innerHTML = `<div class="page-head"><div><h2>系统联动</h2>
    <p>计划任务 / 加速源 · 通知渠道已并入「告警」模块</p></div>
    <div class="head-actions"><button class="btn ghost" id="syRefresh">${icon('i-refresh')} 刷新</button></div></div>
    ${sysHead(sysTab)}<div id="proSysBody"></div>`;
  $$('[data-sys]', box).forEach(b => b.onclick = () => { sysTab = b.dataset.sys; renderSystem(false); });
  $('#syRefresh').onclick = () => renderSystem(true);
  const body = $('#proSysBody', box);
  const D = sysData;
  if (sysTab === 'notify') {
    renderNotifyChannels(body);
  } else if (sysTab === 'tasks') {
    const t = D.tasks || {};
    let opts = { kinds: [], containers: [], stacks: [], images: [] };
    try { opts = await pro('task-options'); } catch (e) {}
    const kinds = (opts.kinds || []).length ? opts.kinds : [
      { id: 'restart-container', label: '重启指定容器', need: true },
      { id: 'check-updates', label: '检查容器镜像更新', need: false },
    ];
    const dl = (id, arr) => `<datalist id="${id}">${(arr || []).map(x => `<option value="${esc(x)}"></option>`).join('')}</datalist>`;
    const labelOf = k => (kinds.find(x => x.id === k) || {}).label || k;
    let editingId = '';
    const WD = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    const wdText = x => (x.weekdays && x.weekdays.length)
      ? x.weekdays.map(d => WD[(+d || 1) - 1] || ('周' + d)).join('、')
      : '周' + WD[(+x.weekday || 1) - 1];
    body.innerHTML = `<div class="panel glass"><div class="panel-head"><h3>${icon('i-clock')} 计划任务</h3>
      <span class="sub">${kinds.length} 种任务 · 每天 / 间隔 / 每周 · 立即执行 / 编辑 / 启停 / 历史</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>任务</th><th>类型</th><th>目标</th><th>调度</th><th>状态</th><th>上次结果</th><th>操作</th></tr></thead>
        <tbody>${(t.tasks || []).map(x => `<tr><td class="strong">${esc(x.name)}</td>
          <td class="small">${esc(labelOf(x.kind))}</td>
          <td class="small mono">${esc(x.target || '—')}</td>
          <td class="small">${esc(({ daily: '每天', interval: '按间隔', weekly: '每周' }[x.schedule || (x.interval_min ? 'interval' : 'daily')] || '每天') + ' ' + (x.schedule === 'interval' ? '每 ' + x.interval_min + ' 分' : (x.schedule === 'weekly' ? wdText(x) + ' ' : '') + (x.time || '')))}</td>
          <td><span class="badge ${x.enabled ? 'ok' : 'warn'}">${x.enabled ? '启用' : '停用'}</span></td>
          <td class="small">${esc(x.last_result || '—')}</td>
          <td><div class="tk-row-acts">
            <button class="btn ghost slim" data-tk-run="${esc(x.id)}">立即执行</button>
            <button class="btn ghost slim" data-tk-edit="${esc(x.id)}">编辑</button>
            <button class="btn ghost slim" data-tk-toggle="${esc(x.id)}">${x.enabled ? '停用' : '启用'}</button>
            <button class="btn ghost slim danger" data-tk-del="${esc(x.id)}">删除</button></div></td></tr>`).join('')
          || '<tr><td colspan="7"><div class="empty-state">还没有计划任务，用下面表单添加一个</div></td></tr>'}</tbody></table></div>

      <h4 id="tkFormTitle">新增任务</h4>
      <div class="tk-form">
        <label>任务名称</label><input class="input" id="tkName" data-autosize placeholder="例如：每天重启 Jellyfin">
        <label>任务类型</label><div class="tk-kind-wrap">
          <select class="input" id="tkKind">${kinds.map(k => `<option value="${esc(k.id)}">${esc(k.label)}</option>`).join('')}</select>
          <span class="sub tk-hint" id="tkHint"></span></div>
        <label>目标</label><div class="tk-target-wrap">
          <input class="input" id="tkTarget" data-autosize placeholder="容器名 / 栈名 / 镜像" list="tkDatalist">
          ${dl('tkDatalist', [...(opts.containers || []), ...(opts.stacks || []), ...(opts.images || [])])}</div>
        <label>调度</label><div class="tk-inline">
          <select class="input" id="tkSchedule"><option value="daily">每天固定时间</option>
            <option value="interval">每隔 N 分钟</option><option value="weekly">每周固定时间</option></select>
          <input type="time" data-du-time class="input tk-time" id="tkTime" value="03:30" autocomplete="off">
          <input type="number" class="input tk-num" id="tkInterval" min="1" max="10080" placeholder="分钟" style="display:none">
          <div class="tk-week-wrap" id="tkWeekWrap" style="display:none">
            <button type="button" class="input du-multi" id="tkWeekdaysBtn">周一</button>
            <div class="du-multi-pop hidden" id="tkWeekdaysPop">
              ${WD.map((w, i) => `<label class="du-multi-row"><input type="checkbox" value="${i + 1}" ${i === 0 ? 'checked' : ''}><span>${w}</span></label>`).join('')}
              <div class="du-multi-acts">
                <button type="button" class="chip" data-wd="work">工作日</button>
                <button type="button" class="chip" data-wd="weekend">周末</button>
                <button type="button" class="chip" data-wd="all">全选</button>
                <button type="button" class="chip" data-wd="none">清空</button>
              </div>
            </div>
          </div>
          <label class="checkline tk-enabled"><input type="checkbox" id="tkEnabled" checked> 启用</label></div>
        <div class="tk-buttons"><button class="btn primary slim" id="tkAdd">${icon('i-plus')} 保存任务</button>
          <button class="btn ghost slim" id="tkReset">清空表单</button></div>
      </div>
      <h4>执行历史</h4><div class="chip-list tk-history">${(t.history || []).slice(0, 20).map(h =>
        `<span class="m ${/失败|错误/.test(h.result || '') ? 'off' : ''}">${esc(h.time)} · ${esc(h.name)} · ${esc(h.result)}</span>`).join('') || '<span class="sub">暂无</span>'}</div>
    </div>`;

    // v3.9-r4：星期多选（统一磨砂浮层，不再用系统原生 select）
    const weekPop = () => $('#tkWeekdaysPop', body);
    const getWeekdays = () => Array.from(weekPop().querySelectorAll('input[type="checkbox"]:checked'))
      .map(c => +c.value).sort((a, b) => a - b);
    const paintWeekdays = () => {
      const a = getWeekdays(), btn = $('#tkWeekdaysBtn', body);
      if (btn) btn.textContent = a.length
        ? (a.length <= 3 ? a.map(d => WD[d - 1]).join('、') : '已选 ' + a.length + ' 天 · ' + a.map(d => WD[d - 1]).join('/'))
        : '请选择星期（可多选）';
    };
    const setWeekdays = (arr) => {
      const want = new Set((arr || []).map(Number));
      weekPop().querySelectorAll('input[type="checkbox"]').forEach(c => { c.checked = want.has(+c.value); });
      paintWeekdays();
    };
    const weekBtn = $('#tkWeekdaysBtn', body), weekWrap = $('#tkWeekWrap', body);
    weekBtn.onclick = e => { e.stopPropagation(); weekPop().classList.toggle('hidden'); };
    weekPop().querySelectorAll('input[type="checkbox"]').forEach(c => { c.onchange = paintWeekdays; });
    weekPop().querySelectorAll('[data-wd]').forEach(b => b.onclick = () => {
      const k = b.dataset.wd;
      const sets = { work: [1, 2, 3, 4, 5], weekend: [6, 7], all: [1, 2, 3, 4, 5, 6, 7], none: [] };
      setWeekdays(sets[k] || []);
    });
    const closeWeek = e => {
      if (!weekWrap.isConnected) { document.removeEventListener('mousedown', closeWeek); return; }
      if (!weekWrap.contains(e.target)) weekPop().classList.add('hidden');
    };
    document.addEventListener('mousedown', closeWeek);
    paintWeekdays();
    if (window.duTimePicker) window.duTimePicker.bind(body);

    // 输入框按内容长度自适应，不再留一大块空白
    const autoSize = (el) => {
      // 中文按 14px、ASCII 按 8px 估算真实字宽，宽度随实际输入内容变化
      const measure = (str) => {
        let w = 0;
        for (const ch of String(str || '')) w += ch.charCodeAt(0) > 255 ? 14 : 8;
        return w;
      };
      const need = Math.max(measure(el.value), 96) + 34;
      el.style.width = Math.max(160, Math.min(520, Math.round(need))) + 'px';
    };
    $$('[data-autosize]', body).forEach(el => { autoSize(el); el.oninput = () => autoSize(el); });
    const syncKind = () => {
      const k = $('#tkKind', body).value;
      const meta = kinds.find(x => x.id === k) || {};
      const target = $('#tkTarget', body);
      target.disabled = !meta.need;
      target.style.opacity = meta.need ? '1' : '.5';
      target.placeholder = meta.need ? (meta.hint || '容器名 / 栈名 / 镜像') : '该任务无需目标';
      if (!meta.need) target.value = '';
      $('#tkHint', body).textContent = meta.desc || '';
      autoSize(target);
    };
    $('#tkKind', body).onchange = syncKind;
    const syncSchedule = () => {
      const v = $('#tkSchedule', body).value;
      // du-sel 会把 <select> 包进 .du-sel 容器，必须一起隐藏，否则只剩一个空的「周一」
      const show = (el, on) => {
        if (!el) return;
        el.style.display = on ? '' : 'none';
        const wrap = el.closest('.du-sel');
        if (wrap) wrap.style.display = on ? '' : 'none';
      };
      show($('#tkTime', body), v !== 'interval');
      show($('#tkInterval', body), v === 'interval');
      show($('#tkWeekWrap', body), v === 'weekly');
    };
    $('#tkSchedule', body).onchange = syncSchedule;
    syncKind(); syncSchedule();

    const resetForm = () => {
      editingId = '';
      $('#tkFormTitle', body).textContent = '新增任务';
      ['tkName', 'tkTarget', 'tkInterval'].forEach(id => { const el = $('#' + id, body); if (el) el.value = ''; });
      $('#tkKind', body).value = kinds[0].id;
      $('#tkSchedule', body).value = 'daily';
      $('#tkTime', body).value = '03:30';
      setWeekdays([1]);
      $('#tkEnabled', body).checked = true;
      $$('[data-autosize]', body).forEach(autoSize);
      syncKind(); syncSchedule();
    };
    $('#tkReset', body).onclick = resetForm;
    $$('[data-tk-edit]', body).forEach(btn => btn.onclick = () => {
      const x = (t.tasks || []).find(y => y.id === btn.dataset.tkEdit);
      if (!x) return;
      editingId = x.id;
      $('#tkFormTitle', body).textContent = '编辑任务「' + x.name + '」';
      $('#tkName', body).value = x.name || '';
      $('#tkKind', body).value = x.kind;
      $('#tkTarget', body).value = x.target || '';
      $('#tkSchedule', body).value = x.schedule || (x.interval_min ? 'interval' : 'daily');
      $('#tkTime', body).value = x.time || '03:30';
      $('#tkInterval', body).value = x.interval_min || '';
      setWeekdays(x.weekdays || [x.weekday || 1]);
      $('#tkEnabled', body).checked = !!x.enabled;
      $$('[data-autosize]', body).forEach(autoSize);
      syncKind(); syncSchedule();
      $('#tkName', body).focus();
    });
    $('#tkAdd', body).onclick = async () => {
      const wds = getWeekdays();
      const sch = val(body, '#tkSchedule');
      if (sch === 'weekly' && !wds.length) { toast('每周执行时请至少选择一天', 'err'); return; }
      const payload = {
        id: editingId,
        name: val(body, '#tkName'), kind: val(body, '#tkKind'), target: val(body, '#tkTarget'),
        schedule: sch, time: val(body, '#tkTime'),
        interval_min: val(body, '#tkInterval'),
        weekday: wds[0] || 1, weekdays: wds.length ? wds : [1],
        enabled: $('#tkEnabled', body).checked,
      };
      try {
        const r = await pro('task-save', payload);
        toast(r.message || '任务已保存', 'ok');
        resetForm(); renderSystem(true);
      } catch (e) { if (!e.auth) toast(e.message || '保存失败', 'err'); }
    };
    $$('[data-tk-run]', body).forEach(b => b.onclick = async () => {
      try { const r = await pro('task-run', { id: b.dataset.tkRun }, { label: '正在执行计划任务' });
        toast(r.message, /失败/.test(r.message || '') ? 'err' : 'ok'); renderSystem(true); }
      catch (e) { if (!e.auth) toast(e.message, 'err'); }
    });
    $$('[data-tk-toggle]', body).forEach(b => b.onclick = async () => {
      const x = (t.tasks || []).find(y => y.id === b.dataset.tkToggle);
      if (!x) return;
      try { const r = await pro('task-toggle', { id: x.id, enabled: !x.enabled });
        toast(r.message, 'ok'); renderSystem(true); } catch (e) { if (!e.auth) toast(e.message, 'err'); }
    });
    $$('[data-tk-del]', body).forEach(b => b.onclick = async () => {
      try { const r = await pro('task-delete', { id: b.dataset.tkDel }); toast(r.message, 'ok'); renderSystem(true); }
      catch (e) { if (!e.auth) toast(e.message, 'err'); }
    });
  } else if (sysTab === 'accel') {
    const a = (D.accel || {}).config || {}, ip = (D.accel || {}).image_probe || null;
    const mirrors = a.gh || [], dks = a.docker || [], repos = a.repo || [];
    const cov = ip ? (ip.coverage || {}) : {};
    const badge = (m) => (m.builtin || m.preset)
      ? '<i class="badge info">内置</i>' : '<i class="badge info">自定义</i>';
    body.innerHTML = `<div class="panel glass"><div class="panel-head"><h3>${icon('i-network')} GitHub 加速源</h3>
      <span class="sub">${mirrors.length} 条 · 延迟 / 速度 / 状态</span></div>
      <p class="store-hint">${esc((D.accel || {}).hint || '')}</p>
      <div class="pro-actions"><button class="btn ghost slim" id="acProbe">${icon('i-refresh')} 立即测速</button>
        <button class="btn ghost slim" id="acRestore">${icon('i-rollback')} 恢复内置</button>
        <button class="btn primary slim" id="acBatch">${icon('i-plus')} 批量添加</button>
        <button class="btn ghost slim" id="acImageProbe">${icon('i-search')} 按容器检测</button>
        <label class="checkline"><input type="checkbox" id="acEnabled" ${a.enabled ? 'checked' : ''}> 启用加速</label>
        <label class="checkline"><input type="checkbox" id="acAuto" ${a.mode === 'auto' ? 'checked' : ''}> 智能·自动选最快</label>
        <label class="checkline"><input type="checkbox" id="acOnlyUntested"> 仅测未测</label></div>
      <div class="ac-filter"><input class="input" id="acSearch" placeholder="搜索名称或地址（本地过滤，不触发后台请求）" autocomplete="off" data-lpignore="true" data-form-type="other">
        <select class="input" id="acFilter"><option value="all">全部状态</option><option value="ok">正常</option>
          <option value="limited">限流</option><option value="fail">失败</option><option value="untested">未测</option></select>
        <span class="sub" id="acFilterCount"></span></div>
      ${ip ? `<div class="v50-ip-summary">${icon('i-check')} 上次「按容器检测」：${esc(ip.at || '')} · ${ip.images || 0} 个镜像，其中 ${ip.available || 0} 个至少有一个可用源
        <button class="btn ghost slim" id="acImageView">查看结果</button></div>` : ''}
      <div class="reg-list" id="acGhList">${mirrors.map(m => `<div class="reg-row" data-ac-row data-ac-status="${m.status === 'ok' ? 'ok' : ((m.status === 'limited' || m.status === 'rate') ? 'limited' : (m.status ? 'fail' : 'untested'))}" data-ac-text="${esc((m.label || m.key) + ' ' + m.url)}"><span class="reg-body">
        <b>${esc(m.label || m.key)} ${badge(m)}</b>
        <em class="mono">${esc(m.url)}</em></span>
        <span class="reg-meta"><i class="badge ${m.status === 'ok' ? 'ok' : (m.status === 'limited' || m.status === 'rate' ? 'warn' : (m.status ? 'danger' : 'info'))}">${esc(m.status || '未测')}</i>
          ${m.latency_ms ? esc(m.latency_ms + ' ms') : '—'} · ${m.speed_bps ? esc((m.speed_bps / 1048576).toFixed(1) + ' MB/s') : '—'}</span>
        <span class="reg-acts"><button class="btn ghost slim" data-ac-del="${esc(m.key)}">删除</button></span></div>`).join('')}</div>
      <h4>添加单个加速源</h4><div class="accel-add">
        <input class="input" id="acName" placeholder="名称" autocomplete="off" data-lpignore="true" data-form-type="other">
        <input class="input" id="acUrl" placeholder="https://gh-proxy.example.com/" autocomplete="off" data-lpignore="true" data-form-type="other">
        <button class="btn primary slim" id="acAdd">${icon('i-plus')} 添加</button></div>
      <h4 style="margin-top:16px">Docker 镜像源 · ${dks.length} 个</h4>
      <p class="store-hint">写进 NAS /etc/docker/daemon.json 的 registry-mirrors 后重启 Docker 才生效；建议只挑 3~5 个覆盖你镜像最多、延迟最低的。<b>覆盖数</b>来自「按容器检测」：这个源真的收录了你的几个镜像。</p>
      <div class="reg-list reg-compact" id="acDkList">${dks.map(m => `<div class="reg-row" data-ac-row data-ac-status="all" data-ac-text="${esc((m.label || m.key) + ' ' + m.url)}"><span class="reg-body">
        <b>${esc(m.label || m.key)} ${cov[m.key] ? `<i class="badge ok">收录 ${cov[m.key]} 个</i>` : '<i class="badge info">未测覆盖</i>'}</b><em class="mono">${esc(m.url)}</em></span>
        <span class="reg-acts"><button class="btn ghost slim" data-ac-copy="${esc(m.url)}">复制</button>
          <button class="btn ghost slim" data-ac-del="${esc(m.key)}">删除</button></span></div>`).join('')}</div>
      <h4 style="margin-top:16px">非 Docker Hub 仓库前缀 · ${repos.length} 条</h4>
      <p class="store-hint">registry-mirrors 只对 Docker Hub 生效；GHCR / Quay / gcr / K8s / MCR 必须改写镜像地址前缀，不能写进 daemon.json。</p>
      <div class="reg-list reg-compact" id="acRepoList">${repos.map(m => `<div class="reg-row" data-ac-row data-ac-status="all" data-ac-text="${esc((m.label || m.key) + ' ' + m.url)}"><span class="reg-body">
        <b>${esc(m.label || m.key)}</b><em class="mono">${esc(m.url)}</em>
        <em>${esc(m.note || '')}</em></span>
        <span class="reg-acts"><button class="btn ghost slim" data-ac-copy="${esc(m.url)}">复制</button>
          <button class="btn ghost slim" data-ac-del="${esc(m.key)}">删除</button></span></div>`).join('')}</div></div>`;

    /* v6.0-r2：「添加单个加速源」「Docker 镜像源」「非 Hub 仓库前缀」原本压在页面最底部，
       这里把它们提为「加速源」下的子选项，顶部一键切换，默认只显示 GitHub 加速源列表。
       用 DOM 搬迁而非重写模板，原有 id / 事件绑定 / 本地过滤全部保留。 */
    const acPanel = $('.panel.glass', body);
    if (acPanel && !$('.ac-subtabs', acPanel)) {
      const tabs = document.createElement('div');
      tabs.className = 'ac-subtabs';
      tabs.innerHTML = [
        ['gh', 'GitHub 加速源 · ' + mirrors.length + ' 条'],
        ['docker', 'Docker 镜像源 · ' + dks.length + ' 个'],
        ['repo', '非 Hub 仓库前缀 · ' + repos.length + ' 条'],
        ['add', '＋ 添加单个加速源']
      ].map(([k, label], i) =>
        `<button class="chip ${i === 0 ? 'on' : ''}" data-ac-tab="${k}">${esc(label)}</button>`).join('');
      const panes = document.createElement('div');
      panes.className = 'ac-panes';
      const heads = $$('h4', acPanel);
      [['add', '添加单个加速源'], ['docker', 'Docker 镜像源'], ['repo', '非 Docker Hub 仓库前缀']]
        .forEach(([key, text]) => {
          const h = heads.find(x => (x.textContent || '').indexOf(text) >= 0);
          if (!h) return;
          const pane = document.createElement('div');
          pane.dataset.acPane = key; pane.hidden = true;
          const nodes = []; let cur = h;
          while (cur && !(nodes.length && cur.nodeType === 1 && cur.tagName === 'H4')) {
            nodes.push(cur); cur = cur.nextSibling;
          }
          nodes.forEach(nd => pane.appendChild(nd));
          panes.appendChild(pane);
        });
      const ghList = $('#acGhList', body);
      let ghPane = null;
      if (ghList) {
        ghPane = document.createElement('div');
        ghPane.dataset.acPane = 'gh';
        ghList.parentNode.insertBefore(ghPane, ghList);
        ghPane.appendChild(ghList);
      }
      const anchorEl = $('.ac-filter', acPanel) || ghPane;
      acPanel.insertBefore(tabs, anchorEl);
      acPanel.insertBefore(panes, anchorEl);
      $$('[data-ac-tab]', tabs).forEach(btn => btn.onclick = () => {
        $$('[data-ac-tab]', tabs).forEach(x => x.classList.toggle('on', x === btn));
        $$('[data-ac-pane]', acPanel).forEach(p => { p.hidden = p.dataset.acPane !== btn.dataset.acTab; });
      });
    }

    // v5.0：本地过滤，只切 hidden，不重建 DOM（避免输入框被自动刷新冲掉）
    const applyFilter = () => {
      const q = ($('#acSearch', body).value || '').trim().toLowerCase();
      const f = $('#acFilter', body).value || 'all';
      let shown = 0;
      $$('[data-ac-row]', body).forEach(row => {
        const txt = String(row.dataset.acText || '').toLowerCase();
        const st = row.dataset.acStatus || 'all';
        const qOK = !q || txt.includes(q);
        const fOK = f === 'all' || st === f || (f === 'fail' && st !== 'ok' && st !== 'limited' && st !== 'untested');
        const vis = qOK && fOK;
        row.classList.toggle('ac-hidden', !vis);
        if (vis) shown++;
      });
      $('#acFilterCount', body).textContent = `显示 ${shown} / ${$$('[data-ac-row]', body).length} 条`;
    };
    $('#acSearch', body).addEventListener('input', applyFilter);
    $('#acFilter', body).onchange = applyFilter;
    applyFilter();

    $('#acProbe', body).onclick = async () => {
      const untested = $$('[data-ac-row]', body).filter(r => r.dataset.acStatus === 'untested').length;
      const payload = $('#acOnlyUntested', body).checked
        ? { keys: mirrors.filter(m => !m.status).map(m => m.key) } : {};
      if (payload.keys && !payload.keys.length) { toast('没有未测的源了', 'info'); return; }
      const r = await pro('accel-probe', payload, { label: '正在测速加速源' });
      toast(r.message, 'ok'); renderSystem(true); };
    $('#acRestore', body).onclick = async () => { const r = await pro('accel-restore', {}); toast(r.message, 'ok'); renderSystem(true); };
    $('#acEnabled', body).onchange = async (e) => { await pro('accel-save', { enabled: e.target.checked }); };
    $('#acAuto', body).onchange = async (e) => { await pro('accel-save', { mode: e.target.checked ? 'auto' : 'manual' }); };
    $('#acAdd', body).onclick = async () => {
      try { const r = await pro('accel-add', { name: val(body, '#acName'), url: val(body, '#acUrl') });
        toast(r.message, 'ok'); renderSystem(true); } catch (e) { if (!e.auth) toast(e.message, 'err'); } };
    $$('[data-ac-del]', body).forEach(b => b.onclick = async () => {
      const r = await pro('accel-delete', { key: b.dataset.acDel }); toast(r.message, 'ok'); renderSystem(true); });
    $$('[data-ac-copy]', body).forEach(b => b.onclick = () => {
      try { navigator.clipboard.writeText(b.dataset.acCopy); toast('已复制地址', 'ok'); }
      catch (e) { toast('复制失败，请手动选中', 'err'); } });

    // ---- v5.0 按容器检测：哪个源真的有我的镜像 ----
    const openProbeResult = (result) => {
      const mask = document.createElement('div');
      mask.className = 'du-mask'; mask.id = 'ipMask';
      const imgs = result.images || [];
      mask.innerHTML = `<div class="du-modal v50-ip-modal"><div class="du-modal-head">
        <h3>${icon('i-search')} 按容器检测 · 哪个源真的有我的镜像</h3>
        <button class="icon-btn" id="ipClose">×</button></div>
        <div class="du-modal-body">
          <p class="store-hint">${esc(result.message || '')} · 耗时 ${esc(result.elapsed_s || '0')} 秒 · ${esc(result.at || '')}</p>
          <div class="tbl-wrap"><table class="tbl"><thead><tr><th>容器</th><th>镜像</th><th>可用源</th><th>最快源</th><th>延迟</th><th>拉取命令</th></tr></thead>
            <tbody>${imgs.map(x => `<tr><td class="small">${esc((x.containers || []).slice(0, 2).join('、') || '—')}</td>
              <td class="mono small">${esc(x.ref)}</td>
              <td><i class="badge ${x.available ? 'ok' : 'danger'}">${x.available} / ${(x.results || []).length}</i></td>
              <td class="small">${esc(x.best || '全部源都没有')}</td>
              <td class="small">${x.best_ms ? esc(x.best_ms + ' ms') : '—'}</td>
              <td>${x.pull_cmd ? `<button class="btn ghost slim" data-ip-copy="${esc(x.pull_cmd)}">复制命令</button>`
                : '<span class="sub">冷门镜像，建议换源或同步到自有仓库</span>'}</td></tr>`).join('')}</tbody></table></div>
          <h4>推荐 daemon.json（只收录覆盖你镜像最多的 ${((result.recommended || {}).registry_mirrors || []).length} 个源）</h4>
          <pre class="v50-code" id="ipDaemon">${esc(JSON.stringify(result.recommended || {}, null, 2))}</pre>
          <div class="pro-inline"><button class="btn ghost slim" id="ipCopyDaemon">复制 daemon.json 片段</button>
            <span class="sub">改完需 SSH 到 NAS 执行 systemctl restart docker 才生效</span></div>
        </div><div class="du-modal-foot"><button class="btn primary slim" id="ipDone">完成</button></div></div>`;
      document.body.appendChild(mask);
      const close = () => mask.remove();
      mask.querySelector('#ipClose').onclick = close;
      mask.querySelector('#ipDone').onclick = close;
      mask.addEventListener('click', e => { if (e.target === mask) close(); });
      $$('[data-ip-copy]', mask).forEach(b => b.onclick = () => {
        try { navigator.clipboard.writeText(b.dataset.ipCopy); toast('已复制拉取命令', 'ok'); }
        catch (e) { toast('复制失败', 'err'); } });
      mask.querySelector('#ipCopyDaemon').onclick = () => {
        try { navigator.clipboard.writeText(mask.querySelector('#ipDaemon').textContent);
          toast('已复制 daemon.json 片段', 'ok'); } catch (e) { toast('复制失败', 'err'); } };
    };
    $('#acImageProbe', body).onclick = async () => {
      const btn = $('#acImageProbe', body);
      btn.disabled = true; btn.textContent = '正在探测…';
      try {
        const r = await pro('image-probe', { mirrors: 10, images: 20 },
          { label: '正在按容器检测哪些镜像源真的有你的镜像' });
        toast(r.message, 'ok');
        openProbeResult(r.result || {});
        btn.disabled = false; btn.innerHTML = icon('i-search') + ' 按容器检测';
        renderSystem(true);
      } catch (e) { btn.disabled = false; btn.innerHTML = icon('i-search') + ' 按容器检测';
        if (!e.auth) toast(e.message || '探测失败', 'err'); }
    };
    if ($('#acImageView', body)) $('#acImageView', body).onclick = async () => {
      try { const r = await pro('image-probe-cache', {});
        if (r.result && r.result.images) openProbeResult(r.result);
        else toast('还没有探测结果', 'info'); } catch (e) { toast(e.message, 'err'); }
    };

    // v3.9-r7：批量添加，一行一个自动按换行区分
    $('#acBatch', body).onclick = () => {
      const mask = document.createElement('div');
      mask.className = 'du-mask'; mask.id = 'acbMask';
      mask.innerHTML = `<div class="du-modal accel-batch-modal"><div class="du-modal-head">
        <h3>${icon('i-plus')} 批量添加加速源</h3><button class="icon-btn" id="acbClose">×</button></div>
        <div class="du-modal-body">
          <p class="store-hint">一行一个，自动按换行区分；支持 <code>名称|地址</code>、<code>名称 地址</code>、纯 URL；
            <code>#</code> 开头为注释，空行忽略；已存在的地址自动跳过。粘贴后可直接提交。</p>
          <div class="acb-target"><label>导入到</label>
            <select class="input" id="acbTarget">
              <option value="gh">GitHub 加速线路（索引 / 图标 / README）</option>
              <option value="docker">Docker 镜像源（写进 daemon.json 的 registry-mirrors）</option>
              <option value="repo">非 Docker Hub 仓库前缀（改写镜像地址）</option>
            </select></div>
          <textarea class="input" id="acbText" rows="9" spellcheck="false"
            placeholder="GH-Proxy|https://gh-proxy.com/&#10;GHFast|https://ghfast.top/&#10;https://gh.llkk.cc/&#10;# 这一行是注释"></textarea>
          <div class="acb-count" id="acbCount">识别到 0 行 · 0 个有效地址</div>
          <div class="acb-result hidden" id="acbResult"></div>
        </div>
        <div class="du-modal-foot"><button class="btn ghost slim" id="acbCancel">取消</button>
          <button class="btn primary slim" id="acbSubmit">${icon('i-check')} 批量导入</button></div></div>`;
      document.body.appendChild(mask);
      const ta = mask.querySelector('#acbText');
      const counter = mask.querySelector('#acbCount');
      const countLines = () => {
        const lines = (ta.value || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean);
        const valid = lines.filter(x => !x.startsWith('#')).length;
        counter.textContent = `识别到 ${lines.length} 行 · ${valid} 个有效地址`;
      };
      ta.addEventListener('input', countLines);
      countLines();
      const close = () => mask.remove();
      mask.querySelector('#acbClose').onclick = close;
      mask.querySelector('#acbCancel').onclick = close;
      mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
      mask.querySelector('#acbSubmit').onclick = async () => {
        const text = ta.value || '';
        if (!text.trim()) { toast('请先粘贴加速源清单', 'err'); return; }
        const btn = mask.querySelector('#acbSubmit');
        btn.disabled = true; btn.textContent = '正在导入…';
        try {
          const r = await pro('accel-batch', { text,
            target: mask.querySelector('#acbTarget').value });
          const res = r.result || {};
          const box = mask.querySelector('#acbResult');
          box.classList.remove('hidden');
          box.innerHTML = `<div class="acb-summary">${esc(r.message || '导入完成')}</div>` +
            (res.lines || []).map(x => `<div class="acb-line ${x.status}">
              <span class="acb-tag">${x.status === 'added' ? '新增' : (x.status === 'skipped' ? '跳过' : '失败')}</span>
              <span class="mono">${esc(String(x.text).slice(0, 90))}</span>
              <span>${esc(x.message || '')}</span></div>`).join('');
          toast(r.message, 'ok');
          btn.textContent = '完成';
          if (res.added) renderSystem(true);
        } catch (e) { toast(e.message || '导入失败', 'err'); btn.disabled = false; btn.textContent = '批量导入'; }
      };
      ta.focus();
    };
  }
}
/* ==================== 主渲染 ==================== */
async function render(v, force) {
  // v3.1：Compose 栈 / 应用模板两个模块已移除；v3.7 只保留系统联动。
  curView = 'system';
  if (!P) await loadState();
  await renderSystem(!!force);
  if (window.duRenderTrendStrip) window.duRenderTrendStrip(curView);
}
window.duOpenDirPicker = openDirPicker;   // v3.3：备份页也要能浏览 fnOS 目录
window.duProRender = render;
window.duProOverview = renderOverviewExtra;
window.duProContainers = renderContainerExtra;
window.duProDetail = renderDetail;
window.duProEnhanceLogs = enhanceLogs;
window.duProCreate = openCreateContainer;
})();
