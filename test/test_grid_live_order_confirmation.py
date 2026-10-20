"""
网格实盘委托成交确认与执行前复核测试

覆盖：
1. 实盘委托成功后不立即落账，等待成交回调确认
2. 部分成交按成交回报增量落账
3. 旧信号、会话错配、价格漂移过大时拒绝执行
4. 成交回调路径不依赖废弃的 GRID_TRADING_ENABLED
"""

import os
import sys
import json
import sqlite3
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
import db_migrate
import settlement_db
from easy_qmt_trader import MyXtQuantTraderCallback, easy_qmt_trader
from grid_database import DatabaseManager
from grid_trading_manager import GridSession, GridTradingManager, PriceTracker
from position_manager import PositionManager
from trading_executor import TradingExecutor, DIRECTION_BUY


class FakeTrade:
    def __init__(self, order_id, stock_code='000001.SZ', volume=100, price=10.0, trade_id='DEAL_1'):
        self.order_id = order_id
        self.stock_code = stock_code
        self.traded_volume = volume
        self.traded_price = price
        self.trade_id = trade_id


class FakeDealInfo:
    m_strInstrumentID = '000001.SZ'
    m_nDirection = DIRECTION_BUY
    m_dPrice = 10.0
    m_nVolume = 100
    m_strTradeID = 'DEAL_OLD_SWITCH'
    m_dComssion = 0.0
    m_strOrderID = 'ORDER_OLD_SWITCH'


class FakeOrderInfo:
    def __init__(self, order_id, stock_code='000001.SZ', status=54):
        self.m_strOrderSysID = order_id
        self.m_strInstrumentID = stock_code
        self.m_nOrderStatus = status
        self.order_sysid = order_id
        self.order_id = order_id
        self.stock_code = stock_code
        self.order_status = status


class FakeBrokerOrder:
    def __init__(self, order_id, status=54, stock_code='000001.SZ', traded_volume=0, traded_price=0):
        self.order_id = order_id
        self.stock_code = stock_code
        self.order_status = status
        self.traded_volume = traded_volume
        self.traded_price = traded_price


class TestGridLiveOrderConfirmation(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.position_manager.signal_lock = __import__('threading').Lock()
        self.position_manager.latest_signals = {}
        self.position_manager._increment_data_version = Mock()
        self.position_manager.data_manager = Mock()
        self.position_manager.data_manager.get_latest_data.return_value = {'lastPrice': 10.0}
        # 大部分 BUY 测试不需要持仓快照，Mock 返回 None 表示没有持仓
        # → _build_grid_order_plan 中 BUY 无持仓时走金额回退路径
        self.position_manager.get_position.return_value = None
        self.executor = Mock(spec=TradingExecutor)
        self.executor._save_trade_record.return_value = True
        self.manager = GridTradingManager(self.db, self.position_manager, self.executor)

        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_confirm = getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
        self.orig_max_age = getattr(config, 'GRID_SIGNAL_MAX_AGE_SECONDS', 60)
        self.orig_drift = getattr(config, 'GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO', 0.01)
        self.orig_reconcile_interval = getattr(config, 'GRID_ORDER_RECONCILE_INTERVAL', 15)
        self.orig_reconcile_stale = getattr(config, 'GRID_ORDER_RECONCILE_STALE_SECONDS', 5)
        self.orig_grid_pending_auto_cancel = getattr(config, 'ENABLE_GRID_PENDING_ORDER_AUTO_CANCEL', True)
        self.orig_grid_pending_timeout = getattr(config, 'GRID_PENDING_ORDER_TIMEOUT_SECONDS', 90)
        self.orig_grid_pending_reorder = getattr(config, 'GRID_PENDING_ORDER_AUTO_REORDER', True)
        self.orig_grid_pending_reorder_max = getattr(config, 'GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS', 1)
        self.orig_grid_guard = getattr(config, 'GRID_ENABLE_PRICE_LIMIT_GUARD', True)

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = self.orig_confirm
        config.GRID_SIGNAL_MAX_AGE_SECONDS = self.orig_max_age
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = self.orig_drift
        config.GRID_ORDER_RECONCILE_INTERVAL = self.orig_reconcile_interval
        config.GRID_ORDER_RECONCILE_STALE_SECONDS = self.orig_reconcile_stale
        config.ENABLE_GRID_PENDING_ORDER_AUTO_CANCEL = self.orig_grid_pending_auto_cancel
        config.GRID_PENDING_ORDER_TIMEOUT_SECONDS = self.orig_grid_pending_timeout
        config.GRID_PENDING_ORDER_AUTO_REORDER = self.orig_grid_pending_reorder
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = self.orig_grid_pending_reorder_max
        config.GRID_ENABLE_PRICE_LIMIT_GUARD = self.orig_grid_guard
        self.db.close()

    def _make_session(self, stock_code='000001.SZ', current_investment=0.0):
        session = GridSession(
            id=None,
            stock_code=stock_code,
            status='active',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            current_investment=current_investment,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(days=7),
        )
        data = asdict(session)
        data['start_time'] = session.start_time.isoformat()
        data['end_time'] = session.end_time.isoformat()
        session.id = self.db.create_grid_session(data)
        self.manager.sessions[self.manager._normalize_code(stock_code)] = session
        self.manager.trackers[session.id] = PriceTracker(session_id=session.id, last_price=10.0)
        return session

    def _buy_signal(self, session, trigger_price=10.0):
        return {
            'stock_code': session.stock_code,
            'strategy': config.GRID_STRATEGY_NAME,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': trigger_price,
            'session_id': session.id,
            'timestamp': datetime.now().isoformat(),
            'signal_source': 'grid_tracker',
            'require_price_recheck': True,
            'valley_price': 9.9,
            'callback_ratio': 0.005,
        }

    def test_live_order_waits_for_deal_and_partial_fills_incrementally(self):
        """部分成交只更新填充量不落账，全部成交后一次性聚合落账"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_PARTIAL'}

        result = self.manager.execute_grid_trade(signal)

        self.assertTrue(result)
        self.assertIn('ORDER_PARTIAL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_PARTIAL')
        self.assertIsNotNone(db_order)
        self.assertEqual(db_order['status'], 'submitted')
        self.assertEqual(session.buy_count, 0)
        self.assertEqual(len(self.db.get_grid_trades(session.id)), 0)
        self.executor._save_trade_record.assert_not_called()

        # 第1笔部分成交：只累积填充量，不落账
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('ORDER_PARTIAL', volume=100, price=10.0, trade_id='DEAL_1')
        ))
        # 部分成交阶段不应写 trade_records 或 grid_trades
        self.executor._save_trade_record.assert_not_called()
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 0)
        self.assertIn('ORDER_PARTIAL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_PARTIAL')
        self.assertEqual(db_order['status'], 'partial_filled')
        self.assertEqual(db_order['filled_volume'], 100)
        # session 统计在部分成交阶段不变
        self.assertEqual(session.buy_count, 0)
        self.assertEqual(session.total_buy_volume, 0)
        self.assertAlmostEqual(session.current_investment, 0.0, places=2)

        # 第2笔部分成交：委托量200已满，全部成交 → 一次性聚合落账
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('ORDER_PARTIAL', volume=100, price=10.1, trade_id='DEAL_2')
        ))
        self.assertNotIn('ORDER_PARTIAL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_PARTIAL')
        self.assertEqual(db_order['status'], 'filled')
        self.assertEqual(db_order['filled_volume'], 200)
        # 统计只 +1（聚合），总量用加权均价
        self.assertEqual(session.buy_count, 1)
        self.assertEqual(session.total_buy_volume, 200)
        self.assertAlmostEqual(session.current_investment, 200 * 10.05, places=2)  # 加权均价 (100*10.0+100*10.1)/200=10.05
        # grid_trades 只有一条聚合记录
        trades = self.db.get_grid_trades(session.id, limit=10)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]['trigger_price'], 10.05, places=4)
        self.assertEqual(trades[0]['volume'], 200)
        # trade_records 只写一条
        self.assertEqual(self.executor._save_trade_record.call_count, 1)
        record = self.executor._save_trade_record.call_args_list[0].kwargs
        self.assertEqual(record['trade_type'], 'BUY')
        self.assertEqual(record['trade_id'], 'ORDER_PARTIAL')
        self.assertEqual(record['volume'], 200)
        self.assertAlmostEqual(record['price'], 10.05, places=4)
        self.assertEqual(record['strategy'], config.GRID_STRATEGY_NAME)
        self.assertEqual(record['deal_meta']['fills'], 2)
        self.assertEqual(record['deal_meta']['fill_ids'], 'DEAL_1,DEAL_2')

    def _start_fill_audit_order(self):
        """仅使用内存库和执行器落账方法，隔离所有真实交易接口。"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute(db_migrate.DDL_TRADE_RECORDS_SIM.replace(
            'trade_records_sim', 'trade_records'))
        for _, ddl in db_migrate.TRADE_RECORDS_INDEXES:
            conn.execute(ddl)
        schema_patch = patch.dict(settlement_db._SCHEMA_CACHE, {}, clear=True)
        schema_patch.start()
        self.addCleanup(schema_patch.stop)
        account_patch = patch.object(settlement_db, 'get_account_id',
                                     return_value='TEST_AUDIT')
        account_patch.start()
        self.addCleanup(account_patch.stop)
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.conn = conn
        executor.data_manager = Mock()
        executor.data_manager.get_stock_name.return_value = '测试股票'
        self.executor._save_trade_record.side_effect = executor._save_trade_record
        session = self._make_session()
        signal = self._buy_signal(session)
        signal['signal_type'] = 'SELL'
        self.manager.pending_grid_orders['135266305'] = {
            'session_id': session.id, 'stock_code': session.stock_code,
            'side': 'SELL', 'signal': signal, 'requested_volume': 1500,
            'filled_volume': 0, 'filled_amount': 0.0,
            'confirmed_trade_ids': set(),
        }
        self.db.create_grid_order({
            'order_id': '135266305', 'session_id': session.id,
            'stock_code': session.stock_code, 'side': 'SELL',
            'status': 'submitted', 'requested_volume': 1500, 'expected_price': 15.28,
        })
        return conn, session

    def _assert_fill_audit_row(self, conn, session, fill_ids):
        rows = conn.execute('SELECT * FROM trade_records').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.db.get_grid_trades(session.id)), 1)
        self.assertEqual(session.sell_count, 1)
        row = rows[0]
        self.assertEqual(row['trade_id'], '135266305')
        self.assertEqual(row['order_id'], '135266305')
        self.assertEqual(row['volume'], 1500)
        with self.subTest('分笔数'):
            self.assertEqual(row['fills'], len(fill_ids))
        with self.subTest('成交编号'):
            self.assertEqual(row['fill_ids'], ','.join(sorted(fill_ids)))
        return row

    def test_aggregate_fill_audit_persists_all_ids_and_weighted_price(self):
        """复现两笔卖出聚合落库，检查审计字段和加权均价。"""
        conn, session = self._start_fill_audit_order()
        for volume, price, trade_id in ((400, 15.2, 'DEAL_2'), (1100, 15.3, 'DEAL_1')):
            self.assertTrue(self.manager.handle_deal_callback(
                FakeTrade('135266305', volume=volume, price=price, trade_id=trade_id)))
        row = self._assert_fill_audit_row(conn, session, ['DEAL_2', 'DEAL_1'])
        self.assertAlmostEqual(row['amount'], 400 * 15.2 + 1100 * 15.3)
        self.assertAlmostEqual(row['price'], row['amount'] / 1500)

    def test_single_fill_audit_uses_real_deal_id(self):
        """单笔全成仍使用真实成交编号，不能退回委托号。"""
        conn, session = self._start_fill_audit_order()
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('135266305', volume=1500, price=15.28, trade_id='DEAL_SINGLE')))
        row = self._assert_fill_audit_row(conn, session, ['DEAL_SINGLE'])
        self.assertAlmostEqual(row['amount'], 22920.0)

    def test_duplicate_partial_fill_audit_is_not_counted_twice(self):
        """重复回报不增加分笔数、金额或流水条数。"""
        conn, session = self._start_fill_audit_order()
        first = FakeTrade('135266305', volume=400, price=15.2, trade_id='DEAL_2')
        last = FakeTrade('135266305', volume=1100, price=15.3, trade_id='DEAL_1')
        self.assertTrue(self.manager.handle_deal_callback(first))
        self.assertFalse(self.manager.handle_deal_callback(first))
        self.assertTrue(self.manager.handle_deal_callback(last))
        self.assertFalse(self.manager.handle_deal_callback(last))
        row = self._assert_fill_audit_row(conn, session, ['DEAL_2', 'DEAL_1'])
        self.assertAlmostEqual(row['amount'], 22910.0)

    def test_failed_aggregate_retry_preserves_fill_audit(self):
        """落账失败后重试，已确认分笔保留，失败的末笔不重复计数。"""
        conn, session = self._start_fill_audit_order()
        first = FakeTrade('135266305', volume=400, price=15.2, trade_id='DEAL_2')
        last = FakeTrade('135266305', volume=1100, price=15.3, trade_id='DEAL_1')
        self.assertTrue(self.manager.handle_deal_callback(first))
        with patch.object(self.db, 'record_grid_trade_and_update_session',
                          side_effect=RuntimeError('模拟落账失败')):
            self.assertFalse(self.manager.handle_deal_callback(last))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM trade_records').fetchone()[0], 0)
        self.assertEqual(self.manager.pending_grid_orders['135266305']['filled_volume'], 400)
        self.assertTrue(self.manager.handle_deal_callback(last))
        row = self._assert_fill_audit_row(conn, session, ['DEAL_2', 'DEAL_1'])
        self.assertAlmostEqual(row['amount'], 22910.0)

    def test_order_cancel_reject_cleans_pending_and_persists_status(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_CANCEL'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.assertIn('ORDER_CANCEL', self.manager.pending_grid_orders)

        handled = self.manager.handle_order_callback(FakeOrderInfo('ORDER_CANCEL', status=54))

        self.assertTrue(handled)
        self.assertNotIn('ORDER_CANCEL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_CANCEL')
        self.assertEqual(db_order['status'], 'canceled')
        self.assertEqual(session.buy_count, 0)
        self.assertEqual(len(self.db.get_grid_trades(session.id)), 0)

    def test_open_grid_orders_recovered_on_manager_restart(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_RESTART'}

        self.assertTrue(self.manager.execute_grid_trade(signal))

        restarted = GridTradingManager(self.db, self.position_manager, self.executor)
        self.assertIn('ORDER_RESTART', restarted.pending_grid_orders)
        self.assertTrue(restarted.handle_deal_callback(
            FakeTrade('ORDER_RESTART', volume=200, price=10.0, trade_id='DEAL_RESTART')
        ))
        self.assertNotIn('ORDER_RESTART', restarted.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_RESTART')['status'], 'filled')

    def test_startup_reconcile_replays_broker_trade_and_closes_canceled_order(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)

        self.db.create_grid_order({
            'order_id': 'ORDER_RECON_FILLED',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'submitted',
            'requested_volume': 200,
            'expected_price': 10.0,
            'reserved_price': 10.2,
            'submitted_at': datetime.now().isoformat(),
            'raw_signal': __import__('json').dumps(signal),
        })
        self.db.create_grid_order({
            'order_id': 'ORDER_RECON_CANCELED',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'submitted',
            'requested_volume': 100,
            'expected_price': 10.0,
            'reserved_price': 10.2,
            'submitted_at': datetime.now().isoformat(),
            'raw_signal': __import__('json').dumps(signal),
        })
        self.executor.query_stock_trades.return_value = [
            FakeTrade('ORDER_RECON_FILLED', volume=200, price=10.1, trade_id='DEAL_RECON_1')
        ]
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_RECON_FILLED', status=56, traded_volume=200, traded_price=10.1),
            FakeBrokerOrder('ORDER_RECON_CANCELED', status=54),
        ]

        restarted = GridTradingManager(self.db, self.position_manager, self.executor)

        self.assertNotIn('ORDER_RECON_FILLED', restarted.pending_grid_orders)
        self.assertNotIn('ORDER_RECON_CANCELED', restarted.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_RECON_FILLED')['status'], 'filled')
        self.assertEqual(self.db.get_grid_order('ORDER_RECON_CANCELED')['status'], 'canceled')
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 1)

    def test_runtime_reconcile_replays_filled_pending_order_without_restart(self):
        """成交推送漏掉时，运行期对账应补记已成委托，不等重启"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_RUNTIME_RECON'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.assertIn('ORDER_RUNTIME_RECON', self.manager.pending_grid_orders)
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 0)

        self.executor.query_stock_trades.return_value = []
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_RUNTIME_RECON', status=56, traded_volume=200, traded_price=10.1)
        ]

        result = self.manager.reconcile_pending_grid_orders_if_due(
            force=True,
            reason='运行期对账测试'
        )

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['replayed'], 1)
        self.assertNotIn('ORDER_RUNTIME_RECON', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_RUNTIME_RECON')['status'], 'filled')
        trades = self.db.get_grid_trades(session.id, limit=10)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['trade_id'], 'ORDER_RUNTIME_RECON')
        self.assertAlmostEqual(trades[0]['trigger_price'], 10.1, places=4)
        self.assertEqual(session.buy_count, 1)
        self.executor._save_trade_record.assert_called_once()

    def test_timed_out_pending_order_requests_cancel_without_immediate_reorder(self):
        """网格pending超时后只提交撤单请求，等待54=已撤后再重挂"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        config.ENABLE_GRID_PENDING_ORDER_AUTO_CANCEL = True
        config.GRID_PENDING_ORDER_TIMEOUT_SECONDS = 90
        config.GRID_PENDING_ORDER_AUTO_REORDER = True
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = 1
        config.GRID_ENABLE_PRICE_LIMIT_GUARD = False
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_TIMEOUT'}
        self.executor.cancel_order.return_value = True

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.manager.pending_grid_orders['ORDER_TIMEOUT']['created_at'] = (
            datetime.now() - timedelta(seconds=91)
        ).isoformat()
        self.executor.buy_stock.reset_mock()
        self.executor.query_stock_trades.return_value = []
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_TIMEOUT', status=50)
        ]

        result = self.manager.reconcile_pending_grid_orders_if_due(
            force=True,
            reason='超时撤单测试'
        )

        self.assertEqual(result['timeout_cancel_requested'], 1)
        self.executor.cancel_order.assert_called_once_with('ORDER_TIMEOUT')
        self.executor.buy_stock.assert_not_called()
        pending = self.manager.pending_grid_orders['ORDER_TIMEOUT']
        self.assertEqual(pending['status'], 'cancel_requested')
        self.assertTrue(pending['reorder_after_cancel'])
        db_order = self.db.get_grid_order('ORDER_TIMEOUT')
        self.assertEqual(db_order['status'], 'cancel_requested')
        self.assertEqual(db_order['reorder_after_cancel'], 1)

    def test_cancel_confirm_reorders_once_after_timeout_cancel(self):
        """超时撤单收到54确认后，按最新行情自动重挂一次"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        config.ENABLE_GRID_PENDING_ORDER_AUTO_CANCEL = True
        config.GRID_PENDING_ORDER_TIMEOUT_SECONDS = 90
        config.GRID_PENDING_ORDER_AUTO_REORDER = True
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = 1
        config.GRID_ENABLE_PRICE_LIMIT_GUARD = False
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_TIMEOUT'}
        self.executor.cancel_order.return_value = True

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.manager.pending_grid_orders['ORDER_TIMEOUT']['created_at'] = (
            datetime.now() - timedelta(seconds=91)
        ).isoformat()
        self.executor.query_stock_trades.return_value = []
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_TIMEOUT', status=50)
        ]
        self.manager.reconcile_pending_grid_orders_if_due(force=True, reason='超时撤单测试')

        self.executor.buy_stock.reset_mock()
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_REORDERED'}
        self.position_manager.data_manager.get_latest_data.return_value = {'lastPrice': 10.0}

        self.assertTrue(self.manager.handle_order_callback(FakeOrderInfo('ORDER_TIMEOUT', status=54)))

        self.assertNotIn('ORDER_TIMEOUT', self.manager.pending_grid_orders)
        self.assertIn('ORDER_REORDERED', self.manager.pending_grid_orders)
        self.executor.buy_stock.assert_called_once()
        old_order = self.db.get_grid_order('ORDER_TIMEOUT')
        new_order = self.db.get_grid_order('ORDER_REORDERED')
        self.assertEqual(old_order['status'], 'canceled')
        self.assertEqual(new_order['parent_order_id'], 'ORDER_TIMEOUT')
        self.assertEqual(new_order['reorder_count'], 1)
        tracker = self.manager.trackers[session.id]
        self.assertTrue(tracker.waiting_callback)
        self.assertEqual(tracker.direction, 'falling')

    def test_reorder_max_attempts_blocks_second_reorder(self):
        """达到最大重挂次数后，超时撤单完成不再自动重挂"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        config.ENABLE_GRID_PENDING_ORDER_AUTO_CANCEL = True
        config.GRID_PENDING_ORDER_TIMEOUT_SECONDS = 90
        config.GRID_PENDING_ORDER_AUTO_REORDER = True
        config.GRID_PENDING_ORDER_REORDER_MAX_ATTEMPTS = 1
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_MAX'}
        self.executor.cancel_order.return_value = True

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.manager.pending_grid_orders['ORDER_MAX'].update({
            'created_at': (datetime.now() - timedelta(seconds=91)).isoformat(),
            'reorder_count': 1,
        })
        self.executor.query_stock_trades.return_value = []
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_MAX', status=50)
        ]
        self.manager.reconcile_pending_grid_orders_if_due(force=True, reason='最大重挂次数测试')

        self.assertFalse(self.manager.pending_grid_orders['ORDER_MAX']['reorder_after_cancel'])
        self.executor.buy_stock.reset_mock()

        self.assertTrue(self.manager.handle_order_callback(FakeOrderInfo('ORDER_MAX', status=54)))

        self.executor.buy_stock.assert_not_called()
        self.assertNotIn('ORDER_MAX', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_MAX')['status'], 'canceled')

    def test_partial_pending_timeout_does_not_auto_cancel_or_reorder(self):
        """已有部分成交的pending不进入V1自动撤单重挂流程"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        config.ENABLE_GRID_PENDING_ORDER_AUTO_CANCEL = True
        config.GRID_PENDING_ORDER_TIMEOUT_SECONDS = 90
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_PARTIAL_TIMEOUT'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.manager.pending_grid_orders['ORDER_PARTIAL_TIMEOUT'].update({
            'created_at': (datetime.now() - timedelta(seconds=91)).isoformat(),
            'filled_volume': 100,
            'filled_amount': 1000.0,
        })
        self.db.update_grid_order('ORDER_PARTIAL_TIMEOUT', {
            'status': 'partial_filled',
            'filled_volume': 100,
            'filled_amount': 1000.0,
        })
        self.executor.query_stock_trades.return_value = []
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_PARTIAL_TIMEOUT', status=55, traded_volume=100, traded_price=10.0)
        ]

        result = self.manager.reconcile_pending_grid_orders_if_due(
            force=True,
            reason='部分成交超时测试'
        )

        self.assertEqual(result['timeout_skipped_partial'], 1)
        self.executor.cancel_order.assert_not_called()
        self.assertIn('ORDER_PARTIAL_TIMEOUT', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_PARTIAL_TIMEOUT')['status'], 'partial_filled')

    def test_broker_partial_timeout_does_not_auto_cancel_or_reorder(self):
        """券商侧已部分成交时，即使本地尚未补记，也不自动撤单重挂"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        config.ENABLE_GRID_PENDING_ORDER_AUTO_CANCEL = True
        config.GRID_PENDING_ORDER_TIMEOUT_SECONDS = 90
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_BROKER_PARTIAL'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.manager.pending_grid_orders['ORDER_BROKER_PARTIAL']['created_at'] = (
            datetime.now() - timedelta(seconds=91)
        ).isoformat()
        self.executor.query_stock_trades.return_value = []
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_BROKER_PARTIAL', status=55, traded_volume=100, traded_price=10.0)
        ]

        result = self.manager.reconcile_pending_grid_orders_if_due(
            force=True,
            reason='券商部分成交超时测试'
        )

        self.assertEqual(result['timeout_skipped_partial'], 1)
        self.executor.cancel_order.assert_not_called()
        self.assertIn('ORDER_BROKER_PARTIAL', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_BROKER_PARTIAL')['status'], 'submitted')

    def test_buy_signal_suppressed_while_same_side_pending_exists(self):
        """同一会话已有 BUY pending 时，不再生成或执行新的 BUY 网格单"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        self.position_manager.get_position.return_value = {
            'stock_code': session.stock_code,
            'volume': 1000,
            'available': 1000,
            'cost_price': 10.0,
            'current_price': 9.46,
            'market_value': 9460.0,
        }
        signal = self._buy_signal(session)
        self.manager.pending_grid_orders['ORDER_PENDING_BUY'] = {
            'order_id': 'ORDER_PENDING_BUY',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'signal': signal,
            'requested_volume': 200,
            'expected_price': 10.0,
            'reserved_price': 10.2,
            'filled_volume': 0,
            'filled_amount': 0.0,
            'confirmed_trade_ids': set(),
            'created_at': datetime.now().isoformat(),
        }
        tracker = self.manager.trackers[session.id]
        tracker.waiting_callback = True
        tracker.direction = 'falling'
        tracker.crossed_level = 9.5
        tracker.valley_price = 9.4

        generated = self.manager.check_grid_signals(session.stock_code, 9.46)

        self.assertIsNone(generated)
        self.assertEqual(session.status, 'active')
        self.assertIn('ORDER_PENDING_BUY', self.manager.pending_grid_orders)

        direct_result = self.manager.execute_grid_trade(signal)
        self.assertFalse(direct_result)
        self.executor.buy_stock.assert_not_called()

    def test_duplicate_deal_ignored_by_db_idempotency(self):
        """重复成交回报（同一 trade_id）在部分成交阶段被忽略"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_DUP'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        # 第一笔回调：200股全部成交 → 一次性聚合落账
        trade = FakeTrade('ORDER_DUP', volume=200, price=10.0, trade_id='DEAL_DUP')
        self.assertTrue(self.manager.handle_deal_callback(trade))
        # 同一 trade_id 重复回调 → 忽略
        self.assertFalse(self.manager.handle_deal_callback(trade))
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 1)

    def test_stale_signal_rejected_before_order(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_SIGNAL_MAX_AGE_SECONDS = 60
        session = self._make_session()
        signal = self._buy_signal(session)
        signal['timestamp'] = (datetime.now() - timedelta(seconds=120)).isoformat()

        result = self.manager.execute_grid_trade(signal)

        self.assertFalse(result)
        self.executor.buy_stock.assert_not_called()

    def test_session_mismatch_rejected_before_order(self):
        config.ENABLE_SIMULATION_MODE = False
        session = self._make_session()
        signal = self._buy_signal(session)
        signal['session_id'] = session.id + 999

        result = self.manager.execute_grid_trade(signal)

        self.assertFalse(result)
        self.executor.buy_stock.assert_not_called()

    def test_price_drift_rejected_before_order(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = 0.01
        session = self._make_session()
        signal = self._buy_signal(session, trigger_price=10.0)
        self.position_manager.data_manager.get_latest_data.return_value = {'lastPrice': 10.25}

        result = self.manager.execute_grid_trade(signal)

        self.assertFalse(result)
        self.executor.buy_stock.assert_not_called()

    def test_trading_executor_deal_callback_without_old_grid_switch(self):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.position_manager = Mock()
        executor.position_manager.grid_manager = Mock()
        executor.order_cache = {}
        executor.callbacks = {}
        executor._save_trade_record = Mock()
        executor._update_position_after_trade = Mock()

        old_present = hasattr(config, 'GRID_TRADING_ENABLED')
        old_value = getattr(config, 'GRID_TRADING_ENABLED', None)
        if old_present:
            delattr(config, 'GRID_TRADING_ENABLED')
        try:
            self.assertFalse(hasattr(config, 'GRID_TRADING_ENABLED'))
            with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
                 patch.object(config, 'ENABLE_GRID_TRADING', True):
                executor._on_deal_callback(FakeDealInfo())
        finally:
            if old_present:
                setattr(config, 'GRID_TRADING_ENABLED', old_value)

        executor.position_manager.grid_manager.handle_deal_callback.assert_called_once()

    def test_trading_executor_deal_callback_delegates_grid_recording(self):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.position_manager = Mock()
        executor.position_manager.grid_manager = Mock()
        executor.position_manager.grid_manager.pending_grid_orders = {'ORDER_OLD_SWITCH': {}}
        executor.position_manager.grid_manager.handle_deal_callback.return_value = True
        executor.order_cache = {
            'ORDER_OLD_SWITCH': {
                'strategy': config.GRID_STRATEGY_NAME,
                'trade_type': 'BUY'
            }
        }
        executor.callbacks = {}
        executor._save_trade_record = Mock()
        executor._update_position_after_trade = Mock()

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(config, 'ENABLE_GRID_TRADING', True), \
             patch.object(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True):
            executor._on_deal_callback(FakeDealInfo())

        executor.position_manager.grid_manager.handle_deal_callback.assert_called_once()
        executor._save_trade_record.assert_not_called()
        executor._update_position_after_trade.assert_called_once()

    def test_trading_executor_order_callback_notifies_grid_manager(self):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.position_manager = Mock()
        executor.position_manager.grid_manager = Mock()
        executor.order_cache = {}
        executor.callbacks = {}

        with patch.object(config, 'ENABLE_GRID_TRADING', True):
            executor._on_order_callback(FakeOrderInfo('ORDER_NOTIFY', status=54))

        executor.position_manager.grid_manager.handle_order_callback.assert_called_once()

    def test_easy_qmt_order_callback_dispatches_external_callbacks(self):
        callback = MyXtQuantTraderCallback({})
        trader = easy_qmt_trader.__new__(easy_qmt_trader)
        trader._callback = callback
        handler = Mock()
        order = FakeOrderInfo('ORDER_QMT_NOTIFY', status=54)

        trader.register_order_callback(handler)
        callback.on_stock_order(order)

        handler.assert_called_once_with(order)

    def test_position_manager_order_callback_notifies_grid_manager(self):
        position_manager = PositionManager.__new__(PositionManager)
        position_manager.grid_manager = Mock()
        order = FakeOrderInfo('ORDER_PM_NOTIFY', status=57)

        with patch.object(config, 'ENABLE_GRID_TRADING', True):
            position_manager._on_order_callback(order)

        position_manager.grid_manager.handle_order_callback.assert_called_once_with(
            order, defer_reorder=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
