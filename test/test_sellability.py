"""可卖数量、冻结原因与执行门控回归，全部使用测试替身。"""
import os
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("MINIQMT_DISABLE_DOTENV", "1")

import config
from strategy import TradingStrategy
from order_utils import (calculate_sell_volume, classify_position_restriction,
                         is_restricted_stop_loss, position_quantities)
from position_manager import PositionManager
from test.test_base import TestBase
from xtquant_manager.stop_profit import StopProfitMonitor


class TestSellabilityExecution(unittest.TestCase):
    def setUp(self):
        market = patch('xtquant_manager.stop_profit.is_continuous_trade_time', return_value=True)
        market.start()
        self.addCleanup(market.stop)

    def test_gateway_zero_available_never_submits_minimum_sell(self):
        monitor = StopProfitMonitor(SimpleNamespace())
        account = MagicMock()
        account.query_positions.return_value = [
            {"证券代码": "000001", "股票余额": 1000, "可用余额": 0}]
        account.query_orders.return_value = []
        account.order_stock.return_value = 123
        monitor._emit_signal("TEST", account, "000001", "take_profit_half", {
            "volume": 0, "current_price": 11.0, "sell_ratio": 0.6})
        account.order_stock.assert_not_called()
        self.assertNotIn("TEST:000001:take_profit_half", monitor._signal_history)

    def test_confirmed_zero_available_does_not_fall_back_to_old_signal(self):
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.position_manager = MagicMock()
        strategy.position_manager.get_position.return_value = {"volume": 1000, "available": 0}
        with patch.object(config, "ENABLE_SIMULATION_MODE", True):
            self.assertEqual(strategy._resolve_sell_base_volume("000001", 1000), 0)

    def test_query_failure_does_not_fall_back_to_old_signal(self):
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.position_manager = MagicMock()
        strategy.position_manager.get_position.side_effect = RuntimeError('查询失败')
        self.assertEqual(strategy._resolve_sell_base_volume('000001', 1000), 0)

    def test_gateway_rechecks_available_and_waits_for_actual_fill(self):
        from xtquant_manager.stop_profit import PositionState
        monitor = StopProfitMonitor(SimpleNamespace())
        state = PositionState(stock_code='000001')
        monitor._states['TEST'] = {'000001': state}
        account = MagicMock()
        account.query_positions.return_value = [
            {'证券代码': '000001', '股票余额': 2000, '可用余额': 1000}]
        account.query_orders.return_value = []
        account.order_stock.return_value = 123
        monitor._emit_signal('TEST', account, '000001', 'take_profit_half', {
            'volume': 2000, 'current_price': 11.0, 'sell_ratio': 0.6})
        self.assertEqual(account.order_stock.call_args.kwargs['order_volume'], 600)
        self.assertFalse(state.profit_triggered)
        account.query_orders.return_value = [
            {'订单编号': 123, '委托状态': 56, '成交数量': 600}]
        monitor._confirm_pending_signals('TEST', account)
        self.assertTrue(state.profit_triggered)
        self.assertEqual(monitor._pending_signals, {})

    def test_gateway_active_sell_blocks_second_signal(self):
        monitor = StopProfitMonitor(SimpleNamespace())
        account = MagicMock()
        account.query_positions.return_value = [
            {'证券代码': '000001', '股票余额': 2000, '可用余额': 1000}]
        account.query_orders.return_value = [
            {'证券代码': '000001', '委托类型': 24, '委托状态': 55}]
        monitor._emit_signal('TEST', account, '000001', 'stop_loss', {'volume': 1000, 'current_price': 9})
        account.order_stock.assert_not_called()
        self.assertEqual(monitor._signal_history, {})

    def test_real_sell_rejects_stale_position_quantity(self):
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.position_manager = MagicMock()
        strategy.position_manager.get_position.return_value = {
            'volume': 1000, 'available': 1000,
            'position_asof': (datetime.now() - timedelta(days=1)).isoformat()}
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            self.assertEqual(strategy._resolve_sell_base_volume('000001', 1000), 0)

    def test_skip_pending_switch_never_bypasses_zero_available(self):
        pm = PositionManager.__new__(PositionManager)
        pm.memory_conn_lock = threading.RLock()
        pm.signal_lock = threading.Lock()
        pm.latest_signals = {}
        pm._position_restrictions = {}
        with patch.object(config, 'ENABLE_SIMULATION_MODE', True), \
             patch.object(config, 'ALLOW_TAKE_PROFIT_FULL_WITH_PENDING', True), \
             patch.object(pm, 'get_position', return_value={'volume': 1000, 'available': 0}):
            self.assertFalse(pm.validate_trading_signal('000001', 'take_profit_full', {
                'current_price': 11, 'cost_price': 10}))

    def test_cached_gateway_price_does_not_renew_its_own_age(self):
        from xtquant_manager.stop_profit import PositionState
        monitor = StopProfitMonitor(SimpleNamespace())
        state = PositionState(stock_code='000001', last_price=11, last_price_time=1)
        monitor._states['TEST'] = {'000001': state}
        account = MagicMock()
        account.get_full_tick.return_value = {}
        with patch('xtquant_manager.stop_profit.time.time', return_value=100), \
             patch.object(monitor, '_emit_signal') as emit:
            monitor._check_position('TEST', account, {
                '证券代码': '000001', '股票余额': 1000, '可用余额': 0,
                '成本价': 10, '市价': 0}, monitor._cfg)
            self.assertEqual(state.last_price_time, 1)
            emit.assert_not_called()

    def test_gateway_query_failure_is_distinct_from_empty_holdings(self):
        from xtquant_manager.account import XtQuantAccount, AccountConfig
        account = XtQuantAccount(AccountConfig(account_id='TEST', qmt_path='dummy'))
        account._xt_trader = MagicMock()
        with patch.object(account, '_check_trader', return_value=True), \
             patch.object(account, '_call', return_value=None):
            self.assertIsNone(account.query_positions(strict=True))
            self.assertEqual(account.query_positions(), [])
            self.assertIsNone(account.query_orders(strict=True))

    def test_gateway_position_snapshot_exposes_verified_t1_and_batches_evidence(self):
        from xtquant_manager.account import XtQuantAccount, AccountConfig
        account = XtQuantAccount(AccountConfig(account_id='TEST', qmt_path='dummy'))
        account._xt_trader = MagicMock()
        position = SimpleNamespace(account_type=1, account_id='TEST', stock_code='000001.SZ',
                                   volume=1000, can_use_volume=0, open_price=10, market_value=10000)
        trades = [SimpleNamespace(stock_code='000001.SZ', order_type=23,
                                  traded_volume=1000, traded_id='BUY1')]
        with patch.object(account, '_check_trader', return_value=True), \
             patch.object(account, '_call', side_effect=[[position], [], trades, [position]]) as query:
            result = account.query_positions(strict=True)
            self.assertEqual(result[0]['restriction_reason'], 't1')
            self.assertEqual(result[0]['unavailable_volume'], 1000)
            self.assertIsNotNone(result[0]['position_asof'])
            account.query_positions(strict=True)
            self.assertEqual(query.call_count, 4)

    def test_gateway_cross_day_rechecks_new_position_instead_of_resubmitting_old_signal(self):
        monitor = StopProfitMonitor(SimpleNamespace())
        monitor._pending_signals[('TEST', '000001')] = {
            'order_id': 123, 'stock_code': '000001', 'signal_type': 'stop_loss',
            'trade_date': (datetime.now() - timedelta(days=1)).date()}
        account = MagicMock()
        account.query_orders.return_value = []
        monitor._confirm_pending_signals('TEST', account, [
            {'证券代码': '000001', '股票余额': 1000, '可用余额': 1000}])
        self.assertEqual(monitor._pending_signals, {})
        account.order_stock.assert_not_called()

    def test_gateway_closed_market_never_submits_automatic_sell(self):
        monitor = StopProfitMonitor(SimpleNamespace())
        account = MagicMock()
        with patch('xtquant_manager.stop_profit.is_continuous_trade_time', return_value=False):
            monitor._emit_signal('TEST', account, '000001', 'stop_loss', {
                'volume': 1000, 'current_price': 9})
        account.order_stock.assert_not_called()
        self.assertEqual(monitor._signal_history, {})

    def test_gateway_client_checks_actual_available_without_total_fallback(self):
        from xtquant_manager.client import XtQuantClient, ClientConfig
        import pandas as pd
        client = XtQuantClient(ClientConfig(base_url='http://127.0.0.1:19999', account_id='TEST'))
        self.addCleanup(client.close)
        with patch.object(client, 'position') as query:
            for available, expected in ((0, False), (500, True), (None, False)):
                query.return_value = pd.DataFrame([{
                    '证券代码': '000001', '股票余额': 1000, '可用余额': available}])
                self.assertEqual(client.check_stock_is_av_sell('000001.SZ', 100), expected)

    def test_ipc_missing_available_is_not_total_position(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qmt-trader'))
        from qmt_ipc_trader import QmtIpcTrader
        trader = QmtIpcTrader.__new__(QmtIpcTrader)
        with patch.object(trader, '_read_account_snapshot', return_value={
                'positions': [{'stock': '000001.SZ', 'volume': 1000}]}):
            self.assertFalse(trader.check_stock_is_av_sell('000001', 100))

    def test_buy_risk_gate_blocks_automatic_but_keeps_manual_route(self):
        from trading_executor import TradingExecutor
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.trade_lock = threading.Lock()
        executor.position_manager = MagicMock()
        executor.position_manager.get_position.return_value = {
            'volume': 1000, 'available': 0, 'cost_price': 10, 'current_price': 9}
        executor.position_manager.simulate_buy_position.return_value = True
        with patch.object(config, 'ENABLE_SIMULATION_MODE', True), \
             patch.object(config, 'ENABLE_DYNAMIC_STOP_PROFIT', True), \
             patch.object(executor, '_generate_sim_order_id', return_value='SIM_TEST'), \
             patch.object(executor, '_format_stock_code_for_order', return_value='000001.SZ'):
            self.assertIsNone(executor.buy_stock('000001', volume=100, price=9, strategy='grid'))
            executor.position_manager.simulate_buy_position.assert_not_called()
            self.assertEqual(executor.buy_stock('000001', volume=100, price=9, strategy='M_simu'), 'SIM_TEST')

    def test_executor_availability_query_failure_stops_sell(self):
        from trading_executor import TradingExecutor
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.trade_lock = threading.Lock()
        executor.position_manager = MagicMock()
        executor.position_manager.qmt_trader.check_stock_is_av_sell.side_effect = RuntimeError('查询失败')
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(config, 'ENABLE_ALLOW_SELL', True), \
             patch.object(config, 'is_trade_time', return_value=True), \
             patch.object(executor, '_format_stock_code_for_order', return_value='000001.SZ'):
            self.assertIsNone(executor.sell_stock('000001', volume=100, price=9))
        executor.position_manager.qmt_trader.sell.assert_not_called()

    def test_partial_profit_after_rejection_can_retry_without_fake_success(self):
        from xtquant_manager.stop_profit import PositionState
        monitor = StopProfitMonitor(SimpleNamespace())
        monitor._states['TEST'] = {'000001': PositionState(stock_code='000001')}
        account = MagicMock()
        account.query_positions.return_value = [
            {'证券代码': '000001', '股票余额': 1000, '可用余额': 1000}]
        account.query_orders.return_value = []
        account.order_stock.return_value = 123
        info = {'volume': 1000, 'current_price': 11.0, 'sell_ratio': 0.6}
        monitor._emit_signal('TEST', account, '000001', 'take_profit_half', info)
        account.query_orders.return_value = [{'订单编号': 123, '委托状态': 57, '成交数量': 0}]
        monitor._confirm_pending_signals('TEST', account)
        self.assertFalse(monitor._states['TEST']['000001'].profit_triggered)
        self.assertEqual(monitor._signal_history, {})
        account.query_orders.return_value = []
        monitor._emit_signal('TEST', account, '000001', 'take_profit_half', info)
        self.assertEqual(account.order_stock.call_count, 2)


class TestRestrictionClassification(unittest.TestCase):
    def setUp(self):
        self.position = {'stock_code': '000001', 'volume': 1000, 'available': 400}
        self.asof = datetime.now().isoformat(timespec='seconds')
        self.buy = {'证券代码': '000001.SZ', '委托类型': 23, '成交数量': 600, '成交编号': 'BUY1'}
        self.sell = {'证券代码': '000001.SZ', '委托类型': 24, '委托数量': 800,
                     '成交数量': 200, '委托状态': 55}

    def classify(self, **kwargs):
        return classify_position_restriction(self.position, position_asof=self.asof, **kwargs)

    def test_confirmed_today_buy_is_t1(self):
        result = self.classify(trades=[self.buy], orders=[])
        self.assertEqual(result['restriction_reason'], 't1')
        self.assertEqual(result['unavailable_volume'], 600)

    def test_active_sell_is_not_t1(self):
        self.assertEqual(self.classify(trades=[], orders=[self.sell])['restriction_reason'], 'sell_order')

    def test_mixed_evidence_accounts_for_exact_difference(self):
        self.buy['成交数量'] = 300
        self.sell['委托数量'] = 500
        self.assertEqual(self.classify(trades=[self.buy], orders=[self.sell])['restriction_reason'], 'mixed')

    def test_incomplete_or_mismatched_evidence_stays_unknown(self):
        for trades, orders in ((None, []), ([self.buy], None), ([], []), ([self.buy], [self.sell])):
            with self.subTest(trades=trades, orders=orders):
                self.assertEqual(self.classify(trades=trades, orders=orders)['restriction_reason'], 'unknown')

    def test_duplicate_fills_do_not_double_today_buy(self):
        self.assertEqual(self.classify(trades=[self.buy, self.buy], orders=[])['restriction_reason'], 't1')

    def test_old_trade_and_old_snapshot_do_not_unlock_or_confirm_t1(self):
        yesterday = datetime.now() - timedelta(days=1)
        self.buy['成交时间'] = yesterday
        self.assertEqual(self.classify(trades=[self.buy], orders=[])['restriction_reason'], 'unknown')
        self.asof = yesterday.isoformat(timespec='seconds')
        self.buy.pop('成交时间')
        self.assertEqual(self.classify(trades=[self.buy], orders=[])['restriction_reason'], 'unknown')

    def test_fund_difference_is_not_assumed_t1(self):
        self.position['stock_code'] = '515050'
        self.buy['证券代码'] = '515050'
        self.assertEqual(self.classify(trades=[self.buy], orders=[])['restriction_reason'], 'unknown')

    def test_cancelled_sell_does_not_count_as_reserved(self):
        self.sell['委托状态'] = 54
        self.assertEqual(self.classify(trades=[], orders=[self.sell])['restriction_reason'], 'unknown')

    def test_other_accounts_cannot_supply_t1_evidence(self):
        self.position['account_id'] = 'TEST'
        self.buy['资金账号'] = 'OTHER'
        self.assertEqual(self.classify(trades=[self.buy], orders=[])['restriction_reason'], 'unknown')

    def test_invalid_quantities_never_become_sellable(self):
        for available in (None, -1, 1001, 1.5, float('nan'), True):
            with self.subTest(available=available):
                position = dict(self.position, available=available)
                self.assertIsNone(position_quantities(position))
                self.assertIsNone(classify_position_restriction(position)['unavailable_volume'])

    def test_legal_quantity_rounding_never_exceeds_available(self):
        for available, ratio, expected in ((0, 0.6, 0), (50, 0.6, 50), (1000, 0.6, 600),
                                           (1000, 0.05, 100), (1050, 1, 1050)):
            self.assertEqual(calculate_sell_volume(available, ratio, lot_size=100), expected)

    def test_buy_hold_only_applies_to_restricted_stop_loss(self):
        position = dict(self.position, cost_price=10)
        self.assertTrue(is_restricted_stop_loss(position, 9, -0.07))
        self.assertFalse(is_restricted_stop_loss(position, 9.5, -0.07))
        self.assertFalse(is_restricted_stop_loss(dict(position, available=1000), 9, -0.07))
        self.assertFalse(is_restricted_stop_loss(dict(position, stop_profit_enabled=False), 9, -0.07))


class TestRestrictionSnapshots(unittest.TestCase):
    def test_expired_snapshot_bumps_api_version_once(self):
        pm = PositionManager.__new__(PositionManager)
        pm.memory_conn_lock = threading.RLock()
        pm.version_lock = threading.Lock()
        pm.data_version = 10
        pm.data_changed = False
        pm._position_restrictions = {'000001': {
            'position_asof': (datetime.now() - timedelta(days=1)).isoformat(),
            'restriction_reason': 't1', 'restriction_detail': '当日买入受限'}}
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            self.assertEqual(pm.get_data_version_info()['version'], 11)
            self.assertEqual(pm.get_data_version_info()['version'], 11)
        self.assertEqual(pm._position_restrictions['000001']['restriction_reason'], 'unknown')

    def test_grid_buy_is_blocked_while_restricted_stop_loss_is_active(self):
        from grid_trading_manager import GridTradingManager
        manager = GridTradingManager.__new__(GridTradingManager)
        session = SimpleNamespace(stock_code='000001.SZ', id=1, status='active')
        with patch.object(config, 'ENABLE_DYNAMIC_STOP_PROFIT', True), \
             patch.object(manager, '_has_open_same_side_order_unlocked', return_value=False):
            plan = manager._build_grid_order_plan(session, {
                'signal_type': 'BUY', 'trigger_price': 9.0}, position_snapshot={
                    'volume': 1000, 'available': 0, 'cost_price': 10.0})
        self.assertIsNone(plan)

    def test_grid_missing_available_never_falls_back_to_total(self):
        from grid_trading_manager import GridTradingManager
        manager = GridTradingManager.__new__(GridTradingManager)
        session = SimpleNamespace(stock_code='000001.SZ', id=1, status='active')
        with patch.object(config, 'GRID_SELL_COOLDOWN', 0), \
             patch.object(manager, '_has_open_same_side_order_unlocked', return_value=False):
            plan = manager._build_grid_order_plan(session, {
                'signal_type': 'SELL', 'trigger_price': 11.0}, position_snapshot={
                    'volume': 1000, 'cost_price': 10.0})
        self.assertIsNone(plan)

    def test_stale_or_changed_snapshot_is_not_reused_as_confirmed_t1(self):
        pm = PositionManager.__new__(PositionManager)
        pm.memory_conn_lock = threading.RLock()
        position = {'volume': 1000, 'available': 0}
        pm._position_restrictions = {'000001': {
            'quantities': (1000, 0), 'unavailable_volume': 1000, 'restriction_reason': 't1',
            'restriction_detail': '当日买入受限', 'position_asof': datetime.now().isoformat()}}
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            self.assertEqual(pm.get_position_restriction('000001', position)['restriction_reason'], 't1')
            self.assertEqual(pm.get_position_restriction('000001', dict(position, available=500))['restriction_reason'], 'unknown')
            pm._position_restrictions['000001']['position_asof'] = (datetime.now() - timedelta(days=1)).isoformat()
            self.assertEqual(pm.get_position_restriction('000001', position)['restriction_reason'], 'unknown')

    def test_zero_available_risk_is_not_enqueued_or_repeated(self):
        pm = PositionManager.__new__(PositionManager)
        pm.signal_lock = threading.Lock()
        pm.memory_conn_lock = threading.RLock()
        pm.latest_signals = {}
        pm._restriction_log_states = {}
        position = {'stock_code': '000001', 'volume': 1000, 'available': 0}
        pm._position_restrictions = {'000001': {
            'quantities': (1000, 0), 'unavailable_volume': 1000, 'restriction_reason': 't1',
            'restriction_detail': '当日买入受限', 'position_asof': datetime.now().isoformat()}}
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(config, 'ENABLE_DYNAMIC_STOP_PROFIT', True), \
             patch.object(config, 'ENABLE_AUTO_TRADING', True), \
             patch.object(pm, '_is_stop_profit_enabled', return_value=True), \
             patch.object(pm, '_has_tracked_pending_order', return_value=False), \
             patch.object(pm, 'get_position', return_value=position), \
             patch.object(pm, 'check_trading_signals', return_value=('stop_loss', {'volume': 0})), \
             patch('position_manager.logger.warning') as warning:
            for _ in range(3):
                self.assertIsNone(pm._detect_and_enqueue_dynamic_signal('000001', 9.0))
            warning.assert_called_once()
            self.assertEqual(pm.latest_signals, {})


class TestSimulationFrozenBalance(TestBase):
    def test_selling_all_available_keeps_frozen_remainder(self):
        pm = PositionManager()
        self.addCleanup(pm.memory_conn.close)
        self.addCleanup(pm.stop_sync_thread)
        pm.stop_sync_thread()
        self.assertTrue(pm.simulate_buy_position('000001', 2000, 10.0))
        with pm.memory_conn_lock:
            pm.memory_conn.execute("UPDATE positions SET available=1000 WHERE stock_code='000001'")
            pm.memory_conn.commit()
        pm._increment_data_version()
        self.assertTrue(pm.simulate_sell_position('000001', 1000, 9.0, sell_type='full'))
        position = pm.get_position('000001')
        self.assertEqual(position['volume'], 1000)
        self.assertEqual(position['available'], 0)


if __name__ == "__main__":
    unittest.main()
