"""DockerUpdate v2.1 Web 服务（Python 标准库 http.server，无第三方依赖）。"""
import base64
import json
import os
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import events
import jobs as jobs_mod
import netwatch
import mounts as mounts_mod
import symlinks as symlinks_mod
import pro as pro_mod
import registry as registry_mod
import runtrace
from auth import Auth
from console import Console
from desktop import Desktop
from docker_api import DockerError
from manager import Manager

_BG_PATHS = frozenset(("/api/check-all", "/api/check-daily", "/api/check",
                       "/api/update", "/api/rollback", "/api/backup"))

mgr = Manager()
auth = Auth()
console = Console(mgr)
desktop = Desktop()
reghub = registry_mod.RegistryHub(mgr)
pro = pro_mod.Pro(mgr, console, desktop)
_login_fails = {}   # 简单的登录失败限流：ip -> (次数, 解封时间)
_login_lock = threading.Lock()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}



def _int_arg(qs, key, default, low=None, high=None):
    """v3.9-r6：畸形查询参数不再让接口抛 500，自动回退默认值并夹到合法范围。"""
    try:
        n = int(qs.get(key, [default])[0] or default)
    except (TypeError, ValueError, IndexError):
        n = default
    if low is not None:
        n = max(low, n)
    if high is not None:
        n = min(high, n)
    return n

class Handler(BaseHTTPRequestHandler):
    server_version = "DockerUpdate/7.0.4"

    def log_message(self, fmt, *args):
        if " 4" in (fmt % args)[:20] or " 5" in (fmt % args)[:20]:
            print("[http]", fmt % args)

    # ---------- 工具 ----------
    def _json(self, obj, status=200):
        # v7.0.1：POST 响应带上 run_id，并把终态写进运行明细；
        # 异步任务 / 后台巡检保持 run 打开，交给后台线程收尾。
        rid = runtrace.current()
        if isinstance(obj, dict) and rid:
            obj = dict(obj)
            is_job = bool(obj.get("async") and obj.get("job_id"))
            if is_job:
                runtrace.add_alias(rid, str(obj["job_id"]))
                obj["run_id"] = str(obj["job_id"])
            else:
                obj["run_id"] = rid
            if self.command == "POST":
                ok = status < 400 and obj.get("ok") is not False
                runtrace.note("HTTP", "← HTTP %d · %s · %s" %
                              (status, "ok" if ok else "fail",
                               str(obj.get("message") or obj.get("error") or "")[:120]),
                              "ok" if ok else "err", rid=rid)
                p = urllib.parse.urlparse(self.path).path
                if is_job or (ok and p in _BG_PATHS):
                    runtrace.handoff(rid)
                    if not is_job:
                        obj["background"] = True
                else:
                    runtrace.finish(rid, ok,
                                    str(obj.get("message") or obj.get("error") or ""))
                runtrace.unbind()
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _binary(self, data, ctype, filename):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _auth(self):
        h = self.headers.get("Authorization", "")
        token = h[7:] if h.startswith("Bearer ") else ""
        if token and auth.verify_token(token):
            return True
        if self._readonly():
            return True
        self._json({"ok": False, "auth_required": True,
                    "error": "登录已失效，请重新登录"}, 401)
        return False

    def _view_key(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (self.headers.get("X-View-Key") or q.get("view", [""])[0] or "")

    def _readonly(self):
        return pro.is_viewer(self._view_key())

    def _apikey(self):
        return self.headers.get("X-API-Key", "")

    def _open_api(self, path):
        """GET /api/open/<what>：X-API-Key 只读接口，供 HA / 脚本调用。"""
        if not pro.is_apikey(self._apikey()):
            self._json({"ok": False, "error": "缺少或无效的 X-API-Key"}, 401)
            return
        what = path.rsplit("/", 1)[-1]
        if what not in ("state", "containers", "images", "logs"):
            self._json({"ok": False, "error": "开放的只读接口不存在"}, 404)
            return
        try:
            self._json(pro.open_data(what))
        except Exception as exc:
            self._error(exc, 500)

    # v3.9：这些 /api/pro 是「读取」，不算动作，不写运行日志
    _READ_PRO = frozenset(("state", "tasks", "task-options", "notify-config", "accel",
                           "audit-list", "dirs", "creds", "hosts", "roles", "api-keys",
                           "desktop-config", "container-table", "overview", "stacks",
                           "stack-read", "stack-logs", "templates", "image-history",
                           "update-preview", "open-data", "registry-diagnose",
                           "image-probe-cache", "ports", "ports-check",
                           "netwatch", "netwatch-probe", "netwatch-test",
                           "image-updates", "mounts-list", "mounts-status",
                           "mounts-plan", "mounts-dirs", "mounts-precopy-check",
                           "mounts-template", "symlinks-list", "symlinks-dirs"))
    # manager 内部已经写过更具体的 history，避免同一动作出现两条
    _HIST_SKIP = frozenset(("/api/check-all", "/api/check-daily", "/api/check",
                            "/api/update", "/api/rollback", "/api/backup",
                            "/api/policy", "/api/note", "/api/restore-name",
                            "/api/restore-all-names"))

    def _audit_post(self, path, body):
        if path in ("/api/login", "/api/auth-status"):
            return
        target = str(body.get("name") or body.get("kind") or "")[:100]
        pro.audit("http:" + path, target, "POST 已受理", {"user": auth.username})
        if path in self._HIST_SKIP:
            return
        action, message = "api", "%s 已执行" % path
        if path == "/api/logout":
            action, message, target = "login", "退出登录", auth.username
        elif path == "/api/settings":
            action, message, target = "settings", "面板设置已保存", "面板"
        elif path == "/api/registry-auth":
            action, message, target = "settings", "私有仓库认证已保存", "仓库"
        elif path == "/api/change-password":
            action, message, target = "settings", "登录密码已修改", auth.username
        elif path == "/api/backup-import":
            action, message, target = "backup", "面板备份已导入", "面板"
        elif path == "/api/console":
            action, message = "container", "容器 / 镜像操作：%s" % (body.get("kind") or "—")
        elif path == "/api/registry":
            action, message = "image", "镜像仓库操作：%s" % (body.get("kind") or "—")
        elif path == "/api/pro":
            kind = str(body.get("kind") or "")
            if kind in self._READ_PRO:
                return
            action, message = "system", "系统联动操作：%s" % kind
            target = str((body.get("body") or {}).get("name") or kind)[:100]
        try:
            mgr.state.add_history(action, target or "面板", message, True)
        except Exception:
            pass

    def _token(self):
        h = self.headers.get("Authorization", "")
        return h[7:] if h.startswith("Bearer ") else ""

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _error(self, exc, status=400):
        msg = getattr(exc, "message", None) or str(exc)
        # v3.9-r6：参数错误是 400，Docker 暂时不可用是 503；
        # 只有真正未预期的异常才留在 500，便于排错也不把可预期错误伪装成服务崩溃。
        if status >= 500:
            if isinstance(exc, ValueError):
                status = 400
            elif exc.__class__.__name__.startswith("Docker") or "Docker Socket" in msg:
                status = 503
        self._json({"ok": False, "error": msg}, status)

    def _base_url(self):
        # 公网／反向代理访问时优先使用转发头，否则 redirect_uri 会被算成内网地址，
        # 导致 token_url 校验 redirect_uri 不一致（表现为授权失败）。
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
                or "%s:%d" % (config.HOST_IP, config.PORT)).split(",")[0].strip()
        scheme = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        return "%s://%s" % (scheme, host)

    def _rate_ok(self):
        ip = self.client_address[0] if self.client_address else "?"
        now = time.time()
        with _login_lock:
            count, until = _login_fails.get(ip, (0, 0))
            if until and now < until:
                return False
            return True

    def _rate_fail(self):
        ip = self.client_address[0] if self.client_address else "?"
        now = time.time()
        with _login_lock:
            count, until = _login_fails.get(ip, (0, 0))
            if until and now >= until:
                count = 0
            count += 1
            _login_fails[ip] = (count, now + 30 if count >= 5 else 0)
        return count

    def _rate_ok_reset(self):
        ip = self.client_address[0] if self.client_address else "?"
        with _login_lock:
            _login_fails.pop(ip, None)

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if path == "/api/auth-status":
                self._json(auth.status(self._token()))
                return
            if path.startswith("/api/open/"):
                self._open_api(path)
                return
            # v3.0：应用商店分区及其 FPK / Moo 源 / OAuth 已整体移除。
            # 应用图标由 <img> 直接加载，浏览器不会带 Authorization 头，
            # 因此这个只读图片代理放在鉴权之前：只返回已登记源里的图片二进制，
            # 校验 URL 白名单、限制 2MB / 8 秒，不返回任何业务数据。
            # v2.1-r12：镜像仓库图标。<img> 不会带 Bearer token，
            # 只返回「面板生成的字母 SVG」或白名单源里的图片二进制，不返回业务数据。
            if path == "/api/registry/icon":
                qs = urllib.parse.parse_qs(parsed.query)
                u = qs.get("url", [""])[0]
                name = qs.get("name", [""])[0]
                try:
                    if u:
                        if not reghub.icon_allowed(u):
                            raise ValueError("图标地址不在已登记镜像源里")
                        data, ctype = reghub.fetch_icon(u)
                    else:
                        data, ctype = reghub.generated_icon(name)
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                except Exception:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "public, max-age=60")
                    self.end_headers()
                return
            # v2.1-r11：SSE 任务进度流。EventSource 不能带 Authorization 头，
            # 所以允许把会话令牌放在 query（与 OAuth 回调一样，不返回业务数据）。
            # 反向代理不保持长连接时前端会自动退化成 /api/job 轮询。
            if path == "/api/job/events":
                qs = urllib.parse.parse_qs(parsed.query)
                tok = (qs.get("token", [""])[0] or self._token())
                if not tok or not auth.verify_token(tok):
                    self._json({"ok": False, "auth_required": True,
                                "error": "登录已失效，请重新登录"}, 401)
                    return
                jid = qs.get("job", [""])[0]
                self.protocol_version = "HTTP/1.1"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(raw):
                    self.wfile.write(("%X\r\n" % len(raw)).encode("ascii") + raw + b"\r\n")
                    self.wfile.flush()

                def push(obj):
                    chunk(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")

                chunk(b"retry: 1500\n\n")
                push({"type": "hello", "id": jid, "stage": "已连接任务进度"})
                deadline = time.time() + 900
                try:
                    while time.time() < deadline:
                        ev = jobs_mod.JOBS.events(jid, timeout=8)
                        if ev is None:
                            chunk(b": heartbeat\n\n")
                            continue
                        push(ev)
                        if ev.get("type") == "end":
                            break
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                return
            if not self._auth():
                return
            try:
                if path == "/api/registry/state":
                    self._json(reghub.state())
                elif path == "/api/registry/search":
                    qs = urllib.parse.parse_qs(parsed.query)
                    self._json(reghub.search(qs.get("q", [""])[0], qs.get("source", [""])[0],
                                             _int_arg(qs, "page", 1, 1, 100000),
                                             _int_arg(qs, "page_size", 0, 0, 200)))
                elif path == "/api/registry/tags":
                    qs = urllib.parse.parse_qs(parsed.query)
                    self._json(reghub.tags(qs.get("image", [""])[0], qs.get("source", ["dockerhub"])[0]))
                elif path == "/api/trace":
                    qs = urllib.parse.parse_qs(parsed.query)
                    self._json({"ok": True, "run": runtrace.snapshot(
                        qs.get("id", [""])[0],
                        _int_arg(qs, "after", 0, 0, 1000000))})
                elif path == "/api/job":
                    qs = urllib.parse.parse_qs(parsed.query)
                    snap = jobs_mod.JOBS.snapshot(qs.get("id", [""])[0])
                    if not snap:
                        self._json({"ok": False, "error": "任务不存在或已被清理"}, 404)
                    else:
                        self._json({"ok": True, "job": snap})
                elif path == "/api/state":
                    d = mgr.snapshot()
                    d["username"] = auth.username
                    d.update(console.extra_state(d["containers"]))
                    self._json(d)
                elif path == "/api/logs":
                    mgr.state.prune_history()
                    self._json({"ok": True, "history": mgr.state.data["history"][:1000]})
                elif path == "/api/backups":
                    name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
                    self._json({"ok": True, "backups": mgr.list_backups(name)})
                elif path == "/api/console-detail":
                    qs = urllib.parse.parse_qs(parsed.query)
                    self._json(console.detail(qs.get("name", [""])[0]))
                elif path == "/api/console-logs":
                    qs = urllib.parse.parse_qs(parsed.query)
                    self._json(console.logs(qs.get("name", [""])[0],
                                            _int_arg(qs, "tail", 200, 1, 5000)))
                elif path == "/api/container-metrics":
                    qs = urllib.parse.parse_qs(parsed.query)
                    name = qs.get("name", [""])[0]
                    self._json({"ok": True, "name": name,
                                "series": console.monitor.series_for(name),
                                "latest": console.monitor.metrics().get(name, {})})
                elif path == "/api/container-config":
                    qs = urllib.parse.parse_qs(parsed.query)
                    d = console.container_config(qs.get("name", [""])[0])
                    self._binary(d["json"].encode("utf-8"), "application/json; charset=utf-8",
                                 "container-%s.json" % d["name"].replace("/", "_"))
                elif path == "/api/image-export":
                    qs = urllib.parse.parse_qs(parsed.query)
                    names = qs.get("names", [""])[0].split(",")
                    names = [n for n in names if n]
                    self._binary(console.resources.export_images(names),
                                 "application/x-tar", "docker-images.tar")
                elif path == "/api/backup-export":
                    self._binary(console.resources.export_backup(),
                                 "application/zip",
                                 "dockerupdate-backup-%s.zip" % time.strftime("%Y%m%d-%H%M"))
                elif path == "/api/registry-auth":
                    try:
                        with open(config.REGISTRY_AUTH_FILE, "r", encoding="utf-8") as f:
                            text = f.read()
                    except Exception:
                        text = "{}"
                    self._json({"ok": True, "text": text})
                elif path == "/api/registry/diagnose":
                    self._json(pro.registry_diagnose())
                elif path == "/api/pro/state":
                    self._json(pro.state())
                else:
                    self._json({"ok": False, "error": "API 不存在"}, 404)
            except Exception as exc:
                self._error(exc, 500)
            return
        self._static(path)

    def _static(self, path):
        if path in ("/", "/index.html"):
            rel = os.path.join("static", "index.html")
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            if ".." in rel or rel.startswith("/"):
                self._json({"error": "forbidden"}, 403)
                return
            rel = os.path.join("static", rel)
        else:
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        full = os.path.join(config.BASE_DIR, rel)
        if not os.path.isfile(full):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if ext == ".html" else "public, max-age=600")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            self._json({"ok": False, "error": "API 不存在"}, 404)
            return
        body = self._body()
        # v7.0.1：每个写请求开一条运行明细 run（登录请求不记录，避免碰密码）。
        if path != "/api/login":
            rid = runtrace.start("POST", "POST " + path)
            runtrace.bind(rid)
            runtrace.note("HTTP", "→ POST %s" % path, "step",
                          "kind=%s · %s" % (str(body.get("kind") or "-"),
                                            runtrace.brief(body)), rid=rid)

        # 公开接口：登录
        if path == "/api/login":
            if not self._rate_ok():
                self._json({"ok": False, "error": "失败次数过多，请 30 秒后再试"}, 429)
                return
            username = str(body.get("username") or "")
            password = str(body.get("password") or "")
            if auth.verify(username, password):
                self._rate_ok_reset()
                try:
                    mgr.state.add_history("login", auth.username, "登录成功", True)
                    events.emit("login_ok", "登录成功",
                                "账号「%s」已从 %s 登录 DockerUpdate 面板。"
                                % (auth.username, self.client_address[0]),
                                "info", auth.username)
                except Exception:
                    pass
                self._json({"ok": True, "token": auth.issue_token(),
                            "username": auth.username, "message": "登录成功"})
            else:
                n = self._rate_fail()
                try:
                    events.emit("login_fail", "登录失败",
                                "来自 %s 的登录尝试失败（账号「%s」），连续失败 %d 次%s。"
                                % (self.client_address[0], username or "(空)", n,
                                   "，已临时锁定 30 秒" if n >= 5 else ""),
                                "high" if n >= 5 else "mid", username or "面板")
                except Exception:
                    pass
                self._json({"ok": False, "error": "账号或密码不正确%s"
                            % ("（连续失败 %d 次）" % n if n >= 3 else "")}, 401)
            return

        if not self._auth():
            return
        if self._readonly():
            self._json({"ok": False, "error": "只读查看者不能执行写操作（管理员请重新登录）"}, 403)
            return
        name = str(body.get("name") or "")
        try:
            if path == "/api/logout":
                self._json({"ok": True, "message": "已退出登录"})
            elif path == "/api/change-password":
                new_token = auth.change_password(
                    str(body.get("old_password") or ""),
                    str(body.get("new_password") or ""),
                    str(body.get("confirm") or ""))
                try:
                    events.emit("password_changed", "登录密码被修改",
                                "登录密码已修改，其它设备的会话已全部失效。",
                                "mid", auth.username, dedupe=False)
                except Exception:
                    pass
                self._json({"ok": True, "token": new_token,
                            "message": "密码已修改，其他设备的登录已失效"})
            elif path == "/api/check-all":
                ok, msg = mgr.check_all(auto=False)
                self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            elif path == "/api/check-daily":
                ok, msg = mgr.check_all(auto=True)
                self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            elif path == "/api/check":
                if not name:
                    raise ValueError("缺少容器名")
                ok, msg = mgr.check_single(name)
                self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            elif path == "/api/update":
                if not name:
                    raise ValueError("缺少容器名")
                ok, msg = mgr.update_single(name)
                self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            elif path == "/api/rollback":
                if not name:
                    raise ValueError("缺少容器名")
                ok, msg = mgr.rollback_single(name, body.get("backup_id"))
                self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            elif path == "/api/backup":
                if not name:
                    raise ValueError("缺少容器名")
                ok, msg = mgr.backup_single(name)
                self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            elif path == "/api/note":
                if not name:
                    raise ValueError("缺少容器名")
                msg = mgr.set_note(name, body.get("alias"), body.get("memo"))
                self._json({"ok": True, "message": "%s %s" % (name, msg)})
            elif path == "/api/restore-name":
                if not name:
                    raise ValueError("缺少容器名")
                removed = mgr.restore_name(name)
                self._json({"ok": True, "removed": removed,
                            "message": "%s 已恢复原名" % name if removed else "%s 本来就没有备注" % name})
            elif path == "/api/restore-all-names":
                removed = mgr.restore_all_names()
                self._json({"ok": True, "count": len(removed), "removed": removed,
                            "message": "已恢复全部原容器名（%d 个）" % len(removed)})
            elif path == "/api/policy":
                mode = str(body.get("mode") or "")
                with mgr.state.lock:
                    label = mgr.set_policy(name, mode)
                self._json({"ok": True, "message": "%s 已设为「%s」" % (name, label)})
            elif path == "/api/settings":
                with mgr.state.lock:
                    mgr.state.set_settings(body.get("settings") or {})
                try:
                    patch = body.get("settings") or {}
                    events.emit("settings_changed", "设置变更",
                                "面板设置已保存：%s" % ("、".join(sorted(str(k) for k in patch)[:6]) or "无字段"),
                                "info", "面板", dedupe=False)
                except Exception:
                    pass
                self._json({"ok": True, "message": "设置已保存"})
            elif path == "/api/registry-auth":
                text = str(body.get("text") or "").strip()
                if text:
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        raise ValueError("不是合法的 JSON")
                else:
                    parsed = {}
                os.makedirs(config.DATA_DIR, exist_ok=True)
                with open(config.REGISTRY_AUTH_FILE, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
                self._json({"ok": True, "message": "仓库认证已保存"})
            elif path == "/api/console":
                kind = str(body.get("kind") or "")
                self._json(console.action(kind, body.get("body") or {}))
            elif path == "/api/backup-import":
                try:
                    raw = base64.b64decode(str(body.get("data") or ""))
                except Exception:
                    raise ValueError("备份数据不是合法的 base64")
                self._json(console.import_backup(
                    raw,
                    restore_compose=bool(body.get("restore_compose")),
                    compose_dir=str(body.get("compose_dir") or "")))
            elif path == "/api/registry/autofix":
                jid = jobs_mod.JOBS.submit(
                    "registry-autofix", "正在自动修复仓库连接",
                    ["探测可用加速源", "经加速源代拉镜像", "重新打回原始标签",
                     "重新检测全部容器", "汇总结果"],
                    lambda emit: pro.registry_autofix(emit))
                self._json({"ok": True, "async": True, "job_id": jid,
                            "message": "自动修复已在后台启动"})
            elif path == "/api/registry":
                self._json(reghub.submit(str(body.get("kind") or ""), body.get("body") or {}))
            elif path == "/api/pro":
                self._json(pro.submit(str(body.get("kind") or ""),
                                      body.get("body") or {},
                                      {"user": auth.username}))
            else:
                self._json({"ok": False, "error": "API 不存在"}, 404)
            self._audit_post(path, body)
        except (DockerError, ValueError) as exc:
            self._error(exc, 400)
        except Exception as exc:
            self._error(exc, 500)


def daily_loop():
    while True:
        time.sleep(20)
        try:
            mgr.daily_tick()
        except Exception as exc:
            print("[scheduler]", exc)


def startup_loop():
    time.sleep(4)
    try:
        mgr.startup_check()
    except Exception as exc:
        print("[startup]", exc)


def main():
    console.start()
    try:
        netwatch.start(mgr)      # v6.0-r17：外网断网 / 恢复双向通知
    except Exception as exc:
        print("[netwatch] 启动失败：", exc)
    try:
        # v6.0-r25：后台登记容器，之后新建 / 重建的容器自动继承默认目录映射
        mounts_mod.start_watch(mgr.docker, mgr)
    except Exception as exc:
        print("[mounts-watch] 启动失败：", exc)
    try:
        # v7.0.2：文件软链接按周期自动校验并重建 B 文件
        symlinks_mod.start_watch()
    except Exception as exc:
        print("[symlinks-watch] 启动失败：", exc)
    threading.Thread(target=daily_loop, daemon=True).start()
    threading.Thread(target=startup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    srv.daemon_threads = True
    print("DockerUpdate v%s 已启动 → http://0.0.0.0:%d (账号: %s；应用商店分区已移除)" %
          (config.APP_VERSION, config.PORT, auth.username), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
