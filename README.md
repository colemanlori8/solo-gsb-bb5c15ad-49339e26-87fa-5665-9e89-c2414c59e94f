# DNS SOA 序列号推进审计 API

基于 **Python + FastAPI**，按 [RFC 1982](https://www.rfc-editor.org/rfc/rfc1982) 序列号环形算术，
对多个 DNS 副本当前持有的 32 位无符号 SOA 序列号与拟发布值进行推进审计，
推导最优发布候选，规划各副本的 IXFR / AXFR 传输，
在字节预算内选择要保留的增量集合（支持按故障域公平分配），
并按时间序列观测对上线做 quorum 门禁判定、
评估每次发布在各故障域间的传播收敛时延（SLO），
沿时间线检测各副本序列号无可共同推进候选的分裂窗口，
对多个镜像上报的无序 IXFR 清单按完整三元组（含内容摘要）
做法定人数计票与可信链共识审计，
并能按 seed 确定性生成跨回绕 / 正对点 / 分叉 / 环路的
RFC 1982 边界场景样本，供上述端点复现测试。

## 一条命令启动（Docker Compose）

```bash
docker compose up --build -d
```

服务地址（端口 **8000**）：

- 健康检查：<http://localhost:8000/health>
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>

停止服务：

```bash
docker compose down
```

> 无 Docker 时本地运行：`pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000`

## RFC 1982 判定规则

序列号空间为模 2³² 的环。定义前向模距离：

```
distance = (proposed - current) mod 2^32
```

| 距离 | 关系 `relation` | 含义 |
|---|---|---|
| `0` | `equal` | 相等 |
| `1 ≤ distance < 2^31` | `newer` | 严格更新、可比较 |
| `distance = 2^31` | **`ambiguous`** | 正对点，RFC 1982 明确规定**不可比较** |
| `2^31 < distance < 2^32` | `older` | 回退（等价于 proposed 落后 current） |

`relation` 不是 `newer` 的副本都会列入 `blocking_replicas`（阻塞副本）。

## API

### `POST /audit` — 审计拟发布值

请求：

```json
{
  "proposed": 2024092101,
  "replicas": [
    {"name": "ns-a", "current": 2024092100},
    {"name": "ns-b", "current": 2024092050},
    {"name": "ns-c", "current": 2024092102}
  ]
}
```

响应（每个副本逐一给出关系、前向模距离、是否阻塞；并汇总阻塞副本名）：

```json
{
  "proposed": 2024092101,
  "results": [
    {"name": "ns-a", "current": 2024092100, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 1, "blocking": false},
    {"name": "ns-b", "current": 2024092050, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 51, "blocking": false},
    {"name": "ns-c", "current": 2024092102, "proposed": 2024092101,
     "relation": "older", "forward_distance": 4294967295, "blocking": true}
  ],
  "blocked": true,
  "blocking_replicas": ["ns-c"]
}
```

正对点示例（差值正好 2³¹，必为 `ambiguous`）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 2147483648,
  "replicas": [{"name": "ns-a", "current": 0}]
}'
# relation: "ambiguous", forward_distance: 2147483648, blocking: true
```

回绕示例（`4294967295 -> 0` 是合法的严格推进）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 0,
  "replicas": [{"name": "ns-a", "current": 4294967295}]
}'
# relation: "newer", forward_distance: 1
```

### `POST /propose` — 推导最优候选

在环形空间中寻找一个对**所有副本**都严格更新且可比较（对每个副本均为 `newer`）的候选值，
最小化 **最大前向模距离** `max_i (proposed - current_i) mod 2^32`；
若多个候选同分，取**数值最小**者。

```bash
curl -s http://localhost:8000/propose -H 'Content-Type: application/json' -d '{
  "replicas": [
    {"name": "ns-a", "current": 100},
    {"name": "ns-b", "current": 150}
  ]
}'
```

```json
{
  "proposed": 151,
  "max_forward_distance": 51,
  "results": [
    {"name": "ns-a", "current": 100, "relation": "newer", "forward_distance": 51},
    {"name": "ns-b", "current": 150, "relation": "newer", "forward_distance": 1}
  ]
}
```

当各副本当前序列号跨越半环、不存在任何单一值能同时严格推进时（例如存在相差正好 2³¹ 的正对点，
或最宽跨度达到 2³¹），返回 **409 Conflict**：

```json
{
  "detail": {
    "error": "no_valid_proposal",
    "message": "不存在能对所有副本都严格更新且可比较的候选值：副本当前序列号跨越半环（含相差 2^31 的正对点）"
  }
}
```

### `POST /transfer-plan` — 规划 IXFR / AXFR 传输

给定目标 SOA 序列号、各副本当前值与一组**无序** IXFR 增量（`from_serial`、`to_serial`），
先对增量集合做整体校验，再为每个副本沿增量链逐跳追踪，规划增量传输（IXFR）或全量传输（AXFR）。

**链路匹配遵循 RFC 1982 环形顺序**：只沿增量自身的 `from_serial -> to_serial` 边前进，
边的合法性以环形先后关系判定（`4294967295 -> 0` 是合法推进），**不按整数大小排序**。

- 完全相同的重复增量自动去重；
- 任一增量的 `to_serial` 不是 `from_serial` 的严格更新（`equal` / `older` / 正对点 `ambiguous`），
  返回 **422**（`error=delta_not_strictly_newer`）；
- 同一 `from_serial` 指向不同 `to_serial` 的**分叉**，返回 **422**（`error=delta_fork`）。

每个副本的结果 `mode`：

| `mode` | 条件 | `reason` |
|---|---|---|
| `up_to_date` | 当前值已等于目标 | `null` |
| `ixfr` | 存在从当前值到目标的唯一完整增量链（无序输入会被排序到 `ordered_deltas`） | `null` |
| `axfr` | 链路存在**缺口** `gap`（某序列号没有对应增量） | `"gap"` |
| `axfr` | 沿链行进重复经过某序列号，形成**环路** `loop` | `"loop"` |
| `axfr` | 当前值与目标在 RFC 1982 下**不可比较**（前向距离恰为 2³¹） | `"target_incomparable"` |

> 校验后每个 `from_serial` 至多有一个后继，因此任何完整链都是唯一的。
> 只要存在从副本出发能实际连通目标的完整链（即便链跨过 2³² 回绕点、目标在环上更旧），
> 即为 `ixfr`；连不通才按缺口 / 环路 / 不可比较降级为 `axfr`。

请求（注意增量是乱序的，且含一条重复）：

```bash
curl -s http://localhost:8000/transfer-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 5,
    "replicas": [
      {"name": "ns-a", "current": 5},
      {"name": "ns-b", "current": 1},
      {"name": "ns-c", "current": 7}
    ],
    "deltas": [
      {"from_serial": 4, "to_serial": 5},
      {"from_serial": 1, "to_serial": 2},
      {"from_serial": 3, "to_serial": 4},
      {"from_serial": 2, "to_serial": 3},
      {"from_serial": 2, "to_serial": 3}
    ]
  }'
```

响应（`results` 保持副本的输入顺序）：

```json
{
  "target": 5,
  "results": [
    {"name": "ns-a", "current": 5, "target": 5,
     "mode": "up_to_date", "reason": null, "ordered_deltas": []},
    {"name": "ns-b", "current": 1, "target": 5,
     "mode": "ixfr", "reason": null,
     "ordered_deltas": [
       {"from_serial": 1, "to_serial": 2},
       {"from_serial": 2, "to_serial": 3},
       {"from_serial": 3, "to_serial": 4},
       {"from_serial": 4, "to_serial": 5}
     ]},
    {"name": "ns-c", "current": 7, "target": 5,
     "mode": "axfr", "reason": "gap", "ordered_deltas": []}
  ]
}
```

跨 2³² 回绕的增量链同样按环形顺序排序（`4294967294 -> 4294967295 -> 0 -> 1` 是合法 IXFR 链）；
副本当前值与目标相差正好 2³¹ 时即使有可连通的边也返回 `axfr / target_incomparable`：

```bash
curl -s http://localhost:8000/transfer-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 2147483648,
    "replicas": [{"name": "ns-a", "current": 0}],
    "deltas": [
      {"from_serial": 0, "to_serial": 1},
      {"from_serial": 1, "to_serial": 2147483648}
    ]
  }'
# {"...","mode":"axfr","reason":"target_incomparable","ordered_deltas":[]}
```

### `POST /retention-plan` — 字节预算内的增量保留规划

给定目标 SOA 序列号、一组**带字节数**的无序 IXFR 增量（`from_serial`、`to_serial`、`bytes`）、
各副本当前值与非负 `priority`（**至多 12 个副本**），以及总字节预算 `budget`：
在 `used_bytes <= budget` 的约束下选择要**保留**的增量子集，使
**拥有到目标完整链的副本 priority 总和最大**，并依次按以下规则确定唯一结果：

1. 被覆盖副本（`ixfr` + `up_to_date`）的 **priority 总和最大**；
2. 被覆盖**副本数最多**；
3. **占用字节最少**；
4. 按 `(from_serial, to_serial)` 升序排列的**增量序列字典序最小**。

增量校验复用 `/transfer-plan` 的环形链路规则（非严格 newer、分叉均为 422），并新增：
同一 `(from_serial, to_serial)` 携带**不同 `bytes`** 时返回 **422**（`error=delta_bytes_conflict`）；
完全相同的重复增量去重。副本数超过 12 返回 **422**（`error=too_many_replicas`）。

每个副本的结果 `mode`（复用 `plan_transfer` 在**完整**增量集合上的判定）：

| `mode` | 条件 | `reason` |
|---|---|---|
| `up_to_date` | 当前值已等于目标（不占用预算，恒视为已覆盖） | `null` |
| `ixfr` | 到目标的完整链被保留集覆盖 | `null` |
| `axfr` | 完整集合下即存在**缺口**（保留任何子集都无法连通） | `"gap"` |
| `axfr` | 完整集合下即形成**环路** | `"loop"` |
| `axfr` | 当前值与目标在 RFC 1982 下**不可比较**（前向距离恰为 2³¹） | `"target_incomparable"` |
| `axfr` | 完整集合下存在完整链，但因**预算**未被保留 | `"budget"` |

> 保留集恰为被覆盖副本需求集（各自完整链上的增量）之并——共享链的副本只计一次字节，
> 对覆盖无贡献的增量不会被保留。**预算无法保留任何完整链时仍返回 200**
> （`retained_deltas` 为空、`used_bytes` 为 0，相关副本标记 `axfr / budget`）。

请求（预算 12 恰好保住 `ns-a` 的完整链，`ns-b` 因共享链后缀被一并覆盖）：

```bash
curl -s http://localhost:8000/retention-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 4,
    "budget": 12,
    "replicas": [
      {"name": "ns-a", "current": 1, "priority": 10},
      {"name": "ns-b", "current": 3, "priority": 5},
      {"name": "ns-c", "current": 4, "priority": 100},
      {"name": "ns-d", "current": 7, "priority": 1}
    ],
    "deltas": [
      {"from_serial": 3, "to_serial": 4, "bytes": 6},
      {"from_serial": 1, "to_serial": 2, "bytes": 3},
      {"from_serial": 2, "to_serial": 3, "bytes": 3},
      {"from_serial": 1, "to_serial": 2, "bytes": 3}
    ]
  }'
```

响应（`retained_deltas` 按 `(from_serial, to_serial)` 升序，`results` 保持副本输入顺序）：

```json
{
  "target": 4,
  "budget": 12,
  "used_bytes": 12,
  "retained_deltas": [
    {"from_serial": 1, "to_serial": 2, "bytes": 3},
    {"from_serial": 2, "to_serial": 3, "bytes": 3},
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-b", "current": 3, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "mode": "axfr", "reason": "gap"}
  ]
}
```

预算不足时（例如上例中 `budget` 改为 11）仍返回 200：

```json
{
  "target": 4,
  "budget": 11,
  "used_bytes": 6,
  "retained_deltas": [
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "mode": "axfr", "reason": "budget"},
    {"name": "ns-b", "current": 3, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "mode": "axfr", "reason": "gap"}
  ]
}
```

### `POST /domain-retention-plan` — 按故障域公平的增量保留规划

在 `/retention-plan` 的基础上为每个副本增加**非空** `failure_domain`（故障域，
如机房 / 可用区），复用完全相同的增量校验、链路原因（`gap` / `loop` /
`target_incomparable` / `budget`）与**最多 12 个副本**限制。
优化目标从“全体 priority 总和”改为**逐故障域 maximin 公平**：

对任一保留子集，先汇总每个 `failure_domain` 中已覆盖（`ixfr` 与 `up_to_date`）
副本的 `priority` 总和，再把各域总和按**升序**排列成向量（向量包含请求中出现的
**全部**域，按域名字典序固定分量顺序；该域无已覆盖副本时分量为 0）。
在 `used_bytes <= budget` 的可行子集中**依次**裁决：

1. 各域已覆盖 priority 总和的**升序向量字典序最大**
   （先抬升覆盖最差的域，即逐域 maximin）；
2. 全体已覆盖副本的 **priority 总和最大**；
3. 已覆盖**副本数最多**；
4. **占用字节最少**；
5. 按 `(from_serial, to_serial)` 升序排列的**增量序列字典序最小**。

> 第 1 级可能压倒“全体总和”：例如三域 `C / P / Q`，预算 20 时，
> 覆盖 P(2+2) 与 Q(3) 得向量 `(0,3,4)`（总和 7），而只覆盖 C(6) 得向量
> `(0,0,6)`（总和 6）——尽管 6 的单域值更大，但最小的非零分量 3 > 0，
> 故仍选择均衡方案。

请求（`failure_domain` 必填且非空）：

```bash
curl -s http://localhost:8000/domain-retention-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 4,
    "budget": 12,
    "replicas": [
      {"name": "ns-a", "current": 1, "priority": 10, "failure_domain": "dc-east"},
      {"name": "ns-b", "current": 3, "priority": 5,  "failure_domain": "dc-west"},
      {"name": "ns-c", "current": 4, "priority": 100,"failure_domain": "dc-west"},
      {"name": "ns-d", "current": 7, "priority": 1,  "failure_domain": "dc-east"}
    ],
    "deltas": [
      {"from_serial": 3, "to_serial": 4, "bytes": 6},
      {"from_serial": 1, "to_serial": 2, "bytes": 3},
      {"from_serial": 2, "to_serial": 3, "bytes": 3}
    ]
  }'
```

响应（`retained_deltas` 与逐副本模式同 `/retention-plan`，逐副本结果回显
`failure_domain`；新增按域名升序的 `domain_summaries`）：

```json
{
  "target": 4,
  "budget": 12,
  "used_bytes": 12,
  "retained_deltas": [
    {"from_serial": 1, "to_serial": 2, "bytes": 3},
    {"from_serial": 2, "to_serial": 3, "bytes": 3},
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "failure_domain": "dc-east",
     "mode": "ixfr", "reason": null},
    {"name": "ns-b", "current": 3, "target": 4, "failure_domain": "dc-west",
     "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "failure_domain": "dc-west",
     "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "failure_domain": "dc-east",
     "mode": "axfr", "reason": "gap"}
  ],
  "domain_summaries": [
    {"failure_domain": "dc-east", "covered_priority": 10, "total_priority": 11},
    {"failure_domain": "dc-west", "covered_priority": 105, "total_priority": 105}
  ]
}
```

`domain_summaries` 中：

- `covered_priority` = 该域最终被覆盖（`ixfr` / `up_to_date`）副本的 priority 总和；
- `total_priority` = 该域**全部**副本的 priority 总和（含因 `budget` / `gap` /
  `loop` / `target_incomparable` 未覆盖者），不随预算变化。

与 `/retention-plan` 一样，预算无法保留任何完整链时仍返回 **200**
（`retained_deltas` 为空、`used_bytes` 为 0；`up_to_date` 副本零成本仍计入
`covered_priority`）。全部裁决键与输出排列均确定，**相同输入恒得相同结果**。

### `POST /rollout-gate` — 上线门禁（quorum 时间窗）

给定基线 SOA 序列号 `baseline`、所需 `quorum`（需同时达到基线的副本数下限，
**正整数**），以及每个副本**按时间排序**的观测序列 `observations`
（`timestamp` 可缺失为 `null`，`serial` 必填）：

- 同一副本**存在的时间戳必须严格递增**：重复返回 422
  （`duplicate_observation_timestamp`），倒序返回 422
  （`observation_timestamp_out_of_order`）；缺失（`null`）的时间戳不参与排序校验，
  副本之间时间戳相同不受影响。
- 相邻观测对按 RFC 1982 逐对判定并标记 `relation`：

  | `relation` | 含义 |
  |---|---|
  | `newer` | 严格推进（**允许跨 2³² 回绕**，如 `4294967295 -> 0`） |
  | `equal` | 序列号未变 |
  | `older` | 回退 |
  | `ambiguous` | 正对点（差值恰为 2³¹，不可比较） |
  | `missing_time_order` | 任一时间戳缺失，无法建立时间顺序 |

- 逐副本输出**首次达到基线之后的有效推进区间** `interval`（闭区间，Unix 秒）：
  从首个等于或严格新于基线的观测起，沿 `newer` / `equal` 且推进后仍达到基线的
  相邻对扩展；遇 `older` / `ambiguous` / `missing_time_order` 或推进后跌破基线
  即终止。无区间时逐副本 `reason` 给出原因：`baseline_not_reached`
  （从未达到基线）或 `missing_time_order`（达到了但首次达到的观测无时间戳，
  无法定位区间）。
- 用**事件扫掠**在各区间端点统计同时处于有效推进区间内的副本数（闭区间端点处
  仍计入），输出 quorum 首次满足时间 `first_satisfied_at`、之后再次跌破时间
  `dropped_below_at`（满足后未再跌破为 `null`）与峰值并发 `peak_concurrent`。
- quorum 从未满足时仍返回 **200**，顶层 `reason` 给出明确原因：
  `quorum_exceeds_replicas`（quorum 超过副本总数）或
  `quorum_never_reached`（峰值并发不足）。

请求：

```bash
curl -s http://localhost:8000/rollout-gate \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline": 2024092101,
    "quorum": 2,
    "replicas": [
      {"name": "ns-a", "observations": [
        {"timestamp": 1727000000, "serial": 2024092100},
        {"timestamp": 1727000060, "serial": 2024092101},
        {"timestamp": 1727000120, "serial": 2024092102}
      ]},
      {"name": "ns-b", "observations": [
        {"timestamp": 1727000030, "serial": 2024092101},
        {"timestamp": 1727000090, "serial": 2024092101}
      ]},
      {"name": "ns-c", "observations": [
        {"timestamp": 1727000200, "serial": 2024092101}
      ]}
    ]
  }'
```

响应（`ns-a` 在 `1727000060` 达到基线，与 `ns-b` 的区间重叠至 `1727000120`，
quorum=2 在该窗口内满足；`ns-c` 的点区间来得太晚）：

```json
{
  "baseline": 2024092101,
  "quorum": 2,
  "satisfied": true,
  "first_satisfied_at": 1727000060,
  "dropped_below_at": 1727000120,
  "peak_concurrent": 2,
  "reason": null,
  "replicas": [
    {"name": "ns-a", "reaches_baseline": true, "first_reached_at": 1727000060,
     "interval": {"start": 1727000060, "end": 1727000120}, "reason": null,
     "transitions": [
       {"index": 1, "from_timestamp": 1727000000, "to_timestamp": 1727000060,
        "from_serial": 2024092100, "to_serial": 2024092101, "relation": "newer"},
       {"index": 2, "from_timestamp": 1727000060, "to_timestamp": 1727000120,
        "from_serial": 2024092101, "to_serial": 2024092102, "relation": "newer"}
     ]},
    {"name": "ns-b", "reaches_baseline": true, "first_reached_at": 1727000030,
     "interval": {"start": 1727000030, "end": 1727000090}, "reason": null,
     "transitions": [
       {"index": 1, "from_timestamp": 1727000030, "to_timestamp": 1727000090,
        "from_serial": 2024092101, "to_serial": 2024092101, "relation": "equal"}
     ]},
    {"name": "ns-c", "reaches_baseline": true, "first_reached_at": 1727000200,
     "interval": {"start": 1727000200, "end": 1727000200}, "reason": null,
     "transitions": []}
  ]
}
```

跨 2³² 回绕的推进是合法的（`4294967294 -> 4294967295 -> 0` 连续 newer）；
缺失时间戳的相邻对标记 `missing_time_order` 并终止该副本的有效推进区间：

```bash
curl -s http://localhost:8000/rollout-gate \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline": 4294967294,
    "quorum": 2,
    "replicas": [
      {"name": "ns-a", "observations": [
        {"timestamp": 1000, "serial": 4294967294},
        {"timestamp": 1010, "serial": 4294967295},
        {"timestamp": 1020, "serial": 0}
      ]},
      {"name": "ns-b", "observations": [
        {"timestamp": 1005, "serial": 4294967295},
        {"timestamp": null, "serial": 0}
      ]}
    ]
  }'
# ns-a 区间 [1000, 1020]（回绕推进）；ns-b 区间止于 [1005, 1005]
# satisfied: true, first_satisfied_at: 1005, dropped_below_at: 1020
```

quorum 无法满足时仍返回 200 并给出明确原因：

```bash
curl -s http://localhost:8000/rollout-gate \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline": 100, "quorum": 3,
    "replicas": [
      {"name": "ns-a", "observations": [{"timestamp": 1, "serial": 100}]},
      {"name": "ns-b", "observations": [{"timestamp": 1, "serial": 100}]}
    ]
  }'
# satisfied: false, reason: "quorum_exceeds_replicas", peak_concurrent: 2
```

### `POST /propagation-slo` — 传播 SLO（逐故障域收敛时延）

给定**按时间严格递增**的发布事件序列（`serial` + `released_at`）、各副本
（携带**非空** `failure_domain`）**按时间严格递增**的观测序列
（`timestamp` 必填 + `serial`），以及非负整数 `deadline_seconds`：

- 对第 i 次发布，**只检查**其到下一次发布前的窗口
  `[released_at[i], released_at[i+1])`（左闭右开；最后一次发布的窗口延伸到
  观测结束）。窗口外的观测不参与本次发布的判定，各窗口的到达状态相互独立、
  互不带入。
- 窗口内每条观测按 RFC 1982 相对本次发布版本分类：

  | 观测值相对发布版本 | 事件 | 效果 |
  |---|---|---|
  | `equal` / `newer` | **到达** | 副本进入已到达状态；其故障域若尚未到达，以该观测时间为域的 `reached_at` |
  | `older` / `ambiguous` | **回退** | 若该副本**已到达**，其到达状态失效，且**所在故障域的本窗口到达状态一并失效**，须等待后续重新到达；未到达副本的回退无影响 |

  同一时间戳的事件**先处理回退再处理到达**（回退只使早于该时刻的到达失效，
  同时刻的到达仍然有效）。观测值严格新于发布版本（含跨 2³² 回绕）同样算到达；
  与发布版本相差正好 2³¹（`ambiguous` 正对点）不算到达。
- 每个故障域的 `reached_at` 为该域**首个有效到达时间**（域内任一副本到达即
  域到达；失效后须由新的到达观测重新确定——同域其他副本仍处于已到达状态
  并不能使域自动恢复）。
- 全部故障域都到达的最早时刻即 `converged_at`（等于各域 `reached_at` 的
  最晚值）；**收敛之后的回退不再影响结果**。
- `latency_seconds = converged_at - released_at`；
  `met_deadline = latency_seconds <= deadline_seconds`。
- 窗口结束仍未覆盖全部域时：`converged_at` 与 `latency_seconds` 为 `null`、
  `met_deadline` 为 `false`，并给出按名称升序的 `missing_domains`
  （未到达域的 `reached_at` 为 `null`）。

请求（两次发布、两个故障域）：

```bash
curl -s http://localhost:8000/propagation-slo \
  -H 'Content-Type: application/json' \
  -d '{
    "deadline_seconds": 300,
    "releases": [
      {"serial": 2024092101, "released_at": 1000},
      {"serial": 2024092102, "released_at": 2000}
    ],
    "replicas": [
      {"name": "ns-a", "failure_domain": "dc-east",
       "observations": [
         {"timestamp": 1000, "serial": 2024092100},
         {"timestamp": 1120, "serial": 2024092101},
         {"timestamp": 2050, "serial": 2024092102}
       ]},
      {"name": "ns-b", "failure_domain": "dc-west",
       "observations": [
         {"timestamp": 1180, "serial": 2024092101},
         {"timestamp": 1900, "serial": 2024092101}
       ]},
      {"name": "ns-c", "failure_domain": "dc-west",
       "observations": [
         {"timestamp": 1250, "serial": 2024092101}
       ]}
    ]
  }'
```

响应（第一次发布两域分别在 1120 / 1180 到达，`converged_at=1180`、
`latency_seconds=180` 达标；第二次发布的窗口 `[2000, +∞)` 内 `dc-west`
没有任何观测，未收敛并列入 `missing_domains`）：

```json
{
  "deadline_seconds": 300,
  "releases": [
    {"serial": 2024092101, "released_at": 1000,
     "converged_at": 1180, "latency_seconds": 180, "met_deadline": true,
     "domains": [
       {"failure_domain": "dc-east", "reached_at": 1120},
       {"failure_domain": "dc-west", "reached_at": 1180}
     ],
     "missing_domains": []},
    {"serial": 2024092102, "released_at": 2000,
     "converged_at": null, "latency_seconds": null, "met_deadline": false,
     "domains": [
       {"failure_domain": "dc-east", "reached_at": 2050},
       {"failure_domain": "dc-west", "reached_at": null}
     ],
     "missing_domains": ["dc-west"]}
  ]
}
```

已到达副本在收敛前回退（`older` / `ambiguous`）会使其域的到达状态失效，
须等待后续重新到达：

```bash
curl -s http://localhost:8000/propagation-slo \
  -H 'Content-Type: application/json' \
  -d '{
    "deadline_seconds": 100,
    "releases": [{"serial": 10, "released_at": 0}],
    "replicas": [
      {"name": "ns-a", "failure_domain": "dc-east", "observations": [
        {"timestamp": 10, "serial": 10},
        {"timestamp": 20, "serial": 9},
        {"timestamp": 50, "serial": 10}
      ]},
      {"name": "ns-b", "failure_domain": "dc-west", "observations": [
        {"timestamp": 30, "serial": 10}
      ]}
    ]
  }'
# dc-east：10 到达 -> 20 回退失效 -> 50 重新到达（reached_at=50）
# dc-west：reached_at=30；converged_at=50，latency_seconds=50 <= 100，met_deadline=true
```

非递增的发布或观测时间返回 **422**：发布时间重复
（`duplicate_release_timestamp`）或倒序（`release_timestamp_out_of_order`）、
同一副本观测时间重复（`duplicate_observation_timestamp`）或倒序
（`observation_timestamp_out_of_order`）；副本名重复
（`duplicate_replica_name`）与空 `failure_domain` 同样返回 422。

### `POST /divergence-windows` — 分裂窗口检测

给定多个副本**按时间排序**的序列号观测（每个副本携带 `observations`：
必填非负的 `timestamp` + `serial`，列表允许为空），沿时间线检测各副本
序列号**不存在任何能同时严格推进全部副本的候选值**的连续区间：

- 全部观测汇成事件按时间排序；每个时间戳**先原子合并该时刻的全部更新**
  （同一时刻任意多个副本的观测一起生效，不存在“先看到谁”的中间状态），
  再沿时间线**携带各副本最近一次观测值**——尚无观测的副本不参与该时刻的判定。
- 每个合并后状态按 **RFC 1982 环形规则**判定：存在候选 `x` 使每个副本的
  最新值 `c_i` 都满足 `1 ≤ (x − c_i) mod 2³² ≤ 2³¹ − 1`（即各开弧
  `{c+1, …, c+2³¹−1} mod 2³²` 的交集非空）才算可行。
  **不能用普通整数极差代替**：`4294967290` 与 `10` 的整数极差接近 2³²，
  前向距离却只有 16，存在共同后继；而 `0` 与 `2³¹−1` 的整数极差不足 2³¹，
  开弧交集却已为空（再如相差正好 2³¹ 的正对点恒不可比较）。
- **连续**无可行候选的状态合并为一个**分裂窗口**：中间任一状态恢复可行即闭窗，
  之后再次发散另开新窗。`start_at` 为首个无候选状态的时间；`end_at` 为其后
  首个恢复可行状态的时间，`duration_seconds = end_at − start_at`；
  观测结束仍未恢复时 `end_at` 与 `duration_seconds` 均为 `null`。
- 每个窗口返回涉及副本 `replicas` 及各副本**进入窗口时**的序列号 `entries`：
  窗口开启时对全部已知副本快照；窗口开启后才首次出现的副本在其窗口内首个
  观测时间戳加入，取该首条观测序列号（按请求中的副本顺序输出）。

同一副本的观测时间戳允许**同刻多条**（同一时刻原子合并），但必须非递减：
倒序返回 422（`observation_timestamp_out_of_order`）；同一副本同一时间戳
出现**不同序列号**返回 422（`observation_serial_conflict`）；
同刻同值的重复观测合法并自动去重。副本名重复（`duplicate_replica_name`）
同样返回 422。

请求（`t=20` 时 `ns-b` 推进到 `ns-a=100` 的正对点 `100+2³¹=2147483748`，
任何单一候选都无法同时严格推进两者，窗口开启；`t=50` 两者汇聚到 200，
恢复可行，窗口关闭）：

```bash
curl -s http://localhost:8000/divergence-windows \
  -H 'Content-Type: application/json' \
  -d '{
    "replicas": [
      {"name": "ns-a", "observations": [
        {"timestamp": 0,  "serial": 100},
        {"timestamp": 20, "serial": 100},
        {"timestamp": 50, "serial": 200}
      ]},
      {"name": "ns-b", "observations": [
        {"timestamp": 10, "serial": 100},
        {"timestamp": 20, "serial": 2147483748},
        {"timestamp": 40, "serial": 2147483748},
        {"timestamp": 50, "serial": 200}
      ]}
    ]
  }'
```

响应（`entries` 为两副本**进入窗口时**持有的序列号：`ns-a=100`、
`ns-b=2147483748`；窗口持续 30 秒）：

```json
{
  "windows": [
    {
      "start_at": 20,
      "end_at": 50,
      "duration_seconds": 30,
      "replicas": ["ns-a", "ns-b"],
      "entries": [
        {"name": "ns-a", "serial": 100},
        {"name": "ns-b", "serial": 2147483748}
      ]
    }
  ]
}
```

整数极差会误判、必须按环形前向距离判定的两个对照：

```bash
# 4294967290 与 10：整数极差近 2^32，但前向距离仅 16，始终可行 -> 无窗口
curl -s http://localhost:8000/divergence-windows \
  -H 'Content-Type: application/json' \
  -d '{
    "replicas": [
      {"name": "ns-a", "observations": [
        {"timestamp": 0, "serial": 4294967290},
        {"timestamp": 10, "serial": 4294967290}
      ]},
      {"name": "ns-b", "observations": [
        {"timestamp": 0, "serial": 10},
        {"timestamp": 10, "serial": 0}
      ]}
    ]
  }'
# {"windows": []}

# 0 与 2147483647（2^31-1）：整数极差不足 2^31 却已无共同候选
curl -s http://localhost:8000/divergence-windows \
  -H 'Content-Type: application/json' \
  -d '{
    "replicas": [
      {"name": "ns-a", "observations": [{"timestamp": 0, "serial": 0}]},
      {"name": "ns-b", "observations": [{"timestamp": 5, "serial": 2147483647}]}
    ]
  }'
# 窗口开启后观测结束仍未恢复：
# {"windows": [{"start_at": 5, "end_at": null, "duration_seconds": null,
#   "replicas": ["ns-a", "ns-b"],
#   "entries": [{"name": "ns-a", "serial": 0},
#               {"name": "ns-b", "serial": 2147483647}]}]}
```

同一时刻的更新**原子合并**：两个副本在 `t=10` 一起更新到相同值，
即使其中任一副本的新值相对另一副本的旧值是正对点，也不会产生瞬时窗口。
同一副本同刻给出不同序列号则为 422：

```bash
curl -s http://localhost:8000/divergence-windows \
  -H 'Content-Type: application/json' \
  -d '{
    "replicas": [
      {"name": "ns-a", "observations": [
        {"timestamp": 10, "serial": 100},
        {"timestamp": 10, "serial": 101}
      ]}
    ]
  }'
# 422 {"detail": {"error": "observation_serial_conflict", ...}}
```

### `POST /delta-consensus` — 多镜像 IXFR 增量共识

给定目标 SOA 序列号 `target`、法定人数 `quorum`（正整数）与多个**镜像**
（mirror）各自上报的**无序** IXFR 清单（每项含 `from_serial`、`to_serial`
与内容摘要 `digest`），先严格校验，再按**完整三元组**计票，沿可信边组装
到目标的唯一连续链，并给出每个镜像相对可信集合的偏差证据。

**第一步：校验（422）**

- 每条上报边的 `to_serial` 必须按 RFC 1982 严格新于 `from_serial`
  （`4294967295 -> 0` 合法；`equal` / `older` / 正对点 `ambiguous` 返回
  422 `delta_not_strictly_newer`，错误体含镜像名）；
- **单个镜像**对同一 `from_serial` 上报冲突版本——不同 `to_serial` 或不同
  `digest`——返回 422 `mirror_version_conflict`（返回冲突的两份完整上报）；
  完全相同的三元组重复上报合法并去重。不同镜像各执一词不算校验错误，
  交给计票裁决；镜像名重复返回 422（`duplicate_replica_name`）。

**第二步：完整三元组计票**

按 `(from_serial, to_serial, digest)` 统计上报的**不同镜像数**（同一镜像
重复只计一票）；票数 `>= quorum` 的三元组进入**可信集合**。`digest` 不同
的同序号对视为不同版本各自计票；即使同一序号对上有**两个不同摘要都达到
法定人数**，也视为分叉（内容无法静默二选一）。

**第三步：组装唯一连续链（不静默选边）**

可信集合中每个 `from_serial` 只有**唯一可信出边**时才能续链。从每个无入边
的根节点沿唯一出边行进，遇以下情况即停：缺口（`gap`，无出边）、分叉
（`fork`，多个可信出边）、环路（`loop`，回到已访问节点）。取各根路径中
最长的一条为 `trusted_chain`（长度并列时按
`(from_serial, to_serial, digest)` 边序列、再按根节点数值字典序破平）。
`target_reachable` 当且仅当目标出现在某条唯一路径**确定到达**的节点集合中
（分叉点 / 回环点本身已到达，但其之后的分支与环的第二次循环不算；纯环中
的节点不可达）。可信集合中的**全部**分叉与环路分别列在 `forks` / `loops`
作为证据，绝不静默选边。

请求（3 个镜像、quorum=2；`ns-c` 对 `20->30` 上报了少数摘要）：

```bash
curl -s http://localhost:8000/delta-consensus \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 30,
    "quorum": 2,
    "mirrors": [
      {"name": "ns-a", "deltas": [
        {"from_serial": 10, "to_serial": 20, "digest": "h10"},
        {"from_serial": 20, "to_serial": 30, "digest": "h20"}]},
      {"name": "ns-b", "deltas": [
        {"from_serial": 20, "to_serial": 30, "digest": "h20"},
        {"from_serial": 10, "to_serial": 20, "digest": "h10"}]},
      {"name": "ns-c", "deltas": [
        {"from_serial": 10, "to_serial": 20, "digest": "h10"},
        {"from_serial": 20, "to_serial": 30, "digest": "h20-bad"}]}
    ]
  }'
```

响应（清单乱序不影响结果；`trusted_edges` 按三元组升序、`voters` 按名称
升序，`mirrors` 保持请求顺序）：

```json
{
  "target": 30,
  "quorum": 2,
  "mirror_count": 3,
  "trusted_chain": [
    {"from_serial": 10, "to_serial": 20, "digest": "h10",
     "votes": 3, "voters": ["ns-a", "ns-b", "ns-c"]},
    {"from_serial": 20, "to_serial": 30, "digest": "h20",
     "votes": 2, "voters": ["ns-a", "ns-b"]}
  ],
  "chain_length": 2,
  "chain_end": 30,
  "chain_reason": "reached",
  "target_reachable": true,
  "trusted_edges": [
    {"from_serial": 10, "to_serial": 20, "digest": "h10",
     "votes": 3, "voters": ["ns-a", "ns-b", "ns-c"]},
    {"from_serial": 20, "to_serial": 30, "digest": "h20",
     "votes": 2, "voters": ["ns-a", "ns-b"]}
  ],
  "forks": [],
  "loops": [],
  "mirrors": [
    {"mirror": "ns-a", "reported_edges": 2,
     "missing": [], "minority_digests": [], "extra_edges": []},
    {"mirror": "ns-b", "reported_edges": 2,
     "missing": [], "minority_digests": [], "extra_edges": []},
    {"mirror": "ns-c", "reported_edges": 2,
     "missing": [],
     "minority_digests": [
       {"from_serial": 20, "to_serial": 30, "digest": "h20-bad", "votes": 1,
        "trusted_digests": [{"digest": "h20", "votes": 2}]}
     ],
     "extra_edges": []}
  ]
}
```

逐镜像证据三类（顺序均稳定：缺失按 `(from,to)`、其余按三元组升序）：

| 字段 | 含义 |
|---|---|
| `missing` | 该镜像**未上报**的可信序号对（含该序号对上全部可信摘要版本及票数） |
| `minority_digests` | 镜像上报的序号对可信、但其 `digest` 未达法定人数（给出该摘要票数与可信摘要对照） |
| `extra_edges` | 不构成任何可信三元组的上报（序号对整体也不可信），给出票数 |

分叉与环路不静默选边：下例中 `10->20` 与 `10->21` 各有 2 票，最长可信链
为空、`chain_reason="fork"`、目标 20 / 21 均 `target_reachable=false`，
两个分支完整列在 `forks` 中：

```bash
curl -s http://localhost:8000/delta-consensus \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 20,
    "quorum": 2,
    "mirrors": [
      {"name": "a", "deltas": [{"from_serial": 10, "to_serial": 20, "digest": "x"}]},
      {"name": "b", "deltas": [{"from_serial": 10, "to_serial": 20, "digest": "x"}]},
      {"name": "c", "deltas": [{"from_serial": 10, "to_serial": 21, "digest": "y"}]},
      {"name": "d", "deltas": [{"from_serial": 10, "to_serial": 21, "digest": "y"}]}
    ]
  }'
# chain_reason: "fork", target_reachable: false,
# forks: [{from_serial: 10, branches: [{to_serial: 20, digests: [{digest: "x", votes: 2}]},
#                                      {to_serial: 21, digests: [{digest: "y", votes: 2}]}]}]
```

跨 2³² 回绕的边同样按 RFC 1982 判定为严格推进（`4294967295 -> 0`）；
`quorum` 超过镜像总数时不报错，但任何边都无法达法定人数（`trusted_chain`
为空、`target_reachable=false`、`chain_reason="gap"`，所有上报按票数计入
`extra_edges`）。

### `POST /serial-scenarios/generate` — 确定性 RFC 1982 边界场景生成

为上述审计 / 规划端点批量生成**可复现**的边界测试输入。请求给出 32 位无符号
`seed`、`replica_count`（**1–12**）、`scenario_count`（**1–32**）与非空无重复的
场景标签子集 `include`（`wrap` / `ambiguous` / `fork` / `loop`），服务以标准库
`random.Random`（Mersenne Twister）为**每个场景派生独立伪随机流**
（仅做 `(seed, index, label)` 整数混合播种，不依赖按进程加盐的哈希），
生成 32 位范围内的副本当前序列号 `replicas`、`target` 与**已随机洗牌的无序**
增量样本 `deltas`，并在 `trigger_points` 中说明命中的边界条件与喂给现有端点后的
预期触发行为。**相同请求恒得相同场景标签与输入**；标签序列与 seed 无关，按
`include` 顺序**轮转**铺满（`include=["wrap","loop"]`、`scenario_count=5`
时标签为 `wrap,loop,wrap,loop,wrap`），增减 `scenario_count` 不影响靠前场景。

| 标签 | 生成的 RFC 1982 边界 | 喂给现有端点的预期行为 |
|---|---|---|
| `wrap` | 目标在回绕点附近小号区，增量链含 `4294967295 -> 0`；部分副本整数上“大于”目标，前向路径却跨过回绕点 | `/audit` 全部 `newer`（前向距离仅几十）；`/transfer-plan` 的 `ns-1` 沿 `… -> 4294967295 -> 0 -> target` 链 **ixfr** |
| `ambiguous` | `ns-1` 当前值与 `target` 前向距离**恰为 2³¹**（正对点）；`ns-2` 等以小距离落后并附两跳链作可比较对照 | `/audit` 对 `ns-1` 判 `ambiguous` 阻塞；`/transfer-plan` 即使有连通边也 **axfr / target_incomparable**，对照副本 ixfr |
| `fork` | 同一 `from_serial` 指向两个不同 `to_serial`（两条边各自严格 newer），另有仅一支通向 target 的延续边 | `/transfer-plan`、`/retention-plan`、`/domain-retention-plan` 整体校验返回 **422 delta_fork**，不静默选边 |
| `loop` | 三条严格 newer 边前向距离 `d1+d2+d3 = 2³²`（均 ∈ [2, 2³¹−1]），构成跨回绕闭环；`ns-2` 已在 target | 三条边各自合法故整体校验通过；`ns-1` 沿链行进回到已访问节点，`/transfer-plan` 判 **axfr / loop**（严格 newer 环至少需 3 条边） |

请求（服务监听 **8000** 端口，以下示例同其他端点）：

```bash
curl -s http://localhost:8000/serial-scenarios/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "seed": 20240921,
    "replica_count": 3,
    "scenario_count": 4,
    "include": ["wrap", "ambiguous", "fork", "loop"]
  }'
```

响应（顶层回显请求参数；每个场景的 `deltas` 是乱序样本，可原样 POST 给
`/transfer-plan` 等端点；`trigger_points` 为触发点说明，类型外字段为 `null`）：

```json
{
  "seed": 20240921,
  "replica_count": 3,
  "scenario_count": 4,
  "include": ["wrap", "ambiguous", "fork", "loop"],
  "scenarios": [
    {
      "index": 0,
      "label": "wrap",
      "replicas": [
        {"name": "ns-1", "current": 4294967288},
        {"name": "ns-2", "current": 4294967242},
        {"name": "ns-3", "current": 4294967229}
      ],
      "target": 5,
      "deltas": [
        {"from_serial": 4294967288, "to_serial": 4294967295},
        {"from_serial": 4294967295, "to_serial": 0},
        {"from_serial": 0, "to_serial": 5}
      ],
      "trigger_points": [
        {"kind": "wrap_edge", "message": "增量 4294967295 -> 0 跨过 2^32-1 回绕点，前向距离为 1，按 RFC 1982 仍是严格 newer …",
         "replica": null, "current": null, "target": null,
         "from_serial": 4294967295, "to_serial": 0, "to_serials": null,
         "nodes": null, "forward_distances": null, "forward_distance": 1},
        {"kind": "wrap_advance", "message": "副本 ns-1 当前值在整数上大于目标，但前向距离 13 跨过回绕点后仍属严格 newer …",
         "replica": "ns-1", "current": 4294967288, "target": 5,
         "from_serial": null, "to_serial": null, "to_serials": null,
         "nodes": null, "forward_distances": null, "forward_distance": 13}
      ]
    },
    {
      "index": 1,
      "label": "ambiguous",
      "replicas": [
        {"name": "ns-1", "current": 3269076739},
        {"name": "ns-2", "current": 1121592902},
        {"name": "ns-3", "current": 1121592927}
      ],
      "target": 1121593091,
      "deltas": [
        {"from_serial": 1121592990, "to_serial": 1121593091},
        {"from_serial": 1121592902, "to_serial": 1121592990}
      ],
      "trigger_points": [
        {"kind": "antipodal_pair", "message": "副本 ns-1 当前值与目标的前向距离恰为 2^31 … /transfer-plan 即使存在可连通的增量，也必须降级为 axfr / target_incomparable",
         "replica": "ns-1", "current": 3269076739, "target": 1121593091,
         "from_serial": null, "to_serial": null, "to_serials": null,
         "nodes": null, "forward_distances": null, "forward_distance": 2147483648},
        {"kind": "comparable_reference", "message": "对照副本 ns-2 的前向距离为 189（< 2^31），严格 newer 且可比较 …",
         "replica": "ns-2", "current": 1121592902, "target": 1121593091,
         "from_serial": null, "to_serial": null, "to_serials": null,
         "nodes": null, "forward_distances": null, "forward_distance": 189}
      ]
    },
    {
      "index": 2,
      "label": "fork",
      "replicas": [
        {"name": "ns-1", "current": 1179580566},
        {"name": "ns-2", "current": 1179582835},
        {"name": "ns-3", "current": 1179580449}
      ],
      "target": 1179582835,
      "deltas": [
        {"from_serial": 1179580618, "to_serial": 1179582835},
        {"from_serial": 1179580566, "to_serial": 1179580618},
        {"from_serial": 1179580566, "to_serial": 1179580951}
      ],
      "trigger_points": [
        {"kind": "fork", "message": "from_serial=1179580566 同时指向 1179580618 与 1179580951 … 返回 422 delta_fork，绝不静默二选一",
         "replica": null, "current": null, "target": null,
         "from_serial": 1179580566, "to_serial": null,
         "to_serials": [1179580618, 1179580951],
         "nodes": null, "forward_distances": null, "forward_distance": null}
      ]
    },
    {
      "index": 3,
      "label": "loop",
      "replicas": [
        {"name": "ns-1", "current": 1952432451},
        {"name": "ns-2", "current": 3019776389},
        {"name": "ns-3", "current": 1952432433}
      ],
      "target": 3019776389,
      "deltas": [
        {"from_serial": 3019776388, "to_serial": 769473376},
        {"from_serial": 1952432451, "to_serial": 3019776388},
        {"from_serial": 769473376, "to_serial": 1952432451}
      ],
      "trigger_points": [
        {"kind": "loop", "message": "三条严格 newer 边的前向距离 1067343937 + 2044664284 + 1182959075 之和恰为 2^32 … /transfer-plan 判定 axfr / loop",
         "replica": null, "current": null, "target": null,
         "from_serial": null, "to_serial": null, "to_serials": null,
         "nodes": [1952432451, 3019776388, 769473376],
         "forward_distances": [1067343937, 2044664284, 1182959075],
         "forward_distance": null},
        {"kind": "loop_edge", "message": "环路边 3019776388 -> 769473376，前向距离 2044664284（整数上跨过 2^32-1 回绕）",
         "replica": null, "current": null, "target": null,
         "from_serial": 3019776388, "to_serial": 769473376, "to_serials": null,
         "nodes": null, "forward_distances": null, "forward_distance": 2044664284}
      ]
    }
  ]
}
```

> 只生成部分标签（如 `"include": ["wrap"]`）或轮转多轮（如 `scenario_count=8`）
> 时规则相同；`ambiguous` 在 `replica_count=1` 时不附对照链（`deltas` 为空）。
> 生成结果与现有端点串联的完整行为（ixfr 跨回绕、target_incomparable、
> delta_fork、axfr/loop）见测试 `tests/test_serial_scenarios.py` 的端到端用例。

### 输入校验（422）

以下情况返回 **422 Unprocessable Entity**：

- 序列号越界：必须为整数且在 `[0, 4294967295]`（即 `[0, 2^32-1]`）；
- 副本名重复（`/audit`、`/propose`、`/transfer-plan`、`/retention-plan`、
  `/domain-retention-plan`、`/rollout-gate`、`/propagation-slo`、
  `/divergence-windows`、`/delta-consensus` 均检查）；
- 副本名为空 / `replicas` 为空数组；
- `/transfer-plan` 额外检查：增量非严格 newer（`delta_not_strictly_newer`）、
  同一 `from_serial` 分叉（`delta_fork`）；
- `/retention-plan` 额外检查：副本数超过 12（`too_many_replicas`）、
  相同增量携带不同 `bytes`（`delta_bytes_conflict`）、
  `bytes` / `priority` / `budget` 为负数或非整数；
- `/domain-retention-plan` 额外检查：在 `/retention-plan` 全部校验之外，
  每个副本的 `failure_domain` 必须为非空字符串（空白字符串同样被拒绝）；
- `/rollout-gate` 额外检查：`baseline` 必须是合法序列号；`quorum` 必须为正整数；
  每个副本至少一条观测，观测 `serial` 必须是合法序列号；`timestamp` 若存在必须是
  非负整数，且同一副本内严格递增——重复（`duplicate_observation_timestamp`）
  或倒序（`observation_timestamp_out_of_order`）均返回 422。
- `/propagation-slo` 额外检查：`deadline_seconds` 必须是非负整数；`releases`
  至少一条，`released_at` 必须是非负整数且各发布之间严格递增——重复
  （`duplicate_release_timestamp`）或倒序（`release_timestamp_out_of_order`）
  返回 422；每个副本的 `failure_domain` 必须为非空字符串（空白字符串同样被
  拒绝）；观测 `timestamp` 必填、非负整数，且同一副本内严格递增——重复
  （`duplicate_observation_timestamp`）或倒序
  （`observation_timestamp_out_of_order`）返回 422。
- `/divergence-windows` 额外检查：观测 `serial` 必须是合法序列号，
  `timestamp` 必填且为非负整数；同一副本的观测时间戳必须**非递减**
  （允许同刻多条），倒序返回 422（`observation_timestamp_out_of_order`）；
  同一副本同一时间戳出现不同序列号返回 422
  （`observation_serial_conflict`，同刻同值的重复观测合法并去重）；
  副本名重复（`duplicate_replica_name`）同样返回 422。
- `/delta-consensus` 额外检查：`quorum` 必须是正整数；`digest` 必须是非空
  字符串（纯空白同样被拒绝）；每条上报边必须 RFC 1982 严格 newer
  （`delta_not_strictly_newer`，错误体含镜像名与关系）；单个镜像对同一
  `from_serial` 上报不同 `to_serial` 或不同 `digest` 返回 422
  （`mirror_version_conflict`，返回冲突的两份完整上报），完全相同的三元组
  重复合法并去重；镜像名重复（`duplicate_replica_name`）同样返回 422。
- `/serial-scenarios/generate` 额外检查：`seed` 必须是 32 位无符号整数
  `[0, 4294967295]`（拒绝 bool / 浮点 / 字符串 / 越界）；`replica_count`
  必须在 `[1, 12]`、`scenario_count` 必须在 `[1, 32]`（非整数同样拒绝）；
  `include` 必须是非空、无重复、元素仅限 `wrap` / `ambiguous` / `fork` /
  `loop` 的标签子集（空数组、未知标签、重复标签或缺字段均返回 422）。

## 算法说明

`/propose` 中，每个当前值 `c` 的合法候选范围是开弧
`{c+1, …, c+2³¹−1}（mod 2³²）`；所有开弧的交集非空时必为一段连续弧，
最优解必位于弧的最小元素，即某个 `(c + 1) mod 2³²`。
因此只需枚举各副本当前值的“紧邻后继”，检查其对所有副本的前向距离均在 `[1, 2³¹−1]`，
再按 `(最大前向距离, 候选数值)` 取最小即可。

`/transfer-plan` 中，增量先经 `build_delta_chain` 校验去重为
`from_serial -> to_serial` 的单后继映射（故完整链唯一，不存在分叉分支选择）；
`plan_transfer` 从副本当前值出发逐跳取后继，**不做整数排序**：
相等即 `up_to_date`，两端前向距离恰为 2³¹ 即 `target_incomparable`；
否则沿边行进直到抵达目标（`ixfr`，记录沿途有序增量）、
找不到后继（`gap`）或重复经过已访问序列号（`loop`）。
注意严格 newer 的边构成的合法环路只能跨 2³² 回绕（至少 3 条边、各边前向距离之和恰为 2³²），
两条边无法在不违反严格 newer 的前提下成环。

`/retention-plan` 中，带字节数的增量先经 `build_sized_delta_chain` 校验去重为
`from_serial -> to_serial` 映射与 `(from_serial, to_serial) -> bytes` 映射；
`plan_retention` 先在**完整**集合上对每个副本跑 `plan_transfer`：
`up_to_date` 恒被覆盖（零成本），`ixfr` 副本的链上增量即其**需求集**，
`gap` / `loop` / `target_incomparable` 在完整集合下即不可达、原因保持。
由于链是单后继映射，副本被子集覆盖当且仅当其需求集被该子集包含，
因此最优保留集必为某组副本需求集之并；副本数不超过 12，
枚举全部副本子集（至多 2¹² = 4096 个，位掩码按位 OR 求并集）即可精确求得
`(priority 总和, 覆盖副本数, -占用字节)` 字典序最大的子集，
再在同分子集中取增量序列（按 `(from_serial, to_serial)` 升序）字典序最小者。
空子集恒可行（`budget >= 0`），故预算再小也能正常返回 200。

`/domain-retention-plan` 复用同一需求集枚举（与 `plan_retention` 共用
`build_sized_delta_chain` 与逐副本 `plan_transfer` 判定），区别仅在裁决键：
先把请求中出现的 failure_domain 按名称升序固定编号，`up_to_date` 副本的
priority 作为常量并入各域基值；对每个可行子集统计各域被覆盖 priority，
将各域总和**升序排列**为元组，按
`(升序域向量, 全体 priority, 覆盖副本数, -占用字节)` 字典序取最大，
最后再以增量序列字典序最小破平手。域向量包含全部域（未覆盖域分量为 0），
故分量排列不随子集变化；连同增量 / 域的固定排序，相同输入的结果完全可复现。

`/rollout-gate` 中，每个副本的观测序列先做时间戳严格递增校验（缺失时间戳跳过），
再逐对按 RFC 1982 标记关系（任一时间戳缺失的相邻对标记 `missing_time_order`，
不判定序列号关系）。首次达到基线（等于或严格更新，即前向距离 ∈ [0, 2³¹)）的
观测即区间起点，之后仅沿 `newer` / `equal` 且仍达到基线的相邻对扩展区间——
注意一次 `newer` 推进可能跨越大半环后重新跌破基线（如基线 10：
`10 -> 10+2³¹−1 -> 8`，第二步距离 2³¹−1 仍是 newer，但 8 相对基线为 older），
因此每一步都重新校验是否达到基线。quorum 判定用**事件扫掠**：每个闭区间
`[start, end]` 产生 `+1` / `−1` 两个事件，同一时间点先计入 `end` 事件再移除
（闭区间端点处仍算在区间内），扫描得到并发数阶梯序列，首个 `>= quorum` 的
时间点即 `first_satisfied_at`，其后首个 `< quorum` 的时间点即
`dropped_below_at`（只报告第一个满足区间）。

`/propagation-slo` 中，发布时间与观测时间先在路由层校验严格递增；对每次
发布取窗口 `[released_at[i], released_at[i+1])`（最后一次发布延伸到观测
结束）内的全部观测，按 RFC 1982 相对本次发布版本分为**到达**
（`equal` / `newer`）与**回退**（`older` / `ambiguous`）两类事件，按
`(时间戳, 回退优先于到达, 副本名)` 排序后扫描：到达事件使副本进入已到达
状态，并在其域尚未到达时记录域的 `reached_at`；**已到达副本**的回退事件
使其所在域的到达状态失效（`reached_at` 清空），须等待后续到达事件重新
到达——同域其他副本仍处于已到达状态并不能使域自动恢复。同一时间戳先
处理回退再处理到达（回退只使早于该时刻的到达失效）。首次全部域均到达的
时间戳即 `converged_at`（此时必等于各域 `reached_at` 的最大值），扫描随即
停止，之后的回退不再影响结果；窗口结束仍有域未到达则 `converged_at=null`
并列出升序的 `missing_domains`。各窗口的到达状态相互独立、互不带入。

`/divergence-windows` 中，各副本观测先经
`check_divergence_observation_order` 校验（时间戳非递减；同刻同副本的序列号
必须一致，同刻同值重复允许），再汇成 `(时间戳, 副本下标, 序列号)` 事件按序
扫掠：同一时间戳的全部更新**先原子合并**到各副本最新值表，再对合并后的
整体状态调用 `has_common_successor`——它复用 `/propose` 的
`propose_serial`，仅判断各副本开弧 `{c+1, …, c+2³¹−1}`（模 2³²）的交集
是否非空，因此严格遵循 RFC 1982 环形规则而非整数极差（正对点、跨度达
2³¹−1 的非回绕状态均无候选；回绕点附近整数极差近 2³² 的状态反而可行）。
尚无观测的副本不在最新值表中、不参与判定。连续的不可行状态合并为窗口：
开窗时对当前全部已知副本的最新值快照为 `entries`；窗口内新出现的副本在其
首个观测时刻（合并后仍不可行）以首条观测序列号补入；遇到首个可行状态即
闭窗，`end_at` 取该状态时间、`duration_seconds = end_at − start_at`；
扫掠结束窗口仍开启则 `end_at` / `duration_seconds` 为 null。

`/delta-consensus` 中，`validate_mirror_reports` 分两遍校验：先按镜像输入
顺序逐条拒绝非 RFC 1982 严格 newer 的上报边（`delta_not_strictly_newer`），
再逐镜像检查同一 `from_serial` 的版本——完全相同的三元组去重，不同
`to_serial` 或不同 `digest` 即 `mirror_version_conflict`（两遍顺序保证
报错确定可复现）。通过后 `evaluate_delta_consensus` 先按完整三元组聚合
上报镜像名集合（同镜像重复自然只计一票），票数达 `quorum` 者进入可信集合；
可信图表示为 `from_serial -> [(to_serial, digest)...]` 的升序出边表，同一
序号对上两个达法定人数的摘要同样产生多条出边（内容分叉）。从每个无入边
节点沿“唯一出边”行进，遇无出边（gap）/ 多出边（fork）/ 后继已访问
（loop）即停；以 `(-边数, 边序列, 根节点)` 字典序选出最长可信链。目标
可达性独立于最长链：合并各条唯一路径**确定到达**的节点集合（含根、分叉点
与回环点，但不含分叉后的分支与环的第二圈），目标在其中才为 true，因此
“目标位于更长可信历史之前”与“目标已安全到达、其后才分叉/回环”都算可达，
而纯环（无根可入）中的节点不可达。环路由 `_trusted_cycles` 沿唯一后继
指针枚举（每个节点至多属于一环），规范化为从最小 `from_serial` 起并按首边
排序。逐镜像证据以镜像去重三元组集合比对：可信序号对未上报为 `missing`
（可信摘要版本全部列出）、序号对可信但摘要未达票数为 `minority_digests`
（含票数与可信摘要对照）、其余上报为 `extra_edges`（含票数），各列表内部
统一按三元组 / 序号对升序，连同 `voters` 名称升序，相同输入恒得相同结果。

`/serial-scenarios/generate` 中（纯函数模块 `app/scenarios.py`，零第三方
依赖），标签层先按 `include` 的请求顺序对 `scenario_count` 个位置做与 seed
无关的轮转铺放（故每个被请求标签在前 `len(include)` 个场景至少出现一次）；
数值层为第 i 个标签为 label 的场景用
`(seed * 1000003 + i * 10007 + code(label) * 97 + 0x5EED) mod 2^64`
播种**独立**的 `random.Random`——刻意避开 Python 按进程加盐的字符串 / 元组
`hash()`，从而跨进程、跨平台（MT19937 为语言规范算法）可复现，且追加场景
不改变靠前场景的数值。各生成器只在 32 位区间内采样并直接以
:mod:`app.serial` 的环形算术构造边界：`wrap` 把链锚定在 `2^32-1` 前
1–15、target 取 `[0, 7]`，链必含前向距离 1 的 `4294967295 -> 0`；
`ambiguous` 以 `target = (origin + 2^31) mod 2^32` 精确放置正对点；
`fork` 从同一 origin 采两条距离互不相同（分别取自 `[1,63]` 与
`[128,511]`）的严格 newer 边；`loop` 采 `d1、d2 ∈ [2, 2^31-1]` 且
`d1+d2 > 2^31`，则 `d3 = 2^32-d1-d2` 落在 `[2, 2^31-1]`，三边均严格
newer 而距离之和恰为 `2^32`（严格 newer 两边无法成环，故环至少三边）。
样本最后用该场景自己的 Random 洗牌，以**无序**形式输出。

## 测试

核心环形算术不依赖 FastAPI，可直接用标准库运行（另含小位宽空间对暴力穷举的对照测试，
`/transfer-plan` 的去重、分叉、缺口、环路、回绕与正对点测试，
`/retention-plan` 的字节数冲突、共享链并集、三级平局裁决、
`budget` 原因与零预算测试，`/domain-retention-plan` 的逐域 maximin
向量优先于全体总和、域向量锚定与排序、覆盖数 / 字节 / 增量序列平局裁决、
covered/total 汇总与稳定性测试，以及 `/rollout-gate` 的时间戳排序校验、
相邻对 RFC 1982 标记、回绕推进、有效推进区间、事件扫掠 quorum 时间窗
与 422 / OpenAPI 示例测试，以及 `/propagation-slo` 的发布 / 观测时间排序
校验、窗口边界（左闭右开）、逐域到达与回退失效、同时刻回退优先、
收敛后回退忽略、缺失域与 deadline 边界测试，以及 `/divergence-windows`
的时间戳非递减 / 同刻序列号冲突校验、同刻原子合并、RFC 1982 共同后继
判定（整数极差对照）、连续状态合并 / 恢复分窗、未恢复窗口 null 字段、
迟到副本进入窗口 entries 与 422 路由测试，以及 `/delta-consensus`
的严格推进 / 单镜像版本冲突校验、完整三元组计票与同镜像去重、摘要分票、
缺口 / 分叉（含同序号对双可信摘要）/ 环路（纯环与带尾环）/ 回绕链组装、
最长链选择与确定性平局、目标可达性（链中间、分叉点、分支后、纯环）、
逐镜像缺失 / 少数摘要 / 额外边证据与稳定排序、422 路由测试，
以及 `/serial-scenarios/generate` 的确定性（含 golden 值与前缀稳定）、
标签轮转与 seed 无关性、独立整数混合播种、32 位范围与乱序样本、
wrap / ambiguous / fork / loop 四类场景与现有 RFC 1982 逻辑的交叉验证
（ixfr 跨回绕、target_incomparable、delta_fork、axfr/loop）、
数量 / seed / include 的 422 校验与端到端路由测试）：

```bash
python3 -m unittest discover -s tests -v
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由与异常（/audit、/propose、/transfer-plan、
│   │                  #   /retention-plan、/domain-retention-plan、/rollout-gate、
│   │                  #   /propagation-slo、/divergence-windows、/delta-consensus、
│   │                  #   /serial-scenarios/generate）
│   ├── schemas.py     # 请求/响应模型与 422 校验（含 /rollout-gate、
│   │                  #   /propagation-slo、/divergence-windows、/delta-consensus
│   │                  #   与 /serial-scenarios/generate 的 OpenAPI 示例）
│   ├── serial.py      # RFC 1982 环形算术、增量链规划、保留规划、上线门禁
│   │                  #   与传播 SLO 评估、分裂窗口扫掠、增量共识
│   │                  #   （纯函数，零第三方依赖）
│   └── scenarios.py   # 确定性伪随机边界场景生成（wrap / ambiguous /
│                      #   fork / loop；纯函数，仅依赖标准库 random）
├── tests/test_serial.py
├── tests/test_transfer_plan.py
├── tests/test_retention_plan.py
├── tests/test_domain_retention_plan.py
├── tests/test_rollout_gate.py
├── tests/test_propagation_slo.py
├── tests/test_divergence_windows.py
├── tests/test_delta_consensus.py
├── tests/test_serial_scenarios.py
├── requirements.txt
├── Dockerfile
├── compose.yaml
└── README.md
```
