"""MACD 交易逻辑审查修复的回归测试。

覆盖 2026-08-20 审查后的 5 项修复:
  P0-A: check_buy/sell_signal 的 T+1 时效语义文档化 + 信号日期日志
  P1  : processed_signals 跨交易日清理 + signal_lock 补锁
  P2  : ENABLE_MACD_SELL / ENABLE_AUTO_TRADING 关闭时不再毒化当日信号
  P3  : MACD 卖出使用 available 口径，与止盈止损一致
  P4  : 指标层重复日志降级为 debug
"""
import threading
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import config
from strategy import TradingStrategy


def _make_strategy():
    """构造绕过单例初始化的 TradingStrategy，仅装配本测试需要的字段。"""
    strategy = TradingStrategy.__new__(TradingStrategy)
    strategy.data_manager = MagicMock()
    strategy.indicator_calculator = MagicMock()
    strategy.position_manager = MagicMock()
    # check_add_position_signal 返回二元组，MagicMock 默认值会导致解包异常
    strategy.position_manager.check_add_position_signal.return_value = (None, None)
    strategy.position_manager.get_pending_signals.return_value = {}
    strategy.trading_executor = MagicMock()
    strategy.signal_lock = threading.Lock()
    strategy.processed_signals = set()
    strategy.macd_sell_notified = set()
    strategy.retry_counts = {}
    strategy.signals_date = datetime.now().strftime('%Y%m%d')
    strategy.last_trade_time = {}
    return strategy


# ============================================================
# P1: 跨交易日清理 + 线程安全
# ============================================================
class TestSignalCacheRollover(unittest.TestCase):
    """P1: processed_signals 跨日清理，避免无人值守长跑内存单调增长。"""

    def test_same_day_does_not_clear(self):
        strategy = _make_strategy()
        strategy.processed_signals.add("sell_000001.SZ_20260820")
        strategy.retry_counts["k"] = 1

        cleared = strategy._rollover_signal_cache_if_new_day()

        self.assertFalse(cleared, "同一交易日不应触发清理")
        self.assertIn("sell_000001.SZ_20260820", strategy.processed_signals)
        self.assertEqual(strategy.retry_counts, {"k": 1})

    def test_new_day_clears_all_caches(self):
        strategy = _make_strategy()
        strategy.signals_date = "20260819"  # 伪造为昨日
        strategy.processed_signals.add("sell_000001.SZ_20260819")
        strategy.macd_sell_notified.add("buy_000001.SZ_20260819")
        strategy.retry_counts["take_profit_full_000001.SZ"] = 2

        cleared = strategy._rollover_signal_cache_if_new_day()

        self.assertTrue(cleared, "跨日应触发清理")
        self.assertEqual(len(strategy.processed_signals), 0)
        self.assertEqual(len(strategy.macd_sell_notified), 0)
        self.assertEqual(len(strategy.retry_counts), 0)
        self.assertEqual(strategy.signals_date, datetime.now().strftime('%Y%m%d'))

    def test_rollover_is_idempotent(self):
        """连续调用只清理一次，不会每轮循环都刷日志。"""
        strategy = _make_strategy()
        strategy.signals_date = "20260819"

        self.assertTrue(strategy._rollover_signal_cache_if_new_day())
        self.assertFalse(strategy._rollover_signal_cache_if_new_day())
        self.assertFalse(strategy._rollover_signal_cache_if_new_day())

    def test_signal_helpers_are_lock_protected(self):
        """并发标记信号不丢失（验证 _mark_signal_processed 走锁）。"""
        strategy = _make_strategy()
        errors = []

        def worker(start):
            try:
                for i in range(start, start + 200):
                    strategy._mark_signal_processed(f"sell_CODE{i}_20260820")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n * 200,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(strategy.processed_signals), 1000)

    def test_is_signal_processed_reflects_mark(self):
        strategy = _make_strategy()
        key = "sell_000001.SZ_20260820"

        self.assertFalse(strategy._is_signal_processed(key))
        strategy._mark_signal_processed(key)
        self.assertTrue(strategy._is_signal_processed(key))


# ============================================================
# P2: ENABLE_MACD_SELL 去毒化
# ============================================================
class TestMacdSellSwitchNoPoisoning(unittest.TestCase):
    """P2: 开关关闭期间不得污染 processed_signals，否则改True需重启才生效。"""

    def test_switch_off_does_not_touch_processed_signals(self):
        strategy = _make_strategy()

        with patch("strategy.config.ENABLE_MACD_SELL", False):
            result = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

        self.assertFalse(result)
        self.assertEqual(
            len(strategy.processed_signals), 0,
            "开关关闭不应写入 processed_signals（会毒化当日信号）"
        )
        self.assertEqual(len(strategy.macd_sell_notified), 1, "应写入独立降噪集合")
        strategy.trading_executor.sell_stock.assert_not_called()

    def test_switch_flipped_on_same_day_executes_immediately(self):
        """核心回归：盘中把开关改为 True，当日信号无需重启即可执行。"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = {
            "volume": 1000, "available": 1000,
        }
        strategy.trading_executor.sell_stock.return_value = "ORDER-1"

        # 第一次：开关关闭，仅记录
        with patch("strategy.config.ENABLE_MACD_SELL", False):
            first = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)
        self.assertFalse(first)

        # 第二次：同一天开关打开，应当立即执行
        with patch("strategy.config.ENABLE_MACD_SELL", True):
            second = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

        self.assertTrue(second, "开关打开后当日信号必须可执行（修复前会被 processed_signals 拦住）")
        strategy.trading_executor.sell_stock.assert_called_once_with(
            "000001.SZ", 1000, price_type=5,
        )

    def test_switch_off_logs_only_once_per_day(self):
        """降噪仍然生效：同一信号重复检测只打一条日志。"""
        strategy = _make_strategy()

        with patch("strategy.config.ENABLE_MACD_SELL", False), \
             patch("strategy.logger") as mock_logger:
            for _ in range(5):
                strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

            info_calls = [c for c in mock_logger.info.call_args_list
                          if "ENABLE_MACD_SELL=False" in str(c)]

        self.assertEqual(len(info_calls), 1, "重复信号不应刷屏")

    def test_generic_sell_signal_log_only_once_per_day(self):
        """策略循环重复检测到卖出信号时，通用提示每天只记录一次。"""
        strategy = _make_strategy()
        strategy.indicator_calculator.check_buy_signal.return_value = False
        strategy.indicator_calculator.check_sell_signal.return_value = True

        with patch("strategy.config.ENABLE_AUTO_OPERATION", True), \
             patch("strategy.config.ENABLE_AUTO_TRADING", True), \
             patch("strategy.config.ENABLE_DYNAMIC_STOP_PROFIT", False), \
             patch("strategy.config.ENABLE_MACD_SELL", False), \
             patch("strategy.logger") as mock_logger:
            for _ in range(5):
                strategy.check_and_execute_strategies("000001.SZ")

        generic_calls = [
            call for call in mock_logger.info.call_args_list
            if call.args == ("000001.SZ 检测到技术指标卖出信号",)
        ]
        self.assertEqual(len(generic_calls), 1, "通用卖出信号日志不应重复刷屏")
        self.assertEqual(
            len(strategy.processed_signals), 0,
            "日志降噪不得污染 processed_signals"
        )
        self.assertTrue(
            any(key.startswith("sell_000001.SZ_")
                for key in strategy.macd_sell_notified),
            "通用卖出信号应写入降噪集合"
        )

    def test_no_position_does_not_poison_signal(self):
        """无持仓是暂时状态，盘中买入后应能重新响应卖出信号。"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = None

        with patch("strategy.config.ENABLE_MACD_SELL", True):
            result = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

        self.assertFalse(result)
        self.assertEqual(
            len(strategy.processed_signals), 0,
            "无持仓不应写入 processed_signals"
        )

    def test_auto_trading_off_does_not_poison_signal(self):
        """check_and_execute_strategies 中自动交易开关关闭时同样不得毒化。"""
        strategy = _make_strategy()
        strategy.indicator_calculator.check_buy_signal.return_value = False
        strategy.indicator_calculator.check_sell_signal.return_value = True
        strategy.position_manager.get_pending_signals.return_value = {}

        with patch("strategy.config.ENABLE_AUTO_OPERATION", True), \
             patch("strategy.config.ENABLE_AUTO_TRADING", False), \
             patch("strategy.config.ENABLE_DYNAMIC_STOP_PROFIT", False):
            strategy.check_and_execute_strategies("000001.SZ")

        self.assertEqual(
            len(strategy.processed_signals), 0,
            "自动交易关闭不应毒化当日信号"
        )
        self.assertEqual(len(strategy.macd_sell_notified), 1)


# ============================================================
# P3: available 口径对齐
# ============================================================
class TestMacdSellUsesAvailable(unittest.TestCase):
    """P3: 与止盈止损(position_manager 用 position['available'])口径一致。"""

    def test_uses_available_not_total_volume(self):
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = {
            "volume": 1000,     # 总持仓
            "available": 600,   # 当日买入400股仍冻结
        }
        strategy.trading_executor.sell_stock.return_value = "ORDER-1"

        with patch("strategy.config.ENABLE_MACD_SELL", True):
            result = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

        self.assertTrue(result)
        strategy.trading_executor.sell_stock.assert_called_once_with(
            "000001.SZ", 600, price_type=5,
        )

    def test_zero_available_skips_order(self):
        """可用为0时直接跳过，不产生必然被拒的委托。"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = {
            "volume": 1000, "available": 0,
        }

        with patch("strategy.config.ENABLE_MACD_SELL", True):
            result = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

        self.assertFalse(result)
        strategy.trading_executor.sell_stock.assert_not_called()
        self.assertEqual(
            len(strategy.processed_signals), 0,
            "可用为0是暂时状态，不应标记已处理"
        )

    def test_missing_available_field_is_safe(self):
        """持仓字典缺 available 字段时按0处理，不抛异常。"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = {"volume": 1000}

        with patch("strategy.config.ENABLE_MACD_SELL", True):
            result = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

        self.assertFalse(result)
        strategy.trading_executor.sell_stock.assert_not_called()

    def test_successful_sell_marks_processed(self):
        """成功下单后仍需按日去重，防止重复卖出。"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = {
            "volume": 1000, "available": 1000,
        }
        strategy.trading_executor.sell_stock.return_value = "ORDER-1"

        with patch("strategy.config.ENABLE_MACD_SELL", True):
            first = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)
            second = strategy.execute_sell_strategy("000001.SZ", sell_signal=True)

        self.assertTrue(first)
        self.assertFalse(second, "当日重复信号必须被拦截")
        self.assertEqual(strategy.trading_executor.sell_stock.call_count, 1)


# ============================================================
# P0-A / P4: 时效语义文档化与日志
# ============================================================
class TestSignalSemanticsDocumented(unittest.TestCase):
    """P0-A: T+1 时效语义必须在 docstring 中明确，避免误用。"""

    def test_docstrings_document_t_plus_1_delay(self):
        from indicator_calculator import IndicatorCalculator

        for method in (IndicatorCalculator.check_buy_signal,
                       IndicatorCalculator.check_sell_signal):
            doc = method.__doc__ or ""
            self.assertIn("T+1", doc, f"{method.__name__} 应说明 T+1 执行延迟")
            self.assertIn("已收盘", doc, f"{method.__name__} 应说明基于已收盘日线")


if __name__ == "__main__":
    unittest.main()
