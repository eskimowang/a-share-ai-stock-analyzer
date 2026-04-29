"""Phase 1 demo: 拉取一只股票日线，验证多源协作。

用法: python fetch_demo.py 300244
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.data_sources import UnifiedDataSource

code = sys.argv[1] if len(sys.argv) > 1 else "300244"

# 读 config 里的 tushare token（暂时为空）
import yaml
config_path = "/opt/stock-analyzer/config/config.yaml"
if os.path.exists(config_path):
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    token = cfg.get("data_sources", {}).get("tushare", {}).get("token") or ""
else:
    token = ""

ds = UnifiedDataSource(tushare_token=token if token else None)

print(f"=== 拉取 {code} 最近 60 天日线 ===")
from datetime import datetime, timedelta
end = datetime.now().strftime("%Y%m%d")
start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

df, source = ds.get_daily(code, start=start, end=end)
if df is None or df.empty:
    print("ERROR: 所有数据源都失败")
    sys.exit(1)

print(f"数据源: {source}")
print(f"行数: {len(df)}")
print(f"日期范围: {df['trade_date'].min()} → {df['trade_date'].max()}")
print(f"\n最近 5 天:")
print(df.tail(5).to_string(index=False))

print(f"\n=== 实时行情 ===")
rt = ds.get_realtime(code)
if rt:
    print(f"名称:  {rt['name']}")
    print(f"现价:  {rt['price']}")
    print(f"涨跌:  {rt['change_pct']}%")
    print(f"PE-TTM: {rt.get('pe_ttm')}")
    print(f"PB:    {rt.get('pb')}")
else:
    print("实时行情未能拉取（非交易时段可能为空）")
