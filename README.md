# π RuView — NiceSemi 部署版

WiFi CSI 人体感知可视化平台（基于开源项目 [ruvnet/RuView](https://github.com/ruvnet/RuView)），
由拟世 NiceSemi 完成中文化、品牌化与云端部署改造。

- 前端：官方 `ui/` 静态包（Dashboard / 实时感知 / 姿态融合 / Observatory / 模型训练），已全站中文
- 后端：`gateway.py`（stdlib HTTP 服务，托管 UI + 适配 `/api/v1/*` + 实时 WebSocket）
- 感知：`archive/v1/src/sensing/ws_server.py`（ESP32 CSI UDP 接收，无硬件时自动降级为模拟数据源）

## 目录

```
gateway.py                 HTTP 网关 + API 适配层 + 训练控制面
ui/                        前端静态资源（含 Nicesemi 品牌 logo 与页脚）
archive/v1/                sensing 管线（仅 src 与依赖锁文件）
docker/Dockerfile.cloud    云端镜像（非 root、监听 $PORT、含 HEALTHCHECK）
docker/cloud-entrypoint.sh 单容器启动：ws_server(8765) + gateway($PORT)
render.yaml                Render Blueprint（免费层）
```

## 本地运行

```bash
docker build -f docker/Dockerfile.cloud -t ruview .
docker run --rm -p 8080:8080 ruview
# 打开 http://127.0.0.1:8080
```

接真实 ESP32 时使用完整镜像（`docker/Dockerfile.python.cn`），并放行 `5005/udp`。

## 云端部署（Render 免费层）

1. Render 控制台 → **New → Blueprint**，选择本仓库，自动读取 `render.yaml`
2. 免费实例：512 MB RAM / 0.1 CPU / 750 小时/月，15 分钟无入站流量后休眠（WebSocket 流量算活动）
3. 自定义域名：服务 → Settings → Custom Domains 添加 `ruview.tech`，然后在阿里云 DNS 添加 CNAME 指向 `<service>.onrender.com`

> 注意：PaaS 容器无法接收入站 UDP，云端实例使用模拟 CSI 数据源做功能演示；
> 真实 ESP32 感知需在本机/内网服务器运行完整镜像。
