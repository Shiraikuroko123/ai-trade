# AI Trade

[架构](docs/ARCHITECTURE.md) · [系统对照](docs/ECOSYSTEM.md) · [标的池与市场规则](docs/UNIVERSE.md) · [研究方法](docs/RESEARCH_METHODOLOGY.md) · [模拟盘运维](docs/PAPER_TRADING.md) · [安全策略](SECURITY.md) · [更新记录](CHANGELOG.md)

这是一个可审计的系统化投资研究与模拟交易工程。默认策略仍使用 A 股场内 ETF 日线，只做多、不加杠杆；底层标的池已改为时点有效的证券主数据模型，不存在“最多 8 只”的代码限制。实盘下单没有实现，并由安全检查保持关闭。

系统已经贯通以下流程：

1. 按证券上市/退市日期和成分生效区间，生成历史时点可见的动态候选池。
2. 下载、校验并以整套快照缓存候选标的真实历史行情。
3. 用趋势、相对强弱、波动率、流动性、资金容量和分组暴露生成目标仓位。
4. 按信号后下一交易日开盘成交，计入整手、滑点、佣金、印花税、过户费、停牌和涨跌停约束。
5. 运行历史回测、沪深 300 ETF 基准对比和连续滚动样本外验证。
6. 维护支持断档逐日追赶、风险冷却和幂等执行的本地模拟账户。
7. 生成 HTML、CSV、JSON 和 Markdown 审计报告。

历史收益不代表未来结果。本项目不承诺盈利，不应在未经人工检查、数据口径确认和长期模拟验证的情况下用于实盘。

## 快速开始

在 PowerShell 中运行：

```powershell
git clone https://github.com/Shiraikuroko123/ai-trade.git
cd ai-trade
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe -m ai_trade.cli download --force
.\.venv\Scripts\python.exe -m ai_trade.cli universe-status
.\.venv\Scripts\python.exe -m ai_trade.cli doctor
.\.venv\Scripts\python.exe -m ai_trade.cli backtest
.\.venv\Scripts\python.exe -m ai_trade.cli walk-forward
.\.venv\Scripts\python.exe -m ai_trade.cli validate
```

从 wheel 安装时可先创建独立工作目录：

```powershell
ai-trade init --directory .\my-ai-trade
cd .\my-ai-trade
ai-trade download --force
ai-trade doctor
```

模拟账户首次创建与日常推进：

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli paper-init
.\.venv\Scripts\python.exe -m ai_trade.cli paper-run
.\.venv\Scripts\python.exe -m ai_trade.cli paper-status
```

`paper-init` 默认创建一个 100,000 元的本地模拟账户。除非明确要开启新账期，否则不要使用 `--overwrite`；该参数会先把旧状态、成交账本、拒单账本、净值账本、审计报告和模拟日报移入 `state/archive/`，再创建新的 `account_id`。

## 工程结构

```text
ai-trade/
├── .github/                 # CI、Issue 和 PR 模板
├── config/default.json      # 策略、数据、风控、成本和模拟盘配置
├── config/security_master.json # 证券主数据、成分区间和交易状态
├── docs/                    # 架构、研究方法和模拟盘运维文档
├── scripts/                 # Windows 初始化与计划任务脚本
├── src/ai_trade/
│   ├── broker/              # 模拟账户、前向审计和实盘阻断
│   ├── data/                # 行情下载、校验、快照和市场访问
│   ├── backtest.py          # 事件驱动回测
│   ├── security.py          # 时点证券主数据与动态标的池
│   ├── strategy.py          # 信号、流动性和组合风险预算
│   ├── validation.py        # Bootstrap 与压力验证
│   └── walk_forward.py      # 连续滚动样本外验证
├── tests/                   # 无网络单元与回归测试
├── LICENSE
├── SECURITY.md
└── README.md
```

行情缓存、模拟账户、成交与净值账本、日志、报告、虚拟环境和 `.env` 不会上传 GitHub。

## 数据安全

- 交易所时区固定为中国标准时间，默认 15:30 后才把当日 bar 视为完整日线。
- 盘中下载会自动剔除当天未完成 bar，信号、回测和模拟盘只读取已完成交易日。
- 全部候选文件先写入临时快照，全部下载成功并通过 schema、日期、数值和 OHLC 校验后才发布。
- 网络刷新失败时，只允许降级到距截止日不超过 7 天的本地已校验缓存；来源会记录在 `data/cache/manifest.json`。
- `MarketData` 会核对 manifest 中的 SHA-256；混合快照或手工改坏的缓存会被拒绝。
- `doctor` 显示共同数据截止日、各标的覆盖范围、哈希及被排除的未完成日期。

## 默认策略

- 候选池包含大盘、中盘、小盘、成长、海外、黄金和国债 ETF。
- 每个标的按上市日期、退市日期、成分生效区间和 180 日上市观察期决定在信号日是否可选。
- 每 20 个交易日重新评估一次。
- 使用 126 日相对强弱，跳过最近 5 日；价格必须位于 200 日均线上方。
- 从合格资产中选择最多 3 个，按逆波动率分配，以 12% 年化波动率为风险上限。
- 20 日平均成交额必须达到 500 万元；该阈值按当前 10 万元模拟账户的参与率设置，不照搬大资金组合门槛。
- 小于组合净值 2% 的目标仓位偏差不交易，减少整手取整和短期波动造成的无意义换手。
- 默认使用保守的单资产波动率加总上界；配置也支持协方差收缩和风险平价，但它们在当前连续样本外比较中没有胜出，因此没有成为默认值。
- 单一资产上限 35%，至少保留 5% 现金。
- 同一资产类别上限 70%、同一风险分组上限 35%；单一目标仓位还受最近平均成交额 5% 的单日参与率约束。
- ETF 与股票使用不同费用表；股票印花税和过户费按历史生效日期切换。
- 组合回撤达到 15% 或单日亏损超过 3.5% 时，在下一交易日清仓并冷却 20 个交易日。

参数位于 `config/default.json`。修改后必须重跑回测和滚动验证。

## 当前研究证据

数据截止 2026-07-10，`v0.5.0` 默认配置的全历史年化收益约 10.16%、Sharpe 约 1.12、最大回撤约 -11.81%；连续滚动样本外账户年化约 11.28%、Sharpe 约 1.23、最大回撤约 -14.01%。1,000 次移动区块 Bootstrap 的年化收益 95% 区间约为 4.21% 至 17.45%，较差 5% 路径的最大回撤约 -23.78%。

这些历史窗口已经用于工程和模型判断，不再是独立最终检验集。它们只能说明当前实现值得继续模拟，不能说明未来会取得相同收益；下一份真正独立的证据来自版本冻结后的未来模拟盘。

## 报告

主要结果位于 `reports/`：

- `backtest_report.html`：使用共同坐标轴的策略/基准权益曲线、指标和最新信号。
- `backtest_summary.json`：指标、参数和带哈希的数据快照信息。
- `equity_curve.csv`：逐日权益、现金、回撤和基准权益。
- `trades.csv`：历史模拟成交。
- `walk_forward.json`、`walk_forward.md`：连续样本外账户结果；参数按区间更新，但持仓、费用、高水位和风险冷却不会重置。
- `validation_report.json`、`validation_report.md`：移动区块自助法、1/2/3 倍成本压力、参数邻域和历史危机区间测试。
- `paper_YYYYMMDD.json`：不可由同日重复运行覆盖的模拟日报。
- `state/paper_trades.csv`：带 `account_id` 和唯一 `trade_id` 的模拟成交账本。
- `state/paper_rejections.csv`：停牌、涨跌停或前置卖单失败造成的可审计拒单账本。
- `state/paper_equity.csv`：带配置指纹和行情快照 ID 的逐交易日前向净值账本。
- `paper_audit.json`、`paper_audit.md`：账本完整性、前向指标和券商沙盒晋级门槛。

## 模拟盘语义

首次 `paper-run` 使用最近完整收盘生成下一交易日目标。新交易日行情完整后再次运行，系统在该日开盘模拟成交；同一天重复运行不会重复成交或覆盖首份日报。若任务停机数日，系统会按基准交易日历逐日重放，依次处理成交、估值、风控和调仓节奏。

```powershell
.\.venv\Scripts\python.exe -m ai_trade.cli paper-audit
```

前向审计至少需要 60 个未来交易日，并要求账本完整、回撤未超限、Sharpe 为正且不落后基准。全部通过也只允许进入券商沙盒复核，不会启用真实下单。

账户状态保存策略、风险和成本配置的 SHA-256 指纹。账户创建后若配置发生变化，`paper-run` 会硬停止；审核变化后必须使用 `paper-init --overwrite` 归档旧账期并创建新的 `account_id`，不能让旧信号在新规则下静默成交。

安装每日 18:10 任务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_paper_task.ps1
Get-ScheduledTask -TaskName 'AI-Trade Paper Daily'
```

日志位于 `logs/scheduled_paper.log`。卸载任务：

```powershell
Unregister-ScheduledTask -TaskName 'AI-Trade Paper Daily' -Confirm:$false
```

计划任务脚本会传递 Python 的非零退出码；数据、状态或网络异常不会被伪装成成功。

## 已知研究边界

- 默认 `adjustment=forward` 使用前复权价格。它适合连续收益研究，但历史价格会随未来分红重述；用它计算历史整手和最低佣金只是近似，并非严格的逐时点成交账本。
- 证券主数据框架支持历史成分区间，但默认 `core_etf` 仍是人工整理的静态 ETF 集合，只解决上市前误入问题，没有消除幸存者偏差和事后选池偏差。
- 海外 ETF 还包含本地交易时段、汇率、溢折价和境外市场休市的影响。
- 当前模拟盘没有现金分红、拆并份额和申赎事件模型，因此必须先长期核对真实券商模拟结果。
- 股票池扩容前仍缺少可靠的历史成分、ST/停复牌、退市、逐时点复权因子与公司行动数据；仅把今天的沪深 300 名单塞进回测会产生错误结论。
- 当前 500 万元流动性阈值和风险模型已经参考过现有历史及滚动窗口结果，因此这些“样本外”窗口也已成为开发数据，不再是完全未触碰的最终检验集。下一阶段独立证据只能来自未来模拟盘。

更严格的生产版本应把不复权成交价、逐时点复权因子、分红拆并和交易日历分别建模。在完成这些工作并选定券商前，实盘适配器保持缺失是有意的安全边界。

## 与 Vibe-Trading 的关系

[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 是本项目的只读 MIT 许可设计参考。本项目借鉴了它的统计验证、组合优化、换手约束、交易日志和 shadow-account 分层思路，但没有导入其 Python 包，也没有复制其 FastAPI、React、多智能体、因子库或券商连接器。

当前实现按本项目的零第三方依赖要求重新编写：风险平价使用坐标下降法，统计验证使用移动区块自助法，并围绕本地 ETF 日线和模拟账户建立。任何复杂算法都必须先在连续样本外和未来模拟盘中证明不劣于简单基线，才能升级为默认值。

除 Vibe-Trading 外，项目还对照了 LEAN、Qlib、NautilusTrader、VeighNa、RQAlpha、vectorbt、OpenBB、PyPortfolioOpt、cvxportfolio、Riskfolio-Lib、FinRL 和 Freqtrade。具体借鉴边界与分层路线见 [系统对照](docs/ECOSYSTEM.md)；本项目采用能力边界和设计思想，不把多个大型框架直接拼接进同一运行时。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ai_trade.cli doctor
.\.venv\Scripts\python.exe -m ai_trade.cli validate
.\.venv\Scripts\python.exe -m ai_trade.cli live-check
```

`live-check` 正常情况下应失败：即使设置风险确认环境变量，系统仍会因为没有券商适配器而拒绝下单。
