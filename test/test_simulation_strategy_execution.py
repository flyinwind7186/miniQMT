#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L3 — 策略层模拟执行分支测试

覆盖 strategy.py 中四条模拟交易执行路径：
    - execute_add_position_strategy      补仓
    - _execute_stop_loss_signal          止损（全仓）
    - _execute_take_profit_half_signal   首次止盈（半仓）
    - _execute_take_profit_full_signal   动态止盈（清仓）

含两个缺陷的回归锚点：
    Bug-A: 补仓模拟分支用 volume=/price= 调用 simulate_buy_position，
           而签名是 buy_volume=/buy_price= → TypeError（模拟补仓完全失效）
    Bug-B: 冷却期 last_trade_time 只在实盘分支写入，模拟分支不写
           → 模拟补仓不受 2 分钟冷却约束，与实盘行为不一致
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("MINIQMT_DISABLE_DOTENV", "1")

import config
from test.test_base import TestBase
from strategy import TradingStrategy


class StrategySimulationTestBase(TestBase):
    """L3 公共夹具：真实 TradingStrategy + Mock 掉 position_manager"""

    def setUp(self):
        super().setUp()

        self._orig_sim_balance = config.SIMULATION_BALANCE
        self._orig_max_position_value = config.MAX_POSITION_VALUE
        config.SIMULATION_BALANCE = 100000.0

        # 用 patch 让构造函数拿到 Mock 依赖，避免真实 PositionManager 的
        # 后台线程与 xtdata 连接（L1 已覆盖真实链路，L3 只关心策略层调度）
        self.mock_pm = MagicMock()
        self.mock_pm._has_tracked_pending_order.return_value = False
        self.mock_pm._has_pending_orders.return_value = False
        self.mock_pm.get_position.return_value = {
            'volume': 1000, 'available': 1000, 'cost_price': 10.0,
            'current_price': 10.0, 'market_value': 10000.0,
            'profit_triggered': False,
        }
        self.mock_pm.simulate_buy_position.return_value = True
        self.mock_pm.simulate_sell_position.return_value = True

        with patch('strategy.get_data_manager', return_value=MagicMock()), \
             patch('strategy.get_indicator_calculator', return_value=MagicMock()), \
             patch('strategy.get_position_manager', return_value=self.mock_pm), \
             patch('strategy.get_trading_executor', return_value=MagicMock()):
            self.strategy = TradingStrategy()

    def tearDown(self):
        config.SIMULATION_BALANCE = self._orig_sim_balance
        config.MAX_POSITION_VALUE = self._orig_max_position_value
        super().tearDown()


class TestAddPositionSimulation(StrategySimulationTestBase):
    """L3-01 ~ L3-06: 补仓模拟分支"""

    def _add_info(self, add_amount=3500.0, current_price=12.34):
        return {'add_amount': add_amount, 'current_price': current_price}

    def test_L3_01_add_position_calls_with_correct_kwargs(self):
        """L3-01 [Bug-A 回归] 补仓须以 buy_volume/buy_price 关键字调用

        修复前：以 volume=/price= 调用 → TypeError 被 except 吞掉 → 返回 False
        修复后：正确调用并返回 True
        """
        ok = self.strategy.execute_add_position_strategy(
            '000001.SZ', self._add_info(add_amount=3500.0, current_price=10.0))

        self.assertTrue(ok, "补仓应执行成功（失败说明 Bug-A 未修复）")
        self.mock_pm.simulate_buy_position.assert_called_once()

        _, kwargs = self.mock_pm.simulate_buy_position.call_args
        self.assertIn('buy_volume', kwargs,
                      "必须用 buy_volume= 调用，否则 TypeError")
        self.assertIn('buy_price', kwargs,
                      "必须用 buy_price= 调用，否则 TypeError")
        self.assertNotIn('volume', kwargs, "不应使用错误的 volume= 形参名")
        self.assertNotIn('price', kwargs, "不应使用错误的 price= 形参名")
        self.assertEqual(kwargs['buy_volume'], 300)   # 3500//10.0/100 → 3 → 300
        self.assertAlmostEqual(kwargs['buy_price'], 10.0, places=2)

    def test_L3_02_volume_floors_to_hundred(self):
        """L3-02 补仓金额转股数向下取整到 100 的倍数"""
        ok = self.strategy.execute_add_position_strategy(
            '000001.SZ', self._add_info(add_amount=3500.0, current_price=12.34))

        self.assertTrue(ok)
        _, kwargs = self.mock_pm.simulate_buy_position.call_args
        expected = int(3500.0 // 12.34 / 100) * 100     # 283 → 200
        self.assertEqual(kwargs['buy_volume'], expected)

    def test_L3_03_volume_below_hundred_skipped(self):
        """L3-03 计算股数 <100 时跳过，不下单"""
        ok = self.strategy.execute_add_position_strategy(
            '000001.SZ', self._add_info(add_amount=500.0, current_price=12.34))

        self.assertFalse(ok, "股数不足 100 应跳过")
        self.mock_pm.simulate_buy_position.assert_not_called()

    def test_L3_04_exceeds_max_position_value_rejected(self):
        """L3-04 持仓市值超限时拒绝补仓"""
        config.MAX_POSITION_VALUE = 12000.0
        # 现有市值 10000 + 补仓 5000 = 15000 > 12000
        ok = self.strategy.execute_add_position_strategy(
            '000001.SZ', self._add_info(add_amount=5000.0, current_price=10.0))

        self.assertFalse(ok, "超过 MAX_POSITION_VALUE 应拒绝")
        self.mock_pm.simulate_buy_position.assert_not_called()

    def test_L3_05_cooldown_recorded_in_simulation(self):
        """L3-05 [Bug-B 回归] 模拟分支须写入冷却期，与实盘行为一致

        修复前：模拟分支 if success: return True 直接返回，不写 last_trade_time
                → 可无限次连续补仓
        修复后：连续两次补仓，第二次被 120 秒冷却拦截
        """
        info = self._add_info(add_amount=3500.0, current_price=10.0)

        first = self.strategy.execute_add_position_strategy('000001.SZ', info)
        self.assertTrue(first, "首次补仓应成功")
        self.assertIn('add_position_000001.SZ', self.strategy.last_trade_time,
                      "模拟分支须记录冷却时间（Bug-B）")

        second = self.strategy.execute_add_position_strategy('000001.SZ', info)
        self.assertFalse(second, "120 秒内二次补仓应被冷却期拦截")
        self.assertEqual(self.mock_pm.simulate_buy_position.call_count, 1,
                         "冷却期内不应重复下单")

    def test_L3_05b_cooldown_expires_after_120s(self):
        """L3-05b 冷却期满 120 秒后允许再次补仓"""
        info = self._add_info(add_amount=3500.0, current_price=10.0)
        self.strategy.execute_add_position_strategy('000001.SZ', info)

        # 把冷却起点回拨 121 秒
        self.strategy.last_trade_time['add_position_000001.SZ'] = \
            datetime.now() - timedelta(seconds=121)

        ok = self.strategy.execute_add_position_strategy('000001.SZ', info)
        self.assertTrue(ok, "冷却期满后应允许补仓")
        self.assertEqual(self.mock_pm.simulate_buy_position.call_count, 2)

    def test_L3_06_tracked_pending_order_blocks(self):
        """L3-06 已有跟踪中的委托时直接拦截"""
        self.mock_pm._has_tracked_pending_order.return_value = True

        ok = self.strategy.execute_add_position_strategy(
            '000001.SZ', self._add_info())

        self.assertFalse(ok)
        self.mock_pm.simulate_buy_position.assert_not_called()
        self.mock_pm.get_position.assert_not_called()


class TestSellSignalSimulation(StrategySimulationTestBase):
    """L3-07 ~ L3-10: 三条卖出信号的模拟分支"""

    def test_L3_07_stop_loss_sells_full(self):
        """L3-07 止损以 sell_type='full' 清仓"""
        ok = self.strategy._execute_stop_loss_signal('000001.SZ', {
            'volume': 1000, 'current_price': 9.0, 'cost_price': 10.0,
        })

        self.assertTrue(ok)
        _, kwargs = self.mock_pm.simulate_sell_position.call_args
        self.assertEqual(kwargs['sell_type'], 'full', "止损应全仓卖出")
        self.assertEqual(kwargs['sell_volume'], 1000)
        self.assertAlmostEqual(kwargs['sell_price'], 9.0, places=2)

    def test_L3_08_take_profit_half_volume_calc(self):
        """L3-08 半仓止盈卖出数量 = int(base*ratio/100)*100，base 取执行时刻可用持仓

        sell_ratio 的真实取值是 config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE
        = 0.6（小数，非百分数），信号由 position_manager.py:2546 产出。
        可卖 1000 股 × 0.6 → int(1000*0.6/100)*100 = 600 股。
        """
        self.mock_pm.get_position.return_value = {
            'volume': 1000, 'available': 1000, 'cost_price': 10.0,
            'current_price': 12.0, 'profit_triggered': True,
        }
        ok = self.strategy._execute_take_profit_half_signal('000001.SZ', {
            'volume': 1000, 'current_price': 12.0,
            'sell_ratio': config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE,
        })

        self.assertTrue(ok)
        _, kwargs = self.mock_pm.simulate_sell_position.call_args
        self.assertEqual(kwargs['sell_volume'], 600,
                         "1000 股按 0.6 比例首次止盈应卖 600 股")
        self.assertEqual(kwargs['sell_type'], 'partial')

    def test_L3_08d_sell_base_uses_live_available_not_signal_snapshot(self):
        """L3-08d 卖出基准量取执行时刻可用持仓：信号快照过期时不得按快照超卖

        2026-10-15 实盘：信号携带检测时刻快照 6100 股，执行时可卖只剩 4600 股
        （网格已抢先卖出 1500 股），按快照算卖 3600 股 → 实际卖出 84%（设计意图 60%）。
        """
        self.mock_pm.get_position.return_value = {
            'volume': 4600, 'available': 4600, 'cost_price': 12.93,
            'current_price': 13.64, 'profit_triggered': False,
        }
        self.strategy._execute_take_profit_half_signal('000001.SZ', {
            'volume': 6100, 'current_price': 13.64, 'sell_ratio': 0.6,
        })

        _, kwargs = self.mock_pm.simulate_sell_position.call_args
        self.assertEqual(kwargs['sell_volume'], 2700,
                         "应按最新可卖 4600 股算 60% = 2700 股，而非按快照 6100 算 3600 股")
        self.mock_pm.invalidate_positions_cache.assert_called_once()

    def test_L3_08e_sell_base_falls_back_to_snapshot_when_available_unreadable(self):
        """L3-08e 读不到可用持仓时退回信号快照，不阻断止盈执行"""
        self.mock_pm.get_position.return_value = None
        self.strategy._execute_take_profit_half_signal('000001.SZ', {
            'volume': 1000, 'current_price': 12.0, 'sell_ratio': 0.6,
        })

        _, kwargs = self.mock_pm.simulate_sell_position.call_args
        self.assertEqual(kwargs['sell_volume'], 600, "读不到持仓时沿用信号快照口径")

    def test_L3_08c_sell_ratio_formula_anchor(self):
        """L3-08c 锚定 sell_volume 公式：int(base * ratio / 100) * 100"""
        for total, ratio, expected in [
            (1000, 0.6, 600),
            (500, 0.6, 300),
            (1000, 0.5, 500),
            (350, 0.6, 200),    # int(350*0.6/100)*100 = int(2.1)*100 = 200
        ]:
            with self.subTest(total=total, ratio=ratio):
                # base 与快照一致，单独锚定比例公式本身
                self.mock_pm.get_position.return_value = {
                    'volume': total, 'available': total, 'profit_triggered': True,
                }
                self.mock_pm.simulate_sell_position.reset_mock()
                self.strategy._execute_take_profit_half_signal('000001.SZ', {
                    'volume': total, 'current_price': 12.0, 'sell_ratio': ratio,
                })
                _, kwargs = self.mock_pm.simulate_sell_position.call_args
                self.assertEqual(kwargs['sell_volume'], expected)

    def test_L3_08b_take_profit_half_minimum_100_shares(self):
        """L3-08b 比例极小时仍保证至少卖 100 股"""
        self.mock_pm.get_position.return_value = {
            'volume': 100, 'available': 100, 'profit_triggered': True,
        }
        # int(100 * 0.6 / 100) * 100 = 0 → max(0, 100) = 100
        self.strategy._execute_take_profit_half_signal('000001.SZ', {
            'volume': 100, 'current_price': 12.0, 'sell_ratio': 0.6,
        })

        _, kwargs = self.mock_pm.simulate_sell_position.call_args
        self.assertEqual(kwargs['sell_volume'], 100, "下限保护：至少 100 股")

    def test_L3_09_take_profit_half_verifies_profit_triggered(self):
        """L3-09 半仓止盈依 profit_triggered 双态返回

        卖出成功后要复查持仓标记：已置位→True；未置位→False（验证失败分支）
        """
        signal = {'volume': 1000, 'current_price': 12.0, 'sell_ratio': 0.6}

        # 已标记 → 验证成功
        self.mock_pm.get_position.return_value = {
            'volume': 500, 'profit_triggered': True}
        self.assertTrue(
            self.strategy._execute_take_profit_half_signal('000001.SZ', signal),
            "profit_triggered=True 应返回 True")

        # 未标记 → 验证失败
        self.mock_pm.get_position.return_value = {
            'volume': 500, 'profit_triggered': False}
        self.assertFalse(
            self.strategy._execute_take_profit_half_signal('000001.SZ', signal),
            "profit_triggered=False 应返回 False（状态异常）")

    def test_L3_09b_take_profit_full_sells_full(self):
        """L3-09b 动态止盈以 sell_type='full' 清仓"""
        ok = self.strategy._execute_take_profit_full_signal('000001.SZ', {
            'volume': 600, 'current_price': 13.0,
            'dynamic_take_profit_price': 12.8,
        })

        self.assertTrue(ok)
        _, kwargs = self.mock_pm.simulate_sell_position.call_args
        self.assertEqual(kwargs['sell_type'], 'full')
        self.assertEqual(kwargs['sell_volume'], 600)

    def test_L3_10_sell_failure_propagates(self):
        """L3-10 simulate_sell_position 返回 False 时三分支均透传 False"""
        self.mock_pm.simulate_sell_position.return_value = False

        cases = [
            ('止损', self.strategy._execute_stop_loss_signal,
             {'volume': 1000, 'current_price': 9.0, 'cost_price': 10.0}),
            ('半仓止盈', self.strategy._execute_take_profit_half_signal,
             {'volume': 1000, 'current_price': 12.0, 'sell_ratio': 0.6}),
            ('全仓止盈', self.strategy._execute_take_profit_full_signal,
             {'volume': 600, 'current_price': 13.0,
              'dynamic_take_profit_price': 12.8}),
        ]
        for name, method, signal in cases:
            with self.subTest(branch=name):
                self.assertFalse(method('000001.SZ', signal),
                                 f"{name} 应透传失败，不吞异常")


if __name__ == '__main__':
    unittest.main(verbosity=2)
