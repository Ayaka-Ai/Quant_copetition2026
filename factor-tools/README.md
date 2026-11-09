# 因子工具

## 运行

```sh
python -m pip install -r requirements.txt
python src/export_factors.py --demo
python src/export_factors.py --input prices.csv --price-basis "调整后的常规交易时段收盘价" --output-dir data/output
python -m unittest discover -s src -p test_export_factors.py
```

Parquet 输入需额外安装 `pyarrow`。输出为 `factor_scores.csv` 和 `run_info.json`；后者记录价格口径、文件摘要与依赖版本。示例另生成 `demo_prices.csv`。

## 输入与输出

输入字段：`date, symbol, close`。同一日期、股票不得重复；日期不带时区或时刻；价格须为有限正数。额外字段忽略。程序不做复权或交易日历核验，输入前需确认价格口径与完整交易日期。单只股票缺失会传播空值；所有股票共同缺失的日期无法自动识别。

输出字段：`date, symbol, custom_momentum20, custom_lowvol20, custom_reversal3, ready_all`。预热期与缺失分数保留空值；`ready_all` 表示三个因子均可用。计算使用当日收盘数据，交易时点须晚于数据可得时点。

| 字段 | 定义 |
|---|---|
| custom_momentum20 | close / close.shift(20) − 1 |
| custom_lowvol20 | −std(close.pct_change(), 20)，ddof=1 |
| custom_reversal3 | 1 − close / close.shift(3) |

以上为自定义定义，不等同于 Qlib Alpha158 的同名特征。工具不包含截面标准化、未来收益标签、组合构建或行情下载；预测期限与执行口径由调用方确定。
