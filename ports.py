"""DockerUpdate v6.0-r19 · 整机端口占用扫描（聚合去重 + 容器 / 镜像归属）

数据源：宿主机 /proc/net/{tcp,tcp6,udp,udp6}（通过 /:/host:ro 只读挂载读 /host/proc）。
不依赖 ss / netstat / lsof 等外部二进制，纯标准库；读不到 /host/proc 时回退容器内 /proc，
并在结果里如实标注 source，不伪造整机视角。

v6.0-r19 修复：
  1. 旧版把每条 socket 都列一行，TIME_WAIT / ESTABLISHED 让同一个端口重复几十次。
     现在 rows 按 (协议, 端口) 聚合成一行，连接态折叠成 connections 计数 / states 分布，
     单独放在 connections 字段里，前端默认不展示。
  2. 归属识别补强：Docker 端口映射 → 容器名 + 镜像名 + 容器内端口；
     /proc/<pid>/fd + cgroup → 宿主进程名 / PID / 容器 ID（host 网络容器也能认出来）；
     实在认不出来的才写「系统 / 宿主进程」。
  3. IPv4 / IPv6 双栈、0.0.0.0 与具体网卡上的同一端口合并为一行，监听地址全列出。

只做读取与展示，绝不 kill 进程、绝不改防火墙、绝不占用端口。
"""
import os
import re
import socket
import threading
import time
from collections import Counter
from datetime import datetime

import config

STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING", "0C": "NEW_SYN_RECV",
}
LISTEN = "0A"

PROC_TABLES = (
    ("tcp", "tcp", 4), ("tcp6", "tcp", 6),
    ("udp", "udp", 4), ("udp6", "udp", 6),
)

# /proc/<pid>/cgroup 里的容器 ID：docker-<64hex>.scope、/docker/<64hex>、crio-<64hex> …
_CGROUP_ID = re.compile(
    r"(?:docker|crio|containerd|kubepods)[-_/.]([0-9a-f]{64})(?:\.scope|\.slice)?",
    re.I)

_lock = threading.RLock()
_cache = {"ts": 0.0, "data": None}
CACHE_TTL = 4.0


def _now():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _proc_sources():
    """v7.0.3：返回 (proc_root, net_dir, scope)。

    /proc/net 是「当前进程所属 netns」的视图（内核里等价于 /proc/self/net），
    即使通过 /:/host:ro 把宿主机根挂进容器，读 /host/proc/net/tcp 拿到的
    仍然是**面板容器自己**的网络命名空间——所以旧版只看到 10000 与
    127.0.0.11（Docker 内嵌 DNS），还把 source 误标成整机视角。
    只有 /proc/<pid>/net 才按该 PID 的 netns 解析；宿主机 PID 1 的 netns
    才是整机视角。优先读 /host/proc/1/net，逐级回退并如实标注。
    """
    host_proc = os.path.join(config.HOST_ROOT, "proc")
    candidates = (
        (host_proc, os.path.join(host_proc, "1", "net"),
         "整机视角 · 宿主机 PID 1 网络命名空间（/host/proc/1/net）"),
        (host_proc, os.path.join(host_proc, "net"),
         "宿主机 /proc/net（注意：可能仍是面板容器 netns）"),
        ("/proc", os.path.join("/proc", "1", "net"),
         "本机 PID 1 网络命名空间"),
        ("/proc", os.path.join("/proc", "net"),
         "仅面板容器可见（未挂载 /host）"),
    )
    for proc_root, net_dir, scope in candidates:
        if os.path.exists(os.path.join(net_dir, "tcp")):
            return proc_root, net_dir, scope
    return "/proc", os.path.join("/proc", "net"), "无法读取 /proc/net"


def _proc_root():
    """兼容旧调用：只返回 proc 根。"""
    return _proc_sources()[0]


def _addr(hexaddr, version):
    """把 /proc/net/tcp 的十六进制地址转成可读 IP。"""
    try:
        ip_hex, port_hex = hexaddr.split(":")
        port = int(port_hex, 16)
        if version == 4:
            raw = bytes.fromhex(ip_hex)[::-1]
            ip = socket.inet_ntop(socket.AF_INET, raw)
        else:
            # tcp6 是 4 组 32bit，每组内部为小端
            raw = b"".join(bytes.fromhex(ip_hex[i:i + 8])[::-1]
                           for i in range(0, 32, 8))
            ip = socket.inet_ntop(socket.AF_INET6, raw)
            if ip == "::":
                ip = "::"
        return ip, port
    except Exception:
        return "", 0


def _read_table(net_dir, name, proto, version):
    rows = []
    path = os.path.join(net_dir, name)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[1:]
    except (OSError, IOError):
        return rows
    for ln in lines:
        p = ln.split()
        if len(p) < 10:
            continue
        ip, port = _addr(p[1], version)
        if not port:
            continue
        rows.append({
            "port": port,
            "proto": proto,
            "ip_version": version,
            "state_code": p[3].upper(),
            "state": STATES.get(p[3].upper(), p[3].upper()),
            "local": ip,
            "inode": p[9],
        })
    return rows


def _docker_meta(docker):
    """Docker Engine 容器列表 → (端口映射, ID 索引, 名称索引)。

    端口映射值含容器名、镜像名、容器 ID 与容器内端口，用于把「谁占用」
    直接写成「容器 · nginx-proxy（nginx:1.27）→80」。
    """
    port_map, by_id, by_name = {}, {}, {}
    try:
        items = docker.list_containers() or []
    except Exception:
        items = []
    for c in items:
        name = ""
        for n in (c.get("Names") or []):
            name = str(n).lstrip("/")
            if name:
                break
        cid = str(c.get("Id") or "")
        if not name:
            name = cid[:12] or "unknown"
        image = str(c.get("Image") or c.get("ImageID") or "").strip()
        meta = {"name": name, "image": image, "id": cid, "private_port": None}
        if cid:
            by_id[cid[:12]] = meta
            by_id[cid] = meta
        by_name[name] = meta
        for p in (c.get("Ports") or []):
            hp = p.get("PublicPort")
            if not hp:
                continue
            key = (str(p.get("Type") or "tcp").lower(), int(hp))
            m = dict(meta)
            m["private_port"] = p.get("PrivatePort")
            port_map.setdefault(key, [])
            if not any(x["name"] == name for x in port_map[key]):
                port_map[key].append(m)
    return port_map, by_id, by_name


def _container_ports(docker):
    """兼容旧接口：{(proto, port): [容器名...]}"""
    port_map, _, _ = _docker_meta(docker)
    return {k: [m["name"] for m in v] for k, v in port_map.items()}


def _cgroup_container(base, pid, by_id):
    """从 /proc/<pid>/cgroup 找容器 ID，返回 (容器名, 镜像名, 短 ID)。"""
    try:
        with open(os.path.join(base, pid, "cgroup"), "r",
                  encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return "", "", ""
    m = _CGROUP_ID.search(txt)
    if not m:
        return "", "", ""
    cid = m.group(1)
    meta = by_id.get(cid) or by_id.get(cid[:12])
    if meta:
        return meta["name"], meta.get("image", ""), cid[:12]
    return ("容器 " + cid[:12]), "", cid[:12]


def _process_names(root, inodes, budget_s=2.0, by_id=None):
    """inode → {pid, process, container, image, cid}。只读 /proc，超时或没权限就留空。"""
    result = {}
    if not inodes:
        return result
    by_id = by_id or {}
    deadline = time.time() + budget_s
    base = os.path.join(root, "")
    try:
        pids = [x for x in os.listdir(base) if x.isdigit()]
    except OSError:
        return result
    for pid in pids[:1500]:
        if time.time() > deadline or len(result) >= len(inodes):
            break
        fddir = os.path.join(base, pid, "fd")
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue
        hit = None
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if not link.startswith("socket:["):
                continue
            ino = link[8:-1]
            if ino in inodes and ino not in result:
                hit = ino
                break
        if not hit:
            continue
        try:
            with open(os.path.join(base, pid, "comm"), "r",
                      encoding="utf-8", errors="replace") as f:
                comm = f.read().strip()
        except OSError:
            comm = ""
        cname, cimage, cid = _cgroup_container(base, pid, by_id)
        result[hit] = {"pid": int(pid), "process": comm,
                       "container": cname, "image": cimage, "cid": cid}
    return result


def _primary_addr(addresses):
    """同一端口多个监听地址时，优先展示通配地址。"""
    for want in ("0.0.0.0", "::"):
        if want in addresses:
            return want
    return sorted(addresses)[0] if addresses else ""


def _owner(metas, processes, pids, cgroup_metas):
    allm = {}
    for m in metas + cgroup_metas:
        if m.get("name"):
            allm[m["name"]] = m
    if allm:
        parts = []
        for m in allm.values():
            label = "容器 · " + m["name"]
            if m.get("image"):
                label += "（" + m["image"] + "）"
            if m.get("private_port"):
                label += " → " + str(m["private_port"])
            parts.append(label)
        return "、".join(parts)
    if processes:
        return "宿主进程 · " + ", ".join(processes[:2]) + (
            " (#%d)" % pids[0] if pids else "")
    if pids:
        return "系统 / 宿主进程（PID #%d）" % pids[0]
    return "系统 / 宿主进程（进程未识别）"


def scan(docker=None, force=False, include_connections=False):
    """返回按端口聚合的监听表、折叠的连接态与统计。

    rows / listeners：每个 (协议, 端口) 恰好一行；
    connections：TIME_WAIT / ESTABLISHED 等连接态原始行（前端默认不展示）。
    """
    with _lock:
        now = time.time()
        if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL:
            cached = dict(_cache["data"])
            if include_connections and not cached.get("include_connections"):
                cached["rows"] = cached["listeners"] + cached["connections"]
                cached["include_connections"] = True
            return cached

    root, net_dir, scope = _proc_sources()
    raw = []
    for name, proto, ver in PROC_TABLES:
        raw.extend(_read_table(net_dir, name, proto, ver))

    port_map, by_id, by_name = (_docker_meta(docker) if docker is not None
                                else ({}, {}, {}))
    listen_inodes = set(r["inode"] for r in raw if r["state_code"] == LISTEN)
    procs = _process_names(root, listen_inodes, by_id=by_id)

    listen_groups, conn_groups = {}, {}
    for r in raw:
        key = (r["proto"], r["port"])
        (listen_groups if r["state_code"] == LISTEN else conn_groups).setdefault(key, []).append(r)

    rows = []
    for (proto, port), items in listen_groups.items():
        metas = list(port_map.get((proto, port), []))
        addresses, processes, pids, cgroup_metas = set(), set(), set(), []
        for it in items:
            if it["local"]:
                addresses.add(it["local"])
            pm = procs.get(it["inode"], {})
            if pm.get("process"):
                processes.add(pm["process"])
            if pm.get("pid"):
                pids.add(pm["pid"])
            if pm.get("container"):
                nm = pm["container"]
                if nm not in [m["name"] for m in metas]:
                    cgroup_metas.append({"name": nm, "image": pm.get("image", ""),
                                         "id": pm.get("cid", ""), "private_port": None})
        allm = metas + cgroup_metas
        addresses = sorted(addresses)
        processes = sorted(processes)
        pids = sorted(pids)
        conns = conn_groups.get((proto, port), [])
        states = dict(Counter(x["state"] for x in conns))
        # 疑似冲突：同端口被多个不同容器 / 不同宿主进程同时监听（排除 docker-proxy 双栈成对）
        owners = set(m["name"] for m in allm)
        procs_only = set(p for p in processes if p != "docker-proxy")
        conflict = len(owners) > 1 or len(procs_only) > 1 or (
            len(procs_only) == 1 and bool(owners))
        rows.append({
            "port": port,
            "proto": proto,
            "ip_version": 6 if all(x["ip_version"] == 6 for x in items) else 4,
            "state": "LISTEN",
            "listen": True,
            "local": _primary_addr(addresses),
            "addresses": addresses,
            "containers": [m["name"] for m in allm],
            "container": ", ".join(m["name"] for m in allm),
            "images": [m["image"] for m in allm if m.get("image")],
            "image": ", ".join(m["image"] for m in allm if m.get("image")),
            "owner": _owner(metas, processes, pids, cgroup_metas),
            "pid": pids[0] if pids else 0,
            "pids": pids,
            "process": ", ".join(processes),
            "processes": processes,
            "inode": items[0]["inode"],
            "connections": len(conns),
            "states": states,
            "conflict": conflict,
        })

    connections = []
    for (proto, port), items in conn_groups.items():
        listener = next((r for r in rows if r["port"] == port and r["proto"] == proto), None)
        for i, it in enumerate(items):
            connections.append({
                "port": port, "proto": proto,
                "ip_version": it["ip_version"],
                "state": it["state"], "listen": False,
                "local": it["local"], "inode": it["inode"],
                "containers": listener["containers"] if listener else [],
                "container": listener["container"] if listener else "",
                "images": listener["images"] if listener else [],
                "image": listener["image"] if listener else "",
                "owner": ("连接态 · 端口 " + str(port) + " 的关联监听：" +
                          (listener["owner"] if listener else "无监听进程")),
                "pid": 0, "process": "", "connections": 0, "states": {},
                "conflict": False, "seq": i,
            })

    # v7.0.3：按 Docker 端口映射补全。某些 NAS 上 /host/proc/<pid>/net 受
    # 权限或 PID namespace 限制读不到，但 docker.sock 一定知道所有已发布端口，
    # 不能让整机视图只剩面板自己那两个端口。
    from_docker = 0
    for (proto, port), metas in port_map.items():
        if any(r["port"] == port and r["proto"] == proto for r in rows):
            continue
        rows.append({
            "port": port, "proto": proto, "ip_version": 4,
            "state": "PUBLISHED", "listen": True,
            "local": "0.0.0.0", "addresses": ["0.0.0.0"],
            "containers": [m["name"] for m in metas],
            "container": ", ".join(m["name"] for m in metas),
            "images": [m["image"] for m in metas if m.get("image")],
            "image": ", ".join(m["image"] for m in metas if m.get("image")),
            "owner": _owner(metas, [], [], []),
            "pid": 0, "pids": [], "process": "", "processes": [],
            "inode": "", "connections": 0, "states": {},
            "conflict": False, "from_docker": True,
        })
        from_docker += 1

    rows.sort(key=lambda x: (x["port"], x["proto"]))
    connections.sort(key=lambda x: (not x["state"].startswith("ESTABLISHED"),
                                    x["port"], x["proto"], x["state"]))
    conflicts = [{"port": r["port"], "proto": r["proto"],
                  "owners": (r["containers"] + r["processes"])[:6]}
                 for r in rows if r["conflict"]]

    summary = {
        "total": len(rows) + len(connections),
        "listen": len(rows),
        "tcp": sum(1 for r in rows if r["proto"] == "tcp"),
        "udp": sum(1 for r in rows if r["proto"] == "udp"),
        "container_ports": sum(1 for r in rows if r["containers"]),
        "host_ports": sum(1 for r in rows if not r["containers"]),
        "identified": sum(1 for r in rows if r["containers"] or r["process"]),
        "connections": len(connections),
        "conflicts": len(conflicts),
    }
    data = {
        "ok": True,
        "rows": rows + connections if include_connections else rows,
        "listeners": rows,
        "connections": connections,
        "conflicts": conflicts,
        "summary": summary,
        "source": scope,
        "net_dir": net_dir,
        "from_docker": from_docker,
        "time": _now(),
        "include_connections": bool(include_connections),
        "note": ("数据来源：" + scope + "。" +
                 ("另按 Docker 端口映射补全 %d 个已发布端口。" % from_docker
                  if from_docker else "") +
                 "每行一个端口：IPv4 / IPv6 双栈与多个监听地址已合并；TIME_WAIT / "
                 "ESTABLISHED 是连接状态而不是端口占用，已折叠为「连接」列，"
                 "可在工具条切换查看。只读扫描，不会结束进程或占用端口；"
                 "容器内部未映射到宿主机的端口不在此列表。"),
    }
    with _lock:
        _cache["data"] = data
        _cache["ts"] = time.time()
    return dict(data)


def check(docker, port, proto="tcp"):
    """检查指定端口是否被占用，返回占用者（容器 / 镜像 / 进程）或可用建议。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("端口必须是 1~65535 的数字")
    if not (1 <= port <= 65535):
        raise ValueError("端口必须是 1~65535 的数字")
    proto = "udp" if str(proto).lower() == "udp" else "tcp"
    data = scan(docker, force=True)
    hits = [r for r in data["listeners"] if r["port"] == port and r["proto"] == proto]
    conns = [r for r in data["connections"] if r["port"] == port and r["proto"] == proto]
    return {
        "ok": True, "port": port, "proto": proto,
        "used": bool(hits),
        "state": hits[0]["state"] if hits else "",
        "owners": [
            {"local": r["local"], "owner": r["owner"],
             "container": r["container"], "image": r["image"],
             "process": r["process"], "pid": r["pid"],
             "addresses": r["addresses"], "connections": r["connections"]}
            for r in hits
        ],
        "connections": len(conns),
        "message": ("端口 %d/%s 已被占用：%s（另有 %d 条连接态）"
                    % (port, proto, hits[0]["owner"], len(conns)))
                   if hits else ("端口 %d/%s 当前空闲，可用于新建容器映射"
                                 % (port, proto)),
        "source": data["source"], "time": data["time"],
    }
