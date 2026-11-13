"""DockerUpdate v3.0 · Docker 管理能力补齐模块（对照 docker要求.txt P0 / P1 / P2）。

只新增能力，不改动原有 9 个模块的逻辑；应用商店分区整体移除后，
底层只保留目录树、桌面快捷方式、加速源与任务中心这些被 Docker 管理复用的组件。

设计约束：
  · 纯 Python 标准库，不引入 pip 依赖；
  · 所有写操作落在可写根 STORE_ROOT 下，宿主机路径经过白名单校验；
  · 高危操作（删除栈 / 清理卷 / 高级网络 / root 终端）前端二次确认，后端再校验；
  · fnOS 私有接口（通知中心 / 桌面原生图标 / 系统账号）不内置地址、不伪造成功；
  · 外部二进制（trivy / ssh）不存在时明确报错，不假装扫描成功。
"""
import base64
import hashlib
import http.client
import inspect
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

import config
import runtrace
import events
import notify
import jobs as jobs_mod
import store as st
import ports as ports_mod          # v6.0-r17：整机端口占用扫描
import netwatch as netwatch_mod    # v6.0-r17：外网断网 / 恢复通知
import image_updates as image_updates_mod   # v6.0-r18：独立「镜像更新」页面
import image_rebuild as image_rebuild_mod   # v7.0.4：更新镜像并重建使用该镜像的容器
import mounts as mounts_mod                 # v6.0-r24：容器目录映射（飞牛文件夹 → 容器内路径）
import symlinks as symlinks_mod             # v7.0.2：文件软链接（A 文件 → 容器内改名成 B）
from docker_api import DockerError, UnixHTTPConnection
from desktop import Desktop

PRO_DIR = os.path.join(config.DATA_DIR, "pro")


def _netwatch_save(body):
    """v6.0-r17：保存外网监测配置，返回统一结构给前端。"""
    cfg, message = netwatch_mod.save_config(body or {})
    return {"ok": True, "message": message, "config": cfg}
TEMPLATES_FILE = os.path.join(PRO_DIR, "templates.json")
TASKS_FILE = os.path.join(PRO_DIR, "tasks.json")
NOTIFY_FILE = os.path.join(PRO_DIR, "notify.json")
HOSTS_FILE = os.path.join(PRO_DIR, "hosts.json")
ROLES_FILE = os.path.join(PRO_DIR, "roles.json")
APIKEY_FILE = os.path.join(PRO_DIR, "apikeys.json")
AUDIT_FILE = os.path.join(PRO_DIR, "audit.json")
SETTINGS_FILE = os.path.join(PRO_DIR, "settings.json")
IMAGE_SRC_FILE = os.path.join(PRO_DIR, "image-sources.json")

for _d in (PRO_DIR,):
    os.makedirs(_d, exist_ok=True)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# v3.7：计划任务类型表（id, 名称, 是否需要目标, 目标提示, 说明）
# 每一种都在 _run_task() 里有真实执行逻辑，失败会如实返回原因。
TASK_KINDS = [
    ("restart-container",   "重启指定容器",           True,  "容器名",   "对指定容器执行 docker restart"),
    ("start-container",     "启动指定容器",           True,  "容器名",   "启动已停止的容器"),
    ("stop-container",      "停止指定容器",           True,  "容器名",   "平滑停止指定容器（10 秒超时）"),
    ("pause-container",     "暂停指定容器",           True,  "容器名",   "冻结容器进程，不释放内存"),
    ("unpause-container",   "恢复指定容器",           True,  "容器名",   "恢复被暂停的容器"),
    ("restart-all",         "重启全部运行中的容器",   False, "",        "逐个重启当前 running 的容器"),
    ("restart-stack",       "重启 Compose 栈",        True,  "栈名",     "按栈重启其下所有服务"),
    ("prune-images",        "清理悬空镜像",           False, "",        "删除 <none> 悬空镜像层"),
    ("prune-volumes",       "清理孤立卷",             False, "",        "删除无容器引用的命名卷"),
    ("backup-compose",      "备份 Compose 配置",      False, "",        "打包导出 Compose 栈配置"),
    ("check-updates",       "检查容器镜像更新",       False, "",        "只检测新版本，不自动更新"),
    ("full-check",          "全量巡检（含自动更新）", False, "",        "按面板策略执行每日巡检"),
    ("pull-latest",         "拉取指定镜像最新版",     True,  "镜像:tag", "只拉镜像不重建容器"),
    ("export-logs",         "导出指定容器日志",       True,  "容器名",   "保存最近 500 行日志到落盘目录"),
    ("health-check",        "健康巡检并推送异常",     False, "",        "检查异常退出容器并推送通知"),
    ("snapshot-container",  "更新前备份指定容器",     True,  "容器名",   "保存配置与旧镜像 ID，可一键回退"),
    ("backup-panel",        "备份面板数据快照",       False, "",        "打包面板配置 / 策略 / 任务到 zip"),
    ("notify-test",         "测试通知推送",           False, "",        "向已配置的通知网关发一条测试"),
]
TASK_KIND_IDS = tuple(x[0] for x in TASK_KINDS)


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))


def _save(path, obj):
    """原子写；临时文件按 进程+线程 唯一，避免并发请求互相覆盖 .tmp 导致 500。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _safe_path(rel):
    rel = str(rel or "").strip()
    if not rel or ".." in rel.split("/") or rel.startswith("~"):
        raise ValueError("路径不合法")
    return rel.lstrip("/")


def _host_local(host_path, writable=False):
    """宿主机路径 → 容器内路径。只读根 /host 与可写根 /store 平级挂载。"""
    p = str(host_path or "").strip()
    if not p.startswith("/"):
        raise ValueError("请填绝对路径")
    if ".." in p.split("/"):
        raise ValueError("路径不合法")
    root = config.STORE_HOST_ROOT.rstrip("/")
    if p == root or p.startswith(root + "/"):
        return os.path.join(config.STORE_ROOT, p[len(root):].lstrip("/"))
    if writable:
        raise ValueError("该路径不在可写根 %s 下，不能写入" % root)
    return os.path.join(config.HOST_ROOT, p.lstrip("/"))


# ---------------- 内置 Compose 模板（P1 应用模板市场） ----------------
BUILTIN_TEMPLATES = [
    {"id": "adguardhome", "name": "AdGuardHome", "category": "网络 / 广告过滤",
     "desc": "全网去广告与 DNS 过滤，自带 Web 管理界面。", "tags": ["latest", "v0.107.52", "edge"],
     "port": 3000, "icon": "🛡️", "mounts": ["/opt/ADGH/work:/opt/adguardhome/work", "/opt/ADGH/conf:/opt/adguardhome/conf"],
     "env": ["TZ=Asia/Shanghai"], "variables": [{"key": "TZ", "label": "时区", "default": "Asia/Shanghai"}],
     "compose": "services:\n  adguardhome:\n    image: adguard/adguardhome:{TAG}\n    container_name: {NAME}\n    restart: unless-stopped\n    ports:\n      - \"{PORT}:3000\"\n      - \"53:53/tcp\"\n      - \"53:53/udp\"\n    environment:\n      - TZ=Asia/Shanghai\n    volumes:\n      - ./work:/opt/adguardhome/work\n      - ./conf:/opt/adguardhome/conf\n"},
    {"id": "homeassistant", "name": "Home Assistant", "category": "智能家居",
     "desc": "开源智能家居中枢，支持上千种设备集成。", "tags": ["latest", "2025.10", "stable"],
     "port": 8123, "icon": "🏠", "mounts": ["./config:/config"],
     "env": ["TZ=Asia/Shanghai"], "variables": [{"key": "TZ", "label": "时区", "default": "Asia/Shanghai"}],
     "compose": "services:\n  homeassistant:\n    image: ghcr.io/home-assistant/home-assistant:{TAG}\n    container_name: {NAME}\n    restart: unless-stopped\n    ports:\n      - \"{PORT}:8123\"\n    environment:\n      - TZ=Asia/Shanghai\n    volumes:\n      - ./config:/config\n"},
    {"id": "rustdesk", "name": "RustDesk Server", "category": "远程桌面",
     "desc": "自建远程桌面中继 / 注册服务器，替代 TeamViewer。", "tags": ["latest", "1.1.11"],
     "port": 21115, "icon": "🖥️", "mounts": ["./data:/data"], "env": [],
     "variables": [{"key": "RELAY", "label": "中继地址", "default": "192.168.1.110"}],
     "compose": "services:\n  hbbs:\n    image: rustdesk/rustdesk-server:{TAG}\n    container_name: {NAME}-hbbs\n    restart: unless-stopped\n    command: hbbs -r {RELAY}:21117\n    ports:\n      - \"21115:21115\"\n      - \"21116:21116\"\n      - \"21116:21116/udp\"\n      - \"21118:21118\"\n    volumes:\n      - ./data:/data\n  hbbr:\n    image: rustdesk/rustdesk-server:{TAG}\n    container_name: {NAME}-hbbr\n    restart: unless-stopped\n    command: hbbr\n    ports:\n      - \"21117:21117\"\n      - \"21119:21119\"\n    volumes:\n      - ./data:/data\n"},
    {"id": "filebrowser", "name": "File Browser", "category": "文件管理",
     "desc": "轻量网页文件管理器，可指定任意目录作为根。", "tags": ["latest", "v2.31.0"],
     "port": 8080, "icon": "📁", "mounts": ["./data:/srv", "./config:/config"], "env": [],
     "variables": [{"key": "ROOT", "label": "文件根目录", "default": "/vol1/1000/docker"}],
     "compose": "services:\n  filebrowser:\n    image: filebrowser/filebrowser:{TAG}\n    container_name: {NAME}\n    restart: unless-stopped\n    ports:\n      - \"{PORT}:80\"\n    volumes:\n      - ./data:/srv\n      - ./config:/config\n    environment:\n      - FB_BASEURL=/\n"},
    {"id": "jellyfin", "name": "Jellyfin", "category": "影音",
     "desc": "开源媒体服务器，硬件转码与多端播放。", "tags": ["latest", "10.10.11", "10.9.11"],
     "port": 8096, "icon": "🎬", "mounts": ["./config:/config", "./cache:/cache", "/vol1/1000/media:/media"],
     "env": ["TZ=Asia/Shanghai"], "variables": [{"key": "TZ", "label": "时区", "default": "Asia/Shanghai"}],
     "compose": "services:\n  jellyfin:\n    image: jellyfin/jellyfin:{TAG}\n    container_name: {NAME}\n    restart: unless-stopped\n    ports:\n      - \"{PORT}:8096\"\n    environment:\n      - TZ=Asia/Shanghai\n    volumes:\n      - ./config:/config\n      - ./cache:/cache\n      - /vol1/1000/media:/media:ro\n"},
    {"id": "qbittorrent", "name": "qBittorrent", "category": "下载",
     "desc": "BT / PT 下载客户端，WebUI 管理。", "tags": ["latest", "4.6.7", "5.0.3"],
     "port": 8080, "icon": "⬇️", "mounts": ["./config:/config", "/vol1/1000/downloads:/downloads"],
     "env": ["TZ=Asia/Shanghai", "WEBUI_PORT=8080"],
     "variables": [{"key": "TZ", "label": "时区", "default": "Asia/Shanghai"}],
     "compose": "services:\n  qbittorrent:\n    image: linuxserver/qbittorrent:{TAG}\n    container_name: {NAME}\n    restart: unless-stopped\n    ports:\n      - \"{PORT}:8080\"\n      - \"6881:6881\"\n      - \"6881:6881/udp\"\n    environment:\n      - TZ=Asia/Shanghai\n      - WEBUI_PORT=8080\n    volumes:\n      - ./config:/config\n      - /vol1/1000/downloads:/downloads\n"},
    {"id": "uptime-kuma", "name": "Uptime Kuma", "category": "运维监控",
     "desc": "自托管uptime监控与状态页，支持多协议告警。", "tags": ["latest", "1.23.16", "2.0.0-beta"],
     "port": 3001, "icon": "📈", "mounts": ["./data:/app/data"], "env": ["TZ=Asia/Shanghai"],
     "variables": [], "compose": "services:\n  uptime-kuma:\n    image: louislam/uptime-kuma:{TAG}\n    container_name: {NAME}\n    restart: unless-stopped\n    ports:\n      - \"{PORT}:3001\"\n    environment:\n      - TZ=Asia/Shanghai\n    volumes:\n      - ./data:/app/data\n"},
    {"id": "portainer", "name": "Portainer CE", "category": "运维监控",
     "desc": "Docker 图形化管理（与本面板互补），可管理栈与镜像。", "tags": ["latest", "2.21.5", "2.27.1"],
     "port": 9000, "icon": "🐳", "mounts": ["./data:/data", "/var/run/docker.sock:/var/run/docker.sock"],
     "env": [], "variables": [],
     "compose": "services:\n  portainer:\n    image: portainer/portainer-ce:{TAG}\n    container_name: {NAME}\n    restart: unless-stopped\n    ports:\n      - \"{PORT}:9000\"\n    volumes:\n      - ./data:/data\n      - /var/run/docker.sock:/var/run/docker.sock\n"},
]


# ---------------- 极简受限 Compose 解析（不支持 anchors/extends/build） ----------------
def parse_compose(text):
    """解析 NAS 常见 Compose：services / image / ports / volumes / environment /
    restart / command / labels / network_mode / privileged / user / read_only /
    mem_limit / depends_on。遇到不支持的语法明确报错，不静默丢配置。"""
    text = str(text or "")
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0]
        if re.search(r"<<\s*:", line) or re.search(r"^\s*&[A-Za-z0-9_-]+\s*$", line) \
                or re.search(r"^\s*\*[A-Za-z0-9_-]+\s*$", line) \
                or re.search(r":\s*&[A-Za-z0-9_-]+", line) \
                or re.search(r":\s*\*[A-Za-z0-9_-]+", line) \
                or re.match(r"^\s*(extends|include)\s*:", line):
            raise ValueError("暂不支持 YAML 锚点 / extends / include 等高级语法：%s" % line.strip()[:60])
    lines = text.splitlines()
    services = {}
    cur = None
    section = ""
    section_item = ""
    for raw in lines:
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()
        if indent == 0:
            if s in ("services:", "version:"):
                section = "services" if s == "services:" else "skip"
            else:
                section = "skip"
            continue
        if section != "services":
            continue
        if indent == 2 and s.endswith(":"):
            key = s[:-1].strip()
            if key in ("volumes", "networks", "configs", "secrets", "x-common"):
                cur = None
                section = "skip"
                continue
            cur = key
            services[cur] = {"image": "", "ports": [], "volumes": [], "environment": [],
                             "restart": "unless-stopped", "command": "", "labels": {},
                             "network_mode": "bridge", "privileged": False, "user": "",
                             "read_only": False, "mem_limit": "", "depends_on": []}
            section_item = ""
            continue
        if cur is None:
            continue
        if indent == 4 and ":" in s and not s.startswith("-"):
            k, _, v = s.partition(":")
            section_item = k.strip()
            if section_item == "build":
                raise ValueError("服务 %s 使用了 build:，本面板只支持现成镜像" % cur)
            v = v.strip().strip('"').strip("'")
            d = services[cur]
            if section_item == "image":
                if v.startswith("build"):
                    raise ValueError("不支持 build:，请使用现成镜像")
                d["image"] = v
            elif section_item == "restart":
                d["restart"] = v
            elif section_item == "command":
                d["command"] = v
            elif section_item == "network_mode":
                d["network_mode"] = v
            elif section_item == "user":
                d["user"] = v
            elif section_item in ("privileged", "read_only"):
                d[section_item] = v.lower() in ("true", "yes", "1")
            elif section_item == "mem_limit":
                d["mem_limit"] = v
            continue
        if indent >= 6 and s.startswith("-"):
            v = s[1:].strip().strip('"').strip("'")
            d = services[cur]
            if section_item == "ports":
                d["ports"].append(v)
            elif section_item == "volumes":
                d["volumes"].append(v)
            elif section_item == "environment":
                d["environment"].append(v)
            elif section_item == "depends_on":
                d["depends_on"].append(v)
            elif section_item == "labels":
                if "=" in v:
                    a, b = v.split("=", 1)
                    d["labels"][a.strip()] = b.strip()
            continue
        if indent == 6 and ":" in s and section_item == "environment":
            k, _, v = s.partition(":")
            services[cur]["environment"].append("%s=%s" % (k.strip(), v.strip().strip('"').strip("'")))
    if not services:
        raise ValueError("没有解析到 services，请检查 YAML 缩进（服务名需缩进 2 空格）")
    for name, d in services.items():
        if not d["image"]:
            raise ValueError("服务 %s 缺少 image（不支持 build）" % name)
    return services


def yaml_check(text):
    try:
        svcs = parse_compose(text)
        return {"ok": True, "services": list(svcs),
                "message": "语法校验通过：%d 个服务" % len(svcs)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "message": str(exc)}


def yaml_format(text):
    out = []
    for raw in str(text or "").splitlines():
        if raw.strip().startswith("#") or not raw.strip():
            out.append(raw.rstrip())
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        out.append(" " * (indent - indent % 2) + raw.strip())
    return {"ok": True, "text": "\n".join(out) + "\n"}


# ---------------- Web 终端（原始 UNIX socket + HTTP hijack，v7.0.3 重写） ----------------
class TermSession:
    """直接握 Docker 的 UNIX socket 完成 exec/start 的 HTTP hijack。

    旧实现用 UnixHTTPConnection(timeout=0)：timeout=0 会被 Python 当成
    「非阻塞 socket」，getresponse() 读不到响应头就抛
    RemoteDisconnected("Remote end closed connection without response")；
    而且 http.client 的 makefile 缓冲会吞掉响应头之后的首批终端输出。
    现在自己发 HTTP/1.1 请求、自己解析头、剩余字节直接当原始流读，
    既不会非阻塞，也不会丢第一行提示符。
    """
    def __init__(self, docker, cid, cmd, cols, rows):
        self.docker, self.cid = docker, cid
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.alive = True
        self.last = time.time()
        self.eid = ""
        obj = docker._request("POST", "/containers/%s/exec" % cid, body={
            "AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
            "Tty": True, "Cmd": cmd or ["/bin/sh"], "Env": ["TERM=xterm-256color"]})
        self.eid = (obj or {}).get("Id") or ""
        if not self.eid:
            raise ValueError("Docker 未返回 exec ID")
        payload = json.dumps({"Detach": False, "Tty": True,
                              "ConsoleSize": [int(rows), int(cols)]}).encode("utf-8")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(10.0)
            self.sock.connect(getattr(docker, "socket_path", None) or config.DOCKER_SOCKET)
            req = ("POST /exec/%s/start HTTP/1.1\r\n"
                   "Host: docker\r\n"
                   "Content-Type: application/json\r\n"
                   "Connection: Upgrade\r\n"
                   "Upgrade: tcp\r\n"
                   "Content-Length: %d\r\n\r\n" % (self.eid, len(payload))
                   ).encode("utf-8") + payload
            self.sock.sendall(req)
            head = bytearray()
            while b"\r\n\r\n" not in head:
                if len(head) > 65536:
                    raise ValueError("exec start 响应头异常（超过 64KB）")
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ValueError("Docker 守护进程关闭了连接，未返回响应头")
                head.extend(chunk)
            status_line, tail = bytes(head).split(b"\r\n\r\n", 1)
            status = status_line.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
            if " 200" not in status and " 101" not in status:
                raise ValueError("exec start 返回 %s" % status)
            # 响应头之后可能已经带上第一批终端输出，先存进缓冲区，绝不丢弃
            if tail:
                with self.lock:
                    self.buf.extend(tail)
            self.sock.settimeout(0.4)
        except Exception:
            try:
                self.sock.close()
            except Exception:
                pass
            raise
        threading.Thread(target=self._reader, daemon=True, name="du-term").start()

    def _reader(self):
        while self.alive:
            try:
                data = self.sock.recv(8192)
                if not data:
                    break
                with self.lock:
                    self.buf.extend(data)
                    if len(self.buf) > 262144:
                        del self.buf[:-131072]
            except socket.timeout:
                continue
            except OSError:
                break
        self.alive = False

    def read(self):
        with self.lock:
            data = bytes(self.buf)
            self.buf.clear()
        self.last = time.time()
        return data.decode("utf-8", "replace")

    def write(self, text):
        if not self.alive:
            raise ValueError("终端会话已结束")
        self.sock.sendall(text.encode("utf-8", "replace"))
        self.last = time.time()
        return True

    def resize(self, cols, rows):
        try:
            self.docker._request("POST", "/exec/%s/resize?h=%d&w=%d"
                                 % (self.eid, int(rows), int(cols)))
            return True
        except Exception:
            return False

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------- 远端 Docker 实例（TCP / TLS / SSH 端口转发） ----------------
class RemoteDocker:
    """只读 + 基础启停。SSH 方式用本机 ssh -L 转发远端 docker.sock 到本地端口。"""
    def __init__(self, host, port=2375, tls=False, verify=False, ssh=None):
        self.host, self.port, self.tls, self.verify = host, int(port), bool(tls), bool(verify)
        self.ssh_proc = None
        self.ssh = ssh or {}
        if self.ssh.get("enabled"):
            local_port = int(self.ssh.get("local_port") or 12375)
            cmd = ["ssh", "-N", "-L", "%d:/var/run/docker.sock" % local_port,
                   "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30"]
            if self.ssh.get("key"):
                cmd += ["-i", self.ssh["key"]]
            if self.ssh.get("port"):
                cmd += ["-p", str(self.ssh["port"])]
            cmd.append("%s@%s" % (self.ssh.get("user") or "root", self.ssh.get("host") or host))
            self.ssh_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
            time.sleep(1.2)
            self.host, self.port, self.tls = "127.0.0.1", local_port, False

    def _conn(self):
        if self.tls:
            ctx = ssl.create_default_context()
            if not self.verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(self.host, self.port, timeout=6, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=6)

    def request(self, method, path, body=None):
        c = self._conn()
        try:
            hdr = {"Content-Type": "application/json"} if body is not None else {}
            c.request(method, path, body=json.dumps(body) if body is not None else None,
                      headers=hdr)
            r = c.getresponse()
            data = r.read().decode("utf-8", "replace")
            if r.status >= 400:
                raise ValueError("远端返回 %d：%s" % (r.status, data[:180]))
            try:
                return json.loads(data) if data.strip() else {}
            except Exception:
                return {"raw": data}
        finally:
            c.close()

    def close(self):
        if self.ssh_proc:
            self.ssh_proc.terminate()
            self.ssh_proc = None


class Pro:
    def __init__(self, mgr, console, desktop=None):
        self.mgr = mgr
        self.docker = mgr.docker
        self.console = console
        self.desktop = desktop or Desktop()
        self.lock = threading.RLock()
        self.terms = {}
        self.remotes = {}
        self._audit_lock = threading.RLock()
        # v6.0-r18：独立镜像更新（只比对 digest / 只拉取，绝不重建容器）
        self.image_updates = image_updates_mod.ImageUpdates(
            self.docker, self.mgr, self.audit)
        try:
            st.ACCEL.start_monitor()      # 加速源自动测速（原本由商店实例启动）
        except Exception:
            pass
        threading.Thread(target=self._task_loop, daemon=True, name="du-pro-tasks").start()

    # ---------------- 通用 ----------------
    def settings(self):
        d = _load(SETTINGS_FILE, {"compose_root": config.STORE_HOST_ROOT + "/compose-stacks",
                                  "template_root": config.STORE_HOST_ROOT + "/apps",
                                  "data_root": config.STORE_HOST_ROOT + "/apps-data",
                                  "advanced_confirmed": False,
                                  "audit_enabled": True, "readonly_mode": False})
        d.setdefault("data_root", config.STORE_HOST_ROOT + "/apps-data")
        return d

    @staticmethod
    def _host_path_ok(p):
        """v3.7：落盘位置是「宿主机路径字符串」，保存时只校验合法与安全，
        不再要求它一定落在容器当前可写挂载里（否则像 /vol1/1000/局域网共享
        这种 NAS 共享目录会被拒，界面重试后又用旧值重绘 = 看起来变回默认）。"""
        p = str(p or "").strip().rstrip("/")
        if not p.startswith("/"):
            raise ValueError("请填绝对路径")
        if ".." in p.split("/"):
            raise ValueError("路径不合法")
        low = p.lower()
        for bad in ("/etc", "/proc", "/sys", "/dev", "/boot", "/root",
                    "/bin", "/sbin", "/usr", "/lib", "/lib64", "/var/lib/docker"):
            if low == bad or low.startswith(bad + "/"):
                raise ValueError("系统目录不能作为落盘位置：%s" % p)
        return p

    def save_settings(self, form):
        s = self.settings()
        notes = []
        for k in ("compose_root", "template_root", "data_root"):
            raw = str(form.get(k) or "").strip()
            if not raw:
                continue
            v = self._host_path_ok(raw)
            s[k] = v
            try:
                _host_local(v, writable=True)
            except ValueError:
                notes.append("%s 不在容器当前可写挂载 %s 下：路径已保存，"
                             "但真正写文件时需要把该目录挂进容器（在 docker-compose.yml 增加 "
                             "- /vol1/1000:/vol1:rw 并重建），否则相关动作会明确报错。"
                             % (v, config.STORE_HOST_ROOT))
        for k in ("advanced_confirmed", "audit_enabled", "readonly_mode"):
            if k in form:
                s[k] = bool(form[k])
        _save(SETTINGS_FILE, s)
        return {"ok": True, "message": "系统设置已保存", "settings": s, "notes": notes}

    def audit(self, action, target, result, ctx=None):
        try:
            if not self.settings().get("audit_enabled", True):
                return
            with self._audit_lock:
                rows = _load(AUDIT_FILE, [])
                rows.insert(0, {"time": _now(), "user": (ctx or {}).get("user", "admin"),
                                "action": action, "target": str(target)[:120],
                                "result": str(result)[:200]})
                _save(AUDIT_FILE, rows[:600])
        except Exception:
            pass

    # ---------------- P0-1 概览补充统计 ----------------
    def overview(self):
        err = ""
        # v3.6：四路数据分别取，单个接口异常不能把其它统计一并清零；
        # list_volumes() 已经返回列表，旧代码再 .get("Volumes") 会抛 AttributeError。
        def _safe(label, fn, default):
            try:
                return fn() or default
            except Exception as exc:
                errors.append("%s：%s" % (label, str(exc)[:80]))
                return default
        errors = []
        cs = _safe("容器列表", lambda: self.docker.list_containers(all_=True), [])
        imgs = _safe("镜像列表", self.docker.list_images, [])
        raw_vols = _safe("数据卷列表", self.docker.list_volumes, [])
        vols = raw_vols if isinstance(raw_vols, list) else (raw_vols.get("Volumes") or [])
        nets = _safe("网络列表", self.docker.list_networks, [])
        if errors:
            err = "部分数据读取失败：" + "；".join(errors[:3])
        running = [c for c in cs if (c.get("State") or "").lower() == "running"]
        stopped = [c for c in cs if c not in running]
        metrics = (self.console.monitor.metrics() if hasattr(self.console, "monitor") else {}) or {}
        cpu = 0.0
        for c in cs:
            key = (c.get("Names") or [""])[0].lstrip("/")
            m = metrics.get(key)
            if isinstance(m, dict):
                cpu += float(m.get("cpu", 0) or 0)
            elif isinstance(m, (int, float)):
                cpu += float(m)
        mem = 0.0
        for c in cs:
            n = (c.get("Names") or [""])[0].lstrip("/")
            m = metrics.get(n) or {}
            if isinstance(m, dict):
                mem += float(m.get("mem", 0) or 0)
        disk = 0
        for im in imgs:
            disk += int((im.get("Size") if isinstance(im, dict) else 0) or 0)
        # 镜像 Size 拿不到时（老版本 daemon），退回 /system/df 的镜像层合计
        if not disk:
            try:
                df = self.docker.system_df() or {}
                di = df.get("Images") or {}
                if isinstance(di, dict):
                    disk = int(di.get("Size") or 0)
            except Exception:
                pass
        flags = []
        for c in cs:
            n = (c.get("Names") or [""])[0].lstrip("/")
            m = metrics.get(n) or {}
            if isinstance(m, dict) and (float(m.get("cpu", 0) or 0) > 85
                                        or float(m.get("mempct", 0) or 0) > 85):
                flags.append({"name": n, "reason": "资源占用过高", "level": "warn"})
            if (c.get("State") or "").lower() == "exited" and int(c.get("ExitCode") or 0) not in (0,):
                flags.append({"name": n, "reason": "异常退出（ExitCode %s）" % c.get("ExitCode"),
                              "level": "danger"})
        return {"ok": True, "docker_error": err,
                "total": len(cs), "running": len(running), "stopped": len(stopped),
                "images": len(imgs), "volumes": len(vols), "networks": len(nets),
                "docker_cpu": round(cpu, 1), "docker_mem_mb": round(mem, 1),
                "docker_disk_gb": round(disk / 1073741824, 2), "flags": flags[:30]}

    # ---------------- P0-2 容器明细表 / 红点 / 标签分组 ----------------
    def container_table(self):
        try:
            cs = self.docker.list_containers(all_=True) or []
        except Exception:
            cs = []
        metrics = (self.console.monitor.metrics() if hasattr(self.console, "monitor") else {}) or {}
        out = []
        for c in cs:
            name = (c.get("Names") or [""])[0].lstrip("/")
            m = metrics.get(name) or {}
            m = m if isinstance(m, dict) else {}
            ports = []
            for p in (c.get("Ports") or []):
                if p.get("PublicPort"):
                    ports.append("%s:%s/%s" % (p.get("PublicPort"), p.get("PrivatePort"),
                                               p.get("Type") or "tcp"))
            labels = c.get("Labels") or {}
            high = float(m.get("cpu", 0) or 0) > 85 or float(m.get("mempct", 0) or 0) > 85
            bad_exit = (c.get("State") or "").lower() == "exited" and int(c.get("ExitCode") or 0) not in (0,)
            out.append({"name": name, "state": c.get("State", ""), "status": c.get("Status", ""),
                        "image": c.get("Image", ""), "ports": ports,
                        "labels": [("%s=%s" % (k, v))[:80] for k, v in list(labels.items())[:8]],
                        "created": time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(int(c.get("Created") or 0))),
                        "cpu": float(m.get("cpu", 0) or 0), "mem": float(m.get("mem", 0) or 0),
                        "mempct": float(m.get("mempct", 0) or 0),
                        "net_in": float(m.get("net_in", 0) or 0), "net_out": float(m.get("net_out", 0) or 0),
                        "dot": "danger" if bad_exit else ("warn" if high else "")})
        return {"ok": True, "rows": out}

    @staticmethod
    def _name(body):
        """v7.0.3：action() 统一把整个 body(dict) 传给方法，早期 process /
        inspect / netio 却声明成 name(字符串)，于是 _cid 收到
        {'name': 'lucky'} 这样的字典，报「容器不存在：{'name': 'lucky'}」。
        这里统一抽出容器名，并兼容历史直接传字符串的调用。"""
        if isinstance(body, str):
            return body.strip()
        if isinstance(body, dict):
            v = body.get("name") or body.get("id") or body.get("container") or ""
            if isinstance(v, dict):
                v = v.get("name") or v.get("id") or ""
            return str(v).strip()
        return ""

    def process(self, body):
        name = self._name(body)
        cid = self._cid(name)
        try:
            d = self.docker.top(cid)
        except DockerError as exc:
            raise ValueError("读取进程失败：%s" % exc)
        if not isinstance(d, dict):
            raise ValueError("读取进程失败：Docker 未返回进程列表（容器可能已停止）")
        return {"ok": True, "name": name,
                "columns": d.get("Titles") or [], "processes": d.get("Processes") or []}

    def inspect(self, body):
        name = self._name(body)
        return {"ok": True, "name": name,
                "json": json.dumps(self.docker.inspect_container(self._cid(name)),
                                   ensure_ascii=False, indent=2)}

    def netio(self, body):
        name = self._name(body)
        s = (self.console.monitor.series_for(name)
             if hasattr(self.console, "monitor") else []) or []
        return {"ok": True, "name": name,
                "series": [{"t": x.get("t", ""), "in": x.get("net_in", x.get("rx", 0)),
                            "out": x.get("net_out", x.get("tx", 0))} for x in s[-120:]]}

    def _cid(self, name):
        name = self._name(name)
        if not name:
            raise ValueError("缺少容器名")
        for c in (self.docker.list_containers(all_=True) or []):
            names = [str(n).lstrip("/") for n in (c.get("Names") or [])]
            cid = str(c.get("Id") or "")
            if name in names or (cid and (cid == name or cid.startswith(name))):
                return cid or name
        raise ValueError("容器不存在：%s" % name)

    # ---------------- P0-2 Web 终端 ----------------
    def term_open(self, body):
        name = str(body.get("name") or "")
        cols, rows = int(body.get("cols") or 100), int(body.get("rows") or 28)
        cmd = str(body.get("cmd") or "/bin/sh").strip()
        if cmd.startswith("/bin/sh") or cmd in ("sh", "bash", "/bin/bash"):
            parts = [cmd] if cmd.startswith("/") else ["/bin/" + cmd]
        else:
            parts = ["/bin/sh", "-c", cmd]
        tid = hashlib.sha256(("%s-%s" % (time.time(), name)).encode()).hexdigest()[:16]
        try:
            self.terms[tid] = TermSession(self.docker, self._cid(name), parts, cols, rows)
        except Exception as exc:
            raise ValueError("无法打开终端：%s" % str(exc)[:200])
        self.audit("terminal-open", name, "已打开")
        return {"ok": True, "id": tid, "message": "终端已连接（原始 socket hijack + 轮询桥接）"}

    def term_read(self, body):
        s = self.terms.get(str(body.get("id") or ""))
        if not s:
            return {"ok": False, "error": "终端会话不存在或已过期"}
        return {"ok": True, "data": s.read(), "alive": s.alive}

    def term_write(self, body):
        s = self.terms.get(str(body.get("id") or ""))
        if not s:
            raise ValueError("终端会话不存在")
        s.write(base64.b64decode(str(body.get("data") or "")).decode("utf-8", "replace"))
        return {"ok": True}

    def term_resize(self, body):
        s = self.terms.get(str(body.get("id") or ""))
        if s:
            s.resize(int(body.get("cols") or 100), int(body.get("rows") or 28))
        return {"ok": True}

    def term_close(self, body):
        s = self.terms.pop(str(body.get("id") or ""), None)
        if s:
            s.close()
        return {"ok": True, "message": "终端已关闭"}

    # ---------------- P0-2 增强创建容器 ----------------
    def create_advanced(self, body):
        name = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(body.get("name") or "").strip())[:64]
        image = str(body.get("image") or "").strip()
        if not name or not image:
            raise ValueError("容器名和镜像不能为空")
        advanced = bool(body.get("advanced"))
        if advanced and not body.get("advanced_confirmed"):
            raise ValueError("高级选项需要勾选「我已确认风险」")
        ref = image if ":" in image.rsplit("/", 1)[-1] else image + ":latest"
        repo = ref.split(":")[0]
        try:
            self.docker.inspect_image(ref)
        except DockerError:
            self.docker.pull_ref(ref, self.mgr._registry_auth(repo),
                                 timeout=int(self.mgr.settings().get("check_timeout", 300)))
        exposed, pb, port_list = {}, {}, []
        for p in (body.get("ports") or []):
            m = re.match(r"^(\d+):(\d+)(?:/(tcp|udp))?$", str(p).strip())
            if not m:
                continue
            h, cport, proto = m.group(1), m.group(2), m.group(3) or "tcp"
            k = "%s/%s" % (cport, proto)
            exposed[k] = {}
            pb[k] = [{"HostPort": h}]
            port_list.append(str(p))
        env = [str(x).strip() for x in (body.get("env") or []) if str(x).strip() and "=" in str(x)]
        binds = [str(x).strip() for x in (body.get("volumes") or []) if str(x).strip() and ":" in str(x)]
        # fnOS 共享文件夹选择器：只允许 /host 下的只读路径或可写根下的路径
        for b in binds:
            host = b.split(":", 1)[0]
            if host.startswith("/"):
                _host_local(host, writable=False)
        hc = {"Binds": binds, "PortBindings": pb or None,
              "RestartPolicy": {"Name": str(body.get("restart") or "unless-stopped")},
              "NetworkMode": str(body.get("network") or "bridge")}
        if body.get("mem_mb"):
            mem = int(float(body["mem_mb"]) * 1048576)
            hc["Memory"], hc["MemorySwap"] = mem, mem * 2
        if body.get("cpu_cores"):
            hc["NanoCpus"] = int(float(body["cpu_cores"]) * 1e9)
        if advanced:
            if body.get("privileged"):
                hc["Privileged"] = True
            if body.get("read_only"):
                hc["ReadonlyRootfs"] = True
        cfg = {"Image": ref, "Hostname": name, "Env": env or None,
               "ExposedPorts": exposed or None}
        if advanced and body.get("user"):
            cfg["User"] = str(body["user"])[:80]
        if advanced and body.get("command"):
            cfg["Cmd"] = str(body["command"]).split()
        cfg = {k: v for k, v in cfg.items() if v is not None}
        created = self.docker.create_container(name, {"HostConfig": hc, **cfg})
        cid = (created or {}).get("Id") or ""
        if cid:
            self.docker.start(cid)
        # docker run 原始命令（可视化表单 ↔ 原始命令对照）
        cmd = ["docker", "run", "-d", "--name", name, "--restart",
               str(body.get("restart") or "unless-stopped")]
        for p in port_list:
            cmd += ["-p", p]
        for e in env:
            cmd += ["-e", e]
        for b in binds:
            cmd += ["-v", b]
        if advanced:
            if body.get("privileged"):
                cmd.append("--privileged")
            if body.get("read_only"):
                cmd.append("--read-only")
            if body.get("user"):
                cmd += ["-u", str(body["user"])]
        cmd.append(ref)
        if advanced and body.get("command"):
            cmd += str(body["command"]).split()
        root = _host_local(self.settings()["template_root"], writable=True)
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "docker-run.txt"), "w", encoding="utf-8") as f:
            f.write(" ".join(cmd) + "\n")
        self.audit("create-container", name, "已创建并启动")
        return {"ok": True, "message": "容器「%s」已创建并启动" % name,
                "command": " ".join(cmd), "dir": st.local_to_host(d)}

    def run_command(self, body):
        """仅生成 docker run 命令，不执行（用于可视化 ↔ 原始命令切换）。"""
        cmd = ["docker", "run", "-d", "--name", str(body.get("name") or "my-container")]
        for p in (body.get("ports") or []):
            cmd += ["-p", str(p)]
        for e in (body.get("env") or []):
            cmd += ["-e", str(e)]
        for v in (body.get("volumes") or []):
            cmd += ["-v", str(v)]
        cmd += ["--restart", str(body.get("restart") or "unless-stopped")]
        if body.get("privileged"):
            cmd.append("--privileged")
        if body.get("read_only"):
            cmd.append("--read-only")
        if body.get("user"):
            cmd += ["-u", str(body["user"])]
        cmd.append(str(body.get("image") or ""))
        if body.get("command"):
            cmd += str(body["command"]).split()
        return {"ok": True, "command": " ".join(x for x in cmd if x)}

    # ---------------- P0-2 fnOS 目录树 / 挂载选择器 ----------------
    def dirs(self, body):
        rel = _safe_path(body.get("path") or config.STORE_HOST_ROOT.lstrip("/"))
        host_path = "/" + rel
        local = _host_local(host_path, writable=False)
        if not os.path.isdir(local):
            # 只读根未挂载时回退可写根
            local2 = _host_local(config.STORE_HOST_ROOT, writable=True)
            return {"ok": True, "path": host_path, "parent": os.path.dirname(host_path) or "/",
                    "dirs": [], "writable": True,
                    "hint": "宿主机根目录未以 /host 只读挂载，仅能浏览可写根 %s" % config.STORE_HOST_ROOT}
        items = []
        try:
            for n in sorted(os.listdir(local))[:200]:
                p = os.path.join(local, n)
                if os.path.isdir(p) and not n.startswith("."):
                    items.append({"name": n, "path": host_path.rstrip("/") + "/" + n})
        except (OSError, PermissionError) as exc:
            return {"ok": False, "error": "读取目录失败：%s" % exc}
        return {"ok": True, "path": host_path,
                "parent": os.path.dirname(host_path.rstrip("/")) or "/",
                "dirs": items, "writable": host_path.startswith(config.STORE_HOST_ROOT),
                "hint": "目录来自宿主机只读挂载 /host；点选后自动填入挂载路径"}

    # ---------------- P0-3 Compose 栈 ----------------
    def stacks(self):
        err = ""
        try:
            cs = self.docker.list_containers(all_=True) or []
        except Exception as exc:
            err = "Docker 守护进程不可用：%s" % str(exc)[:160]
            cs = []
        groups = {}
        for c in cs:
            lb = c.get("Labels") or {}
            proj = lb.get("com.docker.compose.project")
            if not proj:
                continue
            g = groups.setdefault(proj, {"name": proj, "services": [], "dir": "",
                                         "config": "", "running": 0,
                                         "created": int(c.get("Created") or 0)})
            g["services"].append({"name": lb.get("com.docker.compose.service") or
                                  (c.get("Names") or [""])[0].lstrip("/"),
                                  "container": (c.get("Names") or [""])[0].lstrip("/"),
                                  "state": c.get("State", ""), "id": c.get("Id", "")[:12]})
            if (c.get("State") or "").lower() == "running":
                g["running"] += 1
            cfg = lb.get("com.docker.compose.project.config_files") or ""
            wd = lb.get("com.docker.compose.project.working_dir") or ""
            if cfg:
                g["config"] = cfg.split(",")[0]
            if wd:
                g["dir"] = wd
            g["created"] = min(g["created"], int(c.get("Created") or 0))
        # 可写根下手工导入的栈（还没 up 过）也要能看到
        root = _host_local(self.settings()["compose_root"], writable=True)
        if os.path.isdir(root):
            for n in sorted(os.listdir(root)):
                d = os.path.join(root, n)
                if os.path.isdir(d) and os.path.isfile(os.path.join(d, "docker-compose.yml")):
                    if n not in groups:
                        groups[n] = {"name": n, "services": [], "dir": st.local_to_host(d),
                                     "config": st.local_to_host(os.path.join(d, "docker-compose.yml")),
                                     "running": 0, "created": 0, "draft": True}
        out = []
        for g in groups.values():
            g["count"] = len(g["services"])
            g["state"] = ("running" if g["running"] == g["count"] and g["count"]
                          else ("partial" if g["running"] else ("draft" if g.get("draft") else "stopped")))
            g["created_text"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["created"])) if g["created"] else "—"
            out.append(g)
        return {"ok": True, "stacks": sorted(out, key=lambda x: x["name"]),
                "root": self.settings()["compose_root"], "error": err}

    def _stack_dir(self, name, create=False):
        n = st._safe_id(name)
        root = _host_local(self.settings()["compose_root"], writable=True)
        d = os.path.join(root, n)
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    def stack_read(self, body):
        name = str(body.get("name") or "")
        d = self._stack_dir(name)
        yml = os.path.join(d, "docker-compose.yml")
        envf = os.path.join(d, ".env")
        if not os.path.isfile(yml):
            # 尝试从运行中的栈标签指向的宿主机文件读取（只读）
            for s in self.stacks()["stacks"]:
                if s["name"] == name and s["config"]:
                    p = _host_local(s["config"], writable=False)
                    if os.path.isfile(p):
                        yml = p
                        d = os.path.dirname(p)
                        envf = os.path.join(d, ".env")
                        break
        text = ""
        if os.path.isfile(yml):
            try:
                with open(yml, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception as exc:
                text = "# 读取失败：%s" % exc
        env = ""
        if os.path.isfile(envf):
            with open(envf, "r", encoding="utf-8") as f:
                env = f.read()
        return {"ok": True, "name": name, "yaml": text, "env": env,
                "dir": st.local_to_host(d), "read_only": not os.access(yml, os.W_OK) if os.path.isfile(yml) else True}

    def stack_save(self, body):
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("缺少栈名称")
        text = str(body.get("yaml") or "")
        chk = yaml_check(text)
        if not chk["ok"]:
            raise ValueError("YAML 校验未通过：%s" % chk["error"])
        d = self._stack_dir(name, create=True)
        with open(os.path.join(d, "docker-compose.yml"), "w", encoding="utf-8") as f:
            f.write(text)
        if "env" in body:
            with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
                f.write(str(body.get("env") or ""))
        self.audit("stack-save", name, "YAML / .env 已保存")
        return {"ok": True, "message": "栈「%s」配置已保存（%d 个服务）" % (name, len(chk["services"])),
                "dir": st.local_to_host(d)}

    def _stack_services(self, name):
        data = self.stack_read({"name": name})
        if not data["yaml"]:
            raise ValueError("栈「%s」没有可读的 docker-compose.yml，请先导入或新建" % name)
        return parse_compose(data["yaml"]), data

    def stack_up(self, body, emit=None):
        name = str(body.get("name") or "")
        svcs, data = self._stack_services(name)
        d = self._stack_dir(name, create=True)
        base_dir = st.local_to_host(d)
        total = len(svcs)
        idx = 0
        for svc, cfg in svcs.items():
            idx += 1
            if emit:
                emit(stage="创建并启动服务", pct=10 + 80 * idx / max(total, 1),
                     detail="%s (%d/%d)" % (svc, idx, total))
            cname = "%s-%s" % (st._safe_id(name), st._safe_id(svc))
            # 已存在则先删，保证可重复 up
            try:
                self.docker.remove_container(cname, force=True, volumes=False)
            except Exception:
                pass
            ref = cfg["image"]
            if ":" not in ref.rsplit("/", 1)[-1]:
                ref += ":latest"
            try:
                self.docker.inspect_image(ref)
            except DockerError:
                self.docker.pull_ref(ref, self.mgr._registry_auth(ref.split(":")[0]),
                                     timeout=int(self.mgr.settings().get("check_timeout", 300)))
            exposed, pb = {}, {}
            binds = []
            for p in cfg["ports"]:
                m = re.match(r"^(\d+):(\d+)(?:/(tcp|udp))?$", str(p))
                if not m:
                    continue
                k = "%s/%s" % (m.group(2), m.group(3) or "tcp")
                exposed[k] = {}
                pb[k] = [{"HostPort": m.group(1)}]
            for v in cfg["volumes"]:
                parts = str(v).split(":")
                if len(parts) >= 2:
                    hp = parts[0]
                    if hp.startswith("."):
                        hp = base_dir.rstrip("/") + "/" + hp[2:]
                    elif hp.startswith("/"):
                        _host_local(hp, writable=False)
                    binds.append(":".join([hp] + parts[1:]))
            hc = {"Binds": binds, "PortBindings": pb or None,
                  "RestartPolicy": {"Name": cfg["restart"] or "unless-stopped"},
                  "NetworkMode": cfg["network_mode"] or "bridge",
                  "Privileged": bool(cfg["privileged"]), "ReadonlyRootfs": bool(cfg["read_only"])}
            if cfg["mem_limit"]:
                mem = int(float(re.sub(r"[^0-9.]", "", cfg["mem_limit"]) or 0) * 1048576)
                if mem:
                    hc["Memory"], hc["MemorySwap"] = mem, mem * 2
            cbody = {"Image": ref, "Hostname": cname,
                     "Env": cfg["environment"] or None, "ExposedPorts": exposed or None,
                     "Labels": dict(cfg["labels"], **{"com.docker.compose.project": name,
                                                      "com.docker.compose.service": svc})}
            if cfg["user"]:
                cbody["User"] = cfg["user"]
            if cfg["command"]:
                cbody["Cmd"] = cfg["command"].split()
            cbody = {k: v for k, v in cbody.items() if v is not None}
            created = self.docker.create_container(cname, {"HostConfig": hc, **cbody})
            cid = (created or {}).get("Id") or ""
            if cid:
                self.docker.start(cid)
        self.audit("stack-up", name, "%d 个服务" % total)
        return {"ok": True, "message": "栈「%s」已启动 %d 个服务" % (name, total)}

    def stack_down(self, body, emit=None):
        name = str(body.get("name") or "")
        if not body.get("confirm"):
            raise ValueError("停止并移除栈内容器需要二次确认")
        stacks = [x for x in self.stacks()["stacks"] if x["name"] == name]
        if not stacks:
            raise ValueError("栈不存在")
        n = 0
        for s in stacks[0]["services"]:
            if emit:
                emit(stage="停止并移除容器", detail=s["container"])
            try:
                self.docker.remove_container(s["container"], force=True, volumes=False)
                n += 1
            except Exception:
                pass
        self.audit("stack-down", name, "%d 个容器" % n)
        return {"ok": True, "message": "栈「%s」已 down，移除 %d 个容器（配置文件保留）" % (name, n)}

    def stack_restart(self, body, emit=None):
        name = str(body.get("name") or "")
        svc = str(body.get("service") or "")
        stacks = [x for x in self.stacks()["stacks"] if x["name"] == name]
        if not stacks:
            raise ValueError("栈不存在")
        targets = [s for s in stacks[0]["services"] if not svc or s["name"] == svc]
        for s in targets:
            if emit:
                emit(stage="重启服务", detail=s["name"])
            self.docker.restart(s["container"])
        self.audit("stack-restart", name, svc or "whole-stack")
        return {"ok": True, "message": "已重启 %d 个服务" % len(targets)}

    def stack_delete(self, body, emit=None):
        name = str(body.get("name") or "")
        if not body.get("confirm"):
            raise ValueError("删除栈需要二次确认（会删容器与配置目录）")
        try:
            self.stack_down({"name": name, "confirm": True}, emit)
        except Exception:
            pass
        d = self._stack_dir(name)
        if os.path.isdir(d) and st._under(d, st.STORE_ROOT):
            shutil.rmtree(d, ignore_errors=True)
        self.audit("stack-delete", name, "已删除目录")
        return {"ok": True, "message": "栈「%s」已删除（容器与配置目录）" % name}

    def stack_logs(self, body):
        name = str(body.get("name") or "")
        svc = str(body.get("service") or "")
        stacks = [x for x in self.stacks()["stacks"] if x["name"] == name]
        if not stacks:
            return {"ok": True, "lines": [], "services": []}
        targets = [s for s in stacks[0]["services"] if not svc or s["name"] == svc]
        lines = []
        for s in targets:
            try:
                raw = self.docker.logs(s["container"], tail=int(body.get("tail") or 80), timestamps=True)
                txt = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                lines += ["── %s ──" % s["name"]] + txt.splitlines()[-80:]
            except Exception as exc:
                lines.append("%s：读取日志失败 %s" % (s["name"], str(exc)[:100]))
        return {"ok": True, "lines": lines[-600:],
                "services": [s["name"] for s in stacks[0]["services"]]}

    def stack_export(self):
        root = _host_local(self.settings()["compose_root"], writable=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.isdir(root):
                for n in sorted(os.listdir(root)):
                    d = os.path.join(root, n)
                    if not os.path.isdir(d):
                        continue
                    for f in ("docker-compose.yml", ".env", "README.md"):
                        p = os.path.join(d, f)
                        if os.path.isfile(p):
                            z.write(p, "%s/%s" % (n, f))
            for s in self.stacks()["stacks"]:
                if s["config"] and not s.get("draft"):
                    p = _host_local(s["config"], writable=False)
                    if os.path.isfile(p):
                        z.write(p, "running/%s/docker-compose.yml" % st._safe_id(s["name"]))
        return {"ok": True, "data": base64.b64encode(buf.getvalue()).decode(),
                "filename": "dockerupdate-stacks-%s.zip" % time.strftime("%Y%m%d-%H%M")}

    def stack_import(self, body):
        name = str(body.get("name") or "").strip()
        raw = str(body.get("data") or "")
        if not name:
            raise ValueError("请填栈名称")
        try:
            blob = base64.b64decode(raw)
        except Exception:
            raise ValueError("导入数据不是合法 base64")
        text = ""
        env = ""
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for n in z.namelist():
                    if n.endswith(("docker-compose.yml", "docker-compose.yaml")):
                        text = z.read(n).decode("utf-8", "replace")
                    elif n.endswith(".env"):
                        env = z.read(n).decode("utf-8", "replace")
        except Exception:
            text = blob.decode("utf-8", "replace")
        chk = yaml_check(text)
        if not chk["ok"]:
            raise ValueError("导入的 Compose 校验失败：%s" % chk["error"])
        d = self._stack_dir(name, create=True)
        with open(os.path.join(d, "docker-compose.yml"), "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
            f.write(env)
        self.audit("stack-import", name, "%d 个服务" % len(chk["services"]))
        return {"ok": True, "message": "栈「%s」已导入（%d 个服务），可在列表里 up"
                % (name, len(chk["services"])), "dir": st.local_to_host(d)}

    # ---------------- P1 应用模板市场 ----------------
    def templates(self, body=None):
        custom = _load(TEMPLATES_FILE, [])
        out = []
        for t in BUILTIN_TEMPLATES + custom:
            out.append(dict(t, builtin=t in BUILTIN_TEMPLATES,
                            custom=not (t in BUILTIN_TEMPLATES)))
        return {"ok": True, "templates": out, "root": self.settings()["template_root"],
                "total": len(out), "builtin": len(BUILTIN_TEMPLATES), "custom": len(custom)}

    def template_add(self, body):
        name = str(body.get("name") or "").strip()
        text = str(body.get("compose") or "")
        chk = yaml_check(text)
        if not name:
            raise ValueError("请填模板名称")
        if not chk["ok"]:
            raise ValueError("模板 Compose 校验失败：%s" % chk["error"])
        rows = _load(TEMPLATES_FILE, [])
        tid = st._safe_id(name) + "-" + hashlib.sha1(time.time().hex().encode()).hexdigest()[:5]
        t = {"id": tid, "name": name, "category": str(body.get("category") or "自定义"),
             "desc": str(body.get("desc") or "用户导入的自定义模板"),
             "tags": [x.strip() for x in str(body.get("tags") or "latest").split(",") if x.strip()],
             "port": int(body.get("port") or 8080), "icon": "🧩", "mounts": [], "env": [],
             "variables": [], "compose": text}
        rows.append(t)
        _save(TEMPLATES_FILE, rows)
        self.audit("template-add", name, "已导入自定义模板")
        return {"ok": True, "message": "自定义模板「%s」已导入" % name, "id": tid}

    def template_delete(self, body):
        tid = str(body.get("id") or "")
        rows = [x for x in _load(TEMPLATES_FILE, []) if x["id"] != tid]
        _save(TEMPLATES_FILE, rows)
        return {"ok": True, "message": "自定义模板已删除（内置模板不可删）"}

    def template_deploy(self, body, emit=None):
        tid = str(body.get("id") or "")
        tpl = next((x for x in self.templates()["templates"] if x["id"] == tid), None)
        if not tpl:
            raise ValueError("模板不存在")
        name = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(body.get("name") or tpl["id"]).strip())[:60]
        tag = str(body.get("tag") or (tpl["tags"] or ["latest"])[0])
        port = str(body.get("port") or tpl["port"])
        variables = body.get("variables") or {}
        text = str(tpl["compose"]).replace("{TAG}", tag).replace("{NAME}", name).replace("{PORT}", port)
        for k, v in variables.items():
            text = text.replace("{%s}" % k, str(v))
        text = re.sub(r"\{[A-Z_]+\}", "", text)
        chk = yaml_check(text)
        if not chk["ok"]:
            raise ValueError("模板渲染后校验失败：%s" % chk["error"])
        if emit:
            emit(stage="写 Compose 与 .env", pct=8, detail=name)
        root = _host_local(self.settings()["template_root"], writable=True)
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "docker-compose.yml"), "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
            f.write("\n".join("%s=%s" % (k, v) for k, v in variables.items()) + "\n")
        with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
            f.write("# %s\n\n%s\n\n访问：http://%s:%s\n版本：%s\n"
                    % (tpl["name"], tpl["desc"], config.HOST_IP, port, tag))
        # 复用栈启动逻辑
        saved = self.settings()
        old = saved["compose_root"]
        saved["compose_root"] = self.settings()["template_root"]
        _save(SETTINGS_FILE, saved)
        try:
            result = self.stack_up({"name": name}, emit=emit)
        finally:
            saved["compose_root"] = old
            _save(SETTINGS_FILE, saved)
        if emit:
            emit(stage="同步桌面图标", pct=96)
        icon = self.desktop.create(
            {"id": "tpl-" + name, "name": tpl["name"], "icon": tpl.get("icon", ""),
             "homepage": "http://%s:%s" % (config.HOST_IP, port), "ports": [port]},
            {"installed_at": _now()})
        self.audit("template-deploy", name, "版本 %s" % tag)
        return {"ok": True, "message": "%s；模板「%s」%s 部署完成，访问 http://%s:%s%s"
                % (result["message"], tpl["name"], tag, config.HOST_IP, port,
                   "，桌面图标已同步" if not icon.get("skipped") else "（桌面图标未启用）"),
                "dir": st.local_to_host(d), "desktop": icon}

    # ---------------- P0-4 镜像层 / 自动更新预览 / P0-5 孤立卷 ----------------
    def image_history(self, body):
        ref = str(body.get("ref") or "")
        if not ref:
            raise ValueError("缺少镜像引用")
        try:
            rows = self.docker._request("GET", "/images/%s/history" % urllib.parse.quote(ref, safe=""))
        except DockerError as exc:
            raise ValueError("读取镜像层失败：%s" % exc)
        return {"ok": True, "layers": [{"id": str(x.get("Id") or "")[:20],
                                         "created": time.strftime("%Y-%m-%d %H:%M",
                                                                  time.localtime(int(x.get("Created") or 0))),
                                         "created_by": str(x.get("CreatedBy") or "")[:160],
                                         "size": int(x.get("Size") or 0),
                                         "tags": x.get("Tags") or []} for x in (rows or [])]}

    def update_preview(self, body):
        """只拉取镜像、不重建容器（P1 预览模式）。"""
        name = str(body.get("name") or "")
        c = next((x for x in (self.docker.list_containers(all_=True) or [])
                  if (x.get("Names") or [""])[0].lstrip("/") == name), None)
        if not c:
            raise ValueError("容器不存在")
        ref = str(c.get("Image") or "")
        try:
            self.docker.pull_ref(ref, self.mgr._registry_auth(ref.split(":")[0]),
                                 timeout=int(self.mgr.settings().get("check_timeout", 300)))
        except Exception as exc:
            self.notify("镜像更新预览失败", "容器 %s 拉取 %s 失败：%s" % (name, ref, str(exc)[:160]))
            raise ValueError("拉取失败：%s" % str(exc)[:200])
        self.audit("update-preview", name, ref)
        return {"ok": True, "message": "已拉取 %s，未重启容器（预览模式）" % ref}

    def volumes_orphan(self):
        try:
            d = self.docker._request("GET", "/volumes?filters=" +
                                     urllib.parse.quote(json.dumps({"dangling": ["true"]})))
        except DockerError as exc:
            raise ValueError("读取卷失败：%s" % exc)
        rows = [{"name": v.get("Name", ""), "driver": v.get("Driver", ""),
                 "mountpoint": v.get("Mountpoint", ""),
                 "size": (v.get("UsageData") or {}).get("Size", 0)} for v in (d.get("Volumes") or [])]
        return {"ok": True, "volumes": rows, "count": len(rows)}

    def volumes_prune(self, body):
        if not body.get("confirm"):
            raise ValueError("批量清理孤立卷需要二次确认")
        rows = self.volumes_orphan()["volumes"]
        n = 0
        for v in rows:
            try:
                self.docker.remove_volume(v["name"], force=True)
                n += 1
            except Exception:
                pass
        self.audit("volumes-prune", "all", "%d 个" % n)
        return {"ok": True, "message": "已清理 %d 个孤立卷" % n}

    # ---------------- P0-6 / P2-5 高级网络 ----------------
    def network_advanced(self, body):
        if not body.get("confirm"):
            raise ValueError("创建 macvlan / ipvlan 需要二次确认（配置不当会断网）")
        driver = str(body.get("driver") or "macvlan")
        if driver not in ("macvlan", "ipvlan"):
            raise ValueError("驱动只能是 macvlan / ipvlan")
        name = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(body.get("name") or "").strip())
        subnet = str(body.get("subnet") or "").strip()
        gateway = str(body.get("gateway") or "").strip()
        parent = str(body.get("parent") or "eth0").strip()
        if not name or not re.match(r"^\d+\.\d+\.\d+\.\d+/\d+$", subnet):
            raise ValueError("请填网络名与合法子网（如 192.168.1.0/24）")
        obj = {"Name": name, "Driver": driver, "CheckDuplicate": True,
               "IPAM": {"Driver": "default", "Config": [{"Subnet": subnet}]},
               "Options": {"parent": parent}}
        if gateway:
            obj["IPAM"]["Config"][0]["Gateway"] = gateway
        try:
            self.docker.create_network(obj)
        except DockerError as exc:
            raise ValueError("创建失败：%s" % exc)
        self.audit("network-advanced", name, driver)
        return {"ok": True, "message": "%s 网络「%s」已创建（parent=%s）" % (driver, name, parent)}

    # ---------------- P1/P2 仓库凭证 / 通知 / 计划任务 ----------------
    def creds(self):
        try:
            with open(config.REGISTRY_AUTH_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        rows = []
        for k, v in (d.get("auths") or {}).items():
            rows.append({"registry": k, "username": (v or {}).get("username", ""),
                         "has_password": bool((v or {}).get("password"))})
        for k, v in d.items():
            if k == "auths" or not isinstance(v, dict):
                continue
            if not any(x["registry"] == k for x in rows):
                rows.append({"registry": k, "username": v.get("username", ""),
                             "has_password": bool(v.get("password"))})
        return {"ok": True, "creds": rows,
                "hint": "私有 Registry 拉取时按仓库地址自动匹配；密码只存本机 data/registry-auth.json，不在界面回显"}

    def creds_save(self, body):
        registry = str(body.get("registry") or "").strip().rstrip("/")
        user = str(body.get("username") or "").strip()
        pwd = str(body.get("password") or "")
        if not registry or not user:
            raise ValueError("请填仓库地址与用户名")
        try:
            with open(config.REGISTRY_AUTH_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d.setdefault("auths", {})[registry] = {"username": user, "password": pwd}
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.REGISTRY_AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        self.audit("creds-save", registry, "已保存凭证")
        return {"ok": True, "message": "仓库凭证已保存（拉取私有镜像时自动使用）"}

    def creds_delete(self, body):
        registry = str(body.get("registry") or "")
        try:
            with open(config.REGISTRY_AUTH_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d.setdefault("auths", {}).pop(registry, None)
        with open(config.REGISTRY_AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return {"ok": True, "message": "凭证已删除"}

    def notify_effective(self, c):
        """v6.0-r14：把「自动模式会回退到告警页 Webhook」这件事显性化。

        用户常问：通知中心里没加企业微信渠道，为什么微信还收得到消息？
        因为 channel_mode=auto 时，多渠道为空就会改用 alerts.json 里的
        webhook_url（告警页「告警规则」那一栏），那是另一条独立通道。
        """
        ev = events.load()
        mode = ev.get("channel_mode") or "auto"
        chans = [x for x in (c.get("channels") or []) if x.get("enabled") and x.get("url")]
        alert_enabled, alert_url = False, ""
        # 用事件中心已绑定的实例（Alert.__init__ 会注册），不依赖 server 模块名：
        # server.py 以 __main__ 启动，sys.modules.get("server") 可能是 None。
        alerts = events._alerts()
        if alerts is not None:
            try:
                ac = alerts.config()
                alert_enabled = bool(ac.get("enabled"))
                alert_url = str(ac.get("webhook_url") or "")
            except Exception:
                pass
        masked = re.sub(r"(key|token|secret|access_token)=([^&]+)", r"\1=***", alert_url) if alert_url else ""
        use_alert = mode in ("alert", "both") or (mode == "auto" and not chans)
        fallback = bool(use_alert and alert_enabled and alert_url and not chans)
        return {
            "channel_mode": mode,
            "channels_enabled": len(chans),
            "alert_enabled": alert_enabled,
            "alert_webhook_configured": bool(alert_url),
            "alert_webhook_masked": masked,
            "alert_in_use": bool(use_alert and alert_enabled and alert_url),
            "fallback_active": fallback,
        }

    def notify_disable_alert(self, body=None):
        """一键停用告警页 Webhook 并清空地址（不影响通知中心渠道）。"""
        alerts = events._alerts()
        if alerts is None:
            raise ValueError("未找到告警配置，请在告警页手动清空 Webhook 地址")
        alerts.update({"enabled": False, "webhook_url": ""})
        return {"ok": True, "message": "已停用告警页 Webhook 并清空地址；此后只按通知中心的渠道推送"}

    def notify_config(self):
        c = notify.load()
        return {"ok": True, "config": c, "effective": self.notify_effective(c), "types": [
            {"id": a, "label": b, "hint": c_, "desc": d}
            for a, b, c_, d in notify.CHANNEL_TYPES],
            "hint": "多渠道通知：可同时启用自定义 Webhook / 企业微信 / 钉钉 / 飞书 / "
                    "Telegram / Bark / Server 酱。面板不内置任何地址或凭据，未配置时明确显示"
                    "「未配置」，不会伪造推送成功；旧版单 Webhook 配置会自动迁移。"}

    def notify_save(self, body):
        c = notify.load()
        c["enabled"] = bool(body.get("enabled", c.get("enabled")))
        if isinstance(body.get("events"), dict):
            patch = {k: bool(v) for k, v in body["events"].items()
                     if k in notify.DEFAULT_EVENTS}
            c["events"].update(patch)
            # v5.1：开关以 data/events.json 为唯一来源，通知中心保存时同步过去
            try:
                ev = events.load()
                ev["events"].update({k: v for k, v in patch.items() if k in ev["events"]})
                ev["channel_mode"] = str(body.get("channel_mode") or ev["channel_mode"])
                events.save(ev)
            except Exception as exc:
                print("[events] 同步事件开关失败:", exc)
        if isinstance(body.get("channels"), list):
            rows = []
            for x in body["channels"]:
                if not isinstance(x, dict):
                    continue
                url = str(x.get("url") or "").strip()
                typ = str(x.get("type") or "webhook")
                if typ not in tuple(t[0] for t in notify.CHANNEL_TYPES):
                    typ = "webhook"
                if url and not url.startswith(("http://", "https://")):
                    raise ValueError("通知地址必须是完整的 http(s) URL")
                if not url:
                    continue
                rows.append({"id": str(x.get("id") or ("ch-" + hashlib.sha1(
                    (typ + url).encode()).hexdigest()[:8])),
                             "type": typ, "name": str(x.get("name") or typ)[:40],
                             "url": url[:300], "token": str(x.get("token") or "")[:200],
                             "secret": str(x.get("secret") or "")[:200],
                             "enabled": bool(x.get("enabled", True))})
            c["channels"] = rows
        notify.save(c)
        return {"ok": True, "message": "通知设置已保存（%d 个渠道）" % len(c["channels"]),
                "config": notify.load()}

    def notify(self, title, text):
        return notify.send(title, text, None)

    def notify_test(self):
        r = notify.test()
        if r.get("skipped"):
            return r
        detail = "；".join("%s：%s" % (x["channel"], "成功" if x["ok"] else x["message"])
                          for x in r.get("channels") or [])
        return {"ok": r["ok"], "skipped": False,
                "message": r["message"] + ("（" + detail + "）" if detail else ""),
                "channels": r.get("channels") or []}

    def tasks(self):
        return {"ok": True, "tasks": _load(TASKS_FILE, []),
                "history": _load(os.path.join(PRO_DIR, "task-history.json"), [])[:60]}

    def task_options(self):
        """v3.7：给前端下拉 / datalist 提供真实可选项，避免让用户凭记忆敲名字。"""
        try:
            cs = self.docker.list_containers(all_=True) or []
        except Exception:
            cs = []
        names = sorted({(c.get("Names") or [""])[0].lstrip("/") for c in cs} - {""})
        images = sorted({str(c.get("Image") or "") for c in cs} - {""})[:40]
        try:
            stacks = [x.get("name", "") for x in (self.stacks().get("stacks") or [])]
        except Exception:
            stacks = []
        return {"ok": True,
                "kinds": [{"id": k, "label": label, "need": bool(need),
                           "hint": hint, "desc": desc}
                          for k, label, need, hint, desc in TASK_KINDS],
                "containers": names, "stacks": stacks, "images": images}

    def task_save(self, body):
        rows = _load(TASKS_FILE, [])
        tid = str(body.get("id") or "").strip()
        kind = str(body.get("kind") or "")
        if kind not in TASK_KIND_IDS:
            raise ValueError("任务类型不支持：%s" % kind)
        need = next((x[2] for x in TASK_KINDS if x[0] == kind), False)
        target = str(body.get("target") or "").strip()[:160]
        if need and not target:
            raise ValueError("该任务需要填写目标：%s" %
                             next((x[3] for x in TASK_KINDS if x[0] == kind), "名称"))
        schedule = str(body.get("schedule") or ("interval" if body.get("interval_min") else "daily"))
        if schedule not in ("daily", "interval", "weekly"):
            schedule = "daily"
        try:
            interval = max(0, min(10080, int(body.get("interval_min") or 0)))
        except (TypeError, ValueError):
            interval = 0
        if schedule == "interval" and interval < 1:
            raise ValueError("按间隔执行时，间隔分钟数必须 ≥ 1")
        # v3.9-r4：计划任务支持多选星期（weekdays=[1..7]），weekday 保留兼容旧数据
        raw_days = body.get("weekdays")
        if isinstance(raw_days, (list, tuple)):
            days = []
            for x in raw_days:
                try:
                    d = int(x)
                    if 1 <= d <= 7 and d not in days: days.append(d)
                except (TypeError, ValueError):
                    pass
            if not days:
                raise ValueError("每周执行时请至少选择一天")
            weekdays = sorted(days)
        else:
            try:
                weekdays = [max(1, min(7, int(body.get("weekday") or 1)))]
            except (TypeError, ValueError):
                weekdays = [1]
        weekday = weekdays[0]
        old = next((x for x in rows if x.get("id") == tid), None)
        t = {
            "id": tid or hashlib.sha1(("%s-%s" % (kind, time.time())).encode()).hexdigest()[:10],
            "name": str(body.get("name") or next(x[1] for x in TASK_KINDS if x[0] == kind))[:80],
            "kind": kind, "target": target,
            "time": str(body.get("time") or "03:30")[:5],
            "interval_min": interval, "schedule": schedule,
            "weekday": weekday, "weekdays": weekdays,
            "enabled": bool(body.get("enabled", True)),
            "last_run": (old or {}).get("last_run", ""),
            "last_result": (old or {}).get("last_result", ""),
        }
        rows = [x for x in rows if x.get("id") != t["id"]]
        rows.append(t)
        _save(TASKS_FILE, rows)
        self.audit("task-save", t["name"], kind)
        return {"ok": True, "message": "计划任务「%s」已%s" % (t["name"], "更新" if old else "保存")}

    def task_delete(self, body):
        tid = str(body.get("id") or "")
        _save(TASKS_FILE, [x for x in _load(TASKS_FILE, []) if x.get("id") != tid])
        return {"ok": True, "message": "计划任务已删除"}

    def task_toggle(self, body):
        tid, val = str(body.get("id") or ""), bool(body.get("enabled"))
        rows = _load(TASKS_FILE, [])
        hit = next((x for x in rows if x.get("id") == tid), None)
        if not hit:
            raise ValueError("任务不存在")
        hit["enabled"] = val
        _save(TASKS_FILE, rows)
        return {"ok": True, "message": "任务已%s" % ("启用" if val else "停用")}

    def _run_task(self, t):
        k, target = t["kind"], str(t.get("target") or "").strip()
        try:
            if k == "restart-container":
                self.docker.restart(target); msg = "已重启容器 %s" % target
            elif k == "start-container":
                self.docker.start(target); msg = "已启动容器 %s" % target
            elif k == "stop-container":
                self.docker.stop(target); msg = "已停止容器 %s" % target
            elif k == "pause-container":
                self.docker.pause(target); msg = "已暂停容器 %s" % target
            elif k == "unpause-container":
                self.docker.unpause(target); msg = "已恢复容器 %s" % target
            elif k == "restart-all":
                cs = [c for c in (self.docker.list_containers(all_=True) or [])
                      if (c.get("State") or "").lower() == "running"]
                names = [(c.get("Names") or [""])[0].lstrip("/") for c in cs]
                for n in names:
                    if n:
                        self.docker.restart(n)
                msg = "已重启 %d 个运行中的容器" % len(names)
            elif k == "restart-stack":
                msg = self.stack_restart({"name": target})["message"]
            elif k == "prune-images":
                self.docker.prune_images(dangling=True); msg = "已清理悬空镜像"
            elif k == "prune-volumes":
                msg = self.volumes_prune({"confirm": True})["message"]
            elif k == "backup-compose":
                msg = "已导出 Compose 备份 %s" % self.stack_export()["filename"]
            elif k in ("check-updates", "full-check"):
                r = self.mgr.check_all(auto=(k == "full-check"))
                msg = ("已启动%s：%s" % ("全量巡检（含自动更新）" if k == "full-check" else "镜像更新检测",
                                       (r or {}).get("message", "任务已排队")))
            elif k == "pull-latest":
                ref = target if (":" in target.rsplit("/", 1)[-1]) else target + ":latest"
                self.docker.pull_ref(ref, self.mgr._registry_auth(target.split(":")[0]), timeout=600)
                msg = "已拉取镜像 %s（未重建容器）" % ref
            elif k == "export-logs":
                txt = self.docker.logs(target, tail=500)
                if isinstance(txt, bytes):
                    txt = txt.decode("utf-8", "replace")
                out_dir = os.path.join(config.STORE_ROOT, "backups", "logs")
                os.makedirs(out_dir, exist_ok=True)
                safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", target)[:60]
                path = os.path.join(out_dir, "%s-%s.log" % (safe, time.strftime("%Y%m%d-%H%M%S")))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(str(txt))
                msg = "已导出日志 %s" % path
            elif k == "health-check":
                cs = self.docker.list_containers(all_=True) or []
                bad = [(c.get("Names") or [""])[0].lstrip("/")
                       for c in cs if (c.get("State") or "").lower() == "exited"
                       and int(c.get("ExitCode") or 0) != 0]
                msg = ("发现 %d 个异常退出容器：%s" % (len(bad), "、".join(bad[:8]))) if bad \
                    else "全部 %d 个容器状态正常" % len(cs)
                if bad:
                    self.notify("容器健康告警", msg)
            elif k == "snapshot-container":
                rec = self.mgr.backup_one(target, reason="task")
                msg = "已为 %s 保存版本快照（%s）" % (target, (rec or {}).get("time", _now()))
            elif k == "backup-panel":
                out_dir = os.path.join(config.STORE_ROOT, "backups")
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, "panel-%s.zip" % time.strftime("%Y%m%d-%H%M%S"))
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                    for fn in ("state.json", "pro/tasks.json", "pro/settings.json", "pro/notify.json"):
                        src = os.path.join(config.DATA_DIR, fn)
                        if os.path.isfile(src):
                            z.write(src, fn)
                msg = "已备份面板数据 %s" % path
            elif k == "notify-test":
                msg = (self.notify_test() or {}).get("message", "已发送测试通知")
            else:
                raise ValueError("未知任务类型：%s" % k)
            self.notify("计划任务完成", "%s：%s" % (t["name"], msg))
            try:
                events.emit("task_ok", "计划任务执行成功",
                            "任务「%s」（%s）执行完成：%s"
                            % (t["name"], t.get("kind") or "任务", str(msg)[:160]),
                            "info", t["name"], dedupe=False)
            except Exception:
                pass
            return msg
        except Exception as exc:
            self.notify("计划任务失败", "%s：%s" % (t["name"], str(exc)[:160]))
            try:
                events.emit("task_fail", "计划任务执行失败",
                            "任务「%s」（%s）执行失败：%s"
                            % (t["name"], t.get("kind") or "任务", str(exc)[:180]),
                            "high", t["name"], dedupe=False)
            except Exception:
                pass
            return "失败：%s" % str(exc)[:160]

    def task_run(self, body):
        t = next((x for x in _load(TASKS_FILE, []) if x.get("id") == str(body.get("id") or "")), None)
        if not t:
            raise ValueError("任务不存在")
        msg = self._run_task(t)
        self._task_history(t, msg)
        self.audit("task-run", t["name"], msg)
        rows = _load(TASKS_FILE, [])
        for x in rows:
            if x.get("id") == t["id"]:
                x["last_run"], x["last_result"] = _now(), msg
        _save(TASKS_FILE, rows)
        return {"ok": True, "message": msg}

    def _task_history(self, t, msg):
        p = os.path.join(PRO_DIR, "task-history.json")
        rows = _load(p, [])
        rows.insert(0, {"time": _now(), "name": t.get("name", ""),
                        "result": str(msg)[:200]})
        _save(p, rows[:100])

    def _task_loop(self):
        last = ""
        while True:
            time.sleep(30)
            try:
                now = datetime_now()
                key = now.strftime("%Y-%m-%d %H:%M")
                if key == last:
                    continue
                last = key
                for t in _load(TASKS_FILE, []):
                    if not t.get("enabled"):
                        continue
                    sch = t.get("schedule") or ("interval" if t.get("interval_min") else "daily")
                    if sch == "interval":
                        iv = max(1, int(t.get("interval_min") or 1))
                        due = int(time.time() // 60) % iv == 0
                    elif sch == "weekly":
                        days = t.get("weekdays") or [int(t.get("weekday") or 1)]
                        due = (now.isoweekday() in [int(d) for d in days]
                               and now.strftime("%H:%M") == (t.get("time") or "03:30"))
                    else:
                        due = now.strftime("%H:%M") == (t.get("time") or "03:30")
                    if not due:
                        continue
                    msg = self._run_task(t)
                    self._task_history(t, msg)
                    rows = _load(TASKS_FILE, [])
                    for x in rows:
                        if x.get("id") == t["id"]:
                            x["last_run"], x["last_result"] = _now(), msg
                    _save(TASKS_FILE, rows)
            except Exception as exc:
                print("[pro-tasks]", exc)

    # ---------------- P2 多 Docker 连接 / Trivy / 文件管理 / 卷备份 ----------------
    def hosts(self):
        rows = _load(HOSTS_FILE, [])
        return {"ok": True, "hosts": [{"id": "local", "name": "本机 Docker", "type": "unix",
                                        "address": config.DOCKER_SOCKET, "active": True}] + rows,
                "active": _load(SETTINGS_FILE, {}).get("active_host", "local")}

    def host_save(self, body):
        rows = _load(HOSTS_FILE, [])
        hid = str(body.get("id") or "").strip() or hashlib.sha1(time.time().hex().encode()).hexdigest()[:10]
        typ = str(body.get("type") or "tcp")
        if typ not in ("tcp", "tls", "ssh"):
            raise ValueError("连接方式只能是 tcp / tls / ssh")
        h = {"id": hid, "name": str(body.get("name") or hid)[:60], "type": typ,
             "host": str(body.get("host") or "").strip(), "port": int(body.get("port") or 2375),
             "verify": bool(body.get("verify")),
             "ssh": {"enabled": typ == "ssh", "host": str(body.get("ssh_host") or body.get("host") or ""),
                     "user": str(body.get("ssh_user") or "root"), "port": body.get("ssh_port") or 22,
                     "key": str(body.get("ssh_key") or ""),
                     "local_port": int(body.get("ssh_local_port") or 12375)}}
        rows = [x for x in rows if x["id"] != hid]
        rows.append(h)
        _save(HOSTS_FILE, rows)
        self.audit("host-save", h["name"], typ)
        return {"ok": True, "message": "远端实例「%s」已保存，点「测试连接」验证" % h["name"]}

    def host_delete(self, body):
        hid = str(body.get("id") or "")
        if hid == "local":
            raise ValueError("本机实例不能删除")
        _save(HOSTS_FILE, [x for x in _load(HOSTS_FILE, []) if x["id"] != hid])
        return {"ok": True, "message": "远端实例已删除"}

    def host_test(self, body):
        hid = str(body.get("id") or "local")
        if hid == "local":
            return {"ok": True, "version": self.docker.version(), "message": "本机 Docker 已连接"}
        h = next((x for x in _load(HOSTS_FILE, []) if x["id"] == hid), None)
        if not h:
            raise ValueError("远端实例不存在")
        if h["type"] == "ssh" and not shutil.which("ssh"):
            return {"ok": False, "error": "容器内没有 ssh 命令，无法建立 SSH 隧道；请改用 TCP/TLS 或自行映射 socket"}
        r = RemoteDocker(h["host"], h["port"], h["type"] == "tls", h.get("verify"), h.get("ssh"))
        try:
            ver = r.request("GET", "/version")
            cs = r.request("GET", "/containers/json?all=1")
            r.close()
            return {"ok": True, "version": ver.get("Version", ""), "containers": len(cs or []),
                    "message": "连接成功：Docker %s，%d 个容器" % (ver.get("Version", "?"), len(cs or []))}
        except Exception as exc:
            r.close()
            return {"ok": False, "error": "连接失败：%s" % str(exc)[:200]}

    def host_containers(self, body):
        hid = str(body.get("id") or "local")
        if hid == "local":
            return {"ok": True, "rows": [{"name": (c.get("Names") or [""])[0].lstrip("/"),
                                           "state": c.get("State", ""), "image": c.get("Image", "")}
                                          for c in (self.docker.list_containers(all_=True) or [])]}
        h = next((x for x in _load(HOSTS_FILE, []) if x["id"] == hid), None)
        if not h:
            raise ValueError("远端实例不存在")
        r = RemoteDocker(h["host"], h["port"], h["type"] == "tls", h.get("verify"), h.get("ssh"))
        try:
            cs = r.request("GET", "/containers/json?all=1") or []
            return {"ok": True, "rows": [{"name": (c.get("Names") or [""])[0].lstrip("/"),
                                           "state": c.get("State", ""), "image": c.get("Image", "")}
                                          for c in cs]}
        finally:
            r.close()

    def trivy_scan(self, body):
        ref = str(body.get("ref") or "")
        if not ref:
            raise ValueError("缺少镜像引用")
        exe = shutil.which("trivy")
        if not exe:
            return {"ok": False, "scanned": False,
                    "error": "容器内未安装 trivy（面板不内置扫描引擎）。可在宿主机安装后把二进制挂载进容器，"
                             "或继续使用「本地镜像」页的常规管理；本面板不会伪造漏洞结果。"}
        try:
            p = subprocess.run([exe, "image", "--format", "json", "--quiet", ref],
                               capture_output=True, timeout=240)
        except Exception as exc:
            return {"ok": False, "scanned": False, "error": "扫描失败：%s" % str(exc)[:160]}
        try:
            obj = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
        except Exception:
            return {"ok": False, "scanned": False, "error": "trivy 输出解析失败：%s" %
                    p.stderr.decode("utf-8", "replace")[:160]}
        counts = {}
        rows = []
        for r in (obj.get("Results") or []):
            for v in (r.get("Vulnerabilities") or []):
                sev = v.get("Severity", "UNKNOWN")
                counts[sev] = counts.get(sev, 0) + 1
                rows.append({"id": v.get("VulnerabilityID", ""), "severity": sev,
                             "package": v.get("PkgName", ""), "title": str(v.get("Title") or "")[:120]})
        return {"ok": True, "scanned": True, "ref": ref, "counts": counts, "items": rows[:200],
                "message": "扫描完成：%s" % (", ".join("%s %d" % (k, v) for k, v in counts.items()) or "无漏洞")}

    def file_list(self, body):
        name = str(body.get("name") or "")
        path = str(body.get("path") or "/").strip()
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("容器内路径必须以 / 开头")
        cid = self._cid(name)
        try:
            data = self.docker._request_raw("GET", "/containers/%s/archive?path=%s"
                                            % (cid, urllib.parse.quote(path, safe="")))[2]
        except Exception as exc:
            raise ValueError("读取容器内路径失败：%s" % str(exc)[:160])
        rows = []
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                for m in tf.getmembers()[:300]:
                    rows.append({"name": m.name.rstrip("/").split("/")[-1], "path": m.name,
                                 "dir": m.isdir(), "size": int(m.size or 0)})
        except Exception as exc:
            raise ValueError("解析容器文件失败：%s" % str(exc)[:120])
        return {"ok": True, "path": path, "files": rows}

    def file_get(self, body):
        name = str(body.get("name") or "")
        path = str(body.get("path") or "")
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("路径不合法")
        data = self.docker._request_raw("GET", "/containers/%s/archive?path=%s"
                                        % (self._cid(name), urllib.parse.quote(path, safe="")))[2]
        return {"ok": True, "data": base64.b64encode(data).decode(),
                "filename": os.path.basename(path.rstrip("/")) + ".tar"}

    def file_put(self, body):
        name = str(body.get("name") or "")
        path = str(body.get("path") or "/").strip()
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("路径不合法")
        try:
            blob = base64.b64decode(str(body.get("data") or ""))
        except Exception:
            raise ValueError("上传数据不是合法 base64")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(str(body.get("filename") or "upload.bin"))
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
        conn = UnixHTTPConnection(config.DOCKER_SOCKET, timeout=60)
        try:
            conn.request("PUT", "/containers/%s/archive?path=%s"
                         % (self._cid(name), urllib.parse.quote(path, safe="")),
                         body=buf.getvalue(), headers={"Content-Type": "application/x-tar"})
            r = conn.getresponse()
            r.read()
            if r.status >= 400:
                raise ValueError("Docker 返回 %d" % r.status)
        finally:
            conn.close()
        self.audit("file-put", name, path)
        return {"ok": True, "message": "文件已上传到容器 %s:%s" % (name, path)}

    def volume_backup(self, body, emit=None):
        name = str(body.get("name") or "")
        if not name:
            raise ValueError("缺少卷名")
        helper = str(body.get("helper_image") or "busybox:latest")
        try:
            self.docker.inspect_image(helper)
        except DockerError:
            self.docker.pull_ref(helper, None, timeout=300)
        tmp = "du-volbackup-" + hashlib.sha1(name.encode()).hexdigest()[:10]
        try:
            self.docker.remove_container(tmp, force=True, volumes=False)
        except Exception:
            pass
        if emit:
            emit(stage="创建临时打包容器", pct=10)
        created = self.docker.create_container(tmp, {
            "Image": helper, "HostConfig": {"Binds": ["%s:/_du_volume:ro" % name]},
            "Cmd": ["true"]})
        cid = (created or {}).get("Id") or ""
        try:
            if emit:
                emit(stage="打包卷内容", pct=45, detail=name)
            data = self.docker._request_raw("GET", "/containers/%s/archive?path=/_du_volume" % cid)[2]
        finally:
            self.docker.remove_container(cid or tmp, force=True, volumes=False)
        if emit:
            emit(stage="写入导出文件", pct=92)
        return {"ok": True, "data": base64.b64encode(data).decode(),
                "filename": "volume-%s-%s.tar" % (st._safe_id(name), time.strftime("%Y%m%d-%H%M")),
                "message": "卷「%s」已打包（不含跨机迁移元数据）" % name}

    # ---------------- P2 权限 / API / 审计 / 加速源 / 桌面 ----------------
    def roles(self):
        d = _load(ROLES_FILE, {"viewers": [], "readonly_mode": False})
        return {"ok": True, "viewers": [{"id": x["id"], "name": x["name"],
                                          "created": x.get("created", "")}
                                         for x in d["viewers"]],
                "readonly_mode": bool(d.get("readonly_mode")),
                "hint": "轻量化权限：查看者用只读链接访问，只能浏览，所有写操作返回 403；"
                        "不做复杂 RBAC。管理员仍用 admin 登录。"}

    def role_add(self, body):
        d = _load(ROLES_FILE, {"viewers": [], "readonly_mode": False})
        name = str(body.get("name") or "只读查看者").strip()[:40]
        key = hashlib.sha256(("%s-%s" % (time.time(), name)).encode()).hexdigest()[:24]
        d["viewers"].append({"id": key, "name": name, "created": _now()})
        _save(ROLES_FILE, d)
        self.audit("role-add", name, "只读查看者")
        return {"ok": True, "key": key, "message": "只读查看者已创建，复制只读链接给对方即可"}

    def role_delete(self, body):
        d = _load(ROLES_FILE, {"viewers": [], "readonly_mode": False})
        d["viewers"] = [x for x in d["viewers"] if x["id"] != str(body.get("id") or "")]
        _save(ROLES_FILE, d)
        return {"ok": True, "message": "只读查看者已移除"}

    def role_toggle(self, body):
        d = _load(ROLES_FILE, {"viewers": [], "readonly_mode": False})
        d["readonly_mode"] = bool(body.get("enabled"))
        _save(ROLES_FILE, d)
        return {"ok": True, "message": "只读模式已%s" % ("开启" if d["readonly_mode"] else "关闭")}

    def api_keys(self):
        return {"ok": True, "keys": [{"id": x["id"], "name": x["name"],
                                      "created": x["created"], "last_used": x.get("last_used", "")}
                                     for x in _load(APIKEY_FILE, [])],
                "docs": [{"method": "GET", "path": "/api/open/state", "desc": "容器与更新概览"},
                         {"method": "GET", "path": "/api/open/containers", "desc": "容器列表"},
                         {"method": "GET", "path": "/api/open/images", "desc": "本地镜像"},
                         {"method": "GET", "path": "/api/open/logs", "desc": "运行日志"}],
                "hint": "请求头带 X-API-Key；接口只读，供 HA / 脚本调用，不开放写操作。"}

    def apikey_create(self, body):
        rows = _load(APIKEY_FILE, [])
        key = hashlib.sha256(("%s-%s" % (time.time(), body.get("name"))).encode()).hexdigest()[:32]
        rows.append({"id": key, "name": str(body.get("name") or "HA 集成")[:40],
                     "created": _now(), "last_used": ""})
        _save(APIKEY_FILE, rows)
        return {"ok": True, "key": key, "message": "API Key 已生成（只显示这一次，请妥善保存）"}

    def apikey_revoke(self, body):
        _save(APIKEY_FILE, [x for x in _load(APIKEY_FILE, [])
                            if x["id"] != str(body.get("id") or "")])
        return {"ok": True, "message": "API Key 已撤销"}

    def audit_list(self, body):
        rows = _load(AUDIT_FILE, [])
        q = str(body.get("q") or "").lower()
        if q:
            rows = [x for x in rows if q in json.dumps(x, ensure_ascii=False).lower()]
        return {"ok": True, "rows": rows[:200], "total": len(rows)}

    def audit_clear(self):
        _save(AUDIT_FILE, [])
        return {"ok": True, "message": "审计日志已清空"}

    # ---------------- v5.0：镜像级智能选源 ----------------
    def _mirror_manifest_url(self, mirror_url, repo, tag):
        host = urllib.parse.urlparse(str(mirror_url or "")).netloc
        name = repo if "/" in repo else "library/" + repo
        scheme = "https"
        if "127.0.0.1" in host or host.startswith("localhost"):
            scheme = "http"
        return "%s://%s/v2/%s/manifests/%s" % (scheme, host, name, tag or "latest"), host

    def _probe_one_manifest(self, mirror, repo, tag, timeout=2.5):
        url, host = self._mirror_manifest_url(mirror.get("url"), repo, tag)
        if host.startswith("127.0.0.1") or host.startswith("localhost"):
            return {"ok": False, "reason": "容器内无法访问 NAS 本机地址", "latency_ms": 0}
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": "DockerUpdate/5.0",
            "Accept": "application/vnd.docker.distribution.manifest.v2+json, "
                      "application/vnd.oci.image.manifest.v1+json, */*"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code = r.status
                ctype = (r.headers.get("Content-Type") or "").lower()
                body = r.read(2048)
                ms = int((time.time() - t0) * 1000)
                # 有些公益源返回 200 的 HTML 错误页，不能当成「有镜像」
                if b"<html" in body[:400].lower() and b"manifest" not in body[:400].lower():
                    return {"ok": False, "reason": "返回 HTML 错误页", "latency_ms": ms}
                if code == 200:
                    return {"ok": True, "reason": "", "latency_ms": ms}
                return {"ok": False, "reason": "HTTP %d" % code, "latency_ms": ms}
        except urllib.error.HTTPError as e:
            ms = int((time.time() - t0) * 1000)
            if e.code in (401, 403):
                reason = "需要鉴权 / 不提供匿名 manifest"
            elif e.code == 404:
                reason = "该源未收录此镜像"
            elif e.code == 429:
                reason = "429 限流"
            else:
                reason = "HTTP %d" % e.code
            return {"ok": False, "reason": reason, "latency_ms": ms}
        except Exception as exc:
            msg = str(exc)
            reason = "超时" if ("timed out" in msg or "timeout" in msg.lower()) else msg[:60]
            return {"ok": False, "reason": reason, "latency_ms": 0}

    def image_probe(self, body):
        """按本机容器用到的镜像，逐个探测哪些 Docker 源真的有它。

        解决「源很多但某个源没有我的镜像」：不是按延迟选，而是先确认有，再比延迟。
        严格预算：默认最多 10 个源 × 20 个镜像，24 并发、单请求 2.5 秒。
        """
        from concurrent.futures import ThreadPoolExecutor
        try:
            limit_m = max(1, min(30, int((body or {}).get("mirrors") or 10)))
            limit_i = max(1, min(40, int((body or {}).get("images") or 20)))
        except Exception:
            limit_m, limit_i = 10, 20
        cfg = st.ACCEL.cfg()
        mirrors = [{"key": x.get("key"), "label": x.get("label") or x.get("key"),
                    "url": x.get("url")} for x in (cfg.get("docker") or [])
                   if x.get("url") and "127.0.0.1" not in str(x.get("url"))][:limit_m]
        if not mirrors:
            return {"ok": False, "message": "没有可探测的 Docker 镜像源，请先在「批量添加」里导入"}
        try:
            cs = self.docker.list_containers(all_=True) or []
        except Exception as exc:
            return {"ok": False, "message": "读取容器失败：%s" % str(exc)[:120]}
        seen, images = set(), []
        for c in cs:
            ref = str(c.get("Image") or "").strip()
            name = (c.get("Names") or [""])[0].lstrip("/")
            if not ref or ref.startswith("sha256:"):
                continue
            if "@sha256:" in ref:          # 固定 digest，不作为标签镜像探测
                continue
            repo, _, tag = ref.rpartition(":")
            if not repo or "/" in tag:     # 没有 Tag（如 nginx）或端口式仓库
                repo, tag = ref, "latest"
            key = repo + ":" + tag
            if key not in seen:
                seen.add(key)
                images.append({"ref": key, "repo": repo, "tag": tag, "containers": [name]})
            else:
                for x in images:
                    if x["ref"] == key and name and name not in x["containers"]:
                        x["containers"].append(name)
        images = images[:limit_i]
        if not images:
            return {"ok": False, "message": "本机没有可探测的标签镜像（多为固定 digest / 本地构建）"}

        t0 = time.time()
        jobs_ = [(m, im) for im in images for m in mirrors]

        def one(pair):
            m, im = pair
            r = self._probe_one_manifest(m, im["repo"], im["tag"])
            return (m["key"], im["ref"], r)

        results = {}
        with ThreadPoolExecutor(max_workers=24) as ex:
            for mk, ref, r in ex.map(one, jobs_):
                results.setdefault(ref, {})[mk] = r
        by_key = {m["key"]: m for m in mirrors}
        out = []
        for im in images:
            rs = []
            for m in mirrors:
                r = (results.get(im["ref"]) or {}).get(m["key"]) or {}
                rs.append({"key": m["key"], "label": m["label"], "url": m["url"],
                           "ok": bool(r.get("ok")), "latency_ms": r.get("latency_ms", 0),
                           "reason": r.get("reason", "")})
            good = sorted([x for x in rs if x["ok"]], key=lambda x: x["latency_ms"] or 99999)
            best = good[0] if good else None
            prefix = (best["url"].rstrip("/") + "/") if best else ""
            out.append({"ref": im["ref"], "repo": im["repo"], "tag": im["tag"],
                        "containers": im["containers"], "results": rs,
                        "available": len(good),
                        "best": best["label"] if best else "",
                        "best_url": prefix,
                        "best_ms": best["latency_ms"] if best else 0,
                        "pull_cmd": ("docker pull %s%s:%s" %
                                     (prefix.replace("https://", "").replace("http://", ""),
                                      im["repo"], im["tag"])) if best else ""})
        # 统计每个源覆盖了多少个本机镜像，供加速源列表展示
        coverage = {}
        for im in out:
            for r in im["results"]:
                if r["ok"]:
                    coverage[r["key"]] = coverage.get(r["key"], 0) + 1
        best_keys = sorted(coverage, key=lambda k: -coverage[k])[:5]
        daemon = {"registry-mirrors": [by_key[k]["url"] for k in best_keys
                                       if by_key.get(k, {}).get("url")]}
        result = {"ok": True, "at": _now(), "elapsed_s": round(time.time() - t0, 1),
                  "mirrors": mirrors, "images": out, "coverage": coverage,
                  "recommended": daemon,
                  "message": "已探测 %d 个镜像 × %d 个源：%d 个镜像至少有一个可用源，%d 个镜像全部源都没有"
                             % (len(out), len(mirrors),
                                sum(1 for x in out if x["available"]),
                                sum(1 for x in out if not x["available"]))}
        _save(IMAGE_SRC_FILE, result)
        return {"ok": True, "message": result["message"], "result": result}

    def image_probe_cache(self):
        r = _load(IMAGE_SRC_FILE, {})
        return {"ok": True, "result": r if isinstance(r, dict) else {},
                "message": "已读取上次探测结果" if r else "尚未按容器探测过镜像源"}

    def registry_autofix(self, emit=None):
        """v3.9-r4：后台任务入口。经加速源代拉镜像、重新打标签并重新检测。

        面板容器不改 NAS 的 daemon.json：这里用 Docker Engine API 直接从可用
        加速源拉取镜像并打回原始标签，让守护进程无需直连 Docker Hub。"""
        def noop(**kw): return None
        emit = emit or noop
        r = self.mgr.autofix_registry(None, emit)
        ok = int(r.get("ok_count") or 0)
        if ok:
            emit(stage="正在重新检测所有容器", pct=98, message="加速源代拉完成，正在比对版本…")
            try:
                self.mgr._job_check_all(False)
            except Exception as exc:
                r["message"] += "（重新检测异常：%s）" % str(exc)[:100]
        r["message"] = ("自动修复完成：%d 个容器已通过加速源代拉并重新检测，"
                        "%d 个中性跳过（不再显示连接仓库超时）"
                        % (ok, int(r.get("skip_count") or 0)))
        emit(stage="操作已完成", pct=100, message=r["message"])
        return r

    def registry_diagnose(self):
        """v3.9-r3：实测各镜像仓库连通性，给出可直接复制的 daemon.json 片段。"""
        from concurrent.futures import ThreadPoolExecutor
        import urllib.error

        cands = [
            ("Docker Hub 官方", "https://registry-1.docker.io/v2/", "official"),
            ("Docker Hub 鉴权", "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull", "official"),
        ]
        seen = set()
        for label, url in (
            ("轩辕镜像", "https://docker.xuanyuan.me/v2/"),
            ("DaoCloud", "https://docker.m.daocloud.io/v2/"),
            ("1ms.run", "https://docker.1ms.run/v2/"),
            ("1Panel", "https://docker.1panel.live/v2/"),
        ):
            cands.append((label, url, "mirror"))
            seen.add(url)
        try:
            for x in (st.ACCEL.cfg().get("docker") or [])[:8]:
                u = str(x.get("url") or "").strip().rstrip("/")
                if not u.startswith("http") or "127.0.0.1" in u or "localhost" in u:
                    continue
                probe = u + "/v2/"
                if probe not in seen:
                    cands.append((str(x.get("label") or "自定义加速源"), probe, "mirror"))
                    seen.add(probe)
        except Exception:
            pass

        def probe(item):
            label, url, kind = item
            t0 = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "DockerUpdate-Diagnose/1.0"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    resp.read(64)
                    stt, oky = "HTTP %s" % resp.status, True
            except urllib.error.HTTPError as exc:
                stt = "HTTP %s%s" % (exc.code, "（需要令牌，属正常）" if exc.code == 401 else "")
                oky = exc.code in (200, 401, 403)
            except Exception as exc:
                stt, oky = "%s: %s" % (type(exc).__name__, str(exc)[:70]), False
            return {"name": label, "url": url, "kind": kind, "ok": oky,
                    "ms": int((time.time() - t0) * 1000), "status": stt}

        try:
            with ThreadPoolExecutor(max_workers=6) as ex:
                results = list(ex.map(probe, cands))
        except Exception:
            results = [probe(x) for x in cands]
        direct_ok = any(r["ok"] for r in results if r["kind"] == "official")
        mirrors = [r["url"][:-4].rstrip("/") for r in sorted(
            (r for r in results if r["ok"] and r["kind"] == "mirror"),
            key=lambda x: x["ms"])][:4]
        if not mirrors:
            mirrors = ["https://docker.m.daocloud.io", "https://docker.1ms.run",
                       "https://docker.xuanyuan.me"]
        pre = {}
        try:
            pre = self.mgr.state.data.get("registry_precheck") or {}
            settings = self.mgr.settings()
        except Exception:
            settings = {}
        return {
            "ok": True, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results": results, "direct_ok": bool(direct_ok),
            "mirrors": mirrors, "precheck": pre,
            "check_timeout": int(settings.get("check_timeout", 300)),
            "check_retries": int(settings.get("check_retries", 1)),
            "daemon_json": json.dumps({"registry-mirrors": mirrors}, indent=2, ensure_ascii=False),
            "advice": ("面板容器对宿主机根目录是只读挂载，无法代改 Docker 守护进程配置。"
                       "请 SSH 到 NAS 写入 /etc/docker/daemon.json 后执行 systemctl restart docker"
                       "（或 service docker restart），重启后回来点「重试全部检测」。"
                       "若配置后仍超时，多为代理 / DNS 问题，需检查 NAS 自身的网络出口。"),
        }

    def accel(self):
        cfg = st.ACCEL.cfg()
        cache = _load(IMAGE_SRC_FILE, {})
        return {"ok": True, "config": cfg,
                "image_probe": ({"at": cache.get("at", ""),
                                 "images": len(cache.get("images") or []),
                                 "coverage": cache.get("coverage") or {},
                                 "available": sum(1 for x in (cache.get("images") or [])
                                                  if x.get("available"))}
                                if isinstance(cache, dict) and cache.get("images") else None),
                "hint": "%d 条 GitHub 加速线路用于索引 / 图标 / README 获取，%d 个 Docker 镜像源与 %d 条非 Hub 前缀供复制；"
                        "Docker daemon 拉镜像不走这里的代理，需在 NAS 上另配 registry mirror 并重启 Docker。"
                        % (len(cfg.get("gh") or []), len(cfg.get("docker") or []),
                           len(cfg.get("repo") or []))}

    def accel_add(self, body):
        name = str(body.get("name") or "").strip()
        url = str(body.get("url") or "").strip()
        if not name or not url.startswith(("http://", "https://")):
            raise ValueError("请填加速源名称与完整 http(s) 前缀地址")
        r = st.ACCEL.add(name, url)
        return {"ok": True, "message": "加速源已添加，点「立即测速」刷新延迟",
                "config": st.ACCEL.cfg()}

    def accel_batch(self, body):
        r = st.ACCEL.add_batch(str(body.get("text") or body.get("lines") or ""),
                               str(body.get("target") or "gh"))
        return {"ok": True, "message": r["message"], "result": r,
                "config": st.ACCEL.cfg()}

    def accel_delete(self, body):
        key = str(body.get("key") or "")
        st.ACCEL.delete(key)
        return {"ok": True, "message": "加速源已删除", "config": st.ACCEL.cfg()}

    def accel_restore(self, body):
        st.ACCEL.restore()
        return {"ok": True, "message": "已恢复内置加速源", "config": st.ACCEL.cfg()}

    def accel_probe(self, body):
        keys = (body or {}).get("keys")
        if keys is not None and not isinstance(keys, list):
            keys = None
        r = st.ACCEL.check(keys)
        return {"ok": True, "message": (r or {}).get("message") or "测速完成，已自动选择最快线路",
                "config": st.ACCEL.cfg(), "result": r if isinstance(r, dict) else {}}

    def accel_save(self, body):
        r = st.ACCEL.save(body)
        return r if isinstance(r, dict) and r.get("ok") is not None \
            else {"ok": True, "message": "加速源设置已保存", "config": st.ACCEL.cfg()}

    def desktop_config(self):
        return {"ok": True, "status": self.desktop.status()}

    def desktop_save(self, body):
        return self.desktop.save(body)

    # ---------------- 只读查看者 / 开放 API ----------------
    def is_viewer(self, key):
        if not key:
            return False
        return any(x["id"] == key for x in _load(ROLES_FILE, {"viewers": []}).get("viewers", []))

    def is_apikey(self, key):
        if not key:
            return False
        rows = _load(APIKEY_FILE, [])
        for x in rows:
            if x["id"] == key:
                x["last_used"] = _now()
                _save(APIKEY_FILE, rows)
                return True
        return False

    def open_data(self, what):
        if what == "containers":
            try:
                cs = self.docker.list_containers(all_=True) or []
            except Exception:
                cs = []
            return {"ok": True, "containers": [
                {"name": (c.get("Names") or [""])[0].lstrip("/"), "state": c.get("State", ""),
                 "image": c.get("Image", "")} for c in cs]}
        if what == "images":
            try:
                imgs = self.docker.list_images() or []
            except Exception:
                imgs = []
            return {"ok": True, "images": [
                {"id": i.get("Id", ""), "tags": i.get("RepoTags") or [],
                 "size": i.get("Size", 0)} for i in imgs]}
        if what == "logs":
            self.mgr.state.prune_history()
            return {"ok": True, "history": self.mgr.state.data.get("history", [])[:1000]}
        d = mgr_snapshot_safe(self.mgr)
        return {"ok": True, "state": d}

    # ---------------- 汇总状态 ----------------
    def state(self):
        try:
            n_stacks = len(self.stacks()["stacks"])
        except Exception:
            n_stacks = 0
        return {"ok": True, "version": config.APP_VERSION, "settings": self.settings(),
                "overview": self.overview(), "templates": self.templates()["total"],
                "stacks": n_stacks,
                "tasks": len(_load(TASKS_FILE, [])),
                "creds": len(self.creds()["creds"]),
                "hosts": len(self.hosts()["hosts"]),
                "audit": len(_load(AUDIT_FILE, [])),
                "desktop": self.desktop.status(),
                "capabilities": {
                    "p0": "概览补充统计 / 容器明细与红点 / Web 终端 / 进程 / Inspect / 日志增强 / "
                          "增强创建容器 / fnOS 目录树 / 镜像层 / 孤立卷 / 高级网络 / Compose 栈全套",
                    "p1": "内置 8 个应用模板 + 自定义导入 + 版本选择 / 只拉不重启预览 / 网络 IO 曲线 / "
                          "通知中心适配器 / 计划任务 / 多仓库凭证 / 更新失败推送",
                    "p2": "容器目录映射（飞牛文件夹 → 容器内自定义路径，保存后手动应用重建）/ "
                          "多 Docker 连接（TCP/TLS/SSH 隧道）/ trivy 扫描（需外部二进制）/ 容器内文件管理 / "
                          "卷打包备份 / macvlan+ipvlan / 只读 REST API / 只读查看者 / 操作审计",
                    "limits": "fnOS 系统私有接口（原生桌面图标、通知中心、系统账号）不内置地址，"
                              "未配置时明确跳过；trivy / ssh 不存在时明确报错；"
                              "Compose 为受限解析，不支持 build / anchors / extends / 多行块。"}}

    # ---------------- 动作分发 ----------------
    LONG = {
        "stack-up": ("启动 Compose 栈", ["读取并校验 YAML", "拉取镜像", "创建并启动服务", "校验状态"]),
        "stack-down": ("停止并移除栈容器", ["读取栈信息", "停止并移除容器", "核对结果"]),
        "stack-delete": ("删除 Compose 栈", ["停止并移除容器", "删除配置目录", "核对结果"]),
        "template-deploy": ("部署应用模板", ["写 Compose 与 .env", "拉取镜像", "创建并启动服务", "同步桌面图标"]),
        "volume-backup": ("打包数据卷", ["创建临时打包容器", "打包卷内容", "写入导出文件"]),
        "trivy-scan": ("镜像漏洞扫描", ["检查 trivy", "扫描镜像层", "汇总结果"]),
        "update-preview": ("拉取镜像（不重启）", ["连接仓库", "拉取镜像", "写入本地镜像库"]),
        # v6.0-r18：镜像更新（只拉取，不重建容器）
        "image-update-check": ("检查镜像更新", ["读取本地镜像与候选源", "比对远端 digest", "写回检测结果"]),
        "image-update-pull": ("更新选中的镜像", ["准备拉取", "拉取并校验镜像", "汇总结果"]),
        # v7.0.4：镜像更新页第二种模式，拉取后按原配置重建使用该镜像的容器
        "image-update-rebuild": ("更新镜像并重建容器",
                                ["校验镜像与关联容器", "拉取并校验新镜像", "生成版本备份",
                                 "按原配置重建容器", "核对启动状态"]),
        # v6.0-r24：应用目录映射必须重建容器，走后台任务 + SSE 真实进度
        "mounts-apply": ("应用目录映射并重建容器",
                        ["校验路径与冲突", "版本备份", "可选数据预迁移",
                         "重建容器并保留全部原配置", "核对挂载结果"]),
        # v7.0.2：文件软链接一键挂载（先生成 B 文件，再合并映射并重建容器）
        "symlinks-apply": ("把链接目录挂载进容器",
                          ["校验规则与路径", "生成 B 文件", "合并目录映射",
                           "重建容器并保留全部原配置", "核对挂载结果"]),
    }

    def action(self, kind, body, ctx=None):
        # v7.0.3：兼容历史直接传字符串 / 畸形入参，避免 body.get 抛 500
        if isinstance(body, str):
            body = {"name": body}
        elif not isinstance(body, dict):
            body = {}
        runtrace.note("Pro.action", "调用 kind=%s · %s" %
                      (kind, runtrace.brief(body)), "step")
        _fn_map = {
            "settings": "save_settings", "overview": "overview",
            "container-table": "container_table", "process": "process", "inspect": "inspect",
            "netio": "netio", "dirs": "dirs",
            "term-open": "term_open", "term-read": "term_read", "term-write": "term_write",
            "term-resize": "term_resize", "term-close": "term_close",
            "create-advanced": "create_advanced", "run-command": "run_command",
            "image-history": "image_history", "update-preview": "update_preview",
            "volumes-orphan": "volumes_orphan", "volumes-prune": "volumes_prune",
            "network-advanced": "network_advanced",
            "stacks": "stacks", "stack-read": "stack_read", "stack-save": "stack_save",
            "stack-down": "stack_down", "stack-restart": "stack_restart",
            "stack-delete": "stack_delete", "stack-logs": "stack_logs",
            "stack-export": "stack_export", "stack-import": "stack_import",
            "yaml-check": lambda b: yaml_check(str(b.get("text") or "")),
            "yaml-format": lambda b: yaml_format(str(b.get("text") or "")),
            "templates": "templates", "template-add": "template_add",
            "template-delete": "template_delete",
            "creds": "creds", "creds-save": "creds_save", "creds-delete": "creds_delete",
            "notify-config": "notify_config", "notify-save": "notify_save", "notify-test": "notify_test",
            "notify-disable-alert": "notify_disable_alert",
            "tasks": "tasks", "task-save": "task_save", "task-delete": "task_delete",
            "task-run": "task_run", "task-toggle": "task_toggle", "task-options": "task_options",
            "hosts": "hosts", "host-save": "host_save", "host-delete": "host_delete",
            "host-test": "host_test", "host-containers": "host_containers",
            "trivy-scan": "trivy_scan", "file-list": "file_list", "file-get": "file_get",
            "file-put": "file_put", "volume-backup": "volume_backup",
            "roles": "roles", "role-add": "role_add", "role-delete": "role_delete",
            "role-toggle": "role_toggle", "api-keys": "api_keys",
            "apikey-create": "apikey_create", "apikey-revoke": "apikey_revoke",
            "audit-list": "audit_list", "audit-clear": lambda b: self.audit_clear(),
            "registry-diagnose": "registry_diagnose",
            "accel": "accel", "accel-save": "accel_save", "accel-add": "accel_add",
            "accel-delete": "accel_delete", "accel-restore": "accel_restore",
            "accel-probe": "accel_probe", "accel-batch": "accel_batch",
            "image-probe": "image_probe", "image-probe-cache": "image_probe_cache",
            "desktop-config": "desktop_config", "desktop-save": "desktop_save",
            # v6.0-r17：整机端口占用 / 外网连通性监测
            "ports": lambda b: ports_mod.scan(self.docker, force=bool(b.get("force"))),
            "ports-check": lambda b: ports_mod.check(
                self.docker, b.get("port"), str(b.get("proto") or "tcp")),
            "netwatch": lambda b: netwatch_mod.status(),
            "netwatch-save": lambda b: _netwatch_save(b),
            "netwatch-probe": lambda b: netwatch_mod.evaluate(),
            "netwatch-test": lambda b: netwatch_mod.test(str(b.get("event") or "net_down")),
            # v6.0-r18：镜像更新独立页面（读取 / 清缓存；检测与拉取走 LONG 后台任务）
            "image-updates": lambda b: self.image_updates.status(),
            "image-updates-clear": lambda b: self.image_updates.clear(),
            # v7.0.4：更新并重建前先看会牵连哪些容器（只算不写）
            "image-rebuild-plan": lambda b: image_rebuild_mod.plan(
                self.docker, self.mgr, b.get("refs")),
            # v6.0-r24：容器目录映射（两端路径都可自定义；保存不碰容器，应用才重建）
            "mounts-list": lambda b: mounts_mod.list_all(self.docker),
            "mounts-status": lambda b: mounts_mod.status(
                self.docker, str(b.get("name") or "")),
            "mounts-save": lambda b: mounts_mod.save(b),
            "mounts-plan": lambda b: mounts_mod.plan(
                self.docker, self.mgr, str(b.get("name") or "")),
            "mounts-dirs": lambda b: mounts_mod.browse_dirs(b.get("path")),
            "mounts-precopy-check": lambda b: mounts_mod.precopy_check(
                self.docker, str(b.get("name") or ""), b.get("mappings")),
            # v6.0-r25：后续新容器默认模板 / 策略 / 立即扫描
            # v7.0.1：功能总开关（只改 policy.master，不动模板与排除名单）
            "mounts-template": lambda b: mounts_mod.template_get(),
            "mounts-template-save": lambda b: mounts_mod.template_save(b),
            "mounts-master": lambda b: mounts_mod.set_master(b.get("enabled")),
            "mounts-sync": lambda b: mounts_mod.sync_new(
                self.docker, self.mgr, do_apply=bool(b.get("apply")), manual=True),
            # v7.0.2：文件软链接（读 / 写分开，写操作一律进运行日志）
            "symlinks-list": lambda b: symlinks_mod.list_all(self.docker),
            "symlinks-save": lambda b: symlinks_mod.save(b),
            "symlinks-run": lambda b: symlinks_mod.run_rules(b.get("ids"), bool(b.get("force"))),
            "symlinks-delete": lambda b: symlinks_mod.delete(b.get("id")),
            "symlinks-dirs": lambda b: symlinks_mod.dirs(b.get("path")),
        }
        # 分派值有两种：方法名字符串（需绑定到 self）与 lambda（本身可调用）。
        # 旧写法把 lambda 直接塞进 getattr 的属性名位置，Python 会抛
        # "getattr(): attribute name must be string" → 真实服务返回 500。
        _raw = _fn_map.get(kind)
        fn = (_raw if callable(_raw)
              else getattr(self, _raw, None) if isinstance(_raw, str) and _raw
              else None)
        if fn is None:
            raise ValueError("未知操作：%s" % kind)
        # 有些汇总方法（overview / stacks / templates / creds …）不需要 body，
        # 这里按签名决定传参，避免 action() 统一 fn(body) 时抛 TypeError。
        try:
            takes_body = len(inspect.signature(fn).parameters) > 0
        except (TypeError, ValueError):
            takes_body = True
        result = fn(body) if takes_body else fn()
        if kind not in ("term-read", "term-write", "term-resize", "overview", "container-table",
                        "task-options",
                        "stacks", "stack-read", "stack-logs", "templates", "dirs", "audit-list",
                        "roles", "api-keys", "hosts", "tasks", "creds", "notify-config",
                        "yaml-check", "yaml-format", "accel", "accel-probe",
                        "image-probe-cache",
                        "desktop-config", "state", "image-updates",
                        "image-rebuild-plan",
                        "ports", "ports-check", "netwatch", "netwatch-probe",
                        "mounts-list", "mounts-status", "mounts-plan",
                        "mounts-dirs", "mounts-precopy-check", "mounts-template",
                        "symlinks-list", "symlinks-dirs"):
            self.audit(kind, body.get("name") or body.get("id") or body.get("registry") or "",
                       (result or {}).get("message", "ok") if isinstance(result, dict) else "ok", ctx)
        return result

    def submit(self, kind, body, ctx=None):
        if kind not in self.LONG:
            return self.action(kind, body, ctx)
        label, stages = self.LONG[kind]
        target = str(body.get("name") or body.get("ref") or body.get("id") or "")
        if target:
            label = "%s「%s」" % (label, target)
        def run(emit):
            if kind == "stack-up":
                return self.stack_up(body, emit=emit)
            if kind == "stack-down":
                return self.stack_down(body, emit=emit)
            if kind == "stack-delete":
                return self.stack_delete(body, emit=emit)
            if kind == "template-deploy":
                return self.template_deploy(body, emit=emit)
            if kind == "volume-backup":
                return self.volume_backup(body, emit=emit)
            if kind == "trivy-scan":
                r = self.trivy_scan(body)
                if not r.get("ok"):
                    raise ValueError(r.get("error", "扫描失败"))
                return r
            if kind == "update-preview":
                return self.update_preview(body)
            if kind == "image-update-check":
                return self.image_updates.check(body.get("refs"), emit=emit)
            if kind == "image-update-pull":
                return self.image_updates.pull(body.get("refs"), emit=emit)
            if kind == "image-update-rebuild":
                return image_rebuild_mod.apply(
                    self.docker, self.mgr, body.get("refs"), emit=emit,
                    force=bool(body.get("force")), names=body.get("names"),
                    backup=body.get("backup"))
            if kind == "mounts-apply":
                return mounts_mod.apply(self.docker, self.mgr, body, emit=emit)
            if kind == "symlinks-apply":
                return symlinks_mod.apply_mount(self.docker, self.mgr, body, emit=emit)
            return self.action(kind, body, ctx)
        jid = jobs_mod.JOBS.submit("pro-" + kind, label, stages, run)
        return {"ok": True, "async": True, "job_id": jid,
                "message": "「%s」已提交后台任务，进度条实时显示" % label}


def mgr_snapshot_safe(mgr):
    """开放接口只暴露与容器 / 更新相关的字段，绝不返回令牌、密码、会话等敏感信息。"""
    try:
        d = mgr.snapshot()
    except Exception:
        return {}
    return {"containers": d.get("containers", []), "settings": {
        k: v for k, v in (d.get("settings") or {}).items()
        if k in ("daily_time", "auto_update", "monitor_enabled")},
        "last_check": d.get("last_check", ""), "docker_version": d.get("docker_version", "")}


def datetime_now():
    from datetime import datetime
    return datetime.now(config.LOCAL_TZ)
