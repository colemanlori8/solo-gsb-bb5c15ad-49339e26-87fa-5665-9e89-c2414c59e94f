"""确定性 SOA 序列号边界场景生成（POST /serial-scenarios/generate）。

根据 ``(seed, replica_count, scenario_count, include)`` 用标准库
``random.Random`` 为每个场景派生**独立**的伪随机流，生成覆盖 RFC 1982
边界条件的副本当前序列号序列与无序 IXFR 增量样本：

* ``wrap``       —— 增量与副本推进跨过 2^32-1 回绕点（4294967295 -> 0 仍为 newer）；
* ``ambiguous``  —— 副本当前值与目标相差恰为 2^31（正对点，RFC 1982 不可比较）；
* ``fork``       —— 同一 from_serial 指向两个不同 to_serial（喂给 /transfer-plan
                    会在整体校验阶段得到 422 delta_fork）；
* ``loop``       —— 三条严格 newer 边的前向距离之和恰为 2^32，构成跨回绕点环路
                    （/transfer-plan 沿链行进回到已访问节点，axfr / loop）。

相同输入恒得相同场景标签与数值：每场景用 ``(seed, index, label)`` 做确定性
64 位整数混合后再播种独立 Random（不使用随进程加盐的元组 / 字符串哈希），
故增减 scenario_count 不影响靠前场景的内容。本模块为纯函数，零第三方依赖。
"""

from __future__ import annotations

import random
from typing import Callable, Dict, List, Sequence, Tuple

from .serial import HALF, MOD, MAX_SERIAL, forward_distance

# ---------------------------------------------------------------------------
# 参数上限（与请求模型一致；越界由 schemas 层返回 422）
# ---------------------------------------------------------------------------

MAX_SCENARIO_REPLICAS = 12
"""单个场景中生成的副本数上限。"""

MAX_SCENARIO_COUNT = 32
"""单次请求生成的场景数上限。"""

WRAP = "wrap"
AMBIGUOUS = "ambiguous"
FORK = "fork"
LOOP = "loop"

SCENARIO_LABELS: Tuple[str, ...] = (WRAP, AMBIGUOUS, FORK, LOOP)
"""全部可选场景标签（include 必须是它的非空无重复子集）。"""

_LABEL_CODE = {WRAP: 1, AMBIGUOUS: 2, FORK: 3, LOOP: 4}

# 每个场景生成的无序增量：[from_serial, to_serial]
_RawDeltas = List[List[int]]
# 触发点说明：字段是 ScenarioTriggerPoint 的子集
_Trigger = Dict[str, object]


def replica_names(count: int) -> List[str]:
    """生成稳定的副本名 ``ns-1`` ... ``ns-count``（与 seed 无关）。"""
    return [f"ns-{i + 1}" for i in range(count)]


def _scenario_rng(seed: int, index: int, label: str) -> random.Random:
    """用 (seed, index, label) 确定性派生该场景独立的伪随机流。

    只做 64 位整数混合——Python 的字符串 / 元组哈希按进程加盐，不能用于播种。
    """
    mixed = (
        seed * 1_000_003
        + index * 10_007
        + _LABEL_CODE[label] * 97
        + 0x5EED
    ) & 0xFFFFFFFFFFFFFFFF
    return random.Random(mixed)


# ---------------------------------------------------------------------------
# wrap：跨 2^32-1 回绕点
# ---------------------------------------------------------------------------


def _gen_wrap(
    rng: random.Random, count: int, names: Sequence[str]
) -> Tuple[int, List[int], _RawDeltas, List[_Trigger]]:
    """目标在回绕点附近的小号区，增量链跨过 ``4294967295 -> 0``。

    ``ns-1`` 停在链起点（沿链 IXFR 跨过回绕点到达 target）；其余副本以
    ``[1, 127]`` 的小前向距离落后于 target——模 2^32 后大多落在回绕点另一侧
    的大号区（整数上“大于”目标，前向路径却跨过回绕点）。
    """
    target = rng.randrange(0, 8)
    # 链起点距 2^32-1 留出 1..15 的前向距离（randrange 下界取 1，避免自环）
    wrap_anchor = MAX_SERIAL - rng.randrange(1, 16)

    deltas: _RawDeltas = [[wrap_anchor, MAX_SERIAL], [MAX_SERIAL, 0]]
    if target > 0:
        deltas.append([0, target])

    currents: List[int] = [wrap_anchor]
    for _ in range(1, count):
        lag = rng.randrange(1, 128)
        currents.append((target - lag) % MOD)

    triggers: List[_Trigger] = [
        {
            "kind": "wrap_edge",
            "message": (
                "增量 4294967295 -> 0 跨过 2^32-1 回绕点，前向距离为 1，"
                "按 RFC 1982 仍是严格 newer；/transfer-plan 沿环形链逐跳追踪，"
                "不会按整数大小把它误判为回退"
            ),
            "from_serial": MAX_SERIAL,
            "to_serial": 0,
            "forward_distance": 1,
        }
    ]
    for name, current in zip(names, currents):
        if current > target:  # 整数上“更大”，前向路径却跨过回绕点
            triggers.append(
                {
                    "kind": "wrap_advance",
                    "message": (
                        f"副本 {name} 当前值在整数上大于目标，但前向距离 "
                        f"{forward_distance(current, target)} 跨过回绕点后"
                        "仍属严格 newer（/audit 判为 newer 而非 older）"
                    ),
                    "replica": name,
                    "current": current,
                    "target": target,
                    "forward_distance": forward_distance(current, target),
                }
            )

    rng.shuffle(deltas)  # 增量以无序样本形式给出
    return target, currents, deltas, triggers


# ---------------------------------------------------------------------------
# ambiguous：前向距离恰为 2^31 的正对点
# ---------------------------------------------------------------------------


def _gen_ambiguous(
    rng: random.Random, count: int, names: Sequence[str]
) -> Tuple[int, List[int], _RawDeltas, List[_Trigger]]:
    """``ns-1`` 的当前值与 target 相差正好 2^31（RFC 1982 正对点）。

    其余副本以 ``[1, 255]`` 的小前向距离落后于 target，作为可比较的对照；
    ``ns-2``（存在时）携带一条两跳的严格 newer 增量链可 IXFR 到达 target。
    """
    antipode_origin = rng.randrange(0, MOD)
    target = (antipode_origin + HALF) % MOD

    currents: List[int] = [antipode_origin]
    for _ in range(1, count):
        lag = rng.randrange(1, 256)
        currents.append((target - lag) % MOD)

    triggers: List[_Trigger] = [
        {
            "kind": "antipodal_pair",
            "message": (
                f"副本 {names[0]} 当前值与目标的前向距离恰为 2^31"
                "（RFC 1982 SERIAL_BOUND），正对点不可比较：/audit 判为 "
                "ambiguous 并阻塞发布；/transfer-plan 即使存在可连通的增量，"
                "也必须降级为 axfr / target_incomparable"
            ),
            "replica": names[0],
            "current": antipode_origin,
            "target": target,
            "forward_distance": HALF,
        }
    ]

    deltas: _RawDeltas = []
    if count >= 2:
        ref_current = currents[1]
        distance = forward_distance(ref_current, target)  # 必在 [1, 255]
        if distance >= 2:
            hop = rng.randrange(1, distance)
            midpoint = (ref_current + hop) % MOD
            deltas = [[ref_current, midpoint], [midpoint, target]]
        else:
            deltas = [[ref_current, target]]
        triggers.append(
            {
                "kind": "comparable_reference",
                "message": (
                    f"对照副本 {names[1]} 的前向距离为 {distance}（< 2^31），"
                    "严格 newer 且可比较，可沿增量链 IXFR 到达目标"
                ),
                "replica": names[1],
                "current": ref_current,
                "target": target,
                "forward_distance": distance,
            }
        )

    rng.shuffle(deltas)
    return target, currents, deltas, triggers


# ---------------------------------------------------------------------------
# fork：同一 from_serial 指向两个不同 to_serial
# ---------------------------------------------------------------------------


def _gen_fork(
    rng: random.Random, count: int, names: Sequence[str]
) -> Tuple[int, List[int], _RawDeltas, List[_Trigger]]:
    """分叉样本：两条同起点边各自严格 newer，但指向不同目标。

    ``ns-1`` 停在分叉起点；``ns-2``（存在时）已在 target（up_to_date 对照）；
    其余副本停在起点之前的整数小号区（沿链会先撞上分叉）。另附一条
    ``branch_a -> target`` 的延续边，表明仅其中一个分支能通向目标——
    但整体校验阶段就会以 delta_fork 拒绝，不会静默选边。
    """
    origin = rng.randrange(10_000, HALF - 10_000)
    branch_a = origin + rng.randrange(1, 64)
    branch_b = origin + rng.randrange(128, 512)
    target = origin + rng.randrange(1024, 4096)

    deltas: _RawDeltas = [
        [origin, branch_a], [origin, branch_b], [branch_a, target]
    ]

    currents: List[int] = [origin]
    for i in range(1, count):
        if i == 1:
            currents.append(target)
        else:
            currents.append(origin - rng.randrange(1, 128))

    triggers: List[_Trigger] = [
        {
            "kind": "fork",
            "message": (
                f"from_serial={origin} 同时指向 {branch_a} 与 {branch_b} 两个"
                "不同的 to_serial（两条边各自都是严格 newer）；增量集合存在"
                "分叉，喂给 /transfer-plan、/retention-plan 或 "
                "/domain-retention-plan 会在整体校验阶段返回 422 delta_fork，"
                "绝不静默二选一"
            ),
            "from_serial": origin,
            "to_serials": [branch_a, branch_b],
        }
    ]

    rng.shuffle(deltas)
    return target, currents, deltas, triggers


# ---------------------------------------------------------------------------
# loop：三条严格 newer 边前向距离之和恰为 2^32
# ---------------------------------------------------------------------------


def _gen_loop(
    rng: random.Random, count: int, names: Sequence[str]
) -> Tuple[int, List[int], _RawDeltas, List[_Trigger]]:
    """跨回绕点的严格 newer 三环样本（严格 newer 环至少需要 3 条边）。

    采样 d1、d2 ∈ [2, 2^31-1] 且 d1+d2 > 2^31，则 d3 = 2^32-d1-d2 落在
    [2, 2^31-1]，三条边都严格 newer 而距离之和恰为 2^32。target 取 n1+1
    （d2 >= 2 保证它不是环节点），``ns-1`` 从 n0 沿 n0->n1->n2->n0 行进，
    未达 target 先回到 n0，/transfer-plan 判 axfr / loop。
    """
    start = rng.randrange(0, HALF)
    while True:
        d1 = rng.randrange(2, HALF)
        d2 = rng.randrange(2, HALF)
        if d1 + d2 > HALF:
            break
    d3 = MOD - d1 - d2  # 2 <= d3 <= 2^31-1，三条边均严格 newer

    n0 = start
    n1 = (start + d1) % MOD
    n2 = (start + d1 + d2) % MOD
    target = (n1 + 1) % MOD

    deltas: _RawDeltas = [[n0, n1], [n1, n2], [n2, n0]]

    currents: List[int] = [n0]
    for i in range(1, count):
        if i == 1:
            currents.append(target)
        else:
            currents.append((n0 - rng.randrange(1, 64)) % MOD)

    triggers: List[_Trigger] = [
        {
            "kind": "loop",
            "message": (
                f"三条严格 newer 边的前向距离 {d1} + {d2} + {d3} 之和恰为 "
                "2^32，构成跨越回绕点的闭环；从任一环节点出发沿唯一后继行进"
                "都会回到已访问序列号，/transfer-plan 判定 axfr / loop"
                "（严格 newer 的两条边无法成环，合法环路至少需要 3 条边）"
            ),
            "nodes": [n0, n1, n2],
            "forward_distances": [d1, d2, d3],
        }
    ]
    for frm, to, dist in zip((n0, n1, n2), (n1, n2, n0), (d1, d2, d3)):
        triggers.append(
            {
                "kind": "loop_edge",
                "message": (
                    f"环路边 {frm} -> {to}，前向距离 {dist}"
                    + ("（整数上跨过 2^32-1 回绕）" if to < frm else "")
                ),
                "from_serial": frm,
                "to_serial": to,
                "forward_distance": dist,
            }
        )

    rng.shuffle(deltas)
    return target, currents, deltas, triggers


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------

_GENERATORS: Dict[
    str, Callable[[random.Random, int, Sequence[str]], Tuple]
] = {
    WRAP: _gen_wrap,
    AMBIGUOUS: _gen_ambiguous,
    FORK: _gen_fork,
    LOOP: _gen_loop,
}


def generate_serial_scenarios(
    seed: int,
    replica_count: int,
    scenario_count: int,
    include: Sequence[str],
) -> List[dict]:
    """确定性生成 ``scenario_count`` 个边界场景。

    场景标签按 *include* 的请求顺序**轮转**铺到 scenario_count 个位置
    （include=[a,b]、scenario_count=4 -> a,b,a,b），从而：

    * 标签序列只由 include 顺序与 scenario_count 决定，与 seed 无关；
    * include 中每个标签在前 ``len(include)`` 个场景中至少出现一次。

    每个场景的数值由 :func:`_scenario_rng` 派生的独立 Random 生成。
    返回 dict 列表，可直接由 ``SerialScenarioGenerateResponse`` 校验序列化：
    每项含 ``index`` / ``label`` / ``replicas`` / ``target`` / ``deltas``
    （已洗牌的无序样本）/ ``trigger_points``。
    """
    labels = list(include)
    if not labels:
        raise ValueError("include 不能为空")
    if any(label not in _LABEL_CODE for label in labels):
        raise ValueError(f"include 含未知场景标签，可选：{', '.join(SCENARIO_LABELS)}")
    if len(set(labels)) != len(labels):
        raise ValueError("include 中的场景标签不能重复")
    if replica_count < 1 or replica_count > MAX_SCENARIO_REPLICAS:
        raise ValueError(
            f"replica_count 必须在 [1, {MAX_SCENARIO_REPLICAS}] 范围内"
        )
    if scenario_count < 1 or scenario_count > MAX_SCENARIO_COUNT:
        raise ValueError(
            f"scenario_count 必须在 [1, {MAX_SCENARIO_COUNT}] 范围内"
        )
    if not 0 <= seed <= MAX_SERIAL:
        raise ValueError(f"seed 必须在 [0, {MAX_SERIAL}] 范围内")

    names = replica_names(replica_count)
    scenarios: List[dict] = []
    for index in range(scenario_count):
        label = labels[index % len(labels)]
        rng = _scenario_rng(seed, index, label)
        target, currents, deltas, triggers = _GENERATORS[label](
            rng, replica_count, names
        )
        scenarios.append(
            {
                "index": index,
                "label": label,
                "replicas": [
                    {"name": name, "current": current}
                    for name, current in zip(names, currents)
                ],
                "target": target,
                "deltas": [
                    {"from_serial": frm, "to_serial": to}
                    for frm, to in deltas
                ],
                "trigger_points": triggers,
            }
        )
    return scenarios
