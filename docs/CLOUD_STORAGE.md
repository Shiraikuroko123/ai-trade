# Cloudflare R2 行情快照

AI Trade 可以把已校验的本地行情缓存保存为 Cloudflare R2 对象快照。该功能是可选备份，不是网络盘、实时数据源、跨电脑账户同步或实盘交易服务。普通安装保持关闭且不包含任何项目作者的账号、存储桶或令牌；每位用户必须配置自己的私有 R2。

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

## 备份、查看与恢复

先确保本地行情缓存通过正常下载和校验，再上传快照：

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli download --force
.\.venv\Scripts\python.exe -m ai_trade.cli doctor
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-backup
.\.venv\Scripts\python.exe -m ai_trade.cli cloud-list --limit 20
```

wheel 用户使用对应的 `ai-trade download --force`、`ai-trade doctor`、`ai-trade cloud-backup` 和 `ai-trade cloud-list --limit 20` 命令。

刷新使用腾讯网络回退或合格的本地回退缓存时，云快照会保留逐标的来源、实际共同交易日和原始错误，不会把上传时间或请求上界伪装成行情日期。

通过 `scripts/install_paper_task.ps1` 安装的每日模拟盘任务会在当前 Windows 用户明确启用云配置后追加一次行情快照。R2 失败只写入 `logs/scheduled_paper.log`，不会推翻已经成功落盘的行情、模拟盘或审计结果。

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

禁用只移除本机环境设置，不删除 R2 中已有快照。云端保留期限和删除策略由 bucket 所有者在 Cloudflare 中单独管理。
