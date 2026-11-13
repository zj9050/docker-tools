/* DockerUpdate v6.0-r24 · 容器目录映射（飞牛文件夹 → 容器内自定义路径）
   --------------------------------------------------------------------
   两端路径都可自定义；保存配置不碰容器，应用才重建。
   重要：整个页面以内存里的 rows 为唯一数据源，输入框 oninput 直接改 rows，
   5 秒自动轮询不会重绘 DOM、不会冲掉正在输入的内容（r23 刚修过的同类 BUG）。 */
(function () {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  let cur = '', rows = [], containers = [], items = [], statusData = null, planData = null;
  let dirty = false, inited = false, needReload = false, busy = false, lastLoad = 0;
  let browseTarget = -1, browsePath = '', browseData = null;
  let tplRows = [], polData = {}, knownCount = 0, inheritedCount = 0, syncMsg = '';

  async function call(kind, body, opts) {
    return api('/api/pro', Object.assign(
      { method: 'POST', body: { kind: kind, body: body || {} } }, opts || {}));
  }
  const say = (m, t) => (window.duToast ? window.duToast(m, t || 'ok') : toast(m, t || 'ok'));

  /* ---------- 载入 ---------- */
  async function loadAll(force) {
    const box = $('#mountsTable');
    if (box && !inited) box.innerHTML = '<div class="empty-state glass">正在读取容器与映射配置…</div>';
    let d;
    try {
      d = await call('mounts-list', {}, { progress: false });
    } catch (e) { say(e.message || '读取映射配置失败', 'err'); return; }
    if (!d || d.ok === false) { say((d && d.error) || '读取映射配置失败', 'err'); return; }
    containers = d.containers || [];
    items = d.items || [];
    tplRows = (d.template && d.template.mappings) || [];
    polData = d.policy || {};
    knownCount = d.known_count || 0;
    inheritedCount = d.inherited_count || 0;
    if (!cur || !containers.some(c => c.name === cur)) {
      cur = (containers[0] && containers[0].name) || '';
    }
    inited = true;
    lastLoad = Date.now();
    needReload = false;
    paintSelect();
    await loadStatus(force);
  }

  async function loadStatus(force) {
    if (!cur) { paintAll(); return; }
    let d;
    try {
      d = await call('mounts-status', { name: cur }, { progress: false });
    } catch (e) { say(e.message || '读取容器挂载失败', 'err'); return; }
    statusData = (d && d.ok !== false) ? d : null;
    if (statusData && (force || !dirty)) {
      rows = (statusData.mappings || []).map(r => ({
        id: r.id, host: r.host, container: r.container, mode: r.mode,
        enabled: r.enabled !== false, note: r.note || '',
        state: r.state, state_label: r.state_label, current_bind: r.current_bind || ''
      }));
      dirty = false;
    }
    paintAll();
  }

  /* ---------- 渲染 ---------- */
  function paintSelect() {
    const sel = $('#mountsContainer');
    if (!sel) return;
    sel.innerHTML = containers.length
      ? containers.map(c => `<option value="${esc(c.name)}"${c.name === cur ? ' selected' : ''}>${esc(c.name)}${c.inherited ? ' · 模板' : ''}${c.running ? ' · 运行中' : ' · 已停止'}</option>`).join('')
      : '<option value="">（没有可用容器）</option>';
    if (cur) sel.value = cur;
    // app.js 的 duSelect 会把 select 包成自定义浮层，且 enhance() 对已增强过的
    // select 直接 return，不会重读 options。动态替换 options 后若不主动同步，
    // 触发器会一直显示旧占位「请选择」、浮层里也没有新容器。
    if (window.duEnhanceSelects) {
      try { window.duEnhanceSelects(sel.parentElement || document); } catch (e) { /* 忽略 */ }
    }
    syncSelectEnhance(sel);
  }

  function syncSelectEnhance(sel) {
    const wrap = sel.closest('.du-sel');
    if (!wrap) return;
    const opt = sel.options[sel.selectedIndex];
    const full = opt ? (opt.textContent || sel.value) : (sel.value || '请选择');
    const label = wrap.querySelector('.du-sel-text');
    if (label) { label.textContent = full; label.title = full; }
    const trig = wrap.querySelector('.du-sel-btn');
    if (trig) trig.title = full;
    const pop = wrap.querySelector('.du-sel-pop') || document.querySelector('.du-sel-pop.on');
    if (!pop) return;
    const want = Array.from(sel.options).filter(o => !o.disabled);
    let nodes = Array.from(pop.querySelectorAll('.du-sel-opt'));
    if (nodes.length !== want.length) {
      pop.innerHTML = want.map(() =>
        '<button type="button" class="du-sel-opt"><span class="du-sel-tick"></span>'
        + '<span class="du-sel-label"></span></button>').join('');
      nodes = Array.from(pop.querySelectorAll('.du-sel-opt'));
      nodes.forEach(b => b.onclick = (e) => {
        e.stopPropagation();
        sel.value = b.dataset.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        syncSelectEnhance(sel);
        pop.classList.remove('on');
        wrap.classList.remove('open');
        if (pop.parentNode === document.body) wrap.appendChild(pop);
      });
    }
    nodes.forEach((b, i) => {
      if (!want[i]) return;
      b.dataset.value = want[i].value;
      const t = b.querySelector('.du-sel-label');
      if (t) t.textContent = want[i].textContent || want[i].value;
      b.classList.toggle('active', want[i].value === sel.value);
    });
  }

  const STATE_CLS = { applied: 'ok', pending: 'warn', conflict: 'bad', disabled: 'gray' };

  function paintAll() {
    const kpi = $('#mountsKpi');
    if (kpi) {
      const en = rows.filter(r => r.enabled).length;
      const pend = rows.filter(r => r.enabled && r.state !== 'applied').length;
      kpi.innerHTML = `<span class="st-item">容器 <b>${esc(cur || '—')}</b></span>`
        + `<span class="st-item">镜像 <b>${esc((statusData && statusData.image) || '—')}</b></span>`
        + `<span class="st-item">映射 <b>${rows.length}</b> 条（启用 ${en}）</span>`
        + `<span class="st-item">待应用 <b>${pend}</b> 条</span>`
        + `<span class="st-item">上次应用 <b>${esc(((statusData || {}).applied || {}).time || '从未')}</b></span>`;
    }
    const badge = $('#mountsDirty');
    if (badge) badge.textContent = dirty ? '有未保存的修改' : '已与后端同步';
    paintTemplate();
    const tb = $('#mountsTable');
    if (!tb) return;
    if (!cur) { tb.innerHTML = '<div class="empty-state glass">没有可用容器</div>'; return; }
    tb.innerHTML = `<div class="panel glass"><div class="panel-head">
        <h3><svg class="ic"><use href="#i-folder"/></svg> ${esc(cur)} 的目录映射</h3>
        <span class="sub">飞牛文件夹（左）→ 容器内路径（右），两端都可自定义；单文件改名请用「文件软链接」</span></div>
      <div class="mounts-table-wrap"><table class="mounts-table"><thead><tr>
        <th style="width:34%">飞牛上的文件夹（宿主机绝对路径）</th>
        <th style="width:29%">挂载到容器内路径（可自定义）</th>
        <th style="width:11%">权限</th>
        <th style="width:9%">启用</th>
        <th style="width:11%">状态</th>
        <th style="width:6%">操作</th>
      </tr></thead><tbody id="mountsRows">${rows.map(rowHtml).join('')}</tbody></table></div>
      <div class="mounts-foot">
        <button class="btn ghost slim" id="btnMountAdd"><svg class="ic"><use href="#i-plus"/></svg> 添加映射</button>
        <button class="btn ghost slim" id="btnMountImport">读取当前容器已有挂载</button>
        <span class="modal-spacer"></span>
        <span class="sub">最多 32 条 · 同一容器内路径只能有一条</span></div></div>
      <div class="mounts-current"><div class="panel-head"><h3>容器当前实际挂载（只读）</h3></div>
        ${((statusData && statusData.binds) || []).length
          ? `<div class="chip-list">${(statusData.binds || []).map(b => `<span class="m mono">${esc(b)}</span>`).join('')}</div>`
          : '<p class="sub">该容器当前没有任何 bind 挂载。</p>'}</div>
      ${(statusData && statusData.warnings || []).length
        ? `<div class="notice small-notice"><svg class="ic"><use href="#i-info"/></svg><span>${statusData.warnings.map(esc).join('<br>')}</span></div>` : ''}
      <p class="mounts-note">保存只写入 <code>data/pro/container-mounts.json</code>，不会碰容器；
        点「应用并重建」才会先做版本备份再重建。重建会短暂中断该容器，失败会自动还原旧容器，
        也可用更新页的「快速回退」手动还原。面板自身容器禁止修改挂载。</p>`;
    bindRows();
    const goSym = $('#btnGotoSymlinks');
    if (goSym && !goSym.dataset.bound) {
      goSym.dataset.bound = '1';
      goSym.onclick = (e) => {
        e.preventDefault();
        if (window.duGotoView) window.duGotoView('container-symlinks');
      };
    }
  }

  function rowHtml(r, i) {
    const cls = STATE_CLS[r.state] || 'gray';
    return `<tr data-i="${i}">
      <td><div class="mounts-path"><input class="input" data-f="host" value="${esc(r.host)}"
          placeholder="/vol1/1000/docker/影视" autocomplete="off" spellcheck="false" aria-label="飞牛目录">
        <button class="mini-act icon-only" data-act="browse" title="浏览飞牛上的文件夹" aria-label="浏览飞牛上的文件夹"><svg class="ic"><use href="#i-folder"/></svg></button></div></td>
      <td><div class="mounts-path"><input class="input" data-f="container" value="${esc(r.container)}"
          placeholder="/data" autocomplete="off" spellcheck="false" aria-label="容器内路径">
        <button class="mini-act icon-only" data-act="browse-container" title="选择容器内路径" aria-label="选择容器内路径"><svg class="ic"><use href="#i-folder-in"/></svg></button></div></td>
      <td><select class="input" data-f="mode" aria-label="读写权限">
        <option value="rw"${r.mode === 'rw' ? ' selected' : ''}>读写 rw</option>
        <option value="ro"${r.mode === 'ro' ? ' selected' : ''}>只读 ro</option></select></td>
      <td><label class="switch"><input type="checkbox" data-f="enabled"${r.enabled ? ' checked' : ''} aria-label="启用该映射"><span class="track"></span></label></td>
      <td><span class="badge ${cls}">${esc(r.state_label || (dirty ? '未保存' : '待应用'))}</span></td>
      <td><button class="mini-act icon-only danger" data-act="del" title="删除该映射" aria-label="删除该映射"><svg class="ic"><use href="#i-trash"/></svg></button></td>
    </tr>`;
  }

  /* ---------- v7.0.1 · 新容器默认映射模板（含功能总开关） ---------- */
  function paintTemplate() {
    const box = $('#mountsTemplateBox');
    if (!box) return;
    // 弹窗打开时不重绘，避免冲掉用户正在输入的内容
    const modal = document.querySelector('#genericModal');
    if (modal && (modal.classList.contains('on')
        || (document.activeElement && modal.contains(document.activeElement)))) return;
    const pol = polData || {}, rows = tplRows || [];
    const on = !!pol.master;
    const st = (label, val) => `<span class="st-item">${esc(label)} <b>${esc(val)}</b></span>`;
    box.innerHTML = `<div class="panel glass mounts-tpl-box${on ? '' : ' is-off'}">
      <div class="panel-head mounts-tpl-head">
        <h3><svg class="ic"><use href="#i-plus"/></svg> 新容器默认映射模板</h3>
        <label class="mounts-master" title="总开关：关闭后不会给任何新容器套模板，也不会重建容器">
          <span class="mounts-master-txt">功能总开关 <b class="${on ? 'is-on' : 'is-off'}">${on ? '已开启' : '已关闭'}</b></span>
          <span class="switch mounts-master-sw"><input type="checkbox" id="tplMasterSwitch"${on ? ' checked' : ''} aria-label="新容器默认映射模板功能总开关"><span class="track"></span></span>
        </label>
      </div>
      <div class="mounts-tpl-sum">
        ${st('总开关', on ? '已开启' : '已关闭')}
        ${st('模板', rows.length + ' 条')}
        ${st('自动继承', on ? (pol.inherit_new ? '已开启' : '未开启') : '已停用')}
        ${st('继承后自动重建', on ? (pol.auto_apply ? '已开启' : '未开启') : '已停用')}
        ${st('已登记容器', knownCount)}
        ${st('已继承', inheritedCount)}
        ${st('排除', (pol.exclude || []).length + ' 个')}
      </div>
      ${on ? '' : '<p class="mounts-master-off">总开关已关闭：后台不会给任何新容器套模板，也不会自动重建；模板、子开关与排除名单都会保留，开启后立即恢复。</p>'}
      ${rows.length
        ? `<div class="chip-list">${rows.map(r => `<span class="m mono">${esc(r.host)} → ${esc(r.container)} : ${esc(r.mode || 'rw')}${r.enabled === false ? '（停用）' : ''}</span>`).join('')}</div>`
        : '<p class="sub">还没有默认模板。点「编辑模板与策略」添加，之后新建的容器就能自动获得飞牛目录映射。</p>'}
      <div class="mounts-tpl-actions">
        <button class="btn ghost slim" id="btnMountTplEdit"><svg class="ic"><use href="#i-folder"/></svg> 编辑模板与策略</button>
        <button class="btn ghost slim" id="btnMountSync"><svg class="ic"><use href="#i-refresh"/></svg> 立即扫描新容器</button>
        <span class="sub" id="mountsSyncMsg">${esc(syncMsg)}</span>
      </div>
      <p class="mounts-note">支持占位符：<code>{name}</code> = 容器名、<code>{image}</code> = 镜像短名，
        例如 <code>/vol1/1000/docker/appdata/{name}</code> 让每个新容器各用各的文件夹。
        <b>总开关默认关闭</b>；开启后再打开「自动继承」，后台每 30 秒自动登记新容器，无需打开本页。
        已手工配置过的容器不会被模板覆盖；默认只写配置、不重建容器。</p></div>`;
    const edit = $('#btnMountTplEdit');
    if (edit) edit.onclick = editTemplate;
    const sync = $('#btnMountSync');
    if (sync) sync.onclick = syncNow;
    const master = $('#tplMasterSwitch');
    if (master) master.onchange = () => setMaster(master.checked);
  }

  /* v7.0.1：总开关单独走 mounts-master，不带 mappings，避免清空已配好的模板 */
  async function setMaster(on) {
    let d;
    try { d = await call('mounts-master', { enabled: !!on }, { progress: false }); }
    catch (e) { say(e.message || '切换总开关失败', 'err'); await render(true); return; }
    if (!d || d.ok === false) { say((d && d.error) || '切换总开关失败', 'err'); await render(true); return; }
    polData = Object.assign({}, polData, d.policy || {}, { master: !!on });
    say(d.message || (on ? '已开启新容器默认映射模板' : '已关闭新容器默认映射模板'));
    syncMsg = on ? '总开关已开启，后台将按子开关继承模板' : '总开关已关闭，后台只登记容器基线';
    needReload = true;
    await render(true);
  }

  function tplRowHtml(r, i) {
    return `<tr data-i="${i}">
      <td><input class="input" data-f="host" value="${esc(r.host || '')}" placeholder="/vol1/1000/docker/appdata/{name}" autocomplete="off" spellcheck="false" aria-label="默认飞牛目录"></td>
      <td><input class="input" data-f="container" value="${esc(r.container || '')}" placeholder="/data" autocomplete="off" spellcheck="false" aria-label="默认容器内路径"></td>
      <td><select class="input" data-f="mode" aria-label="默认读写权限">
        <option value="rw"${(r.mode || 'rw') === 'rw' ? ' selected' : ''}>读写 rw</option>
        <option value="ro"${r.mode === 'ro' ? ' selected' : ''}>只读 ro</option></select></td>
      <td><label class="switch"><input type="checkbox" data-f="enabled"${r.enabled !== false ? ' checked' : ''} aria-label="启用该模板行"><span class="track"></span></label></td>
      <td><button class="mini-act icon-only danger" data-act="del" title="删除该行" aria-label="删除该行"><svg class="ic"><use href="#i-trash"/></svg></button></td>
    </tr>`;
  }

  function editTemplate() {
    const draft = (tplRows || []).map(r => ({
      host: r.host || '', container: r.container || '', mode: r.mode || 'rw',
      enabled: r.enabled !== false, note: r.note || ''
    }));
    const pol = polData || {};
    const mon = !!pol.master;
    const draw = () => {
      const body = $('#mountTplRows');
      if (!body) return;
      body.innerHTML = draft.length
        ? draft.map(tplRowHtml).join('')
        : '<tr><td colspan="5" class="sub">还没有模板行，点「添加一行」开始。</td></tr>';
    };
    window.duOpenModal('新容器默认映射模板', '以后新建 / 重建的容器将自动获得这组映射',
      `<p class="sub">两端路径都可自定义。<code>{name}</code> 会替换成容器名、<code>{image}</code> 替换成镜像短名；
        保存只写配置，<b>不会重建任何现有容器</b>。</p>
       <div class="mounts-table-wrap"><table class="mounts-table mounts-tpl-table"><thead><tr>
         <th style="width:38%">飞牛上的文件夹（默认）</th><th style="width:30%">容器内路径（默认）</th>
         <th style="width:12%">权限</th><th style="width:9%">启用</th><th style="width:11%">操作</th>
       </tr></thead><tbody id="mountTplRows"></tbody></table></div>
       <div class="mounts-foot"><button class="btn ghost slim" id="btnMountTplAdd"><svg class="ic"><use href="#i-plus"/></svg> 添加一行</button>
         <span class="modal-spacer"></span><span class="sub">最多 32 条 · 同一容器内路径只能有一条</span></div>
       <div class="mounts-tpl-opts">
         <label class="mounts-check mounts-master-row"><input type="checkbox" id="tplMaster"${mon ? ' checked' : ''}><span><b>功能总开关</b>：关闭时不会给任何新容器套模板，也不会重建容器（面板上也能直接切换）</span></label>
         <label class="mounts-check" data-sub="1"><input type="checkbox" id="tplInherit" data-sub="1"${pol.inherit_new ? ' checked' : ''}${mon ? '' : ' disabled'}><span>发现新容器时自动继承此模板（推荐开启）</span></label>
         <label class="mounts-check" data-sub="1"><input type="checkbox" id="tplAutoApply" data-sub="1"${pol.auto_apply ? ' checked' : ''}${mon ? '' : ' disabled'}><span>继承后自动应用并重建（会短暂中断服务）</span></label>
         <label class="mounts-check" data-sub="1"><input type="checkbox" id="tplAutoPrecopy" data-sub="1"${pol.auto_precopy ? ' checked' : ''}${mon ? '' : ' disabled'}><span>自动应用时预迁移容器内原有数据</span></label>
         <label class="mounts-check"><input type="checkbox" id="tplPropagate"><span>把新模板同步到已继承的容器（需重新应用才生效）</span></label>
       </div>
       <p class="sub${mon ? '' : ' mounts-master-off'}" id="tplMasterHint">${mon ? '' : '总开关当前关闭：上面三个子开关已锁定，保存后也不会生效；先打开总开关再配置。'}</p>
       <div class="field"><label>不继承模板的容器名（逗号 / 空格 / 换行分隔）</label>
         <input class="input" id="tplExclude" value="${esc((pol.exclude || []).join(', '))}" placeholder="例如：plex, jellyfin" autocomplete="off"></div>
       <p class="sub">面板自身容器一律跳过；已有手工配置的容器也不会被模板覆盖。
         同名重建（容器 ID 变化）会重新套用模板并清空旧的「已应用」记录。</p>`,
      async () => {
        const body = {
          mappings: draft.filter(r => String(r.host).trim() || String(r.container).trim()),
          master: !!($('#tplMaster') && $('#tplMaster').checked),
          inherit_new: !!($('#tplInherit') && $('#tplInherit').checked),
          auto_apply: !!($('#tplAutoApply') && $('#tplAutoApply').checked),
          auto_precopy: !!($('#tplAutoPrecopy') && $('#tplAutoPrecopy').checked),
          propagate: !!($('#tplPropagate') && $('#tplPropagate').checked),
          exclude: ($('#tplExclude') && $('#tplExclude').value) || ''
        };
        let d;
        try { d = await call('mounts-template-save', body, { progress: false }); }
        catch (e) { say(e.message || '保存模板失败', 'err'); return false; }
        if (!d || d.ok === false) { say((d && d.error) || '保存模板失败', 'err'); return false; }
        say(d.message || '默认模板已保存');
        syncMsg = '模板已更新，可点「立即扫描新容器」验证';
        needReload = true;
        await render(true);
        return true;
      });
    draw();
    const body = $('#mountTplRows');
    if (body) {
      body.addEventListener('input', (e) => {
        const tr = e.target.closest('tr[data-i]'); if (!tr) return;
        const i = Number(tr.dataset.i), f = e.target.dataset.f;
        if (!draft[i] || !f) return;
        draft[i][f] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
      });
      body.addEventListener('change', (e) => {
        if (e.target.type === 'checkbox' || e.target.tagName === 'SELECT')
          e.target.dispatchEvent(new Event('input', { bubbles: true }));
      });
      body.addEventListener('click', (e) => {
        const b = e.target.closest('[data-act="del"]'); if (!b) return;
        const tr = b.closest('tr[data-i]'); if (!tr) return;
        draft.splice(Number(tr.dataset.i), 1); draw();
      });
    }
    const mst = $('#tplMaster');
    if (mst) mst.onchange = () => {
      const on = mst.checked;
      document.querySelectorAll('#genericModal input[data-sub="1"]').forEach(el => { el.disabled = !on; });
      document.querySelectorAll('#genericModal label[data-sub="1"]').forEach(el => {
        el.classList.toggle('is-locked', !on);
      });
      const hint = $('#tplMasterHint');
      if (hint) {
        hint.textContent = on ? '' : '总开关当前关闭：上面三个子开关已锁定，保存后也不会生效；先打开总开关再配置。';
        hint.classList.toggle('mounts-master-off', !on);
      }
    };
    const add = $('#btnMountTplAdd');
    if (add) add.onclick = () => {
      if (draft.length >= 32) return say('最多 32 条模板行', 'err');
      draft.push({ host: '', container: '', mode: 'rw', enabled: true, note: '' });
      draw();
    };
  }

  async function syncNow() {
    let d;
    try { d = await call('mounts-sync', { apply: false }, { progress: false }); }
    catch (e) { return say(e.message || '扫描失败', 'err'); }
    if (!d || d.ok === false) return say((d && d.error) || '扫描失败', 'err');
    syncMsg = d.message || '扫描完成';
    const bits = [];
    if (d.master_off) bits.push('总开关已关闭，本轮未套模板');
    if ((d.inherited || []).length) bits.push('继承 ' + d.inherited.length + ' 个');
    if ((d.errors || []).length) bits.push('需留意 ' + d.errors.length + ' 项');
    say(syncMsg + (bits.length ? '（' + bits.join('，') + '）' : ''));
    needReload = true;
    await render(true);
    const label = $('#mountsSyncMsg'); if (label) label.textContent = syncMsg;
  }

  function bindRows() {
    const body = $('#mountsRows');
    if (body) {
      body.addEventListener('input', (e) => {
        const tr = e.target.closest('tr[data-i]'); if (!tr) return;
        const i = Number(tr.dataset.i), f = e.target.dataset.f;
        if (!rows[i] || !f) return;
        rows[i][f] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
        rows[i].state = 'pending'; rows[i].state_label = '未保存';
        dirty = true;
        const b = $('#mountsDirty'); if (b) b.textContent = '有未保存的修改';
        const badge = tr.querySelector('.badge');
        if (badge) { badge.className = 'badge warn'; badge.textContent = '未保存'; }
      });
      body.addEventListener('change', (e) => {
        if (e.target.type === 'checkbox' || e.target.tagName === 'SELECT') {
          e.target.dispatchEvent(new Event('input', { bubbles: true }));
        }
      });
      body.addEventListener('click', (e) => {
        const b = e.target.closest('[data-act]'); if (!b) return;
        const tr = b.closest('tr[data-i]'); if (!tr) return;
        const i = Number(tr.dataset.i);
        if (b.dataset.act === 'del') { rows.splice(i, 1); dirty = true; paintAll(); }
        else if (b.dataset.act === 'browse') browse(i, rows[i].host || '');
        else if (b.dataset.act === 'browse-container') browseContainerPath(i);
      });
    }
    const add = $('#btnMountAdd');
    if (add) add.onclick = () => {
      if (rows.length >= 32) return say('每个容器最多 32 条映射', 'err');
      rows.push({ host: '', container: '', mode: 'rw', enabled: true, state: 'pending', state_label: '未保存' });
      dirty = true; paintAll();
      const inputs = $('#mountsRows tr:last-child input'); if (inputs) inputs.focus();
    };
    const imp = $('#btnMountImport');
    if (imp) imp.onclick = importCurrent;
  }

  function importCurrent() {
    const binds = (statusData && statusData.binds) || [];
    if (!binds.length) return say('该容器当前没有可导入的 bind 挂载', 'err');
    let n = 0;
    for (const b of binds) {
      const p = String(b).split(':');
      if (p.length < 2 || !p[0].startsWith('/') || !p[1].startsWith('/')) continue;
      if (rows.some(r => r.container === p[1])) continue;
      rows.push({ host: p[0], container: p[1], mode: (p[2] || 'rw').split(',')[0],
                  enabled: true, state: 'pending', state_label: '未保存' });
      n++;
    }
    dirty = true; paintAll();
    say(n ? `已导入 ${n} 条现有挂载，核对后点保存` : '现有挂载都已在列表里了');
  }

  /* ---------- 动作 ---------- */
  async function saveRows() {
    if (!cur) return say('请先选择容器', 'err');
    const payload = rows.filter(r => String(r.host).trim() || String(r.container).trim())
      .map(r => ({ id: r.id, host: r.host, container: r.container,
                   mode: r.mode, enabled: r.enabled, note: r.note || '' }));
    let d;
    try { d = await call('mounts-save', { name: cur, mappings: payload }, { progress: false }); }
    catch (e) { return say(e.message || '保存失败', 'err'); }
    if (!d || d.ok === false) return say((d && d.error) || '保存失败', 'err');
    dirty = false; needReload = true;
    say(d.message || '映射配置已保存（尚未应用到容器）');
    await render(true);
  }

  async function showPlan() {
    if (dirty) return say('请先保存配置，再查看重建计划', 'err');
    if (!cur) return say('请先选择容器', 'err');
    let d;
    try { d = await call('mounts-plan', { name: cur }, { progress: false }); }
    catch (e) { return say(e.message || '生成计划失败', 'err'); }
    if (!d || d.ok === false) return say((d && d.error) || '生成计划失败', 'err');
    planData = d;
    const diff = d.diff || {};
    const line = (label, arr0, cls) => {
      const arr = arr0 || [];
      return `<div class="mounts-diff ${cls || ''}"><b>${esc(label)}</b>
      ${arr.length ? `<div class="chip-list">${arr.map(x => `<span class="m mono">${esc(x)}</span>`).join('')}</div>`
        : '<span class="sub">无</span>'}</div>`;
    };
    const warn = (d.warnings || []).length
      ? `<div class="notice small-notice"><svg class="ic"><use href="#i-info"/></svg><span>${d.warnings.map(esc).join('<br>')}</span></div>` : '';
    const pre = (d.precopy || []).length
      ? `<label class="mounts-check"><input type="checkbox" id="mountPrecopy" checked><span>先把容器内原目录内容复制到飞牛目录（仅支持可写根 ${esc((statusData || {}).store_root || '')} 下的路径）</span></label>
         <label class="mounts-check"><input type="checkbox" id="mountAck"><span>我确认不迁移、直接覆盖（原数据仍在旧镜像层，可回退恢复）</span></label>` : '';
    window.duOpenModal('应用目录映射并重建容器', `${esc(cur)} · 镜像 ${esc(d.image || '—')}`,
      `<p class="sub">核对下面的变更，确认后才会执行。重建会保留镜像、端口、环境变量、网络、标签与重启策略，并先生成版本备份。</p>
       ${line('新增 / 变更挂载', diff.add, 'add')}
       ${line('移除的挂载', diff.remove, 'del')}
       ${line('保持不变', diff.keep, 'keep')}
       ${line('重建后的全部挂载', d.target_binds || [], 'all')}
       ${warn}${pre}
       <p class="sub">${d.has_changes ? '检测到来应用变更，可以执行。' : '当前配置与容器一致，执行也不会重建。'}</p>`,
      async () => {
        const precopy = !!($('#mountPrecopy') && $('#mountPrecopy').checked);
        const ack = !!($('#mountAck') && $('#mountAck').checked);
        if (!window.duConfirm2('mounts-apply:' + cur,
                              '再次点击确定：将重建该容器并短暂中断服务')) return false;
        let r;
        try {
          r = await call('mounts-apply',
            { name: cur, confirm: true, precopy: precopy, ack_nonempty: ack },
            { progress: false });
        } catch (e) { say(e.message || '提交失败', 'err'); return false; }
        if (!r || r.ok === false) { say((r && r.error) || '提交失败', 'err'); return false; }
        if (r.async && r.job_id) {
          const ph = window.duProgress ? window.duProgress.begin(
            '正在应用目录映射并重建容器',
            ['校验路径与冲突', '版本备份', '可选数据预迁移',
             '重建容器并保留全部原配置', '核对挂载结果'], true) : null;
          const fin = await window.duWatchJob(r.job_id, ph);
          say(fin.message || (fin.ok ? '目录映射已应用' : '目录映射未生效'), fin.ok ? 'ok' : 'err');
          needReload = true; await render(true);
        } else {
          say(r.message || '目录映射已应用', r.ok === false ? 'err' : 'ok');
          needReload = true; await render(true);
        }
        return true;
      });
  }

  async function browse(i, start) {
    browseTarget = i;
    browsePath = start || ((statusData && statusData.store_root) || '/vol1/1000/docker');
    const draw = async () => {
      let d;
      try { d = await call('mounts-dirs', { path: browsePath }, { progress: false }); }
      catch (e) { say(e.message || '浏览目录失败', 'err'); return; }
      browseData = d;
      const box = $('#mountBrowseList');
      if (!box) return;
      const label = $('#mountBrowsePath'); if (label) label.textContent = d.path;
      box.innerHTML = `<button class="btn ghost slim" data-dir="${esc(d.parent)}">↑ 上一级</button>`
        + (d.dirs.length ? `<div class="mounts-dirlist">${d.dirs.map(x =>
          `<button class="btn ghost slim" data-dir="${esc(x.path)}">📁 ${esc(x.name)}</button>`).join('')}</div>`
          : '<p class="sub">该目录下没有子目录，可以直接用当前目录。</p>')
        + (d.message ? `<p class="sub">${esc(d.message)}</p>` : '');
      box.querySelectorAll('[data-dir]').forEach(b => b.onclick = () => {
        browsePath = b.dataset.dir; draw();
      });
    };
    window.duOpenModal('选择飞牛上的文件夹', '点目录进入，点「使用当前目录」回填到该条映射',
      `<div class="field"><label>当前目录</label><div class="mono" id="mountBrowsePath">${esc(browsePath)}</div></div>
       <div id="mountBrowseList"></div>
       <p class="sub">也可以关掉本窗口，在输入框里直接粘贴绝对路径。</p>`,
      () => {
        if (browseTarget >= 0 && rows[browseTarget]) {
          rows[browseTarget].host = browsePath;
          rows[browseTarget].state = 'pending'; rows[browseTarget].state_label = '未保存';
          dirty = true; paintAll();
        }
        return true;
      });
    await draw();
  }


  /* v7.0.3：容器内路径选择器。容器内路径不在宿主机文件系统里，
     所以不给目录树，只给常用路径 + 该容器当前已有挂载的容器侧路径，
     并保留一个可自由输入/粘贴的框。 */
  function browseContainerPath(i) {
    const curRow = rows[i] || {};
    const common = ['/data', '/config', '/app', '/opt', '/mnt', '/media',
                    '/downloads', '/etc/localtime', '/var/lib', '/usr/share',
                    '/opt/app/data', '/app/config', '/config/www'];
    const existing = ((statusData && statusData.binds) || []).map(b => {
      const p = String(b).split(':');
      return p.length > 1 && p[1].startsWith('/') ? p[1] : '';
    }).filter(Boolean);
    const all = Array.from(new Set([curRow.container, ...existing, ...common].filter(Boolean)));
    window.duOpenModal('选择容器内路径',
      '点一个路径回填，也可以直接在下面粘贴自定义路径（容器内绝对路径）',
      `<div class="field"><label>自定义容器内路径</label>
         <input class="input" id="duContainerPath" value="${esc(curRow.container || '')}"
                placeholder="/data" autocomplete="off" spellcheck="false"></div>
       <div class="sym-browse"><div class="sym-browse-head">
         <span class="sub">常用路径${existing.length ? ' · 含该容器现有挂载' : ''}</span></div>
         <div class="chip-list">${all.map(x =>
           `<button class="chip" type="button" data-cp="${esc(x)}">${esc(x)}</button>`).join('')}</div>
       </div>
       <p class="sub">容器内路径不需要在飞牛上真实存在；应用并重建后由 Docker 自动创建。</p>`,
      () => {
        const v = String(($('#duContainerPath') || {}).value || '').trim();
        if (v && !v.startsWith('/')) { say('容器内路径必须以 / 开头', 'err'); return false; }
        if (rows[i]) {
          rows[i].container = v || rows[i].container;
          rows[i].state = 'pending'; rows[i].state_label = '未保存';
          dirty = true; paintAll();
        }
        return true;
      });
    setTimeout(() => {
      document.querySelectorAll('[data-cp]').forEach(b => b.onclick = () => {
        const el = $('#duContainerPath'); if (el) { el.value = b.dataset.cp; el.focus(); }
      });
    }, 0);
  }

  /* ---------- 入口 ---------- */
  async function render(force) {
    if (busy) return;
    if (!force && inited) {
      const active = document.activeElement;
      const editing = active && active.closest && active.closest('#mountsRows, #genericModal');
      if (dirty || editing || Date.now() - lastLoad < 30000) return;
    }
    busy = true;
    try { await loadAll(!!force); } finally { busy = false; }
  }

  function openFor(name) {
    if (dirty && !window.confirm('当前有未保存的映射修改，切换容器会丢弃。确定继续？')) return;
    dirty = false;
    cur = String(name || '');
    if (window.duGotoView) window.duGotoView('container-mounts');
    needReload = true;
    render(true);
  }

  function bindBar() {
    const sel = $('#mountsContainer');
    if (sel && !sel.dataset.duBound) {
      sel.dataset.duBound = '1';
      sel.addEventListener('change', () => openFor(sel.value));
    }
    const map = {
      btnMountSave: saveRows,
      btnMountPlan: showPlan,
      btnMountRefresh: () => { dirty = false; render(true); },
      btnMountApply: showPlan
    };
    Object.keys(map).forEach(id => {
      const b = $('#' + id);
      if (b && !b.dataset.duBound) { b.dataset.duBound = '1'; b.onclick = map[id]; }
    });
  }
  document.addEventListener('DOMContentLoaded', bindBar);

  window.duMounts = {
    render: render,
    openFor: openFor,
    bindBar: bindBar,
    get rows() { return rows; },
    get current() { return cur; }
  };
})();
