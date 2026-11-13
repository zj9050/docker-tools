"""DockerUpdate v5.0 · 多渠道通知（纯标准库）。

设计约束：
  · 不内置任何第三方地址或凭据，未配置时明确返回「未配置 / 已跳过」，绝不伪造推送成功；
  · 旧版 notify.json 单 Webhook 格式自动迁移为 channels[0]，升级不丢配置；
  · 每个渠道单独返回结果，一个失败不影响其它渠道；
  · 事件开关按类型过滤，避免自动巡检把消息刷爆。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

import config
import events
import notice

NOTIFY_FILE = os.path.join(config.DATA_DIR, "pro", "notify.json")

CHANNEL_TYPES = (
    ("webhook", "自定义 Webhook", "https://…/notify", "POST JSON：title / text / event / time"),
    ("wecom", "企业微信机器人", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…", "text 消息 · 微信端可直接显示"),
    ("dingtalk", "钉钉机器人", "https://oapi.dingtalk.com/robot/send?access_token=…", "可填加签密钥"),
    ("feishu", "飞书机器人", "https://open.feishu.cn/open-apis/bot/v2/hook/…", "可填加签密钥"),
    ("telegram", "Telegram Bot", "https://api.telegram.org/bot<TOKEN>/sendMessage", "「令牌」栏填 chat_id"),
    ("bark", "Bark", "https://bark.example.com/<KEY>", "iOS 推送"),
    ("serverchan", "Server 酱", "https://sctapi.ftqq.com/<KEY>.send", "form 表单"),
)

DEFAULT_EVENTS = {
    "update_ok": True,        # 容器更新成功
    "update_fail": True,      # 更新失败
    "auto_rollback": True,    # 健康检查失败自动回退
    "found_update": False,    # 发现新版本（默认关，避免每日刷屏）
    "container_down": True,   # 容器意外退出
    "check_fail": False,      # 检测失败
}


def _default():
    return {"enabled": False, "channels": [], "events": dict(DEFAULT_EVENTS)}


def _migrate(c):
    """兼容 v3.x 的 {enabled, mode, url, token} 单渠道格式。"""
    if not isinstance(c, dict):
        return _default()
    if isinstance(c.get("channels"), list):
        out = _default()
        out["enabled"] = bool(c.get("enabled"))
        out["channels"] = [x for x in c["channels"] if isinstance(x, dict) and x.get("url")]
        ev = dict(DEFAULT_EVENTS)
        if isinstance(c.get("events"), dict):
            ev.update({k: bool(v) for k, v in c["events"].items() if k in ev})
        out["events"] = ev
        return out
    out = _default()
    out["enabled"] = bool(c.get("enabled"))
    if c.get("url"):
        out["channels"] = [{"id": "legacy", "type": "webhook", "name": "原有 Webhook",
                            "url": str(c["url"]), "token": str(c.get("token") or ""),
                            "secret": "", "enabled": True}]
    return out


def load():
    try:
        with open(NOTIFY_FILE, "r", encoding="utf-8") as f:
            return _migrate(json.load(f))
    except Exception:
        return _default()


def save(c):
    os.makedirs(os.path.dirname(NOTIFY_FILE), exist_ok=True)
    tmp = NOTIFY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    os.replace(tmp, NOTIFY_FILE)
    return c


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def allowed(event):
    """v5.1：事件开关统一读 data/events.json（告警页与通知中心共用一份）。"""
    c = load()
    if not c.get("enabled") or not c.get("channels"):
        return False
    if not event:
        return True
    return events.enabled(event)


def _signed_url(url, secret, kind):
    """钉钉 / 飞书加签。"""
    if not secret:
        return url
    ts = str(int(time.time() * 1000))
    if kind == "dingtalk":
        sign = base64.b64encode(hmac.new(
            secret.encode(), (ts + "\n" + secret).encode(), hashlib.sha256).digest()).decode()
        extra = {"timestamp": ts, "sign": urllib.parse.quote(sign)}
    else:
        sign = base64.b64encode(hmac.new(
            (ts + "\n" + secret).encode(), b"", hashlib.sha256).digest()).decode()
        extra = {"timestamp": ts, "sign": sign}
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode(extra)


def _payload(ch, title, text, event, level="info", name=""):
    """各渠道统一用 notice.card 排版；保留原有字段以兼容自建接收端。"""
    typ = ch.get("type") or "webhook"
    c = notice.card(title, text, level, name=name, event=event or "")
    # v6.0-r7：全部渠道共用同一份飞牛原生样式纯文本，不再维护第二套 markdown 文案。
    wx, line1 = c["wechat"], c["line1"]
    if typ == "wecom" or notice.is_wechat(ch.get("url") or "", typ):
        # 微信端只认 msgtype=text；判定不依赖单一域名，自建中转也不带 markdown。
        return "application/json", json.dumps(
            notice.build_payload(ch.get("url") or "", typ, c),
            ensure_ascii=False).encode(), {}
    if typ == "dingtalk":
        # 钉钉 text 消息换行原样保留，不再用 markdown（避免两套排版不一致）。
        return "application/json", json.dumps(
            {"msgtype": "text", "text": {"content": wx}},
            ensure_ascii=False).encode(), {}
    if typ == "feishu":
        return "application/json", json.dumps(
            {"msg_type": "text", "content": {"text": wx}},
            ensure_ascii=False).encode(), {}
    if typ == "telegram":
        return "application/json", json.dumps(
            {"chat_id": ch.get("token") or "", "text": wx,
             "disable_web_page_preview": True},
            ensure_ascii=False).encode(), {}
    if typ == "bark":
        return "application/json", json.dumps(
            {"title": line1, "body": wx, "group": "面板通知"},
            ensure_ascii=False).encode(), {}
    if typ == "serverchan":
        return "application/x-www-form-urlencoded", urllib.parse.urlencode(
            {"title": line1, "desp": wx}).encode(), {}
    # 自建 Webhook / 中转网关默认纯 text，绝不附带 markdown 字段。
    return "application/json", json.dumps(
        notice.build_payload(ch.get("url") or "", typ, c),
        ensure_ascii=False).encode(), {}

def send(title, text, event=None, level="info", name=""):
    """返回逐渠道结果；未配置时明确 skipped，不假装成功。"""
    c = load()
    if not c.get("enabled"):
        return {"ok": True, "skipped": True, "message": "通知未启用，已跳过推送", "channels": []}
    channels = [x for x in c.get("channels") or [] if x.get("enabled") and x.get("url")]
    if not channels:
        return {"ok": True, "skipped": True, "message": "未配置任何启用的通知渠道，已跳过推送", "channels": []}
    if event and not events.enabled(event):
        return {"ok": True, "skipped": True,
                "message": "事件「%s」的推送开关已关闭，已跳过" % event, "channels": []}
    out = []
    for ch in channels:
        url = _signed_url(str(ch.get("url") or ""), str(ch.get("secret") or ""),
                          ch.get("type") or "webhook")
        ctype, data, _ = _payload(ch, title, text, event, level, name)
        headers = {"Content-Type": ctype, "User-Agent": "DockerUpdate/6.0"}
        if ch.get("token") and (ch.get("type") or "webhook") == "webhook":
            headers["X-Token"] = str(ch["token"])
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read(512).decode("utf-8", "ignore")
                ok = r.status < 400
                # 部分机器人 HTTP 200 但 body 里 errcode != 0，如实标记
                try:
                    j = json.loads(body)
                    if isinstance(j, dict) and (j.get("errcode") not in (None, 0)
                                                or j.get("code") not in (None, 0)):
                        ok = False
                except Exception:
                    pass
                out.append({"channel": ch.get("name") or ch.get("type"),
                            "ok": ok, "message": "HTTP %d%s" % (r.status, "" if ok else " · " + body[:80])})
        except Exception as exc:
            out.append({"channel": ch.get("name") or ch.get("type"),
                        "ok": False, "message": "推送失败：%s" % str(exc)[:120]})
    good = sum(1 for x in out if x["ok"])
    return {"ok": good > 0 or not out, "skipped": False, "channels": out,
            "message": "推送完成：%d/%d 个渠道成功" % (good, len(out))}


def test():
    return send("DockerUpdate v6.0 测试通知", "多渠道通知联动正常", None)


def selfcheck():
    """v6.0-r4：逐渠道列出实际载荷，用于定位「微信端显示不支持类型」。"""
    import notice as _n, re
    cfg = load()
    c = _n.card("DockerUpdate · 通知自检", "这是一条用于核对消息类型的测试内容。",
                "info", name="面板", event="check_done")
    out = []
    for x in cfg.get("channels") or []:
        url = x.get("url") or ""
        ct, data, _ = _payload(x, "DockerUpdate · 通知自检",
                               "这是一条用于核对消息类型的测试内容。", "check_done", "info", "面板")
        try:
            p = json.loads(data.decode())
        except Exception:
            p = {"raw_form": data.decode()[:200]}
        out.append({
            "name": x.get("name") or x.get("type") or "渠道",
            "type": x.get("type") or "webhook", "enabled": bool(x.get("enabled")),
            "url_masked": re.sub(r"(key|token|secret|access_token)=([^&]+)", r"\1=***", url, flags=re.I) or "(未配置)",
            "is_wechat": _n.is_wechat(url, x.get("type") or ""),
            "msgtype": p.get("msgtype") or p.get("msg_type") or "(表单)",
            "has_markdown": "markdown" in p or "md" in str(p.get("desp", "")),
            "payload": p,
        })
    return {"module": _n.MODULE_TAG, "enabled": bool(cfg.get("enabled")), "channels": out}
