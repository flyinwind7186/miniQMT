"""
实盘限价委托与废单兜底回归测试。

背景（2026-10-26 实盘）:
    网格卖出以 price_type=5(LATEST_PRICE) 下单，QMT 客户端把交易所侧委托价固化为
    昨收 14.69；个股高开 +6% 后该价格落在价格笼子之外，8 笔委托全部废单
    （status_msg=委托价格超出范围），最终以低 4.7% 的价格成交。

覆盖:
1. 实盘下单默认走 11=FIX_PRICE 限价（交易所接收我们算的价）。
2. 委托价按当日涨跌停夹取，且预留滑点余量。
3. 网格废单(57) 清冷却 + 单次重挂；QMT 重复推送不重复重挂。
4. 持仓侧废单(57) 单次重挂，超过上限不再重挂。
"""

import inspect
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
import grid_trading_manager
import trading_executor
from grid_trading_manager import GridTradingManager
from position_manager import PositionManager
from test.test_grid_mece_regression import FakeOrderInfo, GridMeceRegressionBase
from trading_executor import TradingExecutor
from xtquant import xtconstant


class TestOrderPriceTypeAndClamp(unittest.TestCase):
    def test_buy_and_sell_default_to_fix_price(self):
        for fn in (TradingExecutor.buy_stock, TradingExecutor.sell_stock):
            default = inspect.signature(fn).parameters['price_type'].default
            self.assertEqual(default, xtconstant.FIX_PRICE,
                             f"{fn.__name__} 默认价格类型应为 11=FIX_PRICE")

    def _executor(self, slippage=0.01):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.position_manager = Mock()
        executor.position_manager.qmt_trader.slippage = slippage
        return executor

    def _clamp(self, price, detail, slippage=0.01):
        executor = self._executor(slippage=slippage)
        with patch.object(trading_executor.xt, 'get_instrument_detail', return_value=detail):
            return executor._clamp_order_price_to_daily_limits('603466.SH', price, '卖出')

    def test_price_within_limits_is_unchanged(self):
        self.assertAlmostEqual(
            self._clamp(15.68, {'UpStopPrice': 16.16, 'DownStopPrice': 13.22}), 15.68)

    def test_price_above_limit_is_clamped_with_slippage_reserve(self):
        # 涨停 16.16，滑点 0.01 -> 夹取到 16.15，滑点后正好落在涨停价
        self.assertAlmostEqual(
            self._clamp(16.50, {'UpStopPrice': 16.16, 'DownStopPrice': 13.22}), 16.15)

    def test_price_below_limit_is_clamped_with_slippage_reserve(self):
        # 跌停 13.22，滑点 0.01 -> 夹取到 13.23，滑点后正好落在跌停价
        self.assertAlmostEqual(
            self._clamp(12.80, {'UpStopPrice': 16.16, 'DownStopPrice': 13.22}), 13.23)

    def test_missing_limits_keeps_price(self):
        self.assertAlmostEqual(self._clamp(15.68, {'UpStopPrice': 0, 'DownStopPrice': 0}), 15.68)
        self.assertAlmostEqual(self._clamp(15.68, None), 15.68)

    def test_lookup_failure_keeps_price(self):
        executor = self._executor()
        with patch.object(trading_executor.xt, 'get_instrument_detail',
                          side_effect=RuntimeError('boom')):
            self.assertAlmostEqual(
                executor._clamp_order_price_to_daily_limits('603466.SH', 15.68, '卖出'), 15.68)


class TestGridRejectedOrderRetry(GridMeceRegressionBase):
    def setUp(self):
        super().setUp()
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_confirm = getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
        self.orig_auto_reorder = getattr(config, 'GRID_PENDING_ORDER_AUTO_REORDER', True)
        self.orig_max_attempts = getattr(config, 'GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS', 1)
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        config.GRID_PENDING_ORDER_AUTO_REORDER = True
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = 1

    def tearDown(self):
        config.GRID_PENDING_ORDER_AUTO_REORDER = self.orig_auto_reorder
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = self.orig_max_attempts
        super().tearDown()

    def _register_sell(self, session, order_id='ORDER_REJ'):
        self.manager._register_pending_grid_order(
            order_id=order_id,
            session=session,
            signal=self.sell_signal(session),
            side='SELL',
            volume=100,
            expected_price=10.5,
        )
        self.manager.last_sell_times[session.id] = time.time()
        self.manager.last_sell_prices[session.id] = 10.5

    def test_rejected_clears_cooldown_and_reorders_once(self):
        session = self.make_session()
        self._register_sell(session)

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.assertTrue(self.manager.handle_order_callback(
                FakeOrderInfo('ORDER_REJ', status=57)))
            self.assertEqual(reorder.call_count, 1)

        self.assertNotIn(session.id, self.manager.last_sell_times)
        self.assertNotIn(session.id, self.manager.last_sell_prices)

    def test_duplicate_rejected_callback_does_not_reorder_twice(self):
        session = self.make_session()
        self._register_sell(session)

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_REJ', status=57))
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_REJ', status=57))
            self.assertEqual(reorder.call_count, 1)

    def test_already_retried_order_is_not_reordered_again(self):
        session = self.make_session()
        self.manager._register_pending_grid_order(
            order_id='ORDER_RETRIED', session=session, signal=self.sell_signal(session),
            side='SELL', volume=100, expected_price=10.5)
        # 线上重挂成功后由 _reorder_grid_order_after_cancel 回写到 pending
        self.manager.pending_grid_orders['ORDER_RETRIED']['reorder_count'] = 1

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_RETRIED', status=57))
            reorder.assert_not_called()

    def test_cancel_without_request_does_not_retry(self):
        session = self.make_session()
        self._register_sell(session, order_id='ORDER_CANCELED')

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_CANCELED', status=54))
            reorder.assert_not_called()

    def test_execute_grid_trade_sell_rejected_then_retried(self):
        """端到端：卖出废单后走生产路径重挂一次（execute_grid_trade → plan → submit）。"""
        session = self.make_session()
        self.executor.sell_stock.side_effect = [
            {'order_id': 'ORDER_SELL_1'},
            {'order_id': 'ORDER_SELL_2'},
        ]

        self.assertTrue(self.manager.execute_grid_trade(self.sell_signal(session, price=10.5)))
        self.assertIn('ORDER_SELL_1', self.manager.pending_grid_orders)

        self.manager.handle_order_callback(FakeOrderInfo('ORDER_SELL_1', status=57))

        self.assertEqual(self.executor.sell_stock.call_count, 2)
        self.assertIn('ORDER_SELL_2', self.manager.pending_grid_orders)
        self.assertEqual(
            self.manager.pending_grid_orders['ORDER_SELL_2']['reorder_count'], 1)

    def test_rejected_after_cancel_uses_db_snapshot_and_dedupes(self):
        """已撤(54) 清掉内存 pending 后，废单(57) 仍应补一次重挂，且只补一次。"""
        session = self.make_session()
        self.db.create_grid_order({
            'order_id': 'ORDER_DB_REJ',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'SELL',
            'status': 'canceled',
            'requested_volume': 100,
            'expected_price': 10.5,
            'raw_signal': json.dumps(self.sell_signal(session), ensure_ascii=False),
        })

        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, name=None):
                started.append((target, args))

            def start(self):
                pass

        with patch.object(grid_trading_manager.threading, 'Thread', FakeThread):
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_DB_REJ', status=57))
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_DB_REJ', status=57))

        self.assertEqual(len(started), 1)
        self.assertEqual(self.db.get_grid_order('ORDER_DB_REJ')['status'], 'rejected')


class TestPositionRejectedOrderRetry(unittest.TestCase):
    def _manager(self):
        pm = PositionManager.__new__(PositionManager)
        pm.pending_orders = {}
        pm.pending_orders_lock = threading.Lock()
        pm._reorder_after_cancel = Mock()
        return pm

    def _pending(self, retry_count=0):
        return {
            'order_id': '5001',
            'stock_code': '603466',
            'status': 'submitted',
            'signal_type': 'stop_loss',
            'signal_info': {'volume': 800, 'current_price': 15.68,
                            'reject_retry_count': retry_count},
        }

    def test_rejected_reorders_once(self):
        pm = self._manager()
        pm.pending_orders['603466'] = self._pending()

        self.assertTrue(pm._confirm_rejected_order('603466.SH', '5001', '废单回报'))

        pm._reorder_after_cancel.assert_called_once()
        args = pm._reorder_after_cancel.call_args[0]
        self.assertEqual(args[0], '603466')
        self.assertEqual(args[1], 'stop_loss')
        self.assertEqual(args[2]['reject_retry_count'], 1)
        self.assertEqual(args[2]['volume'], 800)

    def test_rejected_second_time_stops_retrying(self):
        pm = self._manager()
        pm.pending_orders['603466'] = self._pending(retry_count=1)

        self.assertTrue(pm._confirm_rejected_order('603466.SH', '5001', '废单回报'))

        pm._reorder_after_cancel.assert_not_called()
        self.assertNotIn('603466', pm.pending_orders)

    def test_rejected_deducts_already_filled_volume(self):
        pm = self._manager()
        pending = self._pending()
        pending['broker_filled_volume'] = 300
        pm.pending_orders['603466'] = pending

        pm._confirm_rejected_order('603466.SH', '5001', '废单回报')

        self.assertEqual(pm._reorder_after_cancel.call_args[0][2]['volume'], 500)

    def test_unknown_order_is_ignored(self):
        pm = self._manager()

        self.assertFalse(pm._confirm_rejected_order('603466.SH', '9999', '废单回报'))
        pm._reorder_after_cancel.assert_not_called()


class TestGridBuyRejectedOrderRetry(GridMeceRegressionBase):
    """网格买入侧的废单兜底（与卖出对称的完整覆盖）。

    生产路径是 execute_grid_trade → _build_grid_order_plan →
    _submit_grid_order_outside_lock，买卖共用；本类确保买入方向在
    「清冷却 / 单次重挂 / 去重 / 库内快照补挂」上行为一致。
    """

    def setUp(self):
        super().setUp()
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_confirm = getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
        self.orig_auto_reorder = getattr(config, 'GRID_PENDING_ORDER_AUTO_REORDER', True)
        self.orig_max_attempts = getattr(config, 'GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS', 1)
        self.orig_buy_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 300)
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        config.GRID_PENDING_ORDER_AUTO_REORDER = True
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = 1
        config.GRID_BUY_COOLDOWN = 300
        config.GRID_USE_COUNTERPARTY_PRICE = True

    def tearDown(self):
        config.GRID_PENDING_ORDER_AUTO_REORDER = self.orig_auto_reorder
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = self.orig_max_attempts
        config.GRID_BUY_COOLDOWN = self.orig_buy_cooldown
        super().tearDown()

    def _register_buy(self, session, order_id='ORDER_BUY_REJ', filled_volume=0):
        self.manager._register_pending_grid_order(
            order_id=order_id,
            session=session,
            signal=self.buy_signal(session, price=9.5),
            side='BUY',
            volume=100,
            expected_price=9.5,
        )
        if filled_volume:
            self.manager.pending_grid_orders[order_id]['filled_volume'] = filled_volume
        self.manager.last_buy_times[session.id] = time.time()

    def test_buy_rejected_clears_cooldown_and_reorders_once(self):
        session = self.make_session()
        self._register_buy(session)

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.assertTrue(self.manager.handle_order_callback(
                FakeOrderInfo('ORDER_BUY_REJ', status=57)))
            self.assertEqual(reorder.call_count, 1)

        self.assertNotIn(session.id, self.manager.last_buy_times)

    def test_buy_duplicate_rejected_callback_does_not_reorder_twice(self):
        session = self.make_session()
        self._register_buy(session)

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_REJ', status=57))
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_REJ', status=57))
            self.assertEqual(reorder.call_count, 1)

    def test_buy_already_retried_is_not_reordered_again(self):
        session = self.make_session()
        self._register_buy(session)
        self.manager.pending_grid_orders['ORDER_BUY_REJ']['reorder_count'] = 1

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_REJ', status=57))
            reorder.assert_not_called()

    def test_buy_partial_fill_rejected_does_not_retry(self):
        session = self.make_session()
        self._register_buy(session, filled_volume=100)

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_REJ', status=57))
            reorder.assert_not_called()

    def test_buy_rejected_when_stop_requested_skips_retry(self):
        session = self.make_session()
        self._register_buy(session)
        self.manager.pending_grid_orders['ORDER_BUY_REJ']['stop_requested'] = True

        with patch.object(self.manager, '_reorder_grid_order_after_cancel',
                          return_value=True) as reorder:
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_REJ', status=57))
            reorder.assert_not_called()

    def test_buy_rejected_after_cancel_uses_db_snapshot(self):
        """已撤(54) 清掉内存 pending 后，57 仍应补一次买入重挂，且只补一次。"""
        session = self.make_session()
        self.db.create_grid_order({
            'order_id': 'ORDER_BUY_DB_REJ',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'canceled',
            'requested_volume': 100,
            'expected_price': 9.5,
            'raw_signal': json.dumps(self.buy_signal(session, price=9.5), ensure_ascii=False),
        })

        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, name=None):
                started.append((target, args))

            def start(self):
                pass

        with patch.object(grid_trading_manager.threading, 'Thread', FakeThread):
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_DB_REJ', status=57))
            self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_DB_REJ', status=57))

        self.assertEqual(len(started), 1)
        snapshot = started[0][1][0]
        self.assertEqual(snapshot['side'], 'BUY')
        self.assertEqual(snapshot['stock_code'], session.stock_code)
        self.assertEqual(self.db.get_grid_order('ORDER_BUY_DB_REJ')['status'], 'rejected')

    def test_reorder_buy_signal_bypasses_buy_cooldown(self):
        """重挂信号必须能穿过 GRID_BUY_COOLDOWN，否则废单重试会被自己拦住。"""
        session = self.make_session(max_investment=10000)
        self.manager.last_buy_times[session.id] = time.time()

        signal = self.buy_signal(session, price=9.5)
        signal['require_price_recheck'] = False
        self.assertIsNone(self.manager._build_grid_order_plan(session, signal))

        signal['is_reorder'] = True
        signal['latest_price'] = 9.5
        plan = self.manager._build_grid_order_plan(session, signal)
        self.assertIsNotNone(plan)
        self.assertEqual(plan['side'], 'BUY')
        self.assertIsNone(plan['order_price'])  # 对手价模式：价格由 executor 取卖三价

    def test_execute_grid_trade_buy_submits_counterparty_price(self):
        """生产买入路径（execute_grid_trade）走对手价，价格交给 executor 限价下单。"""
        session = self.make_session(max_investment=10000)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_BUY_E2E'}

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session, price=9.5)))

        self.executor.buy_stock.assert_called_once()
        kwargs = self.executor.buy_stock.call_args.kwargs
        self.assertIsNone(kwargs['price'])
        self.assertEqual(kwargs['strategy'], config.GRID_STRATEGY_NAME)
        self.assertEqual(kwargs['volume'] % 100, 0)
        self.assertIn('ORDER_BUY_E2E', self.manager.pending_grid_orders)

    def test_execute_grid_trade_buy_rejected_then_retried(self):
        """端到端：买入废单后清冷却并按原信号重挂一次，两次都用对手价。"""
        session = self.make_session(max_investment=10000)
        self.executor.buy_stock.side_effect = [
            {'order_id': 'ORDER_BUY_1'},
            {'order_id': 'ORDER_BUY_2'},
        ]

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session, price=9.5)))
        self.assertIn('ORDER_BUY_1', self.manager.pending_grid_orders)

        self.manager.handle_order_callback(FakeOrderInfo('ORDER_BUY_1', status=57))

        self.assertEqual(self.executor.buy_stock.call_count, 2)
        for call in self.executor.buy_stock.call_args_list:
            self.assertIsNone(call.kwargs['price'])
        self.assertIn('ORDER_BUY_2', self.manager.pending_grid_orders)
        # 重挂委托带 reorder_count=1，再次废单会被 MAX_ATTEMPTS 拦住
        self.assertEqual(
            self.manager.pending_grid_orders['ORDER_BUY_2']['reorder_count'], 1)
        # 重挂被接受后重新武装冷却
        self.assertIn(session.id, self.manager.last_buy_times)


if __name__ == '__main__':
    unittest.main(verbosity=2)
