# 零成本近期行情与前向证据操作手册

本文面向没有 RQData 账号、也不准备购买 Tushare 高积分权限的个人研究用户。当前零成本路线使用东方财富作为配置主源、腾讯作为网络回退，Yahoo 做短窗口独立核对；可选 BaoStock 只用于异常监测。公开接口没有授权数据源的 SLA 或许可保证，因此所有结果固定为 `research_only`，`live_ready` 保持 `false`。

## 已有本地仓库如何更新

在 PowerShell 中进入工程目录，先确认没有尚未保存的个人修改：

```powershell
Set-Location .\ai-trade
git status --short
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -e .
.\.venv\Scripts\python.exe -m ai_trade.cli doctor
```

如果 `git status --short` 有输出，应先提交、备份或明确处理这些修改，再执行 `git pull --ff-only`。不要用 `git reset --hard` 丢弃本地工作。editable 安装会让虚拟环境直接使用当前源码；更新依赖声明或命令入口后仍应重新运行上面的安装命令。

对于刚刚完成本次提交的工作目录，不需要再从远端拉取同一个提交，直接执行下文的每日流程即可。

## 第一次从 GitHub 安装

需要 Python 3.10 或更高版本、Git 和 Windows PowerShell：

```powershell
git clone https://github.com/Shiraikuroko123/ai-trade.git
Set-Location .\ai-trade
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe -m ai_trade.cli doctor
.\.venv\Scripts\python.exe -m ai_trade.cli download --force
.\.venv\Scripts\python.exe -m ai_trade.cli feature-forward-run
```

`bootstrap.ps1` 会创建 `.venv`、安装当前源码并运行完整测试，所以首次执行需要一些时间。不要把账号密码、Token 或代理凭据写入仓库配置；需要凭据的可选服务使用当前 Windows 用户环境变量。

## 每个交易日如何操作

在市场收盘且日线稳定后执行，建议安排在 18:00 以后：

```powershell
Set-Location .\ai-trade
.\.venv\Scripts\python.exe -m ai_trade.cli download --force
.\.venv\Scripts\python.exe -m ai_trade.cli feature-forward-run
```

两个命令必须按顺序运行：

1. `download --force` 刷新并原子发布整套行情缓存，同时尝试独立核对。
2. `feature-forward-run` 不联网，只读取刚发布的缓存，创建当日 FeatureSnapshot，并为已经成熟的旧快照补建 5/20/60 日 LabelSnapshot。

成功输出应满足：

- `feature.genuine_pit` 为 `true`；
- `feature.rows` 与当前证券池数量一致；
- `feature.provider` 是逐文件实际供数者，而不是配置中的首选源；
- 同一天相同输入重跑时 `feature.reused` 为 `true`，且 snapshot ID 不变；
- 尚未经过足够未来交易会话的标签显示为 `pending`。

只有特征日、缓存最新共同会话和运行时已完成会话截止日三者一致，记录才属于 genuine PIT。晚几天再为旧日期补做的快照即使数值相同，也不能进入部署训练。不要用 `--historical-reconstruction` 冒充未来积累证据。

## 常见异常

### 行情刷新失败

如果 `download --force` 返回非零退出码，不要继续创建当日快照。保留旧缓存，稍后重试并运行：

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli doctor
```

系统会先在候选目录完成整套校验，失败不会用半套数据覆盖活动缓存。

### 独立核对出现冲突

查看 `data/cache/manifest.json` 中的 `cross_source_check`。`failed / independent_conflict` 表示公开来源存在未裁决差异；它不会自动改写主缓存，也不会阻止受限研究积累，但必须继续保持 `live_ready=false`。不要手工修改 CSV 消除冲突。

### 缓存滞后导致快照被拒绝

重新执行 `download --force`。如果上游尚未发布共同最新交易日，应等待，而不是把旧缓存强行标为当前快照。

### 云备份返回 400

本地下载成功时，云备份失败只影响可选 R2 副本。未使用 R2 的用户可以保持仅本地模式；使用 R2 的用户应单独检查端点和账号权限，不要把凭据发到 GitHub Issue、提交记录或聊天中。

## GitHub 与本地数据边界

GitHub 仓库保存源码、配置模板、测试和文档。以下内容由 `.gitignore` 排除并只留在本机：

- `data/cache/` 行情 CSV 与活动 manifest；
- `state/` 中的 Feature/Label 快照、模型、组合和账本；
- `reports/`、`logs/`、`.venv/`、`.env*` 和覆盖率文件；
- R2、模型、Tushare、JQData 或券商凭据。

因此，`git push` 不会备份前向快照。需要异机恢复时，应使用项目提供的私有 R2 流程或另行建立受控的本地备份，不要把市场数据和账户状态提交到公开仓库。

## 更新代码后的验证

提交代码前至少运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts adapters\qmt\src
.\.venv\Scripts\python.exe -m mypy
git diff --check
```

当前 20 日模型的最低评估计划需要至少 24 个成熟评估日期。考虑 20 日标签成熟时间，从第一份 genuine 快照起约需连续积累 44 个交易会话。达到这一窗口前，不应因为样本不足而购买商业数据，也不应把当前模型提升为实盘模型。
