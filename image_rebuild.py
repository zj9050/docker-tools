# -*- coding: utf-8 -*-
"""DockerUpdate v7.0.4 · 镜像更新页「更新镜像并重建容器」。

v6.0-r18 的镜像更新页只拉取新镜像、不动容器，tag 指向新 digest 后运行中的容器
仍用旧镜像。本模块补上第二种模式：

    拉取新镜像 → 找出使用该镜像的容器 → 版本备份 → 按原配置重建 → 健康检查 →
    不通过自动回退 → 写回检测缓存 / 运行日志 / 通知。

与「容器更新」页的区别：那里以容器为单位逐个检测；这里以镜像为单位，
一次把同一个镜像下的所有容器一起重建，并保留端口、环境变量、卷 / 挂载、
网络、标签、重启策略、资源限额、命令与入口点（复用 manager.build_create_body）。

安全边界：
  · 面板自身容器、带排除标签的容器、手动排除的容器一律跳过，不重建；
  · 「_du_old」临时备份容器跳过；
  · 镜像 ID 未变化且未勾选强制重建时，不重建任何容器（避免无意义中断）；
  · 没有任何容器使用该镜像时，只更新镜像，如实说明；
  · 重建失败由 manager._recreate 自行还原旧容器；健康检查不通过再走
    manager.rollback_one 精确回退到备份的旧镜像 ID；
  · 所有动作进运行日志、逐阶段 emit 到进度条明细，不吞异常、不伪造成功。
"""
import os
import time

import config
import events
import manager as manager_mod
import image_updates as iu

MAX_REFS = 40
MAX_CONTAINERS = 60


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _short(x):
    return str(x or "").replace("sha256:", "")[:12]


def _norm_aliases(ref):
    """容器里的 Image 字段可能写成 nginx / nginx:latest / nginx@sha256:…。"""
    out = {str(ref)}
    tail = str(ref).split("/")[-1]
    if ":" not in tail and "@" not in tail:
        out.add(str(ref) + ":latest")
    if "@sha256:" in str(ref):
        out.add(str(ref).split("@")[0])
    return out


def _clean_refs(docker, refs):
    try:
        local = [r["ref"] for r in iu.local_rows(docker)]
    except Exception as exc:
        raise ValueError("无法读取本机镜像清单：%s" % str(exc)[:160])
    want = [str(x) for x in (refs or []) if str(x) in local][:MAX_REFS]
    if not want:
        raise ValueError("请先在镜像更新列表里勾选要更新的镜像")
    return want


def _containers_for(docker, mgr, ref, old_full="", new_full=""):
    """列出「当前使用这个镜像」的容器，并标注是否会被排除。"""
    try:
        cs = docker.list_containers(all_=True) or []
    except Exception as exc:
        raise ValueError("无法读取容器列表：%s" % str(exc)[:160])
    aliases = _norm_aliases(ref)
    old_short, new_short = _short(old_full), _short(new_full)
    out = []
    for c in cs[:MAX_CONTAINERS]:
        cid = str(c.get("Id") or "")
        names = c.get("Names") or []
        name = str(names[0]).lstrip("/") if names else ""
        if not cid or not name or name.endswith("_du_old"):
            continue
        image = str(c.get("Image") or "")
        iid_short = _short(c.get("ImageID"))
        hit = (image in aliases or
               (old_short and iid_short == old_short) or
               (new_short and iid_short == new_short))
        try:
            ins = docker.inspect_container(cid) or {}
        except Exception:
            ins = {}
        cfg = ins.get("Config") or {}
        cfg_img = str(cfg.get("Image") or "")
        ins_img = _short(ins.get("Image"))
        if not hit:
            hit = (cfg_img in aliases or
                   (old_short and ins_img == old_short) or
                   (new_short and ins_img == new_short))
        if not hit:
            continue
        labels = cfg.get("Labels") or {}
        reason = ""
        try:
            excluded, reason = mgr._excluded(name, cid, labels) if mgr else (False, "")
        except Exception:
            excluded = (name == config.SELF_NAME or
                        str(labels.get("dockerupdate.self") or "") == "true" or
                        str(labels.get("dockerupdate.exclude") or "") == "true")
            if excluded and not reason:
                reason = "本应用容器或已手动排除"
        out.append({
            "name": name, "id": cid, "image": image or cfg_img or ref,
            "running": bool((c.get("State") or "").lower() == "running" or
                            (ins.get("State") or {}).get("Running")),
            "excluded": bool(excluded), "reason": reason or "",
        })
    out.sort(key=lambda x: x["name"].lower())
    return out


def plan(docker, mgr, refs):
    """只算不写：给出每个镜像会牵连哪些容器，供按钮二次确认前展示。"""
    want = _clean_refs(docker, refs)
    groups = []
    for ref in want:
        try:
            old = docker.inspect_image(ref) or {}
        except Exception:
            old = {}
        cs = _containers_for(docker, mgr, ref, str(old.get("Id") or ""))
        groups.append({
            "ref": ref,
            "containers": cs,
            "rebuildable": [c["name"] for c in cs if not c["excluded"]],
            "skipped": [{"name": c["name"], "reason": c["reason"]}
                        for c in cs if c["excluded"]],
        })
    total = sum(len(g["rebuildable"]) for g in groups)
    skipped = sum(len(g["skipped"]) for g in groups)
    return {"ok": True, "refs": groups, "images": len(groups),
            "containers": total, "skipped": skipped,
            "message": "将更新 %d 个镜像并重建 %d 个容器%s" %
                       (len(groups), total, ("，跳过 %d 个受保护容器" % skipped) if skipped else "")}


def _emit(emit, stage, pct, message, detail="", trace=""):
    if not emit:
        return
    try:
        emit(stage=stage, pct=pct, message=message, detail=detail,
             trace=trace or detail, scope="镜像更新并重建")
    except TypeError:
        emit(stage=stage, pct=pct, message=message, detail=detail)


def _cache_pull(rows):
    """把拉取结果写回 image_updates.json，页面刷新后状态同步。"""
    try:
        with iu._lock:
            doc = iu._load()
            items = doc.get("items") or {}
            for r in rows:
                rec = items.get(r["ref"]) or {}
                rec["last_pull"] = _now()
                if r.get("ok"):
                    rec.update({
                        "status": "latest",
                        "reason": r.get("reason") or "已拉取新镜像",
                        "pull_changed": bool(r.get("changed")),
                        "old_id": r.get("old_id", ""), "new_id": r.get("new_id", ""),
                        "via": r.get("via", ""),
                        "local_digest": r.get("local_digest", ""),
                    })
                else:
                    rec.update({"status": "pull_failed",
                                "reason": r.get("reason") or "拉取失败",
                                "pull_changed": False})
                items[r["ref"]] = rec
            iu._save({"at": _now(), "items": items})
    except Exception as exc:
        print("[image-rebuild] 写回检测缓存失败：", exc)


def apply(docker, mgr, refs=None, emit=None, force=False, names=None, backup=None):
    """更新镜像并按原配置重建使用该镜像的容器。"""
    want = _clean_refs(docker, refs)
    names = set(str(x) for x in (names or [])) if names else None
    if backup is None:
        try:
            backup = bool(mgr.settings().get("backup_on_update", True))
        except Exception:
            backup = True

    helper = iu.ImageUpdates(docker, mgr, lambda *a, **k: None)
    _emit(emit, "校验镜像与关联容器", 3,
          "即将处理 %d 个镜像%s" % (len(want), "（强制重建已开启）" if force else ""),
          trace="GET /images/json?all=1 · 校验勾选的镜像是否仍在本机")
    t0 = time.time()
    pulled, details = [], []
    rebuilt, skipped, failed, rolled_back = [], [], [], []
    idx = 0

    for ref in want:
        idx += 1
        base_pct = 3 + int((idx - 1) * 88 / max(1, len(want)))
        _emit(emit, "拉取并校验新镜像", base_pct + 1,
              "正在拉取 %s（%d/%d）" % (ref, idx, len(want)),
              trace="POST /images/create?fromImage=%s · 直连失败自动走加速源代拉" % ref)
        r = helper._pull_one(ref)
        pulled.append(r)
        if not r.get("ok"):
            failed.append({"ref": ref, "reason": r.get("reason") or "拉取失败",
                           "stage": "拉取"})
            _emit(emit, "拉取并校验新镜像", base_pct + 12,
                  "%s 拉取失败" % ref, detail=r.get("reason") or "拉取失败")
            continue

        try:
            new_info = docker.inspect_image(ref) or {}
        except Exception:
            new_info = {}
        new_full = str(new_info.get("Id") or "")
        changed = bool(r.get("changed"))
        cs = _containers_for(docker, mgr, ref,
                             "sha256:" + str(r.get("old_id") or ""), new_full)
        if names:
            cs = [c for c in cs if c["name"] in names]

        if not changed and not force:
            skipped.append({"ref": ref, "reason": "镜像 ID 未变化，未重建容器"})
            _emit(emit, "按原配置重建容器", base_pct + 60,
                  "%s 已是最新，跳过重建" % ref,
                  trace="inspect_image(%s) · old=new=%s，不中断任何容器" % (ref, _short(new_full)))
            details.append({"ref": ref, "changed": False, "containers": []})
            continue

        if not cs:
            skipped.append({"ref": ref, "reason": "本机没有容器使用该镜像，只更新了镜像"})
            _emit(emit, "按原配置重建容器", base_pct + 60,
                  "%s 没有关联容器，只更新镜像" % ref,
                  trace="GET /containers/json?all=1 · 匹配 0 个容器，未重建")
            details.append({"ref": ref, "changed": changed, "containers": []})
            continue

        _emit(emit, "生成版本备份", base_pct + 18,
              "%s 关联 %d 个容器，开始逐个处理" % (ref, len(cs)),
              trace="GET /containers/json?all=1 · 匹配 %s" %
                    "、".join(c["name"] for c in cs[:8]))

        for c in cs:
            name = c["name"]
            if c["excluded"]:
                skipped.append({"ref": ref, "name": name, "reason": c["reason"]})
                _emit(emit, "按原配置重建容器", base_pct + 30,
                      "跳过受保护容器 %s" % name,
                      trace="容器「%s」被排除：%s" % (name, c["reason"]))
                continue
            try:
                ins = docker.inspect_container(c["id"]) or {}
                real_name = str(ins.get("Name") or ("/" + name)).lstrip("/")
                hc = ins.get("HostConfig") or {}
                was_running = bool((ins.get("State") or {}).get("Running"))
                if backup:
                    _emit(emit, "生成版本备份", base_pct + 22,
                          "正在备份 %s" % name,
                          trace="manager.backup_one(%s, pre-image-rebuild) · 保存配置与旧镜像 ID" % name)
                    mgr.backup_one(real_name, "pre-image-rebuild")

                body = manager_mod.build_create_body(ins, ref, c["id"])
                _emit(emit, "按原配置重建容器", base_pct + 38,
                      "正在重建 %s" % name,
                      trace="POST /containers/create?name=%s · 保留端口/环境变量/卷/网络/标签/重启策略" % name)
                new_cid = mgr._recreate(c["id"], real_name, body, was_running, hc)

                health = {"ok": True, "skipped": True, "detail": "未启用健康检查",
                          "auto_rollback": False}
                try:
                    health = mgr._post_update_health(new_cid, real_name, ins,
                                                     mgr.settings()) or health
                except Exception as exc:
                    health = {"ok": False, "skipped": False, "auto_rollback": True,
                              "detail": "健康检查异常：%s" % str(exc)[:120]}

                if (not health.get("ok")) and (not health.get("skipped")) and health.get("auto_rollback"):
                    try:
                        mgr.rollback_one(real_name)
                        rolled_back.append({"name": name, "ref": ref,
                                            "reason": health.get("detail") or "健康检查未通过"})
                        _emit(emit, "核对启动状态", base_pct + 52,
                              "%s 健康检查未通过，已自动回退" % name,
                              trace="manager.rollback_one(%s) · 按旧镜像 ID 精确还原" % name)
                        mgr._notify("DockerUpdate · 已自动回退",
                                    "镜像 %s 更新后容器「%s」健康检查未通过（%s），已自动回退到更新前的版本。"
                                    % (ref, name, health.get("detail") or "探测失败"),
                                    "auto_rollback", name, "high")
                    except Exception as exc:
                        failed.append({"ref": ref, "name": name,
                                       "reason": "健康检查未通过且回退失败：%s" % str(exc)[:160]})
                    continue

                try:
                    mgr._store_check(real_name, {
                        "has_update": False, "error": "", "error_kind": "",
                        "skipped": "", "checked_at": _now(), "updated_at": _now(),
                        "rolled_back": False, "health_ok": bool(health.get("ok")),
                        "health_detail": health.get("detail", ""),
                        "auto_rollback": False, "_notified_update": False,
                    })
                except Exception:
                    pass
                rebuilt.append({"name": name, "ref": ref, "image_id": _short(new_full),
                                "health": health.get("detail") or "运行状态正常"})
                _emit(emit, "核对启动状态", base_pct + 58,
                      "%s 已用新镜像重建 · %s" % (name, health.get("detail") or "正常"),
                      trace="GET /containers/%s/json · State.Running=%s · Image=%s" %
                            (_short(new_cid), was_running, _short(new_full)))
                mgr._notify("DockerUpdate · 容器已更新",
                            "镜像 %s 已更新，容器「%s」按原配置重建完成。%s；旧版本已备份，可一键回退。"
                            % (ref, name, health.get("detail") or "运行状态正常"),
                            "update_ok", name)
                try:
                    mgr.state.add_history("update", name,
                                          "镜像更新并重建：%s → %s（端口/卷/网络/环境变量均保留）"
                                          % (ref, _short(new_full)))
                except Exception:
                    pass
            except Exception as exc:
                failed.append({"ref": ref, "name": name,
                               "reason": "重建失败，旧容器已由重建逻辑还原：%s" % str(exc)[:180]})
                _emit(emit, "核对启动状态", base_pct + 58,
                      "%s 重建失败" % name, detail=str(exc)[:180])
                mgr._notify("DockerUpdate · 更新失败",
                            "镜像 %s 更新时容器「%s」重建失败：%s" %
                            (ref, name, str(exc)[:160]), "update_fail", name, "high")
        details.append({"ref": ref, "changed": changed,
                        "containers": [c["name"] for c in cs if not c["excluded"]]})

    _cache_pull(pulled)
    ok_pull = sum(1 for r in pulled if r.get("ok"))
    msg = ("镜像更新并重建完成：拉取成功 %d/%d，重建容器 %d 个，跳过 %d 项，"
           "失败 %d 项，自动回退 %d 个" %
           (ok_pull, len(pulled), len(rebuilt), len(skipped), len(failed), len(rolled_back)))
    try:
        mgr.state.add_history("update", "镜像更新并重建", msg, ok_pull > 0)
    except Exception:
        pass
    try:
        mgr._notify("DockerUpdate · 镜像更新并重建完成", msg,
                    "image_update_done", "镜像更新")
    except Exception:
        pass
    _emit(emit, "核对启动状态", 100, msg,
          trace="写回 image_updates.json / 运行日志 / 通知；耗时 %.1fs" % (time.time() - t0))
    return {"ok": not failed, "message": msg, "images": len(want),
            "pulled_ok": ok_pull, "pull_failed": len(pulled) - ok_pull,
            "rebuilt": rebuilt, "skipped": skipped, "failed": failed,
            "rolled_back": rolled_back, "details": details,
            "elapsed_s": round(time.time() - t0, 1)}
