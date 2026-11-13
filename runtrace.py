# -*- coding: utf-8 -*-
"""DockerUpdate v7.0.1 · 后台运行明细（run trace）总线。

进度浮层以前只能显示「服务端处理中」这类示意文案，看不到后台究竟在做什么。
这里给每一个 POST 请求 / 异步任务建立一条 run，逐行记录：

  · HTTP 入口与参数摘要（密码 / 令牌脱敏）
  · 每一次 Docker Engine API 调用（方法、路径、状态码、耗时、返回字节数）
  · 业务阶段（检测 / 拉取 / 重建 / 删除 / 回退 …）与结果
  · 异常摘要与终态

前端进度浮层通过 SSE 事件（异步任务）或 /api/trace 轮询（同步请求）取增量行，
按 seq 追加显示，不重建 DOM，避免历史上「转圈时整页闪」。
模块名不用 `trace`，避免与 Python 标准库同名。
"""
import json
import threading
import time
import uuid
from datetime import datetime

_local = threading.local()
MAX_LINES = 400
MAX_RUNS = 60
_SENSITIVE = ("password", "token", "secret", "key", "auth", "cookie",
              "credential", "data", "content", "header")


def stamp():
    return datetime.now().strftime("%H:%M:%S.") + "%03d" % (int(time.time() * 1000) % 1000)


def brief(body, limit=180):
    """请求体摘要：敏感字段一律打码，避免把密码写进明细。"""
    if not isinstance(body, dict):
        return ""
    out = {}
    for k, v in body.items():
        if any(s in str(k).lower() for s in _SENSITIVE):
            out[k] = "***"
        else:
            s = str(v)
            out[k] = s[:60] + ("…" if len(s) > 60 else "")
    try:
        t = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        t = str(out)
    return t[:limit] + ("…" if len(t) > limit else "")


class TraceBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.runs = {}
        self.aliases = {}
        self.order = []

    def _resolve(self, rid):
        return self.aliases.get(rid, rid)

    def start(self, kind, label, rid=None):
        rid = rid or uuid.uuid4().hex[:16]
        with self.lock:
            run = {"id": rid, "kind": kind, "label": label,
                   "started_at": time.time(), "finished_at": 0,
                   "ok": True, "status": "running", "msg": "",
                   "lines": [], "seq": 0, "last_ts": time.time()}
            self.runs[rid] = run
            self.order.append(rid)
            while len(self.order) > MAX_RUNS:
                old = self.order.pop(0)
                self.runs.pop(old, None)
        return rid

    def relabel(self, rid, kind, label):
        with self.lock:
            r = self.runs.get(self._resolve(rid))
            if r:
                r["kind"], r["label"] = kind, label

    def add_alias(self, rid, alias):
        """一个 HTTP run 同时用 job_id 索引，SSE 与 /api/trace 看到同一条明细。"""
        with self.lock:
            self.aliases[alias] = self._resolve(rid)

    def bind(self, rid):
        _local.rid = rid

    def current(self):
        return getattr(_local, "rid", "")

    def unbind(self):
        _local.rid = ""

    def note(self, scope, msg, level="info", code="", rid=None):
        rid = self._resolve(rid or self.current())
        if not rid:
            return None
        with self.lock:
            r = self.runs.get(rid)
            if not r:
                return None
            now = time.time()
            r["seq"] += 1
            line = {"seq": r["seq"], "ts": stamp(), "level": level,
                    "scope": str(scope)[:40], "msg": str(msg)[:400],
                    "code": str(code)[:160],
                    "dur": int(max(0, (now - r["last_ts"]) * 1000))}
            r["last_ts"] = now
            r["lines"].append(line)
            if len(r["lines"]) > MAX_LINES:
                del r["lines"][:len(r["lines"]) - MAX_LINES]
            return line

    def finish(self, rid, ok=True, msg=""):
        rid = self._resolve(rid or self.current())
        with self.lock:
            r = self.runs.get(rid)
            if r:
                r["finished_at"] = time.time()
                r["ok"] = bool(ok)
                r["status"] = "done" if ok else "error"
                r["msg"] = str(msg)[:300]
        self.note("完成", ("✓ " if ok else "✗ ") +
                  (str(msg)[:200] or ("操作成功" if ok else "操作失败")),
                  "ok" if ok else "err", rid=rid)

    def handoff(self, rid):
        """POST 已返回但后台任务继续：保持 run 打开，交给后台线程 finish。"""
        rid = self._resolve(rid or self.current())
        with self.lock:
            r = self.runs.get(rid)
            if r:
                r["status"] = "background"
        return rid

    def snapshot(self, rid="", after=0):
        with self.lock:
            if rid:
                r = self.runs.get(self._resolve(rid))
            else:
                # 最近一个仍在跑、或刚结束的 run：前端漏传 run_id 时兜底
                cand = [self.runs[x] for x in reversed(self.order) if x in self.runs]
                r = next((x for x in cand if not x["finished_at"]),
                         cand[0] if cand else None)
            if not r:
                return {"id": "", "kind": "", "label": "", "status": "missing",
                        "ok": True, "finished": True, "seq": 0, "lines": []}
            lines = [l for l in r["lines"] if l["seq"] > int(after or 0)]
            return {"id": r["id"], "kind": r["kind"], "label": r["label"],
                    "status": r["status"], "ok": r["ok"],
                    "finished": bool(r["finished_at"]),
                    "started_at": r["started_at"], "seq": r["seq"],
                    "lines": lines[-200:]}


BUS = TraceBus()
start = BUS.start
relabel = BUS.relabel
add_alias = BUS.add_alias
bind = BUS.bind
current = BUS.current
unbind = BUS.unbind
note = BUS.note
finish = BUS.finish
handoff = BUS.handoff
snapshot = BUS.snapshot
