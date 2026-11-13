"""DockerUpdate v2.1-r11 · 异步任务中心（SSE 任务进度架构）。

为什么要有它：飞牛远程访问 / 反向代理网关（截图里的 docker-zj…ccd:55555）对单个
HTTP 请求通常有 60 秒左右的硬超时。安装、下载 FPK、检查更新、批量更新一旦超过，
网关直接返回 504，浏览器只看到「请求失败 504」，而后端其实还在跑。

这里把长操作拆成「立即返回的 POST + 后台线程 + SSE 事件流」：
  · POST 毫秒级返回 job_id，不再占用网关连接；
  · 后台线程执行原逻辑，通过 emit() 推阶段、百分比、下载字节/速度；
  · GET /api/job/events?job=<id> 以 text/event-stream 推给前端；
  · 反代不保持长连接时前端自动退化成 GET /api/job 轮询兜底。
"""
import queue
import threading
import time
import uuid

import runtrace

MAX_JOBS = 80          # 只保留最近 80 个任务，避免长期运行内存膨胀


class JobCenter:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}

    def submit(self, kind, label, stages, fn):
        """提交任务，返回 job_id。fn(emit) 在后台线程执行。"""
        jid = uuid.uuid4().hex[:16]
        # v7.0.1：同一个 HTTP 请求派生出的异步任务复用同一条运行明细 run，
        # SSE 事件里带增量行，/api/trace 轮询也能拿到全量。
        cur = runtrace.current()
        if cur:
            runtrace.relabel(cur, "job-" + kind, label)
            runtrace.add_alias(cur, jid)
        else:
            runtrace.start("job-" + kind, label, rid=jid)
        job = {
            "id": jid, "kind": kind, "label": label, "stages": list(stages or []),
            "status": "queued", "stage": "排队中…", "pct": 0, "detail": "",
            "speed": "", "downloaded": 0, "total": 0,
            "message": "", "error": "", "result": {},
            "started_at": time.time(), "finished_at": 0,
            "q": queue.Queue(maxsize=400), "seq": 0, "trace_id": jid,
        }
        with self.lock:
            self.jobs[jid] = job
            if len(self.jobs) > MAX_JOBS:
                for k in sorted(self.jobs, key=lambda x: self.jobs[x]["started_at"])[:-MAX_JOBS]:
                    self.jobs.pop(k, None)
        t = threading.Thread(target=self._run, args=(job, fn), daemon=True,
                             name="du-job-" + jid[:8])
        t.start()
        return jid

    def _emit(self, job, **fields):
        with self.lock:
            job["seq"] += 1
            for k in ("stage", "pct", "detail", "speed", "downloaded", "total",
                      "message", "error", "status"):
                if k in fields:
                    job[k] = fields[k]
            ev = {k: job[k] for k in
                  ("id", "kind", "label", "stages", "status", "stage", "pct", "detail",
                   "speed", "downloaded", "total", "message", "error")}
            ev["type"] = fields.get("type", "progress")
            ev["seq"] = job["seq"]
            ev["ts"] = time.time()
            ev["trace_id"] = job["trace_id"]
            ev["line"] = None
            # v7.0.1：每个阶段变化写一条明细；emit(trace=...) 可写更具体的代码级信息。
            if ev["type"] != "end":
                lvl = {"start": "step", "error": "err", "done": "ok"}.get(ev["type"], "info")
                txt = str(fields.get("trace") or "").strip()
                if not txt:
                    bits = []
                    if "stage" in fields and fields["stage"]:
                        bits.append(str(fields["stage"]))
                    if "pct" in fields:
                        bits.append("%s%%" % fields["pct"])
                    for k in ("detail", "speed"):
                        if fields.get(k):
                            bits.append(str(fields[k]))
                    txt = " · ".join(bits) or "状态更新"
                ev["line"] = runtrace.note(str(fields.get("scope") or job["kind"]),
                                           txt, lvl, "seq=%d" % job["seq"],
                                           rid=job["trace_id"])
        try:
            job["q"].put_nowait(ev)
        except queue.Full:
            pass
        return ev

    def _run(self, job, fn):
        runtrace.bind(job["trace_id"])
        self._emit(job, type="start", status="running", stage=job["stages"][0] if job["stages"] else "开始处理",
                   pct=2, detail="任务已开始")
        emit = lambda **kw: self._emit(job, **kw)
        try:
            result = fn(emit) or {}
            job["result"] = result if isinstance(result, dict) else {"value": result}
            msg = str(job["result"].get("message") or "操作已完成")
            self._emit(job, type="done", status="done", stage="操作已完成", pct=100,
                       detail="", message=msg)
        except Exception as exc:
            err = str(exc)[:400]
            self._emit(job, type="error", status="error", stage="操作失败", pct=100,
                       error=err, message=err)
        finally:
            job["finished_at"] = time.time()
            self._emit(job, type="end", status=job["status"])
            runtrace.finish(job["trace_id"], job["status"] != "error",
                            job.get("message") or job.get("error") or "任务结束")
            runtrace.unbind()

    def snapshot(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return None
            snap = {k: j[k] for k in
                    ("id", "kind", "label", "stages", "status", "stage", "pct", "detail",
                     "speed", "downloaded", "total", "message", "error", "result",
                     "started_at", "finished_at", "seq", "trace_id")}
            snap["trace"] = runtrace.snapshot(jid)
            return snap

    def events(self, jid, timeout=10):
        """阻塞取一条事件；超时返回 None 让上层写心跳注释，穿透反代缓冲。"""
        with self.lock:
            j = self.jobs.get(jid)
        if not j:
            return {"type": "error", "error": "任务不存在或已被清理", "status": "error",
                    "pct": 100, "stage": "任务不存在", "id": jid, "seq": 0}
        try:
            return j["q"].get(timeout=timeout)
        except queue.Empty:
            # 迟到的订阅者（刷新页面 / 反代掐断后重连）：任务已结束就直接给终态，
            # 不再让它白等一个永远不会来的 end 事件。
            if j["finished_at"]:
                snap = self.snapshot(jid) or {}
                snap["type"] = "end"
                return snap
            return None


JOBS = JobCenter()
