# GPT-image-2 绘图网站（image-site）

M365 Copilot2API 仓库自带的独立绘图网站：浏览器只和服务端通信，**内置的绘图
API Key 永远不出服务端**，通过网关的 `/v1/images/*` 端点生成和编辑图片。

## 功能

- 独立账号密码登录（与网关 Web 控制台互不相干）：密码只存 PBKDF2 哈希，
  会话 Cookie 为 `HttpOnly` + `Secure` + `SameSite=Strict`
- 文生图 `/v1/images/generations`、图片编辑上传 `/v1/images/edits`
  （PNG/JPEG/WebP，单图 ≤ 20 MiB）
- 尺寸三选一：`1024x1024`、`1536x1024`、`1024x1536`
- 网关账号池选择（`GET /v1/accounts`，选中后请求携带 `accountId`）
- 手机端自适应（safe-area、≥44px 触控、横屏/窄屏断点）
- 纯 Python 标准库实现，无第三方依赖（**要求 Python ≤ 3.12**，`cgi` 模块
  在 3.13 已移除）

## 架构

```
浏览器 ──HTTPS──> 反向代理 ──┬─ /image/* ──> 绘图站 127.0.0.1:4180（登录/转发）
                             │                        │ 搭载 IMAGE_SITE_API_KEY
                             └─ 其余 ────> 网关 127.0.0.1:4141 <┘
```

两个服务都只监听回环地址，**必须**通过同域反向代理访问；图片文件 URL 由服务端
改写为相对路径（`/v1/images/files/...`），浏览器按当前站点原点解析，因此换任何
域名部署都不需要改代码。直连 `127.0.0.1:4180` 调试时页面可开，但图片显示会
404（非受支持的访问方式）。

## 部署方式 A：Docker Compose（仓库根目录）

```bash
git clone https://github.com/keaidang/M365-Copilot2API-Revise.git
cd M365-Copilot2API-Revise
mkdir -p data secrets
echo "your-admin-password" > secrets/m365_admin_password   # 网关初始管理员密码

# 绘图站配置写进根目录 .env（git 忽略，不会被提交）
cat >> .env <<EOF
IMAGE_SITE_SESSION_SECRET=$(openssl rand -hex 32)
IMAGE_SITE_API_KEY=          # 先留空，见下方第 3 步
IMAGE_SITE_INITIAL_USERNAME=admin
IMAGE_SITE_INITIAL_PASSWORD=change-me-first-login
EOF

docker compose up -d --build    # 同时启动网关 + 绘图站
```

1. 打开网关 Web 控制台，登录并**创建一个带图片权限的 API Key**；
2. 把该 Key 填入 `.env` 的 `IMAGE_SITE_API_KEY`，`docker compose up -d` 重建；
3. 用 `.env` 里设置的初始账号登录绘图站，进入后立即改掉密码，然后从 `.env`
   删除 `IMAGE_SITE_INITIAL_*` 两行。

绘图站数据（用户库 SQLite）存在 Docker 命名卷 `image_site_db` 中。监听
`127.0.0.1:4180`，同样需要反向代理提供 HTTPS。

## 部署方式 B：systemd（deploy/install.sh）

```bash
sudo ./deploy/install.sh            # 编译网关 + 安装两个服务（可重复执行）
# 预览将要执行的动作（不改任何东西）：
DRY_RUN=1 ./deploy/install.sh
```

安装内容：

| 组件 | 路径 |
| --- | --- |
| 网关二进制 | `/usr/local/bin/m365-copilot2api` |
| 网关数据 | `/var/lib/m365-copilot2api` |
| 网关环境 | `/etc/m365-copilot2api.env`（首次生成） |
| 绘图站文件 | `/opt/m365-image-site`（server.py + 两个页面） |
| 绘图站数据 | `/var/lib/m365-image-site/users.db` |
| 绘图站环境 | `/etc/m365-image-site.env`（首次生成，600，含随机会话密钥） |
| 服务单元 | `deploy/systemd/*.service` → `/etc/systemd/system/` |

已有 env 文件和数据库**永远不会**被覆盖；重复执行只刷新二进制、页面和服务单元。

## 反向代理

见 [`../deploy/Caddyfile.example`](../deploy/Caddyfile.example)：
`/image/*` 转绘图站（**不剥前缀**，server.py 按 `/image` 前缀路由），其余转网关。
nginx 等价配置：`location /image/ { proxy_pass http://127.0.0.1:4180; }`。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IMAGE_SITE_HOST` | `127.0.0.1` | 监听地址（Docker 内为 `0.0.0.0`） |
| `IMAGE_SITE_PORT` | `4180` | 监听端口 |
| `IMAGE_SITE_UPSTREAM` | `http://127.0.0.1:4141` | 网关地址（Compose 内为服务名） |
| `IMAGE_SITE_DB` | `/var/lib/image-site/users.db` | 用户数据库 SQLite 路径 |
| `IMAGE_SITE_API_KEY` | —（空则生成接口返回 503） | 服务端绘图 Key |
| `IMAGE_SITE_SESSION_SECRET` | —（必填，≥32 字节） | 会话 Cookie 签名密钥 |
| `IMAGE_SITE_INITIAL_USERNAME` / `_PASSWORD` | —（可选） | 一次性首登管理员，登录后删除 |

## 安全说明

- 绘图 Key 只存在服务端 env（600 权限）；前端源码、localStorage、网络请求、
  日志中都不会出现。
- 所有 `/image/api/*` 接口未登录返回 401；密码常量时间比较 + PBKDF2
  （310000 轮）；改密码/用户名会使全部旧会话失效。
- 公网必须走 HTTPS（`Secure` Cookie 在明文下不会下发）。

## 故障排查

```bash
journalctl -u m365-copilot2api -f     # 网关（image-gen / empty completion 关键词）
journalctl -u image-site -f           # 绘图站
docker compose logs -f image-site     # Docker 方式
```

| 现象 | 原因与处理 |
| --- | --- |
| 生成报"服务端图像 API 尚未配置" | `IMAGE_SITE_API_KEY` 为空，去网关控制台建 Key 后填入 |
| `upstream is rate limiting` | 账号池冷却中，稍后重试（账号下拉可见冷却状态） |
| 图片过几分钟打不开 | 网关中转 URL 约 15 分钟过期，重新生成 |
| 登录后立刻退回登录页 | 会话密钥变了或改过密码，重新登录即可 |
