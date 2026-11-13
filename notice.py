# -*- coding: utf-8 -*-
"""DockerUpdate v6.0-r14 · 统一通知排版（飞牛原生消息样式）。

v6.0-r9 变更：按用户提供的 fnOS「登录成功 / 退出登录」通知截图重做。

    👋 退出登录通知
    2026-11-11 11:56:13
    用户名: admin
    IP地址: 127.0.0.1
    用户已安全退出系统。

DockerUpdate 对应输出：

    📦 新增容器
    2026-11-11 11:56:13
    检测到新容器「fd6f41c393ff_docker-update」已出现在本机 Docker 中

三条硬要求：
  ① 标题里**不再出现 DockerUpdate 品牌名**，直接就是事件本身；
  ② 开头一个图标/标志，**按消息（事件）类型不同而不同**，不是按级别配色；
  ③ 版面像 fnOS 原生通知：图标标题 → 完整时间戳 → 纯文本详情，
     无【】标签行、无「· 」项目符号、无空行、无页脚、无「信息 · 11-10」这类自造标签。

历史 BUG 不能重现（v6.0-r2~r4）：
  · 企业微信 / 微信端只支持 msgtype=text，markdown 会显示
    「暂不支持此消息类型，点击前往企业微信查看」；
  · 所有渠道统一走 build_payload()，不再各写一份；
  · 自建 Webhook / 中转网关的 JSON 里绝不能出现 markdown 键，
    否则部分网关会把整条转成 markdown 消息；
  · 全渠道共用 c["wechat"] 这一份文本，不维护第二套排版；
  · MODULE_TAG 用于「通知自检」，一眼确认容器里跑的是不是新代码。

兼容字段 title / text / content / level / time / plain / badge 全部保留，
既有网关与自建接收端不用改。
"""
import re
from datetime import datetime

import config

# 跟随 config.APP_VERSION，避免每次改版都要手改这里导致「通知自检」误报旧代码
MODULE_TAG = "v" + config.APP_VERSION

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:                                   # pragma: no cover
    _TZ = None

LEVELS = {
    "high": ("🔴", "高危", "#ff6b7d"),
    "mid":  ("🟠", "注意", "#ffb454"),
    "info": ("🔵", "信息", "#59d8ff"),
    "ok":   ("🟢", "成功", "#4ade9b"),
}

# ── 按「消息类型」选图标（不是按级别）──────────────────────────────
# 键为 events.py 里的事件 id；中文名兜底见 _ICON_BY_WORD。
EVENT_ICONS = {
    # v6.0-r24：容器目录映射
    "mount_applied": "🔗",
    "mount_inherit": "🧩",
    "mount_fail": "⚠️",
    # 更新与版本
    "found_update":        "🆙",
    "update_ok":           "✅",
    "update_fail":         "❌",
    "auto_rollback":       "↩️",
    "rollback_ok":         "↩️",
    "rollback_fail":       "❌",
    "backup_ok":           "💾",
    "check_fail":          "⚠️",
    "check_done":          "🔍",
    "image_update_found":  "🆙",
    "image_update_done":   "⬇️",
    # 容器生命周期
    "container_added":     "📦",
    "container_removed":   "🗑️",
    "container_started":   "▶️",
    "container_stopped":   "⏹️",
    "container_restarted": "🔄",
    "container_down":      "⚠️",
    "container_created":   "🚀",
    "container_paused":    "⏸️",
    "container_unpaused":  "▶️",
    "container_limits":    "⚙️",
    "image_pull":          "⬇️",
    "image_pull_fail":     "❌",
    # 资源与阈值
    "cpu_high":            "🔥",
    "mem_high":            "🧠",
    "disk_high":           "💽",
    "net_high":            "🌐",
    # 网络连通性（v6.0-r17）
    "net_down":            "📴",
    "net_up":              "📶",
    # 面板与系统
    "login_fail":          "🚫",
    "login_ok":            "🔑",
    "password_changed":    "🔐",
    "settings_changed":    "⚙️",
    "task_ok":             "⏰",
    "task_fail":           "❌",
    "panel_backup":        "💾",
    "accel_switch":        "🚀",
}

# 中文事件名关键词兜底（有些调用直接传中文 title，没带 event id）
_ICON_BY_WORD = (
    ("登录失败", "🚫"), ("登录成功", "🔑"), ("退出登录", "👋"),
    ("密码", "🔐"), ("新增容器", "📦"), ("删除容器", "🗑️"),
    ("部署创建", "🚀"), ("意外退出", "⚠️"), ("暂停", "⏸️"),
    ("恢复运行", "▶️"), ("容器启动", "▶️"), ("容器停止", "⏹️"),
    ("容器重启", "🔄"), ("资源限额", "⚙️"), ("拉取失败", "❌"),
    ("拉取完成", "⬇️"), ("可更新", "🆙"), ("更新成功", "✅"),
    ("更新失败", "❌"), ("自动更新失败", "❌"), ("回退", "↩️"),
    ("备份", "💾"), ("恢复", "💾"), ("巡检完成", "🔍"),
    ("检测失败", "⚠️"), ("CPU", "🔥"), ("内存", "🧠"),
    ("磁盘", "💽"), ("网络流量", "🌐"), ("外网断网", "📴"), ("网络异常", "📴"),
    ("外网恢复", "📶"), ("补报", "📴"), ("任务失败", "❌"),
    ("任务执行成功", "⏰"), ("设置变更", "⚙️"), ("加速线路", "🚀"),
)

# 企业微信 text.content 上限 2048 字节，留出余量
WECHAT_MAX = 1500
_FULL_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def now():
    try:
        return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S") if _TZ else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def short(stamp=""):
    """长时间戳缩成 11-10 15:14，供 JSON 兼容字段使用。"""
    s = str(stamp or now())
    m = re.match(r"\d{4}-(\d{2}-\d{2} \d{2}:\d{2})", s)
    return m.group(1) if m else s[:16]


def head(title):
    """去掉重复前缀 / 裸级别标记，得到纯「事件名」。

    历史调用里出现过三种脏标题：叠了两层 "DockerUpdate · "、
    旧告警兼容层传 "[HIGH] 容器名"、以及 "[INFO] xxx"。
    r8 的标题要「直接就是内容」，所以这些都在这里抹掉。
    """
    t = str(title or "面板通知").strip()
    for _ in range(4):
        n = re.sub(r"^\[?DockerUpdate\]?\s*[·\-:：]?\s*", "", t, count=1, flags=re.I)
        n = re.sub(r"^\[(?:HIGH|MID|INFO|OK|WARN|ERROR)\]\s*", "", n, count=1, flags=re.I)
        if n == t:
            break
        t = n
    return t.strip() or "面板通知"


def pick_icon(ev, event="", level="info"):
    """按消息类型挑图标：事件 id → 中文关键词 → 级别兜底。"""
    k = str(event or "").strip().lower()
    if k in EVENT_ICONS:
        return EVENT_ICONS[k]
    for word, icon in _ICON_BY_WORD:
        if word in ev:
            return icon
    lv = level if level in LEVELS else "info"
    return LEVELS[lv][0] if lv in ("high", "mid") else "🔔"


def _clip(core, limit=WECHAT_MAX):
    """按 UTF-8 字节裁剪：**标题行 + 完整时间戳必须保留**。

    r7 的时间戳在末尾，r8 移到第二行，所以裁剪策略跟着改：
    固定保留前两行，只截详情，避免把最像飞牛原生的那行时间戳切掉。
    """
    if len(core.encode("utf-8")) <= limit:
        return core
    lines = core.split("\n")
    fixed = "\n".join(lines[:2])
    tail = "\n".join(lines[2:])
    suffix = "…（详情已截断）"
    room = max(limit - len(("\n" + fixed).encode("utf-8")) - len(suffix.encode("utf-8")), 96)
    cut = tail.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip()
    return fixed + "\n" + cut + suffix


def card(title, text, level="info", name="", event="", stamp=""):
    """返回统一排版结果；所有渠道共用 c["wechat"] 这一份文本。"""
    lv = level if level in LEVELS else "info"
    icon, label, color = LEVELS[lv]
    ev = head(title)
    msg = str(text or "").strip()
    obj = str(name or "").strip() or "面板"
    ts = now() if not stamp else str(stamp)
    full = ts if _FULL_TS.match(ts) else now()

    # ① 标题：事件图标 + 事件名，无品牌名、无级别文字、无时间戳
    ev_icon = pick_icon(ev, event, lv)
    line1 = "%s %s" % (ev_icon, ev)
    # ② 详情：纯行。只有对象名没出现在正文里时才补一行「对象: xxx」，避免重复
    raw = [x.strip() for x in msg.splitlines() if x.strip()] or ["%s 发生了一次事件" % obj]
    detail = list(raw)
    if obj != "面板" and not any(obj in x for x in detail):
        detail.insert(0, "对象: %s" % obj)
    # ③ 组装：图标标题 → 完整时间戳 → 详情，无空行
    core = "%s\n%s\n%s" % (line1, full, "\n".join(detail))
    wechat = _clip(core)

    return {
        "head": ev,                     # 纯事件名，不含品牌
        "line1": line1,                  # 供 Bark / Server 酱当标题
        "icon": ev_icon,
        "badge": "%s %s" % (icon, label),
        "event_name": ev,
        "level": lv,
        "level_label": label,
        "level_color": color,
        "object": obj,
        "text": msg,
        "time": ts,
        "time_full": full,
        "wechat": wechat,
        "plain": wechat,
        "markdown": wechat,             # 全渠道同一份文案，不再第二套排版
        "content": wechat,
        # 兼容字段
        "title": line1,
    }


def is_wechat(url="", ctype=""):
    """宽松判定「这条消息最终会落到微信端」。"""
    u = str(url or "").lower()
    t = str(ctype or "").lower()
    return (t == "wecom" or
            "qyapi.weixin.qq.com" in u or "work.weixin.qq.com" in u or
            "weixin.qq.com" in u or "wechat" in u or "wecom" in u)


def wechat_payload(c):
    """企业微信机器人 / 微信端唯一安全的载荷：只发标准 text 字段。"""
    return {"msgtype": "text", "text": {"content": c["wechat"]}}


def plain_payload(c):
    """自建 Webhook / 中转网关：纯 text，绝不带 markdown 键。"""
    return {
        "msgtype": "text",
        "text": {"content": c["wechat"]},
        "title": c["head"], "badge": c["badge"], "level": c["level"],
        "level_label": c["level_label"], "object": c["object"],
        "event": c["event_name"], "source": "DockerUpdate",
        "time": c["time"], "content": c["wechat"],
        "wechat": c["wechat"], "plain": c["plain"], "text_plain": c["wechat"],
        # 注意：刻意不带 markdown 键——部分中转网关看到它会把整条转成
        # markdown 消息，微信端又会显示「暂不支持此消息类型」。
    }


def build_payload(url="", ctype="", c=None):
    """统一入口：所有发送方都必须走这里，保证微信端一定能显示。"""
    if is_wechat(url, ctype):
        return wechat_payload(c)
    return plain_payload(c)
