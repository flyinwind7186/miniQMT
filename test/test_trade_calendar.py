# -*- coding: utf-8 -*-
"""统一交易日历及其在主程序各调用点的回归测试。

背景: 仓库原先分散着多处"只看周内日、不看节假日"的交易日口径 ——
config.is_trade_time / settlement_db.is_trading_day / premarket_sync /
data_manager._get_completed_history_end_date / utils.get_trading_days。
2026-10-01 国庆长假首日实测被当成交易日。这里锁定两件事:

  1. 交易日判断只有 trade_calendar 一个来源；
  2. 每个原调用点都真的走了它（节假日不再被误判）。

日期基准取 2026 年 9~10 月: 09-25 中秋、10-01~10-07 国庆休市，10-08/10-09 开市，
10-10/10-11 周末。与 Tushare trade_cal 实测一致。
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import trade_calendar
import utils
from data_manager import DataManager
from premarket_sync import PreMarketSyncScheduler
from settlement_db import should_run_close_snapshot

# 2026-09-25 中秋、2026-10-01~10-07 国庆、10-10~10-11 周末
_HOLIDAYS = {
    "20260925", "20260926", "20260927",
    "20261001", "20261002", "20261003", "20261004",
    "20261005", "20261006", "20261007", "20261010", "20261011",
}


def _seed_rows(start=date(2026, 9, 1), end=date(2026, 12, 31)):
    """构造权威日历行: 工作日开市，_HOLIDAYS 与周末休市。"""
    rows = []
    day = start
    while day <= end:
        key = day.strftime("%Y%m%d")
        rows.append((key, 0 if (key in _HOLIDAYS or day.weekday() >= 5) else 1))
        day += timedelta(days=1)
    return rows


def _new_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # _connect 会按需建库，先清掉 mkstemp 的空文件
    return path


class _CalendarFixture(unittest.TestCase):
    """把 trade_calendar 的缓存指向临时库，并预置 2026-09~12 的权威日历。"""

    def setUp(self):
        self.cal_db = _new_temp_db()
        self.addCleanup(lambda: os.path.exists(self.cal_db) and os.remove(self.cal_db))
        trade_calendar.refresh(
            today="2026-10-01", db_path=self.cal_db, fetcher=lambda s, e: _seed_rows()
        )
        # 订阅方(config/data_manager/...)调用的都是默认路径，这里统一重定向到临时库
        patcher = patch.object(trade_calendar, "cache_path",
                               lambda override=None: self.cal_db)
        patcher.start()
        self.addCleanup(patcher.stop)
        trade_calendar.clear_cache()
        self.addCleanup(trade_calendar.clear_cache)

    def _use_empty_calendar(self):
        """切换到空日历(模拟从未刷新成功)，返回该路径。"""
        empty = _new_temp_db()
        self.addCleanup(lambda: os.path.exists(empty) and os.remove(empty))
        patch.object(trade_calendar, "cache_path", lambda override=None: empty).start()
        self.addCleanup(trade_calendar.clear_cache)
        return empty


# ===========================================================================
# 日历本体
# ===========================================================================
class TestTradeCalendarCore(_CalendarFixture):
    """刷新 / 判定 / 降级。"""

    def test_calendar_covers_holidays(self):
        # 2026-10-01 周四、09-25 周五(中秋): 周一至周五口径会判成开市，日历口径必须休市
        self.assertEqual(trade_calendar.is_trading_day_confident("2026-10-01"), (False, True))
        self.assertEqual(trade_calendar.is_trading_day_confident("2026-09-25"), (False, True))
        self.assertEqual(trade_calendar.is_trading_day_confident("2026-09-30"), (True, True))
        self.assertEqual(trade_calendar.is_trading_day_confident("2026-10-08"), (True, True))

    def test_is_trading_day_returns_bool(self):
        self.assertFalse(trade_calendar.is_trading_day("2026-10-01"))
        self.assertTrue(trade_calendar.is_trading_day("2026-09-30"))

    def test_refresh_reports_source_and_coverage(self):
        result = trade_calendar.refresh(
            today="2026-10-01", db_path=self.cal_db, fetcher=lambda s, e: _seed_rows())
        self.assertEqual(result["source"], "tushare")
        self.assertFalse(result["degraded"])
        self.assertGreater(result["count"], 100)
        self.assertEqual(result["start"], "2026-09-01")
        self.assertEqual(result["end"], "2027-10-06")

    def test_refresh_failure_does_not_clobber_existing_calendar(self):
        """取数失败时不得用"周一至周五"近似值覆盖已有的权威日历。"""
        def boom(start, end):
            raise RuntimeError("tushare down")

        result = trade_calendar.refresh(
            today="2026-10-01", db_path=self.cal_db, fetcher=boom)

        self.assertEqual(result["source"], "weekday_fallback")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["count"], 0)
        # 权威日历原样保留: 2026-10-01 仍是休市
        self.assertEqual(trade_calendar.is_trading_day_confident("2026-10-01"), (False, True))

    def test_empty_cache_degrades_without_confidence(self):
        """缓存为空: 退化为周一至周五，但必须标记不可信以便调用方告警。"""
        self._use_empty_calendar()
        is_open_, confident = trade_calendar.is_trading_day_confident("2026-10-01")
        self.assertTrue(is_open_)    # 周一至周五口径把周四当开市
        self.assertFalse(confident)  # → 调用方据此告警，而不是静默采信

    def test_legacy_weekday_fallback_rows_are_not_trusted(self):
        """库里遗留的降级行同样不算权威。"""
        trade_calendar.upsert_calendar(
            [("20261001", 1)], source="weekday_fallback", db_path=self.cal_db)
        self.assertEqual(trade_calendar.is_trading_day_confident("2026-10-01"), (True, False))


class TestCalendarNavigation(_CalendarFixture):
    """前后交易日与区间枚举。"""

    def test_recent_trading_dates_crosses_holiday_block(self):
        """2026-10-09 往前回溯: 10-08 开市，再往前应跨过整个国庆落到 09-30。"""
        self.assertEqual(trade_calendar.recent_trading_dates(2, "2026-10-09"),
                         ["2026-10-08", "2026-09-30"])
        self.assertEqual(trade_calendar.recent_trading_dates(1, "2026-10-12"),
                         ["2026-10-09"])

    def test_previous_trading_day_skips_holiday(self):
        # 10-08 的前一交易日是 09-30，不是 10-07
        self.assertEqual(trade_calendar.previous_trading_day("2026-10-08"), date(2026, 9, 30))
        self.assertEqual(trade_calendar.previous_trading_day("2026-09-30"), date(2026, 9, 29))

    def test_next_trading_day_skips_holiday(self):
        # 10-01 当天及之后最近的交易日是 10-08
        self.assertEqual(trade_calendar.next_trading_day("2026-10-01"), date(2026, 10, 8))
        # 交易日当天返回自身
        self.assertEqual(trade_calendar.next_trading_day("2026-09-30"), date(2026, 9, 30))

    def test_trading_days_between(self):
        self.assertEqual(
            trade_calendar.trading_days_between("2026-09-29", "2026-10-09"),
            ["2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"],
        )

    def test_without_calendar_matches_old_weekday_behaviour(self):
        """日历缺失时必须与旧口径一致(周一至周五)，不能改变既有行为。"""
        self._use_empty_calendar()
        self.assertEqual(trade_calendar.recent_trading_dates(2, "2026-06-14"),
                         ["2026-06-12", "2026-06-11"])


# ===========================================================================
# 主程序各调用点
# ===========================================================================
class TestConfigTradeTime(_CalendarFixture):
    """config.is_trade_time / is_market_hours / get_continuous_trading_seconds。"""

    def setUp(self):
        super().setUp()
        self._old = (config.ENABLE_SIMULATION_MODE, config.DEBUG_SIMU_STOCK_DATA)
        config.ENABLE_SIMULATION_MODE = False
        config.DEBUG_SIMU_STOCK_DATA = False
        self.addCleanup(self._restore)

    def _restore(self):
        config.ENABLE_SIMULATION_MODE, config.DEBUG_SIMU_STOCK_DATA = self._old

    def test_holiday_is_not_trade_time(self):
        """2026-10-01(周四) 长假首日: 时段完全对得上，但必须因休市而不允许交易。"""
        self.assertFalse(config.is_trade_time(datetime(2026, 10, 1, 10, 0)))
        self.assertFalse(config.is_trade_time(datetime(2026, 10, 6, 14, 0)))
        self.assertFalse(config.is_market_hours(datetime(2026, 10, 1, 10, 0)))

    def test_trading_day_still_allows_trade(self):
        self.assertTrue(config.is_trade_time(datetime(2026, 9, 30, 10, 0)))
        self.assertTrue(config.is_trade_time(datetime(2026, 10, 8, 10, 0)))
        self.assertTrue(config.is_market_hours(datetime(2026, 10, 8, 10, 0)))
        # 09:25~09:30 早盘预挂窗口仍然允许下单
        self.assertTrue(config.is_trade_time(datetime(2026, 10, 8, 9, 26)))

    def test_weekend_is_not_trade_time(self):
        self.assertFalse(config.is_trade_time(datetime(2026, 9, 12, 10, 0)))

    def test_continuous_trading_seconds_skips_holiday(self):
        """长假期间的委托不应累计任何"连续竞价秒数"。"""
        self.assertEqual(
            config.get_continuous_trading_seconds(
                datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 1, 15, 0)),
            0,
        )

    def test_continuous_trading_seconds_still_counts_normal_day(self):
        self.assertEqual(
            config.get_continuous_trading_seconds(
                datetime(2026, 9, 30, 11, 29, 50), datetime(2026, 9, 30, 13, 0, 0)),
            10,
        )

    def test_debug_mode_bypasses_calendar_explicitly(self):
        """DEBUG 全周模拟模式通过 ignore_trade_calendar 显式跳过日历。"""
        debug_schedule = dict(config.TRADE_TIME, ignore_trade_calendar=True)
        with patch.object(config, 'TRADE_TIME', debug_schedule):
            self.assertTrue(config._is_in_trade_schedule(
                datetime(2026, 10, 1, 10, 0), config.TRADE_TIME))


class TestPremarketSyncUsesCalendar(_CalendarFixture):
    """盘前同步的下次触发时间。"""

    class _FrozenDatetime(datetime):
        frozen = datetime(2026, 9, 30, 16, 0, 0)

        @classmethod
        def now(cls, tz=None):
            return cls.frozen

    def _next_sync(self, frozen):
        scheduler = PreMarketSyncScheduler.__new__(PreMarketSyncScheduler)
        scheduler.sync_time = (9, 25)
        self._FrozenDatetime.frozen = frozen
        with patch('premarket_sync.datetime', self._FrozenDatetime):
            return scheduler.calculate_next_sync_time()

    def test_skips_holiday_block(self):
        """节前最后一个交易日盘后 → 下次同步应落到 10-08，而不是 10-01。"""
        self.assertEqual(
            self._next_sync(datetime(2026, 9, 30, 16, 0)),
            datetime(2026, 10, 8, 9, 25),
        )

    def test_today_before_sync_time_keeps_today(self):
        self.assertEqual(
            self._next_sync(datetime(2026, 9, 30, 8, 0)),
            datetime(2026, 9, 30, 9, 25),
        )

    def test_skips_weekend(self):
        self.assertEqual(
            self._next_sync(datetime(2026, 9, 11, 16, 0)),  # 周五盘后
            datetime(2026, 9, 14, 9, 25),                   # 下周一
        )


class TestDataManagerUsesCalendar(_CalendarFixture):
    """日线补齐的结束日期。"""

    def _end_date(self, now):
        return DataManager._get_completed_history_end_date(None, now)

    def test_holiday_window_falls_back_to_last_trading_day(self):
        """10-03(周六) → 最近已完成交易日是 09-30，按周内日会推出 10-02。"""
        self.assertEqual(self._end_date(datetime(2026, 10, 3, 16, 0)), "20260930")

    def test_trading_day_after_close_uses_today(self):
        self.assertEqual(self._end_date(datetime(2026, 9, 30, 16, 0)), "20260930")

    def test_trading_day_before_close_uses_previous_trading_day(self):
        self.assertEqual(self._end_date(datetime(2026, 9, 30, 10, 0)), "20260929")


class TestSettlementUsesCalendar(_CalendarFixture):
    """收盘快照的触发判定。"""

    _TARGET = time(15, 5)

    def _should_run(self, now, last_run_date=None):
        trading, confident = trade_calendar.is_trading_day_confident(now.date())
        return should_run_close_snapshot(now, self._TARGET, last_run_date, trading, confident)

    def test_holiday_does_not_run(self):
        self.assertFalse(self._should_run(datetime(2026, 10, 1, 15, 10)))

    def test_trading_day_runs(self):
        self.assertTrue(self._should_run(datetime(2026, 9, 30, 15, 10)))

    def test_before_target_time_does_not_run(self):
        self.assertFalse(self._should_run(datetime(2026, 9, 30, 15, 4)))

    def test_already_ran_today_does_not_run(self):
        self.assertFalse(self._should_run(datetime(2026, 9, 30, 15, 10), date(2026, 9, 30)))

    def test_weekend_does_not_run_even_without_calendar(self):
        """日历不可用时退化为周一至周五，周六仍必须排除（周六实测踩过）。"""
        self._use_empty_calendar()
        self.assertFalse(self._should_run(datetime(2026, 10, 3, 15, 10)))


class TestUtilsTradingDays(_CalendarFixture):
    """utils.get_trading_days。"""

    def test_excludes_holidays(self):
        self.assertEqual(
            utils.get_trading_days("2026-09-29", "2026-10-09"),
            ["2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"],
        )


if __name__ == "__main__":
    unittest.main()
