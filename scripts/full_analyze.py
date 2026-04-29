"""Phase 1 里程碑测试: 单股全流程双模型分析。
用法: python3.11 scripts/full_analyze.py 300244
"""
import sys, os, yaml, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data_sources import UnifiedDataSource
from app.data_sources.tushare_client import TushareClient
from app.ai.multi_brain import MultiBrain, build_brains_from_config

code = sys.argv[1] if len(sys.argv) > 1 else "300244"

with open("/opt/stock-analyzer/config/config.yaml") as f:
    cfg = yaml.safe_load(f)

print(f"{'='*60}")
print(f"股票分析: {code}")
print(f"{'='*60}")

# ---------- 1. 数据准备 ----------
print("\n[1/4] 拉数据…")
ds = UnifiedDataSource(tushare_token=cfg["data_sources"]["tushare"]["token"])
ts = ds.tushare

basics = ts.get_basics(code)
daily_df, _ = ds.get_daily(code, start="20260101")
daily_basic = ts.get_daily_basic(code)
fi = ts.get_fina_indicator(code)
income = ts.get_income(code)
reports_df = ts.get_reports(code, limit=8)

latest_row = daily_basic.iloc[0] if not daily_basic.empty else {}
fi_row = fi.iloc[0] if not fi.empty else {}
inc_row = income.iloc[0] if not income.empty else {}

stock_data = {
    "code": code,
    "name": basics.get("name", ""),
    "industry": basics.get("industry", ""),
    "latest_date": latest_row.get("trade_date", ""),
    "close": latest_row.get("close"),
    "change_pct": daily_df.iloc[-1]["change_pct"] if not daily_df.empty else None,
    "pe_ttm": latest_row.get("pe_ttm"),
    "pb": latest_row.get("pb"),
    "total_mv": latest_row.get("total_mv"),
    "report_period": fi_row.get("end_date", ""),
    "revenue": inc_row.get("revenue"),
    "net_profit": inc_row.get("n_income"),
    "roe": fi_row.get("roe"),
    "gross_margin": fi_row.get("grossprofit_margin"),
    "net_margin": fi_row.get("netprofit_margin"),
    "debt_ratio": fi_row.get("debt_to_assets"),
    "daily": daily_df.to_dict(orient="records") if not daily_df.empty else [],
    "reports": reports_df.to_dict(orient="records") if not reports_df.empty else [],
    "position": None,
}
print(f"  - {stock_data['name']} | PE {stock_data['pe_ttm']} | PB {stock_data['pb']}")
print(f"  - 日线 {len(stock_data['daily'])} 行, 研报 {len(stock_data['reports'])} 份")

# ---------- 2. 构造 AI 客户端 ----------
print("\n[2/4] 初始化 AI 模型…")
brains = build_brains_from_config(cfg)
for b in brains:
    print(f"  - {b.name} ({b.model})")
mb = MultiBrain(brains)

# ---------- 3. 并行分析 ----------
print("\n[3/4] 两家 AI 并行分析…")
t0 = time.time()
opinions = mb.analyze(stock_data, max_tokens=2000)
print(f"  两家并行耗时: {time.time()-t0:.1f}s")

for name, text in opinions.items():
    print(f"\n{'─'*60}")
    print(f"【{name}】")
    print(f"{'─'*60}")
    print(text)

# ---------- 4. 综合仲裁 ----------
print(f"\n{'='*60}")
print("[4/4] DeepSeek 作为仲裁模型综合两家观点…")
print(f"{'='*60}")
consensus = mb.consensus(stock_data, opinions, arbiter=brains[0])
print(consensus)

print(f"\n{'='*60}")
print(f"总耗时: {time.time()-t0:.1f}s")
print(f"{'='*60}")
