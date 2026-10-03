"""
miniqmt_autobuy 交易日历。

背景: 仓库原有交易日口径只按"周一至周五"判断(config.is_market_hours 与
settlement_db.is_trading_day 的注释均承认忽略了节假日)。2026-10-01 国庆长假首日
被当成交易日，服务全天每 30 分钟触发一轮完整筛选(见 logs/miniqmt_autobuy.log)。
长假期间 A 股连续休市 5~9 天，按周内日判断会整段误判。

数据源: Tushare trade_cal(SSE)。本仓库已有 TUSHARE_TOKEN 与 tushare 依赖，
不引入新的外部依赖。

口径:
  - 启动时强制刷新一次并落本地 SQLite 缓存(默认 data/autobuy_trade_calendar.db)。
    进程可能连续运行数周，长假前必须拿到新日历，不能指望运行期自愈。
  - 运行期只读本地缓存，不联网。
  - 取数失败或日期落在缓存覆盖范围之外时，退化为"周一至周五"判断并返回
    confident=False，由调用方决定是否告警 —— 日历不可用时宁可多跑一轮完整筛选，
    也不能因为拿不到日历就少买/不买。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta

from .config import PROJECT_ROOT, get_autobuy_logger

logger = get_autobuy_logger("autobuy.calendar")

DEFAULT_CACHE_PATH = os.path.join(PROJECT_ROOT, "data", "autobuy_trade_calendar.db")
CACHE_PATH_ENV = "MINIQMT_AUTOBUY_CALENDAR_DB"

# 刷新范围: 回看一个月(覆盖跨月复盘) + 前推一年(覆盖下一年的春节/国庆排期)
_LOOKBACK_DAYS = 30
_LOOKAHEAD_DAYS = 370

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


def cache_path(override: str = None) -> str:
    """日历缓存路径。显式参数 > 环境变量 > 默认 data/ 目录。"""
    if override:
        return override
    env = os.environ.get(CACHE_PATH_ENV, "").strip()
    return env or DEFAULT_CACHE_PATH


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
# 读
# ---------------------------------------------------------------------------
def is_open(value, db_path: str = None):
    """判断某日是否开市。返回 (is_open, confident)。

    confident=False 表示缓存里没有这一天的权威数据(覆盖范围之外，或刷新失败退化为
    周一至周五)，此时 is_open 是"周一至周五"的近似值，调用方应告警而不应静默采信。
    """
    day = _coerce_date(value)
    key = day.strftime("%Y-%m-%d")
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT is_open, source FROM trade_calendar WHERE cal_date = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            # 只有权威来源才算 confident；历史遗留的 weekday_fallback 行按近似值处理，
            # 否则"周一至周五"会被当成权威数据，节假日误判再也没人告警。
            return bool(row["is_open"]), row["source"] == _SOURCE_TUSHARE
    except Exception as e:
        # 读取失败不能拖垮调用方: 退化为周一至周五并标记不可信
        logger.warning(f"读取交易日历失败({key})，退化为周一至周五: {e}")
    return day.weekday() < 5, False


def recent_trading_dates(n: int, reference_date=None, db_path: str = None) -> list:
    """返回 reference_date 之前最近 N 个交易日，按近到远排序。

    与 pool.recent_trading_dates 的旧口径(纯周一至周五)相比，本函数在日历覆盖范围
    内会正确跳过法定节假日；覆盖范围外自动退化为周一至周五，行为与旧口径一致。
    """
    ref = _coerce_date(reference_date)
    result = []
    day = ref - timedelta(days=1)
    # 上限保护: 极端长假 + 日历不可用时避免死循环(一年足够覆盖任何连休)
    for _ in range(400):
        if len(result) >= n:
            break
        if is_open(day, db_path)[0]:
            result.append(day.strftime("%Y-%m-%d"))
        day -= timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# 写
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
    return len(values)


def _fetch_tushare_rows(start: date, end: date) -> list:
    """从 Tushare trade_cal 拉取 SSE 日历。失败抛异常，由 refresh 兜底。"""
    import config as root_config
    import tushare as ts

    token = (getattr(root_config, "TUSHARE_TOKEN", "") or
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
    """刷新本地交易日历缓存，返回 {source, count, start, end, updated_at}。

    取数失败时**不写任何降级数据**(绝不用"周一至周五"的近似值覆盖已有的权威日历)，
    直接返回 degraded=True；覆盖范围外的日期由 is_open 逐日返回 confident=False，
    调用方据此告警。流程不中断 —— 日历不可用不应让买入服务起不来。
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
