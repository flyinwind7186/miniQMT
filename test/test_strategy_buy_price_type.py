import inspect
import threading
import unittest
from unittest.mock import MagicMock, patch

from strategy import TradingStrategy
from trading_executor import TradingExecutor
from xtquant import xtconstant
import config


class TestStrategyBuyPriceType(unittest.TestCase):
    def test_initial_buy_uses_counterparty_price_type(self):
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.indicator_calculator = MagicMock()
        strategy.position_manager = MagicMock()
        strategy.position_manager.get_position.return_value = None
        strategy.trading_executor = MagicMock()
        strategy.trading_executor.buy_stock.return_value = "ORDER-1"
        strategy.processed_signals = set()
        # 信号去重集合改为锁保护后，__new__ 构造需显式装配这两个字段
        strategy.signal_lock = threading.Lock()
        strategy.macd_sell_notified = set()

        with patch("strategy.config.POSITION_UNIT", 10000):
            ok = strategy.execute_buy_strategy("002815.SZ", buy_signal=True)

        self.assertTrue(ok)
        # 价格类型不再由调用方硬编码，统一走执行器默认值 11=FIX_PRICE
        strategy.trading_executor.buy_stock.assert_called_once_with(
            "002815.SZ",
            amount=10000,
        )
        default_price_type = inspect.signature(
            TradingExecutor.buy_stock).parameters['price_type'].default
        self.assertEqual(default_price_type, xtconstant.FIX_PRICE,
                         "实盘买入默认必须为 11=FIX_PRICE，否则定价权会交回 QMT 客户端")

    def test_global_monitor_disabled_short_circuits_strategy_execution(self):
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.data_manager = MagicMock()
        strategy.indicator_calculator = MagicMock()
        strategy.position_manager = MagicMock()
        strategy.trading_executor = MagicMock()

        original_monitoring = config.ENABLE_AUTO_OPERATION
        try:
            config.ENABLE_AUTO_OPERATION = False
            strategy.check_and_execute_strategies("002815.SZ")
        finally:
            config.ENABLE_AUTO_OPERATION = original_monitoring

        strategy.data_manager.update_stock_data.assert_not_called()
        strategy.indicator_calculator.calculate_all_indicators.assert_not_called()


if __name__ == "__main__":
    unittest.main()
