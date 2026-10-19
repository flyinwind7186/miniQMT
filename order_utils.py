"""委托（Order）相关的共享常量与格式化工具。

web_server.py（Flask 直连）和 xtquant_manager/server.py（网关）都要把
QMT 原始委托对象映射成前端字段，此处收口以免两边各写一份、口径漂移。
"""
from datetime import datetime
import math

# 委托状态码 → 中文描述
ORDER_STATUS_DESC = {
    48: "未报",
    49: "待报",
    50: "已报",
    51: "已报待撤",
    52: "部成待撤",
    53: "部撤",
    54: "已撤",
    55: "部成",
    56: "已成",
    57: "废单",
}

# 活跃（在途）委托状态码：已提交但尚未终结
ACTIVE_ORDER_STATUS = frozenset({48, 49, 50, 51, 52, 55})

# 委托类型码
ORDER_TYPE_BUY = 23
ORDER_TYPE_SELL = 24


def format_order_time(value):
    """格式化委托时间为 'YYYY-MM-DD HH:MM:SS'。

    QMT 的 order_time 是 Unix 秒级时间戳(int)，不能按字符串日期解析；
    同时兼容已经是字符串/datetime 的情形。无法解析时返回 None。
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()
    if not text:
        return None
    # 纯数字字符串同样按时间戳处理
    if text.isdigit():
        return format_order_time(int(text))

    normalized = text.replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(normalized, fmt).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
    return text


def is_pending(status):
    """委托是否处于在途（已报未成交/部成）状态。"""
    return status in ACTIVE_ORDER_STATUS


def status_desc(status, fallback=''):
    """委托状态描述，优先使用 QMT 回传的 status_msg。"""
    return fallback or ORDER_STATUS_DESC.get(status, '未知')


def sort_orders(orders):
    """就地排序：在途委托整体在前，组内按时间倒序（缺时间排最后）。"""
    orders.sort(key=lambda o: o.get('order_time') or '', reverse=True)
    orders.sort(key=lambda o: not o.get('is_pending'))
    return orders


def position_quantities(position):
    """严格读取持仓数量；缺失、非整数或数量关系非法时返回 None。"""
    if not isinstance(position, dict):
        return None
    values = []
    for english, chinese in (('volume', '股票余额'), ('available', '可用余额')):
        value = position.get(english, position.get(chinese))
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            number = float(value)
        except (ValueError, TypeError):
            return None
        if not math.isfinite(number) or number < 0 or not number.is_integer():
            return None
        values.append(int(number))
    volume, available = values
    return (volume, available) if available <= volume else None


def calculate_sell_volume(available, ratio, *, lot_size):
    """按可用计算卖量；清卖允许零股，部分卖出遵守原有整手口径。"""
    quantities = position_quantities({'volume': available, 'available': available})
    if quantities is None or isinstance(ratio, bool):
        return 0
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(ratio) or not 0 < ratio <= 1 or lot_size <= 0:
        return 0
    available = quantities[1]
    if ratio == 1 or available < lot_size:
        return available
    return min(available, max(lot_size, int(available * ratio) // lot_size * lot_size))


def is_restricted_stop_loss(position, current_price, stop_loss_ratio):
    """股份受限且当前达到固定止损时，临时阻断新增自动买入。"""
    quantities = position_quantities(position)
    if quantities is None or quantities[0] <= quantities[1]:
        return False
    if position.get('stop_profit_enabled') is False:
        return False
    try:
        cost = float(position.get('cost_price') or position.get('成本价') or 0)
        price = float(current_price or position.get('current_price') or 0)
        return (math.isfinite(cost) and math.isfinite(price) and cost > 0 and price > 0
                and price <= cost * (1 + stop_loss_ratio))
    except (TypeError, ValueError):
        return False


def classify_position_restriction(position, *, trades=None, orders=None,
                                  position_asof=None, fresh=True, today=None):
    """用同日成交和活跃卖单解释差额；证据不完整时保留待确认。"""
    today = today or datetime.now().date()
    position_asof = position_asof or position.get('position_asof')
    if position_asof:
        try:
            fresh = fresh and datetime.fromisoformat(str(position_asof)).date() == today
        except ValueError:
            fresh = False
    result = {'unavailable_volume': None, 'restriction_reason': 'unknown',
              'restriction_detail': '不可卖原因待确认', 'position_asof': position_asof}
    quantities = position_quantities(position)
    if quantities is None:
        result['restriction_detail'] = '持仓数量待确认'
        return result
    volume, available = quantities
    result['unavailable_volume'] = volume - available
    if not fresh:
        result['restriction_detail'] = '持仓数据待刷新'
        return result
    if volume == available:
        result.update(restriction_reason='none', restriction_detail='无受限股份')
        return result

    def records(value):
        if hasattr(value, 'to_dict') and hasattr(value, 'columns'):
            converted = value.to_dict('records')
            return converted if isinstance(converted, list) else None
        return value if isinstance(value, (list, tuple)) else None

    def field(record, *names):
        for name in names:
            value = record.get(name) if isinstance(record, dict) else getattr(record, name, None)
            if value is not None:
                return value
        return None

    code = str(position.get('stock_code') or position.get('证券代码') or '')[:6]
    account = position.get('account_id') or position.get('资金账号')
    trades, orders = records(trades), records(orders)
    bought, reserved = 0, 0
    seen = set()
    try:
        for trade in trades or []:
            record_account = field(trade, 'account_id', '资金账号')
            if account and record_account is not None and str(record_account) != str(account):
                continue
            if str(field(trade, 'stock_code', '证券代码'))[:6] != code:
                continue
            if int(field(trade, 'order_type', '委托类型') or 0) != ORDER_TYPE_BUY:
                continue
            when = field(trade, 'traded_time', '成交时间')
            formatted = format_order_time(when) if when is not None else None
            if formatted and formatted[:10] != today.isoformat():
                continue
            identity = field(trade, 'traded_id', '成交编号')
            if identity is not None:
                if str(identity) in seen:
                    continue
                seen.add(str(identity))
            bought += max(0, int(field(trade, 'traded_volume', '成交数量') or 0))
        for order in orders or []:
            record_account = field(order, 'account_id', '资金账号')
            if account and record_account is not None and str(record_account) != str(account):
                continue
            if str(field(order, 'stock_code', '证券代码'))[:6] != code:
                continue
            if (int(field(order, 'order_type', '委托类型') or 0) == ORDER_TYPE_SELL
                    and int(field(order, 'order_status', '委托状态') or 0) in ACTIVE_ORDER_STATUS):
                reserved += max(0, int(field(order, 'order_volume', '委托数量') or 0)
                                - int(field(order, 'traded_volume', '成交数量') or 0))
    except (TypeError, ValueError, OverflowError):
        return result
    unavailable = volume - available
    ordinary_stock = len(code) == 6 and code.isdigit() and code.startswith(
        ('00', '30', '60', '68', '4', '8', '92'))
    if orders is not None and reserved == unavailable and (not bought or not ordinary_stock):
        result.update(restriction_reason='sell_order', restriction_detail='未完成卖单占用')
    elif (position_asof and ordinary_stock and trades is not None and orders is not None
          and bought > 0 and bought + reserved == unavailable):
        reason = 'mixed' if reserved else 't1'
        result.update(restriction_reason=reason,
                      restriction_detail='当日买入受限及卖单占用' if reserved else '当日买入，T+1 暂不可卖')
    return result
