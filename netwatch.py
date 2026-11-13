"""DockerUpdate v6.0-r17 · 外网连通性监测（断网 / 恢复双向通知）

需求：外网断网、网络异常后要能发通知，恢复正常后也要发通知。

必须讲清的物理限制：**真正断网时，企业微信 / 钉钉 / 飞书 / Telegram 这些云端
Webhook 本身也发不出去**。所以这里做了三层，缺一不可：

  ① 断网瞬间 → 除走统一事件中心外，同步尝试「内网 / 局域网 Webhook」
     （局域网里的 HA、自建接收端、本地 Bark 等，不依赖外网），这是断网当下
     唯一可能真正送达的通道；
  ② 断网消息进 pending 队列；云端通道若没送达，恢复后自动补发汇总；
  ③ 恢复瞬间 → 立即推送「外网恢复」，并带上断网时长、当时各目标探测结果。

判定：所有探测目标连续 N 次全部 TCP 连接失败 = 断网；连续 M 次任一可达 = 恢复。
部分目标失败只记「部分异常」，不误报断网。探测默认用国内可直连的 HTTPS 端点，
不会因为 registry-1.docker.io 单个域名不通就误判整机断网。
"""
import json
import os
import socket
import threading
import time
import urllib.request
from datetime import datetime

import config
import events
import notice

CONFIG_FILE = os.path.join(config.DATA_DIR, "netwatch.json")

DEFAULTS = {
    "enabled": True,
    "targets": ["www.baidu.com:443", "www.qq.com:443", "223.5.5.5:443"],
    "interval_s": 60,
    "down_after": 3,
    "up_after": 2,
    "timeout_s": 3,
    "lan_webhook": "",          # 内网 / 局域网通知地址，断网时仍可达
    "queue_offline": True,      # 恢复后补发未送达的断网通知
    "notify_down": True,
    "notify_up": True,
}

_lock = threading.RLock()
_wake = threading.Event()
_started = False
_mgr = None


def _now():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _default_doc():
    return {
        "config": dict(DEFAULTS),
        "status": {
            "state": "unknown",          # online / offline / unknown
            "last_change": "",
            "last_probe": "",
            "fail_streak": 0,
            "ok_streak": 0,
            "reachable": 0,
            "target_total": len(DEFAULTS["targets"]),
            "down_since": "",
            "downtime_s": 0,
        },
        "targets": [],                   # 每个目标最近一次探测结果
        "pending": [],                   # 未确认送达的断网消息
        "events": [],                   # 最近 30 条监测事件
    }


def load():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    base = _default_doc()
    if isinstance(d, dict):
        cfg = d.get("config")
        if isinstance(cfg, dict):
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
            base["config"] = merged
        for k in ("status", "targets", "pending", "events"):
            if isinstance(d.get(k), type(base[k])):
                base[k] = d[k]
    return base


def save(doc):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp-%d" % threading.get_ident()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def config_of():
    return load()["config"]


def save_config(patch):
    """校验并保存；返回 (最终配置, 提示)。"""
    if not isinstance(patch, dict):
        raise ValueError("配置格式不正确")
    with _lock:
        doc = load()
        cfg = doc["config"]
        raw_targets = patch.get("targets", cfg["targets"])
        if isinstance(raw_targets, str):
            raw_targets = [x.strip() for x in raw_targets.replace("，", "\n").splitlines()]
        targets = []
        for t in (raw_targets or []):
            t = str(t).strip().replace("tcp://", "").replace("https://", "").replace("http://", "")
            if not t or t.startswith("#"):
                continue
            if ":" not in t:
                t += ":443"
            host, _, port = t.rpartition(":")
            if not host or not port.isdigit() or not (1 <= int(port) <= 65535):
                raise ValueError("探测目标格式应为 host:port，例如 www.baidu.com:443")
            if t not in targets:
                targets.append(t)
        if not targets:
            raise ValueError("至少保留一个探测目标")
        if len(targets) > 8:
            raise ValueError("最多 8 个探测目标")

        def _int(key, low, high):
            try:
                v = int(patch.get(key, cfg[key]))
            except (TypeError, ValueError):
                raise ValueError("%s 必须是数字" % key)
            return max(low, min(v, high))

        cfg.update({
            "enabled": bool(patch.get("enabled", cfg["enabled"])),
            "targets": targets[:8],
            "interval_s": _int("interval_s", 10, 3600),
            "down_after": _int("down_after", 1, 10),
            "up_after": _int("up_after", 1, 10),
            "timeout_s": _int("timeout_s", 1, 15),
            "lan_webhook": str(patch.get("lan_webhook", cfg["lan_webhook"])).strip()[:500],
            "queue_offline": bool(patch.get("queue_offline", cfg["queue_offline"])),
            "notify_down": bool(patch.get("notify_down", cfg["notify_down"])),
            "notify_up": bool(patch.get("notify_up", cfg["notify_up"])),
        })
        doc["config"] = cfg
        doc["status"]["target_total"] = len(targets)
        save(doc)
    _wake.set()
    return cfg, "外网监测设置已保存；下一次探测将在 %d 秒内进行" % cfg["interval_s"]


def _probe_one(target, timeout):
    host, _, port = target.rpartition(":")
    t0 = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return {"target": target, "ok": True, "ms": int((time.time() - t0) * 1000), "error": ""}
    except Exception as exc:
        return {"target": target, "ok": False, "ms": int((time.time() - t0) * 1000),
                "error": str(exc)[:110]}


def probe_once():
    """并发探测所有目标：断网时不用把 N 个超时串行累加（3 个目标原本最多等 9 秒）。"""
    from concurrent.futures import ThreadPoolExecutor
    cfg = config_of()
    targets = list(cfg["targets"])
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
        ts = list(ex.map(lambda t: _probe_one(t, cfg["timeout_s"]), targets))
    return {"ok": any(x["ok"] for x in ts), "reachable": sum(1 for x in ts if x["ok"]),
            "total": len(ts), "targets": ts, "time": _now()}


def _log(action, message, ok=True):
    """写进运行日志（用户要求「任何动作都要记录在日志里」）。"""
    if _mgr is None:
        return
    try:
        _mgr.state.add_history(action, "外网监测", message, ok)
    except Exception:
        pass


def _post_lan(cfg, title, text, level, event):
    """断网当下唯一可能送达的通道：同步 POST 到内网 / 局域网 Webhook。"""
    url = str(cfg.get("lan_webhook") or "").strip()
    if not url:
        return {"ok": False, "detail": "未配置内网通知地址"}
    c = notice.card(title, text, level, "NAS", event)
    payload = notice.build_payload(url, "", c)
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "DockerUpdate/6.0"},
            method="POST")
        with urllib.request.urlopen(req, timeout=min(4, cfg["timeout_s"])) as r:
            body = r.read(300).decode("utf-8", "ignore")
        return {"ok": True, "detail": "HTTP %s %s" % (r.status, body[:80])}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:140]}


def _event(title, text, level, event_id, enabled):
    if not enabled:
        return {"sent": False, "detail": "该事件的推送开关已关闭"}
    try:
        r = events.emit(event_id, title, text, level, "NAS", dedupe=False)
        return {"sent": True, "detail": "已提交统一事件中心（异步送达）" if r else "事件未触发"}
    except Exception as exc:
        return {"sent": False, "detail": str(exc)[:140]}


def _record(doc, kind, message, detail=""):
    doc["events"] = [{
        "time": _now(), "kind": kind, "message": message, "detail": detail,
    }] + list(doc.get("events") or [])[:29]


def _on_down(doc, cfg, probe):
    ts = _now()
    st = doc["status"]
    st["state"] = "offline"
    st["down_since"] = ts
    st["last_change"] = ts
    failed = ", ".join("%s(%s)" % (x["target"], x["error"][:40])
                       for x in probe["targets"] if not x["ok"]) or "全部探测目标"
    text = ("外网已断开或网络异常：连续 %d 次探测中 %d/%d 个目标不可达。\n"
            "失败目标：%s\n面板会继续按间隔重试，恢复后自动推送恢复通知。"
            % (cfg["down_after"], probe["total"] - probe["reachable"], probe["total"], failed))[:600]
    lan = _post_lan(cfg, "外网断网", text, "high", "net_down")
    cloud = _event("外网断网", text, "high", "net_down", cfg["notify_down"])
    doc["pending"] = [{
        "time": ts, "text": text, "lan": lan, "cloud": cloud,
        "failed_targets": failed,
    }]
    _record(doc, "offline", "外网断网，已尝试推送", "内网通道：%s；云端：%s" %
            (lan["detail"], cloud["detail"]))
    _log("外网断网", "外网断网 · %s；内网通知 %s" %
         (failed[:100], "成功" if lan["ok"] else "失败/未配置"), ok=False)
    return {"down": True, "lan": lan, "cloud": cloud}


def _on_up(doc, cfg, probe):
    ts = _now()
    st = doc["status"]
    prev = st.get("down_since") or ""
    try:
        downtime = int(time.time() - datetime.strptime(prev, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        downtime = 0
    downtime = max(0, downtime)
    st["state"] = "online"
    st["downtime_s"] = downtime
    st["down_since"] = ""
    st["last_change"] = ts
    ok_targets = ", ".join("%s %dms" % (x["target"], x["ms"])
                           for x in probe["targets"] if x["ok"]) or "—"
    text = ("外网已恢复：%d/%d 个探测目标可达（%s）。%s"
            % (probe["reachable"], probe["total"], ok_targets,
               ("本次断网持续约 %s。" % _dur(downtime)) if downtime else ""))[:600]
    res = _event("外网恢复", text, "info", "net_up", cfg["notify_up"])

    # 补发：断网时云端 / 内网都没送达的，恢复后合并成一条再推一次
    backfill = {"done": False, "detail": "无需补发"}
    pending = list(doc.get("pending") or [])
    if cfg["queue_offline"] and pending:
        unsent = [p for p in pending if not p.get("lan", {}).get("ok")]
        if unsent:
            p = unsent[-1]
            bf = ("此前外网断网通知未能送达，现补报：\n%s\n断网时间：%s · 持续约 %s"
                  % (str(p.get("text") or "")[:300], p.get("time", ""), _dur(downtime)))[:600]
            r = _event("外网断网（恢复后补报）", bf, "high", "net_down", cfg["notify_down"])
            backfill = {"done": True, "detail": r["detail"]}
            _record(doc, "backfill", "断网通知已补发", r["detail"])
    doc["pending"] = []
    _record(doc, "online", "外网恢复，已推送恢复通知", "云端：%s；补发：%s" %
            (res["detail"], backfill["detail"]))
    _log("外网恢复", "外网恢复 · 可达 %d/%d · 断网 %s" %
         (probe["reachable"], probe["total"], _dur(downtime)))
    return {"up": True, "cloud": res, "backfill": backfill}


def _dur(sec):
    sec = max(0, int(sec))
    if sec >= 3600:
        return "%d 小时 %d 分" % (sec // 3600, sec % 3600 // 60)
    if sec >= 60:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 秒" % sec


def evaluate(force_probe=None, notify=True):
    """一次探测 + 状态机推进。notify=False 用于纯自检，不触发推送。"""
    with _lock:
        doc = load()
        cfg = doc["config"]
        probe = force_probe or probe_once()
        st = doc["status"]
        st["last_probe"] = probe["time"]
        st["reachable"] = probe["reachable"]
        st["target_total"] = probe["total"]
        doc["targets"] = probe["targets"]

        out = {"state": st["state"], "changed": False}
        if not cfg["enabled"]:
            st["state"] = "paused"
            st["fail_streak"] = 0
            st["ok_streak"] = 0
            save(doc)
            out["state"] = "paused"
            out["probe"] = probe
            return out

        if probe["ok"]:
            st["fail_streak"] = 0
            st["ok_streak"] += 1
        else:
            st["ok_streak"] = 0
            st["fail_streak"] += 1

        if st["state"] in ("unknown", "paused"):
            # 首次探测只建立基线，不推「恢复」通知；连续失败仍按阈值判断网
            st["state"] = "online" if probe["ok"] else "unknown"
        if st["state"] in ("online", "unknown") and st["fail_streak"] >= cfg["down_after"]:
            r = _on_down(doc, cfg, probe) if notify else {"down": True}
            out.update(r)
            out["changed"] = True
        elif st["state"] == "offline" and st["ok_streak"] >= cfg["up_after"]:
            r = _on_up(doc, cfg, probe) if notify else {"up": True}
            out.update(r)
            out["changed"] = True

        out["state"] = st["state"]
        out["probe"] = probe
        save(doc)
        return out


def status():
    with _lock:
        doc = load()
        cfg = doc["config"]
        st = doc["status"]
        return {
            "ok": True,
            "config": cfg,
            "status": st,
            "targets": doc.get("targets") or [],
            "pending": doc.get("pending") or [],
            "events": doc.get("events") or [],
            "running": _started,
            "limits": (
                "断网时企业微信 / 钉钉等云端 Webhook 通常也不可达：面板会同步尝试"
                "「内网通知地址」，并在恢复后自动补发未送达的断网通知。"
                "要有断网当下就收到的效果，请把内网通知地址指向局域网内可达的接收端。"),
        }


def test(event="net_down"):
    ev = "net_up" if str(event) == "net_up" else "net_down"
    return events.test(ev)


def start(mgr=None):
    """后台监测线程；server.main() 调用一次。"""
    global _started, _mgr
    _mgr = mgr
    with _lock:
        if _started:
            return
        _started = True

    def loop():
        while True:
            try:
                cfg = config_of()
                if cfg["enabled"]:
                    evaluate()
            except Exception as exc:
                print("[netwatch]", exc)
            wait = max(10, int(config_of().get("interval_s") or 60))
            _wake.wait(wait)
            _wake.clear()

    threading.Thread(target=loop, daemon=True, name="du-netwatch").start()
