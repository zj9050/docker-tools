"""宿主机指标采集：读取只读挂载的 /host/proc 与 statvfs，无第三方依赖。"""
import os
import socket
import time

import config


def _read(path):
    """优先读宿主机 /host/<path>，挂载不存在时回退容器内 /<path>。"""
    primary = os.path.join(config.HOST_ROOT, path.lstrip("/"))
    fallback = "/" + path.lstrip("/")
    for p in (primary, fallback):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            continue
    return ""


def _first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class HostSampler:
    """维护 /proc/stat 上一次读数，用于计算整机 CPU 使用率与网卡速率。"""

    def __init__(self):
        self.prev_cpu = None
        self.prev_net = None
        self.prev_net_at = 0.0
        self.cores = self._count_cores()
        self.model = self._cpu_model()
        self.hostname = (self._read_hostname() or socket.gethostname())
        self.ip = config.HOST_IP or self._guess_ip()

    @staticmethod
    def _read_hostname():
        h = _read("/etc/hostname").strip()
        return h or ""

    @staticmethod
    def _guess_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname())
            except OSError:
                return ""

    def _count_cores(self):
        txt = _read("/proc/cpuinfo")
        n = sum(1 for l in txt.splitlines() if l.startswith("processor"))
        return n or os.cpu_count() or 1

    def _cpu_model(self):
        txt = _read("/proc/cpuinfo")
        for l in txt.splitlines():
            if l.lower().startswith("model name") or l.lower().startswith("hardware"):
                if ":" in l:
                    return l.split(":", 1)[1].strip()
        return "未知 CPU"

    @staticmethod
    def _cpu_ticks():
        line = _first_line(_read("/proc/stat"))
        if not line.startswith("cpu"):
            return None
        parts = line.split()[1:]
        try:
            vals = [int(x) for x in parts[:8]]
        except ValueError:
            return None
        while len(vals) < 8:
            vals.append(0)
        total = sum(vals)
        idle = vals[3] + vals[4]
        return total, idle

    @staticmethod
    def _meminfo():
        out = {}
        for line in _read("/proc/meminfo").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                try:
                    out[k.strip()] = int(v.strip().split()[0]) * 1024
                except (ValueError, IndexError):
                    pass
        return out

    @staticmethod
    def _net_bytes():
        total_rx = total_tx = 0
        for line in _read("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            if iface.strip() == "lo":
                continue
            parts = rest.split()
            if len(parts) >= 9:
                try:
                    total_rx += int(parts[0])
                    total_tx += int(parts[8])
                except ValueError:
                    pass
        return total_rx, total_tx

    @staticmethod
    def _disks():
        out = []
        seen = set()
        for raw in config.DISK_PATHS:
            path = raw if os.path.isabs(raw) else os.path.join(config.HOST_ROOT, raw.lstrip("/"))
            if not os.path.isdir(path) or path in seen:
                continue
            seen.add(path)
            try:
                st = os.statvfs(path)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                used = total - free
                out.append({
                    "path": raw, "total": total, "used": used, "free": free,
                    "percent": round(used / total * 100, 1) if total else 0,
                })
            except OSError:
                continue
        return out

    @staticmethod
    def _uptime():
        try:
            return float(_read("/proc/uptime").split()[0])
        except (ValueError, IndexError):
            return 0.0

    @staticmethod
    def _loadavg():
        try:
            return [float(x) for x in _read("/proc/loadavg").split()[:3]]
        except (ValueError, IndexError):
            return [0.0, 0.0, 0.0]

    def sample(self):
        """返回整机指标；CPU% 与网卡速率基于两次采样差值。"""
        now = time.time()
        cpu = self._cpu_ticks()
        cpu_pct = 0.0
        if cpu and self.prev_cpu:
            dt, di = cpu[0] - self.prev_cpu[0], cpu[1] - self.prev_cpu[1]
            if dt > 0:
                cpu_pct = max(0.0, min(100.0, (1 - di / dt) * 100))
        if cpu:
            self.prev_cpu = cpu

        mem = self._meminfo()
        total_mem = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_mem = max(0, total_mem - avail)
        swap_total = mem.get("SwapTotal", 0)
        swap_used = max(0, swap_total - mem.get("SwapFree", swap_total))

        net_rx_bps = net_tx_bps = 0.0
        net = self._net_bytes()
        if self.prev_net is not None and now > self.prev_net_at:
            dt = now - self.prev_net_at
            net_rx_bps = max(0, (net[0] - self.prev_net[0]) / dt)
            net_tx_bps = max(0, (net[1] - self.prev_net[1]) / dt)
        self.prev_net, self.prev_net_at = net, now

        disks = self._disks()
        disk_total = sum(d["total"] for d in disks)
        disk_used = sum(d["used"] for d in disks)
        disk_free = sum(d["free"] for d in disks)

        return {
            "cores": self.cores,
            "cpu_model": self.model,
            "hostname": self.hostname,
            "ip": self.ip,
            "cpu_percent": round(cpu_pct, 1),
            "mem_total": total_mem,
            "mem_used": used_mem,
            "mem_percent": round(used_mem / total_mem * 100, 1) if total_mem else 0,
            "swap_total": swap_total,
            "swap_used": swap_used,
            "loadavg": self._loadavg(),
            "uptime": self._uptime(),
            "net_rx_bps": net_rx_bps,
            "net_tx_bps": net_tx_bps,
            "disks": disks,
            "disk_total": disk_total,
            "disk_used": disk_used,
            "disk_free": disk_free,
            "disk_percent": round(disk_used / disk_total * 100, 1) if disk_total else 0,
            "sample_at": now,
        }
