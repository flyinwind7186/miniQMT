"""运行日志暴露的撤单回调阻塞和重复信号通知回归。"""
import os
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("MINIQMT_DISABLE_DOTENV", "1")

import config
from position_manager import PositionManager
from strategy import TradingStrategy


class TestCancelCallbackRegression(unittest.TestCase):
    def setUp(self):
        self.pm = PositionManager.__new__(PositionManager)
        self.pm.pending_orders_lock = threading.Lock()
        self.pm.pending_orders = {}
        self.pm.grid_manager = None
        self.pm.track_order("000001", 1001, "stop_loss", {"volume": 100})
        self.pm.pending_orders["000001"].update(
            status="cancel_requested", reorder_after_cancel=True)

    def test_reorder_does_not_block_callback_or_allow_duplicate_orders(self):
        """回调须先返回，异步下单回报才能继续分发；重挂期间仍须阻断同股新单。"""
        pm = self.pm
        order = types.SimpleNamespace(stock_code="000001.SZ", order_id=1001,
                                      order_status=54)
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        workers = []

        def reorder(*args):
            workers.append(threading.current_thread())
            entered.set()
            release.wait(5)
            pm.track_order("000001", 1002, "stop_loss", {"volume": 100})

        def callback():
            try:
                pm._on_order_callback(order)
            finally:
                returned.set()

        callback_thread = threading.Thread(target=callback)
        with patch.object(config, "ENABLE_SIMULATION_MODE", True), \
             patch.object(pm, "_request_immediate_position_refresh"), \
             patch.object(pm, "_reorder_after_cancel", side_effect=reorder) as submit:
            try:
                callback_thread.start()
                self.assertTrue(entered.wait(2))
                self.assertTrue(returned.wait(1), "重挂阻塞了 QMT 回调线程")
                self.assertIsNot(workers[0], callback_thread)
                self.assertTrue(pm._has_tracked_pending_order("000001.SZ"))
                pm._on_order_callback(order)
                pm._confirm_canceled_order("000001", 1001, "撤单状态查询")
                with patch.object(pm, "_query_order_status") as query:
                    pm._handle_timeout_order(dict(pm.pending_orders["000001"]))
                    query.assert_not_called()
                self.assertEqual(submit.call_count, 1)
            finally:
                release.set()
                callback_thread.join(3)
                for worker in workers:
                    worker.join(3)

        self.assertEqual(pm.pending_orders["000001"]["order_id"], 1002)

    def test_worker_start_failure_preserves_pending_for_retry(self):
        """线程启动失败时保留旧单，后续状态查询可再次执行重挂。"""
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("线程启动失败")):
            with self.assertRaises(RuntimeError):
                self.pm._confirm_canceled_order("000001", 1001, "撤单回报", defer_reorder=True)
        self.assertEqual(self.pm.pending_orders["000001"]["status"], "cancel_requested")
        with patch.object(self.pm, "_reorder_after_cancel") as submit:
            self.assertTrue(self.pm._confirm_canceled_order("000001", 1001, "撤单状态查询"))
        submit.assert_called_once()
        self.assertFalse(self.pm._has_tracked_pending_order("000001"))

    def test_reorder_failure_releases_reservation(self):
        """重挂失败后释放占位，不让同股信号被永久阻断。"""
        with patch.object(self.pm, "_reorder_after_cancel", side_effect=RuntimeError("下单失败")):
            with self.assertRaises(RuntimeError):
                self.pm._confirm_canceled_order("000001", 1001, "撤单状态查询")
        self.assertFalse(self.pm._has_tracked_pending_order("000001"))


class TestDynamicSignalNotificationRegression(unittest.TestCase):
    def test_repeated_signal_refreshes_data_but_not_notification(self):
        """阻断中的信号会持续刷新快照；仅新信号或类型变化需要通知。"""
        pm = PositionManager.__new__(PositionManager)
        pm.signal_lock = threading.Lock()
        pm.latest_signals = {}
        with patch.object(config, "ENABLE_SIMULATION_MODE", True), \
             patch.object(config, "ENABLE_DYNAMIC_STOP_PROFIT", True), \
             patch.object(config, "ENABLE_AUTO_TRADING", True), \
             patch.object(pm, "_is_stop_profit_enabled", return_value=True), \
             patch.object(pm, "_has_tracked_pending_order", return_value=False), \
             patch.object(pm, "get_position", return_value=None), \
             patch.object(pm, "check_trading_signals") as detect, \
             patch("position_manager.logger.info") as notify:
            for price in (9.0, 8.9, 8.8):
                detect.return_value = ("stop_loss", {"current_price": price})
                self.assertEqual(pm._detect_and_enqueue_dynamic_signal("000001", price),
                                 "stop_loss")
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(pm.latest_signals["000001"]["info"]["current_price"], 8.8)
            detect.return_value = ("take_profit_full", {"current_price": 11.0})
            pm._detect_and_enqueue_dynamic_signal("000001", 11.0)
            self.assertEqual(notify.call_count, 2)
            pm.latest_signals.clear()
            pm._detect_and_enqueue_dynamic_signal("000001", 11.0)
            self.assertEqual(notify.call_count, 3)

    def test_blocked_stop_loss_does_not_report_immediate_execution(self):
        """T+1 阻断时只保留节流告警，不反复宣称立即执行，也不消耗信号。"""
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.position_manager = MagicMock()
        strategy.data_manager = MagicMock()
        strategy.indicator_calculator = MagicMock()
        strategy.position_manager.get_pending_signals.return_value = {
            "000001": {"type": "stop_loss", "info": {"current_price": 9.0}}}
        strategy.position_manager.validate_trading_signal.return_value = (
            False, "blocked", "available_zero_sync_delay")
        with patch.object(config, "ENABLE_SIMULATION_MODE", True), \
             patch.object(config, "ENABLE_DYNAMIC_STOP_PROFIT", True), \
             patch.object(config, "ENABLE_AUTO_TRADING", True), \
             patch.object(config, "is_global_monitor_enabled", return_value=True), \
             patch.object(config, "determine_stop_loss_add_position_priority",
                          return_value={"priority": "stop_loss_first", "scenario": "B"}), \
             patch.object(strategy, "_execute_stop_loss_signal") as execute, \
             patch("strategy.logger.warning") as warning, \
             patch("strategy.log_throttled"):
            for _ in range(3):
                strategy.check_and_execute_strategies("000001")
        warning.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(strategy.position_manager.validate_trading_signal.call_count, 3)
        strategy.position_manager.mark_signal_processed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
