# 自动买入模块

`miniqmt_autobuy` 是独立进程模块，负责从外部候选池中筛选标的并复用 miniQMT 的 Web 买入 API 下单。它不直接改主程序持仓状态；买入完成后，后续持仓同步、止盈止损和网格接管仍由主程序负责。

---

## 工作流程

```text
候选池 SQLite
  -> 最近 N 个交易日多表并集
  -> 大盘指数门禁
  -> 已持仓/历史买入防重
  -> 洗牌后惰性条件检查
  -> POST /api/actions/execute_buy
  -> data/autobuy.db 记录复盘
```

### 关键设计

- **独立进程**：通过 `python -m autobuy.app` 运行，由 `miniqmt.bat` 菜单管理。
- **下单复用 Web API**：最终下单走目标账号 `web_server.py` 的 `/api/actions/execute_buy`，因此需要目标账号 Web 服务已启动。
- **大候选池惰性求值**：候选可能上百只，模块先洗牌，再逐只检查，达到 `max_buys_per_run` 后停止，避免无意义拉取全量行情。
- **防重安全优先**：如果 `/api/positions` 持仓查询失败，本轮不下单，避免重复买入。

---

## 启动与管理

```bash
miniqmt.bat
```

菜单入口：

| 菜单 | 功能 |
|------|------|
| `[j]` | 启动自动买入服务（实盘，会调用 Web API 下单） |
| `[v]` | 启动自动买入服务（模拟，只筛选和记录，不下单） |
| `[k]` | 停止自动买入服务 |
| `[l]` | 查看状态（`data/.autobuy_status.json`） |
| `[m]` | 查看日志（`logs/miniqmt_autobuy.log`） |

手动单次触发：

```bash
python -m autobuy.app --once --simulate
```

!!! warning "启动前检查"
    先启动目标账号主程序或 web1.0 Flask 服务。通过菜单 `[j]` / `[v]` 启动时，控制台会自动探测运行中账号的实际 Flask 端口并注入 `MINIQMT_AUTOBUY_BASE_URL`；直接运行模块时才需要确认 `[web].base_url`。`api_token` 留空时会回退到 `QMT_API_TOKEN` 环境变量或项目 `.env`。

---

## 配置文件

配置文件位于 `autobuy/miniqmt_autobuy.cfg`，INI 格式，修改后需重启 autobuy 进程。

### Web 下单通道

| 参数 | 说明 |
|------|------|
| `base_url` | 目标账号 Web 服务地址，如 `http://127.0.0.1:5000` |
| `api_token` | 对应主程序环境变量 `QMT_API_TOKEN`，未启用鉴权时留空 |
| `timeout` | HTTP 请求超时秒数 |

### 候选池

| 参数 | 说明 |
|------|------|
| `db_path` | 外部候选池 SQLite 文件路径 |
| `tables` | 候选表名，多表并集，逗号分隔 |
| `code_column` / `date_column` | 股票代码列和日期列 |
| `latest_n_dates` | 每张表各自取运行日前最近 N 个交易日 |

候选代码支持 `sh.600025` / `sz.000626` 这类前缀格式，模块会转换为系统标准 `600025.SH` / `000626.SZ`。

### 筛选条件

| 条件 | 配置 |
|------|------|
| 大盘指数门禁 | 固定检查 `999999` / `399001` / `399005`，至少一个指数 MA5 向上才继续 |
| 换手率 | `enable_turnover_rate` / `min_turnover_rate` / `volume_unit_multiplier` |
| 近 N 日收盘量比 | `enable_recent_volume_ratio` / `recent_volume_ratio_days` / `min_recent_volume_ratio`，默认开启且要求每天都达标 |
| 盘中累计量比 | `enable_volume_ratio` / `min_volume_ratio`，因盘中分子与全天分母口径不对等，默认关闭 |
| 当日涨幅 | `enable_pct_change` / `min_pct_change`，默认关闭 |
| MA8 方向 | `enable_ma8_uptrend` |
| 现价相对 MA8 | `enable_price_below_ma8_ratio` / `max_price_to_ma8_ratio` |
| MA20 偏离区间 | `enable_price_to_ma20_range` / `min_price_to_ma20_deviation` / `max_price_to_ma20_deviation`，默认 `[-3%, +5%]` |
| 涨停/停牌 | `skip_limit_up` |
| 风险股 | `skip_st`，按证券名称前缀过滤 ST、*ST 和退市整理股 |

### 风控与调度

| 参数 | 说明 |
|------|------|
| `dedup_by_position` | 已持仓则跳过 |
| `dedup_window_days` | 最近 N 天买过则跳过；`0` = 当天，`-1` = 永久 |
| `max_buys_per_run` | 每次触发最多买入数量 |
| `simulation_mode` | 模拟运行；保留筛选、门禁、决策与状态记录，不发送真实买入请求 |
| `mode` | `daily` / `interval` / `both` |
| `daily_times` | 每日定点时间，逗号分隔（默认 `14:40`） |
| `interval_minutes` | 固定间隔分钟数 |
| `only_trade_time` | 仅真实交易时段触发，叠加 `config.is_market_hours()` 时段判定与交易日历节假日判定 |

### 交易日历

A 股法定节假日（春节、国庆等连休 5~9 天）不能按“周一至周五”判断，否则整个长假会被当成交易日全天误触发。

- 服务**启动时**从 Tushare `trade_cal` 无条件拉取一次并写入 `data/autobuy_trade_calendar.db`（回看 30 天 + 前推 370 天），运行期只读本地缓存，不联网。
- 候选池的“最近 N 个交易日”与 `only_trade_time` 的节假日判定共用这份日历。
- 取数失败或日期在缓存覆盖范围外时退化为“周一至周五”并打 WARNING；取数失败不写缓存，不会用近似值覆盖已有的权威日历。
- 缓存路径可用环境变量 `MINIQMT_AUTOBUY_CALENDAR_DB` 覆盖。

---

## 复盘数据

运行数据写入项目根目录：

| 文件 | 说明 |
|------|------|
| `logs/miniqmt_autobuy.log` | 自动买入运行日志（含启动配置摘要与交易日历来源） |
| `data/.autobuy_status.json` | 最近一轮状态摘要，供菜单 `[l]` 读取 |
| `data/autobuy.db` | 买入历史与决策日志 |
| `data/autobuy_trade_calendar.db` | 交易日历缓存，启动时刷新 |

`data/autobuy.db` 主要包含：

- `buy_history`：每次买入尝试、触发源、HTTP 状态、订单结果、金额。
- `decision_log`：实际检查过的标的及条件明细。由于采用惰性求值，未检查的候选不会写入该表。

---

## 端到端验证

1. 确认外部候选池 `chan.db` 路径、表名、列名正确。
2. 启动目标账号主程序，确认 `GET /api/positions` 可访问。
3. 运行 `python -m autobuy.app --once --simulate` 做单次安全验证。
4. 查看 `logs/miniqmt_autobuy.log`，确认大盘门禁、候选数量、通过数量和下单结果。
5. 查看 `data/autobuy.db`，复核 `buy_history` 与 `decision_log`。

---

## 常见注意事项

- 候选池数据量较大时，将 `latest_n_dates` 调小到 `1` 可以明显降低检查量。
- `volume_unit_multiplier` 默认按“手”转“股”处理；如果数据源成交量已是股，应改为 `1`。
- 科创板/创业板标的需要账户权限；无权限或最小交易单位不满足时由 QMT 拒单，模块只记录结果。
- 自动买入只负责“买入入口”，风险控制仍依赖主程序配置，如 `POSITION_UNIT`、止盈止损和最大持仓限制。
