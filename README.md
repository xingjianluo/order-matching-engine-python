# 本地限价订单撮合与风控下单服务

一个**纯本地**的限价订单簿撮合引擎：通过 HTTP 暴露，所有状态仅存在于进程内存与本地
仅追加事件日志（JSONL）中，不依赖任何外部行情、交易所或数据库服务。进程重启后依据
已持久化的事件严格重放，重建出与停机前完全一致的订单簿、账户与成交序列。

- 语言/框架：Python 3.14 + FastAPI（uv 管理依赖）
- 持久化：本地 `append-only` JSONL 事件日志，写入即 `fsync`
- 并发模型：单进程 + 单把可重入锁串行化所有写操作（提交 / 撤单），查询不阻塞彼此语义

## 目录结构

```
main.py                 # uvicorn 入口：app = create_app()
app/
  models.py             # 领域类型、DTO、事件与 payload 模型（Decimal 计价）
  errors.py             # 可区分的业务错误 + 重放错误 + 原因到 HTTP 状态码映射
  store.py              # 仅追加 JSONL 事件存储（seq 分配、fsync、行级解码）
  engine.py             # 撮合 + 风控状态机（锁、事件 apply、重放校验、幂等重建）
  service.py            # 装配存储/引擎，启动时重放
  api.py                # HTTP 路由、lifespan、统一错误响应
tests/
  test_smoke.py         # HTTP 端到端 + 重启
  test_matching.py      # 撮合规则 / 部分成交 / 自成交 / 重复撤单 / 守恒
  test_risk_concurrency.py  # 风控限额 / 幂等 / 并发撤单与成交竞争
  test_replay.py        # 重放一致重建 / 乱序非法事件拒绝
data/events.jsonl       # 默认事件日志（可用环境变量 OME_STORE_PATH 覆盖）
```

## 本地验证方法

```bash
# 1. 安装依赖
uv sync

# 2. 运行全部测试（26 个）。想看输入/关键状态/判定依据的日志：
uv run pytest -o log_cli=true
#    仅跑某一组：
uv run pytest tests/test_matching.py -q

# 3. 启动服务
uv run uvicorn main:app --reload
#    自定义事件日志位置：
OME_STORE_PATH=/tmp/ome.jsonl uv run uvicorn main:app
```

### 快速手工验证

```bash
# 配置两个账户（A 有现金，B 有持仓）
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{
  "account_id":"A","cash":"1000","position":"0",
  "max_order_notional":"900","max_net_position":"10"}'
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{
  "account_id":"B","cash":"1000","position":"10",
  "max_order_notional":"900","max_net_position":"10"}'

# B 卖出 5 @100（进入订单簿）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' -d '{
  "account_id":"B","side":"SELL","price":"100","quantity":"5","idempotency_key":"b-sell"}'

# A 买入 2 @100（立即成交，价格优先取 maker 价 100）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' -d '{
  "account_id":"A","side":"BUY","price":"100","quantity":"2","idempotency_key":"a-buy"}'

curl -s localhost:8000/book
curl -s localhost:8000/trades
curl -s localhost:8000/accounts/A

# 撤掉 B 剩余的 3 个
curl -s -X POST localhost:8000/orders/O3/cancel -H 'Content-Type: application/json' -d '{}'
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 配置（创建）账户与风险限额，重复配置被拒绝 |
| GET  | `/accounts/{id}` | 查询账户现金、冻结、持仓、限额 |
| POST | `/orders` | 提交限价单（支持 `idempotency_key`） |
| POST | `/orders/{id}/cancel` | 撤单（body 可带 `idempotency_key`） |
| GET  | `/orders/{id}` | 查询订单状态与成交量 |
| GET  | `/book` | 订单簿快照（bids 降序、asks 升序） |
| GET  | `/trades` | 全部成交序列（按发生顺序） |

金额、价格、数量一律使用 `Decimal`，网络层以**字符串**传输（如 `"100"`、`"2.5"`），
杜绝浮点误差。

### 错误响应

所有可预期拒绝返回统一结构，并带可区分的 `reason`：

```json
{ "error": "InsufficientCashError", "reason": "INSUFFICIENT_CASH", "detail": "..." }
```

| reason | 含义 | HTTP |
| --- | --- | --- |
| `INVALID_REQUEST` | 价格/数量非正等非法入参 | 400 |
| `UNKNOWN_ACCOUNT` / `UNKNOWN_ORDER` | 引用了不存在的账户/订单 | 404 |
| `ORDER_NOT_ACTIVE` | 订单已全部成交，不可撤销 | 409 |
| `ALREADY_CANCELLED` | 订单已撤销，重复撤销 | 409 |
| `SELF_TRADE` | 与本账户对手方向订单交叉，防自成交 | 409 |
| `IDEMPOTENCY_CONFLICT` | 幂等键被用于不同请求 | 409 |
| `RISK_ORDER_NOTIONAL_EXCEEDED` | 单笔名义金额超限额 | 422 |
| `RISK_NET_POSITION_EXCEEDED` | 预计净持仓超限额 | 422 |
| `INSUFFICIENT_CASH` / `INSUFFICIENT_POSITION` | 可用资金/持仓不足 | 422 |

## 撮合规则

1. **价格优先、时间优先（FIFO）**：买单优先与最低卖价成交，卖单优先与最高买价成交；
   同一价位按入簿先后成交。
2. **允许部分成交**：订单可被多次部分成交，状态在
   `NEW / PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED` 间迁移。
3. **吃单跨价**：当 incoming（taker）限价穿过对手最优价时成交，**成交价取 resting
   （maker）挂单价**；若一张 taker 连续穿越多个价位，逐价位按各自 maker 价成交。
4. **自成交防护（STP）**：订单在入簿前扫描对手方向，只要存在与本账户、且价格可交叉
   的 resting 订单，incoming 订单**整单拒绝**（`SELF_TRADE`），不部分成交、不入簿。
5. **挂单即冻结**：买入按 `价格×数量` 冻结资金；卖出冻结相应数量持仓。成交时按实际
   成交价结算并释放对应冻结；撤单释放剩余冻结。

### 数量与资金守恒（不变量）

- 系统内所有账户 `cash` 之和在成交前后不变（成交只是资金在买卖双方转移）。
- 所有账户 `position` 之和不变（标的数量不凭空增减）。
- 对任意活跃订单：`original_qty = filled_qty + leaves_qty`。
- 账户冻结量与其在簿订单剩余量严格对应，任何被拒请求都不改变订单簿与账户状态。

## 账户级风控

账户在 `/accounts` 配置时声明：

- `max_order_notional`：单笔订单名义金额上限（`price × quantity`）。
- `max_net_position`：净持仓绝对值上限。校验时使用**预计极端净持仓**：
  - 买入：`position + 在簿买单剩余量 + 本单数量 ≤ 上限`（假设所有买单都成交）；
  - 卖出：`|position − 在簿卖单剩余冻结 − 本单数量| ≤ 上限`（假设所有卖单都成交）。
- 此外买入检查可用资金 `cash − reserved_cash`，卖出检查可用持仓
  `position − reserved_sell_qty`。

任一校验不过，订单在落任何状态前即被拒绝，**不写入订单簿、不改变账户冻结/持仓**。
并发提交时所有写操作在同一把锁内「校验 → 落盘 → apply」，因此**超限订单不可能进入
撮合**，也不会出现超卖/超买。

## 幂等语义

- 提交订单与撤单均可携带 `idempotency_key`（建议客户端生成的唯一请求 ID）。
- **同一键 + 同一请求指纹**（账户、方向、价格、数量 / 撤单目标订单）：返回**首次
  请求的一致结果**，不再次下单、不再次成交、不重复冻结，也不追加新事件。
  - 首次被风控拒绝的请求也会被记忆：重复提交得到同样的 `accepted=false` 与同样原因。
  - 幂等结果在**重启重放后同样生效**（引擎在重放末尾从历史事件重建幂等表）。
- **同一键 + 不同请求指纹**：返回 `IDEMPOTENCY_CONFLICT`（409），明确区分于普通成功。
- 不带键的请求不做幂等去重（调用方自行保证）。

## 事件与可恢复重放

状态只能由事件 apply 得到。事件类型：

| 事件 | 作用 |
| --- | --- |
| `ACCOUNT_CONFIGURED` | 建立账户、初始现金/持仓/限额 |
| `ORDER_SUBMITTED` | 订单入簿、建立冻结 |
| `TRADE_EXECUTED` | 一笔成交（maker/taker、价、量），结算资金与持仓 |
| `ORDER_CANCELLED` | 撤单并释放剩余冻结 |
| `ORDER_REJECTED` | 带幂等键的被拒请求（用于重启后一致返回拒绝结果） |

每个事件带单调递增 `seq`（从 1 开始）、类型、时间戳与 payload；订单 ID `O{seq}`、
成交 ID `T{seq}` 与其事件序号严格绑定。每行写入后 `flush + fsync`。

### 重放恢复约束

启动时（FastAPI lifespan）读取事件文件并严格重放：

1. `seq` 必须从 1 开始连续递增，**乱序或缺号 → `ReplayError`，服务拒绝启动**。
2. 每行必须是合法 JSON 且符合事件信封/payload schema，非法行 → `ReplayError`。
3. 语义一致性同样强校验，例如：
   - 成交引用的订单必须存在、双方方向相反、分属不同账户（禁止自成交）；
   - 成交价必须等于 maker 挂单价、且不越过 taker 限价；
   - 成交数量不得超过任一订单剩余量；
   - 成交 ID 必须等于 `T{seq}`、账户与订单归属一致；
   - 同一订单不得重复提交、重复撤销；账户不得重复配置。
4. 任何非法/乱序/矛盾事件都**明确抛错，而不是静默跳过或覆盖**，避免以错误状态对外服务。

重放成功后，订单簿、账户现金/冻结/持仓、成交序列及幂等表与停机前逐项一致
（见 `tests/test_replay.py`）。

## 测试

测试在关键步骤打印**输入、关键状态与判定依据**（logger 名 `test.*`），用
`uv run pytest -o log_cli=true` 可观察完整决策过程。

- `test_matching.py`：全量/部分成交、价格优先跨多价位、同价位 FIFO、不交叉挂单、
  自成交拒绝、未知/重复/已成交撤单、撤单释放冻结与守恒。
- `test_risk_concurrency.py`：单笔名义/净持仓/资金/持仓四类超限、被拒不改状态、
  拒绝的幂等一致返回、同键不同请求冲突、重复提交不重复成交、**撤单与成交并发竞争**
  （结果互斥且数量守恒）、并发买单不超卖。
- `test_replay.py`：重放逐字段一致、成交序列一致、重启后幂等、乱序 seq、非法 JSON、
  超量矛盾成交、自成交历史事件、重复撤销均被明确拒绝。
