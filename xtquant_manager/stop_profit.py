"""
stop_profit.py — 动态止盈止损后台监控（xtquant_manager 模块）

直接复用 position_manager.py 中已验证的止盈止损算法：
  - calculate_stop_loss_price()  — 动态止盈位计算
  - _get_profit_level_info()      — 匹配止盈档位
  - check_trading_signals()       — 信号检测逻辑

设计原则：
  1. 仅依赖 xtquant_manager.account (XtQuantAccount) 进行持仓查询与下单
  2. 状态（最高价/止盈标记等）存储在内存 dict 中，停止即丢失
     （与 main.py 模式不同——那里有 SQLite 持久化。网关模式不持久化，
       因为持仓状态应从 QMT 查询，重启后 current_price/cost_price
       作为 initial 值即可）
  3. 信号去重：同一股票同类型信号 60 秒内不重复触发
  4. 线程安全：每次检测循环加锁，避免与 HTTP handler 竞争
"""

from __future__ import annotations

import threading
import time
import math
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from config import STOCK_TRADE_LOT_SIZE, is_continuous_trade_time
from order_utils import ACTIVE_ORDER_STATUS, ORDER_TYPE_SELL, calculate_sell_volume, position_quantities

try:
    from logger import get_logger
    logger = get_logger("xqm_stop")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.stop_profit")


# ============================================================================
# 配置（默认值来自 config.py，可在 standalone_config 中覆盖）
# ============================================================================

@dataclass
class StopProfitConfig:
    """止盈止损配置——字段名与 config.py 保持一致，方便对照。"""
    # 功能开关
    enabled: bool = True                          # ENABLE_DYNAMIC_STOP_PROFIT

    # 止损
    stop_loss_ratio: float = -0.075               # STOP_LOSS_RATIO

    # 首次止盈（回撤触发）
    initial_take_profit_ratio: float = 0.06       # INITIAL_TAKE_PROFIT_RATIO
    initial_take_profit_pullback_ratio: float = 0.005  # INITIAL_TAKE_PROFIT_PULLBACK_RATIO
    initial_take_profit_sell_ratio: float = 0.6   # INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE

    # 动态止盈档位表 [(最高盈利比例, 止盈位系数), ...]
    dynamic_take_profit: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.05, 0.96),
        (0.10, 0.93),
        (0.15, 0.90),
        (0.20, 0.87),
        (0.30, 0.85),
    ])

    # 监控频率
    monitor_interval: float = 3.0                 # 检测间隔（秒）
    signal_dedup_seconds: float = 60.0            # 同信号去重窗口（秒）
    price_staleness_seconds: float = 120.0        # 价格过期阈值（秒）


# ============================================================================
# 持仓状态（内存跟踪，非持久化）
# ============================================================================

@dataclass
class PositionState:
    stock_code: str
    highest_price: float = 0.0
    profit_triggered: bool = False                # 已触发首次止盈
    profit_breakout_triggered: bool = False       # 已突破止盈阈值
    breakout_highest_price: float = 0.0           # 突破后最高价
    last_price: float = 0.0
    last_price_time: float = 0.0


# ============================================================================
# 止盈止损监控器
# ============================================================================

class StopProfitMonitor:
    """
    后台线程，循环检测所有账号的持仓信号并执行卖出。

    Usage:
        monitor = StopProfitMonitor(manager, config)
        monitor.start()
        ...
        monitor.stop()
    """

    def __init__(self, manager: Any, cfg: Optional[StopProfitConfig] = None):
        """
        Args:
            manager: XtQuantManager 单例（避免循环导入，使用 Any 类型）
            manager: XtQuantManager 单例
            cfg: 止盈止损配置，None 则使用默认值
        """
        self._manager = manager                # XtQuantManager instance
        self._cfg = cfg or StopProfitConfig()
        self._states: Dict[str, Dict[str, PositionState]] = {}  # account_id -> {stock_code -> state}
        self._signal_history: Dict[str, float] = {}  # f"{account_id}:{stock_code}:{signal_type}" -> timestamp
        self._pending_signals = {}
        self._restriction_states = {}
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="StopProfitMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"止盈止损监控已启动 (间隔={self._cfg.monitor_interval}s)")

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        logger.info("止盈止损监控已停止")

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def update_config(self, cfg: StopProfitConfig) -> None:
        with self._lock:
            self._cfg = cfg
        logger.info("止盈止损配置已更新")

    def get_config(self) -> StopProfitConfig:
        return self._cfg

    def get_states(self) -> Dict[str, Dict[str, dict]]:
        """返回所有持仓状态的快照（供 API 查询）。"""
        with self._lock:
            result: Dict[str, Dict[str, dict]] = {}
            for acc_id, states in self._states.items():
                result[acc_id] = {}
                for code, s in states.items():
                    result[acc_id][code] = {
                        "stock_code": s.stock_code,
                        "highest_price": s.highest_price,
                        "profit_triggered": s.profit_triggered,
                        "profit_breakout_triggered": s.profit_breakout_triggered,
                        "breakout_highest_price": s.breakout_highest_price,
                        "last_price": s.last_price,
                    }
            return result

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.wait(self._cfg.monitor_interval):
            try:
                self._tick()
            except Exception as e:
                logger.error(f"止盈止损检测循环异常: {e}")

    def _tick(self) -> None:
        with self._lock:
            cfg = self._cfg

        if not cfg.enabled:
            return

        account_ids = self._manager.list_accounts()
        for acc_id in account_ids:
            try:
                self._check_account(acc_id, cfg)
            except Exception as e:
                logger.warning(f"[{acc_id[:4]}***] 止盈止损检测异常: {e}")

    def _check_account(self, acc_id: str, cfg: StopProfitConfig) -> None:
        account = self._manager._accounts.get(acc_id)
        if account is None or not account._connected:
            return

        # 查询持仓
        positions = account.query_positions(strict=True)
        if positions is None:
            return
        self._confirm_pending_signals(acc_id, account, positions)
        held_codes = {str(p.get('证券代码', ''))[:6] for p in positions}
        with self._lock:
            for code in list(self._states.get(acc_id, {})):
                if code[:6] not in held_codes:
                    self._states[acc_id].pop(code, None)
        if not positions:
            return

        # 个股「动态止盈止损」开关：一次性读取该账号 SQLite，避免每股查库
        sp_flags = self._load_stop_profit_flags(acc_id)

        for pos in positions:
            self._check_position(acc_id, account, pos, cfg, sp_flags)

    def _load_stop_profit_flags(self, acc_id: str) -> Dict[str, bool]:
        """读取账号 data_<aid>/trading.db positions 表的 stop_profit_enabled。

        返回 {裸股票代码: bool}。库不存在 / 旧库无该列 / 读取异常一律返回 {}，
        由调用方 .get(code, True) 兜底为「开启」，保证网关行为向后兼容。
        """
        import sqlite3
        import os as _os
        db_path = _os.path.join(_os.path.dirname(__file__), "..", f"data_{acc_id}", "trading.db")
        db_path = _os.path.normpath(db_path)
        if not _os.path.exists(db_path):
            return {}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT stock_code, stop_profit_enabled FROM positions"
            ).fetchall()
            conn.close()
            flags: Dict[str, bool] = {}
            for r in rows:
                code = str(r["stock_code"] or "").split(".")[0]
                if not code:
                    continue
                val = r["stop_profit_enabled"]
                flags[code] = True if val is None else bool(val)
            return flags
        except Exception:
            # 旧库缺 stop_profit_enabled 列会命中 "no such column" → 全部按开启处理
            return {}

    def _check_position(self, acc_id: str, account, pos: dict, cfg: StopProfitConfig,
                        sp_flags: Optional[Dict[str, bool]] = None) -> None:
        stock_code = pos.get("证券代码", "")
        volume = int(pos.get("股票余额", 0) or 0)
        available = int(pos.get("可用余额", 0) or 0)
        cost_price = float(pos.get("成本价", 0) or 0)
        current_price = float(pos.get("市价", 0) or 0)

        if not stock_code or volume <= 0 or cost_price <= 0:
            return

        # 个股级开关：该股被关闭则跳过（缺库/缺列/缺行默认开启）
        if sp_flags and sp_flags.get(str(stock_code).split(".")[0], True) is False:
            return

        # ——— 获取或创建持仓状态 ———
        with self._lock:
            if acc_id not in self._states:
                self._states[acc_id] = {}
            state = self._states[acc_id].get(stock_code)
            if state is None:
                state = PositionState(stock_code=stock_code)
                self._states[acc_id][stock_code] = state

        # ——— 价格有效性 ———
        fresh_price = current_price > 0
        if current_price <= 0:
            # 每轮优先查询新价格，不能靠反复使用缓存来延长旧价格寿命。
            try:
                tick = account.get_full_tick([stock_code])
                if tick and stock_code in tick:
                    lp = tick[stock_code].get("lastPrice", 0)
                    if lp and float(lp) > 0:
                        current_price = float(lp)
                        fresh_price = True
            except Exception:
                pass
            if (current_price <= 0 and state.last_price > 0
                    and 0 <= time.time() - state.last_price_time < cfg.price_staleness_seconds):
                current_price = state.last_price
            if current_price <= 0:
                return  # 无法获取有效价格，跳过

        # 更新最近价格
        state.last_price = current_price
        if fresh_price:
            state.last_price_time = time.time()

        # ——— 更新最高价 ———
        if current_price > state.highest_price or state.highest_price <= 0:
            state.highest_price = current_price

        # ——— 1) 止损检查（最高优先级） ———
        signal = self._check_stop_loss(stock_code, cost_price, current_price, available, state, cfg)
        restriction = (str(pos.get('restriction_reason', 'unknown')), volume - available,
                       bool(signal), datetime.now().date())
        restriction_key = (acc_id, stock_code)
        with self._lock:
            previous = self._restriction_states.get(restriction_key)
            self._restriction_states[restriction_key] = restriction
        if restriction != previous and available < volume:
            message = (f"[{stock_code}] 可卖={available}，不可卖={volume - available}，"
                       f"原因={pos.get('restriction_detail', '待确认')}")
            if signal:
                logger.warning(f"{message}；已触发止损，继续监控并仅处理可卖部分")
            else:
                logger.info(message)
        elif restriction != previous and previous and previous[1] > 0 and available == volume:
            logger.info(f"[{stock_code}] 可卖数量已恢复，重新评估当前交易条件")
        if signal:
            if available <= 0:
                return
            self._emit_signal(acc_id, account, stock_code, "stop_loss", signal)
            return

        # ——— 2) 首次止盈检查 ———
        if not state.profit_triggered:
            signal = self._check_first_take_profit(stock_code, cost_price, current_price, available, state, cfg)
            if signal:
                self._emit_signal(acc_id, account, stock_code, "take_profit_half", signal)
                return

        # ——— 3) 动态止盈检查（已触发首次止盈后） ———
        if state.profit_triggered and state.highest_price > 0:
            if available <= 0:
                return  # 已有委托在途，跳过
            signal = self._check_dynamic_take_profit(stock_code, cost_price, current_price, available, state, cfg)
            if signal:
                self._emit_signal(acc_id, account, stock_code, "take_profit_full", signal)
                return

    # ==================================================================
    # 信号检测（算法与 position_manager.py 保持一致）
    # ==================================================================

    def _check_stop_loss(
        self, stock_code: str, cost_price: float, current_price: float,
        available: int, state: PositionState, cfg: StopProfitConfig,
    ) -> Optional[dict]:
        """止损检测 — 复刻 position_manager.check_trading_signals 步骤 4"""
        stop_loss_price = cost_price * (1 + cfg.stop_loss_ratio)
        if current_price <= stop_loss_price:
            loss_ratio = (cost_price - current_price) / cost_price
            expected = abs(cfg.stop_loss_ratio)
            if loss_ratio >= expected * 0.5:
                reason = "stop_loss_1" if state.profit_triggered else "stop_loss_0"
                return {
                    "current_price": current_price,
                    "stop_loss_price": stop_loss_price,
                    "cost_price": cost_price,
                    "volume": available,
                    "reason": reason,
                    "profit_triggered": state.profit_triggered,
                }
        return None

    def _check_first_take_profit(
        self, stock_code: str, cost_price: float, current_price: float,
        available: int, state: PositionState, cfg: StopProfitConfig,
    ) -> Optional[dict]:
        """首次止盈检测（含回撤） — 复刻 position_manager.check_trading_signals 步骤 5-6"""
        profit_ratio = (current_price - cost_price) / cost_price

        if not state.profit_breakout_triggered:
            # 检查是否突破止盈阈值
            if profit_ratio >= cfg.initial_take_profit_ratio:
                logger.info(
                    f"[{stock_code}] 首次突破止盈阈值 {cfg.initial_take_profit_ratio:.1%}, "
                    f"当前盈利={profit_ratio:.1%}, 开始监控回撤"
                )
                state.profit_breakout_triggered = True
                state.breakout_highest_price = current_price
                return None  # 不立即交易，继续监控
        else:
            # 更新突破后最高价
            if current_price > state.breakout_highest_price:
                state.breakout_highest_price = current_price

            # 检查回撤条件
            if state.breakout_highest_price > 0:
                pullback = (state.breakout_highest_price - current_price) / state.breakout_highest_price
                if pullback >= cfg.initial_take_profit_pullback_ratio:
                    logger.info(
                        f"[{stock_code}] 触发回撤止盈: breakout_high={state.breakout_highest_price:.2f} "
                        f"current={current_price:.2f} pullback={pullback:.2%}"
                    )
                    return {
                        "current_price": current_price,
                        "cost_price": cost_price,
                        "profit_ratio": profit_ratio,
                        "volume": available,
                        "sell_ratio": cfg.initial_take_profit_sell_ratio,
                        "breakout_highest_price": state.breakout_highest_price,
                        "pullback_ratio": pullback,
                    }
        return None

    def _check_dynamic_take_profit(
        self, stock_code: str, cost_price: float, current_price: float,
        available: int, state: PositionState, cfg: StopProfitConfig,
    ) -> Optional[dict]:
        """动态止盈检测 — 复刻 position_manager.check_trading_signals 步骤 7"""
        dynamic_price, matched_level, coefficient = self._calc_dynamic_stop_price(
            cost_price, state.highest_price, state.profit_triggered, cfg
        )

        if dynamic_price <= 0 or dynamic_price > state.highest_price * 1.1:
            return None

        if current_price <= dynamic_price:
            logger.info(
                f"[{stock_code}] 触发动态全仓止盈: price={current_price:.2f} "
                f"dynamic_stop={dynamic_price:.2f} highest={state.highest_price:.2f} "
                f"level={matched_level:.1%} coeff={coefficient}"
            )
            return {
                "current_price": current_price,
                "dynamic_take_profit_price": dynamic_price,
                "highest_price": state.highest_price,
                "matched_level": matched_level,
                "volume": available,
                "cost_price": cost_price,
            }
        return None

    def _calc_dynamic_stop_price(
        self, cost_price: float, highest_price: float,
        profit_triggered: bool, cfg: StopProfitConfig,
    ) -> Tuple[float, float, float]:
        """
        计算动态止盈价格 — 复刻 position_manager.calculate_stop_loss_price()

        Returns:
            (stop_price, matched_level, coefficient)
        """
        if not profit_triggered:
            return (cost_price * (1 + cfg.stop_loss_ratio), 0.0, 1.0)

        if not cfg.dynamic_take_profit:
            return (highest_price * 0.95, 0.0, 0.95)

        if cost_price <= 0:
            return (0.0, 0.0, 1.0)

        highest_profit_ratio = (highest_price - cost_price) / cost_price
        matched_level = 0.0
        coefficient = 1.0

        for pl, coef in sorted(cfg.dynamic_take_profit, reverse=True):
            if highest_profit_ratio >= pl:
                coefficient = coef
                matched_level = pl
                break

        if matched_level == 0.0:
            # 未达任何动态档位，回退到固定止损
            fallback = cost_price * (1 + cfg.stop_loss_ratio)
            return (fallback, 0.0, 1.0)

        return (highest_price * coefficient, matched_level, coefficient)

    # ==================================================================
    # 信号去重 + 执行
    # ==================================================================

    def _signal_key(self, acc_id: str, stock_code: str, signal_type: str) -> str:
        return f"{acc_id}:{stock_code}:{signal_type}"

    def _is_duplicate(self, key: str, dedup_seconds: float) -> bool:
        last = self._signal_history.get(key, 0)
        return (time.time() - last) < dedup_seconds

    def _emit_signal(
        self, acc_id: str, account, stock_code: str,
        signal_type: str, info: dict,
    ) -> None:
        """去重检查通过后执行卖出。"""
        with self._lock:
            cfg = self._cfg
        key = self._signal_key(acc_id, stock_code, signal_type)
        if not cfg.enabled or not is_continuous_trade_time():
            return
        snapshot = position_quantities({'volume': info.get('volume'), 'available': info.get('volume')})
        if snapshot is None or snapshot[1] <= 0:
            return
        try:
            positions = account.query_positions(strict=True)
            position = next((p for p in positions or []
                             if str(p.get('证券代码', ''))[:6] == stock_code[:6]), None)
            quantities = position_quantities(position)
            orders = account.query_orders(strict=True)
        except Exception as e:
            logger.warning(f"[{stock_code}] 卖出前持仓或委托查询失败: {e}")
            return
        if quantities is None or quantities[1] <= 0 or orders is None:
            return
        if any(str(o.get('证券代码', ''))[:6] == stock_code[:6]
               and o.get('委托类型') == ORDER_TYPE_SELL
               and o.get('委托状态') in ACTIVE_ORDER_STATUS for o in orders):
            return
        ratio = info.get('sell_ratio', cfg.initial_take_profit_sell_ratio) if signal_type == 'take_profit_half' else 1
        sell_volume = calculate_sell_volume(quantities[1], ratio, lot_size=STOCK_TRADE_LOT_SIZE)
        if sell_volume <= 0:
            return

        current_price = info.get("current_price", 0)
        if not isinstance(current_price, (int, float)) or not math.isfinite(current_price) or current_price <= 0:
            return

        pending_key = (acc_id, stock_code[:6])
        with self._lock:
            if pending_key in self._pending_signals or self._is_duplicate(key, cfg.signal_dedup_seconds):
                return
            reservation = {'order_id': None, 'signal_type': signal_type, 'stock_code': stock_code,
                           'trade_date': datetime.now().date()}
            self._pending_signals[pending_key] = reservation
        try:
            # xtquant order_type: 24=限价卖出, price_type: 11=固定价
            order_id = account.order_stock(
                stock_code=stock_code,
                order_type=24,
                order_volume=sell_volume,
                price_type=11,
                price=current_price,
                strategy_name=signal_type,
                order_remark=f"auto {signal_type}",
            )
            if order_id > 0:
                with self._lock:
                    reservation['order_id'] = order_id
                    self._signal_history[key] = time.time()
                logger.warning(
                    f"[{stock_code}] {signal_type} 卖出已提交: "
                    f"volume={sell_volume} price={current_price:.2f} order_id={order_id}"
                )
                # 委托受理不代表成交，状态由后续委托查询确认。
            else:
                logger.error(f"[{stock_code}] {signal_type} 下单失败 (order_id={order_id})")
        except Exception as e:
            logger.error(f"[{stock_code}] {signal_type} 执行异常: {e}")
        finally:
            with self._lock:
                if reservation['order_id'] is None:
                    self._pending_signals.pop(pending_key, None)

    def _confirm_pending_signals(self, acc_id, account, positions=None):
        """利用已有委托查询确认成交，未成交不推进首次止盈状态。"""
        with self._lock:
            pending = [(k, dict(v)) for k, v in self._pending_signals.items()
                       if k[0] == acc_id and v['order_id'] is not None]
        if not pending:
            return
        orders = account.query_orders(strict=True)
        if orders is None:
            return
        for key, info in pending:
            order = next((o for o in orders if str(o.get('订单编号')) == str(info['order_id'])), None)
            if order is None:
                # 日内委托跨日不再出现在当日列表时，先确认新持仓及无同股在途单，
                # 再释放旧意图；新一轮仍须重新取得价格、验证交易条件。
                if (positions is not None and is_continuous_trade_time()
                        and info.get('trade_date') != datetime.now().date()
                        and not any(str(o.get('证券代码', ''))[:6] == key[1]
                                    and o.get('委托状态') in ACTIVE_ORDER_STATUS for o in orders)):
                    current = next((p for p in positions if str(p.get('证券代码', ''))[:6] == key[1]), None)
                    if current is None or position_quantities(current) is not None:
                        with self._lock:
                            self._pending_signals.pop(key, None)
                            self._signal_history.pop(self._signal_key(acc_id, info['stock_code'], info['signal_type']), None)
                        logger.warning(f"[{info['stock_code']}] 跨日委托未取得旧终态，已按新持仓释放旧意图，重新判断")
                continue
            status = order.get('委托状态')
            filled = int(order.get('成交数量') or 0)
            has_fill = filled > 0 or status == 56
            with self._lock:
                state = self._states.get(acc_id, {}).get(info['stock_code'])
                if state and has_fill and info['signal_type'] == 'take_profit_half':
                    state.profit_triggered = True
                if status in (53, 54, 56, 57):
                    self._pending_signals.pop(key, None)
                    if not has_fill:
                        self._signal_history.pop(self._signal_key(acc_id, info['stock_code'], info['signal_type']), None)

    # ==================================================================
    # 手动卖出（供 API 调用）
    # ==================================================================

    def manual_sell(
        self, acc_id: str, stock_code: str, volume: int,
        price: float, strategy: str = "manual",
    ) -> int:
        """手动卖出，成功返回 order_id，失败返回 -1。"""
        account = self._manager._accounts.get(acc_id)
        if account is None or not account._connected:
            return -1
        return account.order_stock(
            stock_code=stock_code,
            order_type=24,
            order_volume=volume,
            price_type=11,
            price=price,
            strategy_name=strategy,
        )
