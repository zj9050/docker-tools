# -*- coding: utf-8 -*-
"""DockerUpdate v6.0-r24 · 容器目录映射（飞牛文件夹 → 容器内自定义路径）。

用户要的是「把飞牛上某个文件夹软链接进容器里指定路径」，且两端路径都可自定义。
面板对宿主机根目录是只读挂载，不会去 NAS 上真的建 ln -s 文件；这里用 Docker
的 bind mount 实现等价效果：宿主机目录直接挂到容器内任意路径，容器重建后依然
生效，而且不要求镜像里有 shell。Docker 不支持热改挂载，所以流程拆成两步：

    保存配置（只写 data/pro/container-mounts.json，完全不碰容器）
    → 查看重建计划（列出将新增 / 移除的挂载与风险）
    → 应用并重建（版本备份 → 可选数据预迁移 → 复用 manager._recreate 重建）

manager._recreate 自带「失败把旧容器改回原名并重启」的回滚；build_create_body
保证端口、环境变量、网络、标签、重启策略、资源限额等原配置一个不丢。

安全边界：
  · 宿主机路径必须绝对路径、无 ..、第一段在允许的存储根里，且不在系统目录黑名单；
  · 容器内路径禁止 /、/etc、/proc、/sys、/dev、docker.sock 等，且不能含 ':';
  · 面板自身容器禁止修改（会把自己赖以运行的 socket / 配置挂载弄丢）；
  · 预迁移只写 config.STORE_HOST_ROOT 对应的可写根，其它目录如实报错要求手动复制；
  · 每容器最多 32 条，单条预迁移 tar 上限 200 MB，超限明确拒绝而不是悄悄截断。
"""
import hashlib
import io
import json
import os
import re
import tarfile
import threading
import time
import urllib.parse

import config
import events
import manager
from docker_api import DockerError

MOUNTS_FILE = os.path.join(config.DATA_DIR, "pro", "container-mounts.json")
MAX_ROWS = 32
MAX_PRECOPY_BYTES = 200 * 1024 * 1024
MAX_DEEP_CHECK = 8

_lock = threading.RLock()

# 飞牛存储池 / 常见 NAS 共享根；系统根目录一律不在其中
_ALLOWED_FIRST = tuple(
    ["vol%d" % i for i in range(1, 10)]
    + ["volume%d" % i for i in range(1, 10)]
    + ["mnt", "media", "home", "opt", "srv", "data", "store", "share", "nas"]
)
_HOST_DENY = ("/etc", "/proc", "/sys", "/dev", "/boot", "/root", "/bin", "/sbin",
              "/usr", "/lib", "/lib64", "/var/lib/docker", "/var/run/docker.sock",
              "/run/docker.sock", "/var/run", "/run")
# 容器内路径：只禁系统根级目录本身与 docker.sock。
# /etc、/var/run 的**子目录**是常见配置挂载点（如 /etc/nginx/conf.d），必须允许，
# 否则「容器内地址可自定义」这条要求就名存实亡。
_CPATH_DENY_EXACT = ("/", "/etc", "/proc", "/sys", "/dev", "/boot", "/usr",
                     "/bin", "/sbin", "/lib", "/lib64", "/var/lib/docker",
                     "/var/run/docker.sock", "/run/docker.sock")
_CPATH_DENY_PREFIX = ("/proc/", "/sys/", "/dev/", "/boot/", "/usr/", "/bin/",
                      "/sbin/", "/lib/", "/lib64/", "/var/lib/docker/")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load():
    try:
        with open(MOUNTS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(doc):
    os.makedirs(os.path.dirname(MOUNTS_FILE), exist_ok=True)
    # 并发写入不能共用一个 .tmp，否则两个保存会互相覆盖（v5.1 修过的同类问题）
    tmp = "%s.%s.%d.tmp" % (MOUNTS_FILE, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MOUNTS_FILE)


def _emit(emit, stage, pct, message):
    if not emit:
        return
    try:
        emit(stage=stage, pct=pct, message=message)
    except TypeError:
        try:
            emit(message)
        except Exception:
            pass
    except Exception:
        pass


def validate_name(name):
    n = str(name or "").strip().lstrip("/")
    if not n or len(n) > 128 or not re.match(r"^[a-zA-Z0-9_.-]+$", n):
        raise ValueError("容器名不合法：%s" % (name or "(空)"))
    # 面板自身改挂载会导致 docker.sock / 配置挂载丢失，面板直接起不来
    if n == config.SELF_NAME or n.startswith("docker-update"):
        raise ValueError("不能给面板自身容器「%s」修改挂载，否则面板将无法启动" % n)
    return n


def validate_host(path):
    """校验「飞牛上的文件夹」宿主机绝对路径。"""
    p = str(path or "").strip().rstrip("/") or "/"
    if not p.startswith("/"):
        raise ValueError("飞牛目录必须是绝对路径（以 / 开头）：%s" % p)
    if ".." in p.split("/") or "\n" in p or "\r" in p or ":" in p:
        raise ValueError("飞牛目录不合法（不能含 .. / 冒号 / 换行）：%s" % p)
    if p == "/":
        raise ValueError("不能把整个根目录 / 挂进容器")
    low = p.lower()
    for bad in _HOST_DENY:
        if low == bad or low.startswith(bad + "/"):
            raise ValueError("系统目录不能挂进容器：%s" % p)
    # 面板自己的可写根无条件放行：STORE_HOST_ROOT 可由环境变量自定义，
    # 第一段未必落在下面的存储池白名单里（测试环境就是 /tmp/... 下）。
    root = config.STORE_HOST_ROOT.rstrip("/")
    if p == root or p.startswith(root + "/"):
        return p
    first = p.split("/")[1] if len(p.split("/")) > 1 else ""
    if first not in _ALLOWED_FIRST:
        raise ValueError("飞牛目录必须位于存储池或共享目录下（如 %s/…、/mnt/…），"
                         "当前第一段「%s」不在允许范围：%s"
                         % (root, first or "(空)", p))
    return p


def validate_cpath(path):
    """校验容器内目标路径，两端都可自定义，但不许覆盖系统关键路径。"""
    p = str(path or "").strip().rstrip("/") or "/"
    if not p.startswith("/"):
        raise ValueError("容器内路径必须是绝对路径（以 / 开头）：%s" % path)
    if ".." in p.split("/") or ":" in p or "\n" in p or "\r" in p or "\t" in p:
        raise ValueError("容器内路径不合法（不能含 .. / 冒号 / 空白字符）：%s" % path)
    if p == "/":
        raise ValueError("不能挂载到容器根目录 /")
    low = p.lower()
    if low in _CPATH_DENY_EXACT or any(low.startswith(b) for b in _CPATH_DENY_PREFIX):
        raise ValueError("容器内路径属于系统关键目录，不能挂载：%s" % p)
    return p


def _bind_of(m):
    return "%s:%s:%s" % (m["host"], m["container"], m.get("mode") or "rw")


def _parse_bind(b):
    parts = str(b or "").split(":")
    if len(parts) < 2:
        return None
    return {"host": parts[0], "container": parts[1],
            "mode": (parts[2] if len(parts) > 2 else "rw").split(",")[0] or "rw"}


def _row_id(host, cpath):
    return hashlib.sha1(("%s\x00%s" % (host, cpath)).encode("utf-8")).hexdigest()[:12]


def normalize(rows, name=""):
    """整表规范化：去空行、校验、按容器内路径去重、生成稳定 id。"""
    if not isinstance(rows, list):
        raise ValueError("映射列表格式不正确")
    if len(rows) > MAX_ROWS:
        raise ValueError("每个容器最多 %d 条映射（当前 %d 条）" % (MAX_ROWS, len(rows)))
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        host = str(r.get("host") or "").strip()
        cpath = str(r.get("container") or "").strip()
        if not host and not cpath:
            continue
        host = validate_host(host)
        cpath = validate_cpath(cpath)
        if cpath in seen:
            raise ValueError("容器内路径重复：%s（同一路径只能挂一个飞牛目录）" % cpath)
        seen.add(cpath)
        mode = str(r.get("mode") or "rw").strip().lower()
        if mode not in ("rw", "ro"):
            raise ValueError("读写模式只能是 rw 或 ro：%s" % mode)
        out.append({
            "id": r.get("id") or _row_id(host, cpath),
            "host": host,
            "container": cpath,
            "mode": mode,
            "enabled": bool(r.get("enabled", True)),
            "note": str(r.get("note") or "")[:120],
        })
    return out


def _host_fs(host):
    """宿主机路径 → 面板容器内只读路径；未挂 /host 时返回 None（跳过存在性判断）。"""
    if not os.path.isdir(config.HOST_ROOT):
        return None
    return os.path.join(config.HOST_ROOT, host.lstrip("/"))


def _writable_local(host):
    """宿主机路径 → 面板容器内的可写路径；不在可写根下就明确拒绝。"""
    root = config.STORE_HOST_ROOT.rstrip("/")
    if host != root and not host.startswith(root + "/"):
        raise ValueError("飞牛目录 %s 不在面板可写根 %s 下，无法自动预迁移；"
                         "请先在飞牛里手动把容器内原目录内容复制过去，再取消勾选预迁移重试"
                         % (host, root))
    return os.path.join(config.STORE_ROOT, host[len(root):].lstrip("/"))


def list_all(docker):
    doc = _load()
    containers = []
    try:
        for c in (docker.list_containers(all_=True) or []):
            n = (c.get("Names") or [""])[0].lstrip("/")
            if n:
                containers.append({"name": n, "image": c.get("Image", ""),
                                   "running": str(c.get("State") or "").lower() == "running"})
    except Exception:
        containers = []
    items = []
    for n, rec in (doc.get("containers") or {}).items():
        rows = rec.get("mappings") or []
        applied = rec.get("applied") or {}
        sig = sorted(_bind_of(r) for r in rows if r.get("enabled"))
        items.append({"name": n, "count": len(rows),
                      "enabled": sum(1 for r in rows if r.get("enabled")),
                      "pending": sig != sorted(applied.get("binds") or []),
                      "applied_at": applied.get("time") or "",
                      "updated": rec.get("updated") or "",
                      "inherited": bool(rec.get("inherited"))})
    tpl = _get_template(doc)
    pol = _get_policy(doc)
    known = _get_known(doc)
    inherited = {n for n, rec in (doc.get("containers") or {}).items()
                 if rec.get("inherited")}
    for c in containers:
        c["inherited"] = c["name"] in inherited
        c["auto_inherited"] = bool((known.get(c["name"]) or {}).get("auto_inherited"))
    return {"ok": True, "containers": containers, "items": items,
            "store_root": config.STORE_HOST_ROOT,
            "max_rows": MAX_ROWS, "version": config.APP_VERSION,
            "template": {"mappings": tpl, "count": len(tpl)},
            "policy": pol, "known_count": len(known),
            "inherited_count": len(inherited)}


def _doc_container(doc, name):
    return (doc.get("containers") or {}).get(name) or {}


def save(body):
    name = validate_name(body.get("name"))
    rows = normalize(body.get("mappings") or [], name)
    with _lock:
        doc = _load()
        doc.setdefault("containers", {})
        old = doc["containers"].get(name) or {}
        doc["containers"][name] = {
            "mappings": rows,
            "updated": _now(),
            "applied": old.get("applied") or {},
            # 用户亲手改过就不再算「模板继承」，避免后续模板变更把它覆盖掉
            "inherited": False,
        }
        _save(doc)
    pending = [r for r in rows if r["enabled"]]
    return {"ok": True, "name": name, "mappings": rows,
            "message": "已保存 %d 条映射配置（尚未应用到容器），点「查看重建计划」核对后再应用"
                       % len(pending),
            "applied": old.get("applied") or {}}


def _inspect(docker, name):
    try:
        ins = docker.inspect_container(name) or {}
    except DockerError as exc:
        raise ValueError("找不到容器「%s」：%s" % (name, exc.message))
    if not ins.get("Id"):
        raise ValueError("找不到容器「%s」" % name)
    return ins


def _current_binds(ins):
    hc = ins.get("HostConfig") or {}
    binds = [str(x) for x in (hc.get("Binds") or []) if str(x).strip()]
    if not binds:
        for m in (ins.get("Mounts") or []):
            if str(m.get("Type") or "").lower() == "bind":
                src, dst = m.get("Source"), m.get("Destination")
                if src and dst:
                    binds.append("%s:%s:%s" % (src, dst, "ro" if m.get("RW") is False else "rw"))
    return binds


def _dir_probe(docker, cid, cpath):
    """检测容器内目标目录是否已有数据；无法判断时返回 has_data=None。"""
    q = urllib.parse.quote(cpath, safe="/")
    try:
        _, _, raw = docker._request_raw(
            "GET", "/containers/%s/archive?path=%s" % (urllib.parse.quote(cid, safe=""), q),
            timeout=15)
    except Exception:
        return {"has_data": None, "reason": "无法读取容器内该目录（可能不存在或容器已停止）"}
    if len(raw) > MAX_PRECOPY_BYTES:
        return {"has_data": None, "reason": "目录内容超过 200 MB，已跳过自动检测，请自行确认"}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            names = [m.name for m in tf.getmembers()]
    except Exception:
        return {"has_data": None, "reason": "目录归档无法解析，已跳过自动检测"}
    base = cpath.strip("/").split("/")[-1]
    data = [n for n in names
            if n.rstrip("/") not in (base, base + "/") and n.rstrip("/") != ""]
    return {"has_data": bool(data), "entries": len(data), "bytes": len(raw), "reason": ""}


def status(docker, name, deep=False):
    name = validate_name(name)
    doc = _load()
    rec = _doc_container(doc, name)
    rows = rec.get("mappings") or []
    applied = rec.get("applied") or {}
    ins = _inspect(docker, name)
    binds = _current_binds(ins)
    parsed = {(_parse_bind(b) or {}).get("container"): b for b in binds if _parse_bind(b)}
    out, warnings = [], []
    for r in rows:
        want = _bind_of(r)
        cur = parsed.get(r["container"])
        if not r["enabled"]:
            state, label = "disabled", "已停用"
        elif cur == want:
            state, label = "applied", "已生效"
        elif cur:
            state, label = "conflict", "路径已被其它目录占用"
            warnings.append("容器内 %s 当前挂载的是 %s，与本条配置冲突" % (r["container"], cur))
        else:
            state, label = "pending", "待应用"
        fs = _host_fs(r["host"])
        if fs is not None and not os.path.isdir(fs):
            warnings.append("飞牛目录 %s 在宿主机上不存在（若位于可写根下，应用时会自动创建）" % r["host"])
        row = dict(r)
        row.update({"state": state, "state_label": label, "current_bind": cur or ""})
        if deep and r["enabled"] and state == "pending" and len(out) < MAX_DEEP_CHECK:
            row["dir_probe"] = _dir_probe(docker, ins["Id"], r["container"])
        out.append(row)
    cfg = ins.get("Config") or {}
    return {"ok": True, "name": name,
            "image": cfg.get("Image") or ins.get("Image") or "",
            "running": bool((ins.get("State") or {}).get("Running")),
            "container_id": ins.get("Id", "")[:12],
            "binds": binds, "mappings": out,
            "applied": applied, "updated": rec.get("updated") or "",
            "pending": sum(1 for r in out if r["state"] == "pending"),
            "warnings": warnings, "store_root": config.STORE_HOST_ROOT}


def plan(docker, mgr, name, deep=True):
    """生成重建计划，只算不写；前后端都能看到将新增 / 移除哪些挂载。"""
    st = status(docker, name, deep=deep)
    doc = _load()
    rec = _doc_container(doc, name)
    ins = _inspect(docker, name)
    applied = rec.get("applied") or {}
    # 容器在飞牛 GUI / Compose 里被删掉再建（同名不同 ID）后，旧 applied 记录
    # 指的是上一个容器，不能拿它的 binds 去判断「哪些挂载是面板加的」。
    if not _applied_valid(ins, rec):
        applied = {}
    current = _current_binds(ins)
    managed = set(str(x) for x in (applied.get("binds") or []))
    keep = [b for b in current if b not in managed]
    target = [_bind_of(r) for r in (st["mappings"] or []) if r.get("enabled")]

    keep_paths = {(_parse_bind(b) or {}).get("container") for b in keep}
    conflicts = [b for b in target if (_parse_bind(b) or {}).get("container") in keep_paths]
    if conflicts:
        raise ValueError("容器内路径已被容器原有挂载占用：%s；请改用同一目录或先删除原有挂载"
                         % "、".join((_parse_bind(b) or {}).get("container", "") for b in conflicts))

    warnings = list(st["warnings"])
    if st["running"]:
        warnings.insert(0, "容器正在运行，应用并重建会短暂中断服务（通常几秒）")
    warnings.append("重建会保留镜像、端口、环境变量、网络、标签、重启策略与资源限额；"
                    "应用前会先生成版本备份，失败会自动把旧容器还原")
    precopy = []
    for r in st["mappings"]:
        p = r.get("dir_probe") or {}
        if r.get("enabled") and r.get("state") == "pending" and p.get("has_data"):
            precopy.append({"host": r["host"], "container": r["container"],
                            "entries": p.get("entries", 0), "bytes": p.get("bytes", 0)})
            warnings.append("容器内 %s 原本有 %s 个文件，挂载后会被遮住；"
                            "建议勾选「先把原数据复制到飞牛目录」"
                            % (r["container"], p.get("entries", "若干")))
    if precopy:
        warnings.append("预迁移只能写入可写根 %s 下的目录；其它目录请先在飞牛里手动复制"
                        % config.STORE_HOST_ROOT)
    return {"ok": True, "name": name, "image": st["image"], "running": st["running"],
            "container_id": st["container_id"],
            "current_binds": current, "target_binds": target + [b for b in keep],
            "managed_binds": sorted(managed),
            "diff": {
                "add": [b for b in target if b not in current],
                "remove": [b for b in current if b in managed and b not in target],
                "keep": keep,
            },
            "has_changes": sorted(target + [b for b in keep]) != sorted(current),
            "precopy": precopy, "warnings": warnings,
            "backup_required": True}


def precopy_check(docker, name, mappings=None):
    name = validate_name(name)
    rows = normalize(mappings, name) if mappings is not None else (
        _doc_container(_load(), name).get("mappings") or [])
    ins = _inspect(docker, name)
    out = []
    for r in rows[:MAX_DEEP_CHECK]:
        p = _dir_probe(docker, ins["Id"], r["container"])
        out.append({"host": r["host"], "container": r["container"],
                    "enabled": r["enabled"], "probe": p})
    return {"ok": True, "name": name, "rows": out,
            "message": "已检测 %d 条映射的容器内目标目录" % len(out)}


def _precopy_one(docker, cid, r):
    dest = _writable_local(r["host"])
    q = urllib.parse.quote(r["container"], safe="/")
    _, _, raw = docker._request_raw(
        "GET", "/containers/%s/archive?path=%s" % (urllib.parse.quote(cid, safe=""), q),
        timeout=120)
    if len(raw) > MAX_PRECOPY_BYTES:
        raise ValueError("容器内 %s 的数据超过 200 MB，已取消预迁移，请手动复制后再应用"
                         % r["container"])
    os.makedirs(dest, exist_ok=True)
    base = r["container"].strip("/").split("/")[-1]
    n = 0
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        for m in tf.getmembers():
            parts = m.name.split("/")
            # 剥掉归档最外层的目录名，把内容直接放进飞牛目录
            rel = "/".join(parts[1:]) if parts and parts[0] == base else m.name
            rel = rel.lstrip("/")
            if not rel or rel in (".", "..") or ".." in rel.split("/"):
                continue
            if m.issym() or m.islnk() or m.isdev():
                continue
            target = os.path.join(dest, rel)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            f = tf.extractfile(m)
            if f is None:
                continue
            with open(target, "wb") as w:
                w.write(f.read())
            n += 1
    return n


def apply(docker, mgr, body, emit=None):
    """应用映射：备份 → 可选预迁移 → 复用 manager._recreate 重建 → 核对 → 通知。"""
    name = validate_name(body.get("name"))
    if not body.get("confirm"):
        raise ValueError("请先在重建计划弹窗中确认，再执行应用并重建")
    _emit(emit, "校验路径与冲突", 5, "正在校验映射与容器状态")
    p = plan(docker, mgr, name, deep=True)
    if not p["has_changes"]:
        return {"ok": True, "name": name, "message": "映射配置与容器当前挂载一致，无需重建",
                "applied": len(p["target_binds"])}
    if p["precopy"] and not body.get("precopy") and not body.get("ack_nonempty"):
        raise ValueError("检测到 %d 个容器内目标路径原本有数据，挂载后会被遮住；"
                         "请勾选「先把原数据复制到飞牛目录」或明确确认覆盖"
                         % len(p["precopy"]))
    ins = _inspect(docker, name)
    cid = ins["Id"]
    real_name = (ins.get("Name") or ("/" + name)).lstrip("/")
    was_running = bool((ins.get("State") or {}).get("Running"))
    hc = ins.get("HostConfig") or {}

    _emit(emit, "版本备份", 15, "正在生成可回退的版本快照")
    try:
        mgr.backup_one(name, "pre-mount")
    except Exception as exc:
        raise ValueError("无法生成版本备份，已中止重建：%s" % exc)

    copied = 0
    if body.get("precopy") and p["precopy"]:
        _emit(emit, "可选数据预迁移", 35, "正在把容器内原目录内容复制到飞牛目录")
        rows = {r["container"]: r for r in (_doc_container(_load(), name).get("mappings") or [])}
        for item in p["precopy"]:
            r = rows.get(item["container"])
            if not r:
                continue
            try:
                copied += _precopy_one(docker, cid, r)
            except Exception as exc:
                raise ValueError("预迁移失败（%s → %s）：%s；容器未被修改，可修正后重试"
                                 % (r["container"], r["host"], exc))
    else:
        _emit(emit, "可选数据预迁移", 35, "未启用预迁移，跳过")

    # 飞牛目录不存在时，只自动创建位于可写根下的目录；其它目录交给用户处理
    _emit(emit, "重建容器并保留全部原配置", 55, "正在按原配置重建容器")
    for b in p["target_binds"]:
        x = _parse_bind(b)
        if not x:
            continue
        fs = _host_fs(x["host"])
        if fs is not None and not os.path.isdir(fs):
            try:
                os.makedirs(_writable_local(x["host"]), exist_ok=True)
            except ValueError:
                # 不在可写根下：Docker daemon 会自行创建，交给 daemon 处理
                pass

    create_body = manager.build_create_body(ins, (ins.get("Config") or {}).get("Image")
                                            or ins.get("Image") or "", cid)
    create_body.setdefault("HostConfig", {})["Binds"] = list(p["target_binds"])
    try:
        mgr._recreate(cid, real_name, create_body, was_running, hc)
    except Exception as exc:
        try:
            events.emit("mount_fail", "目录映射失败",
                        "容器「%s」应用目录映射时重建失败，已自动还原旧容器：%s"
                        % (name, exc), "high", name)
        except Exception:
            pass
        raise ValueError("重建失败，旧容器已自动还原：%s" % exc)

    _emit(emit, "核对挂载结果", 85, "正在核对新容器的挂载")
    try:
        new_ins = _inspect(docker, real_name)
        _new_bind_set = set(_current_binds(new_ins))
        _new_id = new_ins.get("Id", "")
    except Exception:
        _new_bind_set = set()
        _new_id = ""
    missing = [b for b in p["target_binds"] if b not in _new_bind_set]
    if missing:
        try:
            events.emit("mount_fail", "目录映射未完全生效",
                        "容器「%s」已重建，但以下挂载未出现在新容器里：%s；"
                        "可用「快速回退」还原" % (name, "、".join(missing)), "mid", name)
        except Exception:
            pass
        return {"ok": False, "name": name,
                "message": "容器已重建，但有 %d 条挂载未生效，请用「快速回退」还原后检查路径"
                           % len(missing),
                "missing": missing, "applied": len(p["target_binds"]) - len(missing)}

    with _lock:
        doc = _load()
        doc.setdefault("containers", {})
        rec = doc["containers"].get(name) or {}
        rec["applied"] = {"binds": [_bind_of(r) for r in (rec.get("mappings") or [])
                                    if r.get("enabled")],
                          "time": _now(), "container_id": _new_id or real_name}
        rec["updated"] = _now()
        doc["containers"][name] = rec
        k = doc.setdefault("known", {})
        k[name] = {"id": _new_id or real_name, "image": p["image"],
                   "first_seen": (k.get(name) or {}).get("first_seen") or _now(),
                   "auto_inherited": bool(rec.get("inherited")),
                   "applied_at": _now()}
        _save(doc)

    try:
        mgr.state.add_history("mount", name,
                              "已应用 %d 条目录映射并重建容器%s"
                              % (len(p["diff"]["add"]) + len(p["diff"]["keep"]),
                                 ("，预迁移 %d 个文件" % copied) if copied else ""))
    except Exception:
        pass
    try:
        events.emit("mount_applied", "目录映射已生效",
                    "容器「%s」已按新配置重建：新增 %d 条、移除 %d 条挂载%s。"
                    % (name, len(p["diff"]["add"]), len(p["diff"]["remove"]),
                       ("，预迁移 %d 个文件" % copied) if copied else ""),
                    "ok", name)
    except Exception:
        pass
    _emit(emit, "核对挂载结果", 100, "目录映射已生效")
    return {"ok": True, "name": name,
            "message": "已应用并重建「%s」：新增 %d 条、移除 %d 条挂载%s"
                       % (name, len(p["diff"]["add"]), len(p["diff"]["remove"]),
                          ("，预迁移 %d 个文件" % copied) if copied else ""),
            "diff": p["diff"], "precopied": copied,
            "applied": len(p["target_binds"])}


def browse_dirs(path=None):
    """飞牛目录浏览（只读），供「选择飞牛目录」弹窗使用。"""
    root = config.STORE_HOST_ROOT.rstrip("/")
    p = str(path or root).strip().rstrip("/") or root
    p = validate_host(p)
    fs = _host_fs(p)
    parent = os.path.dirname(p) or root
    if parent == "/" or parent == "":
        parent = root
    dirs = []
    if fs and os.path.isdir(fs):
        try:
            for n in sorted(os.listdir(fs))[:200]:
                full = os.path.join(fs, n)
                if os.path.isdir(full) and not os.path.islink(full):
                    dirs.append({"name": n, "path": p.rstrip("/") + "/" + n})
        except OSError:
            pass
    return {"ok": True, "path": p, "parent": parent, "dirs": dirs,
            "exists": bool(fs and os.path.isdir(fs)),
            "writable": p == root or p.startswith(root + "/"),
            "store_root": root,
            "message": "" if fs else "宿主机根目录未挂载到 /host，无法浏览目录；可直接手填绝对路径"}


# ==========================================================================
# v6.0-r25 · 后续新添加的容器也能自动用上目录映射
# --------------------------------------------------------------------------
# 升级时默认不套模板：首次后台巡检只把「当前已存在」的容器登记进 known，
# 之后在飞牛 GUI / Compose / docker run 里新建的容器才算「新容器」。
# 这样既保证后续容器可用，又绝不会在升级瞬间把一堆老容器重建掉。
# ==========================================================================

DEFAULT_POLICY = {
    # v7.0.1：总开关。关闭时后台巡检只登记容器基线，绝不继承模板、绝不重建容器。
    # 默认关闭，升级后不会突然改变任何现有容器的行为。
    "master": False,
    "inherit_new": False,      # 必须用户明确开启
    "auto_apply": False,       # 继承后是否自动重建（会短暂中断），默认关闭
    "auto_precopy": False,     # 自动应用时是否预迁移容器内原有数据
    "exclude": [],             # 不继承模板的容器名
    "updated": "",
}

_watch_started = False
_watch_lock = threading.Lock()


def _get_template(doc):
    rows = (doc.get("template") or {}).get("mappings") or []
    return [dict(r) for r in rows if isinstance(r, dict)]


def _get_policy(doc):
    p = dict(DEFAULT_POLICY)
    cur = doc.get("policy") or {}
    if isinstance(cur, dict):
        p.update({k: cur[k] for k in DEFAULT_POLICY if k in cur})
    p["exclude"] = [str(x) for x in (p.get("exclude") or []) if str(x).strip()]
    for k in ("master", "inherit_new", "auto_apply", "auto_precopy"):
        p[k] = bool(p[k])
    return p


def _get_known(doc):
    k = doc.get("known") or {}
    return {n: v for n, v in k.items() if isinstance(v, dict)}


def _self_or_bad(name):
    try:
        validate_name(name)
        return False
    except ValueError:
        return True


def _applied_valid(ins, rec):
    """旧记录存过容器名（r24）或真实 ID（r25+），两者都认，兼容升级。"""
    aid = str((rec.get("applied") or {}).get("container_id") or "")
    if not aid:
        return True
    return aid in (str(ins.get("Id") or ""), (ins.get("Name") or "").lstrip("/"))


def _container_map(docker):
    out = {}
    try:
        raw = docker.list_containers(all_=True) or []
    except Exception:
        # Docker daemon 暂时不可用时不能抛到 /api/pro（会变 500），
        # 返回空表让 sync_new 走「本轮跳过」分支，后台线程下一轮再试。
        return {}
    for c in raw:
        n = (c.get("Names") or [""])[0].lstrip("/")
        if not n:
            continue
        out[n] = {"id": str(c.get("Id") or ""),
                  "image": str(c.get("Image") or ""),
                  "running": str(c.get("State") or "").lower() == "running"}
    return out


def _image_short(image):
    """模板占位符 {image} 用镜像短名，去掉仓库前缀与 tag 里的冒号。"""
    s = str(image or "image").split("@")[0].split("/")[-1]
    return re.sub(r"[^A-Za-z0-9_.-]", "-", s).strip("-") or "image"


def _clone_template(rows, name, image=""):
    """把模板复制成某容器的专属配置，支持 {name} / {image} 占位符。"""
    out = []
    for r in rows:
        host = (r.get("host") or "").replace("{name}", name).replace("{image}", _image_short(image))
        cpath = (r.get("container") or "").replace("{name}", name).replace("{image}", _image_short(image))
        out.append({"host": validate_host(host), "container": cpath,
                    "mode": r.get("mode") or "rw", "enabled": r.get("enabled", True),
                    "note": r.get("note") or ""})
    return normalize(out, name)


def template_get():
    with _lock:
        doc = _load()
        tpl, pol, known = _get_template(doc), _get_policy(doc), _get_known(doc)
        inherited = sorted(n for n, rec in (doc.get("containers") or {}).items()
                           if rec.get("inherited"))
    return {"ok": True, "mappings": tpl, "policy": pol, "known_count": len(known),
            "inherited": inherited, "store_root": config.STORE_HOST_ROOT,
            "max_rows": MAX_ROWS,
            "hint": "飞牛目录支持 {name}（容器名）与 {image}（镜像短名）占位符，"
                    "例如 /vol1/1000/docker/appdata/{name} 会让每个新容器各用各的文件夹",
            "version": config.APP_VERSION}


def template_save(body):
    """保存默认模板与新容器策略；非法路径整单拒绝，不污染已有配置。

    v7.0.1：body 里没有 mappings 时视为「只改策略」，沿用已有模板，
    不重写 doc["template"]，也不把继承容器的映射清空。
    """
    has_rows = "mappings" in body
    rows = normalize(body.get("mappings") or [])
    raw_ex = body.get("exclude") or []
    if isinstance(raw_ex, str):
        raw_ex = [x for x in re.split("[,\\n\\s]+", raw_ex) if x]
    exclude = []
    for x in raw_ex:
        n = str(x).strip().lstrip("/")
        if not n:
            continue
        if len(n) > 128 or not re.match(r"^[a-zA-Z0-9_.-]+$", n):
            raise ValueError("排除名单里的容器名不合法：%s" % n)
        if n not in exclude:
            exclude.append(n)
    with _lock:
        doc = _load()
        effective_rows = rows if has_rows else _get_template(doc)
        pol = _get_policy(doc)
        for k in ("master", "inherit_new", "auto_apply", "auto_precopy"):
            if k in body:
                pol[k] = bool(body.get(k))
        # 只更新本次提交里明确给出的字段：前端编辑弹窗可能只改映射，
        # 直接覆盖会把用户之前设的排除名单悄悄清空。
        if "exclude" in body:
            pol["exclude"] = exclude
        pol["updated"] = _now()
        if has_rows:
            doc["template"] = {"mappings": rows, "updated": _now()}
        doc["policy"] = pol
        doc.setdefault("containers", {})
        propagated = []
        # 可选：把新模板推给所有「继承而来、且用户没手工改过」的已有容器
        if body.get("propagate"):
            for n, rec in list(doc["containers"].items()):
                if not rec.get("inherited") or _self_or_bad(n):
                    continue
                try:
                    rec["mappings"] = _clone_template(effective_rows, n,
                                                     (_get_known(doc).get(n) or {}).get("image", ""))
                    rec["updated"] = _now()
                    propagated.append(n)
                except ValueError:
                    continue
        _save(doc)
    return {"ok": True, "mappings": effective_rows, "policy": pol,
            "propagated": propagated, "known_count": len(_get_known(_load())),
            "message": "已保存默认模板（%d 条）与新容器策略%s"
                       % (len(effective_rows), ("，并同步到 %d 个已继承容器（需重新应用才生效）"
                                     % len(propagated)) if propagated else ""),
            "version": config.APP_VERSION}


def set_master(enabled):
    """v7.0.1 · 功能总开关：只改 policy.master。

    单独走一个接口，不复用 template_save —— 后者会重写 doc["template"]，
    如果面板上只切总开关、没带 mappings，会把用户配好的默认模板清空
    （r25 刚修过「只改映射却清空排除名单」的同类 BUG，这里不能重演）。
    """
    with _lock:
        doc = _load()
        pol = _get_policy(doc)
        pol["master"] = bool(enabled)
        pol["updated"] = _now()
        doc["policy"] = pol
        _save(doc)
    return {"ok": True, "policy": pol,
            "message": ("已开启「新容器默认映射模板」功能：新容器将按下方子开关继承模板"
                        if pol["master"] else
                        "已关闭「新容器默认映射模板」功能：后台不再给任何新容器套模板，也不重建容器；配置已保留"),
            "version": config.APP_VERSION}


def sync_new(docker, mgr=None, do_apply=None, manual=False, max_apply=2):
    """扫描容器，给「首次出现」或「同名重建」的容器继承默认模板。

    只读扫描 + 写配置；是否自动重建由策略 auto_apply / 参数 do_apply 决定。
    任何单个容器的失败都不影响其它容器，也不会抛到调用方。
    """
    with _lock:
        doc = _load()
        pol = _get_policy(doc)
        master_off = not bool(pol["master"])
        tpl = _get_template(doc)
        known = _get_known(doc)
        containers = doc.setdefault("containers", {})
        cur = _container_map(docker)
        if not cur:
            return {"ok": True, "new": [], "inherited": [], "applied": [],
                    "skipped": ["Docker 暂时不可用或未返回容器列表，已跳过本轮"],
                    "errors": [], "known_count": len(known),
                    "master_off": master_off,
                    "message": "Docker 暂时不可用，本轮未改动任何配置"}
        # 已消失的容器从登记表移除（配置保留），这样同名重建会被重新识别
        for n in list(known):
            if n not in cur:
                del known[n]
        new_names, inherited, applied, skipped, errors, rebuilt = [], [], [], [], [], []
        # 全新安装 / 清空 data 后的第一轮只建立「当前已存在容器」的基线。
        # 否则用户先配好模板再重启面板，首次巡检会把所有老容器误判成新容器。
        baseline = not bool(doc.get("baseline_done"))
        # v7.0.1：总开关关闭时仍维护 known / baseline（避免开启瞬间把老容器
        # 误判成新容器），但绝不继承模板、绝不自动重建。
        want_apply = (not master_off) and bool(pol["auto_apply"] if do_apply is None else do_apply)
        apply_left = max_apply if not manual else max_apply

        for name, info in sorted(cur.items()):
            prev = known.get(name)
            is_new = prev is None
            id_changed = bool(prev and prev.get("id") and info["id"]
                              and prev["id"] != info["id"])
            if not is_new and not id_changed:
                continue
            new_names.append(name)
            if baseline:
                known[name] = {"id": info["id"], "image": info["image"],
                               "first_seen": _now(), "auto_inherited": False,
                               "seen_at": _now()}
                skipped.append("%s（首次基线登记：只记录现有容器，不套模板）" % name)
                continue
            rec = containers.get(name) or {}
            if master_off:
                skipped.append("%s（功能总开关已关闭，仅登记不套模板）" % name)
            elif _self_or_bad(name):
                skipped.append("%s（面板自身容器，跳过）" % name)
            elif name in pol["exclude"]:
                skipped.append("%s（在排除名单里）" % name)
            elif not pol["inherit_new"]:
                skipped.append("%s（未开启「新容器自动继承」）" % name)
            elif not tpl:
                skipped.append("%s（默认模板为空）" % name)
            elif rec.get("mappings") and not rec.get("inherited"):
                skipped.append("%s（已有手工配置，模板不覆盖）" % name)
            else:
                try:
                    rows = _clone_template(tpl, name, info["image"])
                    prev_applied = rec.get("applied") or {}
                    # 同名重建：旧容器的 applied 记录已失效，必须清空，
                    # 否则 plan 会把「上一个容器」的挂载当成当前受管挂载。
                    containers[name] = {
                        "mappings": rows,
                        "updated": _now(),
                        "applied": {} if id_changed else prev_applied,
                        "inherited": True,
                        "inherited_at": _now(),
                        "inherited_from": "template",
                    }
                    inherited.append(name)
                    try:
                        events.emit("mount_inherit", "新容器继承目录映射",
                                    "容器「%s」%s已自动继承 %d 条默认映射（模板），"
                                    "可在「容器 → 目录映射」里核对后应用"
                                    % (name, "重建后" if id_changed else "首次出现", len(rows)),
                                    "info", name)
                    except Exception:
                        pass
                except ValueError as exc:
                    errors.append("%s：%s" % (name, exc))
            known[name] = {
                "id": info["id"], "image": info["image"],
                "first_seen": (prev or {}).get("first_seen") or _now(),
                "auto_inherited": name in inherited,
                "seen_at": _now(),
            }
            if id_changed and name not in rebuilt:
                rebuilt.append(name)
        doc["known"] = known
        if baseline:
            doc["baseline_done"] = _now()
        _save(doc)

    # 自动应用放在锁外：重建容器耗时，不能阻塞其它线程的保存 / 读取
    if want_apply and inherited:
        for name in inherited[:apply_left]:
            if mgr is None:
                errors.append("%s：自动应用需要面板管理器，本轮只保存了配置" % name)
                break
            try:
                r = apply(docker, mgr, {
                    "name": name, "confirm": True,
                    "precopy": bool(pol["auto_precopy"]),
                    "ack_nonempty": bool(pol["auto_precopy"]),
                })
                if r.get("ok"):
                    applied.append(name)
                else:
                    errors.append("%s：%s" % (name, r.get("message") or "应用未生效"))
            except Exception as exc:
                errors.append("%s：自动应用失败（配置已保存，可手动应用）：%s" % (name, exc))
    if master_off:
        msg = ("功能总开关已关闭：本轮仅登记 %d 个容器基线，未套用模板、未重建任何容器。"
               "到「新容器默认映射模板」面板打开总开关后即可恢复" % len(new_names))
    else:
        msg = ("本轮发现 %d 个新容器，%d 个继承模板，%d 个自动应用%s"
               % (len(new_names), len(inherited), len(applied),
                  ("，%d 项需留意" % (len(errors) + len(skipped))) if errors or skipped else ""))
    return {"ok": True, "new": new_names, "inherited": inherited,
            "applied": applied, "rebuilt": sorted(set(rebuilt)),
            "skipped": skipped, "errors": errors, "baseline": baseline,
            "master_off": master_off,
            "known_count": len(known), "message": msg,
            "policy": _get_policy(_load()), "version": config.APP_VERSION}


def start_watch(docker, mgr=None, interval=30, initial_delay=8):
    """后台巡检：不依赖用户是否打开目录映射页，新容器一出现就自动登记 / 继承。"""
    global _watch_started
    with _watch_lock:
        if _watch_started:
            return False
        _watch_started = True

    def _loop():
        time.sleep(max(0, initial_delay))
        while True:
            try:
                sync_new(docker, mgr)
            except Exception as exc:
                print("[mounts-watch]", str(exc)[:200])
            time.sleep(max(1, int(interval)))

    threading.Thread(target=_loop, daemon=True, name="mounts-watch").start()
    return True
