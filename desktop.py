"""DockerUpdate v3.0 · 飞牛桌面快捷方式适配器（独立模块，不依赖已移除的应用商店）。

三种模式：
  off      → 不创建（默认）
  file     → 在可写目录写 / 删 <id>.desktop.json
  webhook  → POST create / delete 到你自己的网关或自动化脚本

如实说明：飞牛桌面原生图标是系统私有接口，面板不内置地址、不绕过系统权限；
只有填了快捷方式目录或网关地址，才会真正同步。
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import config
import store as st

CFG_FILE = os.path.join(config.DATA_DIR, "desktop.json")
DEFAULT = {"mode": "off", "file_dir": st.STORE_HOST_ROOT + "/desktop-shortcuts",
           "webhook_url": "", "webhook_token": ""}


class Desktop:
    def __init__(self):
        self.lock = threading.RLock()
        os.makedirs(config.DATA_DIR, exist_ok=True)
        os.makedirs(os.path.join(config.DATA_DIR, "desktop-icons"), exist_ok=True)

    def cfg(self):
        try:
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        for k, v in DEFAULT.items():
            d.setdefault(k, v)
        return d

    def save(self, form):
        c = self.cfg()
        mode = str(form.get("mode") or c["mode"])
        if mode not in ("off", "file", "webhook"):
            raise ValueError("模式只能是 off / file / webhook")
        c["mode"] = mode
        for k in ("file_dir", "webhook_url", "webhook_token"):
            if k in form:
                c[k] = str(form[k] or "").strip()[:300]
        if mode == "file" and c["file_dir"]:
            local = st.host_to_local(c["file_dir"], allow_root=False)
            if not st._under(local, st.STORE_ROOT) and local != st.STORE_ROOT:
                raise ValueError("快捷方式目录必须位于可写根 %s 下" % st.STORE_HOST_ROOT)
        if mode == "webhook" and c["webhook_url"] and not c["webhook_url"].startswith(("http://", "https://")):
            raise ValueError("Webhook 地址必须是完整的 http(s) URL")
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
        return {"ok": True, "message": "桌面图标适配器已保存", "status": self.status()}

    def status(self):
        c = self.cfg()
        mode = c["mode"]
        configured = mode != "off" and bool(c.get("webhook_url") or c.get("file_dir"))
        return {"mode": mode, "configured": configured,
                "file_dir": c["file_dir"], "webhook_url": c["webhook_url"],
                "native_supported": False,
                "hint": "飞牛桌面原生图标使用系统私有接口；选择「快捷方式文件」或填写你自己的网关地址后，"
                        "安装 / 卸载与创建容器会自动同步。未配置时明确显示跳过，不会伪造成功。"}

    def _file(self, app_id):
        c = self.cfg()
        d = st.host_to_local(c["file_dir"] or DEFAULT["file_dir"], allow_root=False)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, st._safe_id(app_id) + ".desktop.json")

    def _post(self, url, token, payload):
        if not url:
            return {"ok": False, "skipped": True, "message": "未配置 Webhook 地址"}
        data = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"X-Token": token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return {"ok": 200 <= r.status < 300, "skipped": False,
                        "message": "网关已响应 %d" % r.status}
        except Exception as exc:
            return {"ok": False, "skipped": False, "message": "网关调用失败：%s" % str(exc)[:160]}

    def create(self, app, installed):
        c = self.cfg()
        mode = c["mode"]
        payload = {"action": "create", "id": app.get("id", ""), "name": app.get("name", ""),
                   "icon": app.get("icon", ""), "url": app.get("homepage", ""),
                   "port": (app.get("ports") or [""])[0],
                   "installed_at": (installed or {}).get("installed_at", time.strftime("%Y-%m-%d %H:%M:%S"))}
        if mode == "off":
            return {"ok": True, "skipped": True, "message": "桌面图标未启用（适配器 off）"}
        if mode == "file":
            with open(self._file(app.get("id", "app")), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return {"ok": True, "skipped": False, "message": "已生成桌面快捷方式文件"}
        return self._post(c["webhook_url"], c["webhook_token"], payload)

    def remove(self, app_id):
        c = self.cfg()
        if c["mode"] == "off":
            return {"ok": True, "skipped": True, "message": "桌面图标未启用"}
        if c["mode"] == "file":
            p = self._file(app_id)
            if os.path.isfile(p):
                os.remove(p)
            return {"ok": True, "skipped": False, "message": "已删除桌面快捷方式文件"}
        return self._post(c["webhook_url"], c["webhook_token"], {"action": "delete", "id": app_id})
