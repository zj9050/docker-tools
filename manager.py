"""DockerUpdate v1.2 业务逻辑：容器清单 / 版本检测 / 重建更新 / 版本备份与回退 / 策略 / 每日调度。"""
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import config
import events
import runtrace
import notify
from docker_api import DockerClient, DockerError

# create container 时可安全回传的 HostConfig 字段（inspect 中的只读字段一律过滤）
HC_WHITELIST = (
    "Binds", "PortBindings", "PublishAllPorts", "RestartPolicy", "NetworkMode",
    "Privileged", "CapAdd", "CapDrop", "Devices", "DeviceRequests", "Dns",
    "DnsSearch", "DnsOptions", "ExtraHosts", "GroupAdd", "IpcMode", "PidMode",
    "UsernsMode", "UTSMode", "Links", "LogConfig", "Memory", "MemorySwap",
    "MemoryReservation", "MemorySwappiness", "NanoCpus", "CpuShares", "CpuPeriod",
    "CpuQuota", "CpusetCpus", "CpusetMems", "CpuCount", "CpuPercent",
    "CpuRealtimePeriod", "CpuRealtimeRuntime", "OomKillDisable", "OomScoreAdj",
    "PidsLimit", "ReadonlyRootfs", "Runtime", "SecurityOpt", "ShmSize", "StorageOpt",
    "Sysctls", "Tmpfs", "Ulimits", "CgroupParent", "VolumeDriver", "Isolation",
    "Init", "MaskedPaths", "ReadonlyPaths", "Annotations", "BlkioWeight",
    "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps",
    "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps", "DeviceCgroupRules",
    "ConsoleSize", "ContainerIDFile", "CpuRealtimeRuntime",
)

VERSION_LABEL_KEYS = (
    "org.opencontainers.image.version", "org.label-schema.version", "version", "VERSION",
)
VERSION_ENV_KEYS = ("VERSION", "APP_VERSION", "IMAGE_VERSION", "PKG_VERSION")

ERROR_LABELS = {
    "timeout": "连接仓库超时",
    "network": "仓库网络不可达",
    "auth": "仓库认证失败",
    "notfound": "镜像不存在或无权限",
    "other": "检测失败",
}
ERROR_ADVICE = {
    "timeout": "Docker 守护进程连不上镜像仓库（常见于直连 Docker Hub 超时）。本次检测未完成，已保留上一次结果；请在 NAS 上配置 Docker 代理 / 镜像加速，或稍后重试。",
    "network": "Docker 守护进程无法访问镜像仓库，多为 DNS、代理或网络不通。本次检测未完成，已保留上一次结果。",
    "auth": "私有仓库需要账号密码，请在「设置 → 私有仓库认证」中填写。",
    "notfound": "仓库里找不到该镜像或没有拉取权限，请检查镜像名与 Tag。",
    "other": "拉取镜像时出错，可展开详情查看原始错误信息。",
}
RETRYABLE = ("timeout", "network")

# v3.9-r4：面板层可用的公共镜像加速源。用于「自动修复」时由面板经加速源代拉，
# 再重新打回原始 repo:tag，让 Docker 守护进程无需直连 Docker Hub 也能完成版本检测。
# 这不修改 NAS 的 /etc/docker/daemon.json（宿主机根目录是只读挂载）。
DOCKER_HUB_MIRRORS = (
    "docker.m.daocloud.io", "m.daocloud.io", "docker.1ms.run",
    "docker.1panel.live", "docker.xuanyuan.me", "hub.rat.dev",
)
GHCR_MIRRORS = ("ghcr.nju.edu.cn",)
MIRROR_READY_TTL = 6 * 3600     # 代拉结果有效期；过期后重新经加速源拉取以检查新版本


def mirror_refs(repo, tag):
    """把一个镜像引用展开成「加速源优先、原始 ref 兜底」的候选列表。"""
    tag = tag or "latest"
    out = []
    if repo.startswith("ghcr.io/"):
        rest = repo[len("ghcr.io/"):]
        out += ["%s/%s:%s" % (m, rest, tag) for m in GHCR_MIRRORS]
    elif repo.startswith("docker.io/"):
        rest = repo[len("docker.io/"):]
        for m in DOCKER_HUB_MIRRORS:
            out.append("%s/%s:%s" % (m, rest, tag))
    else:
        rest = repo if "/" in repo else "library/" + repo
        for m in DOCKER_HUB_MIRRORS:
            out.append("%s/%s:%s" % (m, rest, tag))
    out.append("%s:%s" % (repo, tag))
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r); uniq.append(r)
    return uniq

def registry_reachable(timeout=4.0):
    """面板容器侧轻量探测 Docker Hub V2 端点。

    只用于「整批检测前的快速失败」：连不上时不再让每个容器各等一次
    check_timeout（默认 300 秒）。注意这是面板容器的网络，与宿主机 Docker
    守护进程的网络基本一致但不等价；探测通过仍可能因 daemon 自身配置而失败。
    """
    import urllib.error
    import urllib.request
    url = "https://registry-1.docker.io/v2/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DockerUpdate/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(64)
            return True, "HTTP %s" % resp.status
    except urllib.error.HTTPError as exc:
        if exc.code in (200, 401, 403):
            return True, "HTTP %s（需要令牌，属正常）" % exc.code
        return False, "HTTP %s" % exc.code
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])
BACKUP_REASON = {"pre-update": "更新前备份", "pre-rollback": "回退前备份", "manual": "手动备份"}


def classify_error(message):
    """把 Docker 守护进程返回的仓库错误归类，便于界面给出准确提示。"""
    m = (message or "").lower()
    if ("client.timeout exceeded" in m or "awaiting headers" in m or "timeout" in m
            or "context deadline exceeded" in m or "request canceled" in m):
        return "timeout"
    if any(k in m for k in ("no such host", "connection refused", "network is unreachable",
                            "tls handshake", "unexpected eof", "i/o timeout",
                            "dial tcp", "proxyconnect", "connection reset", "eof")):
        return "network"
    if any(k in m for k in ("unauthorized", "authentication required", "access denied",
                            "denied: requested access", "incorrect username or password")):
        return "auth"
    if any(k in m for k in ("not found", "manifest unknown", "pull access denied",
                            "repository does not exist")):
        return "notfound"
    return "other"


def now_str():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d")


def short(i):
    return (i or "").replace("sha256:", "")[:12]


def docker_time(s):
    if not s:
        return ""
    try:
        head = s.split("+")[0].rstrip("Z")
        base = head.split(".")[0]
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(config.LOCAL_TZ).strftime("%m-%d %H:%M")
    except Exception:
        return str(s)[:16]


def parse_ref(ref):
    """返回 (repo, tag, kind)。kind: tag / pinned / local"""
    ref = (ref or "").strip()
    if not ref or ref.startswith("sha256:"):
        return "", "", "local"
    if "@sha256:" in ref:
        return ref.split("@")[0], "digest", "pinned"
    tail = ref.rsplit("/", 1)[-1]
    if ":" in tail:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag, "tag"
    return ref, "latest", "tag"


def pick_version(labels, env_list):
    labels = labels or {}
    for key in VERSION_LABEL_KEYS:
        if labels.get(key):
            return str(labels[key])
    for item in env_list or []:
        if "=" in item:
            k, v = item.split("=", 1)
            if k in VERSION_ENV_KEYS and v:
                return v
    return ""


def sanitize_host_config(hc):
    out = {k: v for k, v in (hc or {}).items() if k in HC_WHITELIST and v is not None}
    out["AutoRemove"] = False  # 重建后的容器不能自删除，否则无法回滚 / 清理
    return out


def build_create_body(ins, image_ref, cid):
    """用旧容器的完整配置生成 create 请求体，保证端口、卷、网络、环境变量、重启策略不丢失。"""
    cfg = ins.get("Config") or {}
    hc = ins.get("HostConfig") or {}
    body = {
        "Image": image_ref,
        "Env": cfg.get("Env"),
        "Cmd": cfg.get("Cmd"),
        "Entrypoint": cfg.get("Entrypoint"),
        "WorkingDir": cfg.get("WorkingDir") or None,
        "User": cfg.get("User") or "",
        "Hostname": cfg.get("Hostname") or "",
        "Domainname": cfg.get("Domainname") or "",
        "Labels": cfg.get("Labels") or {},
        "ExposedPorts": cfg.get("ExposedPorts") or {},
        "Volumes": cfg.get("Volumes") or {},
        "Tty": bool(cfg.get("Tty")),
        "OpenStdin": bool(cfg.get("OpenStdin")),
        "StdinOnce": False,
        "AttachStdin": False,
        "AttachStdout": False,
        "AttachStderr": False,
        "StopSignal": cfg.get("StopSignal"),
        "Healthcheck": cfg.get("Healthcheck"),
        "HostConfig": sanitize_host_config(hc),
    }
    body = {k: v for k, v in body.items() if v is not None}

    nm = (hc.get("NetworkMode") or "")
    nets = (ins.get("NetworkSettings") or {}).get("Networks") or {}
    endpoints = {}
    for net_name, info in nets.items():
        if net_name in ("host", "none") or net_name.startswith("container:"):
            continue
        ep = {}
        aliases = [a for a in (info.get("Aliases") or []) if a and a != cid[:12]]
        if aliases:
            ep["Aliases"] = aliases
        ipam = {}
        if info.get("IPv4Address"):
            ipam["IPv4Address"] = info["IPv4Address"].split("/")[0]
        if info.get("IPv6Address"):
            ipam["IPv6Address"] = info["IPv6Address"].split("/")[0]
        if ipam:
            ep["IPAMConfig"] = ipam
        if info.get("MacAddress"):
            ep["MacAddress"] = info["MacAddress"]
        endpoints[net_name] = ep
    if nm and nm not in ("bridge", "default", "host", "none", "") and not nm.startswith("container:"):
        eps = {k: v for k, v in endpoints.items() if k not in ("bridge", "host", "none")}
        if eps:
            body["NetworkingConfig"] = {"EndpointsConfig": eps}
    return body


class State:
    def __init__(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        self.lock = threading.RLock()
        self.data = {
            "settings": dict(config.DEFAULT_SETTINGS),
            "policies": {},
            "checks": {},
            "notes": {},          # 容器名 -> {alias, memo, updated_at}
            "last_daily_date": "",
            "last_full_check": "",
            "history": [],
        }
        try:
            with open(config.STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved.get("settings"), dict):
                self.data["settings"].update(
                    {k: v for k, v in saved["settings"].items() if k in config.SETTING_KEYS})
            for k in ("policies", "checks", "notes"):
                if isinstance(saved.get(k), dict):
                    self.data[k].update(saved[k])
            for k in ("last_daily_date", "last_full_check", "history"):
                if k in saved:
                    self.data[k] = saved[k]
        except Exception:
            pass

    def save(self):
        tmp = config.STATE_FILE + ".tmp"
        with self.lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, config.STATE_FILE)
            except Exception as exc:
                print("[state] 写入失败:", exc)

    def settings(self):
        with self.lock:
            return dict(self.data["settings"])

    def set_settings(self, patch):
        with self.lock:
            s = self.data["settings"]
            t = str(patch.get("daily_time", s.get("daily_time")))
            try:
                parts = t.split(":")
                hh, mm = int(parts[0]), int(parts[1])
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    raise ValueError()
                s["daily_time"] = "%02d:%02d" % (hh, mm)
            except Exception:
                raise ValueError("每日检查时间格式应为 HH:MM")
            for k in ("auto_update", "include_stopped", "check_on_start", "cleanup_images",
                      "verify_start", "exclude_watchtower_labels", "backup_on_update",
                      "monitor_enabled", "show_nav", "link_probe"):
                if k in patch:
                    s[k] = bool(patch[k])
            # v3.1：全局默认容器访问链接（仅在容器未单独配置时兜底）
            for k in ("lan_base", "wan_base", "link_mode"):
                if k in patch:
                    s[k] = str(patch[k] or "").strip()[:300]
            if s.get("link_mode") not in ("auto", "lan", "wan"):
                s["link_mode"] = "auto"
            # v3.2：每个容器单独的内网 / 外网地址与选择模式，key 为容器名。
            # v3.9：应用聚合也支持单独设置（app_links，key 为应用 key），同一套校验。
            for field in ("container_links", "app_links"):
                if field not in patch:
                    continue
                raw = patch.get(field)
                if not isinstance(raw, dict):
                    raise ValueError("访问链接配置格式不正确")
                out = {}
                for k, v in list(raw.items())[:300]:
                    key = str(k)[:128].strip()
                    if not key or not isinstance(v, dict):
                        continue
                    row = {}
                    for f in ("lan_base", "wan_base", "link_mode"):
                        row[f] = str(v.get(f) or "").strip()[:300]
                    if row["link_mode"] not in ("", "auto", "lan", "wan"):
                        row["link_mode"] = "auto"
                    out[key] = row
                s[field] = out
            for k, lo, hi in (("check_timeout", 30, 900), ("check_retries", 0, 3),
                              ("backup_keep", 1, 20), ("sample_interval", 3, 60),
                              ("refresh_interval", 3, 60),
                              ("log_retention_days", 1, 365)):
                if k in patch:
                    try:
                        v = int(patch[k])
                    except Exception:
                        raise ValueError("%s 必须是数字" % k)
                    if not lo <= v <= hi:
                        raise ValueError("%s 取值范围为 %d-%d" % (k, lo, hi))
                    s[k] = v
            # v6.0-r23 修复：「更新后健康检查与自动回退」这 6 个字段前端一直在
            # fillSettings 回填、btnSaveSettings 收集并 POST，但后端此前完全没有
            # 写入 state，导致点「保存」提示成功、刷新后又变回 config 默认值，
            # 实际执行健康检查时用户配置永远不生效。这里补齐校验与持久化。
            if "health_probe" in patch:
                probe = str(patch.get("health_probe") or "running").strip().lower()
                if probe not in ("running", "http", "tcp"):
                    raise ValueError("健康检查方式只能是 running / http / tcp")
                s["health_probe"] = probe
            if "health_path" in patch:
                hp = str(patch.get("health_path") or "/").strip()[:200]
                if hp and not hp.startswith("/"):
                    hp = "/" + hp
                s["health_path"] = hp or "/"
            for k, lo, hi in (("health_port", 0, 65535), ("health_wait_s", 0, 120),
                              ("health_timeout_s", 1, 30), ("health_retries", 0, 10)):
                if k in patch:
                    try:
                        v = int(patch[k])
                    except Exception:
                        raise ValueError("%s 必须是数字" % k)
                    if not lo <= v <= hi:
                        raise ValueError("%s 取值范围为 %d-%d" % (k, lo, hi))
                    s[k] = v
            self.save()

    def _prune_locked(self):
        """v3.9：按 settings.log_retention_days 清除过期运行日志（调用方已持锁）。"""
        try:
            days = max(1, min(365, int(self.data["settings"].get("log_retention_days", 7) or 7)))
        except Exception:
            days = 7
        cutoff = datetime.now(config.LOCAL_TZ) - timedelta(days=days)
        self.data["history"] = self.data["history"][:1000]
        keep = []
        for h in self.data["history"]:
            try:
                t = datetime.strptime(str(h.get("time", ""))[:19], "%Y-%m-%d %H:%M:%S")
                t = t.replace(tzinfo=config.LOCAL_TZ)
                if t >= cutoff:
                    keep.append(h)
            except Exception:
                keep.append(h)          # 格式异常的旧记录保留，避免误删
        self.data["history"] = keep

    def prune_history(self):
        with self.lock:
            before = len(self.data["history"])
            self._prune_locked()
            if len(self.data["history"]) != before:
                self.save()

    def add_history(self, action, container, message, ok=True):
        item = {"time": now_str(), "action": action, "container": container,
                "message": str(message), "ok": bool(ok)}
        with self.lock:
            self.data["history"].insert(0, item)
            self._prune_locked()
            self.save()


class Manager:
    def __init__(self):
        self.docker = DockerClient()
        self.state = State()
        self.job_lock = threading.Lock()
        self.runtime = {"busy": False, "job": "", "progress": 0, "total": 0,
                        "message": "", "started_at": None, "ok": True}
        self.image_cache = {}
        self.self_hostname = socket.gethostname()
        self.docker_version = ""
        self.docker_error = ""
        self.refresh_docker_version()
        self._migrate_timeout_errors()

    def _migrate_timeout_errors(self):
        """v3.9-r4：升级后界面不再残留任何「连接仓库超时」。

        旧 state.json 里存着的 timeout / network 错误一次性转成中性「已跳过」，
        保留 has_update，点「自动修复」或「检查全部」后即由加速源代拉恢复检测。"""
        changed = False
        for name, chk in (self.state.data.get("checks") or {}).items():
            if not isinstance(chk, dict) or not (chk.get("error") or ""):
                continue
            if chk.get("error_kind") in RETRYABLE or "Client.Timeout" in str(chk.get("error", "")):
                chk["error"] = ""; chk["error_kind"] = ""; chk["error_advice"] = ""
                chk["skipped"] = "上次检测因仓库不可达未完成；点「诊断修复 → 自动修复」即可恢复"
                changed = True
        if changed:
            self.state.save()

    # ---------- 基础 ----------
    def refresh_docker_version(self):
        try:
            v = self.docker.version() or {}
            self.docker_version = v.get("Version", "")
            self.docker_error = ""
        except Exception as exc:
            self.docker_error = getattr(exc, "message", str(exc))
            self.docker_version = ""

    def settings(self):
        return self.state.settings()

    def _image_info(self, image_id):
        if not image_id:
            return {}
        if image_id in self.image_cache:
            return self.image_cache[image_id]
        try:
            info = self.docker.inspect_image(image_id) or {}
        except DockerError:
            info = {}
        out = {
            "id": info.get("Id", ""),
            "created": info.get("Created", ""),
            "labels": (info.get("Config") or {}).get("Labels") or {},
            "repo_tags": info.get("RepoTags") or [],
            "repo_digests": info.get("RepoDigests") or [],
            "size": info.get("Size", 0),
        }
        self.image_cache[image_id] = out
        return out

    def _excluded(self, name, cid, labels):
        s = self.settings()
        if labels.get("dockerupdate.self") == "true":
            return True, "本应用容器（不监控自身）"
        if name == config.SELF_NAME or name == self.self_hostname or cid[:12] == self.self_hostname:
            return True, "本应用容器（不监控自身）"
        if labels.get("dockerupdate.exclude") == "true":
            return True, "已手动排除"
        if s.get("exclude_watchtower_labels"):
            if (labels.get("com.centurylinklabs.watchtower.enable") == "false" or
                    labels.get("com.containrrr.watchtower.enable") == "false"):
                return True, "沿用 watchtower 排除标签"
        return False, ""

    def _registry_auth(self, repo):
        try:
            with open(config.REGISTRY_AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        head = repo.split("/")[0]
        registry = head if ("." in head or ":" in head or head == "localhost") else "https://index.docker.io/v1/"
        cred = None
        if isinstance(data.get("auths"), dict):
            cred = data["auths"].get(registry) or data["auths"].get(head)
        if not cred:
            cred = data.get(registry) or data.get(repo)
        if not isinstance(cred, dict):
            return None
        username = cred.get("username") or cred.get("Username") or ""
        password = cred.get("password") or cred.get("Password") or ""
        if not username and not password:
            return None
        return {"username": username, "password": password, "serveraddress": registry}

    def _resolve(self, name):
        for c in self.docker.list_containers(all_=True):
            names = c.get("Names") or []
            n = names[0].lstrip("/") if names else ""
            if n == name or c.get("Id", "").startswith(name):
                return c["Id"], n
        raise DockerError(404, "找不到容器：%s" % name)

    # ---------- 版本备份 ----------
    @staticmethod
    def _backup_dir(name):
        safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(name))
        return os.path.join(config.BACKUP_DIR, safe or "_unnamed")

    def list_backups(self, name):
        d = self._backup_dir(name)
        if not os.path.isdir(d):
            return []
        out = []
        for fn in sorted(os.listdir(d), reverse=True):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                continue
            out.append({
                "id": fn[:-5],
                "time": rec.get("time", ""),
                "image_id": rec.get("image_id_short", ""),
                "image_ref": rec.get("image_ref", ""),
                "version": rec.get("version", ""),
                "image_created": rec.get("image_created", ""),
                "reason": rec.get("reason", ""),
                "reason_label": BACKUP_REASON.get(rec.get("reason", ""), "备份"),
                "running": rec.get("running", True),
            })
        return out

    def _write_backup(self, name, rec):
        d = self._backup_dir(name)
        os.makedirs(d, exist_ok=True)
        bid = str(time.time_ns())
        path = os.path.join(d, bid + ".json")
        tmp = path + ".tmp"
        # 备份里含完整环境变量，限制文件权限
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        keep = int(self.settings().get("backup_keep", 5))
        for old in sorted(os.listdir(d), reverse=True)[keep:]:
            if old.endswith(".json"):
                try:
                    os.unlink(os.path.join(d, old))
                except OSError:
                    pass
        return bid

    def backup_one(self, name, reason="manual"):
        """保存当前容器配置 + 镜像 ID 快照；回退时按镜像 ID 精确还原。"""
        cid, real_name = self._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        cfg = ins.get("Config") or {}
        old_id = ins.get("Image", "")
        info = self._image_info(old_id)
        rec = {
            "name": real_name,
            "time": now_str(),
            "reason": reason,
            "container_name": ins.get("Name", "/" + real_name).lstrip("/"),
            "image_id": old_id,
            "image_id_short": short(old_id),
            "image_ref": cfg.get("Image") or "",
            "image_created": docker_time(info.get("created", "")),
            "version": pick_version(info.get("labels"), cfg.get("Env")),
            "repo_digest": (info.get("repo_digests") or [""])[0],
            "running": bool((ins.get("State") or {}).get("Running")),
            "create_body": build_create_body(ins, cfg.get("Image") or "", cid),
        }
        bid = self._write_backup(real_name, rec)
        self._notify("DockerUpdate · 版本备份完成",
                     "容器「%s」已创建版本快照（%s），可用于一键回退。"
                     % (real_name, BACKUP_REASON.get(reason, "备份")), "backup_ok", real_name)
        self.state.add_history("backup", real_name,
                               "%s · 镜像 %s（%s）" % (BACKUP_REASON.get(reason, "备份"),
                                                       short(old_id), rec.get("version") or "未标注版本"))
        rec["id"] = bid
        return rec

    def _load_backup(self, name, backup_id=None):
        items = self.list_backups(name)
        if not items:
            return None
        item = next((x for x in items if x["id"] == str(backup_id)), None) if backup_id else items[0]
        if not item:
            return None
        path = os.path.join(self._backup_dir(name), item["id"] + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # ---------- 重建（更新与回退共用） ----------
    def _recreate(self, cid, real_name, body, was_running, hc):
        original_name = (self.docker.inspect_container(cid) or {}).get("Name", "/" + real_name).lstrip("/")
        backup = original_name + "_du_old"
        try:
            self.docker.remove_container(backup, force=True)
        except DockerError:
            pass

        self.docker.rename(cid, backup)
        try:
            self.docker.stop(cid, timeout=10)
        except DockerError:
            pass  # 304 表示原本已停止；其它异常会在创建失败时触发整体回滚

        created = None
        try:
            created = self.docker.create_container(original_name, body) or {}
            new_cid = created.get("Id", "")
            if not new_cid:
                raise DockerError(500, "创建容器失败：返回为空")
            if was_running:
                self.docker.start(new_cid)
                if self.settings().get("verify_start"):
                    time.sleep(3)
                    st = (self.docker.inspect_container(new_cid) or {}).get("State") or {}
                    restart = (hc.get("RestartPolicy") or {}).get("Name", "")
                    if st.get("Status") in ("exited", "dead") and restart in ("", "no"):
                        raise DockerError(500, "新容器启动后立即退出")
        except Exception:
            if created and created.get("Id"):
                try:
                    self.docker.remove_container(created["Id"], force=True)
                except Exception:
                    pass
            try:
                self.docker.rename(backup, original_name)
                if was_running:
                    self.docker.start(cid)
            except Exception:
                pass
            raise

        try:
            self.docker.remove_container(backup, force=True)
        except DockerError:
            pass
        return created["Id"]

    def _mirror_ready(self, repo, tag):
        """该 repo:tag 是否在有效期内已经由加速源代拉并重新打回标签。"""
        key = "%s:%s" % (repo, tag or "latest")
        ts = float((self.state.data.get("mirror_ready") or {}).get(key) or 0)
        return bool(ts and time.time() - ts < MIRROR_READY_TTL)

    def registry_reachable_cached(self, ttl=60):
        c = getattr(self, "_reg_cache", None)
        if c and time.time() - c.get("at", 0) < ttl:
            return c["ok"], c["detail"]
        ok, detail = registry_reachable()
        self._reg_cache = {"at": time.time(), "ok": ok, "detail": detail}
        return ok, detail

    def _autofix_one(self, name, emit=None):
        """经可用加速源代拉镜像并重新打回原始 repo:tag；成功即登记 mirror_ready。"""
        cid, real = self._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        repo, tag, kind = parse_ref((ins.get("Config") or {}).get("Image") or "")
        if kind != "tag":
            return {"name": real, "ok": False, "reason": "固定 digest / 本地构建镜像，无需联网检测"}
        timeout = int(self.settings().get("check_timeout", 300))
        tried = []
        for ref in mirror_refs(repo, tag):
            try:
                if emit: emit(stage="正在经加速源代拉镜像", detail=ref,
                              message="%s ← %s" % (real, ref))
                self.docker.pull_ref(ref, self._registry_auth(repo), timeout=min(timeout, 120))
                im = self.docker.inspect_image(ref) or {}
                iid = im.get("Id", "")
                if not iid:
                    continue
                self.docker.tag_image(iid, repo, tag)
                self.state.data.setdefault("mirror_ready", {})["%s:%s" % (repo, tag)] = time.time()
                self.state.save()
                return {"name": real, "ok": True, "via": ref, "image_id": short(iid)}
            except DockerError as exc:
                tried.append("%s：%s" % (ref.split("/")[0], str(exc)[:60]))
            except Exception as exc:
                tried.append("%s：%s" % (ref.split("/")[0], str(exc)[:60]))
        return {"name": real, "ok": False,
                "reason": "可用加速源均未同步该镜像（%s）" % "；".join(tried[:2])}

    def autofix_registry(self, names=None, emit=None):
        """批量自动修复：并发经加速源代拉；返回逐容器结果。"""
        from concurrent.futures import ThreadPoolExecutor
        targets = [str(n) for n in (names or [])]
        if not targets:
            targets = [c["name"] for c in self.list_containers()
                       if not c["excluded"] and c["kind"] == "tag"]
        if emit: emit(stage="准备加速源代拉", pct=4,
                      message="正在为 %d 个容器准备可用镜像源" % len(targets))
        results = []
        done = [0]
        lock = threading.Lock()

        def one(n):
            r = self._autofix_one(n, None)
            with lock:
                done[0] += 1
                results.append(r)
                if emit:
                    emit(stage="正在代拉并校验镜像", pct=min(96, 4 + int(done[0] * 90 / max(1, len(targets)))),
                         detail="%s · %s" % (n, "成功" if r["ok"] else "跳过"),
                         message="已处理 %d/%d" % (done[0], len(targets)))
            return r

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, targets))
        ok = sum(1 for r in results if r["ok"])
        self.state.data["last_autofix"] = {"at": now_str(), "ok": ok, "total": len(results)}
        self.state.save()
        msg = "自动修复完成：%d 个已通过加速源代拉，%d 个跳过（均不再显示连接仓库超时）" % (ok, len(results) - ok)
        self.state.add_history("check", "全部", msg, ok > 0)
        return {"ok": True, "message": msg, "ok_count": ok,
                "skip_count": len(results) - ok, "items": results}

    def _mark_skipped(self, name, reason):
        """检测无法完成时写中性「已跳过」，界面不再出现橙色「连接仓库超时」。"""
        # 不覆盖 has_update：沿用上一次检测到的「发现新版本」，只把本次标记为中性跳过
        self._store_check(name, {"checked_at": now_str(),
                                 "error": "", "error_kind": "", "error_advice": "",
                                 "skipped": reason, "precheck_failed": False})

    def _pull_with_retry(self, repo, tag):
        # v3.9-r4：近期已由加速源代拉并打回原标签，直接复用本地镜像，
        # 不再让 Docker 守护进程去直连超时的 registry-1.docker.io。
        if self._mirror_ready(repo, tag):
            try:
                im = self.docker.inspect_image("%s:%s" % (repo, tag)) or {}
                if im.get("Id"):
                    return
            except DockerError:
                pass
        s = self.settings()
        timeout = int(s.get("check_timeout", 300))
        retries = int(s.get("check_retries", 1))
        last = None
        for attempt in range(retries + 1):
            try:
                self.docker.pull(repo, tag, self._registry_auth(repo), timeout=timeout)
                return
            except DockerError as exc:
                last = exc
                if classify_error(exc.message) in RETRYABLE and attempt < retries:
                    time.sleep(3)
                    continue
                raise
        raise last

    @staticmethod
    def _friendly_pull_error(exc):
        kind = classify_error(exc.message)
        if kind in RETRYABLE:
            return DockerError(502, "%s：%s" % (ERROR_LABELS[kind], ERROR_ADVICE[kind]))
        return exc

    # ---------- 列表 ----------
    def list_containers(self):
        try:
            raw = self.docker.list_containers(all_=True) or []
        except DockerError as exc:
            self.docker_error = exc.message
            raw = []
        else:
            self.docker_error = ""
        notes = self.notes()
        out = []
        for c in raw:
            cid = c.get("Id", "")
            names = c.get("Names") or []
            name = names[0].lstrip("/") if names else cid[:12]
            try:
                ins = self.docker.inspect_container(cid) or {}
            except DockerError:
                continue
            cfg = ins.get("Config") or {}
            hc = ins.get("HostConfig") or {}
            stt = ins.get("State") or {}
            labels = cfg.get("Labels") or {}
            image_ref = cfg.get("Image") or c.get("Image") or ""
            image_id = ins.get("Image", "")
            info = self._image_info(image_id)
            repo, tag, kind = parse_ref(image_ref)
            excluded, reason = self._excluded(name, cid, labels)
            pol = self.state.data["policies"].get(name) or {}
            mode = pol.get("mode", "manual")
            chk = self.state.data["checks"].get(name) or {}
            has_update = bool(chk.get("has_update"))
            latest_id = chk.get("latest_image_id", "")
            ignored = mode == "hold" and latest_id and pol.get("hold_digest") == latest_id

            ports = []
            for k, v in ((ins.get("NetworkSettings") or {}).get("Ports") or {}).items():
                if v:
                    ports.append("%s → %s" % (",".join(x.get("HostPort", "") for x in v), k))
                else:
                    ports.append(k)
            note = notes.get(name) or {}
            out.append({
                "id": cid[:12],
                "name": name,
                "alias": note.get("alias", ""),
                "memo": note.get("memo", ""),
                "has_note": bool(note.get("alias") or note.get("memo")),
                "status": stt.get("Status") or c.get("State") or "",
                "running": bool(stt.get("Running")),
                "created": docker_time(ins.get("Created", "")),
                "project": labels.get("com.docker.compose.project", ""),
                "service": labels.get("com.docker.compose.service", ""),
                "image_ref": image_ref,
                "repo": repo,
                "tag": tag,
                "kind": kind,
                "image_id": short(image_id),
                "image_created": docker_time(info.get("created", "")),
                "version_label": pick_version(info.get("labels"), cfg.get("Env")),
                "repo_digest": (info.get("repo_digests") or [""])[0],
                "ports": ports,
                "volumes": hc.get("Binds") or [],
                "networks": list(((ins.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
                "env": cfg.get("Env") or [],
                "labels": labels,
                "restart_policy": (hc.get("RestartPolicy") or {}).get("Name", ""),
                # v2.0 管理增强：资源限制、特权状态、运行时长与健康检查
                "privileged": bool(hc.get("Privileged")),
                "mem_limit_mb": round(float(hc.get("Memory") or 0) / 1048576),
                "cpu_cores": round(float(hc.get("NanoCpus") or 0) / 1e9, 2),
                "restart_count": int((stt.get("RestartCount") if isinstance(stt, dict) else 0) or 0),
                "health_status": (stt.get("Health") or {}).get("Status", "") if isinstance(stt, dict) else "",
                "started_at": str(stt.get("StartedAt", ""))[:19].replace("T", " ") if isinstance(stt, dict) else "",
                "network_mode": hc.get("NetworkMode") or "",
                "excluded": excluded,
                "exclude_reason": reason,
                "policy": mode,
                "hold_digest": pol.get("hold_digest", ""),
                "checked_at": chk.get("checked_at", ""),
                "has_update": has_update,
                "ignored": ignored,
                "latest_image_id": latest_id,
                "latest_created": chk.get("latest_created", ""),
                "latest_version": chk.get("latest_version", ""),
                "skipped": chk.get("skipped", ""),
                "check_error": chk.get("error", ""),
                "error_kind": chk.get("error_kind", ""),
                "error_advice": chk.get("error_advice", ""),
                "updated_at": chk.get("updated_at", ""),
                "rolled_back": bool(chk.get("rolled_back")),
                "rollback_to": chk.get("rollback_to", ""),
                # v5.0：最近一次更新后的健康检查结果
                "health_ok": chk.get("health_ok"),
                "health_detail": chk.get("health_detail", ""),
                "health_skipped": bool(chk.get("health_skipped")),
                "auto_rollback": bool(chk.get("auto_rollback")),
                "backups": self.list_backups(name)[:5],
                "can_update": (not excluded and kind == "tag"),
            })
        out.sort(key=lambda x: (not x["running"], x["name"].lower()))
        return out

    def stats(self, containers):
        backups = sum(len(c["backups"]) for c in containers)
        return {
            "total": len(containers),
            "running": sum(1 for c in containers if c["running"]),
            "updates": sum(1 for c in containers if c["has_update"] and not c["excluded"]),
            "auto": sum(1 for c in containers if c["policy"] == "auto" and not c["excluded"]),
            "hold": sum(1 for c in containers if c["policy"] == "hold" and not c["excluded"]),
            "excluded": sum(1 for c in containers if c["excluded"]),
            "backups": backups,
            "notes": sum(1 for c in containers if c["has_note"]),
        }

    def next_check(self):
        s = self.settings()
        try:
            parts = str(s.get("daily_time", "03:00")).split(":")
            hh, mm = int(parts[0]), int(parts[1])
        except Exception:
            hh, mm = 3, 0
        now = datetime.now(config.LOCAL_TZ)
        t = now.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t.strftime("%m-%d %H:%M")

    def snapshot(self):
        self.state.prune_history()   # v3.9：到期的运行日志自动清除
        self.refresh_docker_version()
        containers = self.list_containers()
        return {
            "ok": True,
            "app_version": config.APP_VERSION,
            "docker_connected": not bool(self.docker_error),
            "docker_version": self.docker_version,
            "docker_error": self.docker_error,
            "server_time": now_str(),
            "auth_required": True,
            "settings": self.settings(),
            "runtime": dict(self.runtime),
            "stats": self.stats(containers),
            "containers": containers,
            "notes": self.notes(),
            "history": self.state.data["history"][:500],
            "last_full_check": self.state.data.get("last_full_check", ""),
            "next_check": self.next_check(),
        }

    # ---------- 检测 ----------
    def _store_check(self, name, data):
        chk = dict(self.state.data["checks"].get(name) or {})
        chk.update(data)
        self.state.data["checks"][name] = chk
        self.state.save()

    def check_one(self, name):
        cid, real_name = self._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        cfg = ins.get("Config") or {}
        labels = cfg.get("Labels") or {}
        excluded, reason = self._excluded(real_name, cid, labels)
        base = {"checked_at": now_str(), "name": real_name}
        if excluded:
            self._store_check(real_name, dict(base, has_update=False, skipped=reason,
                                              error="", error_kind=""))
            return {"has_update": False, "skipped": reason}
        repo, tag, kind = parse_ref(cfg.get("Image") or "")
        if kind != "tag":
            msg = "镜像以 digest 固定，无法检测浮动版本" if kind == "pinned" else "本地构建 / 无标签镜像，跳过检测"
            self._store_check(real_name, dict(base, has_update=False, skipped=msg,
                                              error="", error_kind=""))
            return {"has_update": False, "skipped": msg}
        # v3.9-r4：Docker Hub 不可达时，不再硬等 300 秒超时；先尝试加速源代拉，
        # 仍不成功就标记为中性「已跳过」，界面不出现「连接仓库超时」。
        if not self._mirror_ready(repo, tag):
            reachable, detail = self.registry_reachable_cached()
            if not reachable:
                fx = self._autofix_one(real_name)
                if not fx["ok"]:
                    self._mark_skipped(real_name, fx["reason"] or "加速源未同步该镜像，已跳过本次检测")
                    return {"has_update": False, "skipped": fx["reason"]}
                self._reg_cache = {"at": time.time(), "ok": True,
                                   "detail": "已由加速源代拉：" + fx["via"]}
        try:
            self._pull_with_retry(repo, tag)
            new_info = self.docker.inspect_image("%s:%s" % (repo, tag)) or {}
            new_id = new_info.get("Id", "")
            has = bool(new_id and new_id != ins.get("Image", ""))
            latest_labels = (new_info.get("Config") or {}).get("Labels") or {}
            self._store_check(real_name, {
                "checked_at": now_str(), "has_update": has, "skipped": "",
                "error": "", "error_kind": "", "error_advice": "", "rolled_back": False,
                "repo": repo, "tag": tag,
                "latest_image_id": short(new_id),
                "latest_created": docker_time(new_info.get("Created", "")),
                "latest_version": pick_version(latest_labels, (new_info.get("Config") or {}).get("Env")),
            })
            self.image_cache.clear()
            self.state.add_history("check", real_name,
                                   "发现新版本 %s" % short(new_id) if has else "已是最新 %s" % tag)
            # v5.1：只在「从无到有」发现新版本时推送，避免每天巡检重复刷屏
            if has and not bool((self.state.data["checks"].get(real_name) or {}).get("_notified_update")):
                self._notify("DockerUpdate · 发现可更新容器",
                             "容器「%s」（%s:%s）发现新版本：%s · %s。可点「立即更新」或设为自动更新。"
                             % (real_name, repo, tag, short(new_id),
                                pick_version(latest_labels, (new_info.get("Config") or {}).get("Env")) or "未标注版本"),
                             "found_update", real_name)
            self._store_check(real_name, {"_notified_update": bool(has)})
            return {"has_update": has, "latest_image_id": short(new_id)}
        except DockerError as exc:
            # 关键：检测失败时不清空 has_update，避免把「上次发现的新版」误判为消失
            kind_err = classify_error(exc.message)
            self._store_check(real_name, {
                "checked_at": now_str(), "error": exc.message,
                "error_kind": kind_err, "error_advice": ERROR_ADVICE[kind_err], "skipped": "",
            })
            self.state.add_history("check", real_name,
                                   "%s（本次检测未完成）：%s" % (ERROR_LABELS[kind_err], exc.message[:120]), False)
            self._notify("DockerUpdate · 版本检测失败",
                         "容器「%s」本次检测未完成（%s）：%s。已保留上一次结果。"
                         % (real_name, ERROR_LABELS[kind_err], exc.message[:120]),
                         "check_fail", real_name, "mid")
            return {"has_update": False, "error": exc.message, "error_kind": kind_err}

    # ---------- 更新 ----------
    def _notify(self, title, text, event, name="面板", level="info"):
        """v5.1：告警 Webhook + 多渠道通知共用一份事件开关，异步发送不阻塞主流程。"""
        try:
            events.emit(event, title, text, level, name)
        except Exception as exc:
            print("[events]", exc)

    def _health_host_port(self, ins, st):
        """取健康检查用的宿主机端口：优先手填，其次取第一个 TCP 端口映射。"""
        try:
            p = int(st.get("health_port") or 0)
        except Exception:
            p = 0
        if 0 < p < 65536:
            return p
        pb = (ins.get("HostConfig") or {}).get("PortBindings") or {}
        for key, binds in pb.items():
            if not str(key).endswith("/tcp"):
                continue
            for b in (binds or []):
                try:
                    hp = int(b.get("HostPort") or 0)
                except Exception:
                    continue
                if 0 < hp < 65536:
                    return hp
        return 0

    def _post_update_health(self, cid, real_name, old_ins, st):
        """v5.0：更新重建之后按配置做健康检查；返回 ok / detail / auto_rollback。"""
        if not st.get("health_enabled"):
            return {"ok": True, "skipped": True, "detail": "健康检查未启用",
                    "auto_rollback": False}
        probe = str(st.get("health_probe") or "running")
        if probe not in ("running", "http", "tcp"):
            probe = "running"
        def _int(k, lo, hi, dflt):
            try:
                return max(lo, min(hi, int(st.get(k) or dflt)))
            except Exception:
                return dflt
        wait = _int("health_wait_s", 0, 120, 5)
        timeout = _int("health_timeout_s", 1, 30, 3)
        retries = _int("health_retries", 0, 10, 2)
        auto_rb = bool(st.get("health_auto_rollback"))
        port = self._health_host_port(old_ins, st)
        path = str(st.get("health_path") or "/")
        if not path.startswith("/"):
            path = "/" + path
        degraded = ""
        if probe in ("http", "tcp") and not port:
            degraded = "该容器没有可用的 TCP 端口映射，已降级为运行状态检查"
            probe = "running"
        # 等待应用起来（分段 sleep，避免长时间完全无响应）
        left = wait
        while left > 0:
            time.sleep(min(2.0, left))
            left -= 2.0
        last = "未知原因"
        for attempt in range(retries + 1):
            try:
                ins = self.docker.inspect_container(cid) or {}
                state = ins.get("State") or {}
            except Exception as exc:
                state = {}
                last = "读取容器状态失败：%s" % str(exc)[:100]
            running = bool(state.get("Running"))
            if not running:
                last = "容器状态 %s（未运行）" % (state.get("Status") or "unknown")
            elif probe == "running":
                return {"ok": True, "skipped": False,
                        "detail": ("%s · " % degraded if degraded else "")
                                  + "容器运行中 · 第 %d 次探测通过" % (attempt + 1),
                        "auto_rollback": auto_rb}
            elif probe == "tcp":
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                        return {"ok": True, "skipped": False,
                                "detail": "TCP 127.0.0.1:%d 可连接 · 第 %d 次通过"
                                          % (port, attempt + 1),
                                "auto_rollback": auto_rb}
                except Exception as exc:
                    last = "TCP 127.0.0.1:%d 连接失败：%s" % (port, str(exc)[:80])
            elif probe == "http":
                url = "http://127.0.0.1:%d%s" % (port, path)
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "DockerUpdate/5.0"})
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        code = r.status
                        r.read(256)
                    # 4xx 说明服务在响应（多半是应用要求登录），不算挂了
                    if code < 500:
                        return {"ok": True, "skipped": False,
                                "detail": "HTTP %s → %d · 第 %d 次通过" % (path, code, attempt + 1),
                                "auto_rollback": auto_rb}
                    last = "HTTP %s → %d（服务端错误）" % (path, code)
                except urllib.error.HTTPError as e:
                    if e.code < 500:
                        return {"ok": True, "skipped": False,
                                "detail": "HTTP %s → %d（服务有响应）" % (path, e.code),
                                "auto_rollback": auto_rb}
                    last = "HTTP %s → %d" % (path, e.code)
                except Exception as exc:
                    last = "HTTP %s 请求失败：%s" % (path, str(exc)[:80])
            if attempt < retries:
                time.sleep(min(2, timeout))
        return {"ok": False, "skipped": False, "detail": last,
                "auto_rollback": auto_rb}

    def update_one(self, name, force=False):
        cid, real_name = self._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        cfg = ins.get("Config") or {}
        labels = cfg.get("Labels") or {}
        excluded, reason = self._excluded(real_name, cid, labels)
        if excluded:
            raise DockerError(403, "该容器已被排除：%s" % reason)
        repo, tag, kind = parse_ref(cfg.get("Image") or "")
        if kind != "tag":
            raise DockerError(400, "固定 digest / 本地镜像无法自动重建，请手动更新")
        image_ref = "%s:%s" % (repo, tag)
        try:
            self._pull_with_retry(repo, tag)
        except DockerError as exc:
            raise self._friendly_pull_error(exc)
        new_info = self.docker.inspect_image(image_ref) or {}
        new_id = new_info.get("Id", "")
        old_image_id = ins.get("Image", "")
        if not new_id:
            raise DockerError(500, "拉取后未找到镜像 %s" % image_ref)
        if new_id == old_image_id and not force:
            self._store_check(real_name, {"has_update": False, "checked_at": now_str(),
                                          "error": "", "error_kind": ""})
            return "已是最新版本，无需重建"

        # 重建之前先落盘一份可精确还原的配置 + 镜像 ID 快照
        if self.settings().get("backup_on_update"):
            self.backup_one(real_name, "pre-update")

        hc = ins.get("HostConfig") or {}
        was_running = bool((ins.get("State") or {}).get("Running"))
        body = build_create_body(ins, image_ref, cid)
        new_cid = self._recreate(cid, real_name, body, was_running, hc)

        # v5.0：更新后健康检查；不通过且开启自动回退时，立刻回到备份的旧版本
        health = self._post_update_health(new_cid, real_name, ins, self.settings())
        if (not health.get("ok")) and (not health.get("skipped")) and health.get("auto_rollback"):
            detail = health.get("detail") or "健康检查未通过"
            try:
                self.rollback_one(real_name)
                self._store_check(real_name, {"health_ok": False, "health_detail": detail,
                                              "auto_rollback": True, "rolled_back": True,
                                              "checked_at": now_str()})
                self.state.add_history("rollback", real_name,
                                       "更新后健康检查未通过（%s），已自动回退到旧版本" % detail)
                self._notify("DockerUpdate · 已自动回退",
                            "容器「%s」更新后健康检查未通过：%s。已自动回退到更新前的版本。"
                            % (real_name, detail), "auto_rollback", real_name, "high")
                return "更新后健康检查未通过（%s），已自动回退到旧版本" % detail
            except Exception as exc:
                self._store_check(real_name, {"health_ok": False, "health_detail": detail,
                                              "auto_rollback": False, "checked_at": now_str()})
                self._notify("DockerUpdate · 更新异常",
                            "容器「%s」更新后健康检查未通过（%s），自动回退也失败：%s，请手动处理。"
                            % (real_name, detail, str(exc)[:150]), "update_fail",
                            real_name, "high")
                return "更新完成但健康检查未通过（%s），自动回退失败：%s" % (detail, str(exc)[:150])

        if self.settings().get("cleanup_images"):
            try:
                self.docker.remove_image(old_image_id)
            except DockerError:
                pass

        self.image_cache.clear()
        self._store_check(real_name, {
            "has_update": False, "error": "", "error_kind": "", "error_advice": "",
            "skipped": "", "checked_at": now_str(), "updated_at": now_str(),
            "rolled_back": False,
            "previous_image_id": short(old_image_id),
            "latest_image_id": short(new_id),
            "latest_created": docker_time(new_info.get("Created", "")),
            "latest_version": pick_version((new_info.get("Config") or {}).get("Labels"),
                                           (new_info.get("Config") or {}).get("Env")),
            "health_ok": bool(health.get("ok")),
            "health_detail": health.get("detail", ""),
            "health_skipped": bool(health.get("skipped")),
            "auto_rollback": False, "_notified_update": False,
        })
        self._notify("DockerUpdate · 容器已更新",
                    "容器「%s」已更新到 %s。%s；旧版本已备份，可一键回退。"
                    % (real_name, short(new_id), health.get("detail") or "运行状态正常"),
                    "update_ok", real_name)
        self.state.data["policies"].setdefault(real_name, {})
        self.state.save()
        self.state.add_history("update", real_name,
                               "%s → %s（已备份旧版本，端口/卷/网络/环境变量均保留）"
                               % (short(old_image_id), short(new_id)))
        return "更新完成：%s（旧版本已备份，可一键回退）" % short(new_id)

    # ---------- 快速回退 ----------
    def rollback_one(self, name, backup_id=None):
        rec = self._load_backup(name, backup_id)
        if not rec:
            raise DockerError(404, "没有可用的版本备份，请先更新一次或手动备份")
        old_id = rec.get("image_id") or ""
        if not old_id:
            raise DockerError(400, "备份文件损坏：缺少镜像 ID")
        ref = old_id if old_id.startswith("sha256:") else "sha256:" + old_id
        try:
            self.docker.inspect_image(ref)
        except DockerError:
            raise DockerError(400, "旧镜像 %s 已不在本机（可能开启了「清理旧镜像」或被手动删除），无法快速回退"
                              % short(old_id))

        # 先记录回退前的状态，方便再切回新版本；目标备份已读入内存，不会被保留策略挤掉
        if self.settings().get("backup_on_update"):
            self.backup_one(name, "pre-rollback")

        cid, real_name = self._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        current_id = ins.get("Image", "")
        was_running = bool((ins.get("State") or {}).get("Running"))
        hc = ins.get("HostConfig") or {}
        body = dict(rec.get("create_body") or {})
        if not body:
            raise DockerError(400, "备份文件损坏：缺少容器配置")
        body["Image"] = ref  # 按镜像 ID 精确还原，不受 Tag 已指向新版的影响
        self._recreate(cid, real_name, body, was_running, hc)

        self.image_cache.clear()
        self._store_check(real_name, {
            "has_update": True, "error": "", "error_kind": "", "error_advice": "",
            "skipped": "", "checked_at": now_str(), "updated_at": now_str(),
            "rolled_back": True, "rollback_to": short(old_id),
            "rollback_time": rec.get("time", ""),
            "previous_image_id": short(current_id),
            "latest_image_id": short(current_id),
            "latest_version": rec.get("version", ""),
        })
        self.state.add_history("rollback", real_name,
                               "已回退到 %s（%s 的备份），仓库中的新版仍可再次更新"
                               % (short(old_id), rec.get("time", "")))
        self._notify("DockerUpdate · 已回退到旧版本",
                     "容器「%s」已回退到 %s（%s 的快照），仓库中的新版仍可再次更新。"
                     % (real_name, short(old_id), rec.get("time", "")),
                     "rollback_ok", real_name)
        return "已回退到 %s（%s），新版本仍可再次更新" % (short(old_id), rec.get("version") or "旧版本")

    # ---------- 任务调度 ----------
    def _start_job(self, job, message, fn):
        if not self.job_lock.acquire(blocking=False):
            return False, "已有任务正在执行，请稍候"
        _rid = runtrace.current()
        self.runtime.update({"busy": True, "job": job, "progress": 0, "total": 0,
                             "message": message, "started_at": now_str(), "ok": True,
                             "trace_id": _rid})

        def runner():
            if _rid:
                runtrace.bind(_rid)
                runtrace.note("任务", "启动后台任务「%s」" % job, "step", rid=_rid)
            try:
                fn()
            except Exception as exc:
                self.runtime["ok"] = False
                self.runtime["message"] = str(exc)
                self.state.add_history(job, "全部", "任务异常：%s" % exc, False)
                emap = {"update": "update_fail", "rollback": "rollback_fail",
                        "check": "check_fail", "check-all": "check_fail",
                        "daily-check": "check_fail", "image-pull": "image_pull_fail"}
                try:
                    events.emit(emap.get(job, "task_fail"), "DockerUpdate · 任务失败",
                                "后台任务「%s」执行失败：%s" % (job, str(exc)[:200]),
                                "high", "面板")
                except Exception:
                    pass
            finally:
                _ok = bool(self.runtime.get("ok", True))
                _msg = str(self.runtime.get("message") or "任务结束")
                self.runtime.update({"busy": False, "job": "", "progress": 0,
                                     "total": 0, "started_at": None})
                if _rid:
                    runtrace.note("任务", ("✓ " if _ok else "✗ ") + _msg,
                                  "ok" if _ok else "err", rid=_rid)
                    runtrace.finish(_rid, _ok, _msg)
                    runtrace.unbind()
                self.job_lock.release()

        threading.Thread(target=runner, daemon=True).start()
        return True, ""

    def _job_check_all(self, auto=False):
        s = self.settings()
        containers = self.list_containers()
        targets = [c for c in containers
                   if not c["excluded"] and c["kind"] == "tag"
                   and (s.get("include_stopped") or c["status"] == "running")]
        self.runtime["total"] = len(targets)
        self.runtime["progress"] = 0

        # v3.9-r3：整批检测前先做一次仓库连通性预检。连不上就快速失败，
        # 不再让每个容器各等一次 check_timeout，也不误触发自动更新。
        ok, detail = registry_reachable()
        pre = {"at": now_str(), "ok": bool(ok), "detail": detail}
        self.state.data["registry_precheck"] = pre
        self.state.save()
        advice = ("面板容器网络探测 registry-1.docker.io 失败（%s）。Docker 守护进程通常同样连不上；"
                  "本次检测未完成，已保留上一次结果。请在 NAS 上配置 Docker 代理 / registry mirror 后重启 Docker，"
                  "再点「检查更新」，或在卡片上点「诊断修复」查看实测结果。") % detail
        if not ok and not s.get("skip_registry_precheck"):
            # v3.9-r4：预检失败先自动经加速源代拉；代拉成功的继续检测，
            # 未成功的写中性「已跳过」，绝不留下橙色「连接仓库超时」。
            self.runtime["message"] = "Docker Hub 不可达，正在经加速源自动代拉镜像…"
            fixed = self.autofix_registry([c["name"] for c in targets])
            ready = {r["name"] for r in fixed["items"] if r["ok"]}
            for c in targets:
                if c["name"] not in ready:
                    reason = next((r["reason"] for r in fixed["items"]
                                   if r["name"] == c["name"] and not r["ok"]), "")
                    self._mark_skipped(c["name"], reason or "加速源未同步该镜像，已跳过本次检测")
            targets = [c for c in targets if c["name"] in ready]
            self.runtime["total"] = len(targets)
            self.runtime["message"] = ("已通过加速源代拉 %d 个镜像，继续比对版本" % len(targets))
            if not targets:
                self.state.data["last_full_check"] = now_str()
                if auto:
                    self.state.data["last_daily_date"] = today_str()
                self.runtime["progress"] = 1
                self.runtime["message"] = fixed["message"]
                self.state.save()
                return

        auto_done, failed, net_fail, circuit = [], 0, 0, 0
        for i, c in enumerate(targets):
            self.runtime["message"] = "正在检测 %s（%d/%d）" % (c["name"], i + 1, len(targets))
            runtrace.note("检测", "检查 %s · 镜像 %s（%d/%d）" %
                          (c["name"], c.get("image") or "-", i + 1, len(targets)), "step")
            try:
                chk = self.check_one(c["name"])
            except Exception as exc:
                self.state.add_history("check", c["name"], "检测失败：%s" % exc, False)
                chk = {"error": str(exc), "error_kind": classify_error(str(exc))}
            self.runtime["progress"] = i + 1
            if chk.get("error"):
                failed += 1
                net_fail = net_fail + 1 if chk.get("error_kind") in RETRYABLE else 0
            else:
                net_fail = 0
                if auto and chk.get("has_update") and s.get("auto_update"):
                    pol = self.state.data["policies"].get(c["name"]) or {}
                    if pol.get("mode") == "auto":
                        self.runtime["message"] = "正在自动更新 %s" % c["name"]
                        runtrace.note("更新", "自动更新 %s" % c["name"], "step")
                        try:
                            self.update_one(c["name"])
                            auto_done.append(c["name"])
                        except Exception as exc:
                            self.state.add_history("auto-update", c["name"], "自动更新失败：%s" % exc, False)
                            try:
                                events.emit("update_fail", "DockerUpdate · 自动更新失败",
                                            "容器「%s」自动更新失败：%s" % (c["name"], str(exc)[:180]),
                                            "high", c["name"])
                            except Exception:
                                pass
            # 预检通过但 daemon 仍连不上时，连续两个仓库超时就熔断剩余容器，
            # 避免 N × check_timeout 的空等，且不触发任何自动更新。
            if net_fail >= 2 and i + 1 < len(targets):
                rest = targets[i + 1:]
                for r in rest:
                    self._mark_skipped(r["name"], "连续仓库超时，已跳过本次检测（可点「自动修复」）")
                failed += len(rest)
                circuit = len(rest)
                self.runtime["progress"] = len(targets)
                break
        self.state.data["last_full_check"] = now_str()
        if auto:
            self.state.data["last_daily_date"] = today_str()
        self.state.save()
        upd = sum(1 for c in self.list_containers() if c["has_update"] and not c["excluded"])
        self.runtime["message"] = "检测完成：%d 个容器，%d 个可更新" % (len(targets), upd)
        if failed:
            self.runtime["message"] += "，%d 个因仓库连接未完成（已保留上次结果）" % failed
        if circuit:
            self.runtime["message"] += "，连续超时后跳过 %d 个" % circuit
        if auto_done:
            self.runtime["message"] += "，已自动更新 %d 个" % len(auto_done)
        self.state.add_history("check-all" if not auto else "daily-check", "全部",
                               self.runtime["message"], failed == 0)
        try:
            events.emit("check_done", "DockerUpdate · 全量巡检完成",
                        self.runtime["message"], "info", "全部", dedupe=False)
        except Exception:
            pass

    def _job_single(self, kind, name, backup_id=None):
        self.runtime["total"] = 1
        label = {"check": "检测", "update": "更新", "rollback": "回退", "backup": "备份"}[kind]
        self.runtime["message"] = "正在%s %s" % (label, name)
        runtrace.note("任务", "开始%s %s" % (label, name), "step")
        if kind == "check":
            self.check_one(name)
        elif kind == "update":
            self.update_one(name, force=True)
        elif kind == "rollback":
            self.rollback_one(name, backup_id)
        else:
            self.backup_one(name, "manual")
        self.runtime["progress"] = 1
        self.runtime["message"] = "%s %s 完成" % (label, name)

    def check_all(self, auto=False):
        return self._start_job("daily-check" if auto else "check-all", "准备检测全部容器…",
                               lambda: self._job_check_all(auto))

    def check_single(self, name):
        return self._start_job("check", "准备检测…", lambda: self._job_single("check", name))

    def update_single(self, name):
        return self._start_job("update", "准备更新（将先备份旧版本）…",
                               lambda: self._job_single("update", name))

    def rollback_single(self, name, backup_id=None):
        return self._start_job("rollback", "准备回退（将先备份当前版本）…",
                               lambda: self._job_single("rollback", name, backup_id))

    def backup_single(self, name):
        return self._start_job("backup", "准备备份…", lambda: self._job_single("backup", name))

    def set_policy(self, name, mode):
        if mode not in ("manual", "auto", "hold"):
            raise ValueError("未知策略：%s" % mode)
        pol = dict(self.state.data["policies"].get(name) or {})
        pol["mode"] = mode
        if mode == "hold":
            pol["hold_digest"] = (self.state.data["checks"].get(name) or {}).get("latest_image_id", "")
        else:
            pol.pop("hold_digest", None)
        pol["updated_at"] = now_str()
        self.state.data["policies"][name] = pol
        self.state.save()
        label = {"manual": "恢复手动", "auto": "自动更新", "hold": "暂不更新"}[mode]
        self.state.add_history("policy", name, "策略设为「%s」" % label)
        return label

    # ---------- 名称备注 ----------
    def notes(self):
        """返回 {容器名: {alias, memo, updated_at}}，只含实际有内容的条目。"""
        with self.state.lock:
            return {k: dict(v) for k, v in self.state.data["notes"].items()
                    if isinstance(v, dict) and (v.get("alias") or v.get("memo"))}

    def set_note(self, name, alias, memo):
        name = str(name or "").strip()
        if not name:
            raise ValueError("缺少容器名")
        alias = str(alias or "").strip()
        memo = str(memo or "").strip()
        if len(alias) > 40:
            raise ValueError("备注名最多 40 个字")
        if len(memo) > 200:
            raise ValueError("备注说明最多 200 个字")
        with self.state.lock:
            if not alias and not memo:
                removed = self.state.data["notes"].pop(name, None)
                action = "已恢复原名" if removed else "无备注可恢复"
            else:
                self.state.data["notes"][name] = {
                    "alias": alias, "memo": memo, "updated_at": now_str(),
                }
                action = "备注设为「%s」" % (alias or name)
            self.state.save()
        self.state.add_history("note", name, "%s%s" % (action, (" · " + memo[:60]) if memo else ""))
        return action

    def restore_name(self, name):
        """恢复原名：删除该容器备注，返回被移除的内容供界面撤销。"""
        with self.state.lock:
            removed = self.state.data["notes"].pop(str(name or ""), None)
            self.state.save()
        if removed:
            self.state.add_history("note", name, "已恢复原容器名（备注可在提示中撤销）")
        return dict(removed) if isinstance(removed, dict) else {}

    def restore_all_names(self):
        """批量恢复全部原名，返回被移除的备注以便撤销。"""
        with self.state.lock:
            removed = {k: dict(v) for k, v in self.state.data["notes"].items()
                       if isinstance(v, dict) and (v.get("alias") or v.get("memo"))}
            self.state.data["notes"] = {}
            self.state.save()
        if removed:
            self.state.add_history("note", "全部", "已恢复全部原容器名（共 %d 个，可在提示中撤销）" % len(removed))
        return removed

    def daily_tick(self):
        s = self.settings()
        try:
            parts = str(s.get("daily_time", "03:00")).split(":")
            hh, mm = int(parts[0]), int(parts[1])
        except Exception:
            hh, mm = 3, 0
        now = datetime.now(config.LOCAL_TZ)
        trigger = now.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        if now < trigger:
            return False
        if self.state.data.get("last_daily_date") == today_str():
            return False
        ok, _ = self.check_all(auto=True)
        return ok

    def startup_check(self):
        s = self.settings()
        if not s.get("check_on_start"):
            return
        now = datetime.now(config.LOCAL_TZ)
        try:
            parts = str(s.get("daily_time", "03:00")).split(":")
            trigger = now.replace(hour=int(parts[0]) % 24, minute=int(parts[1]) % 60,
                                  second=0, microsecond=0)
        except Exception:
            trigger = now.replace(hour=3, minute=0, second=0, microsecond=0)
        self.check_all(auto=(now >= trigger))
