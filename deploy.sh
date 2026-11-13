#!/bin/sh
# 在飞牛 NAS 的 SSH 终端中执行
# 首次安装或覆盖升级：把本目录所有文件放到 /vol1/1000/docker/docker-update 后运行即可
set -e
DIR=/vol1/1000/docker/docker-update
cd "$DIR"
mkdir -p data
echo "==> 启动 / 重载 DockerUpdate v7.0.4（镜像更新并重建容器 · P4 柔灰雕塑 UI）..."
# v3.9-r6：覆盖解压不会删除旧包遗留的死文件，这里主动清理已移除模块的残留。
# 这些文件不再被 index.html 引用，留着只会让浏览器缓存或静态扫描误判。
rm -f static/registry.js static/store.js moo-sources.txt \
      static/bg-light.jpg static/bg-dark.jpg \
      static/bg-lines-light.webp static/bg-lines-dark.webp
# --remove-orphans 清掉旧 service 名遗留容器，避免抢占 10000 端口
# --force-recreate 用于把上一次启动失败的容器彻底重建（挂载配置改了，restart 不会重读）
#
# 可写根必须挂在 /store，与 /:/host:ro **平级**，不能放在 /host/xxx 下面：
# 父挂载是 ro，runc 创建子挂载点会报 "mkdirat ... read-only file system"，容器起不来。
# 若你手工改过 compose，请确认这两行：
#   - /vol1/1000/docker:/store:rw
#   STORE_ROOT: "/store"
docker compose up -d --remove-orphans --force-recreate
echo ""
echo "==> 完成，浏览器打开： http://192.168.1.110:10000"
echo "==> 默认账号 admin / 密码 ABab12.. （登录后请立刻在 设置 → 账户安全 修改）"
echo "==> 容器名： docker-update"
echo "==> 7 个主模块： 概览 / 应用 / 容器 / 更新 / 本地镜像 / 告警 / 系统联动；通知渠道已并入告警页"
echo "==> 更新大模块下有两个独立子页： 容器更新 / 镜像更新（可只拉取，也可拉取后重建容器）"
echo "==> r20：检测到可更新版本才显示更新按钮；容器卡片「自动更新」已改为开关"
echo "==> 应用商店分区、镜像仓库模块、Compose 栈、应用模板、网络/卷、备份恢复栏均已移除"
echo "==> 访问地址：容器卡片「链接设置」、应用卡片链接图标，内网自动带端口、外网逐个自填"
echo "==> 可写根： /vol1/1000/docker（容器内 /store）：容器配置、版本备份与系统联动数据"
echo "==> 覆盖升级不会删除 data/ 里的策略、密码、名称备注、监控历史、版本备份与系统联动数据"
echo "==> 升级后请在浏览器按 Ctrl+F5 强制刷新一次，避免沿用缓存的旧 JS"
echo "==> 【重要】通知格式与 CSS 都在容器进程内，仅覆盖文件不会生效：本脚本已带 --force-recreate 重建容器"
echo "==> 升级后请到「告警 → 通知自检」确认模块版本为 v7.0.4，再点「测试推送」验证微信端显示"
echo "==> 新增：容器 → 目录映射（飞牛文件夹 → 容器内自定义路径），保存配置不碰容器，核对计划后再应用重建"
echo "==> v7.0.2：新容器默认映射模板新增「功能总开关」，默认关闭；开启后才会给新容器套模板"
echo "==> v7.0.2：进度条已显示后台运行明细（HTTP / Docker API / 业务阶段），可折叠、复制、收起"
echo "==> v7.0.3：UI 换为 P4 柔灰雕塑（纯灰阶 / 大留白 / 20px 圆角，零彩色装饰）"
echo "==> v7.0.3：修复 Web 终端连不上、进程/Inspect 报容器不存在、端口只显示 2 个、路径按钮无图标"
echo "==> v7.0.3：目录映射两列都有路径按钮；「文件软链接（A→B 改名）」入口已标明，不与映射混淆"
echo "==> v7.0.4：镜像更新页新增「更新并重建容器」，先查关联容器、再备份 / 保留原配置重建 / 健康检查失败自动回退"
echo "==> v7.0.4：进度条明细逐行显示拉取、匹配容器、备份、重建、回退；版本号统一 v7.0.4"
echo "==> 查看日志： docker logs -f docker-update"
