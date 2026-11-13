"""容器资源监控：并发采集 Docker stats、按小时汇总趋势、Top5 与优化建议。"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
from docker_api import DockerError
from sysinfo import HostSampler


def _now():
    return datetime.now(config.LOCAL_TZ)


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _started_seconds(started_at):
    if not started_at:
        return 0
    try:
        head = started_at.split("+")[0].rstrip("Z").split(".")[0]
        from datetime import timezone
        dt = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return 0


class Monitor:
    def __init__(self, docker, host=None):
        self.docker = docker
        self.host = host or HostSampler()
        self.lock = threading.RLock()
        self.latest = {}            # name -> metrics
        self.series = {}            # name -> 最近 60 个采样点
        self.prev = {}              # name -> (at, rx, tx, io_r, io_w)
        self.history = []           # 小时汇总
        self.started_at = _now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_sample_at = ""
        self.sample_count = 0
        self.host_latest = {}
        self.errors = []
        self._cur_hour = ""
        self._acc = {}
        self._thread = None
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(config.METRICS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.history = d.get("history", [])[-config.TREND_HOURS:]
            self.started_at = d.get("started_at", self.started_at)
            self.sample_count = int(d.get("sample_count", 0))
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            tmp = config.METRICS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"history": self.history, "started_at": self.started_at,
                           "sample_count": self.sample_count}, f, ensure_ascii=False)
            os.replace(tmp, config.METRICS_FILE)
        except Exception as exc:
            print("[metrics] 写入失败:", exc)

    # ---------- 启动 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self.sample_once()
            except Exception as exc:
                self.errors.append(str(exc)[:200])
                self.errors = self.errors[-5:]
            time.sleep(config.SAMPLE_INTERVAL)

    # ---------- 单次采样 ----------
    def _one(self, c, host):
        cid = c.get("Id", "")
        names = c.get("Names") or []
        name = names[0].lstrip("/") if names else cid[:12]
        try:
            st = self.docker.stats(cid, timeout=10)
        except DockerError:
            return None
        if not isinstance(st, dict):
            return None
        cpu = st.get("cpu_stats") or {}
        precpu = st.get("precpu_stats") or {}
        usage = _num((cpu.get("cpu_usage") or {}).get("total_usage"))
        pre_usage = _num((precpu.get("cpu_usage") or {}).get("total_usage"))
        system = _num(cpu.get("system_cpu_usage"))
        pre_system = _num(precpu.get("system_cpu_usage"))
        online = _num(cpu.get("online_cpus"), self.host.cores) or self.host.cores
        d_cpu, d_sys = usage - pre_usage, system - pre_system
        # 相对单核 100% 的口径（docker stats 同款）：多核容器可超过 100%
        cpu_pct = round(max(0.0, min(d_cpu / d_sys * 100, 100 * online)), 1) if d_sys > 0 and d_cpu > 0 else 0.0

        memst = st.get("memory_stats") or {}
        mem_usage = _num(memst.get("usage"))
        mem_limit = _num(memst.get("limit")) or _num(host.get("mem_total"))
        cache = _num((memst.get("stats") or {}).get("inactive_file"))
        mem_used = max(0.0, mem_usage - cache)
        mem_pct = round(mem_used / mem_limit * 100, 1) if mem_limit else 0.0

        rx = tx = 0.0
        for n in (st.get("networks") or {}).values():
            if isinstance(n, dict):
                rx += _num(n.get("rx_bytes"))
                tx += _num(n.get("tx_bytes"))
        io_r = io_w = 0.0
        for item in ((st.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("op", "")).lower() == "read":
                io_r += _num(item.get("value"))
            elif str(item.get("op", "")).lower() == "write":
                io_w += _num(item.get("value"))

        now = time.time()
        with self.lock:
            prev = self.prev.get(name)
            rate_rx = rate_tx = rate_r = rate_w = 0.0
            if prev and now > prev[0]:
                dt = now - prev[0]
                rate_rx = max(0, (rx - prev[1]) / dt)
                rate_tx = max(0, (tx - prev[2]) / dt)
                rate_r = max(0, (io_r - prev[3]) / dt)
                rate_w = max(0, (io_w - prev[4]) / dt)
            self.prev[name] = (now, rx, tx, io_r, io_w)

        created = _num(c.get("Created"))
        uptime = max(0, int(time.time() - created)) if created else 0
        return {
            "name": name, "id": cid[:12], "uptime": uptime,
            "cpu": cpu_pct, "mem": int(mem_used / 1048576),
            "mem_percent": mem_pct, "mem_limit": int(mem_limit / 1048576),
            "net_rx": round(rate_rx, 1), "net_tx": round(rate_tx, 1),
            "net_rx_total": int(rx), "net_tx_total": int(tx),
            "io_read": round(rate_r, 1), "io_write": round(rate_w, 1),
            "pids": int(_num((st.get("pids_stats") or {}).get("current"))),
            "at": now,
        }

    def sample_once(self):
        raw = self.docker.list_containers(all_=False) or []
        host = self.host.sample()   # 整机 CPU 差值每轮只算一次，避免污染
        results = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(self._one, c, host): c for c in raw}
            for fut in as_completed(futs):
                try:
                    m = fut.result()
                    if m:
                        results[m["name"]] = m
                except Exception:
                    pass
        now = _now()
        with self.lock:
            self.latest = results
            self.host_latest = host
            for name, m in results.items():
                arr = self.series.setdefault(name, [])
                arr.append({"t": now.strftime("%H:%M:%S"), "cpu": m["cpu"],
                            "mem": m["mem"], "rx": m["net_rx"], "tx": m["net_tx"]})
                if len(arr) > 60:
                    self.series[name] = arr[-60:]
            self.last_sample_at = now.strftime("%Y-%m-%d %H:%M:%S")
            self.sample_count += 1
            self._aggregate(now, host, results)
        return results

    @staticmethod
    def _hour_point(a, hour_key):
        n = max(1, a.get("n", 1))
        rx = a.get("rx", 0) / n
        tx = a.get("tx", 0) / n
        # 速率 × 采样间隔 ≈ 该小时累计流量（MB）；采样间隔取设置值
        span = config.SAMPLE_INTERVAL * n / 1048576
        return {
            "t": hour_key + ":00",
            "cpu": round(a.get("cpu", 0) / n, 1),
            "mem": round(a.get("mem", 0) / n, 1),
            "disk": round(a.get("disk", 0) / n, 1),
            "net": round(a.get("net", 0) / n / 1048576, 2),  # 兼容旧字段：平均速率 MB/s
            "net_rx": round(rx, 1),       # 平均下行速率 B/s
            "net_tx": round(tx, 1),       # 平均上行速率 B/s
            "net_down": round(rx * span, 2),  # 该小时累计下行 MB
            "net_up": round(tx * span, 2),    # 该小时累计上行 MB
            "mem_gb": round(a.get("mem_gb", 0) / n, 2),
            "containers": round(a.get("containers", 0) / n, 0),
            "samples": n,
        }

    def _aggregate(self, now, host, results):
        hour_key = now.strftime("%Y-%m-%d %H")
        if self._cur_hour and hour_key != self._cur_hour and self._acc.get("n"):
            self.history.append(self._hour_point(self._acc, self._cur_hour))
            self.history = self.history[-config.TREND_HOURS:]
            self._save()
            self._acc = {}
        if hour_key != self._cur_hour:
            self._cur_hour = hour_key
        a = self._acc
        a["n"] = a.get("n", 0) + 1
        a["cpu"] = a.get("cpu", 0) + host.get("cpu_percent", 0)
        a["mem"] = a.get("mem", 0) + host.get("mem_percent", 0)
        a["disk"] = a.get("disk", 0) + host.get("disk_percent", 0)
        a["rx"] = a.get("rx", 0) + host.get("net_rx_bps", 0)
        a["tx"] = a.get("tx", 0) + host.get("net_tx_bps", 0)
        a["net"] = a.get("net", 0) + host.get("net_rx_bps", 0) + host.get("net_tx_bps", 0)
        a["mem_gb"] = a.get("mem_gb", 0) + host.get("mem_used", 0) / 1073741824
        a["containers"] = a.get("containers", 0) + len(results)

    # ---------- 对外数据 ----------
    def metrics(self):
        with self.lock:
            return {k: dict(v) for k, v in self.latest.items()}

    def series_for(self, name):
        with self.lock:
            return list(self.series.get(name, []))

    def trend(self):
        with self.lock:
            pts = list(self.history)
            # 补上当前未结小时，让曲线末端有实时感
            if self._acc.get("n"):
                pts.append(self._hour_point(self._acc, _now().strftime("%Y-%m-%d %H")))
            return pts[-config.TREND_HOURS:]

    def summary(self):
        hist = self.trend()
        last24 = hist[-24:]
        cpu_list = [p["cpu"] for p in last24] or [0]
        net_list = [p["net"] for p in last24] or [0]
        return {
            "started_at": self.started_at,
            "last_sample_at": self.last_sample_at,
            "sample_interval": config.SAMPLE_INTERVAL,
            "sample_count": self.sample_count,
            "points": len(hist),
            "cpu_avg": round(sum(cpu_list) / len(cpu_list), 1),
            "cpu_peak": round(max(cpu_list), 1),
            "net_peak": round(max(net_list), 2),
            "errors": list(self.errors),
        }

    def top(self, containers):
        m = self.metrics()
        running = [c for c in containers if c["running"]]
        by_cpu = sorted(running, key=lambda c: m.get(c["name"], {}).get("cpu", 0), reverse=True)[:5]
        by_mem = sorted(running, key=lambda c: m.get(c["name"], {}).get("mem", 0), reverse=True)[:5]
        by_net = sorted(running, key=lambda c: (m.get(c["name"], {}).get("net_rx", 0)
                                                + m.get(c["name"], {}).get("net_tx", 0)), reverse=True)[:5]
        by_up = sorted(running, key=lambda c: m.get(c["name"], {}).get("uptime", 0), reverse=True)[:5]
        return {
            "cpu": [{"name": c["name"], "alias": c.get("alias") or c["name"],
                     "value": m.get(c["name"], {}).get("cpu", 0), "extra": m.get(c["name"], {}).get("mem", 0)}
                    for c in by_cpu],
            "mem": [{"name": c["name"], "alias": c.get("alias") or c["name"],
                     "value": m.get(c["name"], {}).get("mem", 0), "extra": m.get(c["name"], {}).get("mem_percent", 0)}
                    for c in by_mem],
            "net": [{"name": c["name"], "alias": c.get("alias") or c["name"],
                     "value": round(m.get(c["name"], {}).get("net_rx", 0) + m.get(c["name"], {}).get("net_tx", 0), 1),
                     "extra": m.get(c["name"], {}).get("net_tx", 0)} for c in by_net],
            "uptime": [{"name": c["name"], "alias": c.get("alias") or c["name"],
                        "value": m.get(c["name"], {}).get("uptime", 0), "extra": 0} for c in by_up],
        }

    def suggestions(self, containers, images):
        out = []
        m = self.metrics()
        for c in containers:
            name = c.get("name", "")
            hc = c.get("_hc") or {}
            if c.get("privileged") or hc.get("Privileged"):
                out.append({"level": "high", "title": "存在特权容器", "name": name,
                            "detail": "privileged 几乎等同宿主机 root，非必要请关闭。"})
            binds = c.get("volumes") or []
            if any("/var/run/docker.sock" in str(b) for b in binds) and not c.get("excluded"):
                out.append({"level": "high", "title": "挂载了 Docker Socket", "name": name,
                            "detail": "该容器可完全控制宿主机 Docker，确认来源可信。"})
            if not c.get("restart_policy") or c["restart_policy"] in ("no", ""):
                if c.get("running"):
                    out.append({"level": "mid", "title": "未设置重启策略", "name": name,
                                "detail": "意外退出或 NAS 重启后不会自动拉起，建议 unless-stopped。"})
            if c.get("mem_limit_mb", 0) == 0 and c.get("running"):
                out.append({"level": "low", "title": "未限制内存上限", "name": name,
                            "detail": "可在容器详情里设置内存与 CPU 上限，避免单应用吃满整机。"})
            mc = m.get(name, {})
            if mc.get("cpu", 0) >= 80:
                out.append({"level": "mid", "title": "CPU 占用偏高", "name": name,
                            "detail": "当前 CPU %.1f%%，可查看资源曲线确认是否持续。" % mc["cpu"]})
            if mc.get("mem_percent", 0) >= 90:
                out.append({"level": "mid", "title": "内存接近上限", "name": name,
                            "detail": "当前内存使用率 %.1f%%，有 OOM 风险。" % mc["mem_percent"]})
            if not c.get("running") and c.get("status") not in ("created",):
                out.append({"level": "low", "title": "存在已停止的容器", "name": name,
                            "detail": "长期不用的容器可清理，减少镜像与磁盘占用。"})
        dangling = sum(1 for i in images if not (i.get("tags") or []))
        if dangling:
            out.append({"level": "low", "title": "%d 个悬空镜像" % dangling, "name": "镜像",
                        "detail": "可在「本地镜像」里一键清理悬空镜像释放磁盘。"})
        order = {"high": 0, "mid": 1, "low": 2}
        out.sort(key=lambda x: order.get(x["level"], 3))
        return out[:40]
