"""v2.0 编排层：把「更新中心」与「Docker 管理增强」合并到同一个服务、同一端口、同一套登录。"""
import json
import os
import threading
import time
from datetime import datetime

import config
import events
import runtrace
from alerts import Alerts
from docker_api import DockerError
from monitor import Monitor
from resources import Resources
from sysinfo import HostSampler


def _now():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


class Console:
    def __init__(self, mgr):
        self.mgr = mgr
        self.docker = mgr.docker
        self.host = HostSampler()
        self.monitor = Monitor(self.docker, self.host)
        self.alerts = Alerts()
        self.resources = Resources(self.docker, mgr)
        self.last_alert_check = ""
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        # 启动后先采一次，再按间隔循环；监控关闭时仍提供静态资源列表
        while True:
            try:
                if self.mgr.settings().get("monitor_enabled", True):
                    host = self.monitor.sample_once()
                    raw = self.docker.list_containers(all_=True) or []
                    light = []
                    for c in raw:
                        names = c.get("Names") or []
                        name = names[0].lstrip("/") if names else c.get("Id", "")[:12]
                        light.append({"name": name, "running": str(c.get("State", "")).lower() == "running"})
                    self.alerts.evaluate(self.monitor.host_latest, light, self.monitor.metrics())
                    self.last_alert_check = _now()
            except Exception as exc:
                self.monitor.errors.append(str(exc)[:200])
                self.monitor.errors = self.monitor.errors[-5:]
            time.sleep(max(3, int(self.mgr.settings().get("sample_interval", 3))))

    # ---------- 合并进 /api/state ----------
    CONSOLE_CFG = os.path.join(config.DATA_DIR, "console.json")

    def compose_root(self):
        """v3.3：备份页里可选择的 Compose 根目录，持久化到 data/console.json。"""
        try:
            with open(self.CONSOLE_CFG, encoding="utf-8") as f:
                v = str(json.load(f).get("compose_root") or "").strip()
            if v.startswith("/"):
                return v
        except Exception:
            pass
        return config.COMPOSE_ROOT

    def set_compose_root(self, path):
        p = str(path or "").strip().rstrip("/")
        if not p.startswith("/") or ".." in p.split("/"):
            raise ValueError("请填合法的宿主机绝对路径")
        os.makedirs(os.path.dirname(self.CONSOLE_CFG), exist_ok=True)
        with open(self.CONSOLE_CFG, "w", encoding="utf-8") as f:
            json.dump({"compose_root": p}, f, ensure_ascii=False, indent=2)
        self.resources.invalidate()
        return p

    def backup_overview(self):
        """v3.3：备份页列出所有容器的部署日期 / 运行日期 / 上次快照备份日期。"""
        rows = []
        for c in self.mgr.list_containers():
            bks = c.get("backups") or []
            last = bks[0].get("time", "") if bks else ""
            rows.append({
                "name": c["name"], "alias": c.get("alias", ""),
                "status": c.get("status", ""), "running": c.get("running", False),
                "image": c.get("image_ref", ""),
                "created": c.get("created", ""),
                "started_at": c.get("started_at", ""),
                "last_backup": last, "backup_count": len(bks),
                "policy": c.get("policy", "manual"),
                "ports": c.get("ports", []),
            })
        return {"ok": True, "rows": rows, "compose_root": self.compose_root()}

    def extra_state(self, containers):
        host = dict(self.monitor.host_latest) or self.host.sample()
        metrics = self.monitor.metrics()
        notes = self.mgr.notes()
        apps = self.resources.apps(containers, metrics, notes)
        images = self.resources.images()
        networks = self.resources.networks()
        volumes = self.resources.volumes()
        compose = self.resources.compose_files(self.compose_root())
        alerts = self.alerts.config()
        history = self.alerts.history()
        suggestions = self.monitor.suggestions(containers, images)
        top = self.monitor.top(containers)
        abnormal = sum(1 for a in apps if a["health"] == "error")
        attention = sum(1 for a in apps if a["health"] == "warn")
        try:
            df = self.docker.system_df() or {}
        except DockerError:
            df = {}
        return {
            "console": {
                "host": host,
                "docker_df": {
                    "images": int(((df.get("Images") or {}).get("Total") if isinstance(df.get("Images"), dict) else 0) or 0),
                    "containers": int(((df.get("Containers") or {}).get("Total") if isinstance(df.get("Containers"), dict) else 0) or 0),
                    "volumes": int(((df.get("Volumes") or {}).get("Total") if isinstance(df.get("Volumes"), dict) else 0) or 0),
                    "images_size": round(float((df.get("Images") or {}).get("Size", 0) if isinstance(df.get("Images"), dict) else 0) / 1048576, 1),
                    "reclaimable": round(float((df.get("Images") or {}).get("Reclaimable", 0) if isinstance(df.get("Images"), dict) else 0) / 1048576, 1),
                },
                "metrics": metrics,
                "trend": self.monitor.trend(),
                "monitor": self.monitor.summary(),
                "apps": apps,
                "app_stats": {
                    "total": len(apps), "abnormal": abnormal, "attention": attention,
                    "running_apps": sum(1 for a in apps if a["running"] == a["count"]),
                },
                "images": images,
                "networks": networks,
                "volumes": volumes,
                "compose_files": compose,
                "compose_root": self.compose_root(),
                "top": top,
                "suggestions": suggestions,
                "alerts": alerts,
                "alert_history": history,
                "alert_summary": {
                    "enabled": bool(alerts.get("enabled")),
                    "recent": len(history),
                    "high": sum(1 for h in history if h.get("level") == "high"),
                    "last_check": self.last_alert_check,
                },
                "links": {"nav": config.NAV_URL, "source": config.SOURCE_URL,
                          "show_nav": bool(self.mgr.settings().get("show_nav", True))},
            }
        }

    # ---------- 详情 / 日志 ----------
    def detail(self, name):
        d = self.resources.container_detail(name)
        d["series"] = self.monitor.series_for(name)
        note = self.mgr.notes().get(name) or {}
        d["alias"] = note.get("alias", "")
        d["memo"] = note.get("memo", "")
        return d

    def logs(self, name, tail=200):
        return self.resources.logs(name, tail)

    def container_config(self, name):
        cid, real = self.mgr._resolve(name)
        ins = self.docker.inspect_container(cid) or {}
        return {"ok": True, "name": real,
                "json": json.dumps(ins, ensure_ascii=False, indent=2)}

    # ---------- 操作 ----------
    def action(self, kind, body):
        runtrace.note("Console.action", "调用 kind=%s · %s" %
                      (kind, runtrace.brief(body)), "step")
        r = self.resources
        if kind == "compose-scan":
            root = self.set_compose_root(str(body.get("path") or ""))
            files = r.compose_files(root)
            r.invalidate()
            return {"ok": True, "compose_root": root,
                    "files": [{"path": f["path"], "size": f["size"], "mtime": f["mtime"]} for f in files],
                    "message": "Compose 根目录已设为 %s，扫描到 %d 个文件" % (root, len(files))}
        if kind == "backup-overview":
            return self.backup_overview()
        if kind == "container":
            name = str(body.get("name") or "")
            act = str(body.get("action") or "")
            msg = r.container_action(body.get("name"), body.get("action"))
            emap = {"start": ("container_started", "容器启动", "info"),
                    "stop": ("container_stopped", "容器停止", "mid"),
                    "kill": ("container_stopped", "容器停止", "mid"),
                    "restart": ("container_restarted", "容器重启", "info"),
                    "pause": ("container_paused", "容器暂停", "info"),
                    "unpause": ("container_unpaused", "容器恢复运行", "info"),
                    "remove": ("container_removed", "删除容器", "mid")}
            if act in emap:
                ev, label, lvl = emap[act]
                try:
                    events.emit(ev, label, "容器「%s」%s（由面板操作触发）" % (name, label), lvl, name)
                except Exception:
                    pass
            return {"ok": True, "message": msg}
        if kind == "app":
            containers = self.mgr.list_containers()
            return {"ok": True, "message": r.app_action(body.get("key"), body.get("action"), containers)}
        if kind == "limits":
            msg = r.set_limits(body.get("name"), body.get("cpu_cores"), body.get("mem_mb"))
            try:
                events.emit("container_limits", "资源限额变更",
                            "容器「%s」的 CPU / 内存上限已更新" % str(body.get("name") or ""),
                            "info", str(body.get("name") or ""), dedupe=False)
            except Exception:
                pass
            return {"ok": True, "message": msg}
        if kind == "create-container":
            msg = r.create_container(body)
            try:
                events.emit("container_created", "部署创建容器",
                            "已通过面板创建容器「%s」" % (str(body.get("name") or "新容器")),
                            "info", str(body.get("name") or "新容器"), dedupe=False)
            except Exception:
                pass
            return {"ok": True, "message": msg}
        if kind == "network":
            return {"ok": True, "message": r.network_action(body.get("action"), body)}
        if kind == "volume":
            return {"ok": True, "message": r.volume_action(body.get("action"), body)}
        if kind == "image":
            act = body.get("action")
            if act == "pull":
                ref = str(body.get("ref") or "")

                def job():
                    try:
                        msg = r.image_action("pull", body)
                        self.mgr.state.add_history("image", ref, msg)
                        events.emit("image_pull", "镜像拉取完成",
                                    "镜像「%s」已拉取到本机：%s" % (ref, msg), "info", ref)
                    except Exception as exc:
                        events.emit("image_pull_fail", "镜像拉取失败",
                                    "镜像「%s」拉取失败：%s" % (ref, str(exc)[:180]),
                                    "high", ref)
                        raise

                ok, why = self.mgr._start_job("image-pull", "正在拉取镜像 %s…" % ref, job)
                return {"ok": ok, "message": why or "镜像拉取任务已提交，可在顶部进度条查看", "async": True}
            return {"ok": True, "message": r.image_action(act, body)}
        if kind == "alerts-config":
            cfg = self.alerts.update(body)
            events.emit("settings_changed", "设置变更",
                        "告警规则 / 消息推送事件开关已保存（启用 %d / %d 项）"
                        % (cfg.get("event_enabled_count", 0), cfg.get("event_count", 0)),
                        "info", "面板", dedupe=False)
            return {"ok": True, "message": "告警设置已保存", "alerts": cfg}
        if kind == "alerts-test":
            try:
                r = self.alerts.test(str(body.get("event") or "") or None)
            except ValueError as exc:
                r = {"ok": False, "skipped": True, "message": str(exc),
                     "event": str(body.get("event") or "") or "check_done", "channels": []}
            return {"ok": bool(r.get("ok")), "message": r.get("message") or "测试完成",
                    "skipped": bool(r.get("skipped")), "event": r.get("event"),
                    "result": r}
        if kind == "alerts-selfcheck":
            # v6.0-r4：让用户一眼确认「容器里跑的是不是新代码」和「实际发出去的载荷」。
            import notify as _notify, notice as _notice
            return {"ok": True, "selfcheck": {
                "module": _notice.MODULE_TAG,
                "app_version": config.APP_VERSION,
                "alert_webhook": self.alerts.selfcheck(),
                "channels": _notify.selfcheck(),
            }}
        if kind == "alerts-clear":
            with self.alerts.lock:
                self.alerts.data["history"] = []
                self.alerts._save()
            return {"ok": True, "message": "告警历史已清空"}
        if kind == "refresh":
            r.invalidate()
            return {"ok": True, "message": "资源列表已刷新"}
        raise ValueError("未知操作：%s" % kind)

    def import_backup(self, raw, restore_compose=False, compose_dir=""):
        msg = self.resources.import_backup(raw, restore_compose=restore_compose,
                                           compose_dir=compose_dir)
        # 恢复后重新载入告警与指标数据
        self.alerts = Alerts()
        return {"ok": True, "message": msg}
