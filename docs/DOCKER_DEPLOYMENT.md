# Docker 部署

Docker 方案用于从源码启动 `v0.14.0` 工作台。它不改变策略、回测、风控、
模拟账本或券商权限，也不会把源码部署能力伪装成发行 wheel 中不存在的远程托管服务。

## 安全边界

- 容器必须使用内测登录。`--container-bind` 与 `--owner-local` 同时使用会拒绝启动。
- Compose 只把端口发布到宿主机 `127.0.0.1`，默认地址为
  `http://127.0.0.1:8877/`。不要把端口映射改成 `0.0.0.0`。
- 容器进程虽然监听容器内部接口，但 Host 校验只接受 `localhost` 或回环 IP；
  这是为了适配 Docker 端口转换，不是远程托管模式。
- 当前服务没有 HTTPS、集中式会话撤销或互联网级身份系统。不要通过路由器端口转发、
  公网隧道或反向代理把它交给异地用户。
- 容器以非 root 用户运行，根文件系统只读，删除 Linux capabilities，并启用
  `no-new-privileges`。持久化写入仅进入显式挂载目录。

## 首次启动

需要 Docker Desktop 或 Docker Engine 与 Compose v2。在仓库根目录执行：

```powershell
Copy-Item .\docker.env.example .\.env.docker
docker compose --env-file .\.env.docker build
docker compose --env-file .\.env.docker run --rm workstation beta-user-add shiraikuroko
docker compose --env-file .\.env.docker up -d
docker compose --env-file .\.env.docker ps
Start-Process http://127.0.0.1:8877/
```

`beta-user-add` 会在终端中两次读取密码，不会把明文写入命令历史或
命名卷中的 `state/beta_users.json`。首次命令会自动在命名卷中生成独立配置、
行情、报告、状态和日志目录。已有账号时可先运行：

```powershell
docker compose --env-file .\.env.docker run --rm workstation beta-user-list
```

首次没有行情缓存时，登录后在“数据”页运行刷新，也可以使用 CLI：

```powershell
docker compose --env-file .\.env.docker run --rm workstation download --force
```

## 默认数据卷

| Docker 命名卷 | 容器路径 | 口径 |
|---|---|---|
| `ai-trade_config` | `/workspace/config` | 配置和证券主数据 |
| `ai-trade_data` | `/workspace/data` | 行情缓存与 manifest |
| `ai-trade_reports` | `/workspace/reports` | 回测、验证和模拟日报 |
| `ai-trade_state` | `/workspace/state` | 内测账号、模拟账本、研究与审计状态 |
| `ai-trade_logs` | `/workspace/logs` | 运行日志 |
| `ai-trade_local` | `/workspace/local` | 云恢复暂存等本机文件 |

`docker compose down` 不会删除这些卷，重建镜像也不会覆盖它们。
`docker compose down -v` 会永久删除当前 Compose 项目的所有卷，因此不能作为
普通更新命令。`.env.docker` 仍位于仓库目录并被 Git 忽略。

## 复用仓库现有数据

只有确实要让容器直接读取当前仓库的 `config/data/reports/state/logs/local`
时，才叠加绑定挂载文件；之后每条 Compose 命令都必须带两个 `-f` 参数：

```powershell
New-Item -ItemType Directory -Force data,reports,state,logs,local | Out-Null
docker compose --env-file .\.env.docker `
  -f .\compose.yaml -f .\compose.bind.yaml up -d
```

Docker Desktop 必须允许 Linux 容器访问仓库所在磁盘。如果日志出现
`bind source path does not exist`，先在 Docker Desktop 文件共享设置中允许该
磁盘，或继续使用默认命名卷；不要通过放宽服务监听地址来绕过挂载问题。

Linux 用户应在 `.env.docker` 中把 `AI_TRADE_DOCKER_UID` 和
`AI_TRADE_DOCKER_GID` 改为 `id -u` / `id -g` 的结果，并确保上述目录由该用户
可写。这两个值主要影响绑定挂载模式；默认命名卷使用镜像内的非 root 身份。
Docker Desktop for Windows 通常可直接使用默认值。

## 可选 AI、Webhook 与 R2

只有容器显式收到的环境变量才可见。需要模型增强、监控 Webhook 或 R2 时，
在本地 `.env.docker` 填写对应变量再重建容器；本地规则无需 AI Key，本机通知
收件箱无需 Webhook，纯本地存储无需 R2 凭据。外部 Webhook 必须使用 HTTPS，
密钥至少 16 个 UTF-8 字节；HTTP 只允许容器可达的回环测试端点。

```powershell
docker compose --env-file .\.env.docker up -d --force-recreate
```

`.env.docker` 被 Git 和 Docker 构建上下文排除，但本机管理员仍能通过 Docker
检查容器环境。它不是密钥保险库；应使用最小权限 R2 凭据并定期轮换。

## 日常操作

```powershell
# 状态与健康
docker compose --env-file .\.env.docker ps

# 最近日志
docker compose --env-file .\.env.docker logs --tail 200 workstation

# 重启
docker compose --env-file .\.env.docker restart workstation

# 停止
docker compose --env-file .\.env.docker down
```

更新 `main` 后重新构建，持久化目录不会被镜像覆盖：

```powershell
git pull --ff-only
docker compose --env-file .\.env.docker build --pull
docker compose --env-file .\.env.docker up -d
```

不要使用 `down -v` 作为普通更新步骤。若启动失败，先查看日志；配置中
`auth.enabled=false`、把 Compose 命令改成 `--owner-local`、端口冲突或绑定目录
无写权限都会导致明确失败。
