# 本地限价订单撮合与风控下单服务

一个**完全本地化**的限价订单撮合引擎：通过 HTTP 接口对外，所有状态只存在于
进程内存与本地 JSONL 事件日志中，不依赖任何外部行情或交易所服务。重启后依据
已持久化的事件重放，可得到与停机前完全一致的订单簿、账户与成交序列。

## 目录结构

```
app/
  models.py     领域模型：Order / Fill / Account、AccountRiskSnapshot 等快照类型、
                状态枚举、错误原因
  orderbook.py  单品种订单簿：价格优先、时间优先撮合，允许部分成交
  risk.py       组合级风控：跨品种共享资金占用、分品种持仓占用、持仓限额、
                单笔名义额、自成交预检
  eventlog.py   本地仅追加事件日志（JSONL，append + fsync）
  service.py    交易服务：编排上述组件，线程安全，含风险快照、动态限额、
                重放与守恒校验
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

# 账户组合风险快照（现金/占用/可用、各品种持仓与预计值、限额违约标志）
curl -s localhost:8000/accounts/buyer/risk

# 运行中调整限额：收紧 BTC 净持仓上限并把单笔名义额上限改为 5000
curl -s -X POST localhost:8000/admin/accounts/buyer/limits \
  -H 'Content-Type: application/json' \
  -d '{"max_position":{"BTC":"50"},"max_order_notional":"5000"}'
# max_position 中某品种传 null 表示移除该品种限额；max_order_notional 传 "Infinity"
# 表示不设单笔上限。调整落事件持久化，重启后继续生效。
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
`ORDER_ALREADY_FILLED` / `ORDER_ALREADY_CANCELLED` / `INVALID_LIMIT`，
重放阶段另有 `CORRUPT_EVENT` / `OUT_OF_ORDER_EVENT`。

## 组合级占用与风控语义

下单在**任何状态变更之前**完成以下校验，被拒绝的请求只落一条带
`reject_reason` 的 `ORDER_SUBMITTED` 事件，绝不修改订单簿、现金或持仓：

1. 账户、品种存在；价格、数量为正的有限十进制数（全程 `Decimal`，拒绝有限
   浮点输入）。
2. **在途买单共享同一份现金（组合级）**：同一账户在**所有品种**上的在途买单，
   其剩余量按各自委托价折算的名义额之和共同占用账户现金；可用现金
   `available_cash = cash − Σ(在途买单剩余量 × 委托价)`，不按品种各算一份。
   新买单名义额超过可用现金即 `INSUFFICIENT_CASH`。
3. **可用持仓按品种分别计算**：某品种可卖数量
   `available_position = 当前净持仓 − 该品种全部在途卖单剩余量`。
   卖出量超过总持仓为 `INSUFFICIENT_POSITION`（与“可用不足但总持仓足够”
   使用同一原因码，消息中给出区分）。
4. **持仓限额（组合预计口径）**：校验时把该品种上已有的在途买单与在途卖单
   一并计入预计净持仓
   `projected = 当前持仓 + 在途买单剩余量 − 在途卖单剩余量 ± 新单量`，
   要求 `|projected| ≤ max_position[symbol]`，否则 `RISK_LIMIT_EXCEEDED`。
   因此跨品种资金、同品种双向挂单都不会被重复计算或漏算。
5. **单笔名义额**：`price × quantity ≤ max_order_notional`，超限返回
   `RISK_LIMIT_EXCEEDED`。
6. 自成交预检见上节。

**占用释放**：撤单立即释放该单全部占用；拒单从不产生占用；成交按实际成交量
释放——部分成交时，已成交部分由“在途占用”转为真实的现金扣划/持仓划转，
仅剩余量继续占用。所有计算都基于订单的**剩余量（remaining）**，
因此不存在“已成交部分仍被占用”的重复占用。

服务用单一 `RLock` 串行化所有提交/撤单/快照查询，且撤单的内存移除与事件落盘
对外部原子可见（落盘失败会回滚内存）：并发下单、撤单、成交交错时，
同一笔资金或同一批可用持仓不会被重复占用，快照也读不到中间态。

## 账户风险快照

`GET /accounts/{account_id}/risk`（或 `TradingService.risk_snapshot()`）在
**同一把锁内一次性汇总**订单簿与账户状态，返回与最终状态一致的组合视图，
不会与订单簿/账户出现半拍偏差。字段含义：

账户级：

| 字段 | 含义 |
| --- | --- |
| `cash` | 当前现金（已成交部分已扣划） |
| `reserved_buy_notional` | 跨**所有品种**在途买单的名义额占用合计 |
| `available_cash` | 可用现金 = `cash − reserved_buy_notional` |
| `projected_cash` | 所有在途买单按各自委托价全部成交后的现金（= `available_cash`） |
| `max_order_notional` | 单笔名义额上限 |
| `order_notional_breaches` | 当前名义额超过单笔上限的在途订单 id（限额收紧后可能出现） |
| `cash_limit_breached` / `projected_cash_limit_breached` | 当前/在途全成交后现金是否为负 |
| `any_breach` | 下列任一当前或预计违约标志为真 |

各品种（`positions[]`，覆盖全部已注册品种及有持仓/挂单/限额的品种）：

| 字段 | 含义 |
| --- | --- |
| `position` | 当前净持仓（买正卖负） |
| `reserved_sell` | 该品种在途卖单占用的数量 |
| `available_position` | 可卖数量 = `position − reserved_sell` |
| `max_position` | 该品种净持仓绝对值上限，`null` 表示不限 |
| `projected_position` | 该品种在途买卖单全部成交后的净持仓 |
| `position_limit_breached` | 当前 `|position|` 是否已超限 |
| `projected_position_limit_breached` | 在途订单全部成交后是否超限 |

## 动态限额及其生效边界

`POST /admin/accounts/{account_id}/limits`（或 `TradingService.adjust_limits()`）
可在运行中调整各品种 `max_position` 与 `max_order_notional`：

- 调整作为一条 `LIMITS_ADJUSTED` 事件持久化（落的是合并后的**完整**限额快照），
  **服务重启重放后继续生效**。非法限额（负数、零、NaN）返回 `INVALID_LIMIT`。
- **只影响边界，不追溯存量**：收紧限额**不会强制取消**任何已有在途订单，
  也不立即回滚已成交持仓；但：
  - 后续**新订单**立即按新限额预检，超限整单拒绝且零副作用；
  - 风险快照通过 `projected_position_limit_breached`、
    `order_notional_breaches` 等字段，明确标出已有在途订单全部成交后是否会超限，
    以及哪些存量挂单已超过新的单笔名义额上限。
- 限额调整与下单、撤单、成交在同一把锁上串行，任一时点不会出现同一笔资金或
  同一批可用持仓被重复占用。

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
`LIMITS_ADJUSTED`（运行中限额调整，载荷为合并后的完整 `max_position` 与
`max_order_notional`）。

写入协议（崩溃一致性）：**先把完整事件序列（提交事件及其全部成交事件）
fsync 落盘，再更新内存状态**。因此不会出现“内存已成交而日志缺失”的幻影成交；
极端情况下崩溃在落盘之后、更新内存之前，重启重放会补上该笔成交。撤单则相反，
先在锁内完成内存移除（对外部读原子可见），落盘失败时回滚内存，
不会对外确认一笔未持久化的撤单。

**与既有事件重放的兼容范围**：`LIMITS_ADJUSTED` 是纯增量事件类型，
旧日志（不含该事件）可原样重放，账户沿用 `ACCOUNT_OPENED` 上的初始限额；
新日志中的限额调整按事件顺序覆盖，重放结果与停机前逐字段一致
（包括风险快照中的占用、预计值与违约标志，见
`tests/test_risk_portfolio.py`）。

重放在启动时自动执行（`TradingService.replay()`），并做严格校验，
**任何不一致都明确抛错，绝不静默跳过或覆盖已有状态**：

- 事件 `seq` 必须从 1 开始严格连续递增；缺口、重复、乱序 => `OUT_OF_ORDER_EVENT`。
- 派生 ID 必须与位置一致：`O{seq}`、`F{seq}`；引用的账户/品种/订单必须存在；
  成交双方方向自洽（一买一卖、非同账户）、成交价同时满足两笔限价、
  成交量为正且累计不超委托量；成交/撤单事件必须严格晚于对应订单的提交事件，
  且发生时订单仍在订单簿中 => `CORRUPT_EVENT` / `OUT_OF_ORDER_EVENT`。
- JSON 行损坏或缺必填字段 => `CORRUPT_EVENT`；未知事件类型同样拒绝。
- `LIMITS_ADJUSTED` 必须引用已存在账户，品种限额为非负有限十进制，
  单笔名义额为正数或 `Infinity`，否则 => `CORRUPT_EVENT`。
- 重放结束后执行守恒断言：成交序列与事件流逐条一致、每单累计成交量与订单
  `filled_qty` 相等、被拒单零成交、全系统现金守恒、各品种净持仓等于累计入券量。

成功重放后得到的订单簿档位、订单状态、成交序列、账户现金/持仓与停机前完全一致，
随后的新事件从最大序号继续分配。

## 本地验证

```bash
uv run pytest -q                 # 全部 39 个用例
uv run pytest tests/test_risk_portfolio.py -q   # 仅本轮组合风险相关用例
```

测试分四组，且通过日志（stdout/stderr）打印**输入参数、关键状态快照与判定依据**：

- `tests/test_matching.py`：全部/部分成交、价格-时间优先、不成交挂单、
  撤单与重复撤单、自成交、资金/持仓不足、非法输入与守恒断言。
- `tests/test_risk_portfolio.py`（本轮新增）：
  - 同一账户跨品种在途买单共享同一份现金（超池拒绝、被拒零副作用）；
  - 同品种在途卖单共同占用可用持仓、不同品种互不影响；
  - 运行中收紧持仓限额/单笔名义额：存量单不撤、快照标出预计违约、
    后续新单被整单拒绝且订单簿/成交/账户零副作用；
  - 撤单与部分成交只释放对应占用（已成交部分转为真实划转，剩余继续占用）；
  - 16 线程并发下单 + 并发撤单交错下快照恒等式成立、现金池不超额订阅；
  - 跨“进程”重启重放后风险快照、订单簿、成交、限额调整逐字段一致，
    并经 FastAPI TestClient 验证新 HTTP 端点的持久化。
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
