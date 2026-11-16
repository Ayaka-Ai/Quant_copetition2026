# Quant_copetition2026
量化比赛的策略研究、数据适配与本地模拟回测代码。目前策略、因子、风控和账户模块由本项目实现，尚未调用 Qlib 或接入 AIVIX；Qlib 接入属于后续计划。已保留主线的独立工具，见 [factor-tools](factor-tools/README.md)。

## strategyX 分支快速开始

建议使用 Python 3.12（完整测试已在 Python 3.12 / NumPy 2.3.5 下验证）。从仓库根目录运行：

```bash
git clone --branch strategyX https://github.com/Ayaka-Ai/Quant_copetition2026.git
cd Quant_copetition2026
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-risk.txt
python -m unittest discover -s tests -v
python -m examples.run_portfolio
python -m examples.run_momentum
```

测试和上述两个默认示例使用合成数据，无需 API key、原始行情缓存或联网。完整测试为 172 项；未安装 NumPy 时会跳过 20 项风险相关测试。`factor-tools` 有独立依赖和使用说明。

本分支包含 `quant_data`、`quant_signals`、`quant_portfolio`、`quant_backtest`、配置、测试、示例、文档及 Databento 试跑脚本。原始行情、凭证、`outputs/`、`output/` 和 `tmp/` 不随代码上传。下文历史报告中的收益和产物路径是本地实验记录，并不表示克隆后已有这些数据或产物。

`examples.run_public_momentum`、`examples.run_public_multifactor`、`examples.run_public_dropout` 以及风险估计、接口回放等历史研究入口依赖本地缓存；先按 [公开数据接入与验收](docs/公开数据接入与验收.md) 和 [完整股票池公开数据验收](docs/完整股票池公开数据验收.md) 准备并验收数据。Databento 下载需要用户自己的凭证和相应权限，参见 [试跑说明](experiments/databento_smoke/README.md)。提交和测试本身不发起行情下载或券商交易。

## 下一阶段计划

2026-11-12 已实际重跑 [程序接口演示与过程报告](docs/程序接口运行示例与过程报告_2026-11-12.md)。新增入口 `examples.run_interface_demo`，沿用现有80%风险策略，展示首次20只建仓及下一周4组替换，导出输入、因子分数、风险、拟订单、模拟成交和现金核对。输出在 `outputs/interface_demo_20261112/`，PDF在 `output/pdf/`；172项测试通过，17份核心产物与既有冻结回放一致。仍为本地历史模拟，未接入实时行情或券商。

阶段收口（2026-11-09）：本地研究、历史／合成数据仿真和拟订单账目已贯通；实时模拟行情、持续运行及券商模拟账户适配仍待讨论。本次停止新增策略开发，周五2026-11-13讨论接口。见 [会议报告文字版](docs/周五会议_阶段成果与模拟接口讨论_2026-11-13.md)；可分享PDF位于 `output/pdf/CLEAR-US_阶段成果与模拟接口讨论_2026-11-13.pdf`。本次未修改策略参数或启用外部交易。

2026-11-09 已完成 [80%多头风险诊断与权重优化接口](docs/多头风险诊断与权重优化_2026-11-09.md)，入口 `examples.run_risk_weights`（NumPy运行时）。接入带哈希与时间校验的风险矩阵，输出目标／实际波动、Beta及方差贡献。80%仓位、最多20只、单股4%上限形成唯一解，52次优化均保持4%等权；共同区间两版净收益均7.85%、最大回撤6.62%，逐日净值一致。172项测试通过，没有宣称调权收益或完整指数增强。

2026-11-09 已实现 [风险估计与历史来源调查](docs/风险估计实现与历史来源调查_2026-11-09.md)：生成91期60日Beta及52期252日对角收缩协方差（NumPy），161项测试通过；另核对120项原始Beta和240个协方差元素。估计产物按真实生产时间隔离，严格历史可见版本仍为0；历史基准权重和行业尚未补齐，完整指数增强未启用。运行入口为 `examples.estimate_public_risk`，依赖及本机捆绑运行时命令见说明。

2026-11-09 已完成 [风险约束可行性与数据验收](docs/风险约束可行性与数据验收_2026-11-09.md)，入口 `python3 -m examples.run_risk_preflight`。最多20只×4%无法满足95%或85%目标；91个信号日尚无合格登记的历史基准／行业／Beta／协方差输入，完整优化器可放行记录为0。已明确不自动放宽约束及建仓预算，新增版本契约、PSD和必要条件测试；152项测试通过。原策略和回测不修改。

2026-11-09 已完成实际成交持仓驱动的TopK Drop，并推进执行金额限额及风险诊断基础，见 [阶段实施与进展报告](docs/阶段实施与进展报告_2026-11-09.md)。入口 `python3 -m examples.run_public_dropout`。共同区间累计双边换手由87.29倍降至29.61倍，净收益11.32%变为11.99%；加限额后净收益10.70%、日均仓位66.78%。139项测试通过。完整指数增强仍缺历史基准权重和已验收风险输入，未连接券商；后续计划及未完成事项已在报告中列明。

2026-11-07 已完成第二步 [传统多因子评分与接入](docs/传统多因子评分与接入.md)：新增14个传统因子、行业内标准化及通用 `SignalScore` 接口，入口 `python3 -m examples.run_public_multifactor`。在共同61日完整因子股票池与共同评估期内，多因子净收益11.32%、最大回撤10.19%，动量对照分别为68.72%、17.55%；本组固定多因子尚未证明整体优于动量。127项测试通过，原动量104条周度决定保持一致。下一步接入逐日实际持仓与TopK Drop。

2026-11-07 已完成 [公开动量基线与基准回测](docs/公开动量基线与基准回测.md)：新增独立 `PUBLIC-1.0.1` 执行配置及公开研究适配器，用本地全池缓存运行20日动量、80% SPY和合格池目标80%等权，并输出同口径账目、指标与研究假设。入口为 `python3 -m examples.run_public_momentum`，无需联网。等权组合受10万美元整数股配置影响，实际平均仓位仅38.14%，报告明确标注其比较限制。原严格V1、冻结公开配置和来源缓存保持原样；后续可新增多因子评分与实际持仓换手缓冲。

公开数据探索版已于 2026-11-04 固定为 `PUBLIC-1.0.0`：见 [规则差异与记账约定](docs/公开数据探索版交易规则.md)及 [独立配置](configs/momentum_public_v1.json)。普通分红、拆股和碎股记账已实现，见 [公司行动核查与记账](docs/公司行动核查与记账.md)；公开选股适配及复杂权益仍待接入或处理。历史回测和券商下单均未启用，原 V1 规则和验收状态保持原样。

同日已完成第二步 [完整股票池公开数据获取与检查](docs/完整股票池公开数据验收.md)：504 个代码全部请求，整理 250,423 条日线、3,186 条分红及 29 条来源 `splits` 记录。498 个代码覆盖完整，5 个历史不足，KVUE 日线异常；原股票池未删改。29 条记录进一步归类为 18 条真实股数拆股与 11 条复杂分配，普通事件账户已在五组真实缓存固定目标示例中对账。

[第一版动量策略交易规则](docs/第一版动量策略交易规则.md)已于 2026-11-03 固定为 V1.0.0，对应参数见 [momentum_v1.json](configs/momentum_v1.json)。2026-11-04 已完成组合账户、20 日动量、周度目标、公开数据及普通公司行动账本；冻结 V1 的正式数据验收尚未通过，公开版完整策略及基准比较待接入。

[基础量化策略实施计划](docs/基础量化策略实施计划.md)：以 20 日动量选股和等权配置为第一版研究基线，按五个工作日安排组合账户、数据验收、基准和验证实验。

## 公司行动与账户核对

```bash
python3 -m examples.review_corporate_actions
```

离线核对 3,215 条来源事件，输出价格还原、基准状态以及 AMCR、ANET、APH、TSCO、SPY 五组固定目标账户的公司行动账目，位于 `outputs/corporate_actions_20261104/`。内部金额检查不等于逐笔独立来源核验；复杂行动仍显式阻塞，不输出完整动量策略收益。具体证据与接口见 [核查与记账说明](docs/公司行动核查与记账.md)。

## 公开数据接入与验收

完整股票池入口（原三只试样入口继续保留）：

```bash
python3 -m examples.prepare_public_universe --fetch
python3 -m examples.prepare_public_universe
```

已有响应不会重新下载；结果在 `outputs/public_universe_20261104/`，第二条只作离线检查。详细运行、缺口和公司行动注意事项见 [全股票池验收](docs/完整股票池公开数据验收.md)。

```bash
python3 -m examples.prepare_public_data
python3 -m examples.prepare_public_data --fetch-public-pilot
```

默认只检查本地缓存；第二条在缺少固定试样缓存时获取 AAPL、MSFT、SPY 的两年公开日线，无需凭证或付费接口。已取得共 1,503 条日线及 24 条分红记录。输出位于 `outputs/data_acceptance/`；精确成交额、历史可用时间等仍缺失，明确输出 `strategy_ready: false`，不运行正式回测。输入、来源、缺口及下一步见 [公开数据接入与验收](docs/公开数据接入与验收.md)。

## 动量选股与周度目标

```bash
python3 -m examples.run_momentum
python3 -m examples.run_momentum --check-local-b
```

默认使用 40 只虚构股票验证动量、行业与发行人约束，并将每周目标接入账户；结果位于 `outputs/momentum_synthetic/`。第二条检查本地 B 组数据，当前明确输出数据未准备好，不生成清仓目标。接口、输入字段和结果说明见 [动量选股与周度目标](docs/动量选股与周度目标.md)。

## 外部目标权重组合账户

`simulate_portfolio` 接收完整目标权重，使用共享现金，先卖后买，并输出订单、成交、持仓、调仓原因和每日净值。默认使用显式常规交易日程的次日开盘代理；旧 UTC 数据只能显式选择收盘代理模式。

```bash
python3 -m examples.run_portfolio
python3 -m examples.run_portfolio --sample-b
```

第一条使用虚构价格验证账目，第二条读取本地 B 组数据作 UTC 收盘代理联调。结果分别位于 `outputs/portfolio_synthetic/` 和 `outputs/portfolio_b/`，均为账户示例；完整接口、使用边界和验证见 [组合账户接口](docs/组合账户接口.md)。

## 本地统一数据接口

已提供 `quant_data.load_daily_bars`，从现有 Databento 缓存返回统一的日线记录和数据口径。Python 3.8+，无需额外依赖。

```bash
python3 -m examples.inspect_daily
python3 -m unittest discover -s tests -v
```

字段、时间边界和团队使用约定见 [统一数据接口](docs/统一数据接口.md)。原始行情需在本地准备；当前只支持项目脚本下载的 EQUS.MINI 未复权 UTC 日柱。

## 双均线信号演示

```bash
python3 -m examples.run_signals
```

离线读取 B 组行情，输出 5／20 日均线和做多/空仓目标到 `outputs/ma_signals_b/`。结果仅是信号，不包含交易收益。规则、预热及时间含义见 [双均线信号模块](docs/双均线信号模块.md)。

## 模拟账户演示

```bash
python3 -m examples.run_account
```

用已有 B 组数据模拟 10 万美元、三个独立等额初始子账户的交易，结果写入 `outputs/account_b/`。费用、下一日柱成交代理、账目及局限见 [模拟账户说明](docs/模拟账户说明.md)。不连接券商、不调用行情 API。
