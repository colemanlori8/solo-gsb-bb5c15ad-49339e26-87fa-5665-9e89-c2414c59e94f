"""确定性测试场景生成（POST /serial-scenarios/generate）。

以请求中的 ``seed`` 为唯一随机性来源，用内置的 64 位 SplitMix64 伪随机数
生成器（**不使用** :mod:`random`，避免全局状态与版本差异）确定性地生成：

* 满足 RFC 1982 边界语义的副本序列号序列（含回绕点附近、正对点等锚点）；
* 一组**无序** IXFR 增量样本，覆盖四类场景标签：

  - ``wrap``：跨 2³² 回绕的合法严格推进边（如 ``4294967295 -> 0``）；
  - ``ambiguous``：前向距离恰为 2³¹ 的正对点边（RFC 1982 不可比较，
    在各增量接口必为 422）；
  - ``fork``：同一 ``from_serial`` 指向两个不同 ``to_serial`` 的分叉边
    （各自严格 newer，整体必为 422 ``delta_fork``）；
  - ``loop``：三条各自严格 newer、前向距离之和恰为 2³² 的回绕环
    （两条严格 newer 边不可能成环；``/transfer-plan`` 沿边回到已访问
    序列号，``axfr / loop``）。

相同请求（seed / replica_count / scenario_count / include 相同）恒得完全
相同的场景标签、副本序列、目标与无序增量。所有序列号均为 [0, 2³²-1] 内的
32 位无符号整数。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .serial import HALF, MAX_SERIAL, MOD, compare_serial, forward_distance

# ---------------------------------------------------------------------------
# 数量限制
# ---------------------------------------------------------------------------

MAX_GENERATE_REPLICAS = 12
"""POST /serial-scenarios/generate 允许生成的副本数上限。"""

SCENARIO_LABELS: Tuple[str, ...] = ("ambiguous", "fork", "loop", "wrap")
"""全部受支持的场景标签（亦为输出时的规范顺序）。

按字典序排列：场景的内部构建与最终输出都遵循该顺序，保证相同输入
（无论 include 给出的顺序如何）恒得相同结果。
"""

# 各场景的中文名称（用于触发点说明）
SCENARIO_TITLES = {
    "wrap": "回绕边界",
    "ambiguous": "正对点边界",
    "fork": "分叉边界",
    "loop": "环路边界",
}

_WRAP_NEAR = 8
"""wrap 锚点距 2³²-1 的最大距离 / wrap 边的最大步长。"""

_STRUCT_NEAR = 32
"""fork 分支偏移、loop 长边距 2³¹-1 的回退窗口与填充链步长上界。"""


# ---------------------------------------------------------------------------
# 确定性伪随机数：64 位 SplitMix64
# ---------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1


class DeterministicRandom:
    """以 64 位 SplitMix64 为核心的确定性伪随机数生成器。

    状态推进与取数全部封闭在本类内：不读取系统时间、不依赖 :mod:`random`
    的全局状态，相同种子在任何 Python 版本 / 平台上都产生相同整数序列。
    """

    def __init__(self, seed: int):
        self.state = seed & _MASK64

    def next_u64(self) -> int:
        """推进状态并返回下一个 64 位无符号整数（SplitMix64）。"""
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        """返回 [0, n) 内的无偏整数（Lemire 有界采样，拒绝模偏差）。"""
        # 拒绝边界 = 2^64 mod n = (-n) mod n；单次拒绝概率 <= n/2^64
        threshold = (-n) % n
        while True:
            x = self.next_u64()
            low = (x * n) & _MASK64
            if low >= threshold:
                return (x * n) >> 64

    def randrange(self, low: int, high: int) -> int:
        """返回 [low, high) 内的整数。"""
        return low + self.below(high - low)

    def serial32(self) -> int:
        """返回一个 32 位无符号序列号 [0, 2³²-1]。"""
        return self.next_u64() % MOD

    def shuffle(self, items: List) -> None:
        """Fisher–Yates 原地乱序（随机性只来自本生成器）。"""
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]


# ---------------------------------------------------------------------------
# 场景构建
# ---------------------------------------------------------------------------


class _ScenarioBuilder:
    """在固定随机源上逐个构建场景的增量与锚点序列号。

    场景严格按 :data:`SCENARIO_LABELS` 的规范顺序构建（ambiguous → fork →
    loop → wrap）：先构建的结构把其占用的序列号登记到 ``reserved`` /
    ``froms``，后构建者采样时主动避开，保证四类结构互不串味（例如 wrap
    边不会恰好落在目标或环节点上）。所有采样均带固定重试上限；冲突空间
    相对 2³² 极小，正常首次即命中。
    """

    MAX_ATTEMPTS = 64

    def __init__(self, rng: DeterministicRandom):
        self.rng = rng
        # 目标序列号先行确定（即使只生成 wrap 等场景也需要一个发布目标）
        self.target = rng.serial32()
        # 已登记边（含结构边与填充链），输出前整体乱序
        self.edges: List[Tuple[int, int]] = []
        self.pairs: set = set()
        self.froms: set = set()
        # 结构上“有身份”的序列号：目标、各场景锚点 / 分叉与环节点
        self.reserved: set = {self.target}
        # 场景标签 -> 关键锚点序列号（ambiguous 对极点、fork 起点、
        # loop 的 s1、wrap 的 from_serial）
        self.anchors: Dict[str, int] = {}
        # 场景标签 -> 该场景的全部增量（结构边）
        self.structural: Dict[str, List[Tuple[int, int]]] = {}

    def _add_edge(self, frm: int, to: int) -> None:
        self.edges.append((frm, to))
        self.pairs.add((frm, to))
        self.froms.add(frm)

    def build_ambiguous(self) -> None:
        """正对点：锚点与目标前向距离恰为 2³¹；边 (锚点 -> 目标)。"""
        anchor = (self.target - HALF) % MOD
        self.anchors["ambiguous"] = anchor
        self.structural["ambiguous"] = [(anchor, self.target)]
        self.reserved.add(anchor)
        self._add_edge(anchor, self.target)

    def build_fork(self) -> None:
        """分叉：同一 from_serial 出发的两条短距离严格推进边。"""
        for _ in range(self.MAX_ATTEMPTS):
            base = self.rng.serial32()
            if base in self.reserved or base in self.froms:
                continue
            t1 = (base + 1 + self.rng.below(_STRUCT_NEAR)) % MOD
            t2 = (t1 + 1 + self.rng.below(_STRUCT_NEAR)) % MOD
            if t1 == t2 or t1 == base or t2 == base:
                continue
            nodes = {base, t1, t2}
            if nodes & self.reserved:
                continue
            if (base, t1) in self.pairs or (base, t2) in self.pairs:
                continue
            self.anchors["fork"] = base
            self.structural["fork"] = [(base, t1), (base, t2)]
            self.reserved.update(nodes)
            self._add_edge(base, t1)
            self._add_edge(base, t2)
            return
        raise RuntimeError("fork 场景采样连续冲突")  # pragma: no cover

    def build_loop(self) -> None:
        """回绕环：三条严格推进边，前向距离之和恰为 2³²。

        两条长边贴近 2³¹（d1 = 2³¹-1-r1、d2 = 2³¹-1-r2），短边
        d3 = 2+r1+r2 补满回绕；三者都落在严格 newer 区间 [1, 2³¹-1] 内。
        两条严格 newer 的边距离之和不可能达到 2³²（至多 2³²-2），故合法
        回绕环至少需要三条边：s1 -> s2 -> s3 -> s1。
        """
        for _ in range(self.MAX_ATTEMPTS):
            s1 = self.rng.serial32()
            r1 = self.rng.below(_STRUCT_NEAR)
            r2 = self.rng.below(_STRUCT_NEAR)
            d1 = HALF - 1 - r1
            d2 = HALF - 1 - r2
            # d1 + d2 + d3 = 2*HALF = 2^32，故 s3 + d3 == s1 (mod 2^32)
            d3 = 2 + r1 + r2
            s2 = (s1 + d1) % MOD
            s3 = (s2 + d2) % MOD
            nodes = (s1, s2, s3)
            if len(set(nodes)) != len(nodes):
                continue
            if any(node in self.reserved for node in nodes):
                continue
            if any(node in self.froms for node in nodes):
                continue
            pairs = ((s1, s2), (s2, s3), (s3, s1))
            if any(pair in self.pairs for pair in pairs):
                continue
            self.anchors["loop"] = s1
            self.structural["loop"] = list(pairs)
            self.reserved.update(nodes)
            for frm, to in pairs:
                self._add_edge(frm, to)
            return
        raise RuntimeError("loop 场景采样连续冲突")  # pragma: no cover

    def build_wrap(self) -> None:
        """回绕边：from_serial 贴近 2³²-1，推进后越过回绕点。"""
        for _ in range(self.MAX_ATTEMPTS):
            frm = MAX_SERIAL - self.rng.below(_WRAP_NEAR)
            dist = 1 + self.rng.below(_WRAP_NEAR)
            to = (frm + dist) % MOD
            if frm in self.reserved or to in self.reserved:
                continue
            if frm in self.froms or (frm, to) in self.pairs:
                continue
            self.anchors["wrap"] = frm
            self.structural["wrap"] = [(frm, to)]
            self.reserved.update({frm, to})
            self._add_edge(frm, to)
            return
        raise RuntimeError("wrap 场景采样连续冲突")  # pragma: no cover

    def build_filler_chain(self) -> None:
        """追加一条普通（不回绕、短步长）的合法增量链作为背景样本。

        纯粹由边界边构成的样本集合不够真实，故额外补 1–4 条严格 newer 的
        递增链；链上每个节点都避开既有结构序列号。
        """
        length = 1 + self.rng.below(4)
        for _ in range(self.MAX_ATTEMPTS):
            offsets = [1 + self.rng.below(_STRUCT_NEAR) for _ in range(length)]
            span = sum(offsets)
            if span > MAX_SERIAL:
                continue
            start = self.rng.randrange(0, MOD - span)
            nodes = [start]
            for step in offsets:
                nodes.append(nodes[-1] + step)
            if len(set(nodes)) != len(nodes):
                continue
            if any(node in self.reserved or node in self.froms for node in nodes):
                continue
            chain = list(zip(nodes, nodes[1:]))
            if any(pair in self.pairs for pair in chain):
                continue
            for node in nodes:
                self.reserved.add(node)
            for frm, to in chain:
                self._add_edge(frm, to)
            return
        raise RuntimeError("填充链采样连续冲突")  # pragma: no cover


_BUILDERS = {
    "ambiguous": _ScenarioBuilder.build_ambiguous,
    "fork": _ScenarioBuilder.build_fork,
    "loop": _ScenarioBuilder.build_loop,
    "wrap": _ScenarioBuilder.build_wrap,
}


def _trigger_description(
    label: str,
    edges: List[Tuple[int, int]],
    replica: Optional[str],
    target: int,
) -> str:
    """构造单个场景触发点的中文说明（含具体序列号与预期判定）。"""
    replica_clause = (
        f"副本 {replica} 锚定在该场景的关键序列号上"
        if replica is not None
        else "本场景未单独锚定副本（副本数不足），仅由增量触发"
    )
    if label == "wrap":
        frm, to = edges[0]
        dist = forward_distance(frm, to)
        return (
            f"回绕边界（wrap）：增量 {frm} -> {to} 越过 2^32-1 回绕到 {to}，"
            f"前向距离仅 {dist}，按 RFC 1982 判定为 newer，是合法的严格推进"
            f"（可作为 IXFR 链上的回绕一跳）；{replica_clause}，"
            "对回绕后继的 /audit 同样判定为 newer。"
        )
    if label == "ambiguous":
        frm, to = edges[0]
        return (
            f"正对点边界（ambiguous）：增量 {frm} -> {to} 的前向距离恰为 2^31，"
            "RFC 1982 明确规定二者不可比较；该增量在 /transfer-plan、"
            "/retention-plan、/domain-retention-plan 与 /delta-consensus "
            "均以 422 delta_not_strictly_newer 拒绝；"
            f"{replica_clause}（与目标 {to} 相差 2^31），/audit 判定为 "
            "ambiguous 并阻塞发布，/propose 对此状态返回 409。"
        )
    if label == "fork":
        (base, t1), (_base2, t2) = edges
        return (
            f"分叉边界（fork）：同一 from_serial={base} 同时指向 {t1} 与 {t2}，"
            "两条边单独看都是 RFC 1982 严格推进，但 /transfer-plan 与 "
            "/retention-plan 对无序增量做整体校验时以 422 delta_fork 拒绝"
            f"（增量链不允许分叉、不静默选边）；{replica_clause}（分叉起点）。"
        )
    # loop
    (s1, s2), (s2b, s3), (s3b, back) = edges
    d1 = forward_distance(s1, s2)
    d2 = forward_distance(s2b, s3)
    d3 = forward_distance(s3b, back)
    return (
        f"环路边界（loop）：增量 {s1} -> {s2} 前向距离 {d1}、"
        f"{s2} -> {s3} 前向距离 {d2}、{s3} -> {back} 前向距离 {d3}，"
        "三条边各自都是 RFC 1982 严格 newer，但距离之和恰为 2^32 构成"
        "回绕环（两条严格 newer 边的距离之和至多 2^32-2，故合法环至少"
        "需要三条边）；/transfer-plan 沿边行进会回到已访问序列号，"
        f"{replica_clause}（环上节点），"
        "经该结构到目标的副本一律以 axfr / loop 终止。"
    )


def generate_serial_scenarios(
    seed: int,
    replica_count: int,
    scenario_count: int,
    include: List[str],
) -> dict:
    """确定性地生成副本序列、目标、无序增量样本与场景触发点。

    *include* 为去重后的场景标签集合；*scenario_count* 不超过其大小（由路由
    层校验）。选中的场景按 :data:`SCENARIO_LABELS` 规范顺序取前
    *scenario_count* 个——因此 ``include`` 的给出顺序不影响结果。

    返回可直接由响应模型序列化的 dict：``scenarios``（标签，规范顺序）、
    ``replicas``（``(name, current)`` 列表）、``target``、``deltas``
    （乱序的 ``(from_serial, to_serial)`` 列表）与 ``triggers``
    （与 scenarios 同序的触发点说明）。
    """
    rng = DeterministicRandom(seed)
    selected_set = set(include)
    selected = tuple(label for label in SCENARIO_LABELS if label in selected_set)
    chosen = selected[:scenario_count]

    builder = _ScenarioBuilder(rng)
    for label in chosen:
        _BUILDERS[label](builder)
    builder.build_filler_chain()

    # 场景锚点绑定到副本槽：按规范顺序优先（ambiguous > fork > loop > wrap），
    # 副本不足时靠后的场景只保留增量证据。
    slot_of: Dict[str, int] = {}
    for slot, label in enumerate(chosen):
        if slot < replica_count:
            slot_of[label] = slot

    occupied = {builder.target}
    occupied.update(builder.anchors.values())
    currents: List[Optional[int]] = [None] * replica_count
    for label, slot in slot_of.items():
        currents[slot] = builder.anchors[label]
    for slot in range(replica_count):
        if currents[slot] is not None:
            continue
        for _ in range(_ScenarioBuilder.MAX_ATTEMPTS):
            value = rng.serial32()
            if value not in occupied:
                break
        else:  # pragma: no cover - 32 位空间下实际不会耗尽
            raise RuntimeError("副本序列号采样连续冲突")
        currents[slot] = value
        occupied.add(value)

    replicas = [(f"ns-{slot + 1}", currents[slot]) for slot in range(replica_count)]

    # 无序增量样本：整体乱序后输出（触发点通过具体序列号定位对应增量）
    shuffled_edges = list(builder.edges)
    rng.shuffle(shuffled_edges)

    triggers = []
    for label in chosen:
        edges = builder.structural[label]
        slot = slot_of.get(label)
        replica_name = replicas[slot][0] if slot is not None else None
        triggers.append(
            {
                "label": label,
                "title": SCENARIO_TITLES[label],
                "description": _trigger_description(
                    label, edges, replica_name, builder.target
                ),
                "deltas": [
                    {
                        "from_serial": frm,
                        "to_serial": to,
                        "forward_distance": forward_distance(frm, to),
                        "relation": compare_serial(frm, to),
                    }
                    for frm, to in edges
                ],
                "replica": replica_name,
            }
        )

    return {
        "seed": seed,
        "replica_count": replica_count,
        "scenario_count": scenario_count,
        "include": [label for label in SCENARIO_LABELS if label in selected_set],
        "scenarios": list(chosen),
        "replicas": [{"name": name, "current": current}
                     for name, current in replicas],
        "target": builder.target,
        "deltas": [{"from_serial": frm, "to_serial": to}
                   for frm, to in shuffled_edges],
        "triggers": triggers,
    }
