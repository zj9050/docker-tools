"""Docker Engine API 客户端（Unix Socket）——全部使用 Python 标准库。

除原有「更新中心」用到的接口外，v2.0 扩展了容器管理、资源统计、日志多路分解、
镜像导出、网络、数据卷、系统信息等能力，供「Docker 管理增强」视图使用。
"""
import base64
import http.client
import time
import json
import socket
import struct
import urllib.parse

import config
import runtrace


class DockerError(Exception):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        http.client.HTTPConnection.__init__(self, "localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def _q(value):
    return urllib.parse.quote(str(value), safe="/:@...")


def _registry_auth_header(auth):
    if not auth:
        return {}
    raw = json.dumps(auth, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return {"X-Registry-Auth": token}


def demux_logs(data, tty=False):
    """把 Docker logs 的 8 字节头多路复用流还原成纯文本；TTY 容器无帧头。"""
    if tty or not data:
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    out = []
    buf = data
    pos = 0
    if len(buf) >= 8:
        first_size = struct.unpack(">I", buf[4:8])[0]
        first_stream = buf[0]
        if first_stream in (0, 1, 2, 3) and 0 < first_size <= len(buf) - 8:
            while pos + 8 <= len(buf):
                stream = buf[pos]
                size = struct.unpack(">I", buf[pos + 4:pos + 8])[0]
                if size <= 0 or pos + 8 + size > len(buf):
                    break
                out.append(buf[pos + 8:pos + 8 + size].decode("utf-8", "replace"))
                pos += 8 + size
            if out:
                return "".join(out)
    return buf.decode("utf-8", "replace")


class DockerClient:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or config.DOCKER_SOCKET

    def _conn(self, method, path, body=None, headers=None, timeout=30):
        conn = UnixHTTPConnection(self.socket_path, timeout=timeout)
        payload = None
        hdrs = {}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            return conn, resp, data
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

    def _request(self, method, path, body=None, headers=None, timeout=30, stream=False):
        # v7.0.1：底层 Engine API 调用逐条写入运行明细（含状态码 / 耗时 / 字节数）。
        _rid = runtrace.current()
        _t0 = time.time()
        if _rid:
            runtrace.note("Docker API", "→ %s %s" % (method, path), "step",
                          "timeout=%ss" % timeout, rid=_rid)
        try:
            conn, resp, data = self._conn(method, path, body, headers, timeout)
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            if _rid:
                runtrace.note("Docker API", "✗ %s %s · 连接失败：%s · %.0fms" %
                              (method, path, exc, (time.time() - _t0) * 1000),
                              "err", rid=_rid)
            raise DockerError(0, "无法连接 Docker Socket：%s" % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        text = data.decode("utf-8", "replace") if data else ""
        _ms = (time.time() - _t0) * 1000
        if resp.status >= 400:
            if _rid:
                runtrace.note("Docker API", "✗ %s %s · HTTP %d · %.0fms · %s" %
                              (method, path, resp.status, _ms, text[:120]),
                              "err", rid=_rid)
            msg = text
            try:
                msg = json.loads(text).get("message", text)
            except Exception:
                pass
            raise DockerError(resp.status, msg, text)
        if _rid:
            runtrace.note("Docker API", "✓ %s %s · HTTP %d · %.0fms · %d B" %
                          (method, path, resp.status, _ms, len(data or b"")),
                          "ok", rid=_rid)
        if not text:
            return None
        if stream:
            events = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("error"):
                    raise DockerError(500, obj.get("error") or obj.get("errorDetail", {}).get("message", "拉取失败"))
                events.append(obj)
            return events
        try:
            return json.loads(text)
        except Exception:
            return text

    def _request_raw(self, method, path, body=None, headers=None, timeout=120):
        """返回 (status, headers_dict, bytes)，用于镜像导出 / 日志等二进制流。"""
        try:
            conn, resp, data = self._conn(method, path, body, headers, timeout)
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise DockerError(0, "无法连接 Docker Socket：%s" % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        if resp.status >= 400:
            txt = data.decode("utf-8", "replace")
            try:
                txt = json.loads(txt).get("message", txt)
            except Exception:
                pass
            raise DockerError(resp.status, txt, txt)
        return resp.status, hdrs, data

    # ---------- 系统 ----------
    def version(self):
        return self._request("GET", "/version", timeout=10)

    def info(self):
        return self._request("GET", "/info", timeout=15)

    def system_df(self):
        return self._request("GET", "/system/df", timeout=20)

    # ---------- 容器：只读 ----------
    def list_containers(self, all_=True):
        return self._request("GET", "/containers/json?all=%d" % (1 if all_ else 0), timeout=15) or []

    def inspect_container(self, cid):
        return self._request("GET", "/containers/%s/json" % _q(cid), timeout=15)

    def stats(self, cid, timeout=12):
        """单次 stats 快照；优先 one-shot，老版本引擎回退 stream=false。"""
        for qs in ("?stream=false&one-shot=1", "?stream=false"):
            try:
                return self._request("GET", "/containers/%s/stats%s" % (_q(cid), qs), timeout=timeout)
            except DockerError as exc:
                if "one-shot" in str(exc.message) or exc.status == 400:
                    continue
                raise
        return None

    def logs(self, cid, tail=200, tty=False, since=0, timestamps=True):
        path = ("/containers/%s/logs?stdout=1&stderr=1&tail=%d&timestamps=%d"
                % (_q(cid), int(tail), 1 if timestamps else 0))
        if since:
            path += "&since=%d" % int(since)
        _, _, raw = self._request_raw("GET", path, timeout=40)
        return demux_logs(raw, tty)

    def top(self, cid, ps_args="aux"):
        try:
            return self._request("GET", "/containers/%s/top?ps_args=%s"
                                 % (_q(cid), urllib.parse.quote(ps_args, safe="")), timeout=15)
        except DockerError:
            return None

    # ---------- 容器：动作 ----------
    def create_container(self, name, body):
        return self._request("POST", "/containers/create?name=%s" % urllib.parse.quote(name, safe=""),
                             body=body, timeout=120)

    def rename(self, cid, new_name):
        return self._request("POST", "/containers/%s/rename?name=%s"
                             % (_q(cid), urllib.parse.quote(new_name, safe="")), timeout=30)

    def start(self, cid):
        return self._request("POST", "/containers/%s/start" % _q(cid), timeout=60)

    def stop(self, cid, timeout=10):
        return self._request("POST", "/containers/%s/stop?t=%d" % (_q(cid), timeout), timeout=90)

    def restart(self, cid, timeout=10):
        return self._request("POST", "/containers/%s/restart?t=%d" % (_q(cid), timeout), timeout=90)

    def pause(self, cid):
        return self._request("POST", "/containers/%s/pause" % _q(cid), timeout=30)

    def unpause(self, cid):
        return self._request("POST", "/containers/%s/unpause" % _q(cid), timeout=30)

    def kill(self, cid, signal="SIGKILL"):
        return self._request("POST", "/containers/%s/kill?signal=%s" % (_q(cid), _q(signal)), timeout=30)

    def remove_container(self, cid, force=True, volumes=False):
        return self._request("DELETE", "/containers/%s?force=%d&v=%d"
                             % (_q(cid), 1 if force else 0, 1 if volumes else 0), timeout=60)

    def update_container(self, cid, body):
        """在线修改 cgroup 资源限制（CPU 核数 / 内存）。"""
        return self._request("POST", "/containers/%s/update" % _q(cid), body=body, timeout=60)

    # ---------- 镜像 ----------
    def inspect_image(self, ref):
        return self._request("GET", "/images/%s/json" % _q(ref), timeout=15)

    def list_images(self):
        return self._request("GET", "/images/json?all=1", timeout=20) or []

    def pull(self, repo, tag, auth=None, timeout=600):
        path = "/images/create?fromImage=%s&tag=%s" % (
            urllib.parse.quote(repo, safe=""), urllib.parse.quote(tag or "latest", safe=""))
        return self._request("POST", path, headers=_registry_auth_header(auth),
                             timeout=timeout or 600, stream=True)

    def pull_ref(self, ref, auth=None, timeout=600):
        repo, tag, _ = _split_ref(ref)
        return self.pull(repo, tag, auth, timeout)

    def remove_image(self, image_id, force=False):
        return self._request("DELETE", "/images/%s?force=%d" % (_q(image_id), 1 if force else 0), timeout=60)

    def tag_image(self, image_id, repo, tag):
        return self._request("POST", "/images/%s/tag?repo=%s&tag=%s"
                             % (_q(image_id), urllib.parse.quote(repo, safe=""),
                                urllib.parse.quote(tag or "latest", safe="")), timeout=30)

    def export_images(self, names):
        """GET /images/get 返回 tar 流（可一次导出多个镜像）。"""
        qs = "&".join("names=" + urllib.parse.quote(str(n), safe="") for n in names)
        return self._request_raw("GET", "/images/get?" + qs, timeout=600)[2]

    def prune_images(self, dangling=True):
        filters = '{"dangling":["%s"]}' % ("true" if dangling else "false")
        return self._request("POST", "/images/prune?filters=%s"
                             % urllib.parse.quote(filters, safe=""), timeout=180)

    # ---------- 网络 ----------
    def list_networks(self):
        return self._request("GET", "/networks", timeout=15) or []

    def inspect_network(self, nid):
        return self._request("GET", "/networks/%s" % _q(nid), timeout=15)

    def create_network(self, body):
        return self._request("POST", "/networks/create", body=body, timeout=60)

    def remove_network(self, nid):
        return self._request("DELETE", "/networks/%s" % _q(nid), timeout=30)

    def connect_network(self, nid, cid, aliases=None):
        body = {"Container": cid}
        if aliases:
            body["EndpointConfig"] = {"Aliases": aliases}
        return self._request("POST", "/networks/%s/connect" % _q(nid), body=body, timeout=30)

    def disconnect_network(self, nid, cid, force=False):
        return self._request("POST", "/networks/%s/disconnect" % _q(nid),
                             body={"Container": cid, "Force": bool(force)}, timeout=30)

    # ---------- 数据卷 ----------
    def list_volumes(self):
        return (self._request("GET", "/volumes", timeout=15) or {}).get("Volumes") or []

    def create_volume(self, name, driver="local", labels=None):
        return self._request("POST", "/volumes/create", body={
            "Name": name, "Driver": driver, "Labels": labels or {},
        }, timeout=30)

    def remove_volume(self, name, force=False):
        return self._request("DELETE", "/volumes/%s?force=%d" % (_q(name), 1 if force else 0), timeout=30)

    def prune_volumes(self):
        return self._request("POST", "/volumes/prune", timeout=120)


def _split_ref(ref):
    ref = (ref or "").strip()
    if "@sha256:" in ref:
        return ref.split("@")[0], "latest", "pinned"
    tail = ref.rsplit("/", 1)[-1]
    if ":" in tail:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag, "tag"
    return ref, "latest", "tag"
