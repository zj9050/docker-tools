/* DockerUpdate v7.0.2 · 文件软链接（飞牛 A 文件 → 容器内改名成 B 文件）
   --------------------------------------------------------------------
   内存 rows 是唯一数据源；弹窗打开 / 正在输入时不重绘，避免 5 秒轮询冲掉输入。
   新增 / 编辑 / 保存 / 立即执行 / 删除 / 一键挂载 / 周期设置，全部走 /api/pro。 */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const api = window.duApi;
  const say = (m, t) => (window.duToast ? window.duToast(m, t || 'ok') : alert(m));

  let rows = [], containers = [], periods = [], modes = [], history = [], stats = {};
  let storeRoot = '/vol1/1000/docker', dirty = false, inited = false, busy = false;
  let editIdx = -1, browsePath = '', browseData = null, browseField = '';

  async function call(kind, body, opts) {
    return api('/api/pro', Object.assign(
      { method: 'POST', body: { kind: kind, body: body || {} } }, opts || {}));
  }
  function markDirty(on) {
    dirty = !!on;
    const el = $('#symDirty');
    if (el) el.textContent = dirty ? '有未保存的规则修改（点「保存规则」生效）' : '已与后端同步';
  }
  const periodLabel = (id) => (periods.find(x => x.id === id) || {}).label || id || '不自动';
  const modeLabel = (id) => (modes.find(x => x.id === id) || {}).label || id || '软链接';
  const stateLabel = (s) => ({ ok: '已生效', unchanged: '无需改动', error: '失败', skipped: '已跳过' })[s] || '未执行';

  /* ---------- 载入 ---------- */
  async function loadAll(force) {
    const box = $('#symTable');
    if (box && !inited) box.innerHTML = '<div class="empty-state glass">正在读取文件软链接规则…</div>';
    let d;
    try { d = await call('symlinks-list', {}, { progress: false }); }
    catch (e) { if (box) box.innerHTML = `<div class="empty-state glass">读取失败：${esc(e.message || e)}</div>`; return; }
    if (!d || d.ok === false) { if (box) box.innerHTML = '<div class="empty-state glass">读取失败</div>'; return; }
    rows = d.rules || [];
    containers = d.containers || [];
    periods = d.periods || [];
    modes = d.modes || [];
    history = d.history || [];
    stats = d.stats || {};
    storeRoot = d.store_root || storeRoot;
    inited = true;
    paint();
  }

  /* ---------- 渲染 ---------- */
  function paint() {
    const modal = $('#genericModal');
    if (modal && !modal.classList.contains('hidden')) return;   // 弹窗打开不重绘
    if (document.activeElement && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)
        && $('#viewContainerSymlinks') && $('#viewContainerSymlinks').contains(document.activeElement)) return;
    paintKpi(); paintTable(); paintHistory();
  }

  function paintKpi() {
    const box = $('#symKpi');
    if (!box) return;
    const st = (label, val, sub) => `<div class="stat"><div class="k-label">${esc(label)}</div><div class="k-num">${esc(val)}</div><div class="k-sub">${esc(sub || '')}</div></div>`;
    box.innerHTML = st('规则总数', stats.total != null ? stats.total : rows.length, '按容器 / 目录分组')
      + st('已启用', stats.enabled != null ? stats.enabled : rows.filter(r => r.enabled !== false).length, '停用不参与执行')
      + st('周期自动', stats.auto || 0, '后台按到期时间重建')
      + st('上次异常', stats.errors || 0, '展开历史可看原因');
  }

  function paintTable() {
    const box = $('#symTable');
    if (!box) return;
    if (!rows.length) {
      box.innerHTML = `<div class="panel glass"><div class="empty-state">还没有规则。点「新增规则」：填飞牛上的 A 文件、链接存放目录、容器认得的 B 文件名，再选执行周期。</div></div>`;
      return;
    }
    box.innerHTML = `<div class="panel glass"><div class="panel-head"><h3><svg class="ic"><use href="#i-link"/></svg> 规则列表</h3>
      <span class="sub">${rows.length} 条 · 路径与文件名均可自定义</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th style="width:62px">启用</th><th>容器</th><th>飞牛源文件 A</th><th>链接存放目录</th>
        <th>文件名 B</th><th>方式</th><th>执行周期</th><th>状态</th><th>上次执行</th><th style="width:170px">操作</th>
      </tr></thead><tbody>${rows.map((r, i) => {
        const cls = r.last_state === 'error' ? 'bad' : (r.last_state === 'ok' || r.last_state === 'unchanged') ? 'ok' : 'gray';
        return `<tr><td><label class="switch sm"><input type="checkbox" data-sym-en="${i}" ${r.enabled !== false ? 'checked' : ''} aria-label="启用规则"><span class="track"></span></label></td>
        <td class="strong">${esc(r.name || '—')}</td>
        <td class="mono small">${esc(r.source)}</td>
        <td class="mono small">${esc(r.link_dir)}</td>
        <td class="strong">${esc(r.link_name)}</td>
        <td>${esc(modeLabel(r.mode))}</td>
        <td>${esc(periodLabel(r.period))}</td>
        <td><span class="badge ${cls}" title="${esc(r.last_message || '')}">${esc(stateLabel(r.last_state))}</span></td>
        <td class="small">${esc(String(r.last_run || '—').slice(5))}</td>
        <td><button class="mini-act" data-sym-act="run" data-i="${i}">执行</button>
            <button class="mini-act" data-sym-act="edit" data-i="${i}">编辑</button>
            <button class="mini-act danger" data-sym-act="del" data-i="${i}">删除</button></td></tr>`;
      }).join('')}</tbody></table></div>
      <p class="tiny" style="margin:10px 0 0">${rows.filter(r => r.enabled !== false).length} 条启用 · 下次自动执行时间见各行状态；「执行」只生成 / 校验 B 文件，不重建容器。</p></div>`;
  }

  function paintHistory() {
    const box = $('#symHistory');
    if (!box || !history.length) { if (box) box.innerHTML = ''; return; }
    box.innerHTML = `<div class="panel glass" style="margin-top:14px"><div class="panel-head"><h3><svg class="ic"><use href="#i-list"/></svg> 执行历史</h3>
      <span class="sub">最近 ${history.length} 条</span></div>
      <div class="tbl-wrap" style="max-height:260px;overflow-y:auto"><table class="tbl"><thead><tr>
      <th style="width:150px">时间</th><th style="width:90px">结果</th><th>链接</th><th>说明</th></tr></thead><tbody>
      ${history.map(h => `<tr><td class="mono small">${esc(h.time)}</td>
        <td><span class="badge ${h.state === 'error' ? 'bad' : 'ok'}">${esc(stateLabel(h.state))}</span></td>
        <td class="mono small">${esc(h.link)}</td><td class="small">${esc(h.message)}</td></tr>`).join('')}
      </tbody></table></div></div>`;
  }

  function render(force) {
    if (!inited || force) return loadAll(force);
    paint();
  }

  /* ---------- 目录浏览（内联在编辑弹窗里，不覆盖表单） ---------- */
  async function loadDirList(path) {
    let d;
    try { d = await call('symlinks-dirs', { path: path }, { progress: false }); }
    catch (e) { say(e.message || '读取目录失败', 'err'); return null; }
    browseData = d; browsePath = d.path || path;
    return d;
  }
  function renderDirList(d) {
    const box = $('#symDirList');
    if (!box) return;
    box.innerHTML = (d.dirs || []).length
      ? d.dirs.map(x => `<button type="button" class="chip" data-sym-dir="${esc(x.path)}">${esc(x.name)}</button>`).join('')
      : '<span class="sub">此目录下没有子目录，可直接用当前目录</span>';
    const cur = $('#symDirCur');
    if (cur) cur.textContent = d.path || browsePath;
    const inp = $('#symFLinkDir');
    if (inp) inp.value = d.path || browsePath;
  }
  async function initBrowse(start) {
    const d = await loadDirList(start || storeRoot);
    if (d) renderDirList(d);
  }
  /* ---------- 新增 / 编辑 ---------- */
  function openEdit(idx) {
    editIdx = idx;
    const r = idx >= 0 ? rows[idx] : {
      name: containers.length ? containers[0].name : '', source: '', link_dir: storeRoot,
      link_name: '', container_path: '', mode: 'link', period: 'manual', enabled: true, note: ''
    };
    const opts = (arr, cur) => arr.map(x => `<option value="${esc(x.id || x.name)}" ${String(x.id || x.name) === String(cur || '') ? 'selected' : ''}>${esc(x.label || x.name)}</option>`).join('');
    const html = `
      <div class="form-grid">
        <div class="field"><label>关联容器（可选）</label><select class="input" id="symFName">
          <option value="">不关联（只管理飞牛侧文件）</option>
          ${containers.map(c => `<option value="${esc(c.name)}" ${c.name === r.name ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
        </select><small>用于「一键挂载进容器」；留空也能建链接。</small></div>
        <div class="field"><label>容器内目标目录（可选）</label><input class="input mono" id="symFCpath" value="${esc(r.container_path || '')}" placeholder="/config"></div>
      </div>
      <div class="field"><label>飞牛源文件 A（绝对路径）</label>
        <input class="input mono" id="symFSource" value="${esc(r.source || '')}" placeholder="${esc(storeRoot)}/cert/fullchain.pem">
        <small>必须是已存在的文件；A 内容更新后软链接自动跟随，同步副本按周期重拷。</small></div>
      <div class="field"><label>链接存放目录（飞牛侧）</label>
        <input class="input mono" id="symFLinkDir" value="${esc(r.link_dir || storeRoot)}">
        <div class="sym-browse"><div class="sym-browse-head"><span class="sub">当前目录 <b class="mono" id="symDirCur">${esc(r.link_dir || storeRoot)}</b></span>
          <button class="btn ghost slim" id="symFBrowse" type="button">浏览子目录</button>
          <button class="btn ghost slim" id="symDirUp" type="button">上一级</button></div>
          <div class="chip-list" id="symDirList"><span class="sub">点「浏览子目录」展开</span></div></div>
        <small>软链接方式会自动把该目录设为 A 所在目录；同步副本可任意选可写根下的目录。</small></div>
      <div class="form-grid">
        <div class="field"><label>容器内识别的文件名 B</label>
          <input class="input mono" id="symFLinkName" value="${esc(r.link_name || '')}" placeholder="cert.crt">
          <small>只填文件名，不含路径；容器只认这个名字时就用它。</small></div>
        <div class="field"><label>落地方式</label><select class="input" id="symFMode">${opts(modes, r.mode)}</select></div>
      </div>
      <div class="form-grid">
        <div class="field"><label>执行周期</label><select class="input" id="symFPeriod">${opts(periods, r.period)}</select>
          <small>到期后台自动校验 / 重建；「不自动」只手动执行。</small></div>
        <div class="field"><label>备注</label><input class="input" id="symFNote" value="${esc(r.note || '')}" placeholder="可留空"></div>
      </div>
      <label class="switch"><input type="checkbox" id="symFEnabled" ${r.enabled !== false ? 'checked' : ''}><span class="track"></span><span>启用这条规则</span></label>
      <label class="checkline" style="margin-top:8px"><input type="checkbox" id="symFSameDir">
        <span>自动把「链接存放目录」设为 A 所在目录（软链接方式推荐）</span></label>`;
    window.duOpenModal(idx >= 0 ? '编辑文件软链接规则' : '新增文件软链接规则',
      'A 文件 → 容器内 B 文件名 · 路径与文件名均可自定义', html,
      () => {
        const src = ($('#symFSource').value || '').trim();
        const ldir = ($('#symFLinkDir').value || '').trim();
        const lname = ($('#symFLinkName').value || '').trim();
        if (!src || !ldir || !lname) { say('源文件 A、链接目录、文件名 B 都要填', 'err'); return false; }
        if ($('#symFSameDir') && $('#symFSameDir').checked) {
          const p = src.replace(/\/[^/]*$/, '');
          $('#symFLinkDir').value = p || storeRoot;
        }
        const item = {
          id: idx >= 0 ? rows[idx].id : undefined,
          name: ($('#symFName').value || '').trim(),
          source: src, link_dir: ($('#symFLinkDir').value || '').trim(), link_name: lname,
          container_path: ($('#symFCpath').value || '').trim(),
          mode: $('#symFMode').value, period: $('#symFPeriod').value,
          enabled: !!$('#symFEnabled').checked, note: ($('#symFNote').value || '').trim()
        };
        if (idx >= 0) rows[idx] = Object.assign({}, rows[idx], item); else rows.push(item);
        markDirty(true); paintTable(); paintKpi();
        return true;
      });
    const br = $('#symFBrowse'), up = $('#symDirUp'), listBox = $('#symDirList');
    if (br) br.onclick = () => initBrowse(($('#symFLinkDir').value || storeRoot).trim());
    if (up) up.onclick = () => {
      const cur = (browseData && browseData.parent) || storeRoot;
      initBrowse(cur);
    };
    if (listBox) listBox.addEventListener('click', (e) => {
      const b = e.target.closest('[data-sym-dir]');
      if (!b) return;
      initBrowse(b.dataset.symDir);
    });
  }

  /* ---------- 操作 ---------- */
  async function saveRows() {
    if (busy) return;
    busy = true;
    try {
      const d = await call('symlinks-save', { rules: rows }, { progress: false });
      say(d.message || '已保存', 'ok');
      markDirty(false);
      await loadAll(true);
    } catch (e) { say(e.message || '保存失败', 'err'); }
    finally { busy = false; }
  }

  async function runNow(ids) {
    if (busy) return;
    if (dirty) { say('请先保存规则，再执行', 'err'); return; }
    busy = true;
    try {
      const d = await call('symlinks-run', { ids: ids || [], force: false }, { progress: false });
      say(d.message || '执行完成', d.ok === false ? 'err' : 'ok');
      await loadAll(true);
    } catch (e) { say(e.message || '执行失败', 'err'); }
    finally { busy = false; }
  }

  async function delRow(i) {
    const r = rows[i];
    if (!r) return;
    if (!r.id) { rows.splice(i, 1); markDirty(true); paintTable(); return; }
    if (!window.duConfirm2('sym-del:' + r.id, '再次点击确认删除这条规则（已生成的 B 文件不会被删除）')) return;
    try {
      const d = await call('symlinks-delete', { id: r.id }, { progress: false });
      say(d.message || '已删除', 'ok');
      await loadAll(true);
    } catch (e) { say(e.message || '删除失败', 'err'); }
  }

  async function mountInto() {
    if (dirty) { say('请先保存规则', 'err'); return; }
    const named = rows.filter(r => r.enabled !== false && r.name && r.container_path);
    if (!named.length) { say('请先在规则里填「关联容器」和「容器内目标目录」', 'err'); return; }
    const names = [...new Set(named.map(r => r.name))];
    if (names.length !== 1) { say('「一键挂载」一次只处理一个容器，请分批执行', 'err'); return; }
    const dirs = [...new Set(named.map(r => r.link_dir))];
    if (dirs.length !== 1) { say('同一容器的规则必须使用同一个链接存放目录', 'err'); return; }
    const cpaths = [...new Set(named.map(r => r.container_path))];
    if (cpaths.length !== 1) { say('同一容器的规则必须使用同一个容器内目标目录', 'err'); return; }
    window.duOpenModal('一键挂载进容器', `${esc(names[0])} · ${esc(dirs[0])} → ${esc(cpaths[0])}`,
      `<p class="sub">将先生成全部 B 文件，再把该目录挂进容器并重建。重建会保留镜像、端口、环境变量、网络、标签与重启策略，并先做版本备份；失败会自动还原旧容器。</p>
       <div class="notice small-notice"><svg class="ic"><use href="#i-info"/></svg><span>容器正在运行时会短暂中断服务（通常几秒）。也可以只用「目录映射」页手动核对计划后再应用。</span></div>`,
      async () => {
        if (!window.duConfirm2('sym-mount:' + names[0], '再次点击确定：将重建容器并短暂中断服务')) return false;
        let r;
        try {
          r = await call('symlinks-apply', {
            name: names[0], container_path: cpaths[0], confirm: true
          }, { progress: false });
        } catch (e) { say(e.message || '提交失败', 'err'); return false; }
        if (!r || r.ok === false) { say((r && r.error) || '提交失败', 'err'); return false; }
        if (r.async && r.job_id) {
          const ph = window.duProgress ? window.duProgress.begin(
            '正在把链接目录挂载进容器',
            ['校验规则与路径', '生成 B 文件', '合并目录映射',
             '重建容器并保留全部原配置', '核对挂载结果'], true) : null;
          const fin = await window.duWatchJob(r.job_id, ph);
          say(fin.message || (fin.ok ? '已挂载' : '挂载未生效'), fin.ok ? 'ok' : 'err');
        } else say(r.message || '已提交', 'ok');
        await loadAll(true);
        return true;
      });
  }

  /* ---------- 事件绑定（委托，重绘不丢） ---------- */
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-sym-act]');
    if (!b) return;
    const i = Number(b.dataset.i);
    if (b.dataset.symAct === 'edit') openEdit(i);
    else if (b.dataset.symAct === 'run') {
      const r = rows[i];
      if (!r) return;
      if (!r.id) { say('请先保存规则，再执行', 'err'); return; }
      runNow([r.id]);
    } else if (b.dataset.symAct === 'del') delRow(i);
  });
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t && t.matches('[data-sym-en]')) {
      const i = Number(t.dataset.symEn);
      if (rows[i]) { rows[i].enabled = t.checked; markDirty(true); }
    }
  });
  function bindBar() {
    const map = {
      btnSymAdd: () => openEdit(-1),
      btnSymSave: saveRows,
      btnSymRunAll: () => runNow([]),
      btnSymMount: mountInto,
      btnSymRefresh: () => { markDirty(false); loadAll(true); }
    };
    Object.keys(map).forEach(id => {
      const b = $('#' + id);
      if (b && !b.dataset.duBound) { b.dataset.duBound = '1'; b.onclick = map[id]; }
    });
  }
  document.addEventListener('DOMContentLoaded', bindBar);

  window.duSymlinks = {
    render: render,
    bindBar: bindBar,
    get rows() { return rows; }
  };
})();
