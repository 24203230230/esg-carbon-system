# 部署配置

本目录提供**内网穿透 / 公网入口**的 Compose 配置。所有真实密钥均不入库。

## 文件

| 文件 | 说明 |
| --- | --- |
| `cloudflared-esg-quick.yml` | **临时 Quick 隧道**：随机 `*.trycloudflare.com` 地址，无需账号与 Token，适合一次性测试。可安全直接使用。 |
| `cloudflared-esg-tunnel.yml.example` | **命名隧道模板**：绑定自有域名的长期入口。Token 通过环境变量 `CF_TUNNEL_TOKEN` 注入，不写死在文件里。 |

## 前置条件

```bash
# 业务容器依赖的自定义网络（与业务 docker-compose.yml 共用）
docker network create --subnet 172.23.0.0/16 ai-network
```

## 用法

### 临时测试入口（Quick 隧道）

```bash
docker-compose -f cloudflared-esg-quick.yml up -d
docker logs cloudflared-esg-quick 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'
```

新主机名在全球边缘生效约需 40 秒。

Windows 下推荐用仓库根目录的 `stack_ctl.ps1 -Action StartQuick`，它会顺带抓取并打印地址、做公网连通性检查。

### 长期公网入口（命名隧道）

```bash
# 1) 复制模板并准备本目录的 .env
cp cloudflared-esg-tunnel.yml.example cloudflared-esg-tunnel.yml
echo "CF_TUNNEL_TOKEN=<你的隧道Token>" > .env

# 2) 启动
docker-compose -f cloudflared-esg-tunnel.yml up -d

# 3) 确认隧道真的通了（200 = 有活跃边缘连接）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:20241/ready
```

同时需要在 Cloudflare Zero Trust 控制台把该主机的**已发布应用程序路由**指向服务 `http://esg-frontend:80`（填容器名，不是宿主机端口）。

> `.env` 与 `cloudflared-esg-tunnel.yml` 均已被 `.gitignore` 排除，不会误提交。

## 稳定性要点（实测结论）

- **`--protocol http2` 是硬性要求**：默认 QUIC 走 UDP，在封锁 UDP 出口的网络里必然超时掉线。
- **`--edge-ip-version 4`**：无 IPv6 出口时避免先尝试 AAAA 记录白等一轮。
- **显式 `dns`**：隧道重连需重新解析 `region1.v2.argotunnel.com`，宿主 DNS 不稳定会导致永久断连。
- **`/ready` 端点**：判断隧道状态唯一可靠的程序化信号（比 grep 日志准、比探测公网 URL 快）。建议配套看门狗：`/ready` 非 200 即重启容器，把断线恢复窗口从数分钟压到 1 分钟内。
- **Quick 隧道容器用 `restart: "no"`**：自动重启会换随机地址，使用方链接会静默失效。

## 备案期合规提醒

域名处于 **ICP 首次备案审核期**时，必须遵守：

1. 自有域名**不得解析**（记录需删除或保持不解析）；
2. 服务器**不得开放公网端口**（只留 SSH）；
3. 需要对外演示时，使用本目录的 **Quick 隧道**（不触碰自有域名），或走 SSH 端口转发：

```bash
ssh -N -L 18080:127.0.0.1:80 ubuntu@<server-ip>
# 本机浏览器访问 http://127.0.0.1:18080
```

备案通过后再放行 80/443、配置域名 A 记录与证书。
