/* ============================================================
 * DockerUpdate v6.0-r2 · 通用「折线图 + 数据」趋势带
 * 只画真实数据：监控小时序列 / 告警历史 / 容器与镜像快照。
 * 没有采样点时不编造曲线，明确显示「等待采样」。
 * 1:1 viewBox 且文字全部用 HTML（不进 SVG），不会重现「刻度被压扁」。
 * 只在切到该页时渲染（隐藏容器 clientWidth=0 会算出 0 宽），
 * 用签名缓存避免 5 秒轮询反复重建 DOM（历史「整页闪动」的根因）。
 * ============================================================ */
(function () {
  'use strict';

  /* v7.0.3 · P4 柔灰雕塑：趋势曲线只用中性灰阶，按明暗主题分两套明度，
     避免浅色白卡 / 深色深灰卡上出现看不见的线。 */
  var COLORS_LIGHT = ['#3d3f42', '#5b5d60', '#787a7d', '#95979a', '#2b2d30'];
  var COLORS_DARK  = ['#e2e3e4', '#c7c8ca', '#abadae', '#8f9193', '#f0f0ef'];
  function palette() {
    return document.documentElement.getAttribute('data-theme') === 'dark'
      ? COLORS_DARK : COLORS_LIGHT;
  }
  var MAX_POINTS = 24;

  function n(v) { var x = Number(v); return isFinite(x) ? x : 0; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(v, digits) {
    var d = digits == null ? 1 : digits;
    if (!isFinite(v)) return '—';
    if (Math.abs(v) >= 1024) return (v / 1024).toFixed(1) + ' K';
    return v.toFixed(d);
  }
  function state() { return window.__duState || window.S || null; }
  function consoleOf(S) { return (S && S.console) || {}; }

  /* 最近 24 个整点桶（没有事件的小时就是真实的 0，不是补假数据） */
  function buckets() {
    var out = [], now = new Date();
    now.setMinutes(0, 0, 0);
    for (var i = 23; i >= 0; i--) {
      var d = new Date(now.getTime() - i * 3600000);
      var p = function (x) { return (x < 10 ? '0' : '') + x; };
      out.push({
        key: d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()),
        label: p(d.getHours()) + ':00',
        v: 0
      });
    }
    return out;
  }
  function hourKey(s) {
    if (!s) return '';
    var m = String(s).match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2})/);
    return m ? m[1] + ' ' + m[2] : '';
  }
  function countByHour(records, timeKey, filter) {
    var bs = buckets(), idx = {};
    bs.forEach(function (b, i) { idx[b.key] = i; });
    (records || []).forEach(function (r) {
      if (filter && !filter(r)) return;
      var k = hourKey(r && (r[timeKey] || r.time || r.at || r.ts_text));
      if (k && idx[k] !== undefined) bs[idx[k]].v += 1;
    });
    return { labels: bs.map(function (b) { return b.label; }), values: bs.map(function (b) { return b.v; }) };
  }

  /* v6.0-r17：更新次数改「按年」口径 —— 最近 12 个自然月，每月一个点，
     主数字是近一年累计次数。用户原话「按小时聚合改成按年来吧，一年内更新多少次」。
     没有记录的月份就是真实的 0，绝不补假值。 */
  function monthBuckets() {
    var out = [], now = new Date();
    for (var i = 11; i >= 0; i--) {
      var d = new Date(now.getFullYear(), now.getMonth() - i, 1);
      var y = d.getFullYear(), m = d.getMonth() + 1;
      out.push({
        key: y + '-' + (m < 10 ? '0' : '') + m,
        label: (y !== now.getFullYear() ? String(y).slice(2) + '/' : '') + m + '月',
        v: 0
      });
    }
    return out;
  }
  function monthKey(s) {
    var m = String(s || '').match(/^(\d{4})-(\d{2})/);
    return m ? m[1] + '-' + m[2] : '';
  }
  function countByYear(records, timeKey, filter) {
    var bs = monthBuckets(), idx = {};
    bs.forEach(function (b, i) { idx[b.key] = i; });
    var total = 0;
    (records || []).forEach(function (r) {
      if (filter && !filter(r)) return;
      var k = monthKey(r && (r[timeKey] || r.time || r.at || r.ts_text));
      if (k && idx[k] !== undefined) { bs[idx[k]].v += 1; total += 1; }
    });
    return {
      labels: bs.map(function (b) { return b.label; }),
      values: bs.map(function (b) { return b.v; }),
      total: total
    };
  }
  /* 更新成功同时存在于「告警历史」与「运行日志」：合并后按分钟去重，
     避免同一条更新被数两次；运行日志保留天数可在设置里调到 365 天。 */
  function updateRecords(S, hist) {
    var seen = {}, out = [];
    function add(t) {
      var k = String(t || '').slice(0, 16);
      if (!k || seen[k]) return;
      seen[k] = 1; out.push({ time: t });
    }
    (hist || []).forEach(function (r) {
      if (/update_ok|更新成功|更新完成/i.test(String(r.kind || '') + ' ' + String(r.message || r.text || '')))
        add(r.time || r.at);
    });
    (S.history || []).forEach(function (r) {
      if (/update|auto-update/i.test(String(r.action || '')) &&
          /更新完成|更新成功|已更新/i.test(String(r.message || '')))
        add(r.time);
    });
    return out;
  }
  function trendSeries(field, transform) {
    var S = state(), pts = (consoleOf(S).trend || []).slice(-MAX_POINTS);
    if (!pts.length) return { labels: [], values: [] };
    return {
      labels: pts.map(function (p) { return String(p.t || '').slice(11, 16) || ''; }),
      values: pts.map(function (p) {
        var v = n(p[field]);
        return transform ? transform(v, p) : v;
      })
    };
  }

  function spark(values, color, id) {
    var w = 260, h = 54, pad = 4;
    if (!values || values.length < 2) {
      return '<div class="du-spark-empty">等待采样数据 · 开启资源监控后自动累积</div>';
    }
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values);
    if (max - min < 1e-9) { max = min + 1; }
    var step = (w - pad * 2) / (values.length - 1);
    var xy = values.map(function (v, i) {
      return [pad + i * step, h - pad - ((v - min) / (max - min)) * (h - pad * 2)];
    });
    var line = xy.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
    var area = line + ' L' + xy[xy.length - 1][0].toFixed(1) + ' ' + (h - pad) + ' L' + pad + ' ' + (h - pad) + ' Z';
    var last = xy[xy.length - 1];
    var grid = [0.25, 0.5, 0.75].map(function (r) {
      return '<line x1="0" y1="' + (h * r).toFixed(1) + '" x2="' + w + '" y2="' + (h * r).toFixed(1) + '" stroke="currentColor" stroke-opacity=".09" stroke-width="1"/>';
    }).join('');
    return '<svg class="du-spark" viewBox="0 0 ' + w + ' ' + h + '" width="100%" height="' + h + '" ' +
      'preserveAspectRatio="xMidYMid meet" aria-hidden="true">' +
      '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' + color + '" stop-opacity=".34"/>' +
      '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
      grid + '<path d="' + area + '" fill="url(#' + id + ')"/>' +
      '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
      '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="3.2" fill="' + color + '"/>' +
      '</svg>';
  }

  function card(spec, i, colors) {
    var vals = (spec.values || []).map(n);
    var cur = vals.length ? vals[vals.length - 1] : null;
    var max = vals.length ? Math.max.apply(null, vals) : null;
    var avg = vals.length ? vals.reduce(function (a, b) { return a + b; }, 0) / vals.length : null;
    var d = spec.digits == null ? 1 : spec.digits;
    /* v6.0-r17：按年聚合的卡片，主数字显示「近一年累计」而不是最后一个月的点数 */
    var main = spec.total == null ? cur : spec.total;
    var color = colors[i % colors.length];
    var id = 'duTG_' + (spec.id || i) + '_' + String(vals.length);
    return '<div class="du-trend-card" style="--tc:' + color + '">' +
      '<div class="du-tc-head"><span class="du-tc-dot"></span><b>' + esc(spec.title) + '</b>' +
      (spec.note ? '<em>' + esc(spec.note) + '</em>' : '') + '</div>' +
      '<div class="du-tc-nums"><span class="du-tc-cur">' + (main === null ? '—' : fmt(main, d)) +
      '<i>' + esc(spec.unit || '') + '</i></span>' +
      '<span>' + esc(spec.peakLabel || '峰值') + ' <b>' + (max === null ? '—' : fmt(max, d)) + '</b></span>' +
      '<span>' + esc(spec.avgLabel || '均值') + ' <b>' + (avg === null ? '—' : fmt(avg, d)) + '</b></span></div>' +
      '<div class="du-tc-chart">' + spark(vals, color, id) + '</div>' +
      (spec.footer ? '<div class="du-tc-foot">' + esc(spec.footer) + '</div>' : '') +
      '</div>';
  }

  function specs(view) {
    var S = state() || {}, C = consoleOf(S);
    var hist = C.alert_history || [];
    var apps = C.apps || [], containers = S.containers || [];
    var images = C.images || [], df = C.docker_df || {};
    var updatable = containers.filter(function (c) {
      return c && (c.update_status === 'available' || c.has_update || c.status === 'update_available');
    }).length;
    var running = containers.filter(function (c) { return c && (c.running || c.state === 'running'); }).length;

    function ev(filter) { return countByHour(hist, 'time', filter); }
    function t(field, tr) { return trendSeries(field, tr); }

    var perf = [
      { id: 'cpu', title: 'CPU 占用', unit: '%', digits: 1, values: t('cpu').values, footer: '按小时均值 · 最近 24 点' },
      { id: 'mem', title: '内存占用', unit: 'GB', digits: 2, values: t('mem_gb').values, footer: '整机已用内存' },
      { id: 'net', title: '下行流量', unit: 'MB/h', digits: 1, values: t('net_down').values, footer: '每小时累计下行' }
    ];
    var counts = [
      { id: 'cts', title: '在线容器', unit: '个', digits: 0, values: t('containers').values, footer: '按小时采样' },
      { id: 'cpu2', title: 'CPU 占用', unit: '%', digits: 1, values: t('cpu').values, footer: '按小时均值' },
      { id: 'mem2', title: '内存占用', unit: 'GB', digits: 2, values: t('mem_gb').values, footer: '整机已用内存' }
    ];

    if (view === 'overview' || view === 'apps') return perf.map(function (s, i) {
      if (view === 'apps' && i === 0) s.note = apps.length + ' 个应用 · ' + running + ' 个运行中';
      return s;
    });
    if (view === 'containers' || view === 'container-links' || view === 'container-snapshots' || view === 'container-details')
      return counts;
    if (view === 'updates') {
      /* v6.0-r17：按年聚合（近 12 个自然月），主数字 = 一年内累计次数 */
      var foundY = countByYear(hist, 'time', function (r) {
        return /found|new_version|可更新|发现新版本/i.test(
          String(r.kind || '') + String(r.text || '') + String(r.message || ''));
      });
      var doneY = countByYear(updateRecords(S, hist), 'time', null);
      return [
        { id: 'found', title: '发现新版本', unit: '次/年', digits: 0, values: foundY.values,
          total: foundY.total, peakLabel: '峰值月', avgLabel: '月均',
          footer: '按年聚合 · 近 12 个月共 ' + foundY.total + ' 次 · 当前可更新 ' + updatable + ' 个' },
        { id: 'done', title: '更新完成', unit: '次/年', digits: 0, values: doneY.values,
          total: doneY.total, peakLabel: '峰值月', avgLabel: '月均',
          footer: '按年聚合 · 一年内共更新 ' + doneY.total + ' 次（每月一个点，无记录即真实 0）' },
        { id: 'cts3', title: '巡检容器数', unit: '个', digits: 0, values: t('containers').values, footer: '按小时采样' }
      ];
    }
    if (view === 'images') {
      var pull = ev(function (r) { return /image|镜像|pull/i.test(String(r.kind || '') + String(r.text || '')); });
      return [
        { id: 'pull', title: '镜像相关事件', unit: '次/h', digits: 0, values: pull.values, footer: '来自告警历史' },
        { id: 'net2', title: '下行流量', unit: 'MB/h', digits: 1, values: t('net_down').values, footer: '拉镜像时的每小时流量' },
        { id: 'disk', title: '磁盘占用', unit: '%', digits: 1, values: t('disk').values, footer: '本地镜像 ' + images.length + ' 个 · 占用 ' + n(df.images_size) + ' MB' }
      ];
    }
    if (view === 'alerts') {
      var all = ev(null), high = ev(function (r) { return r.level === 'high'; });
      return [
        { id: 'al', title: '告警事件', unit: '条/h', digits: 0, values: all.values, footer: '最近 24 小时 · 共 ' + hist.length + ' 条' },
        { id: 'hi', title: '高危告警', unit: '条/h', digits: 0, values: high.values, footer: '仅 HIGH 级别' },
        { id: 'cpu3', title: '同期 CPU', unit: '%', digits: 1, values: t('cpu').values, footer: '便于对照阈值触发' }
      ];
    }
    if (view === 'system') return counts;
    if (view === 'logs') {
      var l = ev(null);
      return [
        { id: 'log', title: '面板事件', unit: '条/h', digits: 0, values: l.values, footer: '按小时聚合告警与动作历史' },
        { id: 'net3', title: '下行流量', unit: 'MB/h', digits: 1, values: t('net_down').values, footer: '同期网络' }
      ];
    }
    return null;
  }

  function render(view) {
    if (view === 'settings') return;
    var el = document.getElementById('view' + view.charAt(0).toUpperCase() + view.slice(1));
    if (!el) {
      /* 容器子页面的 id 是 viewContainerXxx，做一次 camel 映射 */
      var camel = view.replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
      el = document.getElementById('view' + camel.charAt(0).toUpperCase() + camel.slice(1));
    }
    if (!el) return;
    var list = specs(view);
    if (!list || !list.length) return;
    var strip = el.querySelector('.du-trend-strip');
    var head = el.querySelector('.page-head');
    if (!strip) {
      strip = document.createElement('div');
      strip.className = 'du-trend-strip glass';
      if (head && head.nextSibling) head.parentNode.insertBefore(strip, head.nextSibling);
      else if (head) head.parentNode.appendChild(strip);
      else el.insertBefore(strip, el.firstChild);
    } else if (head && strip.parentNode === head.parentNode && strip.previousElementSibling !== head) {
      /* 系统联动的标题由 pro.js 异步渲染，首帧 strip 可能落在标题上方，这里自动归位 */
      head.parentNode.insertBefore(strip, head.nextSibling);
    }
    var theme = document.documentElement.getAttribute('data-theme') || 'light';
    var sig = theme + '|' + view + '|' + JSON.stringify(list.map(function (s) {
      return [s.id, (s.values || []).slice(-MAX_POINTS)];
    }));
    if (strip.dataset.sig === sig) return;   /* 数据没变，一个 DOM 都不动 */
    strip.dataset.sig = sig;
    var headNote = view === 'updates'
      ? '折线为真实采样 · 更新次数按年聚合（近 12 个月，每月一个点），其余按小时；未采集到数据时不补假值'
      : '折线为真实采样 / 事件按小时聚合，未采集到数据时不补假值';
    strip.innerHTML = '<div class="du-ts-head"><span class="du-ts-dot"></span><b>趋势</b>' +
      '<em>' + esc(headNote) + '</em></div>' +
      '<div class="du-ts-grid">' + list.map(function (s, i) {
        return card(s, i, palette());
      }).join('') + '</div>';
  }

  window.duRenderTrendStrip = render;
})();
