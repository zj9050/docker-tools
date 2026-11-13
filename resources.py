"""Docker 管理增强：应用聚合 / 镜像 / 网络 / 卷 / 容器操作 / 备份与恢复 / Compose 扫描。"""
import io
import json
import os
import threading
import time
import zipfile
from datetime import datetime

import config
import events
from docker_api import DockerError, _split_ref


def _now():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _ts(v):
    try:
        return datetime.fromtimestamp(float(v)).astimezone(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _mb(v):
    try:
        return round(float(v) / 1048576, 1)
    except (TypeError, ValueError):
        return 0


class Resources:
    def __init__(self, docker, mgr):
        self.docker = docker
        self.mgr = mgr
        self.lock = threading.RLock()
        self._cache = {}

    def _cached(self, key, ttl, fn):
        now = time.time()
        with self.lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        try:
            value = fn()
        except DockerError:
            with self.lock:
                hit = self._cache.get(key)
                return hit[1] if hit else []
        with self.lock:
            self._cache[key] = (now, value)
        return value

    def invalidate(self, *keys):
        with self.lock:
            if keys:
                for k in keys:
                    self._cache.pop(k, None)
            else:
                self._cache.clear()

    # ---------- 镜像 ----------
    def images(self):
        def load():
            out = []
            for i in self.docker.list_images() or []:
                tags = [t for t in (i.get("RepoTags") or []) if t and not t.startswith("<none>")]
                out.append({
                    "id": (i.get("Id") or "").replace("sha256:", "")[:12],
                    "full_id": i.get("Id") or "",
                    "tags": tags,
                    "size": _mb(i.get("Size")),
                    "virtual_size": _mb(i.get("VirtualSize") or i.get("Size")),
                    "created": _ts(i.get("Created")),
                    "containers": int(i.get("Containers") or 0),
                    "dangling": not tags,
                    "digests": i.get("RepoDigests") or [],
                    "labels": i.get("Labels") or {},
                })
            out.sort(key=lambda x: (not x["dangling"], x["tags"][0] if x["tags"] else "zzz"))
            return out
        return self._cached("images", 8, load)

    # ---------- 网络 ----------
    def networks(self):
        def load():
            out = []
            for n in self.docker.list_networks() or []:
                ipam = (n.get("IPAM") or {}).get("Config") or []
                cons = []
                for cid, info in (n.get("Containers") or {}).items():
                    cons.append({"id": cid[:12], "name": info.get("Name", ""),
                                 "ipv4": (info.get("IPv4Address") or "").split("/")[0],
                                 "mac": info.get("MacAddress", "")})
                out.append({
                    "id": (n.get("Id") or "")[:12], "full_id": n.get("Id") or "",
                    "name": n.get("Name") or "", "driver": n.get("Driver") or "",
                    "scope": n.get("Scope") or "", "internal": bool(n.get("Internal")),
                    "subnets": [c.get("Subnet") for c in ipam if c.get("Subnet")],
                    "gateways": [c.get("Gateway") for c in ipam if c.get("Gateway")],
                    "containers": cons, "count": len(cons),
                    "labels": n.get("Labels") or {},
                })
            out.sort(key=lambda x: x["name"].lower())
            return out
        return self._cached("networks", 8, load)

    # ---------- 数据卷 ----------
    def volumes(self):
        def load():
            out = []
            for v in self.docker.list_volumes() or []:
                if not isinstance(v, dict):
                    continue
                out.append({
                    "name": v.get("Name") or "",
                    "driver": v.get("Driver") or "local",
                    "mountpoint": v.get("Mountpoint") or "",
                    "created": _ts(v.get("CreatedAt")) if not isinstance(v.get("CreatedAt"), str)
                    else str(v.get("CreatedAt"))[:16],
                    "labels": v.get("Labels") or {},
                    "options": v.get("Options") or {},
                })
            out.sort(key=lambda x: x["name"].lower())
            return out
        return self._cached("volumes", 20, load)

    # ---------- Compose 文件扫描 ----------
    def compose_files(self, host_root=None):
        """host_root 是宿主机绝对路径；容器内通过 /host 只读挂载访问。"""
        def load():
            chosen = (host_root or config.COMPOSE_ROOT).rstrip("/")
            root = os.path.join(config.HOST_ROOT, chosen.lstrip("/"))
            if not os.path.isdir(root):
                root = chosen if os.path.isdir(chosen) else ""
            out = []
            if not root:
                return out
            skip = {"node_modules", ".git", "data", "backups", "cache", "__pycache__",
                    ".cache", "logs", "log", "tmp"}
            names = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
                depth = os.path.relpath(base, root).count(os.sep)
                if depth > 3:
                    dirs[:] = []
                    continue
                for fn in files:
                    low = fn.lower()
                    if not (low in names or low.startswith("docker-compose.") or low.startswith("compose.")):
                        continue
                    if not low.endswith((".yml", ".yaml")):
                        continue
                    full = os.path.join(base, fn)
                    try:
                        stt = os.stat(full)
                        if stt.st_size > 2 * 1048576:
                            continue
                        rel = os.path.relpath(full, root)
                        host_path = os.path.join(chosen, rel)
                        out.append({"rel": rel, "path": host_path, "full": full,
                                    "size": stt.st_size, "mtime": _ts(stt.st_mtime)})
                    except OSError:
                        continue
                    if len(out) >= 300:
                        break
                if len(out) >= 300:
                    break
            out.sort(key=lambda x: x["path"])
            return out
        return self._cached("compose_files", 60, load)

    # ---------- 应用聚合 ----------
    def apps(self, containers, metrics, notes):
        groups = {}
        for c in containers:
            key = c.get("project") or ("独立:" + c["name"])
            g = groups.setdefault(key, {
                "key": key, "name": c.get("project") or c["name"],
                "project": c.get("project", ""), "members": [], "images": set(),
                "ports": [], "notes": [], "policies": {},
            })
            g["members"].append(c["name"])
            if c.get("image_ref"):
                g["images"].add(c["image_ref"])
            g["ports"].extend(c.get("ports") or [])
            if notes.get(c["name"], {}).get("alias"):
                g["notes"].append(notes[c["name"]]["alias"])
            g["policies"][c["name"]] = c.get("policy", "manual")
        out = []
        for g in groups.values():
            names = g["members"]
            running = sum(1 for n in names for c in containers if c["name"] == n and c["running"])
            total = len(names)
            health = "ok" if running == total else ("warn" if running else "error")
            mm = [metrics.get(n, {}) for n in names]
            out.append({
                "key": g["key"], "name": g["name"], "project": g["project"],
                "members": names, "count": total, "running": running,
                "health": health,
                "display": (g["notes"][0] if len(g["notes"]) == 1 else g["name"]),
                "aliases": {n: (notes.get(n, {}).get("alias") or n) for n in names},
                "images": sorted(g["images"]), "ports": g["ports"][:8],
                "cpu": round(sum(x.get("cpu", 0) for x in mm), 1),
                "mem": sum(x.get("mem", 0) for x in mm),
                "net": round(sum(x.get("net_rx", 0) + x.get("net_tx", 0) for x in mm), 1),
                "updates": sum(1 for c in containers if c["name"] in names
                               and c.get("has_update") and not c.get("excluded")),
                "policies": g["policies"],
            })
        out.sort(key=lambda a: ({"error": 0, "warn": 1, "ok": 2}[a["health"]],
                                -a["updates"], a["name"].lower()))
        return out

    # ---------- 容器详情 ----------
    def container_detail(self, name):
        cid, real = self.mgr._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        cfg = ins.get("Config") or {}
        hc = ins.get("HostConfig") or {}
        st = ins.get("State") or {}
        nets = (ins.get("NetworkSettings") or {}).get("Networks") or {}
        mounts = []
        for m in ins.get("Mounts") or []:
            mounts.append({"type": m.get("Type", ""), "source": m.get("Source", ""),
                           "destination": m.get("Destination", ""), "mode": m.get("Mode", ""),
                           "rw": bool(m.get("RW"))})
        return {
            "ok": True, "name": real, "id": cid[:12],
            "status": st.get("Status", ""), "running": bool(st.get("Running")),
            "started_at": str(st.get("StartedAt", ""))[:19].replace("T", " "),
            "finished_at": str(st.get("FinishedAt", ""))[:19].replace("T", " "),
            "restart_count": int(st.get("RestartCount") or 0),
            "health": (st.get("Health") or {}).get("Status", ""),
            "image": cfg.get("Image", ""), "image_id": (ins.get("Image") or "").replace("sha256:", "")[:12],
            "created": str(ins.get("Created", ""))[:19].replace("T", " "),
            "env": cfg.get("Env") or [], "cmd": cfg.get("Cmd") or [],
            "entrypoint": cfg.get("Entrypoint") or [],
            "working_dir": cfg.get("WorkingDir") or "", "user": cfg.get("User") or "",
            "labels": cfg.get("Labels") or {},
            "restart_policy": (hc.get("RestartPolicy") or {}).get("Name", ""),
            "network_mode": hc.get("NetworkMode") or "",
            "privileged": bool(hc.get("Privileged")),
            "memory_mb": round(float(hc.get("Memory") or 0) / 1048576),
            "cpu_cores": round(float(hc.get("NanoCpus") or 0) / 1e9, 2),
            "ports": [{"container": k, "host": ",".join(x.get("HostPort", "") for x in (v or []) if x)}
                      for k, v in ((hc.get("PortBindings") or {}).items() if isinstance(hc.get("PortBindings"), dict) else [])],
            "binds": hc.get("Binds") or [],
            "networks": [{"name": k, "ip": (v.get("IPAddress") or ""),
                          "aliases": v.get("Aliases") or [], "mac": v.get("MacAddress", "")}
                         for k, v in nets.items()],
            "mounts": mounts,
            "log_path": ins.get("LogPath", ""),
            "top": self.docker.top(cid) or {},
        }

    def logs(self, name, tail=200):
        cid, real = self.mgr._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        tty = bool((ins.get("Config") or {}).get("Tty"))
        text = self.docker.logs(cid, tail=int(tail), tty=tty)
        return {"ok": True, "name": real, "tail": int(tail), "lines": text[-200000:]}

    # ---------- 容器动作 ----------
    def _guard(self, name):
        if name in (config.SELF_NAME, "docker-update", self.mgr.self_hostname):
            raise DockerError(403, "不能对面板自身执行该操作（会影响管理界面）")

    def container_action(self, name, action):
        cid, real = self.mgr._resolve(name)
        self._guard(real)
        if action == "start":
            self.docker.start(cid)
        elif action == "stop":
            self.docker.stop(cid)
        elif action == "restart":
            self.docker.restart(cid)
        elif action == "pause":
            self.docker.pause(cid)
        elif action == "unpause":
            self.docker.unpause(cid)
        elif action == "kill":
            self.docker.kill(cid)
        elif action == "remove":
            self.docker.remove_container(cid, force=True)
            with self.mgr.state.lock:
                self.mgr.state.data["checks"].pop(real, None)
                self.mgr.state.save()
        else:
            raise ValueError("未知容器操作：%s" % action)
        self.invalidate("images", "networks", "volumes")
        self.mgr.state.add_history("container", real, "容器操作：%s" % action)
        return "%s：%s 完成" % (real, action)

    def app_action(self, key, action, containers):
        apps = self.apps(containers, {}, self.mgr.notes())
        target = next((a for a in apps if a["key"] == key), None)
        if not target:
            raise DockerError(404, "找不到应用：%s" % key)
        done, failed = 0, []
        for n in target["members"]:
            try:
                self.container_action(n, action)
                done += 1
            except Exception as exc:
                failed.append("%s(%s)" % (n, getattr(exc, "message", str(exc))[:40]))
        msg = "应用 %s：%s %d 个容器" % (target["name"], action, done)
        if failed:
            msg += "，失败 " + ", ".join(failed[:3])
        return msg

    def set_limits(self, name, cpu_cores, mem_mb):
        cid, real = self.mgr._resolve(name)
        self._guard(real)
        body = {}
        if cpu_cores is not None:
            body["NanoCpus"] = int(round(float(cpu_cores) * 1e9))
        if mem_mb is not None:
            mem = int(round(float(mem_mb) * 1048576))
            body["Memory"] = mem
            body["MemorySwap"] = mem * 2
        if not body:
            raise ValueError("请填写 CPU 核数或内存上限")
        self.docker.update_container(cid, body)
        self.mgr.state.add_history("limits", real, "资源限制：CPU %s 核 / 内存 %s MB"
                                   % (cpu_cores or "不变", mem_mb or "不变"))
        return "%s 资源限制已更新（在线生效，无需重建）" % real

    def create_container(self, form):
        name = str(form.get("name") or "").strip()
        image = str(form.get("image") or "").strip()
        if not name or not image:
            raise ValueError("容器名和镜像不能为空")
        repo, tag, _ = _split_ref(image)
        image_ref = image if ":" in image.rsplit("/", 1)[-1] else "%s:latest" % image
        try:
            self.docker.inspect_image(image_ref)
        except DockerError:
            self.docker.pull_ref(image_ref, self.mgr._registry_auth(repo),
                                 timeout=int(self.mgr.settings().get("check_timeout", 300)))
        ports = []
        exposed = {}
        pb = {}
        for p in (form.get("ports") or []):
            h, c, proto = str(p.get("host") or "").strip(), str(p.get("container") or "").strip(), \
                str(p.get("proto") or "tcp").strip()
            if not h or not c:
                continue
            key = "%s/%s" % (c, proto)
            exposed[key] = {}
            pb[key] = [{"HostPort": h}]
            ports.append(key)
        binds = [str(b).strip() for b in (form.get("volumes") or []) if str(b).strip() and ":" in str(b)]
        env = [str(e).strip() for e in (form.get("env") or []) if str(e).strip() and "=" in str(e)]
        hc = {
            "Binds": binds, "PortBindings": pb,
            "RestartPolicy": {"Name": str(form.get("restart") or "unless-stopped")},
            "NetworkMode": str(form.get("network") or "bridge"),
        }
        if form.get("mem_mb"):
            mem = int(float(form["mem_mb"]) * 1048576)
            hc["Memory"] = mem
            hc["MemorySwap"] = mem * 2
        if form.get("cpu_cores"):
            hc["NanoCpus"] = int(float(form["cpu_cores"]) * 1e9)
        body = {"Image": image_ref, "Env": env or None, "ExposedPorts": exposed, "HostConfig": hc}
        cmd = str(form.get("command") or "").strip()
        if cmd:
            body["Cmd"] = cmd.split()
        body = {k: v for k, v in body.items() if v is not None}
        created = self.docker.create_container(name, body) or {}
        cid = created.get("Id", "")
        if not cid:
            raise DockerError(500, "创建容器失败")
        if form.get("start", True):
            self.docker.start(cid)
        self.invalidate("images", "networks", "volumes")
        self.mgr.state.add_history("container", name, "新建容器：%s" % image_ref)
        return "容器 %s 已创建%s" % (name, "并启动" if form.get("start", True) else "（未启动）")

    # ---------- 镜像 / 网络 / 卷动作 ----------
    def image_action(self, action, body):
        if action == "pull":
            ref = str(body.get("ref") or "").strip()
            if not ref:
                raise ValueError("请填写镜像地址")
            repo, _, _ = _split_ref(ref)
            self.docker.pull_ref(ref, self.mgr._registry_auth(repo),
                                 timeout=int(self.mgr.settings().get("check_timeout", 300)))
            self.invalidate("images")
            return "镜像 %s 拉取完成" % ref
        if action == "remove":
            ids = [str(x) for x in (body.get("ids") or [])]
            if not ids:
                raise ValueError("未选择镜像")
            removed, failed = 0, []
            for i in ids:
                try:
                    self.docker.remove_image(i, force=bool(body.get("force")))
                    removed += 1
                except DockerError as exc:
                    failed.append(exc.message[:50])
            self.invalidate("images")
            return "已删除 %d 个镜像%s" % (removed, ("，失败：" + "; ".join(failed[:2])) if failed else "")
        if action == "prune":
            r = self.docker.prune_images(dangling=True) or {}
            self.invalidate("images")
            return "已清理悬空镜像，释放 %.1f MB" % _mb((r.get("SpaceReclaimed") or 0))
        if action == "remove-unused":
            # v6.0-r14：删除所有「没有任何容器引用」的镜像（含悬空镜像）；
            # 正在被容器使用的镜像一律跳过，避免把运行中的服务搞坏。
            imgs = self.images() or []
            unused = [x for x in imgs if not int(x.get("containers") or 0)]
            removed, failed = 0, []
            for x in unused:
                ref = x.get("full_id") or x.get("id")
                if not ref:
                    continue
                try:
                    self.docker.remove_image(ref, force=True)
                    removed += 1
                except Exception as exc:
                    failed.append("%s：%s" % (str(x.get("tags") or x.get("id") or "")[:28], str(exc)[:60]))
            self.invalidate("images")
            msg = "已删除 %d 个未使用镜像（跳过 %d 个使用中镜像）" % (removed, len(imgs) - len(unused))
            if failed:
                msg += "，失败 %d 个：%s" % (len(failed), "; ".join(failed[:2]))
            return msg
        if action == "tag":
            i = str(body.get("id") or "")
            repo = str(body.get("repo") or "").strip()
            tag = str(body.get("tag") or "latest").strip()
            if not i or not repo:
                raise ValueError("镜像和仓库不能为空")
            self.docker.tag_image(i, repo, tag)
            self.invalidate("images")
            return "已打标签 %s:%s" % (repo, tag)
        raise ValueError("未知镜像操作：%s" % action)

    def export_images(self, names):
        if not names:
            raise ValueError("未选择镜像")
        return self.docker.export_images(names)

    def network_action(self, action, body):
        if action == "create":
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("网络名不能为空")
            cfg = {"Name": name, "Driver": str(body.get("driver") or "bridge"),
                   "Labels": {"dockerupdate.created": "true"}}
            subnet = str(body.get("subnet") or "").strip()
            if subnet:
                cfg["IPAM"] = {"Driver": "default", "Config": [{"Subnet": subnet}]}
            if body.get("internal"):
                cfg["Internal"] = True
            self.docker.create_network(cfg)
            self.invalidate("networks")
            return "网络 %s 已创建" % name
        if action == "remove":
            nid = str(body.get("id") or "")
            if not nid:
                raise ValueError("未选择网络")
            self.docker.remove_network(nid)
            self.invalidate("networks")
            return "网络已删除"
        if action in ("connect", "disconnect"):
            nid, cname = str(body.get("id") or ""), str(body.get("container") or "")
            if not nid or not cname:
                raise ValueError("缺少网络或容器")
            cid, _ = self.mgr._resolve(cname)
            if action == "connect":
                self.docker.connect_network(nid, cid)
            else:
                self.docker.disconnect_network(nid, cid, force=True)
            self.invalidate("networks")
            return "容器 %s 已%s网络" % (cname, "加入" if action == "connect" else "退出")
        raise ValueError("未知网络操作：%s" % action)

    def volume_action(self, action, body):
        if action == "create":
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("卷名不能为空")
            self.docker.create_volume(name)
            self.invalidate("volumes")
            return "数据卷 %s 已创建" % name
        if action == "remove":
            name = str(body.get("name") or "")
            if not name:
                raise ValueError("未选择数据卷")
            self.docker.remove_volume(name, force=bool(body.get("force")))
            self.invalidate("volumes")
            return "数据卷 %s 已删除" % name
        if action == "prune":
            r = self.docker.prune_volumes() or {}
            self.invalidate("volumes")
            return "已清理未使用数据卷，释放 %.1f MB" % _mb((r.get("SpaceReclaimed") or 0))
        raise ValueError("未知数据卷操作：%s" % action)

    # ---------- 备份与恢复 ----------
    def export_backup(self):
        buf = io.BytesIO()
        files = self.compose_files()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            manifest = {
                "app": "DockerUpdate", "version": config.APP_VERSION,
                "time": _now(), "compose_root": config.COMPOSE_ROOT,
                "compose_files": len(files),
                "contains": ["data/state.json", "data/alerts.json", "data/events.json",
                             "data/registry-auth.json", "data/metrics.json", "data/pro/*", "data/desktop.json",
                             "data/store/accel.json", "data/backups/*", "compose/*",
                             "data/pro/container-mounts.json"],
                "note": "不含 auth.json 与 secret.key（避免覆盖当前登录密码）；data/pro/ 含计划任务、API Key、审计与只读查看者，data/registry-auth.json 含私有仓库凭据，请妥善保管；数据卷内容不会打包。",
            }
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            try:
                events.emit("panel_backup", "面板备份已导出",
                            "已导出面板数据备份（含设置、策略、备注、检测结果、告警配置、版本快照与 Compose 文件）。",
                            "info", "面板", dedupe=False)
            except Exception:
                pass
            z.writestr("README.txt",
                       "DockerUpdate v3.0 面板备份\n导出时间：%s\n\n"
                       "包含：面板设置/策略/名称备注/检测结果/告警配置/趋势历史/版本备份/系统联动数据，以及扫描到的 Compose 配置文件。\n"
                       "恢复：在「备份与恢复」页上传本 zip，只恢复面板数据；Compose 文件请按需手动放回对应目录。\n"
                       "安全：zip 内含容器环境变量与私有仓库凭据，请妥善保管。\n" % _now())
            for rel in ("state.json", "alerts.json", "events.json", "registry-auth.json",
                        "metrics.json", "desktop.json"):
                p = os.path.join(config.DATA_DIR, rel)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        z.writestr("data/" + rel, f.read())
            pro_dir = os.path.join(config.DATA_DIR, "pro")
            if os.path.isdir(pro_dir):
                for base, _, fns in os.walk(pro_dir):
                    for fn in fns:
                        if not fn.endswith(".json"):
                            continue
                        full = os.path.join(base, fn)
                        arc = "data/pro/" + os.path.relpath(full, pro_dir).replace(os.sep, "/")
                        with open(full, "rb") as f:
                            z.writestr(arc, f.read())
            store_dir = os.path.join(config.DATA_DIR, "store")
            if os.path.isdir(store_dir):
                for base, _, fns in os.walk(store_dir):
                    for fn in fns:
                        if not fn.endswith((".json", ".md", ".yml", ".yaml")):
                            continue
                        full = os.path.join(base, fn)
                        arc = "data/store/" + os.path.relpath(full, store_dir).replace(os.sep, "/")
                        with open(full, "rb") as f:
                            z.writestr(arc, f.read())
            if os.path.isdir(config.BACKUP_DIR):
                for base, _, fns in os.walk(config.BACKUP_DIR):
                    for fn in fns:
                        if not fn.endswith(".json"):
                            continue
                        full = os.path.join(base, fn)
                        arc = "data/backups/" + os.path.relpath(full, config.BACKUP_DIR).replace(os.sep, "/")
                        with open(full, "rb") as f:
                            z.writestr(arc, f.read())
            for f in files:
                try:
                    with open(f["full"], "rb") as fp:
                        z.writestr("compose/" + f["rel"].replace(os.sep, "/"), fp.read())
                except OSError:
                    pass
        return buf.getvalue()

    def import_backup(self, raw, restore_compose=False, compose_dir=""):
        if len(raw) > 32 * 1048576:
            raise ValueError("备份文件不能超过 32 MB")
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise ValueError("不是有效的 zip 备份文件")
        names = set(z.namelist())
        if "data/state.json" not in names:
            raise ValueError("备份里缺少 data/state.json，无法恢复面板数据")
        state = json.loads(z.read("data/state.json").decode("utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state.json 格式不正确")
        with self.mgr.state.lock:
            for k in ("settings", "policies", "checks", "notes", "history",
                      "last_daily_date", "last_full_check"):
                if k in state and k != "settings":
                    self.mgr.state.data[k] = state[k]
            if isinstance(state.get("settings"), dict):
                self.mgr.state.data["settings"].update(
                    {k: v for k, v in state["settings"].items() if k in config.SETTING_KEYS})
            self.mgr.state.save()
        restored = ["面板设置", "策略", "名称备注", "检测结果", "日志"]
        for rel, target in (("data/alerts.json", config.ALERTS_FILE),
                            ("data/events.json", events.EVENTS_FILE),
                            ("data/registry-auth.json", config.REGISTRY_AUTH_FILE),
                            ("data/metrics.json", config.METRICS_FILE)):
            if rel in names:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(z.read(rel))
        events.invalidate()
        try:
            events.emit("panel_backup", "面板数据已恢复",
                        "已上传并恢复面板数据备份（设置、策略、备注、告警与事件开关等），事件配置已立即生效。",
                        "mid", "面板", dedupe=False)
        except Exception:
            pass
        # 版本备份快照
        bdir = config.BACKUP_DIR
        os.makedirs(bdir, exist_ok=True)
        n_bk = 0
        for arc in names:
            if not (arc.startswith("data/backups/") and arc.endswith(".json")):
                continue
            rel = arc[len("data/backups/"):]
            if ".." in rel or rel.startswith("/"):
                continue
            dest = os.path.join(bdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(z.read(arc))
            n_bk += 1
        # v3.0 系统联动数据（加速源配置 + pro/ 下的计划任务、凭证、审计、API Key、权限等）
        n_store = 0
        sdir = os.path.join(config.DATA_DIR, "store")
        for arc in names:
            if not (arc.startswith("data/store/") and
                    arc.endswith((".json", ".md", ".yml", ".yaml"))):
                continue
            rel = arc[len("data/store/"):]
            if ".." in rel or rel.startswith("/"):
                continue
            dest = os.path.join(sdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(z.read(arc))
            n_store += 1
        for rel, target in (("data/desktop.json", os.path.join(config.DATA_DIR, "desktop.json")),):
            if rel in names:
                with open(target, "wb") as f:
                    f.write(z.read(rel))
        n_pro = 0
        pro_dir = os.path.join(config.DATA_DIR, "pro")
        for arc in names:
            if not (arc.startswith("data/pro/") and arc.endswith(".json")):
                continue
            rel = arc[len("data/pro/"):]
            if ".." in rel or rel.startswith("/"):
                continue
            dest = os.path.join(pro_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(z.read(arc))
            n_pro += 1
        if n_store or n_pro:
            restored.append("系统联动数据 %d 个文件" % (n_store + n_pro))
        compose_n = sum(1 for n in names if n.startswith("compose/") and n.endswith((".yml", ".yaml")))
        wrote = 0
        if restore_compose and compose_dir:
            # v3.3：用户可在「恢复面板数据」里选择 Compose 文件写回的目录。
            # 只允许落在可写根（/vol1/1000/docker）下，防止任意路径写入。
            cd = str(compose_dir).strip().rstrip("/")
            writable = config.STORE_HOST_ROOT.rstrip("/")
            if not cd.startswith("/") or ".." in cd.split("/"):
                raise ValueError("Compose 恢复目录必须是合法的绝对路径")
            if not (cd == writable or cd.startswith(writable + "/")):
                raise ValueError("Compose 恢复目录必须在可写根 %s 下" % writable)
            local = os.path.join(config.STORE_ROOT, cd[len(writable):].lstrip("/"))
            os.makedirs(local, exist_ok=True)
            for arc in names:
                if not (arc.startswith("compose/") and arc.endswith((".yml", ".yaml"))):
                    continue
                rel = arc[len("compose/"):]
                if ".." in rel.split("/") or rel.startswith("/") or chr(92) in rel:
                    continue
                dest = os.path.join(local, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(z.read(arc))
                wrote += 1
        self.invalidate()
        self.mgr.state.add_history("backup", "面板", "从备份恢复面板数据（%d 份版本快照，%d 个 Compose 文件跳过写回）"
                                   % (n_bk, compose_n))
        tail = ("%d 个 Compose 文件已写回 %s" % (wrote, compose_dir)) if wrote else \
               ("%d 个 Compose 文件仅包含在下载包中，未写回宿主机" % compose_n)
        return ("已恢复：%s；版本快照 %d 份。%s"
                % ("、".join(restored), n_bk, tail))
