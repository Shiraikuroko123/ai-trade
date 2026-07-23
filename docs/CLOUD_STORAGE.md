# Cloudflare R2 行情快照

AI Trade 可以把已校验的本地行情缓存保存为 Cloudflare R2 对象快照。该功能是可选备份，不是网络盘、实时数据源、跨电脑账户同步或实盘交易服务。仓库不包含项目作者的账号、存储桶或令牌；从 GitHub 克隆项目的普通用户可以配置自己的私有 R2，并在自己的工作台查看相同的存储页和本机统计。

## 数据边界

每个快照只包含当前配置标的对应的 `data/cache/*.csv`、云端安全导出版 `data/cache/manifest.json`，以及快照内部生成的 `snapshot-manifest.json`。创建前会从 CSV 重新计算行数、每支最新日和实际共同完成交易日，并与活动 manifest 核对。云端 manifest 采用严格字段白名单：保留来源路由、结构化错误类型、回退状态、精度元数据和 SHA-256，但删除任意额外字段、原始异常文本、URL 和本地路径，避免把错误消息中的凭据带入 R2。

腾讯来源条目的精度元数据也会保留：当前响应呈现两位“万元”量化，即 100 元分辨率；50 元只是按四舍五入解释时的名义误差界限，并非接口承诺。最新日是否使用报价字段精确覆盖由 manifest 单独标记。R2 只保存这些证据，不会提高行情精度，也不会把公共提供方数据变成交易所认证数据。

以下内容不在备份范围内：

- `reports/`、`state/`、`logs/` 和 `local/`
- 模拟账户、成交/拒单/净值账本和内测用户文件
- 会话、密码验证器、券商账号或 API 凭据
- 实盘授权、紧急停止文件和其他实盘控制状态
- `.env`、用户环境配置和云存储凭据

上传使用白名单，而不是先打包整个工作区再排除敏感文件。R2 中的对象位于 `ai-trade/<installation-id>/v1/` 命名空间；不同本地安装默认不会相互覆盖。

## 安装与配置

源码工作区先安装云端可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[cloud]'
```

通过 wheel 或软件包索引安装时使用：

```powershell
python -m pip install 'ai-trade[cloud]'
```

交互式配置脚本只随源码仓库和源码发行包提供。源码用户在仓库根目录运行它，并按提示输入自己的 Cloudflare Account ID、私有 R2 bucket、Access Key ID 和 Secret Access Key：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_cloud.ps1
```

Cloudflare 账户使用司法辖区 endpoint 时增加 `-Jurisdiction eu` 或 `-Jurisdiction fedramp`。仅本机迁移既有 Paper Scout 明文环境文件时，必须显式指定来源，不存在自动搜索本机文件的行为：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_cloud.ps1 -ImportPaperScout -PaperScoutEnv <path-to-env>
```

仅安装 wheel 的用户可以从同版本源码发行包取得该脚本，或通过操作系统的用户级环境变量界面手工设置下列变量；wheel 不会把配置脚本安装到当前目录。每台电脑应使用自己的 `AI_TRADE_CLOUD_INSTALLATION_ID`，不得复制项目作者或其他用户的凭据。

脚本只为当前 Windows 用户设置以下环境变量，不会写入仓库文件：

- `AI_TRADE_CLOUD_ENABLED`
- `AI_TRADE_CLOUD_PREFIX`
- `AI_TRADE_CLOUD_INSTALLATION_ID`
- `AI_TRADE_R2_ENDPOINT`
- `AI_TRADE_R2_REGION`
- `AI_TRADE_R2_BUCKET`
- `AI_TRADE_R2_ACCESS_KEY_ID`
- `AI_TRADE_R2_SECRET_ACCESS_KEY`

用户环境变量变更不会自动进入已经运行的进程。配置完成后关闭并重新打开终端，重启 AI Trade，再执行连接检查：

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-status
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-status --check
```

wheel 用户把上述命令替换为 `ai-trade cloud-status` 和 `ai-trade cloud-status --check`。

状态和备份输出只显示公共状态、摘要和快照 ID，不回显 endpoint、bucket、对象 key、Access Key 或 Secret Key。未配置用户执行备份时会失败关闭，不会使用他人的账号。

连接参数只存在于启动 AI Trade 的当前 Windows 用户环境中。浏览器不会接收 R2 endpoint、bucket、installation ID、命名空间、对象 key、Access Key 或 Secret Key；这些值也不会进入报告、快照 manifest 或 Git 仓库。不要把环境变量转存到 `.env`、教程、截图或问题报告中。朋友在自己的电脑部署时，必须使用他自己的 R2 配置；内测登录账号不会携带或共享云存储权限。

## 存储策略与工作台

启动工作台后进入“存储”页，可以选择两种自动保存策略：

- **仅本地（`local`）**：活动行情、回测和模拟盘继续使用本地文件，不自动上传 R2。已经配置 R2 时，仍可显式点击“备份行情”创建一次快照。
- **本地 + R2（`hybrid`）**：本地文件仍是唯一活动工作区；后续成功的行情下载、带刷新的信号流程和模拟盘流程会尝试追加 R2 快照。上传失败只记录告警，不会把已经有效的本地任务改判为失败。

系统不提供“纯云端活动缓存”模式。回测和模拟盘需要一致、可锁定、可校验的本地缓存，恢复快照也必须先进入独立暂存目录。没有完整 R2 配置时不能选择 `hybrid`，并会安全回落到本地运行。

“存储”页提供以下操作：

- **清点云端**：分页列出当前安装命名空间，更新全部对象的容量与对象数，并保留最近按 ID 排序的最多 1,000 条安全快照摘要；这是一次真实 R2 列表操作。
- **备份行情**：启动一次后台 `cloud-backup` 任务，只上传通过校验的行情快照。
- **保存设置**：保存存储策略、容量预算、A/B 类操作预算和用户预算周期起始日；这些非敏感设置保存在 Git 忽略的 `state/cloud_profiles/<profile-id>/preferences.json`。

清点结果和本机操作账本保存在同一配置目录下的 `usage.sqlite3`，因此重启工作台后仍可查看。`<profile-id>` 是由 endpoint、region、bucket、prefix 和 installation ID 生成的不可逆本地指纹；它不会返回浏览器，也不包含访问密钥。更换 R2 账号、bucket 或 installation ID 会切换到独立目录，旧配置的容量、快照和 A/B 计数不会混入新视图。页面首次打开只读取当前配置最近一次本地清点缓存，不会在每次浏览时静默访问 R2；需要最新容量和快照列表时点击“清点云端”。恢复仍使用命令行，以保留明确的暂存目录和人工复核步骤。

## 用量与预算口径

工作台显示的数值用于本机管理，不是 Cloudflare 官方账单或全账户用量：

- **R2 容量**：最近一次“清点云端”时，当前 `installation ID` 命名空间内对象大小的合计。它不包含同一 bucket 的其他前缀、其他 AI Trade 安装或其他应用，也不是 Cloudflare 可能采用的计费平均值。
- **A 类操作**：AI Trade 在本机观测到的 `put_object` 和 `list_objects_v2` 高层请求；上传和云端清点通常会增加该计数。
- **B 类操作**：AI Trade 在本机观测到的 `head_object` 和 `get_object` 高层请求；上传后的对象校验、重复快照检查、最新指针读取和恢复可能增加该计数。

A/B 账本从当前公开发行版 `v0.17.0` 在当前工作区首次启用云功能时开始，无法补记首次使用前的操作。它不包含 Cloudflare 控制台、其他应用、其他设备、其他 AI Trade 工作区或 SDK 内部重试发出的请求。因此页面明确标记为“本机观测”，不能用它替代 Cloudflare Analytics 或账单。

容量预算、A/B 操作预算及“剩余”均由用户自行填写。初始值为 10 GB、A 类 1,000,000 次、B 类 10,000,000 次；这些只是可编辑的本地管理默认值，不证明账户享有相应免费额度，也不会阻止请求、删除对象或改变 Cloudflare 计费。用户预算周期起始日可设为每月 1 至 28 日，按 UTC 日期划分本机 A/B 统计区间；它不代表 Cloudflare 官方计费周期。预算不足时页面的剩余值最低显示为 0，实际超出量仍会保留在本机账本中。

## 备份、查看与恢复

先确保本地行情缓存通过正常下载和校验，再上传快照：

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli download --force
.\.venv\Scripts\python.exe -m ai_trade.cli doctor
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-backup
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-list --limit 20
```

wheel 用户使用对应的 `ai-trade download --force`、`ai-trade doctor`、`ai-trade cloud-backup` 和 `ai-trade cloud-list --limit 20` 命令。

刷新使用腾讯网络回退或合格的本地回退缓存时，云快照会保留逐标的来源、实际共同交易日和脱敏后的结构化错误类型，不会把上传时间或请求上界伪装成行情日期。

通过 `scripts/install_paper_task.ps1` 安装的每日模拟盘任务会加载当前 Windows 用户环境；只有完整配置 R2 并选择 `hybrid` 后，成功的模拟盘刷新流程才会尝试追加一次行情快照。R2 失败只写入 `logs/scheduled_paper.log`，不会推翻已经成功落盘的行情、模拟盘或审计结果。

从 `cloud-list` 取得快照 ID 后，可下载到默认暂存区：

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-restore <snapshot-id>
```

默认目标为 `local/cloud-restore/<snapshot-id>/`。也可以用 `--directory` 指定一个尚不存在的新目录。恢复流程会限制对象和成员大小，验证对象 SHA-256、内部清单、允许的文件名、每个文件哈希和行情 schema，并拒绝路径穿越、符号链接、额外文件与混合标的集。

恢复命令只写暂存区并返回 `active_cache_unchanged: true`，不会自动覆盖 `data/cache`。检查暂存清单、截止日、标的范围和 `doctor` 预期后，再由操作者决定是否采用；不要在运行回测、工作台任务或模拟盘推进时替换活动缓存。

## 凭据与轮换

建议为 AI Trade 创建独立、仅限所需私有 bucket 的 R2 API token，不与其他应用共用。Windows 当前用户环境变量会保存在用户配置中，也可被以该用户身份运行的程序读取，因此它只是本地配置边界，不等同于密码保险库。

本机可以从既有 Paper Scout 环境配置迁移参数，但其来源若是明文环境文件，应视为已有共享面。迁移时不要打印、截图或提交来源路径和 token；完成后创建独立的 AI Trade 最小权限 token，撤销或轮换旧 token，并检查 R2 审计记录和对象列表。

怀疑凭据泄露时，先在 Cloudflare 撤销 token，再生成新 token 并重新运行配置脚本。禁用本机云功能并清除当前用户设置：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_cloud.ps1 -Disable
```

禁用会清除当前 Windows 用户的云开关、endpoint、bucket 和访问凭据，但保留非敏感的 installation ID 与 prefix。以后重新配置同一 R2 账号和 bucket 时会重新连接原命名空间；它不会删除 R2 中已有快照。云端保留期限和删除策略由 bucket 所有者在 Cloudflare 中单独管理。若要有意创建全新的独立命名空间，应先自行保存旧 installation ID，再显式清除 `AI_TRADE_CLOUD_INSTALLATION_ID` 后重新配置；丢失旧 ID 会使旧快照无法通过普通列表命令定位。
