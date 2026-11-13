"""DockerUpdate v6.0-r18 · 镜像更新（与「容器更新」并列的独立页面）。

和容器更新页的区别，必须说清楚：
  · 容器更新 = 拉新镜像 → 备份 → 按原配置重建容器 → 健康检查 → 失败回退；
  · 镜像更新 = 只比对 digest / 只拉取刷新本地 tag，**绝不重建容器**。
    更新完成后已有容器仍在跑旧镜像，需要去「容器更新」页重建才生效；
    旧镜像若不再被任何 tag 引用会变成悬空镜像，可在「本地镜像」页清理。

检测原理：向 registry 发 manifest GET，读响应头 Docker-Content-Digest，
与本地 RepoDigests 比对。只取 manifest、不下载镜像层，所以「检测」很轻。
候选源来自加速源配置（多个源串行回退，单个失败不影响其它镜像），
匿名 401 会自动取一次 Bearer token 重试；全部源不可达时标记为中性
「已跳过」，不写橙色「连接仓库超时」，也不假装已是最新。

纯标准库、只读本地镜像、不结束容器、不改 daemon.json；写操作只落
DATA_DIR/image_updates.json（已纳入面板备份）。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import config
import events
import store as st
from docker_api import DockerError

CACHE_FILE = os.path.join(config.DATA_DIR, "image_updates.json")
_lock = threading.RLock()
MAX_REFS = 40
MAX_HOSTS = 4
REQ_TIMEOUT = 2.5
PULL_TIMEOUT_CAP = 300

ACCEPT = ("application/vnd.docker.distribution.manifest.list.v2+json, "
          "application/vnd.docker.distribution.manifest.v2+json, "
          "application/vnd.oci.image.index.v1+json, "
          "application/vnd.oci.image.manifest.v1+json, */*")

LIMITS = ("两种模式：①「更新选中」只拉取镜像、不重建容器，拉取后 tag 指向新 digest，"
          "运行中的容器仍用旧镜像，旧镜像会变成悬空镜像，可在「本地镜像」页清理；"
          "②「更新并重建」拉取后按原配置重建使用该镜像的容器，先备份、保留端口/环境变量/卷/网络/标签，"
          "健康检查不通过自动回退，会短暂中断服务。只有检测到可更新版本才显示这两个按钮。")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(doc):
    tmp = "%s.%d.%d.tmp" % (CACHE_FILE, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_FILE)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def split_ref(ref):
    """'nginx:1.27' / 'ghcr.io/a/b:v1' / 'reg:5000/a/b' → (repo, tag)。"""
    ref = str(ref or "").strip()
    if "@sha256:" in ref:
        return ref.split("@", 1)[0], "digest"
    tail = ref.rsplit("/", 1)[-1]
    if ":" in tail and not tail.startswith("sha256:"):
        repo, tag = ref.rsplit(":", 1)
        return repo, tag or "latest"
    return ref, "latest"


def _norm_repo(repo):
    r = str(repo or "").strip().lower()
    if r.startswith("docker.io/"):
        r = r[len("docker.io/"):]
    if r.startswith("index.docker.io/"):
        r = r[len("index.docker.io/"):]
    if "/" not in r:
        r = "library/" + r
    return r


def _pick_local_digest(digests, repo):
    """从 RepoDigests 中挑出属于该 repo 的 digest（sha256:...）。"""
    ds = [str(x) for x in (digests or []) if x]
    if not ds:
        return ""
    want = _norm_repo(repo)
    vals = []
    for d in ds:
        name, _, val = d.partition("@")
        if val:
            vals.append((_norm_repo(name), val))
    for name, val in vals:
        if name == want:
            return val
    uniq = set(v for _, v in vals)
    if len(uniq) == 1:
        return next(iter(uniq))
    return vals[0][1] if vals else ""


def _ts(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(v)))
    except Exception:
        return str(v or "—")


def _mb(v):
    try:
        return round(float(v) / 1048576, 1)
    except Exception:
        return 0.0


def local_rows(docker):
    """把 Docker 镜像清单展开成「一个 repo:tag 一行」。"""
    rows, seen = [], set()
    try:
        imgs = docker.list_images() or []
    except DockerError:
        raise
    for i in imgs:
        tags = [t for t in (i.get("RepoTags") or [])
                if t and "<none>" not in t and not t.startswith("sha256:")]
        full_id = str(i.get("Id") or "")
        digests = i.get("RepoDigests") or []
        for t in tags:
            if "@sha256:" in t or t in seen:
                continue
            repo, tag = split_ref(t)
            if not repo or tag == "digest":
                continue
            seen.add(t)
            rows.append({
                "ref": t, "repo": repo, "tag": tag,
                "id": full_id.replace("sha256:", "")[:12], "full_id": full_id,
                "size_mb": _mb(i.get("Size")),
                "created": _ts(i.get("Created")),
                "containers": int(i.get("Containers") or 0),
                "local_digest": _pick_local_digest(digests, repo),
                "repo_digests": [str(x) for x in digests],
            })
    rows.sort(key=lambda x: (x["containers"] == 0, x["ref"].lower()))
    return rows


def _registry_hosts():
    """候选 manifest 主机：加速源在前，官方兜底；去掉本机地址。"""
    out, seen = [], set()
    try:
        cfg = st.ACCEL.cfg()
    except Exception:
        cfg = {}
    for x in (cfg.get("docker") or []):
        u = str((x or {}).get("url") or "").strip()
        p = urllib.parse.urlparse(u if "://" in u else "https://" + u)
        host = p.netloc
        if not host or host in seen:
            continue
        if host.startswith("127.0.0.1") or host.startswith("localhost"):
            continue
        seen.add(host)
        out.append({"label": str((x or {}).get("label") or host),
                    "host": host, "scheme": "http" if p.scheme == "http" else "https"})
        if len(out) >= MAX_HOSTS:
            break
    if "registry-1.docker.io" not in seen:
        out.append({"label": "Docker Hub 官方", "host": "registry-1.docker.io",
                    "scheme": "https"})
    return out


def _anon_token(auth_header):
    """解析 WWW-Authenticate: Bearer realm=...,service=...,scope=... 并取匿名 token。"""
    if not auth_header or "Bearer" not in auth_header:
        return ""
    kv = dict(re.findall(r'(\w+)="([^"]+)"', auth_header))
    realm = kv.get("realm")
    if not realm:
        return ""
    q = {}
    if kv.get("service"):
        q["service"] = kv["service"]
    if kv.get("scope"):
        q["scope"] = kv["scope"]
    url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "DockerUpdate/6.0"}),
                timeout=REQ_TIMEOUT) as r:
            d = json.loads(r.read(4096).decode("utf-8", "replace"))
        return str(d.get("token") or d.get("access_token") or "")
    except Exception:
        return ""


def _manifest_digest(src, repo, tag):
    """GET manifest，返回 (digest, 耗时ms, 错误原因)。不下镜像层。"""
    name = _norm_repo(repo)
    url = "%s://%s/v2/%s/manifests/%s" % (src["scheme"], src["host"], name, tag)
    headers = {"User-Agent": "DockerUpdate/6.0", "Accept": ACCEPT}
    t0 = time.time()

    def once(extra=None):
        h = dict(headers)
        if extra:
            h.update(extra)
        req = urllib.request.Request(url, method="GET", headers=h)
        with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            head = r.read(512)
            if "html" in ctype or b"<html" in head[:200].lower():
                raise ValueError("返回 HTML 错误页")
            return (r.headers.get("Docker-Content-Digest") or "").strip()

    try:
        d = once()
        ms = int((time.time() - t0) * 1000)
        if not d.startswith("sha256:"):
            return "", ms, "源未提供 digest 响应头"
        return d, ms, ""
    except urllib.error.HTTPError as e:
        ms = int((time.time() - t0) * 1000)
        if e.code == 401:
            tok = _anon_token(e.headers.get("WWW-Authenticate"))
            if tok:
                try:
                    d = once({"Authorization": "Bearer " + tok})
                    if d.startswith("sha256:"):
                        return d, int((time.time() - t0) * 1000), ""
                    return "", int((time.time() - t0) * 1000), "源未提供 digest 响应头"
                except Exception as ex2:
                    return "", ms, "鉴权后仍失败：%s" % str(ex2)[:50]
            return "", ms, "需要鉴权且未提供匿名 token"
        reason = {403: "需要鉴权 / 不提供匿名 manifest", 404: "该源未收录此镜像",
                  429: "429 限流"}.get(e.code, "HTTP %d" % e.code)
        return "", ms, reason
    except Exception as exc:
        msg = str(exc)
        reason = "超时" if ("timed out" in msg or "timeout" in msg.lower()) else msg[:60]
        return "", int((time.time() - t0) * 1000), reason


def _remote(repo, tag, hosts):
    """多源串行回退，返回第一个拿到 digest 的结果。"""
    tried = []
    for src in hosts:
        d, ms, err = _manifest_digest(src, repo, tag)
        if d:
            return {"ok": True, "digest": d, "via": src["label"],
                    "host": src["host"], "latency_ms": ms, "tried": tried}
        tried.append("%s：%s" % (src["label"], err or "无 digest"))
    return {"ok": False, "digest": "", "via": "", "host": "",
            "latency_ms": 0, "reason": "；".join(tried[:2])}


class ImageUpdates:
    def __init__(self, docker, mgr=None, audit=None):
        self.docker = docker
        self.mgr = mgr
        self.audit = audit or (lambda *a, **k: None)

    # ---------------- 读：本地镜像 + 缓存结果 ----------------
    def status(self):
        with _lock:
            doc = _load()
        cached = doc.get("items") or {}
        try:
            rows = local_rows(self.docker)
            docker_ok = True
            err = ""
        except Exception as exc:
            rows, docker_ok, err = [], False, str(exc)[:160]
        out = []
        for r in rows:
            c = cached.get(r["ref"]) or {}
            st_ = c.get("status") or "unchecked"
            row = dict(r)
            row.update({
                "status": st_,
                "remote_digest": c.get("remote_digest", ""),
                "remote_via": c.get("remote_via", ""),
                "remote_ms": c.get("remote_ms", 0),
                "checked_at": c.get("checked_at", ""),
                "reason": c.get("reason", ""),
                "last_pull": c.get("last_pull", ""),
                "pull_changed": bool(c.get("pull_changed")),
                "old_id": c.get("old_id", ""),
                "new_id": c.get("new_id", ""),
                "status_label": STATUS_LABELS.get(st_, "未检测"),
            })
            out.append(row)
        summary = {"total": len(out), "outdated": 0, "latest": 0, "skipped": 0,
                   "unknown_digest": 0, "unchecked": 0, "used": 0, "pulled": 0}
        for r in out:
            s = r["status"]
            if s in summary:
                summary[s] += 1
            if r["containers"] > 0:
                summary["used"] += 1
        return {"ok": True, "rows": out, "summary": summary,
                "at": doc.get("at", ""), "docker_ok": docker_ok, "error": err,
                "sources": _registry_hosts(), "limits": LIMITS,
                "message": ("读取到 %d 个带标签的本地镜像" % len(out)) if docker_ok
                           else ("Docker 不可用：%s" % err)}

    # ---------------- 检测：只取 manifest digest ----------------
    def check(self, refs=None, emit=None):
        try:
            rows = local_rows(self.docker)
        except Exception as exc:
            msg = "Docker 暂时不可用，无法读取本地镜像：%s" % str(exc)[:120]
            if emit:
                emit(stage="读取本地镜像", pct=100, message=msg)
            return {"ok": False, "message": msg}
        all_refs = [r["ref"] for r in rows]
        want = [str(x) for x in (refs or []) if str(x) in all_refs] or all_refs
        want = want[:MAX_REFS]
        if not want:
            return {"ok": False, "message": "本机没有可检测的标签镜像（悬空 / digest 固定镜像不参与）"}
        by_ref = {r["ref"]: r for r in rows}
        hosts = _registry_hosts()
        t0 = time.time()
        if emit:
            emit(stage="读取本地镜像与候选源", pct=4,
                 message="正在检测 %d 个镜像 · %d 个候选源" % (len(want), len(hosts)))

        def one(ref):
            r = by_ref[ref]
            remote = _remote(r["repo"], r["tag"], hosts)
            return ref, r, remote

        results = []
        done = [0]
        rlock = threading.Lock()
        with ThreadPoolExecutor(max_workers=6) as ex:
            for ref, r, remote in ex.map(one, want):
                if not remote["ok"]:
                    rec = {"status": "skipped",
                           "reason": "仓库暂不可达或该源未收录（已跳过）：%s" %
                                     (remote.get("reason") or "无可用源"),
                           "remote_digest": "", "remote_via": "", "remote_ms": 0}
                elif not r["local_digest"]:
                    rec = {"status": "unknown_digest",
                           "reason": "本地无 RepoDigest 可比（本地构建或旧版 Docker），可直接拉取刷新",
                           "remote_digest": remote["digest"], "remote_via": remote["via"],
                           "remote_ms": remote["latency_ms"]}
                elif remote["digest"] == r["local_digest"]:
                    rec = {"status": "latest", "reason": "与远端 digest 一致，已是最新",
                           "remote_digest": remote["digest"], "remote_via": remote["via"],
                           "remote_ms": remote["latency_ms"]}
                else:
                    rec = {"status": "outdated", "reason": "远端 digest 已变化，可更新",
                           "remote_digest": remote["digest"], "remote_via": remote["via"],
                           "remote_ms": remote["latency_ms"]}
                    try:
                        events.emit("image_update_found", "镜像有新版本",
                                    "%s 远端 digest 已更新（%s）" % (ref, remote["via"]),
                                    "info", ref)
                    except Exception:
                        pass
                rec["checked_at"] = _now()
                rec["local_digest"] = r["local_digest"]
                results.append((ref, rec))
                with rlock:
                    done[0] += 1
                if emit:
                    emit(stage="比对远端 digest", pct=min(96, 4 + int(done[0] * 90 / len(want))),
                         detail="%s · %s" % (ref, STATUS_LABELS.get(rec["status"], "")),
                         message="已检测 %d/%d" % (done[0], len(want)))

        with _lock:
            doc = _load()
            items = doc.get("items") or {}
            for ref, rec in results:
                old = items.get(ref) or {}
                old.update(rec)
                items[ref] = old
            doc = {"at": _now(), "items": items}
            _save(doc)
        counts = {}
        for _, rec in results:
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        msg = ("检测完成：%d 个镜像 · 可更新 %d · 已最新 %d · 需拉取刷新 %d · 中性跳过 %d · 耗时 %.1fs"
               % (len(results), counts.get("outdated", 0), counts.get("latest", 0),
                  counts.get("unknown_digest", 0), counts.get("skipped", 0),
                  time.time() - t0))
        try:
            if self.mgr:
                self.mgr.state.add_history("check", "镜像更新", msg, True)
        except Exception:
            pass
        self.audit("image-update-check", "镜像更新", msg)
        return {"ok": True, "message": msg, "counts": counts,
                "elapsed_s": round(time.time() - t0, 1)}

    # ---------------- 更新：只拉取镜像，不重建容器 ----------------
    def _pull_one(self, ref):
        repo, tag = split_ref(ref)
        try:
            old = self.docker.inspect_image(ref) or {}
        except Exception:
            old = {}
        old_id = str(old.get("Id") or "")
        auth = None
        try:
            if self.mgr:
                auth = self.mgr._registry_auth(repo)
        except Exception:
            auth = None
        timeout = PULL_TIMEOUT_CAP
        try:
            if self.mgr and self.mgr.settings:
                timeout = min(PULL_TIMEOUT_CAP, int(self.mgr.settings().get("check_timeout", 300)) or 300)
        except Exception:
            pass
        via = "直接拉取"
        err = ""
        try:
            self.docker.pull_ref(ref, auth, timeout=timeout)
        except Exception as exc:
            err = str(exc)[:100]
            # 直连失败时沿用 r4 的加速源代拉链路：拉镜像 → 重新打回原始 tag
            ok2, via2 = False, ""
            try:
                import manager
                for mref in manager.mirror_refs(repo, tag)[:4]:
                    if mref == ref:
                        continue
                    try:
                        self.docker.pull_ref(mref, auth, timeout=min(timeout, 120))
                        im = self.docker.inspect_image(mref) or {}
                        iid = str(im.get("Id") or "")
                        if iid:
                            self.docker.tag_image(iid, repo, tag)
                            ok2, via2 = True, mref.split("/")[0]
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if not ok2:
                return {"ref": ref, "ok": False,
                        "reason": "仓库不可达，且加速源均未同步该镜像；可先在「告警 → 自动修复」处理或给 NAS 的 Docker daemon 配加速后重试（%s）" % err}
            via = "加速源代拉 " + via2
        try:
            new = self.docker.inspect_image(ref) or {}
        except Exception:
            new = {}
        new_id = str(new.get("Id") or "")
        digests = new.get("RepoDigests") or []
        changed = bool(new_id and old_id and new_id != old_id)
        return {"ref": ref, "ok": True, "via": via, "old_id": old_id.replace("sha256:", "")[:12],
                "new_id": new_id.replace("sha256:", "")[:12], "changed": changed,
                "local_digest": _pick_local_digest(digests, repo),
                "reason": ("已拉取新镜像（ID 变化）" if changed else
                           "拉取完成，镜像 ID 未变化（远端本就一致）")}

    def pull(self, refs=None, emit=None):
        try:
            rows = local_rows(self.docker)
        except Exception as exc:
            msg = "Docker 暂时不可用，无法更新镜像：%s" % str(exc)[:120]
            if emit:
                emit(stage="准备拉取", pct=100, message=msg)
            return {"ok": False, "message": msg}
        all_refs = [r["ref"] for r in rows]
        # 允许更新本机尚未存在的 ref 吗？——不允许，避免误拉一堆镜像；只更新本地已存在的 tag
        want = [str(x) for x in (refs or []) if str(x) in all_refs][:MAX_REFS]
        if not want:
            return {"ok": False, "message": "请先在列表里勾选要更新的镜像"}
        t0 = time.time()
        if emit:
            emit(stage="准备拉取", pct=3, message="即将更新 %d 个镜像（不会重建容器）" % len(want))
        out, done = [], [0]
        for ref in want:
            r = self._pull_one(ref)
            out.append(r)
            done[0] += 1
            if emit:
                emit(stage="拉取并校验镜像", pct=min(96, 3 + int(done[0] * 90 / len(want))),
                     detail="%s · %s" % (ref, "成功" if r["ok"] else "失败"),
                     message="已处理 %d/%d" % (done[0], len(want)))
        with _lock:
            doc = _load()
            items = doc.get("items") or {}
            for r in out:
                rec = items.get(r["ref"]) or {}
                rec["last_pull"] = _now()
                if r["ok"]:
                    rec.update({"status": "latest", "reason": r["reason"],
                                "pull_changed": r["changed"], "old_id": r["old_id"],
                                "new_id": r["new_id"], "via": r["via"],
                                "local_digest": r["local_digest"]})
                else:
                    rec.update({"status": "pull_failed", "reason": r["reason"],
                                "pull_changed": False})
                items[r["ref"]] = rec
            _save({"at": _now(), "items": items})
        ok = sum(1 for r in out if r["ok"])
        changed = sum(1 for r in out if r.get("changed"))
        msg = ("镜像更新完成：成功 %d / 共 %d，其中 %d 个镜像 ID 实际变化；"
               "已有容器未被重建，需要到「容器更新」页重建才生效" % (ok, len(out), changed))
        try:
            if self.mgr:
                self.mgr.state.add_history("update", "镜像更新", msg, ok > 0)
        except Exception:
            pass
        try:
            events.emit("image_update_done", "镜像更新完成", msg, "ok", "镜像更新")
        except Exception:
            pass
        self.audit("image-update-pull", "镜像更新", msg)
        return {"ok": True, "message": msg, "ok_count": ok, "changed": changed,
                "fail_count": len(out) - ok, "items": out,
                "elapsed_s": round(time.time() - t0, 1)}

    def clear(self):
        with _lock:
            try:
                os.remove(CACHE_FILE)
            except OSError:
                pass
        return {"ok": True, "message": "已清空镜像更新检测记录，下次检测会重新比对"}


STATUS_LABELS = {
    "unchecked": "未检测", "outdated": "可更新", "latest": "已是最新",
    "unknown_digest": "可拉取刷新", "skipped": "中性跳过",
    "pull_failed": "更新失败", "pulled": "已更新",
    "local": "本地构建",
}
