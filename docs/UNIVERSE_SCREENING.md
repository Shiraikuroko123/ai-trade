# 证券池批量筛选

数据页的“证券池筛选”是一个只读的横向研究视图（当前契约 `screen.schema_version = 2`）。它把证券主数据、已完成日线和缓存清单绑定到同一份快照，返回每个候选证券的可复核指标：

- 20 日平均成交额（流动性参考）
- 配置动量窗口的历史收益
- 配置波动窗口的年化波动率
- 当前收盘相对长期均线的趋势状态
- 历史长度、覆盖百分比、数据日期、滞后天数和来源提供方/路由

空值表示历史不足、快照滞后或数据源不可用，不会用零替代。筛选结果只是候选集合，不会修改策略参数、模拟账户、风控门禁或券商权限。

## 页面操作

在工作台进入“数据”，选择截面日期后，可以组合以下条件：

- 资产类别、板块/分组
- 上行、下行、混合或排除下行趋势
- 历史长度达标或当日完整
- 最低 20 日平均成交额、最高年化波动率
- 当日有效、排序指标、升降序和最多返回条数（1–500）

页面显示返回数、匹配数、排除数、来源分布、完整率、历史达标率、覆盖中位数和最大日期滞后。若使用腾讯网络或已验证本地回退，页面会显示数据边界警告；这不是错误，但需要在研究记录中保留。宽表可以横向滚动；筛选条件、条件指纹、主数据指纹、完成截止日和快照 ID 会保留在“筛选证据”区域。页面读取时间来自顶层 `generated_at`，与行情截面日期分开显示。

## 接口契约

只读接口：

```text
GET /api/universe/screen
```

支持的查询参数：`date`、`asset_class`、`sector`、`trend`、`coverage`、`min_average_amount`、`max_annual_volatility`、`active_only`、`sort`、`direction`、`limit`。服务端会限制参数长度、重复字段、数值范围和排序字段；未知参数直接返回 400。

响应中的 `screen.filter_fingerprint` 只由规范化后的筛选参数派生，同一组条件会得到相同指纹；排序方向、数量限制或任一筛选值变化时，指纹也会变化。`screen.snapshot_id` 由截面日期、证券主数据指纹、manifest 指纹和各证券文件 SHA-256 派生。重新下载或更换主数据后，快照 ID 会变化，旧筛选结果不能被当作当前证据。

`screen.metric_definitions` 给出动量、波动、成交额、趋势和覆盖的公式/窗口；`screen.data_quality` 统计所有候选记录（不是只统计 `instruments` 返回行）的状态计数、完整率、历史达标率、覆盖分位和滞后分位；`screen.source_summary` 与 `screen.returned_source_summary` 分别统计候选全集和当前返回集的来源。`screen.completed_session_cutoff`、`screen.latest_common_session` 用于区分页面读取时间、请求截面日和行情完成日。缺少来源、行情或历史时，接口保留空值并在 `screen.warnings` / `screen.empty_reason` 中说明恢复动作。

示例（字段节选）：

```json
{
  "screen": {
    "schema_version": 2,
    "data_quality": {
      "complete_percent": 100.0,
      "history_ready_percent": 100.0,
      "coverage_percent": {"median": 100.0},
      "lag_days": {"maximum": 0.0}
    },
    "source_summary": {
      "providers": [{"provider": "eastmoney", "count": 8, "fallback": false}]
    }
  }
}
```

## 数据边界

筛选指标来自复权日线缓存，不代表盘中行情、交易所认证报价或未来收益。基本面、新闻和情绪指标尚未纳入该接口；在这些数据接入前，页面会保持明确的覆盖边界。
