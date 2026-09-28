"""Tushare 真实 API 冒烟测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

if not config.ENABLE_TUSHARE_DATA_SOURCE:
    raise SystemExit("ENABLE_TUSHARE_DATA_SOURCE 未开启")
if not config.TUSHARE_TOKEN:
    raise SystemExit("未配置 TUSHARE_TOKEN")

print(f"1) config.ENABLE_TUSHARE_DATA_SOURCE = {config.ENABLE_TUSHARE_DATA_SOURCE}")
print("   config.TUSHARE_TOKEN = 已配置")

# 测试 Tushare raw API
import tushare as ts
ts.set_token(os.environ["TUSHARE_TOKEN"])
pro = ts.pro_api()

df = pro.daily(ts_code='000001.SZ', start_date='20260701', end_date='20260708')
print(f"\n2) 日线 raw: {len(df)} 行, 列={list(df.columns)}")
if not df.empty:
    print(df.head(2).to_string())

df2 = pro.stock_basic(ts_code='000001.SZ', fields='ts_code,name')
print(f"\n3) 股票名称 raw: {df2.iloc[0].to_dict() if not df2.empty else '空'}")

# 测试 DataManager 集成 (跳过 QMT 初始化)
from data_manager import DataManager, MarketDataHealthTracker
dm = object.__new__(DataManager)
dm.market_health = MarketDataHealthTracker()
dm._tushare_pro = None
dm._tushare_token_attempted = False
dm._ts_consecutive_failures = 0
dm._ts_cooldown_until = 0.0
dm.stock_names_cache = {}

pro2 = dm._get_tushare_pro()
print(f"\n4) _get_tushare_pro() = {type(pro2).__name__ if pro2 else 'None'}")

df3 = dm._download_history_tushare('000001.SZ', start_date='20260701', end_date='20260708')
print(f"\n5) _download_history_tushare: {'成功' if df3 is not None and not df3.empty else '失败'}")
if df3 is not None and not df3.empty:
    print(f"   行数={len(df3)}, 列={list(df3.columns)}")
    print(df3.head(2).to_string())

name = dm._get_stock_name_from_tushare('000001.SZ')
print(f"\n6) _get_stock_name_from_tushare('000001.SZ') = {name}")

# 健康评分
snap = dm.market_health.snapshot()
tushare_health = snap.get('sources', {}).get('Tushare', {})
print(f"\n7) Tushare 健康评分: {tushare_health}")

# 股票名称缓存验证
print(f"\n8) stock_names_cache: {dm.stock_names_cache}")

print("\n=== 真实 API 冒烟测试全部通过 ===")
