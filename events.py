"""DockerUpdate v6.0 · 统一消息推送事件中心（自 v5.1 起）。

为什么要有它：告警页原本只有「CPU / 内存 / 磁盘 / 容器意外退出」四条硬编码规则，
v5.0 的多渠道通知又有一套独立开关，两处配置容易分裂。这里把所有「发生了什么」
收敛成一份事件目录 + 一份开关，告警页 Webhook 与多渠道通知共用同一套开关。

设计约束：
  · 纯标准库，不引入第三方依赖；
  · 关闭的事件不推送、也不写告警历史（但运行日志仍由各自业务记录）；
  · 网络发送全部异步，绝不阻塞更新 / 检测主流程；
  · 面板不内置任何地址或凭据，未配置就明确「未配置」，不伪造推送成功；
  · 旧版 notify.json / alerts.json 里的开关自动迁移，覆盖升级不丢配置。
"""
import json
import os
import sys
import threading
import time

import config

EVENTS_FILE = os.path.join(config.DATA_DIR, "events.json")

# (id, 标签, 分组, 默认, 说明)
EVENT_CATALOG = (
    # ---------------- 更新与版本 ----------------
    ("found_update",      "检测到有可更新容器",   "更新与版本", True,
     "巡检或手动检查发现新版本时推送，含容器名、新镜像 ID 与版本号"),
    ("update_ok",         "更新成功",             "更新与版本", True,
     "容器重建完成且健康检查通过，含旧 / 新镜像 ID 与健康结论"),
    ("update_fail",       "更新失败",             "更新与版本", True,
     "拉取失败、重建失败或自动更新异常，含失败原因"),
    ("auto_rollback",     "健康检查失败自动回退", "更新与版本", True,
     "更新后探测不通过，已自动回到更新前的版本"),
    ("rollback_ok",       "手动回退完成",         "更新与版本", True,
     "点「快速回退 / 回退到此版本」成功后推送"),
    ("rollback_fail",     "版本回退失败",         "更新与版本", True,
     "旧镜像已不在本机或重建失败，含失败原因"),
    ("backup_ok",         "版本备份完成",         "更新与版本", False,
     "更新前 / 手动创建的配置与镜像 ID 快照"),
    ("check_fail",        "检测失败 / 仓库不可达", "更新与版本", True,
     "本次版本检测未完成，已保留上一次结果"),
    ("check_done",        "全量巡检完成",         "更新与版本", False,
     "每日巡检或手动「检查全部」结束时的汇总"),
    # ---------------- 镜像更新（v6.0-r18） ----------------
    ("image_update_found", "检测到镜像有新版本", "更新与版本", True,
     "镜像更新页比对远端 digest 发现变化，含镜像名与探测源；仅更新镜像，不重建容器"),
    ("image_update_done",  "镜像更新完成",       "更新与版本", False,
     "镜像更新页批量拉取结束的汇总，含成功数与镜像 ID 实际变化数"),
    # ---------------- 容器生命周期 ----------------
    ("container_added",   "新增容器",             "容器生命周期", True,
     "发现本机出现新容器（含通过面板创建 / 外部创建）"),
    ("container_removed", "删除容器",             "容器生命周期", True,
     "发现本机容器被删除，含容器名"),
    ("container_started", "容器启动",             "容器生命周期", True,
     "由停止变为运行，或手动点「启动」"),
    ("container_stopped", "容器停止",             "容器生命周期", True,
     "由运行变为停止，或手动点「停止」"),
    ("container_restarted", "容器重启",           "容器生命周期", True,
     "手动点「重启」或计划任务重启"),
    ("container_down",    "容器意外退出",         "容器生命周期", True,
     "采样时发现原本运行的容器不再运行（原有告警规则）"),
    ("container_created", "部署创建容器",         "容器生命周期", True,
     "通过「创建容器 / 镜像部署」新建容器成功"),
    ("container_paused",  "容器暂停",             "容器生命周期", False,
     "暂停 / 恢复容器进程"),
    ("container_unpaused", "容器恢复运行",        "容器生命周期", False,
     "从暂停状态恢复"),
    ("container_limits",  "资源限额变更",         "容器生命周期", False,
     "在线修改 CPU / 内存上限"),
    ("image_pull",        "镜像拉取完成",         "容器生命周期", False,
     "拉取镜像或批量拉取任务结束"),
    ("image_pull_fail",   "镜像拉取失败",         "容器生命周期", True,
     "仓库不可达、镜像不存在或鉴权失败"),
    # ---------------- 容器目录映射（v6.0-r24） ----------------
    ("mount_applied",     "目录映射已生效",       "容器生命周期", True,
     "飞牛目录已挂进容器内自定义路径并完成重建，含新增 / 移除条数与预迁移文件数"),
    ("mount_fail",        "目录映射失败",         "容器生命周期", True,
     "重建失败已自动还原旧容器，或挂载结果核对不一致，含具体原因"),
    ("mount_inherit",     "新容器继承目录映射",   "容器生命周期", True,
     "升级后新建 / 重建的容器自动继承默认映射模板，含继承条数；只写配置，不自动重建"),
    # ---------------- 资源与阈值 ----------------
    ("cpu_high",          "CPU 超阈值",           "资源与阈值", True,
     "连续 3 次采样超过 CPU 阈值"),
    ("mem_high",          "内存超阈值",           "资源与阈值", True,
     "连续 3 次采样超过内存阈值"),
    ("disk_high",         "磁盘超阈值",           "资源与阈值", True,
     "宿主机磁盘使用率超过阈值"),
    ("net_high",          "网络流量异常",         "资源与阈值", False,
     "容器网络 IO 明显高于近一小时均值"),
    # ---------------- 网络连通性（v6.0-r17） ----------------
    ("net_down",          "外网断网 / 网络异常",   "网络连通性", True,
     "连续多次探测全部目标不可达；断网当下尝试内网通知，恢复后自动补发未送达的消息"),
    ("net_up",            "外网恢复",             "网络连通性", True,
     "连续多次探测恢复可达，含断网时长与当前可达目标"),
    # ---------------- 面板与系统 ----------------
    ("login_fail",        "登录失败 / 临时锁定",   "面板与系统", True,
     "密码错误或连续失败触发限流"),
    ("login_ok",          "登录成功",             "面板与系统", False,
     "每次成功登录面板"),
    ("password_changed",  "登录密码被修改",       "面板与系统", True,
     "修改密码成功，其它会话失效"),
    ("settings_changed",  "设置变更",             "面板与系统", False,
     "巡检 / 监控 / 健康检查等设置被保存"),
    ("task_ok",           "计划任务执行成功",     "面板与系统", False,
     "18 种计划任务执行完成"),
    ("task_fail",         "计划任务执行失败",     "面板与系统", True,
     "计划任务或后台任务异常"),
    ("panel_backup",      "面板备份 / 恢复",      "面板与系统", True,
     "导出 zip、上传恢复或 Compose 写回"),
    ("accel_switch",      "加速线路切换",         "面板与系统", False,
     "智能选最快自动切换 GitHub / 镜像加速源"),
)

EVENT_IDS = tuple(x[0] for x in EVENT_CATALOG)
GROUPS = ("更新与版本", "容器生命周期", "资源与阈值", "网络连通性", "面板与系统")
DEFAULTS = {x[0]: x[3] for x in EVENT_CATALOG}

# 推送通道：auto = 优先多渠道通知中心，未配置才用告警页 Webhook；both = 两处都发
CHANNEL_MODES = (("auto", "自动 · 多渠道优先"),
                 ("notify", "只推通知中心"),
                 ("alert", "只推告警 Webhook"),
                 ("both", "两处都推送"))

_lock = threading.RLock()
_cache = None
_cache_mtime = 0
_last_sent = {}
_bound_alerts = None


def catalog():
    return [{"id": a, "label": b, "group": c, "default": d, "desc": e}
            for a, b, c, d, e in EVENT_CATALOG]


def _default_doc():
    return {"version": 2, "channel_mode": "auto", "events": dict(DEFAULTS), "stats": {}}


def _migrate(d):
    out = _default_doc()
    if not isinstance(d, dict):
        return out
    mode = str(d.get("channel_mode") or "auto")
    out["channel_mode"] = mode if mode in tuple(x[0] for x in CHANNEL_MODES) else "auto"
    src = d.get("events")
    if not isinstance(src, dict):
        src = d.get("push_events") or {}
    if isinstance(src, dict):
        for k, v in src.items():
            if k in out["events"]:
                out["events"][k] = bool(v)
    st = d.get("stats")
    if isinstance(st, dict):
        for k, v in st.items():
            if k in out["events"] and isinstance(v, dict):
                out["stats"][k] = {"last": str(v.get("last") or "")[:20],
                                   "count": int(v.get("count") or 0)}
    return out


def load():
    """带 mtime 缓存：告警页渲染 / 每次 emit 都要读，避免重复 IO。"""
    global _cache, _cache_mtime
    try:
        mt = os.path.getmtime(EVENTS_FILE)
    except OSError:
        mt = 0
    with _lock:
        if _cache is not None and mt == _cache_mtime:
            return {"channel_mode": _cache["channel_mode"],
                    "events": dict(_cache["events"]),
                    "stats": {k: dict(v) for k, v in _cache.get("stats", {}).items()}}
        doc = _default_doc()
        # ① 读 events.json
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                doc = _migrate(json.load(f))
            # 目录新增了事件但旧文件没有 → 补默认值
            for k, v in DEFAULTS.items():
                doc["events"].setdefault(k, v)
            doc.setdefault("stats", {})
        except Exception:
            pass
        # ② 首次升级：把旧 notify.json 的 events / alerts.json 的 container_down 迁进来
        if not os.path.exists(EVENTS_FILE):
            try:
                np = os.path.join(config.DATA_DIR, "pro", "notify.json")
                with open(np, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old.get("events"), dict):
                    for k, v in old["events"].items():
                        if k in doc["events"]:
                            doc["events"][k] = bool(v)
            except Exception:
                pass
            try:
                with open(config.ALERTS_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if "container_down" in old:
                    doc["events"]["container_down"] = bool(old["container_down"])
                # 老版本把阈值告警写在 alerts.enabled 里；关掉则三项也关
                if not old.get("enabled"):
                    for k in ("cpu_high", "mem_high", "disk_high"):
                        doc["events"][k] = False
            except Exception:
                pass
            save(doc)
        _cache, _cache_mtime = doc, (os.path.getmtime(EVENTS_FILE) if os.path.exists(EVENTS_FILE) else 0)
        return {"channel_mode": doc["channel_mode"],
                "events": dict(doc["events"]),
                "stats": {k: dict(v) for k, v in doc.get("stats", {}).items()}}


def save(doc):
    global _cache, _cache_mtime
    clean = _migrate(doc)
    clean["version"] = 2
    for k, v in DEFAULTS.items():
        clean["events"].setdefault(k, v)
    clean.setdefault("stats", {})
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = "%s.%s.%d.tmp" % (EVENTS_FILE, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, EVENTS_FILE)
    with _lock:
        _cache, _cache_mtime = clean, os.path.getmtime(EVENTS_FILE)
    return {"channel_mode": clean["channel_mode"],
            "events": dict(clean["events"]),
            "stats": {k: dict(v) for k, v in clean.get("stats", {}).items()}}


def invalidate():
    """面板数据恢复后强制下次 load() 重新读盘。"""
    global _cache, _cache_mtime
    with _lock:
        _cache, _cache_mtime = None, 0


def stats():
    d = load()
    return {k: dict(v) for k, v in d.get("stats", {}).items()}


def record_stat(event):
    """记一次事件触发：告警页可显示「最近触发时间 / 累计次数」，方便判断该开哪些。"""
    if event not in EVENT_IDS:
        return
    from datetime import datetime
    now = datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        d = load()
        st = d.setdefault("stats", {})
        row = st.get(event) or {"last": "", "count": 0}
        row["last"] = now
        try:
            row["count"] = int(row.get("count") or 0) + 1
        except (TypeError, ValueError):
            row["count"] = 1
        st[event] = row
        save(d)


def enabled(event, doc=None):
    if not event:
        return True
    d = doc or load()
    return bool(d["events"].get(event, True))


def bind(alerts):
    """由 Alerts.__init__ 直接注册，单元测试 / 独立运行时也能写告警历史。"""
    global _bound_alerts
    with _lock:
        _bound_alerts = alerts


def _alerts():
    """优先用已绑定实例；否则延迟取 server.console.alerts（避免循环 import）。"""
    with _lock:
        if _bound_alerts is not None:
            return _bound_alerts
    srv = sys.modules.get("server")
    con = getattr(srv, "console", None) if srv else None
    return getattr(con, "alerts", None)


def _dedupe_ok(key, interval):
    now = time.time()
    with _lock:
        last = _last_sent.get(key, 0)
        if now - last < max(0, int(interval or 0)):
            return False
        _last_sent[key] = now
        # 防止字典无限增长
        if len(_last_sent) > 600:
            for k in sorted(_last_sent, key=_last_sent.get)[:200]:
                _last_sent.pop(k, None)
    return True


def _deliver(title, text, level, name, event, doc, rec):
    """后台线程：真正发网络请求，然后回填告警历史的 sent 标记。"""
    import notify
    alerts = _alerts()
    # 统一标题：多渠道与告警 Webhook 都用「DockerUpdate · 事件名」，
    # 不再退化成 "[INFO] 容器名"（用户收到的推送看不出发生了什么）。
    title = title if str(title).startswith("DockerUpdate") else "DockerUpdate · " + str(title)
    mode = doc["channel_mode"]
    results = []
    use_notify = mode in ("auto", "notify", "both")
    use_alert = mode in ("alert", "both")

    # ① v5.0 多渠道通知中心
    if use_notify:
        try:
            ncfg = notify.load()
            has = bool(ncfg.get("enabled") and
                       [x for x in ncfg.get("channels") or [] if x.get("enabled") and x.get("url")])
            if has:
                # notify.send 内部也读同一份开关，不会二次过滤
                r = notify.send(title, text, event, level, name)
                results.append(("多渠道 · %d 个渠道" % len(r.get("channels") or []),
                                bool(r["ok"]) and not r.get("skipped"),
                                r.get("message") or ""))
            elif mode == "auto":
                use_alert = True      # 多渠道没配 → 自动回退到告警页 Webhook
            else:
                results.append(("多渠道", False, "未配置启用渠道"))
        except Exception as exc:
            results.append(("多渠道", False, str(exc)[:100]))
            if mode == "auto":
                use_alert = True

    # ② 告警页通用 Webhook（原有能力，保持不变）
    if use_alert and alerts is not None:
        try:
            acfg = alerts.config()
            if acfg.get("enabled") and acfg.get("webhook_url"):
                ok, detail = alerts._send(title, text, level)
                results.append(("告警 Webhook", ok, str(detail)[:120]))
            else:
                results.append(("告警 Webhook", False, "未启用或未配置地址"))
        except Exception as exc:
            results.append(("告警 Webhook", False, str(exc)[:100]))

    sent = any(x[1] for x in results)
    detail = "；".join("%s：%s" % (a, c) for a, b, c in results) or "没有可用的推送渠道"
    if alerts is not None and rec is not None:
        try:
            with alerts.lock:
                for it in alerts.data["history"]:
                    if it is rec:
                        it["sent"] = sent
                        it["detail"] = detail
                        it["event"] = event
                        break
            alerts._save()
        except Exception:
            pass
    return {"sent": sent, "detail": detail, "channels": results}


def emit(event, title, text, level="info", name="面板", dedupe=True, force=False):
    """统一入口。返回 None 表示该事件被关闭 / 被去重，没有产生告警记录。

    force=True 用于「测试推送」等忽略开关的场景（仍写入历史）。
    """
    if event not in EVENT_IDS:
        return None
    doc = load()
    if not force and not enabled(event, doc):
        return None
    alerts = _alerts()
    cfg = alerts.config() if alerts is not None else {}
    if dedupe:
        interval = int(cfg.get("dedupe_interval", 300) or 0)
        if not _dedupe_ok("%s:%s:%s" % (event, level, name), interval):
            return None
    rec = None
    if alerts is not None:
        try:
            rec = alerts._record(level, event, name, text, False)
        except Exception:
            rec = None
    try:
        record_stat(event)
    except Exception:
        pass
    t = threading.Thread(target=_deliver,
                         args=(title, text, level, name, event, doc, rec),
                         daemon=True, name="du-event-" + str(event)[:16])
    t.start()
    return {"event": event, "level": level, "name": name, "message": text,
            "async": True, "channel_mode": doc["channel_mode"]}


def test(event=None):
    """告警页「测试此事件」：忽略该事件的开关，同步拿到逐渠道真实结果。"""
    ev = event or "check_done"
    if ev not in EVENT_IDS:
        return {"ok": False, "skipped": True, "message": "未知事件：" + str(ev), "event": ev}
    doc = load()
    title = "DockerUpdate · 测试推送"
    text = ("这是一条来自告警页的测试消息，用来验证「%s」这条推送通道是否可用；"
            "收到即表示渠道配置正确，可以放心开启该事件的推送。"
            % next((x[1] for x in EVENT_CATALOG if x[0] == ev), ev))
    alerts = _alerts()
    rec = None
    try:
        if alerts is not None:
            rec = alerts._record("info", ev, "面板", text, False)
        record_stat(ev)
    except Exception:
        pass
    r = _deliver(title, text, "info", "面板", ev, doc, rec)
    return {"ok": bool(r["sent"]), "skipped": not r["sent"],
            "message": r["detail"], "event": ev, "channels": r["channels"]}
