"""
miniqmt_autobuy 独立进程入口与调度。

由 miniqmt.bat 菜单 [j] 启动。调度循环每 30s tick，支持两种触发模式:
  daily    — 命中 cfg.daily_times 的时刻 (当日去重)
  interval — 距上次触发 >= cfg.interval_minutes (仅交易时段)
  both     — 两者都启用

单轮流程: 拉候选池 → 大盘指数门禁 → 防重过滤 → 洗牌后惰性条件检查(记决策日志) → HTTP下单(复用web买入API) → 记买入历史。
下单后止盈止损由主程序 position_manager 自动接管。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import threading
from datetime import date, datetime

import config
import trade_calendar
from .config import DEFAULT_CFG_PATH, PROJECT_ROOT, get_autobuy_logger, load_config
from .pool import normalize_code, read_candidates
from .store import AutoBuyStore
from .client import WebClient
from .filter import BuyConditionFilter, MarketIndexFilter

logger = get_autobuy_logger("autobuy")

STATUS_FILE = os.path.join(PROJECT_ROOT, "data", ".autobuy_status.json")
TICK_SECONDS = 30


class AutoBuyApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.store = AutoBuyStore()
        self.client = WebClient(cfg)

        # data_manager 用于取行情/历史/标的明细 (独立进程内自取，下单才走 HTTP)
        from data_manager import get_data_manager
        self.dm = get_data_manager()
        self.filter = BuyConditionFilter(cfg, self.dm)
        self.market_filter = MarketIndexFilter(self.dm)

        # 调度状态
        self._fired_daily = set()        # 当日已触发的 (h, m)
        self._fired_daily_date = None
        self._last_interval_run = datetime.now()  # 启动后等一个间隔再触发 interval
        self._calendar_fallback_warned = None     # 交易日历降级告警的当日去重

    # ------------------------------------------------------------------
    # 单轮执行
    # ------------------------------------------------------------------
    def run_once(self, trigger: str) -> None:
        run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode_tag = " [模拟运行]" if self.cfg.simulation_mode else ""
        logger.info(f"===== 触发自动买入 [{trigger}]{mode_tag} {run_time} =====")

        codes = read_candidates(self.cfg)
        status = {
            "last_run": run_time, "trigger": trigger,
            "simulation_mode": self.cfg.simulation_mode,
            "candidates": len(codes), "checked": 0, "passed": 0, "bought": [],
        }
        if not codes:
            logger.info("候选池为空，结束本轮")
            self._write_status(status)
            return

        market_ok, market_reason = self.market_filter.check()
        status["market_filter"] = market_reason
        if not market_ok:
            logger.info(f"大盘门禁未通过，本轮不买入: {market_reason.get('failed', market_reason)}")
            self._write_status(status)
            return
        idx = market_reason.get("passed_index")
        idx_detail = (market_reason.get("details") or {}).get(idx, {})
        if "ma5" in idx_detail:
            logger.info(
                f"大盘门禁通过: 指数 {idx} MA5 {idx_detail['ma5_prev']}→{idx_detail['ma5']} 向上"
            )
        else:
            logger.info(f"大盘门禁通过: 指数 {idx} MA5 向上")

        # 防重过滤前置: 先剔除已持仓/窗口内已买，避免对不可买标的做昂贵的条件检查
        eligible = self._dedup_filter(codes)
        if not eligible:
            logger.info("候选池经防重过滤后无可买标的，结束本轮")
            self._write_status(status)
            return

        # 洗牌 + 惰性条件检查: 收集到 max_buys_per_run 只通过即停。
        # 对均匀洗牌后的列表取"前 k 个通过项"，等价于在全部通过标的中均匀随机选 k 只，
        # 且无需检查整个候选池(可能数百只)。
        random.shuffle(eligible)
        need = self.cfg.max_buys_per_run
        chosen = []
        checked = 0
        failures = []  # (code, 原因) 供"本轮无标的通过"时给日志留样本
        for code in eligible:
            if len(chosen) >= need:
                break
            checked += 1
            try:
                ok, reason = self.filter.check(code)
            except Exception as e:
                ok, reason = False, {"code": code, "failed": [f"检查异常: {e}"]}
                logger.error(f"{code} 条件检查异常: {e}")
            self.store.record_decision(run_time, code, ok, reason)
            if ok:
                chosen.append(code)
                logger.info(f"  ✓ {code} 通过条件检查")
            else:
                failed = reason.get("failed") or []
                failures.append((code, "/".join(failed) or "未通过"))
                logger.debug(f"  ✗ {code} 未通过: {failed}")
        status["checked"] = checked
        status["passed"] = len(chosen)
        logger.info(
            f"惰性检查: 合格标的 {len(eligible)} 只，检查 {checked} 只，"
            f"命中 {len(chosen)}/{need} 只: {chosen}"
        )
        if not chosen:
            # 条件明细此前只有 DEBUG(文件日志记 INFO) + 落库 decision_log，
            # 排查"为什么不买"必须开 DEBUG 或查库；此处补一条带样本的 INFO。
            logger.info(
                f"检查完毕无标的通过条件，结束本轮(共检查 {checked} 只)"
                + (f"；示例 {self._failure_samples(failures)}" if failures else "")
            )
            self._write_status(status)
            return

        # 下单 (复用 web 买入 API; 模拟运行时跳过真实请求)
        simulated = self.cfg.simulation_mode
        for code in chosen:
            if simulated:
                success, http_status, result = True, None, {"simulated": True}
                logger.info(f"  [模拟] 应买入 {code}，未发送真实下单请求")
            else:
                success, http_status, result = self.client.buy(code)
            self.store.record_buy(
                code, trigger, success, http_status, result,
                amount=None, is_simulation=simulated,
            )
            if success:
                status["bought"].append(code)
                if not simulated:
                    logger.info(f"  下单成功: {code} (后续止盈止损交由主程序)")
            else:
                logger.warning(f"  下单失败: {code} -> {result}")

        self._write_status(status)

    @staticmethod
    def _failure_samples(failures: list, limit: int = 3) -> str:
        """把未通过原因压成简短样本，避免大候选池把日志刷爆。"""
        parts = [f"{code}: {why}" for code, why in failures[:limit]]
        if len(failures) > limit:
            parts.append(f"...另 {len(failures) - limit} 只")
        return "；".join(parts)

    def _dedup_filter(self, codes: list) -> list:
        """过滤掉已持仓 / 防重窗口内已买过的股票。"""
        cfg = self.cfg
        held = None
        if cfg.dedup_by_position:
            held = self.client.get_held_codes()
            if held is None:
                # 持仓查询失败：安全优先，跳过本轮买入，避免重复买入
                logger.warning("持仓查询失败，为避免重复买入，本轮不下单")
                return []
        recent = self.store.recently_bought_codes(cfg.dedup_window_days)

        eligible = []
        for code in codes:
            key = normalize_code(code)
            if held is not None and key in held:
                logger.info(f"  防重跳过 {code}: 已持仓")
                continue
            if key in recent:
                logger.info(f"  防重跳过 {code}: {cfg.dedup_window_days}日内已买过")
                continue
            eligible.append(code)
        return eligible

    # ------------------------------------------------------------------
    # 调度循环
    # ------------------------------------------------------------------
    def _non_trade_reason(self):
        """返回本轮不可交易的原因；None 表示可以交易。

        时段与节假日分开判定: 原实现只查 config.is_market_hours()(周一至周五 +
        09:30~15:00)，法定节假日会被当成交易日全天误触发。交易日历拿不到当日权威
        数据时按"可交易"处理并告警 —— 宁可多跑一轮完整筛选，也不能因为日历缺失漏买。
        """
        if not self.cfg.only_trade_time:
            return None
        if not config.is_market_hours():
            return "非交易时段"
        is_open_today, confident = trade_calendar.is_trading_day_confident(date.today())
        if not confident:
            self._warn_calendar_fallback()
            return None
        if not is_open_today:
            return "今日休市(交易日历)"
        return None

    def _warn_calendar_fallback(self) -> None:
        """交易日历降级为"周一至周五"时告警，每日一次。"""
        today = date.today()
        if self._calendar_fallback_warned == today:
            return
        self._calendar_fallback_warned = today
        logger.warning(
            "交易日历无当日权威数据，已退化为周一至周五口径"
            " —— 法定节假日可能被误判为交易日"
        )

    def _tick(self) -> None:
        now = datetime.now()
        mode = self.cfg.mode

        # 重置当日 daily 去重
        if self._fired_daily_date != now.date():
            self._fired_daily.clear()
            self._fired_daily_date = now.date()

        # 每 tick 只判定一次，daily/interval 共用同一结论
        skip_reason = self._non_trade_reason()

        # daily 触发
        if mode in ("daily", "both"):
            for (h, m) in self.cfg.daily_times:
                if now.hour == h and now.minute == m and (h, m) not in self._fired_daily:
                    self._fired_daily.add((h, m))
                    if skip_reason:
                        logger.info(f"daily {h:02d}:{m:02d} 命中但{skip_reason}，跳过")
                    else:
                        self._safe_run(f"daily-{h:02d}:{m:02d}")

        # interval 触发: 仅在交易时段计时与触发。非交易时段完全静默，且不消费
        # 计时器，使开盘后能尽快触发首轮（而非从盘前的残留计时起算）。
        if mode in ("interval", "both") and skip_reason is None:
            elapsed = (now - self._last_interval_run).total_seconds()
            if elapsed >= self.cfg.interval_minutes * 60:
                self._last_interval_run = now
                self._safe_run(f"interval-{self.cfg.interval_minutes}m")

    def _safe_run(self, trigger: str) -> None:
        try:
            self.run_once(trigger)
        except Exception as e:
            logger.error(f"本轮执行异常 [{trigger}]: {e}", exc_info=True)

    def run_loop(self) -> None:
        if self.cfg.simulation_mode:
            logger.info("⚠️  模拟运行模式: 不会发送真实买入请求，其余逻辑照常执行")
        logger.info(
            f"自动买入调度启动: mode={self.cfg.mode} "
            f"daily={self.cfg.daily_times} interval={self.cfg.interval_minutes}min "
            f"only_trade_time={self.cfg.only_trade_time}"
        )
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"调度 tick 异常: {e}", exc_info=True)
            self.stop_event.wait(TICK_SECONDS)
        logger.info("调度循环已退出")

    def shutdown(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------
    def _write_status(self, status: dict) -> None:
        status["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATUS_FILE)
        except OSError as e:
            logger.debug(f"写状态文件失败: {e}")

    def close(self) -> None:
        self.store.close()
        try:
            if os.path.exists(STATUS_FILE):
                os.remove(STATUS_FILE)
        except OSError:
            pass


def _log_startup_banner(cfg) -> None:
    """记录本次启动的生效配置，复盘时不必再从 cfg 文件反推当时参数。"""
    logger.info(
        f"进程 PID={os.getpid()} 运行方式="
        f"{'模拟(不下单)' if cfg.simulation_mode else '实盘(会真实下单)'}"
    )
    logger.info(
        f"候选池: {cfg.db_path} 表={','.join(cfg.tables)} "
        f"取运行日前 {cfg.latest_n_dates} 个交易日"
    )
    logger.info(
        f"调度: mode={cfg.mode} daily={cfg.daily_times} "
        f"interval={cfg.interval_minutes}min only_trade_time={cfg.only_trade_time}"
    )
    logger.info(
        f"风控: 单轮最多买 {cfg.max_buys_per_run} 只，"
        f"持仓防重={cfg.dedup_by_position}，历史防重窗口={cfg.dedup_window_days} 天"
    )


def _refresh_calendar_at_startup() -> None:
    """启动时无条件刷新交易日历缓存。

    进程可能连续运行数周，长假(国庆/春节)前必须拿到新日历；否则只剩
    config.is_market_hours() 的周一至周五口径，会把整个长假当成交易日，
    全天每 30 分钟空跑一轮完整筛选(2026-10-01 实测如此)。刷新失败只告警，
    不阻断启动。
    """
    result = trade_calendar.refresh()
    if result["degraded"]:
        logger.warning(
            "交易日历刷新失败，已退化为周一至周五口径(法定节假日会被误判为交易日): "
            f"覆盖 {result['start']} ~ {result['end']} 共 {result['count']} 天"
        )
    else:
        logger.info(
            f"交易日历已刷新: source={result['source']} "
            f"覆盖 {result['start']} ~ {result['end']} 共 {result['count']} 天"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="miniQMT 自动买入服务")
    parser.add_argument("--config", default=DEFAULT_CFG_PATH, help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="立即执行一轮后退出(用于测试/手动触发)")
    parser.add_argument(
        "--simulate", action="store_true",
        help="模拟运行: 不发送真实买入请求，其余逻辑照常(覆盖配置 risk.simulation_mode)",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"加载配置失败: {e}")
        return 1

    if args.simulate:
        cfg.simulation_mode = True

    _log_startup_banner(cfg)
    _refresh_calendar_at_startup()

    app = AutoBuyApp(cfg)

    def _handle_signal(signum, _frame):
        logger.info(f"收到信号 {signum}，准备退出...")
        app.shutdown()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    try:
        if args.once:
            if cfg.simulation_mode:
                logger.info("⚠️  模拟运行模式: 不会发送真实买入请求，其余逻辑照常执行")
            app.run_once("manual-once")
        else:
            app.run_loop()
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt，退出")
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
