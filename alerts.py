"""告警：CPU / 内存 / 磁盘阈值、容器生命周期、Webhook 推送、去重与历史。

v5.1：事件开关统一收敛到 events.py，告警页 Webhook 与 v5.0 多渠道通知
共用同一份配置；关闭的事件不推送也不写告警历史。
"""
import json
import os
import threading
import urllib.request
import urllib.error
from datetime import datetime

import config
import events


def _now():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _mask(u):
    """URL 脱敏：只留域名与路径，key/token 用 *** 代替。"""
    import re
    s = str(u or "")
    return re.sub(r"(key|token|secret|access_token)=([^&]+)", r"\1=***", s, flags=re.I) or "(未配置)"


class Alerts:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = dict(config.DEFAULT_ALERTS)
        self.data["history"] = []
        self._load()
        self.prev_running = None
        self.prev_all = None          # v5.1：上一轮全部容器名，用于识别新增 / 删除
        self.streak = {}              # key -> 连续超阈值次数
        self.last_sent = {}           # key -> timestamp（保留给旧逻辑 / 外部读取）

    def _load(self):
        try:
            with open(config.ALERTS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k in ("enabled", "webhook_url", "cpu_threshold", "mem_threshold",
                      "disk_threshold", "net_threshold", "container_down", "dedupe_interval"):
                if k in d:
                    self.data[k] = d[k]
            if isinstance(d.get("history"), list):
                self.data["history"] = d["history"][:100]
        except Exception:
            pass
        events.bind(self)   # v5.1：让统一事件中心能写回本实例的告警历史
        events.load()   # 触发旧配置迁移，首次启动即生成 events.json

    def _save(self):
        tmp = None
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            # v5.1：后台推送线程会并发回填历史，临时文件名必须唯一，
            # 否则两个线程共用 .tmp 时 os.replace 会报 "src -> dst 不存在"。
            tmp = "%s.%s.%d.tmp" % (config.ALERTS_FILE, os.getpid(), threading.get_ident())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, config.ALERTS_FILE)
        except Exception as exc:
            print("[alerts] 写入失败:", exc)
            try:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass

    def config(self):
        with self.lock:
            d = dict(self.data)
        ev = events.load()
        d["push_events"] = ev["events"]
        d["event_catalog"] = events.catalog()
        d["event_groups"] = list(events.GROUPS)
        d["channel_mode"] = ev["channel_mode"]
        d["channel_modes"] = [{"id": a, "label": b} for a, b in events.CHANNEL_MODES]
        d["event_count"] = len(events.EVENT_IDS)
        d["event_enabled_count"] = sum(1 for v in ev["events"].values() if v)
        d["event_stats"] = ev.get("stats") or {}
        return d

    def update(self, patch):
        with self.lock:
            d = self.data
            d["enabled"] = bool(patch.get("enabled", d["enabled"]))
            d["webhook_url"] = str(patch.get("webhook_url", d["webhook_url"])).strip()[:500]
            for k, lo, hi in (("cpu_threshold", 1, 100), ("mem_threshold", 1, 100),
                              ("disk_threshold", 1, 100), ("net_threshold", 64, 1048576),
                              ("dedupe_interval", 30, 86400)):
                if k in patch:
                    try:
                        v = int(patch[k])
                    except (TypeError, ValueError):
                        raise ValueError("%s 必须是数字" % k)
                    d[k] = max(lo, min(v, hi))
            d["container_down"] = bool(patch.get("container_down", d["container_down"]))
            self._save()
        # v5.1：事件开关与推送通道保存到 data/events.json
        ev = events.load()
        new_events = patch.get("push_events") or patch.get("events")
        if isinstance(new_events, dict):
            for k, v in new_events.items():
                if k in ev["events"]:
                    ev["events"][k] = bool(v)
        # 兼容旧界面的「容器意外退出」开关
        if "container_down" in patch:
            ev["events"]["container_down"] = bool(patch["container_down"])
        if "channel_mode" in patch:
            ev["channel_mode"] = str(patch["channel_mode"])
        events.save(ev)
        return self.config()

    def history(self):
        with self.lock:
            return list(self.data["history"])

    def _record(self, level, kind, name, message, sent):
        self._last_name, self._last_event = str(name or ""), str(kind or "")
        item = {"time": _now(), "level": level, "kind": kind,
                "name": name, "message": message, "sent": bool(sent)}
        with self.lock:
            self.data["history"].insert(0, item)
            self.data["history"] = self.data["history"][:100]
            self._save()
        return item

    def _send(self, title, message, level):
        """通用 JSON Webhook：企业微信/钉钉/飞书机器人及自建接口可直接用。"""
        url = self.data.get("webhook_url") or ""
        if not url:
            return False, "未配置 Webhook 地址"
        import notice
        c = notice.card(title, message, level, name=getattr(self, "_last_name", "") or "",
                        event=getattr(self, "_last_event", "") or "")
        # v6.0-r4：所有载荷统一由 notice.build_payload 构造。
        # 微信端只认 msgtype=text；判定不再依赖单一域名，自建中转也不会再带 markdown。
        payload = notice.build_payload(url, "wecom" if notice.is_wechat(url) else "", c)
        try:
            req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")[:200]
            return True, body
        except (urllib.error.URLError, OSError) as exc:
            return False, str(exc)[:200]

    def test(self, event=None):
        r = events.test(event)
        # 保持旧行为：一个可用渠道都没有时明确报错，避免「点了测试却以为推送成功」
        if not r.get("ok"):
            raise ValueError("测试推送失败：%s" % (r.get("message") or "没有可用的推送渠道"))
        return r

    def _emit(self, level, kind, name, message):
        """旧签名兼容层：kind 直接当事件 id 用（cpu/mem/disk 映射到新 id）。"""
        emap = {"cpu": "cpu_high", "mem": "mem_high", "disk": "disk_high",
                "container-down": "container_down"}
        ev = emap.get(kind, kind)
        labels = {"cpu_high": "CPU 超阈值", "mem_high": "内存超阈值",
                  "disk_high": "磁盘超阈值", "container_down": "容器意外退出"}
        # v6.0-r8：标题只放事件名，不再拼 "[HIGH] 容器名"，图标由事件类型决定。
        return events.emit(ev, labels.get(ev, "告警通知"), message, level, name)

    def evaluate(self, host, containers, metrics):
        """每轮采样后调用；返回本轮产生的告警条数。

        v5.1：在原有阈值 / 意外退出之外，识别新增、删除、启动、停止四类生命周期事件。
        轮询只能看到状态变化，手动点按钮的即时事件由 console.action 直接 emit，
        去重键相同，所以不会重复推送两条。
        """
        cfg = self.config()
        pe = cfg["push_events"]
        made = 0
        cur_all = {c["name"] for c in containers}
        cur_running = {c["name"] for c in containers if c.get("running")}

        if self.prev_all is not None:
            for name in sorted(cur_all - self.prev_all):
                if pe.get("container_added") and events.emit(
                        "container_added", "新增容器",
                        "检测到新容器「%s」已出现在本机 Docker 中" % name,
                        "info", name):
                    made += 1
            for name in sorted(self.prev_all - cur_all):
                if pe.get("container_removed") and events.emit(
                        "container_removed", "删除容器",
                        "容器「%s」已从本机 Docker 中移除" % name,
                        "mid", name):
                    made += 1
            for name in sorted((cur_running & self.prev_all) - (self.prev_running or set())):
                if pe.get("container_started") and events.emit(
                        "container_started", "容器启动",
                        "容器「%s」已启动运行" % name, "info", name):
                    made += 1
            # 停止 / 意外退出：两个开关只发一条，优先「容器停止」
            for name in sorted(((self.prev_running or set()) & cur_all) - cur_running):
                ev = "container_stopped" if pe.get("container_stopped") else (
                    "container_down" if pe.get("container_down") else "")
                if ev and events.emit(ev, "容器停止" if ev == "container_stopped" else "容器意外退出",
                                      "容器「%s」已停止运行%s" %
                                      (name, "" if ev == "container_stopped" else "（可能意外退出）"),
                                      "high" if ev == "container_down" else "mid", name):
                    made += 1
        self.prev_all = cur_all
        self.prev_running = cur_running

        # 连续 3 次超阈值才告警，避免瞬时抖动；关闭的事件直接跳过采样累计
        for name, m in metrics.items():
            for kind, key, label, val, thr, unit in (
                ("cpu_high", "cpu_threshold", "CPU", m.get("cpu", 0), cfg["cpu_threshold"], "%"),
                ("mem_high", "mem_threshold", "内存", m.get("mem_percent", 0), cfg["mem_threshold"], "%"),
            ):
                if not pe.get(kind):
                    continue
                skey = "%s:%s" % (kind, name)
                if val >= thr:
                    self.streak[skey] = self.streak.get(skey, 0) + 1
                    if self.streak[skey] >= 3:
                        if self._emit("mid", "cpu" if kind == "cpu_high" else "mem", name,
                                      "%s %s 占用 %.1f%s，连续 3 次超过阈值 %s%s"
                                      % (name, label, val, unit, thr, unit)):
                            made += 1
                        self.streak[skey] = 0
                else:
                    self.streak[skey] = 0

        # v5.1：网络流量异常（上下行合计 KB/s，同样连续 3 次才触发）
        if pe.get("net_high"):
            for name, m in metrics.items():
                val = (float(m.get("net_rx", 0)) + float(m.get("net_tx", 0))) / 1024.0
                skey = "net_high:%s" % name
                if val >= float(cfg.get("net_threshold", 20480)):
                    self.streak[skey] = self.streak.get(skey, 0) + 1
                    if self.streak[skey] >= 3:
                        if events.emit("net_high", "网络流量异常",
                                      "容器「%s」上下行合计 %.1f KB/s，连续 3 次超过阈值 %s KB/s"
                                      % (name, val, cfg.get("net_threshold", 20480)),
                                      "mid", name):
                            made += 1
                        self.streak[skey] = 0
                else:
                    self.streak[skey] = 0

        disk = float(host.get("disk_percent", 0))
        if pe.get("disk_high") and disk >= float(cfg["disk_threshold"]):
            if self._emit("high", "disk", "宿主机",
                          "磁盘使用率 %.1f%%，已超过阈值 %s%%" % (disk, cfg["disk_threshold"])):
                made += 1
        return made


    @staticmethod
    def _mask(u):
        return _mask(u)

    def selfcheck(self):
        """v6.0-r4：返回「本进程实际会发出去」的载荷，供告警页一键核对。"""
        import notice as _n
        url = self.data.get("webhook_url") or ""
        c = _n.card("DockerUpdate · 通知自检", "这是一条用于核对消息类型的测试内容。",
                    "info", name="面板", event="check_done")
        p = _n.build_payload(url, "wecom" if _n.is_wechat(url) else "", c)
        return {
            "enabled": bool(self.data.get("enabled")),
            "url_masked": _mask(url),
            "is_wechat": _n.is_wechat(url),
            "payload": p,
            "has_markdown": "markdown" in p,
            "msgtype": p.get("msgtype"),
        }

