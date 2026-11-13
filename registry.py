"""DockerUpdate v2.1-r12 · 镜像仓库模块（Docker 管理分区第 9 个页面）。

对齐飞牛 Docker「镜像仓库」页：搜索 Docker Hub / 自定义源、看图标 · 星标 · 下载量 ·
简介、分页、选标签拉取、拉取后直接创建容器，并同步飞牛桌面图标。

如实说明的边界：
  · Docker 镜像的**实际存储位置**由 Docker daemon 的 data-root 决定，面板无法修改；
    这里可改的是「容器配置文件 / Compose 落盘目录」与「容器数据卷根目录」。
  · Docker Hub 公开搜索接口是 hub.docker.com/v2/search/repositories，私有仓库没有
    标准关键词搜索（Registry V2 只有 _catalog 列名 + tags/list），因此自定义源分三类。
  · Docker Hub 搜索结果不返回官方 logo，所以无 logo 的镜像由面板生成品牌字母 SVG，
    保证「每个镜像都有图标」且离线可见；源若提供 logo_url 则优先走白名单代理。
"""
import base64
import hashlib
import json
import os
import re
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import urllib.parse
import urllib.request

import config
import runtrace
import desktop as desktop_mod
import store as st
from docker_api import DockerError, UnixHTTPConnection
from jobs import JOBS

CFG_FILE = os.path.join(st.STORE_DIR, "registry.json")
ICON_DIR = os.path.join(st.STORE_DIR, "registry-icons")

DEFAULT_SETTINGS = {
    "container_root": st.STORE_HOST_ROOT + "/registry-containers",
    "data_root": st.STORE_HOST_ROOT + "/registry-data",
    "default_tag": "latest",
    "page_size": 20,
    "auto_desktop": True,
    "pull_timeout": 600,
    "restart": "unless-stopped",
    "network": "bridge",
}

BUILTIN = {
    "id": "dockerhub", "name": "Docker Hub 官方", "type": "hub",
    "url": "https://hub.docker.com/v2", "enabled": True, "builtin": True,
    "desc": "公开镜像目录：官方镜像在 library 命名空间，无需登录即可搜索元数据",
}

# v3.1：飞牛 Docker「镜像仓库」页同款目录（图 1 的 nginx / memcached / busybox / alpine /
# postgres / redis / ubuntu / python 等）。这是随面板内置的**离线快照**，不依赖 hub.docker.com：
# 国内直连超时也能看到完整列表、图标、下载按钮与项目地址；联网后可切回 Docker Hub 实时
# 2500+ 项逐页浏览。星标 / 下载量为目录快照参考值，联网时以实时数据为准。
BUILTIN_FNOS = {
    "id": "fnos", "name": "飞牛官方镜像目录（内置）", "type": "builtin",
    "url": "", "enabled": True, "builtin": True,
    "desc": "飞牛 Docker 镜像仓库同款官方/热门镜像，离线可用；联网可切 Docker Hub 实时目录",
}

FNOS_OFFICIAL = [
    ("nginx", "Official build of Nginx.", "1B+", 21419, "nginx"),
    ("memcached", "Free & open source, high-performance, distributed memory object caching system.", "1B+", 2474, "memcached"),
    ("busybox", "Busybox base image.", "1B+", 3525, "busybox"),
    ("alpine", "A minimal Docker image based on Alpine Linux with a complete package index and only 5 MB in size!", "1B+", 11583, "alpine"),
    ("postgres", "The PostgreSQL object-relational database system provides reliability and data integrity.", "1B+", 15051, "postgres"),
    ("redis", "Redis is the world's fastest data platform for caching, vector search, and NoSQL databases.", "1B+", 13633, "redis"),
    ("ubuntu", "Ubuntu is a Debian-based Linux operating system based on free software.", "1B+", 17878, "ubuntu"),
    ("python", "Python is an interpreted, interactive, object-oriented, open-source programming language.", "1B+", 10546, "python"),
    ("mysql", "MySQL 官方关系型数据库，最流行的开源数据库之一", "1B+", 13000, "mysql"),
    ("mariadb", "MariaDB Server：高性能开源关系型数据库", "1B+", 5200, "mariadb"),
    ("mongo", "MongoDB 文档数据库，提供高可用与易扩展能力", "1B+", 9900, "mongo"),
    ("node", "Node.js：基于 JavaScript 的服务端与网络应用平台", "1B+", 12000, "node"),
    ("php", "PHP：广泛用于 Web 开发的通用脚本语言", "1B+", 6600, "php"),
    ("httpd", "The Apache HTTP Server Project.", "1B+", 4300, "apache"),
    ("tomcat", "Apache Tomcat：Java Servlet / JSP 开源实现", "500M+", 3300, "tomcat"),
    ("debian", "Debian：完全由自由软件组成的 Linux 发行版", "1B+", 5200, "debian"),
    ("centos", "CentOS：企业级 Linux 发行版（旧版镜像）", "500M+", 3800, "centos"),
    ("fedora", "Fedora：提供最新自由开源软件的 Linux 发行版", "100M+", 1100, "fedora"),
    ("archlinux", "Arch Linux：轻量、灵活的滚动更新发行版", "100M+", 1600, "arch"),
    ("amazonlinux", "Amazon Linux 2 / 2023 官方镜像", "100M+", 900, "amazon"),
    ("hello-world", "Hello World：最小化 Docker 示例镜像", "100M+", 2200, "docker"),
    ("registry", "Docker Registry：存储与分发镜像的服务端", "100M+", 3900, "docker"),
    ("openjdk", "OpenJDK 官方 Java 运行时 / JDK 镜像", "500M+", 3600, "java"),
    ("golang", "Go（golang）：开源编译型编程语言", "500M+", 3600, "go"),
    ("ruby", "Ruby：动态、开源的编程语言", "500M+", 2400, "ruby"),
    ("rust", "Rust：兼顾性能与安全的系统编程语言", "100M+", 1500, "rust"),
    ("maven", "Apache Maven：Java 项目构建与依赖管理", "100M+", 1200, "java"),
    ("gradle", "Gradle：通用构建自动化工具", "100M+", 1000, "java"),
    ("composer", "Composer：PHP 依赖管理", "100M+", 700, "php"),
    ("caddy", "Caddy 2：自动 HTTPS 的现代 Web 服务器", "100M+", 2900, "caddy"),
    ("traefik", "Traefik：现代 HTTP 反向代理与负载均衡", "500M+", 4200, "traefik"),
    ("haproxy", "HAProxy：高性能 TCP / HTTP 负载均衡", "100M+", 1900, "haproxy"),
    ("jc21/nginx-proxy-manager", "Nginx Proxy Manager：可视化反向代理与 SSL 证书管理", "100M+", 1800, "nginx"),
    ("certbot/certbot", "Certbot：自动申请 Let's Encrypt 证书", "100M+", 1200, "certbot"),
    ("influxdb", "InfluxDB：开源时序数据库", "500M+", 2200, "influx"),
    ("clickhouse", "ClickHouse：开源列式分析型数据库", "100M+", 1900, "clickhouse"),
    ("elasticsearch", "Elasticsearch：分布式 RESTful 搜索与分析引擎", "1B+", 5300, "elastic"),
    ("kibana", "Kibana：Elasticsearch 数据可视化", "500M+", 1900, "elastic"),
    ("logstash", "Logstash：服务端数据处理管道", "500M+", 1400, "elastic"),
    ("couchdb", "Apache CouchDB：面向文档的 NoSQL 数据库", "100M+", 900, "couch"),
    ("neo4j", "Neo4j：图数据库", "100M+", 1500, "neo4j"),
    ("rabbitmq", "RabbitMQ：开源消息中间件", "500M+", 3300, "rabbit"),
    ("zookeeper", "Apache ZooKeeper：分布式协调服务", "100M+", 1200, "zookeeper"),
    ("cassandra", "Apache Cassandra：高可扩展 NoSQL 数据库", "100M+", 1200, "cassandra"),
    ("adminer", "Adminer：单文件数据库管理前端", "100M+", 1200, "adminer"),
    ("phpmyadmin", "phpMyAdmin：MySQL / MariaDB Web 管理", "100M+", 1500, "phpmyadmin"),
    ("portainer/portainer-ce", "Portainer CE：Docker / Kubernetes 可视化管理", "500M+", 3200, "portainer"),
    ("grafana/grafana", "Grafana：指标与日志可视化平台", "500M+", 3400, "grafana"),
    ("prom/prometheus", "Prometheus：开源监控与告警系统", "500M+", 4600, "prometheus"),
    ("prom/node-exporter", "Prometheus Node Exporter：主机指标采集", "100M+", 1100, "prometheus"),
    ("netdata/netdata", "Netdata：实时全栈监控", "100M+", 1700, "netdata"),
    ("amir20/dozzle", "Dozzle：轻量实时容器日志查看器", "100M+", 1600, "dozzle"),
    ("louislam/uptime-kuma", "Uptime Kuma：自托管站点可用性监控", "100M+", 2600, "uptime"),
    ("containrrr/watchtower", "Watchtower：自动更新运行中的容器", "100M+", 1900, "watchtower"),
    ("crazymax/diun", "DIUN：镜像更新通知工具", "50M+", 900, "diun"),
    ("jellyfin/jellyfin", "Jellyfin：开源媒体服务器", "100M+", 2600, "jellyfin"),
    ("emby/embyserver", "Emby Server：媒体库与串流服务", "100M+", 1000, "emby"),
    ("linuxserver/qbittorrent", "qBittorrent：BT 下载客户端（含 WebUI）", "100M+", 1200, "qbittorrent"),
    ("linuxserver/transmission", "Transmission：轻量 BT 下载", "100M+", 800, "transmission"),
    ("linuxserver/sonarr", "Sonarr：剧集自动追更与整理", "100M+", 900, "sonarr"),
    ("linuxserver/radarr", "Radarr：电影自动追更与整理", "100M+", 900, "radarr"),
    ("linuxserver/lidarr", "Lidarr：音乐自动整理", "50M+", 600, "lidarr"),
    ("linuxserver/jackett", "Jackett：下载索引器代理", "100M+", 800, "jackett"),
    ("linuxserver/bazarr", "Bazarr：字幕自动下载", "50M+", 600, "bazarr"),
    ("homeassistant/home-assistant", "Home Assistant：开源智能家居中枢", "100M+", 2600, "homeassistant"),
    ("adguard/adguardhome", "AdGuard Home：全网广告与 DNS 过滤", "100M+", 2100, "adguard"),
    ("pihole/pihole", "Pi-hole：DNS 级广告拦截", "100M+", 2300, "pihole"),
    ("linuxserver/heimdall", "Heimdall：应用导航主页", "50M+", 900, "heimdall"),
    ("nextcloud", "Nextcloud：自托管网盘与协作平台", "500M+", 3200, "nextcloud"),
    ("wordpress", "WordPress：博客与内容管理系统", "1B+", 6400, "wordpress"),
    ("vaultwarden/server", "Vaultwarden：轻量 Bitwarden 密码库服务端", "100M+", 2300, "vaultwarden"),
    ("syncthing/syncthing", "Syncthing：去中心化文件同步", "100M+", 1500, "syncthing"),
    ("filebrowser/filebrowser", "File Browser：Web 文件管理器", "100M+", 1200, "filebrowser"),
    ("duplicati/duplicati", "Duplicati：加密增量备份", "50M+", 800, "duplicati"),
    ("minio/minio", "MinIO：高性能 S3 兼容对象存储", "500M+", 3300, "minio"),
    ("gitea/gitea", "Gitea：轻量自托管 Git 服务", "100M+", 2200, "gitea"),
    ("gitlab/gitlab-ce", "GitLab CE：完整 DevOps 平台", "100M+", 2300, "gitlab"),
    ("jenkins/jenkins", "Jenkins：持续集成 / 持续交付", "500M+", 2400, "jenkins"),
    ("sonarqube", "SonarQube：代码质量与安全扫描", "100M+", 1100, "sonarqube"),
    ("codercom/code-server", "code-server：浏览器里的 VS Code", "100M+", 1700, "code"),
    ("onlyoffice/documentserver", "ONLYOFFICE Docs：在线文档协作", "100M+", 900, "onlyoffice"),
    ("collabora/code", "Collabora Online：LibreOffice 在线协作", "50M+", 700, "collabora"),
    ("rustdesk/rustdesk-server", "RustDesk Server：自建远程桌面中继", "100M+", 1400, "rustdesk"),
    ("snowdreamtech/frps", "frp 服务端：内网穿透", "50M+", 700, "frp"),
    ("jeessy/ddns-go", "ddns-go：动态域名解析", "50M+", 900, "ddns"),
    ("xhofe/alist", "AList：多网盘聚合挂载", "100M+", 1600, "alist"),
    ("gdy666/lucky", "Lucky：端口转发 / 反向代理 / 动态域名", "50M+", 700, "lucky"),
    ("linuxserver/wireguard", "WireGuard：轻量 VPN 服务端", "100M+", 900, "wireguard"),
    ("cloudflare/cloudflared", "cloudflared：Cloudflare Tunnel 隧道", "100M+", 1200, "cloudflare"),
    ("aquasec/trivy", "Trivy：容器镜像漏洞扫描", "100M+", 1500, "trivy"),
    ("eclipse-mosquitto", "Eclipse Mosquitto：MQTT 消息代理", "100M+", 1100, "mqtt"),
    ("redis/redis-stack", "Redis Stack：带搜索 / JSON / 时序能力的 Redis", "50M+", 700, "redis"),
    ("photoprism/photoprism", "PhotoPrism：AI 照片管理与检索", "50M+", 1400, "photoprism"),
]

# 品牌色板：每个镜像都有「相应」的专属图标（离线矢量 SVG，不依赖外网）。
# (渐变起色, 渐变止色, 缩写, 形状)
BRANDS = {
    "nginx": ("#009639", "#68b445", "N", "hex"),
    "memcached": ("#32b6c9", "#1f7f9c", "M", "stack"),
    "busybox": ("#8d6e4b", "#c49a67", "B", "box"),
    "alpine": ("#0d5fa6", "#3b9bd6", "Al", "mount"),
    "postgres": ("#316192", "#5b9bd5", "PG", "circle"),
    "redis": ("#d82c20", "#f4693c", "R", "stack"),
    "ubuntu": ("#e95420", "#f9a03f", "U", "ring"),
    "python": ("#306998", "#ffd43b", "Py", "wave"),
    "mysql": ("#00758f", "#f29111", "My", "circle"),
    "mariadb": ("#003545", "#1f7a8c", "Ma", "circle"),
    "mongo": ("#13aa52", "#4db33d", "Mo", "mount"),
    "node": ("#3c873a", "#68b445", "JS", "hex"),
    "php": ("#4f5b93", "#8892bf", "PHP", "box"),
    "apache": ("#d22128", "#f0603a", "A", "wave"),
    "tomcat": ("#f8dc75", "#d89b2c", "T", "circle"),
    "debian": ("#a80030", "#d94062", "De", "ring"),
    "centos": ("#932279", "#e0699e", "Ce", "box"),
    "fedora": ("#294172", "#3c6eb4", "Fe", "circle"),
    "arch": ("#1793d1", "#4fb8e8", "Ar", "mount"),
    "amazon": ("#232f3e", "#ff9900", "Aw", "box"),
    "docker": ("#0db7ed", "#2496ed", "D", "wave"),
    "java": ("#e76f00", "#f4a11d", "J", "circle"),
    "go": ("#00add8", "#5dc9e2", "Go", "wave"),
    "ruby": ("#cc342d", "#e86a58", "Rb", "hex"),
    "rust": ("#3a2b20", "#b8783c", "Rs", "box"),
    "caddy": ("#1f9fe0", "#42c5a8", "C", "wave"),
    "traefik": ("#24a1c1", "#5cc6d0", "Tr", "hex"),
    "haproxy": ("#1062a4", "#3b9bd6", "HA", "circle"),
    "certbot": ("#2b6cb0", "#63b3ed", "Cb", "ring"),
    "influx": ("#22adf6", "#6cd2f6", "In", "wave"),
    "clickhouse": ("#ffcc00", "#ffe066", "CH", "box"),
    "elastic": ("#00bfb3", "#fec514", "El", "stack"),
    "couch": ("#e33b2e", "#f47a5a", "Co", "circle"),
    "neo4j": ("#018bff", "#5bb3f5", "N4", "circle"),
    "rabbit": ("#ff6600", "#ffa64d", "Rb", "stack"),
    "zookeeper": ("#d5a419", "#f0c74e", "ZK", "hex"),
    "cassandra": ("#1287b1", "#4bb4cf", "Ca", "circle"),
    "adminer": ("#3b6cb6", "#6f9bd8", "Ad", "box"),
    "phpmyadmin": ("#6c78af", "#9aa4d0", "PA", "box"),
    "portainer": ("#13bef9", "#5bd0f7", "Po", "stack"),
    "grafana": ("#f46800", "#ff9830", "Gr", "wave"),
    "prometheus": ("#e6522c", "#f08b4b", "Pr", "circle"),
    "netdata": ("#00ab44", "#4ecb6a", "Nd", "hex"),
    "dozzle": ("#3b82f6", "#8b5cf6", "Dz", "box"),
    "uptime": ("#5ac27e", "#8ddaa3", "UK", "circle"),
    "watchtower": ("#2f6fb3", "#5b9bd5", "Wt", "ring"),
    "diun": ("#4b5563", "#8b95a5", "Di", "box"),
    "jellyfin": ("#00a4dc", "#aa5cc3", "Jf", "hex"),
    "emby": ("#52b54b", "#83cf6d", "Em", "circle"),
    "qbittorrent": ("#2f7ec4", "#5aa9e0", "Qb", "box"),
    "transmission": ("#c43c35", "#e4755f", "Ts", "circle"),
    "sonarr": ("#3a8fd8", "#6fb6ea", "Sn", "wave"),
    "radarr": ("#d6a12b", "#f0c65b", "Rd", "wave"),
    "lidarr": ("#3b9b5a", "#6fc183", "Li", "wave"),
    "jackett": ("#c94f2c", "#e8845c", "Jk", "box"),
    "bazarr": ("#f2c037", "#f7d46b", "Bz", "wave"),
    "homeassistant": ("#18bcf2", "#53c7f2", "HA", "hex"),
    "adguard": ("#68bc71", "#8fd08c", "Ag", "circle"),
    "pihole": ("#96060c", "#d13c3c", "Ph", "ring"),
    "heimdall": ("#2b3a55", "#5b6f95", "Hd", "box"),
    "nextcloud": ("#0082c9", "#4db1e2", "Nc", "ring"),
    "wordpress": ("#21759b", "#4a9fc4", "Wp", "circle"),
    "vaultwarden": ("#175ddc", "#4c86e8", "Vw", "hex"),
    "syncthing": ("#0891d4", "#4cb8e8", "St", "wave"),
    "filebrowser": ("#3b6cf6", "#7d9df8", "Fb", "box"),
    "duplicati": ("#4a7b3a", "#7bb05f", "Du", "stack"),
    "minio": ("#c72e49", "#e1647a", "Mi", "stack"),
    "gitea": ("#609926", "#8bc34a", "Gi", "mount"),
    "gitlab": ("#e24329", "#fc6d26", "Gl", "hex"),
    "jenkins": ("#d33833", "#e8796f", "Jk", "circle"),
    "sonarqube": ("#4b9fd5", "#7cc0e8", "Sq", "wave"),
    "code": ("#0065a9", "#3b9bd5", "Vs", "box"),
    "onlyoffice": ("#ff6f3d", "#ff9b6b", "Oo", "box"),
    "collabora": ("#4b8b3b", "#7cb56a", "Co", "circle"),
    "rustdesk": ("#2c6ecb", "#5f9ae0", "Rd", "ring"),
    "frp": ("#3b6cb6", "#6f9bd8", "Fr", "wave"),
    "ddns": ("#2f855a", "#62b48a", "Dd", "circle"),
    "alist": ("#3b82f6", "#70a5f8", "Al", "hex"),
    "lucky": ("#f59e0b", "#fbbf24", "Lk", "circle"),
    "wireguard": ("#88171a", "#c1463f", "Wg", "hex"),
    "cloudflare": ("#f38020", "#f8b558", "Cf", "wave"),
    "trivy": ("#1904da", "#5d54e8", "Tv", "box"),
    "mqtt": ("#6b46c1", "#9f7aea", "Mq", "circle"),
    "photoprism": ("#1f2937", "#4b5563", "Pp", "box"),
}


def brand_of(name):
    """library/nginx / linuxserver/qbittorrent → 品牌键；找不到时返回 None。"""
    leaf = str(name or "").strip().strip("/").split("/")[-1].split(":")[0].lower()
    key = leaf
    if key in BRANDS:
        return key
    # 目录里显式登记的 brand 键（如 nginx-proxy-manager → nginx）通过 _brand_alias 兜底
    alias = {
        "nginx-proxy-manager": "nginx", "portainer-ce": "portainer",
        "redis-stack": "redis", "uptime-kuma": "uptime", "ddns-go": "ddns",
    }
    return alias.get(leaf)

# v3.1-r2：面板内置的镜像仓库源链接（16 条），默认全部登记，下拉与「镜像源设置」里可见、可点开、可复制。
# 分两类，界面会明确区分，不把「加速镜像」伪装成可搜索仓库：
#   · registry / catalog：Registry V2 _catalog 列目录（部分公共仓库需要匿名 token，
#     列不出来会明确报错并自动回退内置目录，不假装成功）
#   · mirror：只能写进 NAS 的 Docker daemon.json 做拉取加速，没有搜索 API
PRESET_SOURCES = [
    {"id": "quay", "name": "Quay.io 公共仓库", "type": "catalog", "category": "registry",
     "url": "https://quay.io", "enabled": True, "builtin": True,
     "desc": "Red Hat Quay 公共镜像仓库；_catalog 常需匿名 token，失败时界面会如实提示"},
    {"id": "ghcr", "name": "GitHub Container Registry", "type": "catalog", "category": "registry",
     "url": "https://ghcr.io", "enabled": True, "builtin": True,
     "desc": "GHCR 公共镜像；列目录通常需要匿名 Bearer token"},
    {"id": "mcr", "name": "Microsoft Container Registry", "type": "catalog", "category": "registry",
     "url": "https://mcr.microsoft.com", "enabled": True, "builtin": True,
     "desc": "微软官方公共镜像仓库，_catalog 可能返回 401"},
    {"id": "k8s", "name": "Kubernetes 官方仓库", "type": "catalog", "category": "registry",
     "url": "https://registry.k8s.io", "enabled": True, "builtin": True,
     "desc": "Kubernetes 组件镜像仓库；不提供关键词搜索，只按仓库名过滤"},
    {"id": "redhat", "name": "Red Hat 公共仓库", "type": "catalog", "category": "registry",
     "url": "https://registry.access.redhat.com", "enabled": True, "builtin": True,
     "desc": "Red Hat 官方公共镜像（UBI / OpenShift 基础镜像）"},
    {"id": "aliyun-acr", "name": "阿里云容器镜像服务", "type": "catalog", "category": "registry",
     "url": "https://registry.cn-hangzhou.aliyuncs.com", "enabled": True, "builtin": True,
     "desc": "ACR 杭州地域；私有命名空间需在源里补用户名 / 密码"},
    {"id": "tencent-ccr", "name": "腾讯云容器镜像服务", "type": "catalog", "category": "registry",
     "url": "https://ccr.ccs.tencentyun.com", "enabled": True, "builtin": True,
     "desc": "腾讯云 CCR（内网域名，NAS 在腾讯云环境时可用）"},
    {"id": "huawei-swr", "name": "华为云容器镜像仓库", "type": "catalog", "category": "registry",
     "url": "https://swr.cn-south-1.myhuaweicloud.com", "enabled": True, "builtin": True,
     "desc": "华为云 SWR 华南区域；列目录需对应地域与权限"},
    {"id": "daocloud", "name": "DaoCloud 镜像加速", "type": "mirror", "category": "mirror",
     "url": "https://docker.m.daocloud.io", "enabled": True, "builtin": True,
     "desc": "国内常用 Docker Hub 拉取加速；写进 daemon.json 的 registry-mirrors"},
    {"id": "1ms", "name": "毫秒镜像 1ms.run", "type": "mirror", "category": "mirror",
     "url": "https://docker.1ms.run", "enabled": True, "builtin": True,
     "desc": "社区公共加速镜像，仅用于拉取，不提供搜索接口"},
    {"id": "xuanyuan", "name": "轩辕镜像", "type": "mirror", "category": "mirror",
     "url": "https://docker.xuanyuan.me", "enabled": True, "builtin": True,
     "desc": "社区公共加速镜像，仅用于拉取"},
    {"id": "ustc", "name": "中科大 USTC 镜像", "type": "mirror", "category": "mirror",
     "url": "https://docker.mirrors.ustc.edu.cn", "enabled": True, "builtin": True,
     "desc": "教育网 / 公网 Docker Hub 加速"},
    {"id": "nju", "name": "南京大学镜像", "type": "mirror", "category": "mirror",
     "url": "https://docker.nju.edu.cn", "enabled": True, "builtin": True,
     "desc": "南京大学开源镜像站 Docker Hub 加速"},
    {"id": "sjtu", "name": "上海交大 SJTUG 镜像", "type": "mirror", "category": "mirror",
     "url": "https://docker.mirrors.sjtug.sjtu.edu.cn", "enabled": True, "builtin": True,
     "desc": "上海交大镜像站 Docker Hub 加速"},
    {"id": "163", "name": "网易云镜像加速", "type": "mirror", "category": "mirror",
     "url": "https://hub-mirror.c.163.com", "enabled": True, "builtin": True,
     "desc": "网易云提供的 Docker Hub 加速地址"},
    {"id": "tencent-mirror", "name": "腾讯云内网加速", "type": "mirror", "category": "mirror",
     "url": "https://mirror.ccs.tencentyun.com", "enabled": True, "builtin": True,
     "desc": "腾讯云主机内网加速；非腾讯云环境可能不可达"},
]
PRESET_IDS = tuple(x["id"] for x in PRESET_SOURCES)

PULL_STAGES = ["校验镜像引用", "连接镜像仓库", "下载镜像层", "写入本地镜像库"]
CREATE_STAGES = ["校验镜像引用", "连接镜像仓库", "下载镜像层", "写入本地镜像库",
                 "写容器配置与 Compose", "创建并启动容器", "同步桌面图标"]


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _short_num(n):
    n = int(n or 0)
    if n >= 100000000:
        return "%.1f亿" % (n / 100000000)
    if n >= 10000:
        return "%.1f万" % (n / 10000)
    return str(n)


def _slug(full_name):
    """nginx → library/nginx；bitnami/redis 原样；去掉首尾空白。"""
    s = str(full_name or "").strip().strip("/")
    if not s:
        return ""
    return s if "/" in s else "library/" + s


def _display(full_name):
    s = str(full_name or "")
    if s.startswith("library/"):
        return s[len("library/"):]
    return s


def _http(url, timeout=8, headers=None):
    """走加速源候选链（GitHub 资源）/ 直连，返回 bytes。"""
    urls = st.ACCEL.candidates(url) or [url]
    last = None
    for u in urls[:5]:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "DockerUpdate/2.1",
                                                     "Accept": "application/json,*/*",
                                                     **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(4 * 1024 * 1024)
        except Exception as exc:
            last = exc
    raise ValueError("访问镜像源失败：%s" % (last or "未知错误"))


def _basic_header(src):
    user = str(src.get("username") or "")
    pwd = str(src.get("password") or src.get("token") or "")
    if not user:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode()}


def _host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


class RegistryHub:
    def __init__(self, mgr, appstore=None):
        self.mgr = mgr
        self.docker = mgr.docker
        self.desktop = desktop_mod.Desktop()   # 独立适配器，不依赖已移除的应用商店
        self.lock = threading.RLock()
        self.cache = {}               # (src, q, page, size) -> (ts, payload)
        os.makedirs(st.STORE_DIR, exist_ok=True)
        os.makedirs(ICON_DIR, exist_ok=True)

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d.setdefault("sources", [dict(BUILTIN_FNOS), dict(BUILTIN)])
        d.setdefault("settings", dict(DEFAULT_SETTINGS))
        d.setdefault("created", {})
        for k, v in DEFAULT_SETTINGS.items():
            d["settings"].setdefault(k, v)
        # 两个内置源永不丢失，但允许用户停用；飞牛官方内置目录排在最前（默认展示）
        for bid, obj, at in (("dockerhub", BUILTIN, None), ("fnos", BUILTIN_FNOS, 0)):
            if not any(x.get("id") == bid for x in d["sources"]):
                if at is None:
                    d["sources"].append(dict(obj))
                else:
                    d["sources"].insert(at, dict(obj))
        # 旧版 registry.json 里 fnos 排在后面时，提到最前
        for i, x in enumerate(d["sources"]):
            if x.get("id") == "fnos" and i > 0:
                d["sources"].insert(0, d["sources"].pop(i))
                break
        # v3.1-r2：16 个内置源链接按 id 幂等补齐，不覆盖用户已改的启停状态
        have = {x.get("id") for x in d["sources"]}
        for obj in PRESET_SOURCES:
            if obj["id"] not in have:
                d["sources"].append(dict(obj))
                have.add(obj["id"])
        return d

    def _save(self, d):
        tmp = CFG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_FILE)

    def settings(self):
        return dict(self._load()["settings"])

    def pro_data_root(self):
        """v3.2：系统联动里改的「容器数据卷默认落盘位置」作为镜像仓库部署的默认数据根目录。"""
        try:
            with open(os.path.join(config.DATA_DIR, "pro", "settings.json"), encoding="utf-8") as f:
                v = str(json.load(f).get("data_root") or "").strip()
            if v:
                return v.rstrip("/")
        except Exception:
            pass
        return ""

    def sources(self):
        return self._load()["sources"]

    # ---------- 图标 ----------
    def icon_allowed(self, url):
        """SSRF 白名单：只允许已登记源的 host，禁止本机 / 私网。"""
        u = str(url or "")
        if not u.startswith(("http://", "https://")):
            return False
        host = _host_of(u)
        if not host or host in ("localhost", "127.0.0.1", "::1"):
            return False
        if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host):
            return False
        for s in self.sources():
            if _host_of(s.get("url", "")) == host:
                return True
        return host in ("raw.githubusercontent.com", "cdn.jsdelivr.net", "hub.docker.com")

    def fetch_icon(self, url):
        data = _http(url, timeout=8)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("图标超过 2MB")
        ctype = "image/png"
        if data[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif data[:4] == b"<svg" or data.lstrip()[:5].lower() == b"<?xml":
            ctype = "image/svg+xml"
        elif data[:4] == b"RIFF":
            ctype = "image/webp"
        return data, ctype

    def generated_icon(self, name):
        """每个镜像都有「相应」的图标：命中品牌色板时用专属配色 + 形状，否则生成渐变字母 SVG。
        全部为本地矢量，不请求外网，离线也能显示。"""
        s = _display(name) or "image"
        parts = [p for p in s.split("/") if p]
        label = (parts[-1] if parts else s)[:2].upper()
        h = int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)
        palettes = [
            ("#3b82f6", "#8b5cf6"), ("#06b6d4", "#3b82f6"), ("#10b981", "#059669"),
            ("#f59e0b", "#ef4444"), ("#ec4899", "#8b5cf6"), ("#6366f1", "#22d3ee"),
            ("#14b8a6", "#0ea5e9"), ("#f97316", "#fbbf24"),
        ]
        key = brand_of(name)
        if key:
            a, b, label, shape = BRANDS[key]
        else:
            a, b = palettes[h % len(palettes)]
            shape = ""
        shapes = {
            "hex": '<path d="M64 10 L110 37 L110 91 L64 118 L18 91 L18 37 Z" fill="#fff" opacity=".16"/>',
            "circle": '<circle cx="64" cy="64" r="42" fill="#fff" opacity=".15"/>',
            "ring": '<circle cx="64" cy="64" r="36" fill="none" stroke="#fff" stroke-width="10" opacity=".85"/>',
            "stack": '<path d="M26 52 L64 32 L102 52 L64 72 Z" fill="#fff" opacity=".85"/>'
                     '<path d="M26 72 L64 92 L102 72" fill="none" stroke="#fff" stroke-width="7" opacity=".55"/>',
            "mount": '<path d="M16 98 L52 44 L70 70 L88 42 L112 98 Z" fill="#fff" opacity=".82"/>',
            "wave": '<path d="M14 74 Q34 52 54 74 T94 74 T128 70" fill="none" stroke="#fff" '
                    'stroke-width="9" opacity=".8"/>',
            "box": '<rect x="24" y="24" width="80" height="80" rx="20" fill="#fff" opacity=".14"/>',
        }
        deco = shapes.get(shape, '<circle cx="100" cy="26" r="34" fill="#fff" opacity=".10"/>')
        font = 46 if len(label) > 2 else 54
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
               '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
               '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient></defs>'
               '<rect width="128" height="128" rx="28" fill="url(#g)"/>%s'
               '<text x="64" y="%d" text-anchor="middle" font-family="Segoe UI,Arial,sans-serif" '
               'font-size="%d" font-weight="700" fill="#fff" letter-spacing="1">%s</text></svg>'
               % (a, b, deco, 78 if font == 54 else 76, font,
                  label.replace("&", "&amp;").replace("<", "&lt;")))
        return svg.encode("utf-8"), "image/svg+xml"

    def _item(self, raw, src):
        full = _slug(raw.get("name") or raw.get("repo_name") or raw.get("full_name") or "")
        if not full:
            return None
        owner = str(raw.get("repo_owner") or raw.get("namespace") or "").strip()
        if owner and "/" not in str(raw.get("name") or ""):
            full = "%s/%s" % (owner, _display(full))
        logo = str(raw.get("logo_url") or raw.get("icon_url") or raw.get("icon") or "")
        if logo.startswith("/"):
            logo = urllib.parse.urljoin(src.get("url", ""), logo)
        icon_url = ("/api/registry/icon?url=" + urllib.parse.quote(logo, safe="")
                    if logo.startswith(("http://", "https://")) and self.icon_allowed(logo)
                    else "/api/registry/icon?name=" + urllib.parse.quote(full, safe=""))
        return {
            "id": full, "name": _display(full), "full_name": full,
            "official": bool(raw.get("is_official") or full.startswith("library/")),
            "stars": int(raw.get("star_count") or raw.get("stars") or 0),
            "pulls": int(raw.get("pull_count") or raw.get("pulls") or 0),
            "stars_text": _short_num(raw.get("star_count") or raw.get("stars") or 0),
            "pulls_text": _short_num(raw.get("pull_count") or raw.get("pulls") or 0) + ("+" if raw.get("pull_count") else ""),
            "description": str(raw.get("short_description") or raw.get("description") or "")[:240],
            "icon": icon_url, "logo": logo,
            "homepage": str(raw.get("homepage") or ("https://hub.docker.com/r/" + full)),
            "last_updated": str(raw.get("last_updated") or "")[:10],
            "source_id": src["id"], "source_name": src["name"],
        }

    # ---------- 搜索 ----------
    def search(self, q="", source_id="", page=1, page_size=0, progress=None):
        d = self._load()
        size = int(page_size or d["settings"]["page_size"] or 20)
        size = max(5, min(100, size))
        page = max(1, int(page or 1))
        # v3.5：界面不再让用户选「当前是哪个源」，一次性聚合全部启用源
        if str(source_id or "").lower() == "all":
            return self.search_all(q, page, size)
        src = next((x for x in d["sources"] if x["id"] == (source_id or "fnos")), None)
        if not src or not src.get("enabled", True):
            # v3.3：源被删除 / 停用时诚实回退内置目录，不抛错、不留空白页
            payload = self._search_builtin(BUILTIN_FNOS, q, page, size)
            payload.update({"source": "fnos", "source_name": "飞牛官方镜像目录（内置）",
                            "hint": "所选镜像源已删除或停用，已自动显示内置官方目录；"
                                    "可在「镜像源设置 → 一键恢复」里找回内置源。"})
            return payload
        key = (src["id"], q.strip().lower(), page, size, time.time() // 45)
        if key in self.cache:
            return self.cache[key][1]
        try:
            if src["type"] == "hub":
                payload = self._search_hub(src, q, page, size)
            elif src["type"] == "builtin":
                payload = self._search_builtin(src, q, page, size)
            elif src["type"] == "catalog":
                payload = self._search_catalog(src, q, page, size)
            elif src["type"] == "json":
                payload = self._search_json(src, q, page, size)
            elif src["type"] == "mirror":
                # 加速镜像没有搜索 API，诚实提示并回退内置目录，绝不发起无意义的网络超时
                payload = self._search_builtin(BUILTIN_FNOS, q, page, size)
                payload.update({
                    "source": src["id"], "source_name": src["name"] + "（仅加速）",
                    "hint": "该源是 registry mirror，只能写进 NAS 的 Docker daemon.json 做拉取加速，"
                            "不提供在线搜索接口；已自动显示内置飞牛官方镜像目录。"
                            "可在「镜像源设置」里复制加速地址。",
                    "mirror": src.get("url", "")})
            else:
                raise ValueError("未知的镜像源类型：%s" % src["type"])
        except Exception as exc:
            # v3.1：在线源失败不再留空白页，自动回退到面板内置的飞牛官方目录
            payload = self._search_builtin(BUILTIN_FNOS, q, page, size)
            payload.update({
                "source": src["id"], "source_name": src["name"] + "（已回退）",
                "error": "镜像源访问失败：%s" % str(exc)[:200],
                "hint": "已自动切换为面板内置的飞牛官方镜像目录（%d 项，离线可用）；"
                        "联网恢复后点「刷新」即回到实时目录，根治仍需给 NAS 的 Docker daemon 配代理或镜像加速。"
                        % len(FNOS_OFFICIAL),
                "fallback": "fnos"})
        self.cache[key] = (time.time(), payload)
        if len(self.cache) > 40:
            for k in sorted(self.cache)[:10]:
                self.cache.pop(k, None)
        return payload

    # ---------------- v3.5：全源聚合 + 自动测速 ----------------
    def _source_host(self, src):
        url = str(src.get("url") or "").strip()
        if not url.startswith("http"):
            return ""
        try:
            host = urllib.parse.urlsplit(url).netloc
        except Exception:
            return ""
        return host

    def _probe_ms(self, src):
        """对 Registry V2 / Hub 主机做一次轻量可达性 + 延迟探测，结果缓存 5 分钟。"""
        url = str(src.get("url") or "").strip()
        if not url.startswith("http"):
            return None
        sid = src.get("id", "")
        hit = getattr(self, "_latency", {}).get(sid)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        target = url.rstrip("/")
        if target.endswith("/v2"):
            target += "/"
        elif "/v2" not in target:
            target += "/v2/"
        t0 = time.time()
        ms = None
        try:
            req = urllib.request.Request(target, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=2.0) as r:
                    r.read(1)
                    ms = round((time.time() - t0) * 1000)
            except urllib.error.HTTPError as e:
                # 401 / 404 说明主机可达，只是需要 token 或没有该路径
                if 400 <= e.code < 500:
                    ms = round((time.time() - t0) * 1000)
        except Exception:
            ms = None
        if not hasattr(self, "_latency"):
            self._latency = {}
        self._latency[sid] = (time.time(), ms)
        return ms

    def choose_registry(self, full, tag, candidates):
        """同一个镜像在多个仓库里都有时，返回延迟最低的 Registry 主机前缀后的 ref。

        只对有真实 registry 主机（catalog / 私有 Registry V2）的候选生效；
        Docker Hub / 内置离线目录仍交给 daemon 默认解析，mirror 只做 daemon 加速配置，
        不在这里伪装成可拉取的仓库。探测失败全部回退原 ref。
        """
        cands = []
        for c in (candidates or []):
            if not isinstance(c, dict):
                continue
            if c.get("type") not in ("catalog", "hub", "json"):
                continue
            host = c.get("host") or self._source_host(c)
            if host and c.get("type") == "catalog":
                cands.append((c, host))
        if not cands:
            return ""
        scored = []
        for c, host in cands:
            ms = self._probe_ms({"id": c.get("id", ""), "url": c.get("url") or "https://" + host})
            if ms is not None:
                scored.append((ms, host, c))
        if not scored:
            return ""
        scored.sort(key=lambda x: x[0])
        ms, host, chosen = scored[0]
        self.last_choice = {"host": host, "latency_ms": ms, "source": chosen.get("name", host)}
        # full 已经带域名（ghcr.io/xxx）时不再叠加
        first = full.split("/", 1)[0]
        if "." in first or ":" in first or first == "localhost":
            return ""
        base = full if ":" in full.rsplit("/", 1)[-1] else "%s:%s" % (full, tag)
        return "%s/%s" % (host, base)

    def _fetch_source(self, src, q, limit):
        t = src.get("type")
        if t == "hub":
            return self._search_hub(src, q, 1, min(limit, 100))
        if t == "builtin":
            return self._search_builtin(src, q, 1, min(limit, 100))
        if t == "catalog":
            return self._search_catalog(src, q, 1, min(limit, 20))   # _catalog 逐个查 tag 很慢，限 20
        if t == "json":
            return self._search_json(src, q, 1, min(limit, 100))
        return None

    def search_all(self, q="", page=1, page_size=20):
        """并发拉取所有启用源的第 1 页 → 按完整镜像名去重 → 合并可用源 → 自动测速选最快。"""
        d = self._load()
        size = max(5, min(100, int(page_size or 20)))
        page = max(1, int(page or 1))
        ql = str(q or "").strip().lower()
        key = ("all", ql, page, size, time.time() // 60)
        if key in self.cache:
            return self.cache[key][1]
        sources = [x for x in d["sources"] if x.get("enabled", True)
                   and x.get("type") in ("hub", "builtin", "catalog", "json")]
        if not sources:
            sources = [dict(BUILTIN_FNOS)]
        used, failed, merged = [], [], {}

        def one(src):
            try:
                p = self._fetch_source(src, q, 100)
                return src, (p or {}).get("items") or [], ""
            except Exception as exc:
                return src, [], str(exc)[:90]

        with ThreadPoolExecutor(max_workers=min(8, max(2, len(sources)))) as ex:
            futs = {ex.submit(one, s): s for s in sources}
            try:
                for f in as_completed(futs, timeout=12):
                    src, items, err = f.result()
                    if err:
                        failed.append({"id": src["id"], "name": src["name"], "error": err})
                        continue
                    if not items:
                        continue
                    used.append({"id": src["id"], "name": src["name"], "type": src["type"],
                                 "count": len(items)})
                    entry = {"id": src["id"], "name": src["name"], "type": src["type"],
                             "url": src.get("url", ""), "host": self._source_host(src)}
                    for it in items:
                        k = str(it.get("full_name") or it.get("id") or "").lower()
                        if not k:
                            continue
                        if k not in merged:
                            it = dict(it)
                            it["sources"] = [dict(entry)]
                            merged[k] = it
                        else:
                            cur = merged[k]
                            if not [x for x in cur["sources"] if x["id"] == entry["id"]]:
                                cur["sources"].append(dict(entry))
                            # 信息更全的（有简介/星标/图标）优先
                            if (int(it.get("stars") or 0) > int(cur.get("stars") or 0)) or \
                               (not cur.get("description") and it.get("description")):
                                keep = cur["sources"]
                                it = dict(it)
                                it["sources"] = keep
                                merged[k] = it
            except Exception:
                pass

        if not merged:
            # 全部在线源都失败：诚实回退内置离线目录
            payload = self._search_builtin(BUILTIN_FNOS, q, page, size)
            payload.update({"source": "all", "source_name": "全部镜像源（自动测速）",
                            "sources_used": [], "failed_sources": failed,
                            "hint": "在线镜像源暂时不可用，已自动回退内置飞牛官方镜像目录（%d 项，离线可用）；"
                                    "联网恢复后点「刷新」即会重新聚合全部源。" % len(FNOS_OFFICIAL)})
            for it in payload["items"]:
                it["sources"] = [{"id": "fnos", "name": "飞牛官方镜像目录（内置）",
                                  "type": "builtin", "url": "", "host": ""}]
            self.cache[key] = (time.time(), payload)
            return payload

        # 给每个镜像标注最快仓库（只标，不在这里阻塞式逐个探测）
        for it in merged.values():
            hosts = [s for s in it["sources"] if s.get("host") and s.get("type") == "catalog"]
            it["multi_source"] = len(it["sources"]) > 1
            it["best_source"] = hosts[0]["name"] if hosts else it["sources"][0]["name"]
            it["source_name"] = "全部镜像源" if it["multi_source"] else it["sources"][0]["name"]
        # 供批量拉取按镜像名回查候选源
        self._source_index = {k: v["sources"] for k, v in merged.items()}

        rows = sorted(merged.values(), key=lambda x: (-int(x.get("stars") or 0),
                                                      str(x.get("full_name") or "")))
        count = len(rows)
        chunk = rows[(page - 1) * size:page * size]
        hint = ("已一次性聚合 %d 个镜像源、去重后 %d 个镜像；同一镜像在多个仓库都有时，"
                "拉取前会自动探测延迟并使用最快的仓库。"
                % (len(used), count))
        if failed:
            hint += " 本次有 %d 个源未连通（已跳过，不影响其它源）。" % len(failed)
        payload = {"ok": True, "source": "all", "source_name": "全部镜像源（自动测速）",
                   "query": q, "page": page, "page_size": size, "count": count,
                   "pages": max(1, min(200, -(-count // size))), "items": chunk,
                   "error": "", "hint": hint,
                   "sources_used": used, "failed_sources": failed,
                   "offline_only": not used}
        self.cache[key] = (time.time(), payload)
        if len(self.cache) > 40:
            for k in sorted(self.cache)[:10]:
                self.cache.pop(k, None)
        return payload

    def _wrap(self, src, q, page, size, count, items, extra=None):
        out = {"ok": True, "source": src["id"], "source_name": src["name"],
               "query": q, "page": page, "page_size": size,
               "count": int(count or len(items)), "pages": max(1, min(125, -(-int(count or len(items)) // size))),
               "items": items, "error": "", "hint": ""}
        if extra:
            out.update(extra)
        return out

    def _search_builtin(self, src, q, page, size):
        """飞牛官方镜像目录（离线快照）：图标 / 下载量 / 星标 / 简介 / 项目地址一应俱全。"""
        ql = str(q or "").strip().lower()
        rows = [r for r in FNOS_OFFICIAL
                if not ql or ql in (r[0] + " " + r[1]).lower()]
        chunk = rows[(page - 1) * size:page * size]
        items = []
        for name, desc, pulls_text, stars, brand in chunk:
            full = _slug(name)
            hub = "https://hub.docker.com/_/" + name if "/" not in name \
                else "https://hub.docker.com/r/" + name
            items.append({
                "id": full, "name": _display(full), "full_name": full,
                "official": "/" not in name,
                "stars": stars, "pulls": 0,
                "stars_text": _short_num(stars), "pulls_text": pulls_text,
                "description": desc,
                "icon": "/api/registry/icon?name=" + urllib.parse.quote(full, safe=""),
                "logo": "", "homepage": hub, "last_updated": "",
                "source_id": src["id"], "source_name": src["name"],
                "builtin": True,
            })
        return self._wrap(src, q, page, size, len(rows), items, {
            "offline": True,
            "hint": "内置飞牛官方镜像目录共 %d 项（离线快照，星标 / 下载量为参考值）；"
                    "切到「Docker Hub 官方」可联网浏览实时 2500+ 项。" % len(FNOS_OFFICIAL)})

    def _search_hub(self, src, q, page, size):
        base = str(src.get("url") or "https://hub.docker.com/v2").rstrip("/")
        qs = {"query": q or "", "page": page, "page_size": size}
        if not q:
            qs["ordering"] = "-pull_count"     # 空搜索按热度列出，和官方仓库页一致
        url = "%s/search/repositories/?%s" % (base, urllib.parse.urlencode(qs))
        obj = json.loads(_http(url, timeout=10, headers=_basic_header(src)).decode("utf-8", "replace"))
        items = [x for x in (self._item(r, src) for r in (obj.get("results") or [])) if x]
        return self._wrap(src, q, page, size, obj.get("count") or len(items), items,
                          {"next": obj.get("next") or ""})

    def _search_json(self, src, q, page, size):
        obj = json.loads(_http(src["url"], timeout=10, headers=_basic_header(src)).decode("utf-8", "replace"))
        rows = obj.get("images") or obj.get("apps") or obj.get("results") or obj
        if isinstance(rows, dict):
            rows = [dict(v, name=k) for k, v in rows.items()]
        ql = q.strip().lower()
        if ql:
            rows = [r for r in rows if ql in json.dumps(r, ensure_ascii=False).lower()]
        chunk = rows[(page - 1) * size:page * size]
        items = [x for x in (self._item(r, src) for r in chunk) if x]
        return self._wrap(src, q, page, size, len(rows), items)

    def _search_catalog(self, src, q, page, size):
        """Registry V2：_catalog 只能列仓库名，再用 tags/list 补最新标签。"""
        base = str(src["url"]).rstrip("/")
        if not base.endswith("/v2"):
            base += "/v2"
        hdrs = _basic_header(src)
        obj = json.loads(_http(base + "/_catalog?n=1000", timeout=10,
                               headers=hdrs).decode("utf-8", "replace"))
        names = obj.get("repositories") or []
        ql = q.strip().lower()
        if ql:
            names = [n for n in names if ql in n.lower()]
        rows = []
        for n in names[(page - 1) * size:page * size]:
            tags = []
            try:
                t = json.loads(_http("%s/%s/tags/list" % (base, urllib.parse.quote(n, safe="/")),
                                     timeout=4, headers=hdrs).decode("utf-8", "replace"))
                tags = (t.get("tags") or [])[:8]
            except Exception:
                pass
            rows.append({"name": n, "description": "标签：" + (", ".join(tags[:5]) or "—"),
                         "tags": tags})
        items = [x for x in (self._item(r, src) for r in rows) if x]
        return self._wrap(src, q, page, size, len(names), items,
                          {"hint": "Registry V2 没有关键词搜索接口，这里按仓库名过滤；"
                                   "不提供星标 / 下载量 / 描述，图标为面板生成的字母图标。"})

    def tags(self, full_name, source_id="dockerhub"):
        allsrc = self.sources()
        if str(source_id or "").lower() == "all":
            src = next((x for x in allsrc if x["type"] == "hub"), None) \
                or next((x for x in allsrc if x["type"] == "builtin"), None)
        else:
            src = next((x for x in allsrc if x["id"] == source_id), None)
        if not src:
            src = dict(BUILTIN_FNOS)
        full = _slug(full_name)
        if src["type"] == "mirror":
            return {"ok": True, "count": 1,
                    "tags": [{"name": "latest", "size": 0, "arch": "", "updated": ""}],
                    "hint": "加速镜像没有标签接口，默认 latest；可在输入框手填具体版本"}
        if src["type"] == "builtin":
            return {"ok": True, "count": 4, "tags": [
                {"name": "latest", "size": 0, "arch": "", "updated": ""},
                {"name": "alpine", "size": 0, "arch": "", "updated": ""},
                {"name": "stable", "size": 0, "arch": "", "updated": ""},
                {"name": "bookworm", "size": 0, "arch": "", "updated": ""}],
                "hint": "内置离线目录提供常用标签；联网切到 Docker Hub 官方源可读取全部标签"}
        if src["type"] == "hub":
            try:
                url = "%s/repositories/%s/tags/?page_size=25&ordering=last_updated" % (
                    str(src["url"]).rstrip("/"), urllib.parse.quote(full, safe="/"))
                obj = json.loads(_http(url, timeout=10, headers=_basic_header(src)).decode("utf-8", "replace"))
            except Exception:
                return {"ok": True, "count": 1,
                        "tags": [{"name": "latest", "size": 0, "arch": "", "updated": ""}],
                        "hint": "镜像源暂时不可达，已回退到 latest；不影响拉取"}
            return {"ok": True, "tags": [{"name": r.get("name", ""),
                                           "size": sum(int(i.get("size") or 0) for i in (r.get("images") or [])),
                                           "arch": ",".join(sorted({i.get("architecture", "")
                                                                     for i in (r.get("images") or [])}))[:60],
                                           "updated": str(r.get("last_updated") or "")[:10]}
                                          for r in (obj.get("results") or [])],
                    "count": obj.get("count", 0)}
        if src["type"] == "catalog":
            base = str(src["url"]).rstrip("/")
            if not base.endswith("/v2"):
                base += "/v2"
            obj = json.loads(_http("%s/%s/tags/list" % (base, urllib.parse.quote(full, safe="/")),
                                   timeout=8, headers=_basic_header(src)).decode("utf-8", "replace"))
            return {"ok": True, "tags": [{"name": t, "size": 0, "arch": "", "updated": ""}
                                          for t in (obj.get("tags") or [])[:100]],
                    "count": len(obj.get("tags") or [])}
        return {"ok": True, "tags": [{"name": "latest", "size": 0, "arch": "", "updated": ""}],
                "count": 1, "hint": "自定义 JSON 源没有标签接口，默认 latest"}

    # ---------- 设置 / 源 ----------
    def state(self):
        d = self._load()
        return {"ok": True, "sources": [self._public(x) for x in d["sources"]],
                "pro_data_root": self.pro_data_root(),
                "presets": [dict(x) for x in PRESET_SOURCES],
                "preset_count": len(PRESET_SOURCES),
                "mirror_urls": [x["url"] for x in PRESET_SOURCES if x["type"] == "mirror"],
                "settings": d["settings"], "created": d["created"],
                "desktop": self.desktop.status(),
                "capabilities": {
                    "builtin": "内置飞牛官方镜像目录 %d 项（离线可用）+ Docker Hub 实时搜索 / 星标 / 下载量 / 官方徽章 / 分页 / 标签列表" % len(FNOS_OFFICIAL),
                    "builtin_count": len(FNOS_OFFICIAL),
                    "sources": "hub（Hub 兼容 API）· catalog（Registry V2 _catalog）· json（自定义索引）· mirror（仅 daemon 加速，不搜索）",
                    "preset_sources": "内置 %d 个镜像仓库源链接（8 个可列目录仓库 + 8 个 Docker 加速镜像），设置里可查看 / 复制 / 一键恢复" % len(PRESET_SOURCES),
                    "icons": "源提供 logo_url 时走白名单代理；否则面板生成品牌字母 SVG，离线也有图标",
                    "pull": "Docker Engine API /images/create，异步任务 + SSE 阶段与层进度",
                    "container": "拉取后可直接建容器，写 Compose 与数据卷目录，自动同步桌面图标",
                    "storage_note": "镜像实际存储位置由 Docker daemon 的 data-root 决定，面板不能改；"
                                    "可改的是容器配置目录与数据卷根目录",
                    "not_replicated": "Docker Desktop 的 Extensions / Builds / Dev Environments / "
                                      "Kubernetes / Scout 安全扫描 / 镜像签名不属于本地 Docker Engine，本面板不复刻",
                }}

    def _public(self, s):
        return {k: v for k, v in s.items() if k not in ("password", "token")}

    def set_settings(self, form):
        d = self._load()
        s = d["settings"]
        for k in ("default_tag", "restart", "network"):
            if str(form.get(k) or "").strip():
                s[k] = str(form[k]).strip()[:80]
        for k in ("page_size", "pull_timeout"):
            if form.get(k) not in (None, ""):
                s[k] = max(5, min(100, int(form[k]))) if k == "page_size" else max(60, min(3600, int(form[k])))
        s["auto_desktop"] = bool(form.get("auto_desktop", s["auto_desktop"]))
        for k in ("container_root", "data_root"):
            v = str(form.get(k) or "").strip()
            if v:
                # 必须落在可写根下，避免写进容器镜像层 / 系统目录
                local = st.host_to_local(v, allow_root=False)
                if not st._under(local, st.STORE_ROOT) and local != st.STORE_ROOT:
                    raise ValueError("%s 必须在可写根 %s 下" % (k, st.STORE_HOST_ROOT))
                s[k] = v.rstrip("/")
        self._save(d)
        self.cache.clear()
        return {"ok": True, "message": "镜像仓库设置已保存", "settings": s}

    def add_source(self, form):
        d = self._load()
        sid = st._safe_id(form.get("id") or form.get("name") or ("reg-" + str(int(time.time()))))
        if any(x["id"] == sid for x in d["sources"]):
            raise ValueError("镜像源 ID 已存在：%s" % sid)
        typ = str(form.get("type") or "json")
        if typ not in ("hub", "catalog", "json", "builtin", "mirror"):
            raise ValueError("镜像源类型只能是 hub / catalog / json / builtin / mirror")
        url = str(form.get("url") or "").strip()
        if typ != "catalog" and not url.startswith(("http://", "https://")):
            raise ValueError("请填完整的 http(s) 地址")
        src = {"id": sid, "name": str(form.get("name") or sid)[:60], "type": typ, "url": url,
               "enabled": True, "builtin": False, "added_at": _now(),
               "username": str(form.get("username") or "")[:80],
               "password": str(form.get("password") or form.get("token") or "")[:200],
               "desc": str(form.get("desc") or "")[:200]}
        if typ == "catalog" and not url:
            raise ValueError("Registry V2 源需要填 registry 地址，如 http://192.168.1.20:5000")
        d["sources"].append(src)
        self._save(d)
        self.cache.clear()
        return {"ok": True, "message": "已添加镜像源「%s」，切换后可搜索" % src["name"],
                "source": self._public(src)}

    def delete_source(self, sid):
        # v3.3：按用户要求「所有项目要可以删除」，内置源也允许删；
        # 删错了用「一键恢复 / 启用全部内置源」即可找回，不会丢配置。
        d = self._load()
        before = len(d["sources"])
        d["sources"] = [x for x in d["sources"] if x["id"] != sid]
        if len(d["sources"]) == before:
            raise ValueError("镜像源不存在：%s" % sid)
        self._save(d)
        self.cache.clear()
        return {"ok": True, "message": "已删除镜像源 %s（可在「一键恢复」里找回内置源）" % sid}

    def restore_presets(self):
        """一键恢复 / 启用全部 16 个内置源链接（用户停用或旧配置缺项时使用）。"""
        d = self._load()
        have = {x.get("id") for x in d["sources"]}
        added = 0
        for obj in PRESET_SOURCES + [BUILTIN_FNOS, BUILTIN]:
            if obj["id"] not in have:
                d["sources"].append(dict(obj))
                have.add(obj["id"])
                added += 1
        for x in d["sources"]:
            if x.get("id") in PRESET_IDS and not x.get("enabled", True):
                x["enabled"] = True
        self._save(d)
        self.cache.clear()
        return {"ok": True, "added": added,
                "message": "已恢复 %d 个缺失源并启用全部 %d 个内置源链接" % (added, len(PRESET_SOURCES))}

    def toggle_source(self, sid):
        d = self._load()
        for x in d["sources"]:
            if x["id"] == sid:
                x["enabled"] = not x.get("enabled", True)
                self._save(d)
                self.cache.clear()
                return {"ok": True, "message": "镜像源已%s" % ("启用" if x["enabled"] else "停用")}
        raise ValueError("镜像源不存在")

    # ---------- 拉取（真实层进度） ----------
    def _pull(self, ref, auth=None, timeout=600, emit=None):
        repo, _, tag = ref.rpartition(":")
        if "/" in tag or not repo:
            repo, tag = ref, "latest"
        path = "/images/create?fromImage=%s&tag=%s" % (
            urllib.parse.quote(repo, safe=""), urllib.parse.quote(tag, safe=""))
        conn = UnixHTTPConnection(config.DOCKER_SOCKET, timeout=timeout)
        try:
            conn.request("POST", path, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status >= 400:
                txt = resp.read().decode("utf-8", "replace")
                raise DockerError(resp.status, txt[:300])
            seen, done, layers = {}, 0, 0
            t0 = time.time()
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if not line or line in (b"0", b"0\r\n"):
                    continue
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if ev.get("error"):
                    raise DockerError(500, str(ev.get("error"))[:300])
                lid = str(ev.get("id") or "")
                status = str(ev.get("status") or "")
                pd = ev.get("progressDetail") or {}
                cur, total = int(pd.get("current") or 0), int(pd.get("total") or 0)
                if lid and status in ("Downloading", "Extracting", "Verifying Checksum",
                                      "Download complete", "Pull complete", "Extracting"):
                    seen[lid] = (cur, total, status)
                if emit and (len(seen) > layers or status in ("Pull complete", "Download complete")):
                    layers = len(seen)
                    tot = sum(t for _, t, _ in seen.values())
                    curall = sum(c for c, _, _ in seen.values())
                    pct = (28 + 62 * curall / tot) if tot else min(72, 28 + layers * 3)
                    rate = curall / max(0.3, time.time() - t0)
                    emit(stage="下载镜像层", pct=round(pct, 1),
                         detail="%d 个层 · %s" % (layers, status[:60]),
                         speed="%.1f MB/s" % (rate / 1048576) if rate >= 1048576
                               else "%.0f KB/s" % (rate / 1024),
                         downloaded=curall, total=tot)
                if "Status: Downloaded newer image" in status or "up to date" in status.lower():
                    done = 1
            return {"layers": len(seen), "done": done, "ref": "%s:%s" % (repo, tag)}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def pull(self, form, progress=None):
        def emit(**kw):
            if progress:
                progress(**kw)
        full = _slug(form.get("image") or form.get("full_name") or "")
        tag = str(form.get("tag") or self.settings()["default_tag"] or "latest").strip()
        if not full:
            raise ValueError("缺少镜像名")
        ref = full if ":" in full.rsplit("/", 1)[-1] else "%s:%s" % (full, tag)
        # v3.5：多个库都有该镜像时，自动探测并使用延迟最低的仓库
        chosen = self.choose_registry(full, tag, form.get("sources") or [])
        if chosen:
            ref = chosen
        emit(stage="校验镜像引用", pct=4, detail=ref)
        emit(stage="连接镜像仓库", pct=14, detail="Docker Engine /images/create")
        timeout = int(self.settings().get("pull_timeout", 600))
        try:
            self._pull(ref, self.mgr._registry_auth(full.split(":")[0]), timeout, emit)
        except (OSError, socket.timeout, AttributeError):
            # 某些 daemon / 代理不支持逐行读：退回标准库实现，阶段进度照旧
            self.docker.pull_ref(ref, self.mgr._registry_auth(full.split(":")[0]), timeout=timeout)
            emit(stage="下载镜像层", pct=72, detail="已由 Docker daemon 完成拉取")
        emit(stage="写入本地镜像库", pct=94, detail=ref)
        self.docker.inspect_image(ref)
        extra = ""
        ch = getattr(self, "last_choice", None)
        if chosen and ch:
            extra = "（已从 %s 自动选择最快仓库 %s · %dms）" % (
                len(form.get("sources") or []), ch["host"], ch["latency_ms"])
        return {"ok": True, "message": "已拉取镜像 %s%s" % (ref, extra), "ref": ref}

    # ---------- 创建容器 + 桌面图标 ----------
    def create_container(self, form, progress=None):
        def emit(**kw):
            if progress:
                progress(**kw)
        full = _slug(form.get("image") or form.get("full_name") or "")
        tag = str(form.get("tag") or self.settings()["default_tag"] or "latest").strip()
        ref = full if ":" in full.rsplit("/", 1)[-1] else "%s:%s" % (full, tag)
        chosen = self.choose_registry(full, tag, form.get("sources") or [])
        if chosen:
            ref = chosen
        name = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(form.get("name") or _display(full)).strip())[:64]
        if not full or not name:
            raise ValueError("镜像名与容器名不能为空")
        s = self.settings()
        # v3.1：每次部署都可自定义「配置文件 / 数据 / 额外共享」三个落盘位置。
        # 表单里填的是宿主机路径（/vol1/1000/docker/...），host_to_local 换成容器内
        # /store/... 用于写文件；Docker daemon 的 Binds 必须用宿主机路径，否则会写错位置。
        # v3.3：用户要求「所有文件单独放在一个文件夹里并以容器名称命名」，不再分开放。
        # 统一根 = 系统联动里的默认落盘位置（或镜像源设置里的配置根），
        # 其下 <容器名>/ 放 Compose 与配置，<容器名>/data、<容器名>/share 是子目录。
        root = (self.pro_data_root() or s["container_root"]).rstrip("/")
        app_root = root + "/" + name
        cdir_host = str(form.get("container_dir") or "").strip().rstrip("/") or app_root
        ddir_host = str(form.get("data_dir") or "").strip().rstrip("/") or (app_root + "/data")
        app_dir = st.host_to_local(cdir_host, allow_root=False)
        vol_dir = st.host_to_local(ddir_host, allow_root=False)
        # 拉取（若本机已有就跳过）
        try:
            self.docker.inspect_image(ref)
            emit(stage="写入本地镜像库", pct=32, detail="本机已有 %s，跳过拉取" % ref)
        except DockerError:
            self.pull({"image": ref, "tag": tag, "sources": form.get("sources") or []},
                      progress=lambda **kw: emit(**{**kw, "pct": round(float(kw.get("pct", 0)) * 0.5, 1)}))
        emit(stage="写容器配置与 Compose", pct=58, detail=name)

        os.makedirs(app_dir, exist_ok=True)
        os.makedirs(vol_dir, exist_ok=True)

        # 端口：支持 "8080:80, 5353:53/udp" 或数组
        ports_raw = form.get("ports") or []
        if isinstance(ports_raw, str):
            ports_raw = [p.strip() for p in re.split(r"[,\n]", ports_raw) if p.strip()]
        exposed, pb, host_ports = {}, {}, []
        for p in ports_raw:
            m = re.match(r"^(\d+):(\d+)(?:/(tcp|udp))?$", str(p).strip())
            if not m:
                continue
            h, c, proto = m.group(1), m.group(2), m.group(3) or "tcp"
            key = "%s/%s" % (c, proto)
            exposed[key] = {}
            pb[key] = [{"HostPort": h}]
            host_ports.append(h)
        env = []
        for line in str(form.get("env") or "").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                env.append(line)
        binds = []
        raw_vols = form.get("volumes") or []
        if isinstance(raw_vols, str):
            raw_vols = [v.strip() for v in re.split(r"[,\n]", raw_vols) if v.strip()]
        cleanup_paths = [cdir_host, ddir_host]
        for b in raw_vols:
            b = str(b).strip()
            if ":" not in b:
                continue
            hp, cp = b.split(":", 1)
            hp_local = st.host_to_local(hp, allow_root=False)   # 校验越界
            os.makedirs(hp_local, exist_ok=True)
            binds.append("%s:%s" % (hp.rstrip("/"), cp.strip()))
            cleanup_paths.append(hp.rstrip("/"))
        if form.get("mount_data", True):
            binds.append("%s:/data" % ddir_host)

        body = {"Image": ref, "Hostname": name,
                "Env": env or None, "ExposedPorts": exposed or None,
                "HostConfig": {"PortBindings": pb or None, "Binds": binds,
                               "RestartPolicy": {"Name": str(form.get("restart") or s["restart"])},
                               "NetworkMode": str(form.get("network") or s["network"])}}
        body = {k: v for k, v in body.items() if v is not None}
        compose_lines = [
            "services:", "  %s:" % name, "    image: %s" % ref,
            "    container_name: %s" % name,
            "    restart: %s" % str(form.get("restart") or s["restart"]),
        ]
        if host_ports:
            compose_lines.append("    ports:")
            for p in ports_raw:
                compose_lines.append("      - \"%s\"" % p)
        if env:
            compose_lines.append("    environment:")
            for e in env:
                compose_lines.append("      - %s" % e)
        if binds:
            compose_lines.append("    volumes:")
            for b in binds:
                compose_lines.append("      - %s" % b)
        with open(os.path.join(app_dir, "docker-compose.yml"), "w", encoding="utf-8") as f:
            f.write("\n".join(compose_lines) + "\n")
        with open(os.path.join(app_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write("# %s\n\n镜像：`%s`\n\n由 DockerUpdate「镜像仓库」创建于 %s。\n\n"
                    "- 配置文件目录：`%s`\n- 数据卷目录：`%s`\n"
                    % (_display(full), ref, _now(), cdir_host, ddir_host))

        emit(stage="创建并启动容器", pct=76, detail=name)
        created = self.docker.create_container(name, body)
        cid = (created or {}).get("Id") or ""
        if not cid:
            raise ValueError("Docker 未返回容器 ID")
        self.docker.start(cid)

        emit(stage="同步桌面图标", pct=92, detail="")
        desktop = {"ok": True, "skipped": True, "message": "桌面图标未启用"}
        if form.get("auto_desktop", s.get("auto_desktop", True)):
            app = {"id": "reg-" + name, "name": _display(full) + " · " + name,
                   "icon": "/api/registry/icon?name=" + urllib.parse.quote(full, safe=""),
                   "homepage": ("http://%s:%s" % (config.HOST_IP, host_ports[0])) if host_ports else "",
                   "ports": host_ports}
            desktop = self.desktop.create(app, {"installed_at": _now()})
        d = self._load()
        d["created"]["reg-" + name] = {"name": _display(full), "container": name,
                                       "image": ref, "cid": cid, "compose_dir": cdir_host,
                                       "data_dir": ddir_host, "app_root": app_root,
                                       "cleanup_paths": sorted(set(cleanup_paths)),
                                       "desktop": desktop, "created_at": _now(), "ports": host_ports}
        self._save(d)
        return {"ok": True,
                "message": "已创建并启动容器「%s」(%s)%s" % (
                    name, ref, "，桌面图标已同步" if not desktop.get("skipped") else "，桌面图标适配器未启用"),
                "container": name, "image": ref, "cid": cid,
                "compose_dir": st.local_to_host(app_dir), "desktop": desktop}

    def _cleanup_roots(self, rec):
        """算出要删除的宿主机目录，只认本模块写入清单的路径。

        安全边界（防止误删 NAS 上其它文件夹）：
          · 必须在可写根 STORE_HOST_ROOT 之下，且不等于可写根本身；
          · 路径中必须出现「容器名」这一段，并上提到以容器名结尾的那一层，
            这样 v3.3 之后 <根>/<容器名>/{data,share,docker-compose.yml} 一次性删干净；
          · 兼容 v3.2 及更早的旧记录（配置根 / 数据根分开），分别上提后删除；
          · 拒绝相对路径、..、符号链接。
        """
        name = str(rec.get("container") or "").strip()
        if not name:
            return []
        host_root = config.STORE_HOST_ROOT.rstrip("/")
        raw = []
        for p in (rec.get("cleanup_paths") or []):
            raw.append(str(p))
        for k in ("app_root", "compose_dir", "data_dir", "share_dir"):
            if rec.get(k):
                raw.append(str(rec[k]))
        out = []
        for p in raw:
            p = str(p or "").strip().rstrip("/")
            if not p.startswith("/") or ".." in p.split("/"):
                continue
            if not p.startswith(host_root + "/"):
                continue                       # 绝不允许等于可写根或越界
            parts = p[len(host_root):].strip("/").split("/")
            if name not in parts:
                continue                       # 路径里没有容器名 → 不是本应用的目录
            idx = parts.index(name)
            target = host_root + "/" + "/".join(parts[:idx + 1])
            if target != host_root and target.startswith(host_root + "/"):
                out.append(target)
        # 去重：长的在前，去掉被其它目标包含的
        out = sorted(set(out), key=len, reverse=True)
        res = []
        for p in out:
            if not any(p == q or p.startswith(q + "/") for q in res):
                res.append(p)
        return res

    def remove_created(self, key):
        """彻底删除：容器 + 匿名卷 + 桌面图标 + 配置文件 / 数据 / 共享目录 + 清单记录。

        v3.4 修复：容器已被外部删除 / 用 compose 重建过 ID 变化时，Docker 会返回
        「No such container: <旧 cid>」——旧版直接抛错并中断，导致清单、图标、文件全删不掉。
        现在把 404 视为「容器已不在」，无论哪一步出问题都继续把本地残留清干净。
        """
        d = self._load()
        rec = d["created"].get(key)
        if not rec:
            raise ValueError("该容器不是本模块创建的")
        name = str(rec.get("container") or "")
        warnings = []

        # ① 删容器：优先按创建时的 cid（避免误删同名新容器），再按容器名；404 视为已不在
        removed = False
        for ident in (rec.get("cid"), name):
            ident = str(ident or "").strip()
            if not ident:
                continue
            try:
                self.docker.remove_container(ident, force=True, volumes=True)
                removed = True
                break
            except DockerError as exc:
                msg = str(exc)
                low = msg.lower()
                if "no such container" in low or " 404" in low or "not found" in low or exc.status == 404:
                    removed = True             # 容器已经不在了，继续清理本地残留
                    warnings.append("容器 %s 已不在 Docker 中（可能之前被手动删除或重建），已跳过" % name)
                    break
                last = msg
            except Exception as exc:
                last = str(exc)
        if not removed:
            warnings.append("删除容器失败：%s（本地记录与文件仍会清理，请手动确认容器状态）" % locals().get("last", "未知错误"))

        # ② 桌面图标（失败不影响后续）
        try:
            self.desktop.remove(key)
        except Exception as exc:
            warnings.append("桌面图标移除失败：%s" % exc)

        # ③ 彻底删除该容器的所有文件（配置 / Compose / data / share 都在同一个容器名文件夹里）
        deleted, kept = [], []
        for host_path in self._cleanup_roots(rec):
            try:
                local = st.host_to_local(host_path, allow_root=False)
                if os.path.islink(local):
                    kept.append(host_path + "（符号链接，未删除）")
                elif os.path.isdir(local):
                    shutil.rmtree(local)
                    deleted.append(host_path)
                else:
                    deleted.append(host_path + "（原本就不存在）")
            except Exception as exc:
                kept.append("%s（%s）" % (host_path, exc))

        # ④ 无论前面有没有报错，清单一定清掉，保证「删得掉」
        d["created"].pop(key, None)
        self._save(d)
        self.cache.clear()

        bits = ["容器「%s」%s" % (name, "已删除" if removed else "清理完成"),
                "桌面图标已移除",
                ("已彻底删除目录：%s" % "、".join(deleted)) if deleted else "没有需要删除的目录"]
        if kept:
            bits.append("以下目录未能删除：%s" % "；".join(kept))
            warnings.append("部分文件未删干净，请手动检查")
        msg = "；".join(bits) + "。"
        if warnings:
            msg += " 注意：" + "；".join(warnings) + "。"
        return {"ok": True, "message": msg, "removed_dirs": deleted, "warnings": warnings}

    # ---------- 对外动作（长操作走 JOBS，避免网关 504） ----------
    def action(self, kind, body, progress=None):
        runtrace.note("Registry.action", "调用 kind=%s · %s" %
                      (kind, runtrace.brief(body)), "step")
        if kind == "state":
            return self.state()
        if kind == "search":
            return self.search(str(body.get("q") or ""), str(body.get("source") or ""),
                               int(body.get("page") or 1), int(body.get("page_size") or 0))
        if kind == "tags":
            return self.tags(str(body.get("image") or ""), str(body.get("source") or "dockerhub"))
        if kind == "settings":
            return self.set_settings(body)
        if kind == "source-add":
            return self.add_source(body)
        if kind == "source-delete":
            return self.delete_source(str(body.get("id") or ""))
        if kind == "source-toggle":
            return self.toggle_source(str(body.get("id") or ""))
        if kind == "preset-restore":
            return self.restore_presets()
        if kind == "remove-created":
            return self.remove_created(str(body.get("id") or ""))
        if kind == "pull":
            return self.pull(body, progress=progress)
        if kind == "pull-batch":
            # v3.5：聚合搜索时已按镜像名记下「哪些库有它」，批量下载同样自动选最快仓库
            idx = getattr(self, "_source_index", {}) or {}
            # v3.2：镜像目录多选后批量下载，逐个拉取、单个失败不拖累其它镜像
            def emit(**kw):
                if progress:
                    progress(**kw)
            raw = body.get("images") or []
            refs = []
            for x in raw[:30]:
                full = _slug(str(x if isinstance(x, str) else (x.get("image") or "")))
                if full and full not in refs:
                    refs.append(full)
            if not refs:
                raise ValueError("请先勾选要下载的镜像")
            tag = str(body.get("tag") or self.settings()["default_tag"] or "latest").strip()
            n = len(refs)
            ok, fail = [], []
            emit(stage="准备批量任务", pct=2, detail="共 %d 个镜像" % n)
            for i, full in enumerate(refs):
                ref = full if ":" in full.rsplit("/", 1)[-1] else "%s:%s" % (full, tag)
                emit(stage="逐个拉取镜像", pct=int(4 + 88 * i / n),
                     detail="(%d/%d) %s" % (i + 1, n, ref))
                try:
                    cands = idx.get(full.lower()) or next(
                        (v for k, v in idx.items() if k.endswith("/" + full.lower())), [])
                    self.pull({"image": full, "tag": tag, "source": body.get("source"),
                               "sources": cands})
                    ok.append(ref)
                except Exception as exc:
                    fail.append({"ref": ref, "error": str(exc)[:200]})
            emit(stage="校验本地镜像库", pct=96, detail="成功 %d / 失败 %d" % (len(ok), len(fail)))
            return {"ok": True, "ok_count": len(ok), "fail_count": len(fail), "fail": fail,
                    "message": "批量拉取完成：成功 %d 个，失败 %d 个%s"
                               % (len(ok), len(fail), ("（" + "；".join(x["ref"] for x in fail[:3]) + "）") if fail else "")}
        if kind == "create-container":
            return self.create_container(body, progress=progress)
        raise ValueError("未知的镜像仓库操作：%s" % kind)

    LONG = {
        "pull": ("拉取镜像", PULL_STAGES),
        "pull-batch": ("批量拉取镜像", ["准备批量任务", "逐个拉取镜像", "校验本地镜像库", "汇总结果"]),
        "create-container": ("拉取镜像并创建容器", CREATE_STAGES),
    }

    def submit(self, kind, body):
        """与商店一致：POST 立即返回 job_id，后台线程 + SSE 推进度。"""
        if kind not in self.LONG:
            return self.action(kind, body)
        label, stages = self.LONG[kind]
        if kind == "pull-batch":
            label = "批量拉取 %d 个镜像" % len(body.get("images") or [])
        ref = _display(_slug(body.get("image") or body.get("full_name") or ""))
        if ref:
            label = "%s「%s」" % (label, ref)
        jid = JOBS.submit("registry-" + kind, label, stages,
                          lambda emit: self.action(kind, body, progress=emit))
        return {"ok": True, "async": True, "job_id": jid,
                "message": "「%s」已提交为后台任务，可在进度条里实时查看" % label}
