# Databento 元数据与费用查询

## B 组三个月下载及验收

从仓库根目录运行 `python3 experiments/databento_smoke/download_b.py`，在隐藏提示中输入 API key。范围固定为 EQUS.MINI / AAPL、MSFT、SPY / ohlcv-1d，UTC 2026-07-01 至 2026-10-01（右端不含）。先重新估价，超过原报价 0.00030040741 美元就停止。估价不等于实际账单。

下载保存至已忽略的 `data/b/`，验收报告为 `reports/b_validation.json`。检查字段、映射、午夜 UTC 时间戳、重复、OHLC、成交量和 64 个预期交易日（每只股票，共 192 条），并与 A 组重叠记录核对。固定样本日历排除 7 月 3 日、9 月 7 日及周末，依据 [Nasdaq 日历](https://nasdaqtrader.com/Trader.aspx?id=Calendar)。非交易日 UTC 柱保留并提示检查，不直接删除。缺失、异常或 A/B 差异会令验收停止并留下报告。完整缓存通过参数和 SHA256 核验后只做本地验收；未完成的下载不会自动重试。

2026-10-28 状态：用户已完成真实下载，随后离线验收通过。共 192 条，AAPL、MSFT、SPY 各 64 条，日期覆盖 2026-07-01 至 09-30；无缺失、重复或字段异常，A/B 重叠的 15 条 OHLCV 全部一致。原始文件 20,375 字节，SHA256 与下载记录一致。本次验收未调用 API、未上传 GitHub。详见 reports/b_validation.json 和 [B 组验收说明](reports/B组验收说明.md)。该日线仍是未复权 UTC 日柱，不能直接视作常规时段开收盘价。

## 日线口径核对（2026-10-28）

已完成官方定义核查及三条外部价格抽查，详见 [A 组日线口径核对](reports/A组日线口径核对.md)。结论：现有 UTC 日柱不能直接作为常规时段日线或次日 09:30 成交价；本地流程未复权，外部抽查存在价格差异，尚未完成同口径对账。结构通过不等于正式回测可用。本次没有新增行情下载，原始数据保持不变。

## A 组下载及验收（2026-10-28 新增）

用户已指定下载 EQUS.MINI A 组。运行：

```bash
python3 experiments/databento_smoke/download_a.py
```

从仓库根目录执行，按隐藏提示输入 key。脚本先重新估价，超过原 A 组报价（仅允许末位舍入误差）即停止，然后解析代码映射并下载指定三个标的的五天日线。估价不是最终账单保证，实际扣费及 credits 抵扣仍需以 Billing 为准。

数据保存在被 Git 忽略的 `data/a/`，包括原始 CSV、映射、请求记录、SHA256 和规范化 CSV。结构验收结果在 `reports/a_validation.json`。重复运行有完整缓存时仅做本地验收；发现已开始但未完整完成的下载会停止，避免自动重复计费。

验收检查字段、映射、每只标的五个 UTC 日柱日期、重复、价格正值、OHLC 关系和成交量。该日期范围没有美股周中休市日；此专用脚本不作为通用交易日历实现。仍需另行验证常规交易时段口径、公司行动及外部行情一致性，结构通过不代表可直接用于正式回测。

当前执行状态（2026-10-28 更新）：用户已在本地完成 A 组下载，HTTP 200，原始 CSV 为 1,651 字节。修复了 Python 3.8 无法直接解析九位小数时间戳的问题；解析副本截断到微秒、不四舍五入，原始纳秒字符串保持不变。五项时间戳回归测试通过。

已用本地缓存完成实际数据结构验收：15 行，AAPL、MSFT、SPY 各 5 行，无重复、缺失或 OHLC 结构错误。本次修复和重新验收未调用 API、未重复下载。下载估价为 0.000023469329 美元，实际账单和免费额度抵扣仍未验证。所有更改保留本地，未上传 GitHub。

此阶段只查询目录、格式、历史范围和估价，不下载行情，不提交批量下载任务，不开通订阅。仅依赖 Python 3.8+ 和系统 curl。

## 运行

在终端中执行：

```bash
cd /path/to/Quant_copetition2026
python3 experiments/databento_smoke/probe.py
```

出现 `Databento API key (hidden):` 时粘贴 key 并回车，输入不会显示。也支持已由本地安全方式设置的 `DATABENTO_API_KEY` 环境变量。不要把真实 key 写入源代码或命令示例。

脚本将结果写入 `reports/metadata_probe.json`；再次运行会覆盖这个文件。无自动重试，优先使用已保存的报告。输出仅含查询参数、HTTP 状态和脱敏响应，不含认证头。脚本在认证失败后不会尝试行情下载。

## 查询范围

先 `list_datasets`；再对目录中存在的 `EQUS.MINI`、`XNAS.ITCH`、`DBEQ.BASIC` 查询 `list_schemas` 和 `get_dataset_range`，并按支持的格式估价：

| 情景 | 标的 | 格式 | UTC 起止，右端不包含 |
|---|---|---|---|
| A | AAPL、MSFT、SPY | 日线 | 2026-09-14 至 2026-09-19 |
| B | 同上 | 日线 | 2026-07-01 至 2026-10-01 |
| C | 同上 | 分钟线 | 2026-09-14 至 2026-09-15 |

候选 dataset 不保证有比赛授权，覆盖场所也不相同。超出历史范围时保留 API 错误，不自动扩大请求或切换日期。

## 如何解释结果

- HTTP 200 的目录或估价：证明对应请求成功，不证明已获得行情下载许可。
- 估价为正：这是请求预计消耗金额，不一定等于最终自付金额；必须另外确认免费 credits 或比赛授权。
- 估价为零：仍需确认许可、其他收费及账号状态，不能把估价当成扣款上限。
- 401：认证失败；403：权限/访问受限；429：限流。保留具体脱敏响应，由账号负责人核对，不自行推断为余额不足。
- 账号免费余额、有效期和其他队员共享消耗，需要在 Databento 账户 Billing 页面确认。本脚本没有余额或许可检查接口，也没有下载开关。

源码依据：[官方 metadata 实现](https://github.com/databento/databento-python/blob/main/databento/historical/api/metadata.py)、[官方认证实现](https://github.com/databento/databento-python/blob/main/databento/common/http.py)。费用依据：[估价文档](https://databento.com/docs/api-historical/metadata/metadata-get-cost?lang=python)、[计费与 credits](https://databento.com/docs/faqs/usage-pricing-and-data-credits)。
