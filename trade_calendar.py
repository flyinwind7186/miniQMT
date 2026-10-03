"""
miniQMT 交易日历 —— 全仓库唯一的交易日口径来源。

背景: 仓库多处按"周一至周五"判断交易日(config.is_market_hours / settlement_db /
premarket_sync / data_manager / utils.get_trading_days)，A 股法定节假日会整段误判。
2026-10-01 国庆长假首日实测：自动买入服务把它当成交易日，全天每 30 分钟空跑一轮
完整筛选。长假连休 5~9 天，按周内日判断没有任何一处能躲过去。

数据源: Tushare trade_cal(SSE)。本仓库已有 TUSHARE_TOKEN 与 tushare 依赖，
不引入新的外部依赖。

口径:
  - 启动时刷新一次并落本地 SQLite 缓存(默认 data/trade_calendar.db)。
    进程可能连续运行数周，长假前必须拿到新日历，不能指望运行期自愈。
  - 运行期只读本地缓存，不联网。
  - 取数失败或日期落在缓存覆盖范围之外时，退化为"周一至周五"判断并返回
    confident=False，由调用方决定是否告警 —— 日历不可用时宁可多跑一轮完整逻辑，
    也不能因为拿不到日历就漏掉真实交易日。
  - 取数失败**不写降级数据**，不会用近似值覆盖已有的权威日历。

本模块是全仓库唯一的交易日判定实现，其他模块一律调用这里的函数，不再自行判断。
调用方若只需布尔值可用 is_trading_day()；需要区分"权威判定"与"退化猜测"时用
is_trading_day_confident()。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta

from logger import get_logger

logger = get_logger('trade_calendar')

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE_PATH = os.path.join(PROJECT_ROOT, "data", "trade_calendar.db")
CACHE_PATH_ENV = "MINIQMT_TRADE_CALENDAR_DB"

# 刷新范围: 回看一个月(覆盖跨月复盘) + 前推一年(覆盖下一年的春节/国庆排期)
_LOOKBACK_DAYS = 30
_LOOKAHEAD_DAYS = 370

# 兜底搜索上限(天): 覆盖任何连休，同时避免日历不可用时死循环
_MAX_SEARCH_DAYS = 400

_SOURCE_TUSHARE = "tushare"
_SOURCE_WEEKDAY = "weekday_fallback"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_calendar (
    cal_date   TEXT PRIMARY KEY,
    is_open    INTEGER NOT NULL,
    source     TEXT,
    updated_at TEXT
)
"""

# 进程内缓存: (缓存路径, 'YYYY-MM-DD') -> (is_trading_day, confident)。
# is_trade_time() 等函数在监控/策略线程里被高频调用，逐次打开 SQLite 在 Windows 上
# (文件锁 + 杀软扫描)可达毫秒级。同一交易日的日历不会在盘中变化，缓存到进程退出；
# refresh()/upsert_calendar() 会清空缓存。CPython 下 dict 读写原子，无需加锁。
_day_cache = {}


def cache_path(override: str = None) -> str:
    """日历缓存路径。显式参数 > 环境变量 > 默认 data/ 目录。"""
    if override:
        return override
    env = os.environ.get(CACHE_PATH_ENV, "").strip()
    return env or DEFAULT_CACHE_PATH


def clear_cache() -> None:
    """清空进程内日历缓存(刷新后调用)。"""
    _day_cache.clear()


def _connect(db_path: str = None) -> sqlite3.Connection:
    path = cache_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _coerce_date(value) -> date:
    """接受 date / datetime / 'YYYY-MM-DD' / 'YYYYMMDD'，统一成 date。"""
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法解析的日期: {value!r}")


def _fmt(value) -> str:
    return _coerce_date(value).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def is_trading_day_confident(value, db_path: str = None):
    """判断某日是否交易日。返回 (is_trading_day, confident)。

    confident=False 表示缓存里没有这一天的权威数据(覆盖范围之外，或从未刷新成功)，
    此时返回值是"周一至周五"的近似值，调用方应告警而不应静默采信。
    """
    day = _coerce_date(value)
    key = (cache_path(db_path), day.strftime("%Y-%m-%d"))
    cached = _day_cache.get(key)
    if cached is not None:
        return cached

    result = None
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT is_open, source FROM trade_calendar WHERE cal_date = ?",
                (key[1],),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            # 只有权威来源才算 confident；历史遗留的 weekday_fallback 行按近似值处理，
            # 否则"周一至周五"会被当成权威数据，节假日误判再也没人告警。
            result = (bool(row["is_open"]), row["source"] == _SOURCE_TUSHARE)
    except Exception as e:
        # 读取失败不能拖垮调用方: 退化为周一至周五并标记不可信
        logger.warning(f"读取交易日历失败({key[1]})，退化为周一至周五: {e}")

    if result is None:
        result = (day.weekday() < 5, False)
    _day_cache[key] = result
    return result


def is_trading_day(value, db_path: str = None) -> bool:
    """某日是否交易日(只取布尔值，不关心是否权威)。"""
    return is_trading_day_confident(value, db_path)[0]


def previous_trading_day(value=None, db_path: str = None) -> date:
    """value 之前最近的一个交易日(严格小于 value)。"""
    day = _coerce_date(value) - timedelta(days=1)
    for _ in range(_MAX_SEARCH_DAYS):
        if is_trading_day(day, db_path):
            return day
        day -= timedelta(days=1)
    raise RuntimeError(f"{_MAX_SEARCH_DAYS} 天内找不到交易日(日历可能异常): {value!r}")


def next_trading_day(value=None, db_path: str = None) -> date:
    """value 当天或之后最近的一个交易日(大于等于 value)。"""
    day = _coerce_date(value)
    for _ in range(_MAX_SEARCH_DAYS):
        if is_trading_day(day, db_path):
            return day
        day += timedelta(days=1)
    raise RuntimeError(f"{_MAX_SEARCH_DAYS} 天内找不到交易日(日历可能异常): {value!r}")


def recent_trading_dates(n: int, reference_date=None, db_path: str = None) -> list:
    """返回 reference_date 之前最近 N 个交易日，按近到远排序。"""
    day = _coerce_date(reference_date)
    result = []
    for _ in range(_MAX_SEARCH_DAYS):
        if len(result) >= n:
            break
        day -= timedelta(days=1)
        if is_trading_day(day, db_path):
            result.append(day.strftime("%Y-%m-%d"))
    return result


def trading_days_between(start, end, db_path: str = None) -> list:
    """返回 [start, end] 闭区间内的交易日列表，按时间升序。"""
    day = _coerce_date(start)
    last = _coerce_date(end)
    result = []
    while day <= last:
        if is_trading_day(day, db_path):
            result.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# 刷新
# ---------------------------------------------------------------------------
def upsert_calendar(rows, source: str, db_path: str = None) -> int:
    """写入日历。rows 为 (cal_date, is_open) 序列。"""
    values = [
        (_fmt(cal_date), 1 if is_open_ else 0, source,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        for cal_date, is_open_ in rows
    ]
    if not values:
        return 0
    conn = _connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO trade_calendar (cal_date, is_open, source, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(cal_date) DO UPDATE SET "
            "is_open=excluded.is_open, source=excluded.source, updated_at=excluded.updated_at",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    clear_cache()
    return len(values)


def _fetch_tushare_rows(start: date, end: date) -> list:
    """从 Tushare trade_cal 拉取 SSE 日历。失败抛异常，由 refresh 兜底。"""
    import config
    import tushare as ts

    token = (getattr(config, "TUSHARE_TOKEN", "") or
             os.environ.get("TUSHARE_TOKEN", "")).strip()
    if not token:
        raise RuntimeError("未配置 TUSHARE_TOKEN(.env 或环境变量)")

    df = ts.pro_api(token).trade_cal(
        exchange="SSE",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    if df is None or len(df) == 0:
        raise RuntimeError("trade_cal 返回空数据")
    return [(row.cal_date, int(row.is_open)) for row in df.itertuples(index=False)]


def refresh(today=None, db_path: str = None, fetcher=None) -> dict:
    """刷新本地交易日历缓存，返回 {source, degraded, count, start, end, updated_at}。

    取数失败时**不写任何降级数据**(绝不用"周一至周五"的近似值覆盖已有的权威日历)，
    直接返回 degraded=True；覆盖范围外的日期由 is_trading_day_confident 逐日返回
    confident=False，调用方据此告警。流程不中断 —— 日历不可用不应让程序起不来。
    """
    ref = _coerce_date(today)
    start = ref - timedelta(days=_LOOKBACK_DAYS)
    end = ref + timedelta(days=_LOOKAHEAD_DAYS)

    fetch = fetcher or _fetch_tushare_rows
    try:
        rows = fetch(start, end)
    except Exception as e:
        logger.warning(f"交易日历取数失败，本地缓存保持不变，本次运行退化为周一至周五口径: {e}")
        rows, source = [], _SOURCE_WEEKDAY
    else:
        source = _SOURCE_TUSHARE

    count = upsert_calendar(rows, source=source, db_path=db_path)
    return {
        "source": source,
        "degraded": source != _SOURCE_TUSHARE,
        "count": count,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def refresh_at_startup(db_path: str = None) -> dict:
    """启动时刷新并记录结果。返回 refresh() 的字典。

    进程可能连续运行数周，长假(国庆/春节)前必须拿到新日历，故启动时无条件拉一次。
    """
    result = refresh(db_path=db_path)
    if result["degraded"]:
        logger.warning(
            "交易日历刷新失败，已退化为周一至周五口径(法定节假日会被误判为交易日): "
            f"覆盖 {result['start']} ~ {result['end']} 共 {result['count']} 天"
        )
    else:
        logger.info(
            f"交易日历已刷新: source={result['source']} "
            f"覆盖 {result['start']} ~ {result['end']} 共 {result['count']} 天"
        )
    return result
