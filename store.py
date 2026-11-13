"""DockerUpdate v3.0 · Docker 管理底层工具

纯 Python 标准库实现，不引入 PyYAML / docker-compose 二进制：
  - 应用源：内置离线模板源、URL JSON 索引、本地文件、飞牛官方商店（OAuth + 网关代理）
  - 安装：把 compose / env / README / 变更日志写进用户指定的宿主机目录，再用 Docker Engine API 建容器
  - 卸载：按安装清单精确删除容器 / 网络 / 卷 / 文件夹，绝不按模糊名字删
  - 更新：对比源索引版本与 compose 内容指纹，支持一键更新与每日自动更新
  - .fnp：按 zip / tar 解析其中的 compose 与文档，只读取文本文件、绝不执行包内脚本
  - 桌面图标 / 官方商店：做成可配置适配器，未配置时明确回退，不伪造飞牛私有接口
"""
import base64
import copy
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import config
from docker_api import DockerError
from jobs import JOBS

# v2.1-r11：这些操作可能跑几十秒到几分钟，飞牛远程访问 / 反向代理网关会在
# 60 秒左右返回 504。统一改成「立即返回 job_id + 后台线程 + SSE 进度」。
LONG_OPS = {
    "install": ("安装应用", ["写入配置与安装清单", "拉取容器镜像", "创建网络 / 卷 / 容器",
                           "同步桌面图标", "校验并回写状态"]),
    "uninstall": ("卸载应用", ["停止并移除容器", "清理网络与卷", "删除应用目录与下载文件",
                             "移除桌面图标", "核对安装清单"]),
    "update": ("更新应用", ["比对版本与备份清单", "拉取新镜像", "重建容器", "校验并回写状态"]),
    "update-all": ("批量更新应用", ["收集可更新应用", "逐个备份", "逐个重建容器",
                                 "校验结果并汇总"]),
    "fpk-download": ("下载 FPK 安装包", ["连接下载地址", "下载安装包", "校验 SHA256", "写入下载目录"]),
    "fpk-direct-install": ("解包并以 Docker 容器安装", ["下载并校验 FPK 安装包", "解包读取 Compose / 镜像定义",
                                                "创建网络、卷与容器", "同步桌面图标与安装清单"]),
    "check-updates": ("检查应用更新", ["读取已安装应用清单", "并发同步相关应用源",
                                  "比对版本与 Compose 指纹", "写回检测结果"]),
}
# 这些 kind 即使没有下载地址也要先在提交前拦下来，避免任务提交后才报错
_PRECHECK_FPK = ("fpk-download", "fpk-direct-install")

STORE_DIR = os.path.join(config.DATA_DIR, "store")
CACHE_DIR = os.path.join(STORE_DIR, "cache")

# 容器内可写根 ↔ 宿主机路径。compose 里把宿主机 /vol1/1000/docker 以 rw 挂到 /store
# （与 /:/host:ro 平级，避免在只读父挂载内创建子挂载点导致容器起不来）
STORE_HOST_ROOT = config.STORE_HOST_ROOT.rstrip("/")


def _probe_store_root():
    """兼容只改了挂载行、忘了改 STORE_ROOT 环境变量的旧配置：
    若配置的根仍落在只读的 /host 下而 /store 可写，就自动切到 /store。"""
    root = config.STORE_ROOT.rstrip("/")
    if root.startswith("/host/") and os.path.isdir("/store"):
        probe = os.path.join("/store", ".du-write-probe")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            print("[store] STORE_ROOT=%s 位于只读挂载内，已自动切换到 /store" % root, flush=True)
            return "/store"
        except OSError:
            pass
    return root


STORE_ROOT = _probe_store_root()

DEFAULT_SETTINGS = {
    "install_root": STORE_HOST_ROOT + "/apps",
    "download_root": STORE_HOST_ROOT + "/downloads",
    "auto_update": False,
    "check_daily": True,
    "desktop": {"mode": "off", "webhook_url": "", "webhook_token": "", "file_dir": ""},
}
DEFAULT_OAUTH = {
    "enabled": False,
    "client_id": "", "client_secret": "",
    "authorize_url": "", "token_url": "", "gateway_url": "",
    # 自动授权：由飞牛本地网关/自建代理签发 code 的端点（可选）。
    # 面板服务端直接请求它拿 code → 自动换 token，全程无需人工复制粘贴。
    "auto_authorize_url": "",
    "redirect_uri": "", "scope": "appstore.read",
    "access_token": "", "refresh_token": "", "expires_at": 0,
    # v2.1-r7：可同时存在多个待校验 state（重复发起授权 / 多标签页 / 自动授权
    # 不再互相踢掉），每个带创建时间、来源与一次性标记，30 分钟内有效。
    "pending": [],
    "state": "", "last_error": "", "authorized_at": "",
}

# ---------------------------------------------------------------- 内置离线目录
# 这些是「自带 Compose 模板」的应用，不依赖任何第三方索引 URL，离线也能装。
# 镜像名是公开仓库引用，真正拉取时仍受 NAS 到 Docker Hub 的网络影响。
_PRESETS = [
    {
        "id": "builtin-media", "name": "影音媒体 · 内置模板", "desc": "Jellyfin / Plex / Emby 等常用影音服务",
        "apps": [
            {"id": "jellyfin", "name": "Jellyfin", "version": "10.9.11", "icon": "🎬", "category": "影音",
             "summary": "开源媒体服务器，刮削、转码、多端播放", "author": "Jellyfin", "homepage": "https://jellyfin.org",
             "tags": ["media", "影音"], "readme": "# Jellyfin\n\n开源媒体服务器。首次启动后访问 `http://NAS_IP:8096` 完成初始化。\n\n- 把影视目录挂到 `/media`\n- 配置与缓存保存在应用目录内，卸载会一并删除\n",
             "changelog": "## 10.9.11\n- 模板初始版本\n", "env": "TZ=Asia/Shanghai\n",
             "services": {"jellyfin": {"image": "jellyfin/jellyfin:10.9.11",
                "ports": ["8096:8096/tcp"], "volumes": ["./config:/config", "./cache:/cache", "/media:/media"],
                "environment": ["TZ=Asia/Shanghai"], "restart": "unless-stopped"}}},
            {"id": "plex", "name": "Plex", "version": "1.40.5", "icon": "📺", "category": "影音",
             "summary": "成熟的个人媒体库与串流服务端", "author": "Plex", "homepage": "https://www.plex.tv",
             "tags": ["media"], "readme": "# Plex\n\n访问 `http://NAS_IP:32400/web`，首次需要 Plex 账号与 Claim Token。\n",
             "changelog": "## 1.40.5\n- 模板初始版本\n", "env": "TZ=Asia/Shanghai\n",
             "services": {"plex": {"image": "plexinc/pms-docker:latest",
                "ports": ["32400:32400/tcp"], "volumes": ["./config:/config", "./transcode:/transcode", "/media:/data"],
                "environment": ["TZ=Asia/Shanghai", "PLEX_CLAIM="], "restart": "unless-stopped"}}},
            {"id": "emby", "name": "Emby Server", "version": "4.8.10", "icon": "🎞️", "category": "影音",
             "summary": "媒体库管理，客户端覆盖广", "author": "Emby", "homepage": "https://emby.media",
             "tags": ["media"], "readme": "# Emby Server\n\n访问 `http://NAS_IP:8096`。\n",
             "changelog": "## 4.8.10\n- 模板初始版本\n", "env": "TZ=Asia/Shanghai\n",
             "services": {"emby": {"image": "emby/embyserver:latest",
                "ports": ["8096:8096/tcp"], "volumes": ["./config:/config", "/media:/mnt/media"],
                "environment": ["TZ=Asia/Shanghai"], "restart": "unless-stopped"}}},
        ],
    },
    {
        "id": "builtin-download", "name": "下载工具 · 内置模板", "desc": "qBittorrent / Transmission / SABnzbd",
        "apps": [
            {"id": "qbittorrent", "name": "qBittorrent", "version": "4.6.7", "icon": "🧲", "category": "下载",
             "summary": "BT 下载客户端，WebUI 轻量好用", "author": "LinuxServer", "homepage": "https://www.qbittorrent.org",
             "tags": ["download"], "readme": "# qBittorrent\n\n访问 `http://NAS_IP:8080`，默认密码在容器日志里。\n",
             "changelog": "## 4.6.7\n- 模板初始版本\n", "env": "PUID=1000\nPGID=1000\nTZ=Asia/Shanghai\n",
             "services": {"qbittorrent": {"image": "linuxserver/qbittorrent:latest",
                "ports": ["8080:8080/tcp", "6881:6881/tcp", "6881:6881/udp"],
                "volumes": ["./config:/config", "./downloads:/downloads"],
                "environment": ["PUID=1000", "PGID=1000", "TZ=Asia/Shanghai", "WEBUI_PORT=8080"],
                "restart": "unless-stopped"}}},
            {"id": "transmission", "name": "Transmission", "version": "4.0.6", "icon": "⬇️", "category": "下载",
             "summary": "极简 BT 客户端", "author": "LinuxServer", "homepage": "https://transmissionbt.com",
             "tags": ["download"], "readme": "# Transmission\n\n访问 `http://NAS_IP:9091`。\n",
             "changelog": "## 4.0.6\n- 模板初始版本\n", "env": "PUID=1000\nPGID=1000\nTZ=Asia/Shanghai\n",
             "services": {"transmission": {"image": "linuxserver/transmission:latest",
                "ports": ["9091:9091/tcp", "51413:51413/tcp", "51413:51413/udp"],
                "volumes": ["./config:/config", "./downloads:/downloads", "./watch:/watch"],
                "environment": ["PUID=1000", "PGID=1000", "TZ=Asia/Shanghai"], "restart": "unless-stopped"}}},
            {"id": "sabnzbd", "name": "SABnzbd", "version": "4.3.2", "icon": "📦", "category": "下载",
             "summary": "Usenet 下载与自动解包", "author": "LinuxServer", "homepage": "https://sabnzbd.org",
             "tags": ["download"], "readme": "# SABnzbd\n\n访问 `http://NAS_IP:8080` 完成向导。\n",
             "changelog": "## 4.3.2\n- 模板初始版本\n", "env": "PUID=1000\nPGID=1000\nTZ=Asia/Shanghai\n",
             "services": {"sabnzbd": {"image": "linuxserver/sabnzbd:latest",
                "ports": ["8080:8080/tcp"], "volumes": ["./config:/config", "./downloads:/downloads"],
                "environment": ["PUID=1000", "PGID=1000", "TZ=Asia/Shanghai"], "restart": "unless-stopped"}}},
        ],
    },
    {
        "id": "builtin-home", "name": "家庭自动化 · 内置模板", "desc": "Home Assistant / MQTT / Node-RED",
        "apps": [
            {"id": "home-assistant", "name": "Home Assistant", "version": "2024.11.3", "icon": "🏠", "category": "智能家居",
             "summary": "本地优先的智能家居中枢", "author": "Home Assistant", "homepage": "https://www.home-assistant.io",
             "tags": ["iot"], "readme": "# Home Assistant\n\n访问 `http://NAS_IP:8123`。配置目录已持久化到应用目录。\n",
             "changelog": "## 2024.11.3\n- 模板初始版本\n", "env": "TZ=Asia/Shanghai\n",
             "services": {"homeassistant": {"image": "ghcr.io/home-assistant/home-assistant:stable",
                "ports": ["8123:8123/tcp"], "volumes": ["./config:/config", "/etc/localtime:/etc/localtime:ro"],
                "environment": ["TZ=Asia/Shanghai"], "restart": "unless-stopped"}}},
            {"id": "mosquitto", "name": "Eclipse Mosquitto", "version": "2.0.20", "icon": "📡", "category": "智能家居",
             "summary": "轻量 MQTT Broker", "author": "Eclipse", "homepage": "https://mosquitto.org",
             "tags": ["iot", "mqtt"], "readme": "# Mosquitto\n\n默认监听 `1883`，配置放 `./config/mosquitto.conf`。\n",
             "changelog": "## 2.0.20\n- 模板初始版本\n", "env": "",
             "services": {"mosquitto": {"image": "eclipse-mosquitto:2",
                "ports": ["1883:1883/tcp", "9001:9001/tcp"],
                "volumes": ["./config:/mosquitto/config", "./data:/mosquitto/data", "./log:/mosquitto/log"],
                "restart": "unless-stopped"}}},
            {"id": "node-red", "name": "Node-RED", "version": "4.0.5", "icon": "🔀", "category": "智能家居",
             "summary": "可视化流程编排", "author": "Node-RED", "homepage": "https://nodered.org",
             "tags": ["iot", "automation"], "readme": "# Node-RED\n\n访问 `http://NAS_IP:1880`。\n",
             "changelog": "## 4.0.5\n- 模板初始版本\n", "env": "TZ=Asia/Shanghai\n",
             "services": {"nodered": {"image": "nodered/node-red:latest",
                "ports": ["1880:1880/tcp"], "volumes": ["./data:/data"],
                "environment": ["TZ=Asia/Shanghai"], "restart": "unless-stopped"}}},
        ],
    },
    {
        "id": "builtin-ops", "name": "运维工具 · 内置模板", "desc": "Uptime Kuma / Watchtower / Nginx Proxy Manager",
        "apps": [
            {"id": "uptime-kuma", "name": "Uptime Kuma", "version": "1.23.13", "icon": "📈", "category": "运维",
             "summary": "自托管监控与状态页", "author": "Louis Lam", "homepage": "https://github.com/louislam/uptime-kuma",
             "tags": ["monitor"], "readme": "# Uptime Kuma\n\n访问 `http://NAS_IP:3001`，首次创建管理员账号。\n",
             "changelog": "## 1.23.13\n- 模板初始版本\n", "env": "",
             "services": {"uptime-kuma": {"image": "louislam/uptime-kuma:1",
                "ports": ["3001:3001/tcp"], "volumes": ["./data:/app/data"],
                "restart": "unless-stopped"}}},
            {"id": "watchtower", "name": "Watchtower", "version": "1.7.1", "icon": "🔭", "category": "运维",
             "summary": "自动更新运行中的容器（与本面板的更新中心可二选一）", "author": "containrrr", "homepage": "https://containrrr.dev/watchtower/",
             "tags": ["docker"], "readme": "# Watchtower\n\n⚠️ 它也会扫描并更新容器。若已用本面板的「自动更新」，建议给不想被动的容器加 `com.centurylinklabs.watchtower.enable=false` 标签。\n",
             "changelog": "## 1.7.1\n- 模板初始版本\n", "env": "TZ=Asia/Shanghai\n",
             "services": {"watchtower": {"image": "containrrr/watchtower:latest",
                "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
                "environment": ["TZ=Asia/Shanghai", "WATCHTOWER_CLEANUP=false", "WATCHTOWER_POLL_INTERVAL=86400"],
                "restart": "unless-stopped"}}},
            {"id": "npm", "name": "Nginx Proxy Manager", "version": "2.12.3", "icon": "🌐", "category": "运维",
             "summary": "可视化反向代理与证书管理", "author": "jc21", "homepage": "https://nginxproxymanager.com",
             "tags": ["proxy"], "readme": "# Nginx Proxy Manager\n\n管理界面 `http://NAS_IP:81`，默认 `admin@example.com / changeme`。⚠️ 需要占用 80/443，请先确认未被飞牛自带 Web 服务占用。\n",
             "changelog": "## 2.12.3\n- 模板初始版本\n", "env": "",
             "services": {"npm": {"image": "jc21/nginx-proxy-manager:latest",
                "ports": ["80:80/tcp", "81:81/tcp", "443:443/tcp"],
                "volumes": ["./data:/data", "./letsencrypt:/etc/letsencrypt"],
                "restart": "unless-stopped"}}},
        ],
    },
]

PRESET_INDEX = [
    {"id": p["id"], "name": p["name"], "desc": p["desc"], "count": len(p["apps"])}
    for p in _PRESETS
]


def _now():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return copy.deepcopy(default)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _safe_id(s):
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", str(s or "")).strip("-.")[:64] or "app"


def _dir_name(name, fallback=""):
    """应用目录名：直接用应用显示名（保留中文与空格），只去掉路径非法字符。"""
    s = re.sub(r"[/\\:*?\"<>|\x00-\x1f]", "", str(name or "")).strip(" .")
    return s[:80] or _safe_id(fallback or "app")


def _unique_dir(parent, base):
    """同名应用/残留目录不覆盖：已有本商店清单就拒绝，否则自动加 -2、-3…"""
    p = os.path.join(parent, base)
    if not os.path.exists(p):
        return base
    if os.path.isfile(os.path.join(p, "dockerupdate-app.json")):
        raise ValueError("目录 %s 已存在一个由本商店安装的应用，请先卸载它" % p)
    for i in range(2, 100):
        cand = "%s-%d" % (base, i)
        if not os.path.exists(os.path.join(parent, cand)):
            return cand
    raise ValueError("同名目录过多，请手动清理 %s" % p)


def _norm(p):
    return os.path.normpath(str(p).replace("\\", "/"))


def host_to_local(path, allow_root=True):
    """把用户填的宿主机路径换成容器内的可写路径（拒绝越界）。"""
    p = _norm(path)
    if not p.startswith("/"):
        raise ValueError("安装/下载位置必须是绝对路径，例如 %s/apps" % STORE_HOST_ROOT)
    root = STORE_ROOT
    legacy = "/host" + STORE_HOST_ROOT          # /host/vol1/1000/docker（整机只读挂载视角）
    if p == root or p.startswith(root + "/"):
        local = p
    elif p == legacy or p.startswith(legacy + "/"):
        local = root + p[len(legacy):]
    elif p == STORE_HOST_ROOT or p.startswith(STORE_HOST_ROOT + "/"):
        local = root + p[len(STORE_HOST_ROOT):]
    else:
        raise ValueError("路径 %s 不在可写根 %s 下。要装到别处，请在 compose 里加一条 rw 挂载并重启面板" % (p, STORE_HOST_ROOT))
    local = _norm(local)
    if not (local == root or local.startswith(root + "/")):
        raise ValueError("路径越界：%s" % path)
    if not allow_root and local == root:
        raise ValueError("请指定根目录下的子文件夹，不要直接把整个 %s 当作应用目录" % STORE_HOST_ROOT)
    return local


def local_to_host(path):
    p = _norm(path)
    if p == STORE_ROOT:
        return STORE_HOST_ROOT
    if p.startswith(STORE_ROOT + "/"):
        return STORE_HOST_ROOT + p[len(STORE_ROOT):]
    return p


def _under(child, parent):
    child, parent = _norm(child), _norm(parent)
    return child == parent or child.startswith(parent + "/")


# ---------------------------------------------------------------- 受限 Compose 解析 / 生成
def _scalar(v):
    v = str(v).strip()
    if not v:
        return ""
    if (v[0] == v[-1]) and v[0] in ("'", '"') and len(v) >= 2:
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    if v.startswith("{") and v.endswith("}"):
        out = {}
        for part in v[1:-1].split(","):
            if ":" in part:
                k, val = part.split(":", 1)
                out[_scalar(k)] = _scalar(val)
        return out
    if v.lower() in ("true", "yes", "on"):
        return True
    if v.lower() in ("false", "no", "off"):
        return False
    if v.lower() in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_compose(text):
    """解析 Compose 的常用子集：services / image / ports / volumes / environment / restart /
    command / labels / networks / depends_on / network_mode / privileged / mem_limit。
    不支持 anchors、extends、多行块、复杂变量插值——遇到会给出明确错误而不是静默装错。"""
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        lines.append((indent, line.strip()))
    if not lines:
        raise ValueError("Compose 内容为空")

    tree = {}
    stack = [(-1, tree)]

    def put(container, key, val):
        if isinstance(container, dict):
            container[key] = val
        else:
            container.append(val)

    for idx, (indent, content) in enumerate(lines):
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if content.startswith("- "):
            item = content[2:].strip()
            if not isinstance(parent, list):
                raise ValueError("Compose 解析失败：列表项出现在非列表位置 → %s" % content[:60])
            # 只有「- key: value」这种带空格冒号的才是字典项；
            # 「- 8096:8096/tcp」「- ./config:/config」必须保持为字符串
            if ": " in item and "=" not in item and not item.startswith(("\"", "'")):
                k, v = item.split(": ", 1)
                d = {_scalar(k): _scalar(v.strip())}
                parent.append(d)
                stack.append((indent, d))
            else:
                parent.append(_scalar(item))
            continue
        if ":" not in content:
            raise ValueError("Compose 解析失败：无法识别的行 → %s" % content[:60])
        key, val = content.split(":", 1)
        key, val = _scalar(key), val.strip()
        if val in ("|", ">"):
            raise ValueError("Compose 不支持多行块（%s: |）；请把内容放到应用源的 readme / changelog 字段" % key)
        if val == "":
            # 预判下一行是不是更深的列表项，决定占位容器是 dict 还是 list
            is_list = (idx + 1 < len(lines) and lines[idx + 1][0] > indent
                       and lines[idx + 1][1].startswith("- "))
            child = [] if is_list else {}
            put(parent, key, child)
            stack.append((indent, child))
        else:
            put(parent, key, _scalar(val))
    services = tree.get("services") or {}
    if not isinstance(services, dict) or not services:
        raise ValueError("Compose 里没有有效的 services 段（仅支持 services 子集）")
    out = {}
    for svc, cfg in services.items():
        if not isinstance(cfg, dict):
            raise ValueError("服务 %s 的配置无法解析" % svc)
        if not cfg.get("image"):
            raise ValueError("服务 %s 缺少 image（本商店不支持 build:）" % svc)
        norm = {"image": str(cfg.get("image"))}
        for key in ("container_name", "restart", "command", "network_mode", "privileged",
                    "mem_limit", "cpus", "user", "entrypoint"):
            if key in cfg:
                norm[key] = cfg[key]
        for key in ("ports", "volumes", "environment", "labels", "networks", "depends_on"):
            v = cfg.get(key)
            if v is None:
                continue
            if isinstance(v, list):
                norm[key] = [str(x) for x in v]
            elif isinstance(v, dict):
                if key == "environment":
                    norm[key] = ["%s=%s" % (k, "" if val is None else val) for k, val in v.items()]
                elif key == "labels":
                    norm[key] = ["%s=%s" % (k, "" if val is None else val) for k, val in v.items()]
                else:
                    norm[key] = list(v.keys())
            else:
                norm[key] = [str(v)]
        out[str(svc)] = norm
    return out


def compose_text(app):
    """由 app['services'] 生成写进安装目录的 docker-compose.yml。

    注意：这里**绝不能带时间戳**。指纹（fingerprint）基于本函数的输出计算，
    若每次生成都不同，更新检测会把所有已安装应用永远判成「有更新」。
    安装/更新时间已记录在安装清单与快照里。"""
    lines = ["# 由 DockerUpdate 生成 · 请勿手动修改", "services:"]
    for svc, cfg in (app.get("services") or {}).items():
        lines.append("  %s:" % svc)
        lines.append("    image: %s" % cfg["image"])
        if cfg.get("container_name"):
            lines.append("    container_name: %s" % cfg["container_name"])
        if cfg.get("restart"):
            lines.append("    restart: %s" % cfg["restart"])
        if cfg.get("command"):
            lines.append("    command: %s" % cfg["command"])
        if cfg.get("network_mode"):
            lines.append("    network_mode: %s" % cfg["network_mode"])
        for key in ("ports", "volumes", "environment", "labels", "depends_on"):
            vals = cfg.get(key) or []
            if not vals:
                continue
            lines.append("    %s:" % key)
            for v in vals:
                lines.append("      - %s" % v)
    lines.append("")
    return "\n".join(lines)


def normalize_app(raw, source_id="", source_name=""):
    """把源里的一条应用规范成内部统一结构。

    两类条目：
      compose —— 带 services / compose，可由本面板直接安装；
      fpk     —— 飞牛 / Moo / FnDepot / conversun 原生 FPK 包，常规只能浏览与下载，
                  完整安装需要飞牛应用中心；v2.1-r10 起若包内能读出 Compose / 镜像定义，
                  可「解包直装」成普通 Docker 容器（不注册系统应用、不执行脚本）。
                  这类没有 Compose 时用一份不含时间戳的静态文本生成指纹，保证更新检测稳定。
    """
    a = dict(raw or {})
    a["id"] = _safe_id(a.get("id") or a.get("name"))
    a["name"] = str(a.get("name") or a["id"])
    a["version"] = str(a.get("version") or "1.0.0")
    a["source_id"] = source_id
    a["source_name"] = source_name
    a["icon"] = str(a.get("icon") or "📦")
    a["category"] = str(a.get("category") or "未分类")
    a["summary"] = str(a.get("summary") or "")
    a["readme"] = str(a.get("readme") or a.get("description") or "# %s\n\n暂无说明。\n" % a["name"])
    a["changelog"] = str(a.get("changelog") or "## %s\n- 初始版本\n" % a["version"])
    a["env"] = str(a.get("env") or "")
    a["app_type"] = str(a.get("app_type") or "compose")

    if a["app_type"] == "fpk":
        # 没有任何 Compose 内容，仅作目录条目；不要塞假 services 以免误安装
        a["services"] = {}
        a["_text"] = "\n".join([
            "# FPK 原生安装包（第三方源索引，仅解析元数据）",
            "id: %s" % a["id"],
            "name: %s" % a["name"],
            "version: %s" % a["version"],
            "download: %s" % str(a.get("download_url") or ""),
            "sha256: %s" % str(a.get("sha256") or ""),
        ]) + "\n"
        a["fingerprint"] = hashlib.sha256(a["_text"].encode("utf-8")).hexdigest()[:16]
        return a

    if a.get("compose"):
        a["services"] = parse_compose(a["compose"])
    elif isinstance(a.get("services"), dict) and a["services"]:
        a["services"] = parse_compose(compose_text(a))
    else:
        raise ValueError("应用 %s 缺少 services 或 compose" % a["name"])
    a["_text"] = compose_text(a)
    a["fingerprint"] = hashlib.sha256((a["_text"] + a["env"]).encode("utf-8")).hexdigest()[:16]
    return a


# ---------------------------------------------------------------- Moo / FnDepot V2 / conversun 源协议
def _ver_key(v):
    """把版本号拆成可比较的元组，用于 releases 取最新版。"""
    parts = re.split(r"[.\-+_]", str(v or "0").strip().lstrip("v"))
    out = []
    for p in parts:
        if p.isdigit():
            out.append((1, int(p)))
        else:
            m = re.match(r"(\d+)(.*)", p)
            out.append((1, int(m.group(1))) if m else (0, p.lower()))
    return tuple(out)


def _abs_url(u, base):
    """相对路径按索引文件所在目录补全；绝对 URL 原样返回。"""
    u = str(u or "").strip()
    if not u:
        return ""
    if u.startswith(("http://", "https://", "data:")):
        return u
    return urllib.parse.urljoin(base, u)


def _pick_package(packages, arch_pref=None):
    """从 packages 里挑一个架构包：x86 优先 → all → arm → 任意。"""
    if not isinstance(packages, dict):
        return {}
    pref = list(arch_pref or []) + ["x86", "x86_64", "amd64", "all", "arm", "arm64", "aarch64"]
    for k in pref:
        if k in packages and isinstance(packages[k], dict):
            return packages[k]
    for v in packages.values():
        if isinstance(v, dict) and (v.get("download_url") or v.get("url")):
            return v
    return {}


def parse_moo_index(data, base_url, source_id="", source_name="", arch_pref=None):
    """解析 Moo / FnDepot V1+V2 / conversun fnos-apps 的索引。

    兼容三种顶层形态：
      {"apps": {key: {...}}}       Moo V2 / FnDepot V2（对象表 + releases）
      {"apps": [{...}]}            Moo V1 / conversun apps.json / 本面板原生 Compose
      [...]                        裸数组
    条目里若带 services / compose 则视为可由本面板安装的 Compose 应用；
    否则一律标为 fpk（飞牛原生包，可浏览、可下载，不能在此安装）。
    """
    if isinstance(data, list):
        raw_items = [(str(i), x) for i, x in enumerate(data)]
    elif isinstance(data, dict):
        raw = data.get("apps")
        if isinstance(raw, dict):
            raw_items = list(raw.items())
        elif isinstance(raw, list):
            raw_items = [("", x) for x in raw]
        else:
            raw_items = []
    else:
        raw_items = []
    base = base_url if base_url.endswith("/") else (base_url.rsplit("/", 1)[0] + "/" if "/" in base_url else base_url)
    src_name = source_name
    if isinstance(data, dict) and not src_name:
        src_name = str((data.get("source_info") or {}).get("name") or "")
    out = []
    for key, x in raw_items:
        if not isinstance(x, dict):
            continue
        aid = str(x.get("id") or x.get("appname") or x.get("app_id") or key or "")
        name = str(x.get("display_name") or x.get("name") or x.get("title") or aid or "")
        if not name:
            continue
        releases = x.get("releases") if isinstance(x.get("releases"), dict) else {}
        vers = sorted(releases.keys(), key=_ver_key, reverse=True) if releases else []
        latest = vers[0] if vers else ""
        rel = releases.get(latest, {}) if latest and isinstance(releases.get(latest), dict) else {}
        pkg = _pick_package(rel.get("packages"), arch_pref)
        dl = str(x.get("download_url") or pkg.get("download_url") or pkg.get("url") or "")
        sha = str(x.get("sha256") or pkg.get("sha256") or "")
        size = x.get("size") or pkg.get("size") or ""
        version = str(x.get("version") or latest or "1.0.0")
        cats = x.get("categories") or x.get("category") or []
        if isinstance(cats, str):
            cats = [cats]
        cat = str(cats[0]) if cats else "未分类"
        desc = str(x.get("desc") or x.get("description") or x.get("summary") or "")
        author = str(x.get("author") or x.get("maintainer") or x.get("distributor") or "")
        homepage = str(x.get("homepage") or x.get("homepage_url") or "")
        port = x.get("service_port") or x.get("port") or ""
        platform = x.get("platform") or []
        if isinstance(platform, str):
            platform = [platform]
        readme_url = _abs_url(x.get("readme_url") or x.get("readme"), base)
        icon_url = _abs_url(x.get("icon_url") or x.get("icon"), base)
        dl = _abs_url(dl, base) if dl and not dl.startswith(("http://", "https://")) else dl

        # 更新日志：把各版本 changelog 拼起来
        cl = []
        for v in vers[:12]:
            r = releases.get(v, {}) if isinstance(releases.get(v), dict) else {}
            c = str(r.get("changelog") or x.get("changelog") or "").strip()
            cl.append("## %s\n%s" % (v, c or "- 未填写更新说明"))
        changelog = "\n\n".join(cl) if cl else "## %s\n- 初始版本\n" % version

        # README：源里给了远程地址就放链接，正文用元信息生成，避免同步时逐个拉文件
        lines = ["# %s" % name, "", desc or "暂无说明。", "",
                 "- **版本**：`%s`" % version,
                 "- **分类**：%s" % cat,
                 "- **安装包**：FPK（飞牛原生格式）",
                 "- **适用架构**：%s" % ("、".join(map(str, platform)) if platform else "未标注"),
                 "- **服务端口**：%s" % (port or "未标注")]
        if author:
            lines.append("- **维护者 / 发布者**：%s" % author)
        if homepage:
            lines.append("- **主页**：%s" % homepage)
        if sha:
            lines.append("- **SHA256**：`%s`" % sha)
        if dl:
            lines += ["", "在下方「安装」标签点「下载 FPK」把安装包保存到下载目录，"
                      "然后到飞牛 **应用中心 → 手动安装** 选择该文件。",
                      "", "原始下载地址：`%s`" % dl]
        if readme_url:
            lines += ["", "上游 README：`%s`" % readme_url]
        lines += ["", "> 本面板只索引 FPK 目录，不执行包内脚本、不代替飞牛安装器。"]
        readme = "\n".join(lines) + "\n"

        item = {
            "id": aid, "name": name, "version": version,
            "icon": str(x.get("icon") or "📦"), "category": cat,
            "summary": desc[:160], "readme": readme, "changelog": changelog,
            "env": "", "homepage": homepage, "author": author,
            "app_type": "fpk", "download_url": dl, "sha256": sha, "size": size,
            "service_port": str(port or ""), "readme_url": readme_url,
            "icon_url": icon_url, "platform": platform,
            "source_url": base_url, "fpk_filename": os.path.basename(urllib.parse.urlparse(dl).path) if dl else "",
        }
        # 混合源：条目自带 Compose 就按可直接安装的应用处理
        if x.get("services") or x.get("compose"):
            item["app_type"] = "compose"
            item["services"] = x.get("services")
            item["compose"] = x.get("compose")
        out.append(normalize_app(item, source_id, src_name))
    return out


# 索引文件名按 Moo 的优先级：moo.json → fnpack.json → fndepot.json → fndepot_v2.json → apps.json
_INDEX_FILES = ("moo.json", "fnpack.json", "fndepot.json", "fndepot_v2.json", "apps.json")


def _gh_expand(url):
    """把一个源地址展开成候选索引 URL：GitHub 仓库根自动发现索引文件，
    raw.githubusercontent.com 失败自动回退 jsdelivr CDN（国内直连 GitHub 常超时）。"""
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        return [u] if u else []
    p = urllib.parse.urlparse(u)
    host, path = p.netloc.lower(), p.path.strip("/")
    cands = []

    def add(*xs):
        for x in xs:
            if x and x not in cands:
                cands.append(x)

    parts = [x for x in path.split("/") if x]
    if host in ("github.com", "www.github.com") and len(parts) >= 2:
        owner, repo = parts[0], parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        if len(parts) >= 5 and parts[2] in ("blob", "raw", "tree"):
            br, f = parts[3], "/".join(parts[4:]) or "moo.json"
            add("https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, br, f),
                "https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (owner, repo, br, f))
        else:
            for br in ("main", "master"):
                for n in _INDEX_FILES:
                    add("https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, br, n))
                for n in _INDEX_FILES:
                    add("https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (owner, repo, br, n))
        return cands
    if host == "raw.githubusercontent.com" and len(parts) >= 3:
        owner, repo, rest = parts[0], parts[1], parts[2:]
        if len(rest) >= 4 and rest[0] == "refs" and rest[1] == "heads":
            br, f = rest[2], "/".join(rest[3:])
        else:
            br, f = rest[0], "/".join(rest[1:])
        add(u, "https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (owner, repo, br, f))
        return cands
    add(u)
    return cands


_INDEX_FETCH_BUDGET = 10.0   # 单个源索引拉取的总时间预算（秒）
_INDEX_REQ_TIMEOUT = 5.0     # 单个候选地址的连接/读取超时


def fetch_source_index(url, resolved="", budget=0):
    """按顺序尝试候选地址，返回 (data, 实际生效的 URL)。

    v2.1-r10：加总时间预算。旧实现每个候选 12 秒、GitHub 仓库根会展开 20 个
    候选地址，国内网络下一个源最坏能卡 4 分钟；96 个源串行时「检查更新」
    就变成截图里 57 秒还在转圈。现在单个源最多占用 budget 秒，超时保留旧缓存。
    """
    budget = float(budget) if budget else _INDEX_FETCH_BUDGET
    cands = ([resolved] if resolved else []) + _gh_expand(url)
    seen, uniq = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    deadline = time.monotonic() + budget
    last, tried = "", 0
    for c in uniq:
        if time.monotonic() >= deadline:
            last = "超过 %.0f 秒预算，已停止尝试（保留上次缓存）" % budget
            break
        rem = max(1.0, min(_INDEX_REQ_TIMEOUT, deadline - time.monotonic()))
        tried += 1
        try:
            req = urllib.request.Request(c, headers={"Accept": "application/json, text/plain, */*",
                                                     "User-Agent": "DockerUpdate/2.1"})
            with urllib.request.urlopen(req, timeout=rem) as r:
                data = json.loads(r.read(8 * 1024 * 1024).decode("utf-8", "replace") or "{}")
            apps = data.get("apps") if isinstance(data, dict) else data
            if isinstance(apps, (list, dict)) and apps:
                return data, c
            last = "%s：索引里没有应用" % c
        except Exception as exc:
            last = "%s：%s" % (c, exc)
    raise ValueError("无法获取应用源索引（%d/%d 个地址，含 CDN 加速回退，预算 %.0f 秒）：%s"
                     % (tried, len(uniq), budget, last[:200]))


# ---------------------------------------------------------------- GitHub / Docker 加速源
# 名称与地址逐条核对自 Blue-Mink/moo 的 internal/config/mirrors.go（GitHubMirrorOptions），
# 与 Moo 设置页「加速源设置」里的 18 条完全一致；初始延迟 / 速度取自用户提供的
# Moo 测速截图，标记 preset=True，点「同步」后由本机真实测速覆盖。
ACCEL_FILE = os.path.join(STORE_DIR, "accel.json")

# 探针：Moo 自有小文件，公开只读，避免拿别人的 release 资产刷下载量
ACCEL_PROBE_URL = "https://raw.githubusercontent.com/Blue-Mink/moo/main/moo.json"

_DEFAULT_GH_MIRRORS = [
    ("gh-proxy-org", "GH-Proxy.ORG", "https://gh-proxy.org/", 282, 5.8 * 1024 * 1024, "ok", "", 0),
    ("gh-proxy", "GH-Proxy", "https://gh-proxy.com/", 343, 1.3 * 1024 * 1024, "ok", "", 0),
    ("gh-dpik", "GH DPIK", "https://gh.dpik.top/", 490, 784 * 1024, "ok", "", 0),
    ("gh-proxy-hk", "GH-Proxy HK", "https://hk.gh-proxy.org/", 0, 40 * 1024, "fail", "慢（10 秒读 403KB ≈ 40KB/s < 153KB/s 门槛）", 2),
    ("ghproxy-cxkpro", "GHProxy CXK", "https://ghproxy.cxkpro.top/", 0, 0, "limited", "429 限流（临时）", 0),
    ("felicity", "GH Felicity", "https://gh.felicity.ac.cn/", 0, 0, "limited", "429 限流（临时）", 0),
    ("memory-echoes", "GHProxy ME", "https://github-proxy.memory-echoes.cn/", 0, 0, "limited", "429 限流（临时）", 0),
    ("dpik", "GitHub DPIK", "https://github.dpik.top/", 0, 0, "limited", "429 限流（临时）", 0),
    ("gitproxy-mrhjx", "GitProxy MRHJX", "https://gitproxy.mrhjx.cn/", 0, 0, "limited", "429 限流（临时）", 0),
    ("gh-ddlc", "GH DDLC", "https://gh.ddlc.top/", 0, 0, "limited", "429 限流（临时）", 0),
    ("cdn-ghproxy", "CDN GHProxy", "https://cdn.gh-proxy.org/", 0, 21 * 1024, "fail", "慢（10 秒读 217KB ≈ 21KB/s < 门槛）", 2),
    ("gh-tbap", "GH TBAP", "https://github.tbap.top/", 0, 0, "fail", "超时", 4),
    ("ghproxy-net", "GHProxy.net", "https://ghproxy.net/", 0, 12 * 1024, "fail", "慢（10 秒读 127KB ≈ 12KB/s < 门槛）", 2),
    ("cors-isteed", "Cors Proxy", "https://cors.isteed.cc/", 0, 0, "fail", "超时", 64),
    ("conversun", "Conversun Hub", "https://hub.conversun.com/", 0, 0, "fail", "HTTP 503（仅代理 conversun 仓库）", 64),
    ("yylx", "YYLX Git", "https://git.yylx.win/", 0, 13 * 1024, "fail", "慢（10 秒读 134KB ≈ 13KB/s < 门槛）", 5),
    ("wget-la", "WGET.LA", "https://wget.la/", 0, 2 * 1024, "fail", "慢（10 秒读 27KB ≈ 2KB/s < 门槛）", 64),
    ("ghfast", "GHFast", "https://ghfast.top/", 0, 0, "fail", "超时", 64),
    # v3.9-r7：由公开加速站清单补充的候选线路。初始状态留空（未测），
    # 不伪造延迟与速度；点「立即测速」后由 NAS 本机真实探测覆盖。
    ("gh-llkk", "GH LLKK", "https://gh.llkk.cc/", 0, 0, "", "", 0),
    ("dockerproxy-link", "DockerProxy.Link", "https://dockerproxy.link/", 0, 0, "", "", 0),
    ("mirror-houlang", "厚浪镜像", "https://mirror.houlang.cloud/", 0, 0, "", "", 0),
    ("github-akams", "GitHub Akams", "https://github.akams.cn/", 0, 0, "", "", 0),
    ("hub-gitmirror", "GitMirror Hub", "https://hub.gitmirror.com/", 0, 0, "", "", 0),
    ("moeyy-ghproxy", "Moeyy GHProxy", "https://moeyy.cn/gh-proxy/", 0, 0, "", "", 0),
    ("ghp-ci", "GHP.CI", "https://ghp.ci/", 0, 0, "", "", 0),
    ("ghproxy-homeboyc", "GHProxy HomeboyC", "https://ghproxy.homeboyc.cn/", 0, 0, "", "", 0),
    ("toolwa-github", "Toolwa GitHub", "https://toolwa.com/github/", 0, 0, "", "", 0),
    ("kkgithub", "KKGitHub", "https://kkgithub.com/", 0, 0, "", "", 0),
    ("gitclone-com", "GitClone.COM", "https://gitclone.com/", 0, 0, "", "", 0),
    ("bgithub-xyz", "BGitHub", "https://bgithub.xyz/", 0, 0, "", "", 0),
    ("jsdelivr-gh", "jsDelivr CDN", "https://cdn.jsdelivr.net/", 0, 0, "", "", 0),
    ("tuna-github-release", "清华 TUNA Release", "https://mirrors.tuna.tsinghua.edu.cn/github-release/", 0, 0, "", "", 0),
    ("gitee-mirrors", "Gitee 镜像库", "https://gitee.com/organizations/mirrors/", 0, 0, "", "", 0),
]

# Docker 镜像加速：同样取自 Moo 内置清单，本面板只展示与复制，不擅自改 NAS 的 daemon.json
_DEFAULT_DK_MIRRORS = [
    ("daocloud", "DaoCloud", "https://m.daocloud.io/"),
    ("daocloud-docker", "DaoCloud Docker", "https://docker.m.daocloud.io/"),
    ("nju-ghcr", "NJU ghcr", "https://ghcr.nju.edu.cn/"),
    ("docker-1ms", "1ms.run", "https://docker.1ms.run/"),
    ("ratdev", "Rat.Dev", "https://hub.rat.dev/"),
    ("1panel", "1Panel", "https://docker.1panel.live/"),
    ("dockerproxy", "DockerProxy", "https://dockerproxy.net/"),
    ("kspeeder", "KSpeeder (本地)", "http://127.0.0.1:5443/"),
    # v3.9-r7：更多公开 registry mirror 候选。仅作「候选池」展示与复制，
    # 面板不会擅自写入 NAS 的 /etc/docker/daemon.json。
    ("xuanyuan-me", "轩辕镜像 免费版", "https://docker.xuanyuan.me/"),
    ("xuanyuan-run", "轩辕镜像 专业版", "https://docker.xuanyuan.run/"),
    ("xuanyuan-dev", "轩辕镜像 专业版2", "https://docker.xuanyuan.dev/"),
    ("1panel-dev", "1Panel 核心用户", "https://docker.1panel.dev/"),
    ("1panel-top", "1Panel.TOP", "https://docker.1panel.top/"),
    ("proxy-1panel", "Proxy 1Panel", "https://proxy.1panel.live/"),
    ("hub-1panel-dev", "Hub 1Panel.DEV", "https://hub.1panel.dev/"),
    ("ketches", "Ketches", "https://docker.ketches.cn/"),
    ("dockerhub-icu", "DockerHub.ICU", "https://dockerhub.icu/"),
    ("netease-163", "网易数帆", "https://hub-mirror.c.163.com/"),
    ("baidu-bce", "百度智能云", "https://mirror.baidubce.com/"),
    ("tencent-inner", "腾讯云 内网", "https://mirror.ccs.tencentyun.com/"),
    ("tencent-ccr", "腾讯云 CCR", "https://ccr.ccs.tencentyun.com/"),
    ("nju-docker", "南京大学", "https://docker.nju.edu.cn/"),
    ("sjtug-docker", "上海交大 SJTUG", "https://docker.mirrors.sjtug.sjtu.edu.cn/"),
    ("ustc-docker", "中科大 USTC", "https://docker.mirrors.ustc.edu.cn/"),
    ("ali-hangzhou", "阿里云 杭州", "https://registry.cn-hangzhou.aliyuncs.com/"),
    ("ali-beijing", "阿里云 北京", "https://registry.cn-beijing.aliyuncs.com/"),
    ("ali-shanghai", "阿里云 上海", "https://registry.cn-shanghai.aliyuncs.com/"),
    ("ali-shenzhen", "阿里云 深圳", "https://registry.cn-shenzhen.aliyuncs.com/"),
    ("ali-qingdao", "阿里云 青岛", "https://registry.cn-qingdao.aliyuncs.com/"),
    ("ali-zhangjiakou", "阿里云 张家口", "https://registry.cn-zhangjiakou.aliyuncs.com/"),
    ("fnnas", "飞牛 fnOS 官方", "https://docker.fnnas.com/"),
    ("ixdev", "CNIX 前海交换中心", "https://docker.m.ixdev.cn/"),
    ("vvvv-ee", "mxjia Proxy", "https://proxy.vvvv.ee/"),
    ("kejilion", "科技 Lion", "https://docker.kejilion.pro/"),
    ("nmqu", "NMQU Registry", "https://docker-registry.nmqu.com/"),
    ("nat-tf-1", "NAT.TF 节点1", "https://hub1.nat.tf/"),
    ("nat-tf-2", "NAT.TF 节点2", "https://hub2.nat.tf/"),
    ("nat-tf-3", "NAT.TF 节点3", "https://hub3.nat.tf/"),
    ("nat-tf-4", "NAT.TF 节点4", "https://hub4.nat.tf/"),
    ("iscas", "中科院软件所", "https://mirror.iscas.ac.cn/"),
    ("unsee-tech", "Unsee.Tech", "https://docker-0.unsee.tech/"),
    ("apiba", "APIBA", "https://docker.apiba.cn/"),
    ("hpcloud", "HPCloud", "https://docker.hpcloud.cloud/"),
    ("atomhub", "AtomHub 开放原子", "https://atomhub.openatom.cn/"),
    ("anyhub", "AnyHub", "https://docker.anyhub.us.kg/"),
    ("jobcher", "Jobcher", "https://dockerhub.jobcher.com/"),
    ("doublezonline", "DoubleZOnline", "https://doublezonline.cloud/"),
    ("mrxn", "MRXN", "https://docker.mrxn.net/"),
    ("dislabaiot", "DislabAIoT", "https://dislabaiot.xyz/"),
    ("fxxk-dedyn", "FXXK deDyn", "https://docker.fxxk.dedyn.io/"),
    ("aigc2d", "AIGC2D", "https://docker-mirror.aigc2d.com/"),
    ("aityp", "AITyp", "https://docker.aityp.com/"),
    ("xisoul", "XiSoul", "https://dockerhub.xisoul.cn/"),
    ("littlediary", "LittleDiary", "https://hub.littlediary.cn/"),
    ("actima", "Actima", "https://docker.actima.top/"),
    ("sparkcr", "SparkCR", "https://docker.sparkcr.cn/"),
    ("dockerproxy-cool", "DockerProxy.Cool", "https://dockerproxy.cool/"),
    ("anye-in", "Anye", "https://docker.anye.in/"),
    ("amingg", "爱铭网络", "https://docker.amingg.com/"),
    ("gxl-367231", "GXL 1Panel", "https://docker.367231.xyz/"),
    ("geekery", "Geekery", "https://hub.geekery.cn/"),
    ("shaunyang", "ShaunYang", "https://docker.shaunyang.site/"),
]


def _default_accel():
    return {
        "enabled": True,
        "mode": "auto",                 # auto = 智能·自动选最快；manual = 固定某个源
        "active": "gh-proxy-org",      # 当前线路（auto 模式下由测速结果自动更新）
        "include_direct": False,       # 智能选源是否把「直连」也纳入候选
        "interval_min": 5,             # 自动监测间隔（分钟），对齐 Moo 的 0h5m
        "auto_monitor": True,
        "last_check": "",
        "last_switch": "",
        "gh": [{"key": k, "label": n, "url": u, "latency_ms": lm, "speed_bps": int(sp),
                "status": stt, "error": err, "consec_fails": fails,
                "preset": True, "builtin": True, "custom": False, "last_check": ""}
               for k, n, u, lm, sp, stt, err, fails in _DEFAULT_GH_MIRRORS],
        "docker": [{"key": k, "label": n, "url": u, "builtin": True}
                   for k, n, u in _DEFAULT_DK_MIRRORS],
        "repo": [{"key": k, "label": n, "url": u, "note": note, "builtin": True}
                 for k, n, u, note in _DEFAULT_REPO_PREFIXES],
    }


# v3.9-r7：registry-mirrors 只对 Docker Hub 生效。GHCR / Quay / gcr / K8s / MCR 等
# 必须「改写镜像地址前缀」才能加速，不能塞进 daemon.json。这里只做展示与复制。
_DEFAULT_REPO_PREFIXES = [
    ("daocloud-universal", "DaoCloud 通用前缀", "m.daocloud.io/", "在原镜像名前加此前缀"),
    ("daocloud-ghcr", "DaoCloud GHCR", "ghcr.m.daocloud.io/", "替换 ghcr.io/"),
    ("daocloud-quay", "DaoCloud Quay", "quay.m.daocloud.io/", "替换 quay.io/"),
    ("daocloud-gcr", "DaoCloud GCR", "gcr.m.daocloud.io/", "替换 gcr.io/"),
    ("daocloud-k8s", "DaoCloud K8s", "k8s.m.daocloud.io/", "替换 registry.k8s.io/"),
    ("daocloud-k8s-old", "DaoCloud K8s 旧域", "k8s-gcr.m.daocloud.io/", "替换 k8s.gcr.io/"),
    ("daocloud-mcr", "DaoCloud MCR", "mcr.m.daocloud.io/", "替换 mcr.microsoft.com/"),
    ("daocloud-nvcr", "DaoCloud NVCR", "nvcr.m.daocloud.io/", "替换 nvcr.io/"),
    ("daocloud-elastic", "DaoCloud Elastic", "elastic.m.daocloud.io/", "替换 docker.elastic.co/"),
    ("daocloud-ollama", "DaoCloud Ollama", "ollama.m.daocloud.io/", "替换 registry.ollama.ai/"),
    ("daocloud-docker", "DaoCloud DockerHub", "docker.m.daocloud.io/", "替换 docker.io/library/"),
    ("1ms-ghcr", "1ms GHCR", "ghcr.1ms.run/", "替换 ghcr.io/"),
    ("nju-ghcr", "南大 GHCR", "ghcr.nju.edu.cn/", "替换 ghcr.io/"),
    ("nju-quay", "南大 Quay", "quay.nju.edu.cn/", "替换 quay.io/"),
    ("chenby-ghcr", "Chenby GHCR", "ghcr.chenby.cn/", "替换 ghcr.io/"),
    ("chenby-quay", "Chenby Quay", "quay.chenby.cn/", "替换 quay.io/"),
    ("chenby-gcr", "Chenby GCR", "gcr.chenby.cn/", "替换 gcr.io/"),
    ("chenby-k8s", "Chenby K8s", "k8s.chenby.cn/", "替换 registry.k8s.io/"),
    ("cyou-ghcr", "registry.cyou GHCR", "ghcr.registry.cyou/", "替换 ghcr.io/"),
    ("cyou-quay", "registry.cyou Quay", "quay.registry.cyou/", "替换 quay.io/"),
    ("cyou-k8s", "registry.cyou K8s", "k8s.registry.cyou/", "替换 registry.k8s.io/"),
    ("cyou-nvcr", "registry.cyou NVCR", "nvcr.registry.cyou/", "替换 nvcr.io/"),
]

_GH_HOSTS = ("raw.githubusercontent.com", "github.com", "codeload.github.com",
             "objects.githubusercontent.com", "github.githubassets.com",
             "opengraph.githubassets.com", "gist.githubusercontent.com")


def _is_github_url(u):
    try:
        return urllib.parse.urlparse(str(u or "")).netloc.lower() in _GH_HOSTS
    except Exception:
        return False


class Accel:
    """GitHub 下载加速：多源优选、真实测速、自动切换、供所有远程请求复用。

    安全边界：只代理公开的 GitHub 静态资源（前缀代理形式），不携带任何
    Cookie / Authorization；HTTPS 源地址白名单由用户自行添加，只允许 http(s)。
    """
    PROBE_TIMEOUT = 3.0
    PROBE_BYTES = 256 * 1024

    def __init__(self):
        self.lock = threading.RLock()
        self._monitor_started = False

    def cfg(self):
        with self.lock:
            c = _read(ACCEL_FILE, _default_accel())
            if not isinstance(c, dict):
                c = _default_accel()
            base = _default_accel()
            for k in ("enabled", "mode", "active", "include_direct", "interval_min",
                      "auto_monitor", "last_check", "last_switch"):
                if k in c:
                    base[k] = c[k]
            builtin_urls = set(x[2] for x in _DEFAULT_GH_MIRRORS)
            dk_urls = set(x[2] for x in _DEFAULT_DK_MIRRORS)
            if isinstance(c.get("gh"), list):
                rows = [x for x in c["gh"] if isinstance(x, dict) and x.get("url")]
                for x in rows:
                    # r8 之前测速会把 preset 清成 False，导致内置源被标成「自定义」
                    if x.get("url") in builtin_urls:
                        x["builtin"] = True
                        x["preset"] = True
                        x["custom"] = False
                base["gh"] = rows
            if isinstance(c.get("docker"), list):
                rows = [x for x in c["docker"] if isinstance(x, dict) and x.get("url")]
                for x in rows:
                    if x.get("url") in dk_urls:
                        x["builtin"] = True
                base["docker"] = rows
            if isinstance(c.get("repo"), list):
                base["repo"] = [x for x in c["repo"] if isinstance(x, dict) and x.get("url")]
            return base

    def _write(self, c):
        _write(ACCEL_FILE, c)

    def state(self):
        c = self.cfg()
        good = [x for x in c["gh"] if x.get("status") == "ok"]
        limited = [x for x in c["gh"] if x.get("status") == "limited"]
        cur = next((x for x in c["gh"] if x.get("key") == c.get("active")), None)
        return {"ok": True, "config": c,
                "summary": {"enabled": bool(c["enabled"]), "mode": c["mode"],
                            "active": c["active"], "active_label": cur["label"] if cur else "",
                            "ok": len(good), "limited": len(limited),
                            "total": len(c["gh"]),
                            "docker_total": len(c.get("docker") or []),
                            "repo_total": len(c.get("repo") or []),
                            "candidate_total": len(c["gh"]) + len(c.get("docker") or [])
                                               + len(c.get("repo") or []),
                            "last_check": c.get("last_check", ""),
                            "probe_url": ACCEL_PROBE_URL}}

    def save(self, patch):
        c = self.cfg()
        for k in ("enabled", "mode", "active", "include_direct", "interval_min", "auto_monitor"):
            if k in patch:
                c[k] = patch[k]
        try:
            c["interval_min"] = max(1, min(1440, int(c["interval_min"] or 5)))
        except Exception:
            c["interval_min"] = 5
        if c["mode"] not in ("auto", "manual", "direct"):
            c["mode"] = "auto"
        self._write(c)
        return self.state()

    def add(self, name, url):
        name = str(name or "").strip()[:40]
        url = str(url or "").strip().rstrip("/") + "/"
        if not name:
            raise ValueError("请填写加速源名称")
        if not url.startswith(("http://", "https://")):
            raise ValueError("加速源地址必须是 http:// 或 https:// 开头")
        c = self.cfg()
        if any(x["url"] == url for x in c["gh"]):
            raise ValueError("这个加速源地址已经存在")
        key = "custom-" + hashlib.sha1(url.encode()).hexdigest()[:10]
        c["gh"].append({"key": key, "label": name, "url": url, "latency_ms": 0,
                        "speed_bps": 0, "status": "", "error": "", "consec_fails": 0,
                        "preset": False, "custom": True, "last_check": ""})
        self._write(c)
        return {"ok": True, "message": "已添加加速源「%s」" % name, "state": self.state()}

    def add_batch(self, text, target="gh"):
        """v3.9-r7：一行一个批量添加，自动按换行区分。

        支持：
          名称|https://前缀/
          名称 https://前缀/
          https://前缀/                （名称自动取域名）
          # 注释 / 空行               （忽略）
        自动补协议、归一尾斜杠、与现有列表及本批内去重；逐行返回成功/跳过/失败。
        """
        raw = str(text or "")
        lines = [x.strip() for x in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        c = self.cfg()
        if target not in ("gh", "docker", "repo"):
            target = "gh"
        rows = c[target]
        existing = set(str(x.get("url") or "").rstrip("/") + "/" for x in rows)
        detail, added = [], 0
        for idx, line in enumerate(lines, 1):
            if not line or line.startswith("#"):
                continue
            name, url = "", ""
            if "|" in line:
                name, url = line.split("|", 1)
                name, url = name.strip(), url.strip()
            else:
                # 「名称 地址」：最后一个空白分隔段看起来是 URL / 域名时才拆
                parts = line.rsplit(None, 1)
                tail = parts[-1].strip() if parts else ""
                if len(parts) == 2 and (tail.lower().startswith(("http://", "https://"))
                                        or ("." in tail and " " not in tail)):
                    name, url = parts[0].strip(), tail
                else:
                    url = line
            # 省略协议的裸域名自动补 https
            if url and not url.lower().startswith(("http://", "https://")) \
                    and "." in url and " " not in url:
                url = "https://" + url.lstrip("/")
            if not url.lower().startswith(("http://", "https://")):
                detail.append({"line": idx, "status": "failed", "text": line,
                               "message": "不是有效的 http(s) 地址"})
                continue
            url = url.rstrip("/") + "/"
            host = ""
            try:
                host = urllib.parse.urlparse(url).netloc
            except Exception:
                pass
            if not host:
                detail.append({"line": idx, "status": "failed", "text": line,
                               "message": "地址缺少域名"})
                continue
            if url in existing:
                detail.append({"line": idx, "status": "skipped", "text": line,
                               "message": "已存在，跳过"})
                continue
            label = (name or host)[:40]
            key = "custom-" + target + "-" + hashlib.sha1(url.encode()).hexdigest()[:10]
            if target == "gh":
                rows.append({"key": key, "label": label, "url": url, "latency_ms": 0,
                             "speed_bps": 0, "status": "", "error": "", "consec_fails": 0,
                             "preset": False, "builtin": False, "custom": True,
                             "last_check": ""})
            else:
                rows.append({"key": key, "label": label, "url": url,
                             "note": "自定义添加", "builtin": False, "custom": True})
            existing.add(url)
            added += 1
            detail.append({"line": idx, "status": "added", "text": line,
                           "message": "已添加「%s」" % label})
        if added:
            self._write(c)
        ok = sum(1 for x in detail if x["status"] == "added")
        skipped = sum(1 for x in detail if x["status"] == "skipped")
        failed = sum(1 for x in detail if x["status"] == "failed")
        return {"ok": True, "added": ok, "skipped": skipped, "failed": failed,
                "lines": detail, "total_lines": len(lines),
                "message": "批量导入完成：新增 %d · 跳过 %d · 失败 %d" % (ok, skipped, failed),
                "state": self.state()}

    def delete(self, key):
        c = self.cfg()
        hit, belong = None, "gh"
        for group in ("gh", "docker", "repo"):
            hit = next((x for x in (c.get(group) or []) if x.get("key") == key), None)
            if hit:
                belong = group
                break
        if not hit:
            raise ValueError("找不到这个加速源")
        c[belong] = [x for x in c[belong] if x.get("key") != key]
        if c["active"] == key:
            c["active"] = "auto"
            c["mode"] = "auto"
        self._write(c)
        return {"ok": True, "message": "已删除加速源「%s」" % hit["label"], "state": self.state()}

    def restore(self):
        c = self.cfg()
        d = _default_accel()
        # 只补回被删掉的内置项，用户自己批量添加的自定义源不丢
        for group in ("gh", "docker", "repo"):
            custom = [x for x in (c.get(group) or []) if isinstance(x, dict) and x.get("custom")]
            have = set(str(x.get("url") or "") for x in custom)
            merged = list(d[group]) + [x for x in custom
                                       if str(x.get("url") or "") not in
                                       set(str(y.get("url") or "") for y in d[group])]
            c[group] = merged
        c["active"] = d["active"]
        c["mode"] = "auto"
        c["enabled"] = True
        self._write(c)
        return {"ok": True, "message": "已恢复 %d 条 GitHub 加速线路、%d 个 Docker 镜像源与 %d 条非 Hub 前缀"
                                        % (len(d["gh"]), len(d["docker"]), len(d["repo"])),
                "state": self.state()}

    # ---------- 真实测速 ----------
    def _probe_one(self, item):
        prefix = str(item.get("url") or "").rstrip("/") + "/"
        target = prefix + ACCEL_PROBE_URL
        out = dict(item)
        t0 = time.time()
        try:
            req = urllib.request.Request(target, headers={
                "User-Agent": "DockerUpdate/2.1", "Accept": "*/*",
                "Range": "bytes=0-%d" % (self.PROBE_BYTES - 1)})
            with urllib.request.urlopen(req, timeout=self.PROBE_TIMEOUT) as r:
                code = r.status
                first = r.read(64 * 1024)
                latency = int((time.time() - t0) * 1000)
                body = first
                t1 = time.time()
                while len(body) < self.PROBE_BYTES and (time.time() - t1) < 2.0:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    body += chunk
                elapsed = max(0.001, time.time() - t1)
                speed = int(len(body) / elapsed)
            if code == 429 or code == 403:
                out.update({"status": "limited" if code == 429 else "fail",
                            "error": "429 限流（临时）" if code == 429 else "403 拒绝",
                            "latency_ms": latency, "speed_bps": speed,
                            "consec_fails": 0, "last_check": _now()})
            elif code >= 400:
                out.update({"status": "fail", "error": "HTTP %d" % code,
                            "latency_ms": latency, "consec_fails": int(item.get("consec_fails") or 0) + 1,
                            "last_check": _now()})
            elif speed < 153 * 1024 and len(body) >= self.PROBE_BYTES:
                out.update({"status": "fail", "error": "慢（%.0fKB/s < 153KB/s 门槛）" % (speed / 1024),
                            "latency_ms": latency, "speed_bps": speed,
                            "consec_fails": int(item.get("consec_fails") or 0) + 1,
                            "last_check": _now()})
            else:
                out.update({"status": "ok", "error": "", "latency_ms": latency,
                            "speed_bps": speed, "consec_fails": 0, "last_check": _now()})
        except Exception as exc:
            msg = str(exc)
            if "timed out" in msg or "timeout" in msg.lower():
                err = "超时"
            elif "429" in msg:
                err = "429 限流（临时）"
            else:
                err = msg[:60]
            out.update({"status": "limited" if "429" in msg else "fail", "error": err,
                        "consec_fails": int(item.get("consec_fails") or 0) + 1,
                        "last_check": _now()})
        # r7：保留内置 / 自定义标记，避免测速后内置源在界面上变成「自定义」
        out["preset"] = bool(item.get("preset")) or bool(item.get("builtin"))
        out["builtin"] = bool(item.get("builtin")) or bool(item.get("preset"))
        out["custom"] = not out["builtin"]
        return out

    def check(self, keys=None):
        """并发测速全部（或指定）加速源，并按吞吐自动选最快。"""
        from concurrent.futures import ThreadPoolExecutor
        c = self.cfg()
        want = set(keys or [])
        targets = [x for x in c["gh"] if not want or x["key"] in want]
        if not targets:
            return {"ok": True, "message": "没有需要测速的加速源", "state": self.state()}

        def one(x):
            return self._probe_one(x)

        # v5.0：117 个候选源全测时 8 并发太慢，提到 24
        with ThreadPoolExecutor(max_workers=24) as ex:
            results = list(ex.map(one, targets))
        by_key = {r["key"]: r for r in results}
        c["gh"] = [by_key.get(x["key"], x) for x in c["gh"]]
        prev = c.get("active", "")
        if c["mode"] == "auto":
            good = sorted([x for x in c["gh"] if x.get("status") == "ok"],
                          key=lambda x: -(x.get("speed_bps") or 0))
            if good:
                c["active"] = good[0]["key"]
                if prev != c["active"]:
                    c["last_switch"] = "%s → %s @ %s" % (prev, c["active"], _now())
                    try:
                        import events
                        events.emit("accel_switch", "加速线路切换",
                                    "GitHub 加速线路已由「%s」自动切换为「%s」（按测速结果选最快）。"
                                    % (prev or "直连", c["active"]),
                                    "info", "加速源")
                    except Exception:
                        pass
        c["last_check"] = _now()
        self._write(c)
        st = self.state()
        return {"ok": True,
                "message": "测速完成：%d/%d 正常，当前线路 %s" %
                           (st["summary"]["ok"], st["summary"]["total"], st["summary"]["active_label"]),
                "state": st}

    def best_keys(self):
        c = self.cfg()
        if not c.get("enabled"):
            return []
        if c["mode"] == "direct":
            return []
        if c["mode"] == "manual":
            hit = next((x for x in c["gh"] if x["key"] == c["active"]), None)
            return [hit["url"]] if hit else []
        good = sorted([x for x in c["gh"] if x.get("status") == "ok"],
                      key=lambda x: -(x.get("speed_bps") or 0))
        # 没有实测数据时，用内置静态排序（Moo 的声明顺序）兜底
        if not good:
            good = [x for x in c["gh"] if x.get("status") == "ok"][:4]
        urls = [x["url"] for x in good[:4]]
        cur = next((x for x in c["gh"] if x["key"] == c.get("active")), None)
        if cur and cur["url"] in urls:
            urls.remove(cur["url"])
            urls.insert(0, cur["url"])
        return urls

    def candidates(self, url):
        """为任意 GitHub 资源生成候选地址：最快加速线路 → 备选 → 直连 → jsdelivr。"""
        u = str(url or "").strip()
        if not u.startswith(("http://", "https://")):
            return []
        out = []
        if _is_github_url(u):
            c = self.cfg()
            if c.get("include_direct"):
                out.append(u)
            for prefix in self.best_keys():
                cand = prefix.rstrip("/") + "/" + u
                if cand not in out:
                    out.append(cand)
        if u not in out:
            out.append(u)
        # jsdelivr 对 raw 文件是真正的 CDN 回退，与前缀代理不同
        p = urllib.parse.urlparse(u)
        parts = [x for x in p.path.strip("/").split("/") if x]
        if p.netloc.lower() == "raw.githubusercontent.com" and len(parts) >= 3:
            rest = parts[2:]
            if len(rest) >= 4 and rest[0] == "refs" and rest[1] == "heads":
                br, f = rest[2], "/".join(rest[3:])
            else:
                br, f = rest[0], "/".join(rest[1:])
            alt = "https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (parts[0], parts[1], br, f)
            if alt not in out:
                out.append(alt)
        return out

    def start_monitor(self):
        """后台按「自动监测间隔」定期测速；首次延迟 5 分钟，避免刚启动就打满外网。"""
        if self._monitor_started:
            return
        self._monitor_started = True

        def loop():
            time.sleep(300)
            while True:
                try:
                    c = self.cfg()
                    if c.get("auto_monitor") and c.get("enabled"):
                        self.check()
                except Exception:
                    pass
                try:
                    gap = max(60, int(self.cfg().get("interval_min") or 5) * 60)
                except Exception:
                    gap = 300
                time.sleep(gap)

        threading.Thread(target=loop, daemon=True, name="du-accel-monitor").start()


ACCEL = Accel()

# ---------------------------------------------------------------- 远程资源（图标 / README）按需抓取 + 本机缓存
ICON_DIR = os.path.join(CACHE_DIR, "icons")
README_DIR = os.path.join(CACHE_DIR, "readmes")
_ICON_ALLOW = {"ts": 0.0, "urls": frozenset()}
for _d in (ICON_DIR, README_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass

_REMOTE_TIMEOUT = 8


def _remote_candidates(url):
    """远程资源候选：GitHub 资源先走加速源优选，再直连，最后 jsdelivr CDN 回退。

    源索引同步 / 应用图标 / 上游 README / FPK 下载全部经过这里，
    因此「加速源设置」里选的线路对商店所有网络行为都生效。"""
    return ACCEL.candidates(url)


def _remote_get(url, max_bytes=2 * 1024 * 1024, want="binary"):
    cands = _remote_candidates(url)
    last = ""
    for c in cands:
        try:
            req = urllib.request.Request(c, headers={"User-Agent": "DockerUpdate/2.1",
                                                     "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=_REMOTE_TIMEOUT) as r:
                data = r.read(max_bytes + 1)
                ctype = (r.headers.get("Content-Type") or "application/octet-stream").split(";")[0]
            if len(data) > max_bytes:
                raise ValueError("远程资源超过 %d MB" % (max_bytes // 1024 // 1024))
            # 某些加速源挂掉时会返回一个 200 的 HTML 提示页，不能当成资源成功
            if ctype.startswith("text/html") and not url.lower().split("?")[0].endswith((".html", ".htm")):
                raise ValueError("加速源返回 HTML 页面而不是资源（%s）" % ctype)
            return data, ctype
        except Exception as exc:
            last = "%s：%s" % (c, exc)
    raise ValueError("远程资源获取失败（已尝试 %d 个地址）：%s" % (len(cands), last[:200]))


def fetch_icon(url):
    """把一个应用图标抓到本机缓存并返回 (bytes, content-type)。

    图标按 URL 的 sha1 命名，落在 data/store/cache/icons/；同一图标只拉一次。
    只接受常见图片格式，其它一律拒绝，避免把 HTML 错误页当图片返回。"""
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        raise ValueError("图标地址不是 http/https")
    key = hashlib.sha1(u.encode("utf-8")).hexdigest()[:24]
    ctype_file = os.path.join(ICON_DIR, key + ".type")
    blob_file = os.path.join(ICON_DIR, key + ".bin")
    if os.path.isfile(blob_file) and os.path.isfile(ctype_file):
        with open(blob_file, "rb") as f:
            data = f.read()
        with open(ctype_file, "r", encoding="utf-8") as f:
            ctype = f.read().strip()
        return data, ctype
    data, ctype = _remote_get(u, 2 * 1024 * 1024)
    low = u.lower().split("?")[0]
    ext = os.path.splitext(low)[1]
    allowed = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
               ".ico": "image/x-icon", ".bmp": "image/bmp"}
    if ext in allowed:
        ctype = allowed[ext]
    elif not ctype.startswith("image/"):
        # 内容嗅探兜底
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif data[:4] == b"GIF8":
            ctype = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"
        elif b"<svg" in data[:512].lower():
            ctype = "image/svg+xml"
        else:
            raise ValueError("远程返回的不是图片（%s）" % ctype)
    # SVG 可能带脚本，统一按纯文本处理由浏览器渲染；这里限制大小已足够
    with open(blob_file, "wb") as f:
        f.write(data)
    with open(ctype_file, "w", encoding="utf-8") as f:
        f.write(ctype)
    return data, ctype


def fetch_readme_text(url):
    """拉取上游 README 原文并缓存，供应用详情页展示真实软件介绍。"""
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        raise ValueError("README 地址不是 http/https")
    key = hashlib.sha1(u.encode("utf-8")).hexdigest()[:24]
    p = os.path.join(README_DIR, key + ".md")
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    data, _ = _remote_get(u, 512 * 1024)
    text = data.decode("utf-8", "replace")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return text


# ---------------------------------------------------------------- 桌面图标适配器

# ============================================================================
# v3.0：应用商店分区（应用源 / FPK / Moo / OAuth / 官方商店 / 已安装）已整体移除。
# 本文件只保留被 Docker 管理复用的底层组件：路径映射、受限 Compose 解析、
# GitHub 加速源与图标代理工具；桌面快捷方式已独立到 desktop.py。
# ============================================================================
