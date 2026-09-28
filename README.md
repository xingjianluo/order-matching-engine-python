# 本地限价订单撮合与风控下单服务

一个**完全本地化**的限价订单撮合引擎：通过 HTTP 接口对外，所有状态只存在于
进程内存与本地 JSONL 事件日志中，不依赖任何外部行情或交易所服务。重启后依据
已持久化的事件重放，可得到与停机前完全一致的订单簿、账户与成交序列。

## 目录结构

```
app/
  models.py     领域模型：Order / Fill / Account、风险快照、状态枚举、错误原因
  orderbook.py  单品种订单簿：价格优先、时间优先撮合，允许部分成交
  risk.py       组合级风控：跨品种共享现金占用、按品种占用持仓、持仓/单笔限额、自成交预检
  eventlog.py   本地仅追加事件日志（JSONL，append + fsync）
  service.py    交易服务：编排上述组件，线程安全，含风险快照、动态限额、重放与守恒校验
  api.py        FastAPI 路由与请求/响应模型
main.py         入口：启动时加载 data/events.jsonl 并重放
tests/          pytest 测试（撮合 / 组合风险 / 并发风控幂等 / 重放恢复 + HTTP 端到端）
```

## 快速开始

```bash
uv sync                       # 安装依赖（Python >= 3.14）
uv run uvicorn main:app --port 8000   # 启动（首次会创建 data/events.jsonl）
```

服务首次启动需要通过管理接口开户、注册品种并入金/入券（这些操作同样写事件，
可被重放）：

```bash
# 开户（cash 现金；max_position 为各品种净持仓绝对值上限）
curl -s -X POST localhost:8000/admin/accounts -H 'Content-Type: application/json' \
  -d '{"account_id":"buyer","cash":"100000"}'
curl -s -X POST localhost:8000/admin/accounts -H 'Content-Type: application/json' \
  -d '{"account_id":"seller","cash":"0","max_position":{"BTC":"1000"}}'
curl -s -X POST localhost:8000/admin/symbols/BTC
curl -s -X POST localhost:8000/admin/deposits -H 'Content-Type: application/json' \
  -d '{"account_id":"seller","symbol":"BTC","quantity":"100"}'

# 下单：price/quantity 使用字符串十进制（Decimal），禁止浮点
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"client_order_id":"c1","symbol":"BTC","side":"SELL",
       "price":"100","quantity":"10","account":"seller"}'
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"client_order_id":"c2","symbol":"BTC","side":"BUY",
       "price":"100","quantity":"4","account":"buyer"}'

# 撤单 / 查询 / 订单簿 / 成交 / 账户
curl -s -X POST localhost:8000/orders/cancel -H 'Content-Type: application/json' \
  -d '{"order_id":"O5"}'
curl -s localhost:8000/orders/O5
curl -s localhost:8000/books/BTC
curl -s localhost:8000/fills?symbol=BTC
curl -s localhost:8000/accounts/buyer
# 组合风险快照：现金/在途买占用/可用现金、各品种持仓与在途卖占用、预估成交后状态
curl -s localhost:8000/accounts/buyer/risk
# 运行中收紧持仓限额（按品种合并）与单笔名义额上限；不影响已有在途单
curl -s -X POST localhost:8000/admin/limits -H 'Content-Type: application/json' \
  -d '{"account_id":"buyer","max_position":{"BTC":"8"},"max_order_notional":"50000"}'
```

## 撮合规则

- 仅限价单。订单进入时立即与对手盘撮合，未成交（或部分成交后剩余）的数量挂入簿中。
- **价格优先**：买单按价格从高到低、卖单按价格从低到高依次成交；
  **时间优先**：同一价位上按进入订单簿的先后（全局 `sequence`）FIFO。
- 撮合是连续、即时的（无开盘集合竞价）。买单在 `对手卖价 <= 买价` 时成交，
  卖单在 `对手买价 >= 卖价` 时成交；**成交价取挂单方价格**（吃单吃到对手价）。
- **允许部分成交**：每次按 `min(吃单剩余, 挂单剩余)` 成交，产生一条 `FILL` 事件；
  一笔吃单可连续与多个价位/多笔挂单成交。订单状态在
  `NEW / PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED` 间迁移。
- **自成交拒绝**：同一账户的买卖订单若会互相成交，吃单整体拒绝
  （`SELF_TRADE`），且由于撮合采用“先规划（无副作用）后提交”的两阶段方式，
  被拒订单不会成交任何一笔，挂单方订单与订单簿保持不变。
- **数量守恒**：任意操作前后，每笔订单累计成交量不超过其委托量；全市场每个品种
  买方增加的持仓与卖方减少的持仓相等；成交仅在账户间划转现金与持仓，
  全系统现金总额、各品种持仓总额不变（入金/入券除外）。

拒绝原因（`reject_reason`，可区分）：
`UNKNOWN_SYMBOL` / `INVALID_PRICE` / `INVALID_QUANTITY` / `UNKNOWN_ACCOUNT` /
`INSUFFICIENT_POSITION` / `INSUFFICIENT_CASH` / `RISK_LIMIT_EXCEEDED` /
`SELF_TRADE` / `DUPLICATE_IDEMPOTENCY_KEY` / `ORDER_NOT_FOUND` /
`ORDER_ALREADY_FILLED` / `ORDER_ALREADY_CANCELLED`，
重放阶段另有 `CORRUPT_EVENT` / `OUT_OF_ORDER_EVENT`。

## 风控语义（组合级占用）

下单在**任何状态变更之前**完成以下校验，被拒绝的请求只落一条带
`reject_reason` 的 `ORDER_SUBMITTED` 事件，绝不修改订单簿、现金或持仓
（整单拒绝：订单簿、成交记录、账户状态三者都不变）。

**占用口径（同一账户、多品种组合视图）：**

- **现金按账户统一占用**：该账户在**全部品种**上的在途买单，其剩余数量按各自
  委托价折算的名义额 `remaining × price` 合计，共同占用同一份现金：
  `可用现金 = cash − Σ所有品种在途买单名义额`。BTC 上挂的买单会挤占 ETH 买单
  可用的现金，绝不会按品种各算一份。
- **持仓按品种分别占用**：在途卖单只占用**本品种**的可卖数量：
  `可用持仓(symbol) = 当前持仓(symbol) − Σ该品种在途卖单剩余量`。
- 占用随订单生命周期即时变化：
  - 撤单：立即释放该订单全部占用；
  - 拒单：从不占用（预检在落簿之前）；
  - 成交：只释放**已成交部分**——买单按成交均价实际扣减现金、卖单实际交割
    持仓，剩余未成交部分继续占用，直到完全成交或撤销。

**新单准入校验（任一不过则整单拒绝）：**

1. 账户、品种存在；价格、数量为正的有限十进制数（全程 `Decimal`，拒绝浮点输入）。
2. **卖单**：卖出量先不得超过总持仓，再不得超过
   “持仓 − 该品种在途卖单占用”（区分 `INSUFFICIENT_POSITION`）。
3. **买单**：委托名义额 `price × quantity` 不得超过
   “现金 − 全品种在途买单占用”（`INSUFFICIENT_CASH`）。
4. **持仓限额**：`max_position[symbol]` 约束的是**该品种全部在途买卖单都成交后**
   的预估净持仓绝对值：买单看 `|持仓 + 在途买量 + 本单 − 在途卖量|`，
   卖单看 `|持仓 + 在途买量 − 在途卖量 − 本单|`；同账户买卖不会互相对手
   （自成交预检拦截），因此在途买、卖量都参与预估。超限 `RISK_LIMIT_EXCEEDED`。
5. **单笔名义额**：`max_order_notional` 约束单笔订单名义额，超限同样
   `RISK_LIMIT_EXCEEDED`。
6. 自成交预检见“撮合规则”。

服务用单一 `RLock` 串行化所有提交/撤单/成交/限额调整/查询：并发场景下不会出现
同一笔资金或同一批可用持仓被重复占用、超卖、同一挂单被吃两次、超限漏检等问题。

### 风险快照

`GET /accounts/{account_id}/risk` 返回账户在**某一串行化时点**的组合风险视图，
字段与订单簿、账户最终状态严格一致（同一把锁内汇总，看不到中间态）：

| 字段 | 含义 |
| --- | --- |
| `cash` | 当前现金 |
| `reserved_buy_notional` | 全品种在途买单剩余名义额合计（现金总占用） |
| `available_cash` | `cash − reserved_buy_notional`，新买单真正可用现金 |
| `projected_cash` | 按各在途单委托价估算“全部成交后”的现金（买单花钱、卖单收钱） |
| `cash_breached` / `projected_cash_breached` | 当前 / 预估成交后现金是否为负 |
| `max_order_notional` | 当前生效的单笔名义额上限 |
| `over_capped_orders` | 名义额已超过当前上限的在途订单 id（收紧后的提示，不强制撤单） |
| `current_limit_breached` | 是否已有品种当前净持仓严格超限 |
| `projected_limit_breached` | 是否有品种在途单全部成交后预估严格超限 |
| `symbols[*].position` | 该品种当前净持仓（买正卖负） |
| `symbols[*].reserved_sell` | 该品种在途卖单剩余量合计 |
| `symbols[*].available_position` | 当前持仓 − 在途卖单占用（可再卖数量） |
| `symbols[*].resting_buy_qty` | 该品种在途买单剩余量合计 |
| `symbols[*].projected_position` | 预估全部成交后净持仓（持仓 + 在途买 − 在途卖） |
| `symbols[*].max_position` | 该品种当前持仓限额（未配置为 `null`） |
| `symbols[*].limit_breached` / `projected_limit_breached` | 当前 / 预估是否严格超限 |

### 动态限额（运行中调整）

`POST /admin/limits`（事件 `LIMITS_ADJUSTED`，先 fsync 落盘再生效、重启后继续
有效）可调整 `max_position`（按品种合并覆盖；`reset_positions=true` 时整体替换）
与 `max_order_notional`。生效边界：

- **不追溯**：收紧限额不取消、不改动任何已有在途订单，它们仍可正常成交或撤销；
- **管新增**：调整只对之后提交的新单做准入拦截；
- **可观测**：收紧后风险快照的 `projected_limit_breached` 会标出在途单全部成交后
  是否超限，`over_capped_orders` 列出超过新单笔上限的旧在途单；
- 调整与下单/撤单/成交在同一把锁内串行，任一时点占用都不会被重复计算。

## 幂等语义

- 每个下单请求必须携带 **`client_order_id`（幂等标识）**。
- 相同 `client_order_id` + 完全相同参数的重复提交（无论并发还是重启后）
  **返回同一个订单、相同状态，绝不产生第二次成交**。
- 相同 `client_order_id` 但价格/数量/方向/账户/品种任一不同：返回
  `DUPLICATE_IDEMPOTENCY_KEY`，拒绝执行。
- 撤单是对订单当前状态的操作而非幂等创建：对已成交单撤单返回
  `ORDER_ALREADY_FILLED`，对已撤/被拒单撤单返回 `ORDER_ALREADY_CANCELLED`，
  对不存在的订单返回 `ORDER_NOT_FOUND`，原因可区分。

HTTP 状态码约定：下单被业务规则拒绝时返回 **200**，响应体
`status="REJECTED"` 且带 `reject_reason`（这是一次“已受理但未通过风控”的
确定性结果，也参与幂等重放）；撤单/查询类操作错误返回 **422** 且带 `reason`。

## 事件与重放恢复约束

所有状态变更只通过追加事件完成，事件类型：
`ACCOUNT_OPENED` / `SYMBOL_REGISTERED` / `DEPOSIT` /
`ORDER_SUBMITTED`（含 `reject_reason`）/ `FILL` / `ORDER_CANCELLED` /
`LIMITS_ADJUSTED`（运行中限额调整）。

写入协议（崩溃一致性）：**先把完整事件序列（提交事件及其全部成交事件）
fsync 落盘，再更新内存状态**。因此不会出现“内存已成交而日志缺失”的幻影成交；
极端情况下崩溃在落盘之后、更新内存之前，重启重放会补上该笔成交。

重放在启动时自动执行（`TradingService.replay()`），并做严格校验，
**任何不一致都明确抛错，绝不静默跳过或覆盖已有状态**：

- 事件 `seq` 必须从 1 开始严格连续递增；缺口、重复、乱序 => `OUT_OF_ORDER_EVENT`。
- 派生 ID 必须与位置一致：`O{seq}`、`F{seq}`；引用的账户/品种/订单必须存在；
  成交双方方向自洽（一买一卖、非同账户）、成交价同时满足两笔限价、
  成交量为正且累计不超委托量；成交/撤单事件必须严格晚于对应订单的提交事件，
  且发生时订单仍在订单簿中 => `CORRUPT_EVENT` / `OUT_OF_ORDER_EVENT`。
- JSON 行损坏或缺必填字段 => `CORRUPT_EVENT`；未知事件类型同样拒绝。
- 重放结束后执行守恒断言：成交序列与事件流逐条一致、每单累计成交量与订单
  `filled_qty` 相等、被拒单零成交、全系统现金守恒、各品种净持仓等于累计入券量。

**与既有事件重放的兼容范围**：风险占用（现金/持仓预留）本身**不单独落事件**，
它是订单簿在途订单的派生量——重放 `ORDER_SUBMITTED` / `FILL` /
`ORDER_CANCELLED` 后由在途订单重新汇总，因此老日志（不含任何新事件类型）
可直接重放，重算出的风险快照与停机前一致。动态限额通过 `LIMITS_ADJUSTED`
事件持久化，旧版本写入的日志没有该事件时，账户限额停留在 `ACCOUNT_OPENED`
携带的初始值；包含该事件的新日志由旧版本重放会按“未知事件类型”明确拒绝
（不会静默忽略）。

成功重放后得到的订单簿档位、订单状态、成交序列、账户现金/持仓、动态限额与
停机前完全一致，随后的新事件从最大序号继续分配。

## 本地验证

```bash
uv run pytest -q                 # 全部 40 个用例
```

测试分四组，且通过日志（stdout/stderr）打印**输入参数、关键状态快照与判定依据**：

- `tests/test_matching.py`：全部/部分成交、价格-时间优先、不成交挂单、
  撤单与重复撤单、自成交、资金/持仓不足、非法输入与守恒断言。
- `tests/test_portfolio_risk.py`：跨品种在途买单共享同一份现金、同品种在途卖单
  共同占用可用持仓、风险快照各字段与“全部成交后”预估、动态收紧持仓限额/单笔
  上限后的拒绝零副作用（旧在途单不撤销）、撤单与部分成交只释放对应占用、
  并发下单/撤单与快照交错下的占用一致性、以及重启重放后以上结果与限额保持一致。
- `tests/test_concurrency.py`：16 线程下撤单与成交竞争（只允许一方生效）、
  100 个并发买单不得超卖 10 的挂量、同一幂等键 50 并发只成交一次、
  持仓限额拒绝的零副作用、混合 300 次读写无内部错误。
- `tests/test_replay.py`：跨“进程”重放状态完全一致、重放幂等、
  seq 缺口/重复/起始错误、成交超量、引用未知订单、成交早于提交、
  撤销已成交单、未知事件类型、损坏 JSON 等均被明确拒绝；
  以及经 FastAPI TestClient 的端到端持久化与重启验证。

查看持久化日志（每行一条事件）：

```bash
cat data/events.jsonl | python3 -m json.tool
```
