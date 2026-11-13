/* DockerUpdate v3.0 · 后台任务进度监听（SSE 优先，轮询兜底）
   从已移除的应用商店脚本里抽出来，供镜像仓库 / Compose 栈 / 模板部署 / 系统联动共用。
   飞牛远程访问与反向代理不一定保持 text/event-stream：3 秒没事件就退化成轮询，
   任务在服务端后台线程执行，刷新页面也不会中断。 */
(function () {
'use strict';
function jobToken() {
  return (typeof token !== 'undefined' && token) || localStorage.getItem('du_token')
    || sessionStorage.getItem('du_token') || '';
}
function watchJob(jobId, ph) {
  return new Promise((resolve) => {
    let settled = false, es = null, poll = null, got = false, lastSeq = 0;
    const done = async (ok, msg, ev) => {
      if (settled) return;
      settled = true;
      try { if (es) es.close(); } catch (_) {}
      if (poll) clearInterval(poll);
      if (ph && window.duProgress) window.duProgress.finish(ph, ok);
      if (msg && window.duToast) window.duToast(msg, ok ? 'ok' : 'err');
      // 后台任务的返回值（导出的 base64、扫描结果等）存在 job.result 里，
      // SSE 的 done 事件不带它，终态再拉一次 snapshot 拿全。
      let result = (ev && ev.result) || {};
      try {
        const r = await window.duApi('/api/job?id=' + encodeURIComponent(jobId));
        if (r && r.job && r.job.result) result = r.job.result;
      } catch (_) {}
      resolve({ ok: !!ok, message: msg || '', result: result || {} });
    };
    const apply = (ev) => {
      if (!ev || settled) return;
      if (ev.type === 'hello') return;
      const seq = Number(ev.seq || 0);
      if (seq && seq <= lastSeq) return;
      if (seq) lastSeq = seq;
      got = true;
      if (window.duProgress) window.duProgress.applyJob({
        stage: ev.stage, pct: ev.pct, detail: ev.detail, speed: ev.speed,
        status: ev.status, type: ev.type,
        line: ev.line, trace: ev.trace, trace_id: ev.trace_id || ev.run_id });
      if (ev.status === 'done' || ev.type === 'done') done(true, ev.message || '操作已完成', ev);
      else if (ev.status === 'error' || ev.type === 'error')
        done(false, ev.error || ev.message || '操作失败', ev);
      else if (ev.type === 'end')
        done(ev.status !== 'error', ev.message || ev.error ||
             (ev.status === 'error' ? '操作失败' : '操作已完成'), ev);
    };
    const startPoll = () => {
      if (poll || settled) return;
      const tick = async () => {
        if (settled) return;
        try {
          const r = await window.duApi('/api/job?id=' + encodeURIComponent(jobId));
          if (r && r.job) {
            const j = r.job;
            apply(Object.assign({}, j, {
              type: j.finished_at ? 'end' : 'progress',
              error: j.error || '', message: j.message || '' }));
          }
        } catch (_) { /* 网络抖动保留轮询 */ }
      };
      tick();
      poll = setInterval(tick, 1500);
    };
    try {
      const u = '/api/job/events?job=' + encodeURIComponent(jobId)
        + '&token=' + encodeURIComponent(jobToken());
      es = new EventSource(u);
      es.onmessage = (e) => { try { apply(JSON.parse(e.data)); } catch (_) {} };
      es.onerror = () => {
        try { if (es) es.close(); } catch (_) {}
        es = null;
        if (!settled) startPoll();
      };
    } catch (_) { startPoll(); }
    setTimeout(() => {
      if (!got && !settled) {
        try { if (es) es.close(); } catch (_) {}
        es = null;
        startPoll();
      }
    }, 3500);
  });
}
window.duWatchJob = watchJob;
})();
