"""DockerUpdate v3.0 全局配置：全部基于 Python 标准库，无任何第三方依赖。"""
import os
from datetime import timedelta, timezone

APP_VERSION = "7.0.4"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
STATE_FILE = os.path.join(DATA_DIR, "state.json")
REGISTRY_AUTH_FILE = os.path.join(DATA_DIR, "registry-auth.json")
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
METRICS_FILE = os.path.join(DATA_DIR, "metrics.json")
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "10000"))

# 宿主机根目录（只读挂载），用于读取 /proc 与磁盘占用；未挂载时自动回退容器内的 /proc
HOST_ROOT = os.getenv("HOST_ROOT", "/host")
# Compose 项目根目录（宿主机视角）。通过 HOST_ROOT 拼接后在容器内只读扫描
COMPOSE_ROOT = os.getenv("COMPOSE_ROOT", "/vol1/1000/docker")
# v3.0 Docker 管理可写根与其对应的宿主机路径。
# 必须挂载到 /store 这样的独立路径，与 /:/host:ro 平级；不能挂在 /host 下面，
# 否则 runc 无法在只读父挂载里创建子挂载点，容器会启动失败。
STORE_ROOT = os.getenv("STORE_ROOT", "/store")
STORE_HOST_ROOT = os.getenv("STORE_HOST_ROOT", "/vol1/1000/docker")
# 磁盘使用率检测点（容器内路径）
DISK_PATHS = tuple(p.strip() for p in os.getenv("DISK_PATHS", "/host,/host/vol1").split(",") if p.strip())

DEFAULT_USERNAME = os.getenv("WEB_USERNAME", "admin")
DEFAULT_PASSWORD = os.getenv("WEB_PASSWORD", "ABab12..")
SESSION_DAYS = max(1, min(int(os.getenv("SESSION_DAYS", "7")), 365))

SELF_NAME = os.getenv("SELF_CONTAINER_NAME", "docker-update")
TZ_OFFSET = float(os.getenv("TZ_OFFSET", "8"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

# 顶部自定义链接（导航站 / 开源地址），留空则不显示
NAV_URL = os.getenv("NAV_URL", "")
SOURCE_URL = os.getenv("SOURCE_URL", "https://gitee.com/jinghui111/docker-center")
# 宿主机局域网 IP（仅用于界面显示，容器内无法可靠探测宿主网卡）
HOST_IP = os.getenv("HOST_IP", "192.168.1.110")

# 资源采样
SAMPLE_INTERVAL = max(3, min(int(os.getenv("SAMPLE_INTERVAL", "3")), 60))
TREND_HOURS = max(6, min(int(os.getenv("TREND_HOURS", "720")), 720))  # 默认保留 30 天小时点，支持「近 30 天」视图


def _bool(key, default):
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(key, default, low, high):
    try:
        v = int(os.getenv(key, str(default)))
    except ValueError:
        return default
    return max(low, min(v, high))


DEFAULT_SETTINGS = {
    "daily_time": os.getenv("DAILY_CHECK_TIME", "03:00"),
    "auto_update": _bool("AUTO_UPDATE_ENABLED", True),
    "include_stopped": _bool("INCLUDE_STOPPED", True),
    "check_on_start": _bool("CHECK_ON_START", True),
    "cleanup_images": _bool("CLEANUP_IMAGES", False),
    "verify_start": _bool("VERIFY_START", True),
    "exclude_watchtower_labels": _bool("EXCLUDE_WATCHTOWER_LABELS", True),
    "backup_on_update": _bool("BACKUP_ON_UPDATE", True),
    "backup_keep": _int("BACKUP_KEEP", 5, 1, 20),
    "check_timeout": _int("CHECK_TIMEOUT", 300, 30, 900),
    "check_retries": _int("CHECK_RETRIES", 1, 0, 3),
    # v2.0：监控与自动刷新
    "monitor_enabled": _bool("MONITOR_ENABLED", True),
    "sample_interval": _int("SAMPLE_INTERVAL", 3, 3, 60),
    "refresh_interval": _int("REFRESH_INTERVAL", 5, 3, 60),
    "show_nav": _bool("SHOW_NAV", True),
    # v3.1：容器端口跳转链接。lan/wan 可带 {port} 占位符（反向代理场景），
    # 不带占位符时面板自动追加 ":宿主机端口"。link_mode=auto 时按当前访问地址 +
    # 内网可达性探测自动选择；lan / wan 为强制。
    "lan_base": os.getenv("LAN_BASE", "http://192.168.1.110"),
    "wan_base": os.getenv("WAN_BASE", ""),
    "link_mode": os.getenv("LINK_MODE", "auto"),
    "link_probe": _bool("LINK_PROBE", True),
    # v3.9：运行日志保留天数（到期自动清除），应用聚合单独的内网 / 外网地址
    "log_retention_days": _int("LOG_RETENTION_DAYS", 7, 1, 365),
    "app_links": {},
    # v5.0：更新后健康检查 + 失败自动回退。默认只校验容器 running，
    # 不主动访问业务端口，避免把「需要登录 / 返回 4xx」误判成服务挂了。
    "health_enabled": _bool("HEALTH_ENABLED", True),
    "health_probe": os.getenv("HEALTH_PROBE", "running"),   # running / http / tcp
    "health_path": os.getenv("HEALTH_PATH", "/"),
    "health_port": _int("HEALTH_PORT", 0, 0, 65535),        # 0 = 自动取第一个映射端口
    "health_wait_s": _int("HEALTH_WAIT_S", 5, 0, 120),
    "health_timeout_s": _int("HEALTH_TIMEOUT_S", 3, 1, 30),
    "health_retries": _int("HEALTH_RETRIES", 2, 0, 10),
    "health_auto_rollback": _bool("HEALTH_AUTO_ROLLBACK", True),
}

SETTING_KEYS = tuple(DEFAULT_SETTINGS.keys())

DEFAULT_ALERTS = {
    "enabled": False,
    "webhook_url": os.getenv("WEBHOOK_URL", ""),
    "cpu_threshold": 85,          # 单容器 CPU 占整机核数百分比
    "mem_threshold": 85,          # 单容器内存使用率百分比
    "disk_threshold": 90,         # 主机磁盘使用率百分比
    "net_threshold": 20480,       # 单容器上下行合计速率阈值（KB/s，20 MB/s）
    "container_down": True,       # 运行中的容器意外退出
    "dedupe_interval": 300,       # 同一对象告警去重秒数
    "history": [],                # 最近 100 条
}
