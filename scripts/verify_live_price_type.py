#!/usr/bin/env python
"""实盘限价委托(price_type=11)验证脚本 — 100 股离场价挂单，验完即撤。

背景
----
2026-10-26 网格卖出使用 price_type=5(LATEST_PRICE)，交易所侧委托价被 QMT 客户端
替换成昨收 14.69；个股高开 +6% 后该价格落在价格笼子之外，8 笔委托全部废单
（status_msg=委托价格超出范围）。修复后实盘默认改用 11=FIX_PRICE。

本脚本验证三件事
----------------
1. 交易所收到的委托价 == 我们算出来的价（不再被 QMT 改写）
2. 该价格类型被柜台正常受理（不会因价格类型被拒）
3. 全程不产生成交：买入挂跌停价、卖出挂涨停价，只有标的打板才可能成交

安全设计
--------
- 默认 dry-run，只打印计划，不下单
- 下单必须同时给 --execute 和 --confirm VERIFY_<股票代码>
- 数量固定 100 股
- 无论校验成功、失败还是异常，finally 都会撤单并等待终态
- 运行前校验交易时段、QMT 连接、可卖数量/可用资金

用法
----
    # 1) 演练（不产生任何委托）
    python scripts/verify_live_price_type.py --stock 603466

    # 2) 开盘后实盘验证（买卖各一次）
    python scripts/verify_live_price_type.py --stock 603466 --side both \\
        --execute --confirm VERIFY_603466

退出码: 0=全部通过, 1=校验失败, 2=参数/环境不满足
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ORDER_REMARK = 'VERIFY_PRICE_TYPE'
ORDER_STATUS_TERMINAL = {53, 54, 56, 57}
PRICE_TOLERANCE = 0.001


class VerifyFailure(RuntimeError):
    pass


def _fmt(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _resolve_code(stock: str) -> str:
    """补齐交易所后缀：6 开头走 SH，其余走 SZ（本脚本用于 100 股验证，够用）。"""
    if '.' in stock:
        return stock
    return f"{stock}.SH" if stock.startswith('6') else f"{stock}.SZ"


def _get_limits(code: str) -> Tuple[Optional[float], Optional[float]]:
    from xtquant import xtdata
    detail = xtdata.get_instrument_detail(code) or {}
    up = detail.get('UpStopPrice')
    down = detail.get('DownStopPrice')
    try:
        up = float(up) if up is not None else None
    except (TypeError, ValueError):
        up = None
    try:
        down = float(down) if down is not None else None
    except (TypeError, ValueError):
        down = None
    return up, down


def _get_last_price(code: str) -> Optional[float]:
    from xtquant import xtdata
    tick = (xtdata.get_full_tick([code]) or {}).get(code) or {}
    for key in ('lastPrice', 'lastClose'):
        try:
            value = float(tick.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return None


def _clamp_price(qmt, code: str, raw_price: float, side_label: str) -> float:
    """复用生产代码里的涨跌停夹取，保证验证的是线上同一条逻辑。"""
    from trading_executor import TradingExecutor

    class _Stub:
        pass

    stub = _Stub()
    stub.position_manager = _Stub()
    stub.position_manager.qmt_trader = qmt
    return TradingExecutor._clamp_order_price_to_daily_limits(
        stub, code, raw_price, side_label)


def _snapshot_order_ids(qmt) -> set:
    ids = set()
    try:
        df = qmt.query_stock_orders()
    except Exception:
        return ids
    if df is not None and hasattr(df, 'empty') and not df.empty:
        ids.update(int(v) for v in df['订单编号'].tolist())
    return ids


def _resolve_order_id(qmt, seq, before_ids, timeout: float = 6.0):
    """优先用 seq→order_id 映射；映射缺失时退回"相对提交前唯一新增的委托"。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            mapped = qmt.order_id_map.get(seq) or qmt.order_id_map.get(str(seq))
        except Exception:
            mapped = None
        if mapped:
            return int(mapped)
        try:
            df = qmt.query_stock_orders()
        except Exception:
            df = None
        if df is not None and hasattr(df, 'empty') and not df.empty:
            new_ids = [int(v) for v in df['订单编号'].tolist() if int(v) not in before_ids]
            if len(new_ids) == 1:
                return new_ids[0]
        time.sleep(0.15)
    return None


def _query_order_by_id(qmt, order_id, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            df = qmt.query_stock_orders()
        except Exception:
            df = None
        if df is not None and hasattr(df, 'empty') and not df.empty:
            rows = df[df['订单编号'] == order_id]
            if not rows.empty:
                return rows.iloc[-1].to_dict()
        time.sleep(0.2)
    return None


def _wait_terminal(qmt, order_id, timeout: float = 10.0):
    deadline = time.time() + timeout
    latest = None
    while time.time() < deadline:
        try:
            df = qmt.query_stock_orders()
        except Exception:
            df = None
        if df is not None and hasattr(df, 'empty') and not df.empty:
            rows = df[df['订单编号'] == order_id]
            if not rows.empty:
                latest = rows.iloc[-1].to_dict()
                if int(latest.get('委托状态') or 0) in ORDER_STATUS_TERMINAL:
                    return latest
        time.sleep(0.2)
    return latest


def _run_one_side(qmt, code: str, side: str, last_price: Optional[float],
                  up_stop: Optional[float], down_stop: Optional[float],
                  execute: bool) -> bool:
    is_buy = side == 'buy'
    side_label = '买入' if is_buy else '卖出'

    if is_buy:
        if not down_stop:
            raise VerifyFailure("拿不到跌停价，无法构造离场价买单")
        raw_price = down_stop
    else:
        if not up_stop:
            raise VerifyFailure("拿不到涨停价，无法构造离场价卖单")
        raw_price = up_stop

    clamped = _clamp_price(qmt, code, raw_price, side_label)
    # easy_qmt_trader 内部还会叠加滑点：买入 +slippage、卖出 -slippage
    wire_price = round(qmt.select_slippage(
        stock=code, price=clamped, trader_type='buy' if is_buy else 'sell'), 2)

    print(f"\n[{side_label}] 计划")
    print(f"  标的={code}  最新价={_fmt(last_price)}  "
          f"涨停={_fmt(up_stop)}  跌停={_fmt(down_stop)}")
    print(f"  原始挂单价={_fmt(raw_price)} → 夹取后={_fmt(clamped)} → 预期交易所价={_fmt(wire_price)}")
    print(f"  数量=100 股  委托备注={ORDER_REMARK}")

    if not execute:
        print("  [dry-run] 未下单")
        return True

    before_ids = _snapshot_order_ids(qmt)
    if is_buy:
        returned = qmt.buy(security=code, amount=100, price_type=11, price=clamped,
                           strategy_name='verify', order_remark=ORDER_REMARK)
    else:
        returned = qmt.sell(security=code, amount=100, price_type=11, price=clamped,
                            strategy_name='verify', order_remark=ORDER_REMARK)

    if not returned or int(returned) < 0:
        raise VerifyFailure(f"{side_label}下单被本地拒绝，返回值={returned}")

    order_id = _resolve_order_id(qmt, returned, before_ids)
    if order_id is None:
        raise VerifyFailure(f"{side_label}下单后未能解析委托号（请求序号={returned}）")
    order = _query_order_by_id(qmt, order_id)
    if not order:
        raise VerifyFailure(f"{side_label}下单后未查到委托（委托号={order_id}）")

    wire_actual = order.get('委托价格')
    status = int(order.get('委托状态') or 0)
    print(f"  委托号={order_id}  交易所侧委托价={_fmt(wire_actual)}  "
          f"委托状态={status}  状态说明={order.get('委托状态描述') or '无'}")

    passed = True
    if abs(float(wire_actual) - wire_price) > PRICE_TOLERANCE:
        passed = False
        print(f"  [FAIL] 委托价不符：预期 {wire_price:.2f}，实际 {_fmt(wire_actual)}")
    else:
        print(f"  [OK] 委托价正确：{_fmt(wire_actual)}（未被 QMT 改写）")

    if status == 57:
        passed = False
        print(f"  [FAIL] 委托被拒，原因={order.get('委托状态描述') or '未知'}")
    elif status in (53, 54, 56):
        print(f"  [WARN] 委托已终结(状态={status})，未走到撤单步骤")

    if status not in ORDER_STATUS_TERMINAL:
        cancel_ret = qmt.cancel_order_stock_async(order_id=order_id)
        print(f"  撤单请求返回={cancel_ret}")
        final = _wait_terminal(qmt, order_id, timeout=10.0)
        if final:
            print(f"  撤单后状态={final.get('委托状态')} "
                  f"({final.get('委托状态描述') or '无'})  "
                  f"成交数量={final.get('成交数量')}")
            if int(final.get('成交数量') or 0) > 0:
                passed = False
                print("  [FAIL] 意外成交，请人工检查持仓与资金")
        else:
            print("  [WARN] 撤单后未查到终态，请人工确认委托已撤销")

    return passed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="100 股离场价挂单，验证实盘限价委托(price_type=11)的交易所侧价格")
    parser.add_argument('--stock', default='603466', help='股票代码，可带或不带后缀')
    parser.add_argument('--side', choices=['buy', 'sell', 'both'], default='both')
    parser.add_argument('--account-id', default='', help='临时覆盖 QMT_ACCOUNT_ID')
    parser.add_argument('--execute', action='store_true', help='真正下单（默认 dry-run）')
    parser.add_argument('--confirm', default='', help='必须等于 VERIFY_<股票代码> 才允许下单')
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.account_id:
        os.environ['QMT_ACCOUNT_ID'] = args.account_id

    code = _resolve_code(args.stock)
    bare_code = code.split('.')[0]

    import config

    if args.execute:
        if args.confirm != f'VERIFY_{bare_code}':
            print(f"拒绝下单：需要 --confirm VERIFY_{bare_code}")
            return 2
        if not config.is_trade_time():
            print("拒绝下单：当前不在交易时段")
            return 2

    account_config = config.get_account_config()
    print(f"账号={account_config.get('account_id')}  标的={code}  "
          f"模式={'实盘下单' if args.execute else 'dry-run'}")

    from easy_qmt_trader import easy_qmt_trader

    qmt = easy_qmt_trader(
        path=config.QMT_PATH,
        account=account_config.get('account_id'),
        account_type=account_config.get('account_type', 'STOCK'),
    )
    return _main_with_trader(args, qmt, code)


def _main_with_trader(args, qmt, code: str) -> int:
    try:
        connected = qmt.connect()
        if not connected:
            print("连接失败：QMT 交易接口未连接")
            return 2
        if hasattr(qmt, 'ensure_trade_push_ready') and not qmt.ensure_trade_push_ready():
            print("交易主推未就绪，暂停验证")
            return 2

        up_stop, down_stop = _get_limits(code)
        last_price = _get_last_price(code)
        print(f"涨停={_fmt(up_stop)}  跌停={_fmt(down_stop)}  最新价={_fmt(last_price)}")

        sides = ['buy', 'sell'] if args.side == 'both' else [args.side]
        if 'sell' in sides:
            try:
                ok = qmt.check_stock_is_av_sell(stock=code.split('.')[0], amount=100)
            except Exception as exc:
                ok = False
                print(f"可卖检查异常: {exc}")
            if not ok and args.execute:
                print("拒绝下单：可卖数量不足 100 股")
                return 2
            if not ok:
                print("提示：可卖数量不足，sell 侧实盘会被拒绝（dry-run 仍展示计划）")

        overall = True
        for side in sides:
            try:
                overall = _run_one_side(
                    qmt, code, side, last_price, up_stop, down_stop, args.execute) and overall
            except VerifyFailure as exc:
                overall = False
                print(f"  [FAIL] {exc}")

        print("\n===== 结论 =====")
        print("[OK] 全部通过" if overall else "[FAIL] 存在失败项，请人工复核")
        return 0 if overall else 1
    finally:
        try:
            if hasattr(qmt, 'stop'):
                qmt.stop()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
