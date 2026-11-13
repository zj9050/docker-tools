# -*- coding: utf-8 -*-
"""DockerUpdate v7.0.2 · 文件软链接（飞牛 A 文件 → 容器内改名成 B 文件）。

用户场景：飞牛上有一个文件 A，但容器只认文件名 B。本模块把 A 以 B 的名字
放进飞牛某个已挂进容器的目录，容器里就出现 B，无需改镜像、无需重建容器。

两种落地方式（每条规则可选）：
  · link（软链接，默认）：同目录下建立相对符号链接 B -> A。
    容器内挂载该目录后 B 一定能解析；A 内容变化自动跟随，零拷贝。
    为避免容器内断链，强制要求「链接存放目录 = A 所在目录」；跨目录请选 copy。
  · copy（同步副本）：把 A 复制成 B，允许任意链接目录。
    适合跨目录、A 会被原子替换（证书续期 / 配置重新生成）的场景。

执行周期：不自动 / 5 分钟 / 30 分钟 / 1 小时 / 6 小时 / 每天 / 每周。
后台线程按到期时间重新校验并重建（链接断了自动补、副本过期自动重拷），
每次结果写进规则历史，界面能直接看到。默认「不自动」，升级后不会偷偷跑。

安全边界：
  · 源 A 必须是已存在的普通文件；禁止系统目录、..、冒号、换行；
  · 链接目录必须位于面板可写根（STORE_HOST_ROOT）下，否则面板无法创建 B；
  · B 已存在且是普通文件/目录时一律拒绝覆盖，绝不误删用户数据；
  · 面板自身容器禁止作为挂载目标；每容器最多 64 条规则。
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time

import config
import mounts as mounts_mod

SYMLINKS_FILE = os.path.join(config.DATA_DIR, "pro", "file-symlinks.json")
MAX_RULES = 64
MAX_HISTORY = 60

PERIODS = {
    "manual": 0,
    "5m": 300,
    "30m": 1800,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
    "7d": 604800,
}
PERIOD_LABELS = [
    {"id": "manual", "label": "不自动（仅手动）"},
    {"id": "5m", "label": "每 5 分钟"},
    {"id": "30m", "label": "每 30 分钟"},
    {"id": "1h", "label": "每 1 小时"},
    {"id": "6h", "label": "每 6 小时"},
    {"id": "1d", "label": "每天"},
    {"id": "7d", "label": "每周"},
]
MODES = [
    {"id": "link", "label": "软链接（同目录，零拷贝）"},
    {"id": "copy", "label": "同步副本（可跨目录）"},
]

_lock = threading.RLock()
_watch_started = False
_watch_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _default_doc():
    return {"version": 1, "rules": [], "history": [], "updated": ""}


def _load():
    try:
        with open(SYMLINKS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return _default_doc()
        d.setdefault("rules", [])
        d.setdefault("history", [])
        return d
    except Exception:
        return _default_doc()


def _save(doc):
    os.makedirs(os.path.dirname(SYMLINKS_FILE), exist_ok=True)
    # 并发写不能共用一个 .tmp（v5.1 修过的同类问题）
    tmp = "%s.%s.%d.tmp" % (SYMLINKS_FILE, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SYMLINKS_FILE)


def _valid_name(name):
    """容器名可选：留空表示只管理飞牛侧链接，不挂容器。"""
    n = str(name or "").strip().lstrip("/")
    if not n:
        return ""
    return mounts_mod.validate_name(n)   # 内含面板自身容器保护


def _valid_source(p):
    p = str(p or "").strip()
    if not p.startswith("/"):
        raise ValueError("飞牛源文件 A 必须是绝对路径：%s" % (p or "(空)"))
    if ".." in p.split("/") or "\n" in p or "\r" in p or "\t" in p or ":" in p:
        raise ValueError("源文件路径不合法（不能含 .. / 冒号 / 空白）：%s" % p)
    if p.endswith("/"):
        raise ValueError("源文件 A 必须是文件，不能是目录：%s" % p)
    low = p.lower()
    for bad in mounts_mod._HOST_DENY:
        if low == bad or low.startswith(bad + "/"):
            raise ValueError("系统目录下的文件不能作为源：%s" % p)
    return p


def _valid_link_dir(p):
    p = str(p or "").strip().rstrip("/") or config.STORE_HOST_ROOT
    if not p.startswith("/") or ".." in p.split("/") or ":" in p:
        raise ValueError("链接存放目录必须是合法绝对路径：%s" % p)
    root = config.STORE_HOST_ROOT.rstrip("/")
    if p != root and not p.startswith(root + "/"):
        raise ValueError("链接存放目录必须位于面板可写根 %s 下（面板只读挂载宿主机根目录，"
                         "其它位置无法创建 B 文件）：%s" % (root, p))
    return p


def _valid_link_name(n):
    n = str(n or "").strip()
    if not n:
        raise ValueError("容器内要识别的文件名 B 不能为空")
    if len(n) > 128 or "/" in n or "\\" in n or ":" in n or "\n" in n or "\r" in n:
        raise ValueError("文件名 B 不合法（不能含路径分隔符 / 冒号 / 换行，最长 128 字符）：%s" % n)
    if n in (".", "..") or n.startswith("\x00"):
        raise ValueError("文件名 B 不合法：%s" % n)
    return n


def _row_id(r):
    key = "%s\x00%s\x00%s" % (r["source"], r["link_dir"], r["link_name"])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def normalize(rows):
    if not isinstance(rows, list):
        raise ValueError("规则列表格式不正确")
    if len(rows) > MAX_RULES:
        raise ValueError("最多 %d 条规则（当前 %d 条）" % (MAX_RULES, len(rows)))
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        src = str(r.get("source") or "").strip()
        ldir = str(r.get("link_dir") or "").strip()
        lname = str(r.get("link_name") or "").strip()
        if not src and not lname:
            continue
        src = _valid_source(src)
        ldir = _valid_link_dir(ldir)
        lname = _valid_link_name(lname)
        key = (ldir, lname)
        if key in seen:
            raise ValueError("同一目录下不能有两个同名的 B 文件：%s/%s" % (ldir, lname))
        seen.add(key)
        mode = str(r.get("mode") or "link").strip().lower()
        if mode not in ("link", "copy"):
            raise ValueError("方式只能是「软链接」或「同步副本」：%s" % mode)
        period = str(r.get("period") or "manual").strip().lower()
        if period not in PERIODS:
            raise ValueError("执行周期不合法：%s" % period)
        cpath = str(r.get("container_path") or "").strip()
        if cpath:
            cpath = mounts_mod.validate_cpath(cpath)
        if mode == "link" and os.path.dirname(src) != ldir:
            raise ValueError("软链接方式要求「链接存放目录」与 A 所在目录一致（%s）；"
                             "跨目录请改用「同步副本」方式" % os.path.dirname(src))
        name = _valid_name(r.get("name"))
        item = {
            "id": r.get("id") or _row_id({"source": src, "link_dir": ldir, "link_name": lname}),
            "name": name,
            "source": src,
            "link_dir": ldir,
            "link_name": lname,
            "container_path": cpath,
            "mode": mode,
            "period": period,
            "enabled": bool(r.get("enabled", True)),
            "note": str(r.get("note") or "")[:120],
        }
        # 保留运行状态，避免「保存规则」把上次执行结果清空
        for k in ("last_run", "last_state", "last_message", "next_run", "run_count"):
            if k in r:
                item[k] = r[k]
        out.append(item)
    return out


def _readable_fs(host):
    """源 A 的面板内可读路径：优先可写根，其次宿主机只读视图。"""
    root = config.STORE_HOST_ROOT.rstrip("/")
    if host == root or host.startswith(root + "/"):
        return os.path.join(config.STORE_ROOT, host[len(root):].lstrip("/"))
    return mounts_mod._host_fs(host)


def _link_fs(ldir, lname):
    return os.path.join(mounts_mod._writable_local(ldir), lname)


def run_one(r, force=False):
    """执行一条规则：建立/校验 B 文件。只写可写根，不碰容器。"""
    src = r["source"]
    ldir = r["link_dir"]
    lname = r["link_name"]
    mode = r.get("mode") or "link"
    src_fs = _readable_fs(src)
    if not src_fs or not os.path.exists(src_fs):
        return {"ok": False, "state": "error", "message": "找不到源文件 A：%s（若不在可写根下，需挂载宿主机根目录到 /host）" % src}
    if not os.path.isfile(src_fs):
        return {"ok": False, "state": "error", "message": "源文件 A 不是普通文件：%s" % src}
    dst = _link_fs(ldir, lname)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        if mode == "link":
            target = os.path.basename(src)   # 同目录相对链接，容器内也能解析
            if os.path.islink(dst):
                old = os.readlink(dst)
                if old == target:
                    return {"ok": True, "state": "unchanged",
                            "message": "软链接已存在且指向正确，无需改动：%s → %s" % (lname, target)}
                os.unlink(dst)
                os.symlink(target, dst)
            elif os.path.exists(dst):
                return {"ok": False, "state": "error",
                        "message": "%s 已是真实文件/目录，拒绝覆盖以免丢失数据；请改名或手动移除后再执行" % lname}
            else:
                os.symlink(target, dst)
            return {"ok": True, "state": "ok",
                    "message": "已建立软链接 %s → %s" % (lname, target)}
        # copy：原子替换，避免容器读到写一半的文件
        tmp = dst + ".du-%d.tmp" % threading.get_ident()
        shutil.copy2(src_fs, tmp)
        os.replace(tmp, dst)
        return {"ok": True, "state": "ok",
                "message": "已同步 %s → %s（%d 字节）" % (os.path.basename(src), lname, os.path.getsize(dst))}
    except Exception as exc:
        try:
            if os.path.exists(dst + ".du-%d.tmp" % threading.get_ident()):
                os.unlink(dst + ".du-%d.tmp" % threading.get_ident())
        except Exception:
            pass
        return {"ok": False, "state": "error", "message": "创建 B 文件失败：%s" % exc}


def _next_run(period, base=None):
    sec = PERIODS.get(period, 0)
    if not sec:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((base or time.time()) + sec))


def _record(doc, r, res):
    r["last_run"] = _now()
    r["last_state"] = res.get("state") or "error"
    r["last_message"] = res.get("message") or ""
    r["run_count"] = int(r.get("run_count") or 0) + 1
    r["next_run"] = _next_run(r.get("period"))
    doc.setdefault("history", []).insert(0, {
        "time": r["last_run"], "rule": r["id"], "state": r["last_state"],
        "message": r["last_message"], "name": r["name"],
        "link": "%s/%s" % (r["link_dir"], r["link_name"]),
    })
    del doc["history"][MAX_HISTORY:]


def run_rules(ids=None, force=False):
    """立即执行：ids 为空表示全部启用的规则。"""
    with _lock:
        doc = _load()
        rules = doc.get("rules") or []
        want = set(ids or [])
        picked = [r for r in rules if (not want or r["id"] in want) and (force or r.get("enabled", True))]
        if not picked:
            return {"ok": True, "message": "没有需要执行的规则", "results": [], "ran": 0}
        out = []
        for r in picked:
            res = run_one(r, force=force)
            _record(doc, r, res)
            out.append({"id": r["id"], "name": r["name"], "link_name": r["link_name"], **res})
        doc["updated"] = _now()
        _save(doc)
        ok = sum(1 for x in out if x["ok"])
        return {"ok": ok == len(out), "ran": len(out), "ok_count": ok,
                "message": "执行 %d 条：成功 %d、失败 %d" % (len(out), ok, len(out) - ok),
                "results": out}


def run_due(now=None):
    """后台周期巡检：只跑到期的启用规则。

    now 仅用于确定性回归测试（传入模拟时间戳），生产调用不传，取当前时间。
    """
    with _lock:
        doc = _load()
        now = time.time() if now is None else now
        due = []
        for r in doc.get("rules") or []:
            if not r.get("enabled", True):
                continue
            sec = PERIODS.get(r.get("period"), 0)
            if not sec:
                continue
            nr = str(r.get("next_run") or "")
            try:
                ts = time.mktime(time.strptime(nr, "%Y-%m-%d %H:%M:%S")) if nr else 0
            except Exception:
                ts = 0
            if ts <= now:
                due.append(r)
        if not due:
            return 0
        for r in due:
            try:
                res = run_one(r)
            except Exception as exc:
                res = {"ok": False, "state": "error", "message": str(exc)[:200]}
            _record(doc, r, res)
        doc["updated"] = _now()
        _save(doc)
        return len(due)


def list_all(docker=None):
    doc = _load()
    containers = []
    if docker is not None:
        try:
            for c in (docker.list_containers(all_=True) or []):
                n = (c.get("Names") or [""])[0].lstrip("/")
                if n and n != config.SELF_NAME and not n.startswith("docker-update"):
                    containers.append({"name": n, "image": c.get("Image", "")})
        except Exception:
            containers = []
    rules = doc.get("rules") or []
    return {"ok": True, "rules": rules, "history": (doc.get("history") or [])[:MAX_HISTORY],
            "containers": containers, "periods": PERIOD_LABELS, "modes": MODES,
            "store_root": config.STORE_HOST_ROOT, "max_rules": MAX_RULES,
            "updated": doc.get("updated") or "",
            "stats": {"total": len(rules),
                      "enabled": sum(1 for r in rules if r.get("enabled", True)),
                      "auto": sum(1 for r in rules if r.get("enabled", True) and PERIODS.get(r.get("period"), 0)),
                      "errors": sum(1 for r in rules if r.get("last_state") == "error")}}


def save(body):
    rows = normalize(body.get("rules") or [])
    with _lock:
        doc = _load()
        old = {r["id"]: r for r in (doc.get("rules") or [])}
        merged = []
        for r in rows:
            # 保留历史执行状态
            for k in ("last_run", "last_state", "last_message", "next_run", "run_count"):
                if k in old.get(r["id"], {}) and k not in r:
                    r[k] = old[r["id"]][k]
            if r.get("enabled", True) and PERIODS.get(r.get("period"), 0) and not r.get("next_run"):
                r["next_run"] = _next_run(r["period"])
            merged.append(r)
        doc["rules"] = merged
        doc["updated"] = _now()
        _save(doc)
    return {"ok": True, "rules": merged,
            "message": "已保存 %d 条文件软链接规则（%d 条启用）"
                       % (len(merged), sum(1 for r in merged if r.get("enabled", True)))}


def delete(rule_id):
    rid = str(rule_id or "").strip()
    if not rid:
        raise ValueError("缺少规则 id")
    with _lock:
        doc = _load()
        before = len(doc.get("rules") or [])
        doc["rules"] = [r for r in (doc.get("rules") or []) if r.get("id") != rid]
        if len(doc["rules"]) == before:
            raise ValueError("找不到规则 %s" % rid)
        doc["updated"] = _now()
        _save(doc)
    return {"ok": True, "message": "规则已删除（不会删除已生成的 B 文件，可在飞牛里手动清理）"}


def dirs(path=None):
    """浏览链接存放目录。链接目录必须位于可写根下，因此直接列 STORE_ROOT，
    不经过只读的 /host 视图（真实环境更快，未挂 /host 时也能正常浏览）。"""
    root = config.STORE_HOST_ROOT.rstrip("/")
    p = str(path or root).strip().rstrip("/") or root
    p = _valid_link_dir(p)
    fs = mounts_mod._writable_local(p)
    parent = os.path.dirname(p) or root
    if p == root or not (parent == root or parent.startswith(root + "/")):
        parent = root      # 上一级不会跳出可写根
    out = []
    if os.path.isdir(fs):
        try:
            for n in sorted(os.listdir(fs))[:200]:
                full = os.path.join(fs, n)
                if os.path.isdir(full) and not os.path.islink(full):
                    out.append({"name": n, "path": p.rstrip("/") + "/" + n})
        except OSError:
            pass
    return {"ok": True, "path": p, "parent": parent, "dirs": out,
            "exists": os.path.isdir(fs), "writable": True, "store_root": root,
            "message": ""}


def apply_mount(docker, mgr, body, emit=None):
    """可选：把链接存放目录挂进容器（合并进目录映射配置后复用其重建链路）。"""
    name = mounts_mod.validate_name(body.get("name"))
    cpath = str(body.get("container_path") or "").strip()
    if not cpath:
        raise ValueError("请填写容器内目标目录")
    cpath = mounts_mod.validate_cpath(cpath)
    with _lock:
        doc = _load()
        rules = [r for r in (doc.get("rules") or [])
                 if r.get("enabled", True) and r.get("name") == name]
    if not rules:
        raise ValueError("容器「%s」没有关联且启用的文件软链接规则；请先在规则里填「关联容器」" % name)
    # 先执行一遍，保证 B 文件存在
    res = run_rules(force=True)
    if not res["ok"]:
        raise ValueError("生成 B 文件失败，已中止重建：%s"
                         % "；".join(x["message"] for x in res["results"] if not x["ok"]))
    ldirs = sorted({r["link_dir"] for r in rules})
    if len(ldirs) != 1:
        raise ValueError("「一键挂载」要求所有规则使用同一个链接存放目录，当前有 %d 个：%s"
                         % (len(ldirs), "、".join(ldirs)))
    ldir = ldirs[0]
    mdoc = mounts_mod._load()
    mdoc.setdefault("containers", {})
    rec = mdoc["containers"].get(name) or {}
    rows = list(rec.get("mappings") or [])
    exist = any(str(x.get("host")) == ldir and str(x.get("container")) == cpath for x in rows if isinstance(x, dict))
    if not exist:
        rows.append({"host": ldir, "container": cpath, "mode": "rw",
                     "enabled": True, "note": "文件软链接自动挂载"})
        mounts_mod.save({"name": name, "mappings": rows})
    if emit:
        try:
            emit(stage="应用挂载", pct=45, message="已合并目录映射，正在重建容器")
        except Exception:
            pass
    return mounts_mod.apply(docker, mgr,
                            {"name": name, "confirm": True, "ack_nonempty": True}, emit=emit)


def start_watch(interval=20, initial_delay=10):
    """后台周期执行线程；重复调用只启一次。"""
    global _watch_started
    with _watch_lock:
        if _watch_started:
            return False
        _watch_started = True

    def _loop():
        time.sleep(initial_delay)
        while True:
            try:
                run_due()
            except Exception as exc:
                print("[symlinks-watch]", exc)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="du-symlinks-watch").start()
    return True
