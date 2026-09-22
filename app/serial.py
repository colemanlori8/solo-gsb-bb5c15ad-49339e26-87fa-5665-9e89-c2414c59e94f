"""RFC 1982 序列号算术（32 位无符号整数环形空间）。

序列号空间为模 2^32 的环。给定当前值 ``current`` 与拟发布值 ``proposed``，
定义前向（顺时针）模距离 ``d = (proposed - current) mod 2^32``，则：

* ``d == 0``          -> ``equal``      （相等）
* ``1 <= d < 2^31``   -> ``newer``      （严格更新，可比较）
* ``d == 2^31``       -> ``ambiguous``  （正对点，RFC 1982 规定不可比较）
* ``2^31 < d < 2^32`` -> ``older``      （回退，可比较）

参考：RFC 1982 <https://www.rfc-editor.org/rfc/rfc1982>，SERIAL-BOUND = 2^31。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

BITS = 32
MOD = 1 << BITS          # 2^32，序列号空间大小
HALF = 1 << (BITS - 1)   # 2^31，SERIAL-BOUND
MAX_SERIAL = MOD - 1     # 2^32 - 1，合法序列号上界

NEWER = "newer"
EQUAL = "equal"
OLDER = "older"
AMBIGUOUS = "ambiguous"


def forward_distance(current: int, proposed: int) -> int:
    """返回 *proposed* 相对 *current* 的前向模距离，取值 [0, 2^32 - 1]。"""
    return (proposed - current) % MOD


def compare_serial(current: int, proposed: int) -> str:
    """按 RFC 1982 判定 *proposed* 相对 *current* 的关系。

    返回 ``"newer"`` / ``"equal"`` / ``"older"`` / ``"ambiguous"``。
    """
    distance = forward_distance(current, proposed)
    if distance == 0:
        return EQUAL
    if distance == HALF:
        return AMBIGUOUS
    if distance < HALF:
        return NEWER
    return OLDER


def is_strictly_newer(current: int, proposed: int) -> bool:
    """*proposed* 是否对 *current* 严格更新且可比较（1 <= 距离 <= 2^31 - 1）。"""
    return compare_serial(current, proposed) == NEWER


def propose_serial(currents: List[int]) -> Optional[Tuple[int, int]]:
    """推导对所有副本都严格更新且可比较的最佳拟发布值。

    对每个副本 ``c_i``，候选 ``x`` 必须满足
    ``1 <= (x - c_i) mod 2^32 <= 2^31 - 1``。

    优化目标：最小化最大前向距离 ``max_i (x - c_i) mod 2^32``；
    若多个候选同分，取数值最小者。

    可行域是每个副本所允许的开弧 ``{c+1, ..., c+2^31-1}``（模 2^32）
    之交，非空时必为一段连续弧，其最小元素必是某个 ``(c + 1) mod 2^32``；
    而沿弧每前进一步，目标函数恰好增加 1，故只需枚举这些下界候选。

    返回 ``(proposed, max_forward_distance)``；不存在可行候选时返回 ``None``。
    """
    unique_current = set(currents)
    candidates = [(c + 1) % MOD for c in unique_current]

    feasible: List[Tuple[int, int]] = []
    for candidate in candidates:
        distances = [forward_distance(c, candidate) for c in unique_current]
        if all(1 <= d <= HALF - 1 for d in distances):
            feasible.append((max(distances), candidate))

    if not feasible:
        return None

    # 先按最大前向距离排序，同分再按候选数值排序
    max_distance, proposed = min(feasible, key=lambda item: (item[0], item[1]))
    return proposed, max_distance


# ---------------------------------------------------------------------------
# IXFR 增量链（POST /transfer-plan）
# ---------------------------------------------------------------------------

UP_TO_DATE = "up_to_date"
IXFR = "ixfr"
AXFR = "axfr"

# axfr 的 reason
GAP = "gap"
"""增量链存在缺口（某序列号没有对应的下一条增量）。"""
LOOP = "loop"
"""沿增量链行进时回到了已经过的序列号，形成环路。"""
TARGET_INCOMPARABLE = "target_incomparable"
"""副本当前值与目标序列号在 RFC 1982 下不可比较（前向距离恰为 2^31）。"""
BUDGET = "budget"
"""完整增量集合下存在到目标的完整链，但受字节预算限制未被保留。"""

MAX_RETENTION_REPLICAS = 12
"""POST /retention-plan 与 /domain-retention-plan 允许的副本数上限
（选择算法枚举副本子集，复杂度 2^n）。"""


class DeltaValidationError(ValueError):
    """增量集合非法：存在非严格 newer 的增量、同一 from_serial 分叉，
    或同一增量携带冲突的字节数。"""

    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def _check_strictly_newer(deltas: List[Tuple[int, ...]]) -> None:
    """逐条拒绝 to_serial 非严格 newer 的增量（保留输入顺序，报错确定可复现）。

    每条增量取前两个元素为 ``(from_serial, to_serial)``，其余元素（如字节数）
    不参与判定。
    """
    for delta in deltas:
        from_serial, to_serial = delta[0], delta[1]
        relation = compare_serial(from_serial, to_serial)
        if relation != NEWER:
            raise DeltaValidationError(
                {
                    "error": "delta_not_strictly_newer",
                    "message": (
                        "增量的 to_serial 必须按 RFC 1982 严格新于 from_serial"
                        "（前向距离在 [1, 2^31-1]）"
                    ),
                    "from_serial": from_serial,
                    "to_serial": to_serial,
                    "relation": relation,
                }
            )


def build_delta_chain(
    deltas: List[Tuple[int, int]],
) -> dict:
    """校验并归一化一组无序 IXFR 增量，返回 ``from_serial -> to_serial`` 映射。

    处理顺序：

    1. 每条增量都必须满足 ``to_serial`` 按 RFC 1982 **严格新于**
       ``from_serial``（前向距离在 ``[1, 2^31-1]``），否则抛出
       ``DeltaValidationError``（error=delta_not_strictly_newer）；
    2. 完全相同的 ``(from_serial, to_serial)`` 重复增量去重；
    3. 同一 ``from_serial`` 指向不同 ``to_serial`` 即链分叉，抛出
       ``DeltaValidationError``（error=delta_fork）。

    校验后每个 from_serial 至多有一个后继，因此任何完整链都是唯一的。
    """
    # 第一步：逐条拒绝非严格 newer 的增量
    _check_strictly_newer(deltas)

    # 第二步 + 第三步：重复去重，分叉拒绝
    chain: dict = {}
    for from_serial, to_serial in deltas:
        if from_serial in chain:
            if chain[from_serial] == to_serial:
                continue  # 重复增量，去重
            raise _fork_error(from_serial, chain[from_serial], to_serial)
        chain[from_serial] = to_serial
    return chain


def _fork_error(from_serial: int, existing: int, new: int) -> DeltaValidationError:
    return DeltaValidationError(
        {
            "error": "delta_fork",
            "message": (
                f"同一 from_serial={from_serial} 指向不同的 to_serial，"
                "增量链不允许分叉"
            ),
            "from_serial": from_serial,
            "to_serials": [existing, new],
        }
    )


def build_sized_delta_chain(
    deltas: List[Tuple[int, int, int]],
) -> Tuple[dict, dict]:
    """校验并归一化一组带字节数的无序 IXFR 增量。

    返回 ``(chain, sizes)``：``chain`` 为 ``from_serial -> to_serial`` 映射，
    ``sizes`` 为 ``(from_serial, to_serial) -> bytes`` 映射。

    处理顺序（与 :func:`build_delta_chain` 一致，先验合法性再去重/查冲突）：

    1. 每条增量都必须严格 newer，否则抛出 ``DeltaValidationError``
       （error=delta_not_strictly_newer）；
    2. 完全相同的 ``(from_serial, to_serial, bytes)`` 重复增量去重；
    3. 按输入顺序逐条判定冲突：同一 ``(from_serial, to_serial)`` 携带不同
       bytes 即字节数冲突，抛出 ``DeltaValidationError``
       （error=delta_bytes_conflict）；同一 ``from_serial`` 指向不同
       ``to_serial`` 即链分叉，抛出 ``DeltaValidationError``
       （error=delta_fork）。
    """
    # 第一步：逐条拒绝非严格 newer 的增量
    _check_strictly_newer(deltas)

    # 第二步 + 第三步：重复去重，字节数冲突与分叉拒绝
    chain: dict = {}
    sizes: dict = {}
    for from_serial, to_serial, size in deltas:
        if from_serial in chain:
            if chain[from_serial] != to_serial:
                raise _fork_error(from_serial, chain[from_serial], to_serial)
            if sizes[(from_serial, to_serial)] != size:
                raise DeltaValidationError(
                    {
                        "error": "delta_bytes_conflict",
                        "message": (
                            f"增量 ({from_serial}, {to_serial}) 携带了"
                            "不同的字节数，相同增量的 bytes 必须一致"
                        ),
                        "from_serial": from_serial,
                        "to_serial": to_serial,
                        "bytes": [sizes[(from_serial, to_serial)], size],
                    }
                )
            continue  # 完全相同的重复增量，去重
        chain[from_serial] = to_serial
        sizes[(from_serial, to_serial)] = size
    return chain, sizes


def plan_transfer(
    current: int,
    target: int,
    chain: dict,
) -> Tuple[str, Optional[str], List[Tuple[int, int]]]:
    """为单个副本规划从 *current* 到 *target* 的传输方式。

    *chain* 为 :func:`build_delta_chain` 产出的 ``from_serial -> to_serial``
    映射，每条边都是 RFC 1982 严格推进。沿边逐跳追踪（**不做整数排序**，
    仅以增量自带的环形先后关系为准）：

    * ``current == target``：``(up_to_date, None, [])``；
    * 两端在 RFC 1982 下不可比较（前向距离恰为 2^31）：
      ``(axfr, target_incomparable, [])``，即使存在能连通的边也必须 AXFR；
    * 沿唯一增量链可抵达 *target*：``(ixfr, None, ordered_deltas)``；
    * 某一跳找不到后继：``(axfr, gap, [])``；
    * 行进中重复经过某序列号（环路）：``(axfr, loop, [])``。
    """
    if current == target:
        return UP_TO_DATE, None, []

    if compare_serial(current, target) == AMBIGUOUS:
        return AXFR, TARGET_INCOMPARABLE, []

    ordered: List[Tuple[int, int]] = []
    visited = {current}
    node = current
    while node != target:
        nxt = chain.get(node)
        if nxt is None:
            return AXFR, GAP, []
        if nxt in visited:
            return AXFR, LOOP, []
        ordered.append((node, nxt))
        visited.add(nxt)
        node = nxt
    return IXFR, None, ordered


# ---------------------------------------------------------------------------
# 增量保留规划（POST /retention-plan、POST /domain-retention-plan）
# ---------------------------------------------------------------------------


def _requirement_masks(
    target: int,
    replicas: List[Tuple[int, int]],
    chain: dict,
    edge_id: dict,
) -> Tuple[List[Optional[int]], List[Optional[str]]]:
    """在**完整**增量集合上为每个副本求需求集（增量位掩码）与固有原因。

    依次跑 :func:`plan_transfer`：

    * ``ixfr``：链上增量编号取位 OR，得到该副本的需求集掩码；
    * ``up_to_date``：需求集为 0（恒被覆盖，零成本），固有原因为 None；
    * ``axfr``（``gap`` / ``loop`` / ``target_incomparable``）：掩码为 None，
      保留其固有原因（任何保留子集都无法改变）。
    """
    req_masks: List[Optional[int]] = []
    inherent_reasons: List[Optional[str]] = []
    for current, _priority in replicas:
        mode, reason, ordered = plan_transfer(current, target, chain)
        if mode == IXFR:
            mask = 0
            for edge in ordered:
                mask |= 1 << edge_id[edge]
            req_masks.append(mask)
            inherent_reasons.append(None)
        elif mode == UP_TO_DATE:
            req_masks.append(0)
            inherent_reasons.append(None)
        else:
            req_masks.append(None)
            inherent_reasons.append(reason)
    return req_masks, inherent_reasons


def _mask_bytes(mask: int, edge_bytes: List[int]) -> int:
    """位掩码中选中增量的字节总数。"""
    used = 0
    bits = mask
    while bits:
        bit = bits & (-bits)
        used += edge_bytes[bit.bit_length() - 1]
        bits ^= bit
    return used


def _mask_sequence(
    mask: int, edges: List[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    """位掩码展开为按 ``(from_serial, to_serial)`` 升序的增量序列。"""
    sequence: List[Tuple[int, int]] = []
    bits = mask
    while bits:
        bit = bits & (-bits)
        sequence.append(edges[bit.bit_length() - 1])
        bits ^= bit
    return sequence


def plan_retention(
    target: int,
    replicas: List[Tuple[int, int]],
    chain: dict,
    sizes: dict,
    budget: int,
) -> Tuple[List[Tuple[int, int]], int, List[Tuple[str, Optional[str]]]]:
    """在字节预算内选择要保留的增量子集。

    *replicas* 为 ``(current, priority)`` 二元组（至多
    ``MAX_RETENTION_REPLICAS`` 个）；*chain* / *sizes* 为
    :func:`build_sized_delta_chain` 的产物；*budget* 为非负总字节预算。

    先用 :func:`plan_transfer` 在**完整**增量集合上判定每个副本：

    * ``up_to_date``：当前值已等于目标，无需任何增量，恒视为已覆盖；
    * ``ixfr``：存在唯一完整链，链上增量即该副本的**需求集**——需求集
      被保留子集包含时标记 ``ixfr``，否则 ``axfr / budget``；
    * ``axfr``（``gap`` / ``loop`` / ``target_incomparable``）：完整集合
      下即不可达，保留任何子集都无法改变，原因保持。

    在所有满足 ``字节总数 <= budget`` 的保留子集中，依次：

    1. 最大化被覆盖副本（拥有到目标完整链）的 priority 总和；
    2. 最大化被覆盖副本数；
    3. 最小化占用字节数；
    4. 取按 ``(from_serial, to_serial)`` 升序排列的增量序列中字典序最小者。

    保留集必为被覆盖副本需求集之并（多保留对覆盖无贡献的增量只会浪费
    预算），因此枚举全部副本子集（至多 2^12）即可求得精确最优；
    预算无法保留任何完整链时，最优即空集（仍正常返回）。

    返回 ``(retained, used_bytes, results)``：``retained`` 为按
    ``(from_serial, to_serial)`` 升序的增量列表；``results`` 与输入副本
    同序，每项为 ``(mode, reason)``。
    """
    # 增量按 (from_serial, to_serial) 升序编号：按位展开即为有序序列
    edges = sorted(sizes)
    edge_id = {edge: i for i, edge in enumerate(edges)}
    edge_bytes = [sizes[edge] for edge in edges]

    # 第一遍：完整集合上判定，求出每个副本的需求集（增量位掩码）
    # None 表示完整集合下即不可达（gap / loop / target_incomparable）
    req_masks, inherent_reasons = _requirement_masks(
        target, replicas, chain, edge_id
    )

    # up_to_date 副本恒被覆盖，作为常量计入目标函数
    base_prio = sum(
        prio for (_, prio), mask in zip(replicas, req_masks) if mask == 0
    )
    base_count = sum(1 for mask in req_masks if mask == 0)

    # 只需枚举"完整集合下为 ixfr"的副本子集（至多 12 个 -> 至多 4096 个子集）
    enum_idx = [i for i, mask in enumerate(req_masks) if mask]
    reqs = [req_masks[i] for i in enum_idx]
    prios = [replicas[i][1] for i in enum_idx]
    k = len(enum_idx)

    union_of = [0] * (1 << k)
    best_key: Optional[Tuple[int, int, int]] = None
    best_unions: List[int] = []
    for subset in range(1 << k):
        lsb = subset & (-subset)
        if lsb:
            union = union_of[subset ^ lsb] | reqs[lsb.bit_length() - 1]
        else:
            union = 0
        union_of[subset] = union

        used = _mask_bytes(union, edge_bytes)
        if used > budget:
            continue

        prio = base_prio
        count = base_count
        for req, p in zip(reqs, prios):
            if req & ~union == 0:  # 需求集被保留集包含即被覆盖
                prio += p
                count += 1

        key = (prio, count, -used)
        if best_key is None or key > best_key:
            best_key = key
            best_unions = [union]
        elif key == best_key:
            best_unions.append(union)

    # 最后一级：增量序列（按 (from_serial, to_serial) 升序）字典序最小
    best_union = min(best_unions, key=lambda union: _mask_sequence(union, edges))
    retained = _mask_sequence(best_union, edges)

    # 空子集恒可行（budget >= 0），best_unions 必非空
    used_bytes = sum(sizes[edge] for edge in retained)

    results: List[Tuple[str, Optional[str]]] = []
    for mask, reason in zip(req_masks, inherent_reasons):
        if mask is None:
            results.append((AXFR, reason))
        elif mask == 0:
            results.append((UP_TO_DATE, None))
        elif mask & ~best_union == 0:
            results.append((IXFR, None))
        else:
            results.append((AXFR, BUDGET))
    return retained, used_bytes, results


def plan_domain_retention(
    target: int,
    replicas: List[Tuple[int, int, str]],
    chain: dict,
    sizes: dict,
    budget: int,
) -> Tuple[
    List[Tuple[int, int]],
    int,
    List[Tuple[str, Optional[str]]],
    List[Tuple[str, int, int]],
]:
    """在字节预算内按 failure_domain 维度选择要保留的增量子集。

    *replicas* 为 ``(current, priority, failure_domain)`` 三元组（至多
    ``MAX_RETENTION_REPLICAS`` 个，``failure_domain`` 非空）；*chain* /
    *sizes* 为 :func:`build_sized_delta_chain` 的产物；*budget* 为非负总字节
    预算。每个副本在完整集合下的可达性判定与 :func:`plan_retention` 完全一致
    （复用 :func:`plan_transfer`，``gap`` / ``loop`` /
    ``target_incomparable`` 原因保持）。

    对任一保留子集，先按 failure_domain 汇总该域已覆盖（``ixfr`` 与
    ``up_to_date``）副本的 priority 总和，再把各域总和按**升序**排列成向量。
    在所有满足 ``字节总数 <= budget`` 的保留子集中，依次：

    1. 各域已覆盖 priority 总和的**升序向量字典序最大**（逐域 maximin）；
    2. 全体已覆盖副本的 **priority 总和最大**；
    3. 已覆盖**副本数最多**；
    4. **占用字节最少**；
    5. 取按 ``(from_serial, to_serial)`` 升序排列的增量序列中字典序最小者。

    向量含输入中出现的**全部** failure_domain（按名称升序排列）：该域无任何
    已覆盖副本时对应分量为 0，从而域的排列不依赖于哪个子集被选中。

    与 :func:`plan_retention` 相同，保留集必为被覆盖 ixfr 副本需求集之并，
    枚举全部 ixfr 副本子集（至多 2^12）即可精确求解；空子集恒可行。

    返回 ``(retained, used_bytes, results, domain_summaries)``：``retained``
    为按 ``(from_serial, to_serial)`` 升序的增量列表；``results`` 与输入副本
    同序，每项为 ``(mode, reason)``；``domain_summaries`` 为按 failure_domain
    名称升序的 ``(failure_domain, covered_priority, total_priority)`` 列表，
    其中 ``total_priority`` 为该域**全部**副本的 priority 之和（不随子集变化）。
    """
    # 增量按 (from_serial, to_serial) 升序编号：按位展开即为有序序列
    edges = sorted(sizes)
    edge_id = {edge: i for i, edge in enumerate(edges)}
    edge_bytes = [sizes[edge] for edge in edges]

    # failure_domain 按名称升序固定编号，向量分量顺序与子集无关
    domains = sorted({domain for _c, _p, domain in replicas})
    domain_id = {domain: i for i, domain in enumerate(domains)}
    replica_domain = [domain_id[domain] for _c, _p, domain in replicas]
    domain_total = [0] * len(domains)
    for (_c, priority, _domain), di in zip(replicas, replica_domain):
        domain_total[di] += priority

    # 完整集合上判定，求出每个副本的需求集（增量位掩码）与固有原因
    replica_base = [(current, priority) for current, priority, _d in replicas]
    req_masks, inherent_reasons = _requirement_masks(
        target, replica_base, chain, edge_id
    )

    # up_to_date 副本恒被覆盖（零成本），其 priority 作为常量并入域向量
    base_vector = [0] * len(domains)
    base_prio = 0
    base_count = 0
    for (mask, (_c, priority, _d), di) in zip(
        req_masks, replicas, replica_domain
    ):
        if mask == 0:
            base_vector[di] += priority
            base_prio += priority
            base_count += 1

    # 只需枚举"完整集合下为 ixfr"的副本子集（至多 12 个 -> 至多 4096 个子集）
    enum_idx = [i for i, mask in enumerate(req_masks) if mask]
    reqs = [req_masks[i] for i in enum_idx]
    prios = [replicas[i][1] for i in enum_idx]
    req_domains = [replica_domain[i] for i in enum_idx]
    k = len(enum_idx)

    union_of = [0] * (1 << k)
    # key = (域向量, 全体 priority, 覆盖数, -字节数)；域向量升序字典序最大化。
    # 域向量须为 tuple（list 之间不支持 < / >，无法参与字典序比较）。
    best_key: Optional[Tuple[Tuple[int, ...], int, int, int]] = None
    best_unions: List[int] = []
    for subset in range(1 << k):
        lsb = subset & (-subset)
        if lsb:
            union = union_of[subset ^ lsb] | reqs[lsb.bit_length() - 1]
        else:
            union = 0
        union_of[subset] = union

        used = _mask_bytes(union, edge_bytes)
        if used > budget:
            continue

        covered = [0] * len(domains)
        prio = base_prio
        count = base_count
        for req, p, di in zip(reqs, prios, req_domains):
            if req & ~union == 0:  # 需求集被保留集包含即被覆盖
                covered[di] += p
                prio += p
                count += 1

        vector = tuple(
            sorted(base_vector[i] + covered[i] for i in range(len(domains)))
        )
        key = (vector, prio, count, -used)
        if best_key is None or key > best_key:
            best_key = key
            best_unions = [union]
        elif key == best_key:
            best_unions.append(union)

    # 最后一级：增量序列（按 (from_serial, to_serial) 升序）字典序最小
    best_union = min(best_unions, key=lambda union: _mask_sequence(union, edges))
    retained = _mask_sequence(best_union, edges)

    # 空子集恒可行（budget >= 0），best_unions 必非空
    used_bytes = sum(sizes[edge] for edge in retained)

    results: List[Tuple[str, Optional[str]]] = []
    domain_covered = [0] * len(domains)
    for mask, reason, (_, priority, _d), di in zip(
        req_masks, inherent_reasons, replicas, replica_domain
    ):
        if mask is None:
            results.append((AXFR, reason))
        elif mask == 0:
            results.append((UP_TO_DATE, None))
            domain_covered[di] += priority
        elif mask & ~best_union == 0:
            results.append((IXFR, None))
            domain_covered[di] += priority
        else:
            results.append((AXFR, BUDGET))

    domain_summaries = [
        (domain, domain_covered[domain_id[domain]], total)
        for domain, total in zip(domains, domain_total)
    ]
    return retained, used_bytes, results, domain_summaries


# ---------------------------------------------------------------------------
# 上线门禁（POST /rollout-gate）
# ---------------------------------------------------------------------------

MISSING_TIME_ORDER = "missing_time_order"
"""相邻观测至少一条缺失时间戳，无法建立时间顺序（不判定序列号关系）。"""

BASELINE_NOT_REACHED = "baseline_not_reached"
"""副本的观测序列中没有任何一条达到基线序列号。"""

QUORUM_EXCEEDS_REPLICAS = "quorum_exceeds_replicas"
"""所需 quorum 超过副本总数，任何时刻都不可能满足。"""

QUORUM_NEVER_REACHED = "quorum_never_reached"
"""峰值同时处于有效推进区间内的副本数仍低于 quorum。"""


class ObservationOrderError(ValueError):
    """同一副本的观测时间戳重复或倒序。"""

    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def check_observation_time_order(
    name: str, timestamps: List[Optional[int]]
) -> None:
    """校验单个副本的观测时间戳严格递增（None 表示缺失，跳过不参与校验）。

    重复（与前一存在值相等）抛出 error=duplicate_observation_timestamp；
    倒序（小于前一存在值）抛出 error=observation_timestamp_out_of_order。
    两者均为 :class:`ObservationOrderError`，由路由层转为 422。
    """
    prev_index = -1
    prev: Optional[int] = None
    for index, ts in enumerate(timestamps):
        if ts is None:
            continue
        if prev is not None:
            if ts == prev:
                raise ObservationOrderError(
                    {
                        "error": "duplicate_observation_timestamp",
                        "message": (
                            f"副本 {name} 的观测时间戳重复：{ts}"
                            f"（下标 {prev_index} 与 {index}），时间戳必须严格递增"
                        ),
                        "replica": name,
                        "index": index,
                        "timestamp": ts,
                    }
                )
            if ts < prev:
                raise ObservationOrderError(
                    {
                        "error": "observation_timestamp_out_of_order",
                        "message": (
                            f"副本 {name} 的观测时间戳必须严格递增："
                            f"下标 {prev_index} 为 {prev}，下标 {index} 为 {ts}"
                        ),
                        "replica": name,
                        "index": index,
                        "timestamp": ts,
                        "previous_timestamp": prev,
                    }
                )
        prev = ts
        prev_index = index


def reaches_serial(baseline: int, serial: int) -> bool:
    """*serial* 是否达到 *baseline*（相等，或按 RFC 1982 严格更新）。

    即 ``(serial - baseline) mod 2^32 ∈ [0, 2^31 - 1]``；相对基线为
    ``older`` 或 ``ambiguous``（正对点）的序列号不算达到。
    """
    return compare_serial(baseline, serial) in (EQUAL, NEWER)


def evaluate_replica_rollout(
    baseline: int,
    observations: List[Tuple[Optional[int], int]],
) -> Tuple[
    bool,
    Optional[int],
    Optional[Tuple[int, int]],
    Optional[str],
    List[Tuple[int, str]],
]:
    """评估单个副本的观测序列相对基线的推进情况。

    *observations* 为按时间排序的 ``(timestamp, serial)`` 序列（timestamp 可
    缺失；存在的时间戳必须严格递增，由 :func:`check_observation_time_order`
    事先校验）。

    相邻观测对的关系标记：

    * 任一时间戳缺失：``missing_time_order``（无法建立时间顺序，不判定序列号
      关系）；
    * 否则按 RFC 1982 判定：``newer``（严格推进，允许跨 2^32 回绕）/
      ``equal`` / ``older`` / ``ambiguous``。

    首次达到基线 = 首个满足 :func:`reaches_serial` 的观测。**有效推进区间**
    从该观测起沿相邻对逐步扩展，每一步都必须同时满足：

    1. 时间戳齐全（非 ``missing_time_order``）；
    2. 关系为 ``newer`` 或 ``equal``（``older`` / ``ambiguous`` 为回退或
       不可比较，终止区间）；
    3. 新观测仍达到基线——一次 ``newer`` 推进可能跨越大半环后重新跌破基线
       （如基线 10：``10 -> 10+2^31-1 -> 8``，第二步仍是 newer 但 8 未达到
       基线），故每一步都重新校验。

    区间为闭区间 ``[首次达到的时间戳, 最后一个有效观测的时间戳]``；
    区间之外副本状态未知，不计入事件扫掠。

    返回 ``(reaches, first_reached_at, interval, reason, transitions)``：

    * ``reaches``：是否有任一观测达到基线；
    * ``first_reached_at``：首次达到基线的观测时间戳（该观测无时间戳时为
      None）；
    * ``interval``：``(start, end)`` 闭区间，无区间时为 None；
    * ``reason``：无区间时的原因——``baseline_not_reached``（从未达到）或
      ``missing_time_order``（达到了但首次达到的观测无时间戳，无法定位区间）；
    * ``transitions``：``(后一条观测下标, 关系标记)`` 列表，按输入顺序。
    """
    transitions: List[Tuple[int, str]] = []
    for i in range(1, len(observations)):
        t_prev, _s_prev = observations[i - 1]
        t_curr, _s_curr = observations[i]
        if t_prev is None or t_curr is None:
            relation = MISSING_TIME_ORDER
        else:
            relation = compare_serial(observations[i - 1][1], observations[i][1])
        transitions.append((i, relation))

    first: Optional[int] = None
    for i, (_ts, serial) in enumerate(observations):
        if reaches_serial(baseline, serial):
            first = i
            break
    if first is None:
        return False, None, None, BASELINE_NOT_REACHED, transitions

    first_reached_at = observations[first][0]
    if first_reached_at is None:
        return True, None, None, MISSING_TIME_ORDER, transitions

    end = first
    for i in range(first, len(observations) - 1):
        relation = transitions[i][1]  # transitions[i] 对应相邻对 (i, i+1)
        if relation not in (NEWER, EQUAL):
            break
        if not reaches_serial(baseline, observations[i + 1][1]):
            break
        end = i + 1
    interval = (first_reached_at, observations[end][0])
    return True, first_reached_at, interval, None, transitions


def sweep_quorum(
    intervals: List[Tuple[int, int]], quorum: int
) -> Tuple[Optional[int], Optional[int], int]:
    """事件扫掠：计算 quorum 首次满足与首次满足后再次跌破的时间。

    *intervals* 为各副本的有效推进**闭区间** ``[start, end]``（端点处副本仍
    计入）。并发数只在区间端点处变化，因此只在事件时间点上评估。

    返回 ``(first_satisfied_at, dropped_below_at, peak)``：

    * ``first_satisfied_at``：并发数首次 >= quorum 的事件时间戳；从未满足为
      None；
    * ``dropped_below_at``：首次满足之后并发数首次 < quorum 的事件时间戳；
      满足后直到观测结束未再跌破为 None；
    * ``peak``：所有事件时间点上的峰值并发数（无区间为 0）。
    """
    starts: dict = {}
    ends: dict = {}
    for start, end in intervals:
        starts[start] = starts.get(start, 0) + 1
        ends[end] = ends.get(end, 0) + 1

    first_satisfied_at: Optional[int] = None
    dropped_below_at: Optional[int] = None
    peak = 0
    active = 0
    for t in sorted(set(starts) | set(ends)):
        active += starts.get(t, 0)
        # 闭区间：end == t 的副本在 t 时刻仍计入，先计数再移除
        if active > peak:
            peak = active
        if first_satisfied_at is None:
            if active >= quorum:
                first_satisfied_at = t
        elif dropped_below_at is None and active < quorum:
            dropped_below_at = t
        active -= ends.get(t, 0)
    return first_satisfied_at, dropped_below_at, peak


def evaluate_rollout_gate(
    baseline: int,
    quorum: int,
    replica_observations: List[List[Tuple[Optional[int], int]]],
) -> Tuple[list, bool, Optional[int], Optional[int], int, Optional[str]]:
    """对全部副本做推进评估，并用事件扫掠判定 quorum 时间窗。

    *replica_observations* 与输入副本同序，每项为该副本按时间排序的
    ``(timestamp, serial)`` 观测序列（同 :func:`evaluate_replica_rollout`）。

    返回 ``(results, satisfied, first_satisfied_at, dropped_below_at, peak,
    reason)``：``results`` 与输入副本同序，每项为
    :func:`evaluate_replica_rollout` 的返回元组；``reason`` 在 quorum 从未
    满足时给出明确原因——``quorum_exceeds_replicas``（quorum 超过副本总数）
    或 ``quorum_never_reached``（峰值并发不足），满足时为 None。
    """
    results = []
    intervals: List[Tuple[int, int]] = []
    for observations in replica_observations:
        outcome = evaluate_replica_rollout(baseline, observations)
        results.append(outcome)
        interval = outcome[2]
        if interval is not None:
            intervals.append(interval)

    first_satisfied_at, dropped_below_at, peak = sweep_quorum(intervals, quorum)
    satisfied = first_satisfied_at is not None
    if satisfied:
        reason = None
    elif quorum > len(replica_observations):
        reason = QUORUM_EXCEEDS_REPLICAS
    else:
        reason = QUORUM_NEVER_REACHED
    return results, satisfied, first_satisfied_at, dropped_below_at, peak, reason


# ---------------------------------------------------------------------------
# 传播 SLO（POST /propagation-slo）
# ---------------------------------------------------------------------------

REACH = "reach"
"""窗口内观测达到发布版本（equal 或 newer）：副本进入“已到达”状态。"""

REGRESS = "regress"
"""窗口内观测相对发布版本为 older 或 ambiguous：已到达副本的到达状态失效。"""

DUPLICATE_RELEASE_TIMESTAMP = "duplicate_release_timestamp"
"""相邻两次发布的 released_at 相同（发布时间必须严格递增）。"""

RELEASE_TIMESTAMP_OUT_OF_ORDER = "release_timestamp_out_of_order"
"""发布的 released_at 小于前一次发布（发布时间必须严格递增）。"""


class ReleaseOrderError(ValueError):
    """发布事件的 released_at 重复或倒序。"""

    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def check_release_time_order(released_ats: List[int]) -> None:
    """校验发布事件时间严格递增。

    重复（与前一次发布相等）抛出 error=duplicate_release_timestamp；
    倒序（小于前一次发布）抛出 error=release_timestamp_out_of_order。
    两者均为 :class:`ReleaseOrderError`，由路由层转为 422。
    """
    prev_index = -1
    prev: Optional[int] = None
    for index, ts in enumerate(released_ats):
        if prev is not None:
            if ts == prev:
                raise ReleaseOrderError(
                    {
                        "error": DUPLICATE_RELEASE_TIMESTAMP,
                        "message": (
                            f"发布事件时间戳重复：{ts}"
                            f"（下标 {prev_index} 与 {index}），发布时间必须严格递增"
                        ),
                        "index": index,
                        "timestamp": ts,
                    }
                )
            if ts < prev:
                raise ReleaseOrderError(
                    {
                        "error": RELEASE_TIMESTAMP_OUT_OF_ORDER,
                        "message": (
                            f"发布事件时间戳必须严格递增："
                            f"下标 {prev_index} 为 {prev}，下标 {index} 为 {ts}"
                        ),
                        "index": index,
                        "timestamp": ts,
                        "previous_timestamp": prev,
                    }
                )
        prev = ts
        prev_index = index


def evaluate_release_window(
    serial: int,
    released_at: int,
    window_end: Optional[int],
    domains: List[str],
    replica_observations: List[Tuple[str, str, List[Tuple[int, int]]]],
) -> Tuple[Optional[int], List[Tuple[str, Optional[int]]], List[str]]:
    """评估单次发布在其窗口内的逐故障域到达与收敛。

    *serial* / *released_at* 为本次发布的版本与发布时间；*window_end* 为下一次
    发布的发布时间（窗口为左闭右开 ``[released_at, window_end)``），最后一次
    发布传 None（窗口延伸到观测结束）。*domains* 为按名称升序的全部故障域；
    *replica_observations* 为 ``(副本名, failure_domain, [(timestamp, serial)])``
    列表（观测时间已校验严格递增，副本名已校验不重复）。

    窗口内每条观测按 RFC 1982 相对本次发布版本分类为事件：

    * ``equal`` / ``newer``：**到达**事件——副本进入已到达状态；若其故障域
      当前未到达，该域以本事件时间为 reached_at（首个有效到达）；
    * ``older`` / ``ambiguous``：**回退**事件——若该副本当前处于已到达状态，
      其到达状态失效，且**所在故障域的本窗口到达状态一并失效**（reached_at
      清空），须等待后续到达事件重新到达；未到达副本的回退无影响。

    同一时间戳的事件先处理回退再处理到达：回退只使**早于**该时刻的到达失效，
    同时刻的到达仍然有效。首次全部故障域都处于已到达状态的时间戳即
    converged_at（此时必等于各域 reached_at 的最大值），扫描随即停止，
    之后的回退不再影响结果。

    返回 ``(converged_at, domain_status, missing_domains)``：``domain_status``
    与 *domains* 同序（按域名升序），每项为 ``(failure_domain, reached_at)``
    （窗口结束时仍未到达的域 reached_at 为 None）；``missing_domains`` 为
    窗口结束时仍未到达的故障域（升序），收敛时为空。
    """
    # 收集窗口内事件：(时间戳, 回退=0/到达=1, 事件类型, 副本名, 故障域)
    events: List[Tuple[int, int, str, str, str]] = []
    for name, domain, observations in replica_observations:
        for ts, obs_serial in observations:
            if ts < released_at:
                continue
            if window_end is not None and ts >= window_end:
                continue
            relation = compare_serial(serial, obs_serial)
            kind = REACH if relation in (EQUAL, NEWER) else REGRESS
            events.append((ts, 0 if kind == REGRESS else 1, kind, name, domain))
    # 同一时间戳先处理回退（0）再处理到达（1）；再按副本名保证完全确定
    events.sort(key=lambda event: (event[0], event[1], event[3]))

    replica_arrived = {name: False for name, _domain, _obs in replica_observations}
    reached_at: dict = {domain: None for domain in domains}
    arrived_domains = 0
    converged_at: Optional[int] = None
    index = 0
    while index < len(events):
        ts = events[index][0]
        # 处理该时间戳的全部事件后再判定收敛（同时刻事件视为同时发生）
        while index < len(events) and events[index][0] == ts:
            _t, _order, kind, name, domain = events[index]
            if kind == REACH:
                replica_arrived[name] = True
                if reached_at[domain] is None:
                    reached_at[domain] = ts
                    arrived_domains += 1
            else:  # REGRESS：仅“已到达副本”的回退使所在域到达状态失效
                if replica_arrived[name]:
                    replica_arrived[name] = False
                    if reached_at[domain] is not None:
                        reached_at[domain] = None
                        arrived_domains -= 1
            index += 1
        if arrived_domains == len(domains):
            converged_at = ts
            break

    domain_status = [(domain, reached_at[domain]) for domain in domains]
    missing = [domain for domain in domains if reached_at[domain] is None]
    return converged_at, domain_status, missing


def evaluate_propagation_slo(
    releases: List[Tuple[int, int]],
    replicas: List[Tuple[str, str, List[Tuple[int, int]]]],
    deadline_seconds: int,
) -> List[Tuple[int, int, Optional[int], Optional[int], bool, list, List[str]]]:
    """对每次发布评估其到下一次发布前窗口内的传播收敛。

    *releases* 为 ``(serial, released_at)`` 列表，released_at 必须严格递增
    （由 :func:`check_release_time_order` 事先校验）；*replicas* 为
    ``(副本名, failure_domain, [(timestamp, serial)])`` 列表；
    *deadline_seconds* 为非负整数。

    每次发布只检查窗口 ``[released_at[i], released_at[i+1])``（最后一次发布
    延伸到观测结束），各窗口的到达状态相互独立、互不带入。

    返回与 *releases* 同序的结果列表，每项为
    ``(serial, released_at, converged_at, latency_seconds, met_deadline,
    domain_status, missing_domains)``：

    * 收敛时 ``latency_seconds = converged_at - released_at``，
      ``met_deadline = latency_seconds <= deadline_seconds``；
    * 窗口结束仍未覆盖全部域时 ``converged_at`` 与 ``latency_seconds`` 为
      None，``met_deadline`` 为 False，``missing_domains`` 给出未到达的域。
    """
    domains = sorted({domain for _name, domain, _obs in replicas})
    results = []
    for idx, (serial, released_at) in enumerate(releases):
        window_end = releases[idx + 1][1] if idx + 1 < len(releases) else None
        converged_at, domain_status, missing = evaluate_release_window(
            serial, released_at, window_end, domains, replicas
        )
        if converged_at is None:
            latency_seconds = None
            met_deadline = False
        else:
            latency_seconds = converged_at - released_at
            met_deadline = latency_seconds <= deadline_seconds
        results.append(
            (
                serial,
                released_at,
                converged_at,
                latency_seconds,
                met_deadline,
                domain_status,
                missing,
            )
        )
    return results


# ---------------------------------------------------------------------------
# 分裂窗口（POST /divergence-windows）
# ---------------------------------------------------------------------------

OBSERVATION_SERIAL_CONFLICT = "observation_serial_conflict"
"""同一副本在同一时间戳出现不同序列号（同刻同值的重复观测允许并去重）。"""


def check_divergence_observation_order(
    name: str, observations: List[Tuple[int, int]]
) -> None:
    """校验单个副本的观测序列适用于分裂窗口扫掠。

    *observations* 为 ``(timestamp, serial)`` 列表（timestamp 必填非负，由 schema
    层保证）。与 :func:`check_observation_time_order` 不同，这里**允许同一时间戳
    出现多次**（同一时刻的观测原子合并），但要求：

    * 时间戳**非递减**：后一条早于前一条（倒序）抛出
      error=observation_timestamp_out_of_order；
    * 同一时间戳的多条观测序列号必须相同，否则抛出
      error=observation_serial_conflict（同一副本同刻不可能持有两个序列号）；
    * 完全相同的 ``(timestamp, serial)`` 重复观测合法，后续按去重处理。

    非递减保证相同时间戳的观测必相邻，故只需逐条与前一条比较。
    两者均抛出 :class:`ObservationOrderError`，由路由层转为 422。
    """
    for index in range(1, len(observations)):
        ts, serial = observations[index]
        prev_ts, prev_serial = observations[index - 1]
        if ts < prev_ts:
            raise ObservationOrderError(
                {
                    "error": "observation_timestamp_out_of_order",
                    "message": (
                        f"副本 {name} 的观测时间戳必须非递减："
                        f"下标 {index - 1} 为 {prev_ts}，下标 {index} 为 {ts}"
                    ),
                    "replica": name,
                    "index": index,
                    "timestamp": ts,
                    "previous_timestamp": prev_ts,
                }
            )
        if ts == prev_ts and serial != prev_serial:
            raise ObservationOrderError(
                {
                    "error": OBSERVATION_SERIAL_CONFLICT,
                    "message": (
                        f"副本 {name} 在时间戳 {ts} 出现了不同的序列号："
                        f"{prev_serial} 与 {serial}，同一时刻只能持有一个序列号"
                    ),
                    "replica": name,
                    "index": index,
                    "timestamp": ts,
                    "serials": [prev_serial, serial],
                }
            )


def has_common_successor(currents: List[int]) -> bool:
    """是否存在一个候选序列号能对所有当前值**同时严格推进**（RFC 1982 newer）。

    复用 :func:`propose_serial`：可行域是每个副本的开弧
    ``{c+1, ..., c+2^31-1}``（模 2^32）之交，交集为空（如存在相差正好 2^31
    的正对点，或各值跨越半环）时返回 False。**不使用整数极差**：例如
    ``4294967290`` 与 ``10`` 的整数极差接近 2^32，但前向距离仅 16，
    存在共同后继；而 ``0`` 与 ``2^31-1`` 的整数极差不足 2^31，交集却已为空。
    """
    return propose_serial(currents) is not None


# ---------------------------------------------------------------------------
# IXFR 增量共识（POST /delta-consensus）
# ---------------------------------------------------------------------------

CONSENSUS_REACHED = "reached"
"""唯一连续可信链恰好抵达目标。"""

CONSENSUS_GAP = "gap"
"""最长可信链在某序列号处没有唯一后继，且未抵达目标。"""

CONSENSUS_FORK = "fork"
"""可信集合在某序列号处分叉，不静默选边。"""

CONSENSUS_LOOP = "loop"
"""可信集合形成环路，沿边行进回到已访问序列号。"""


class MirrorReportError(ValueError):
    """单个镜像上报内容非法：非严格 newer 的边，或对同一起点上报冲突版本。"""

    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def validate_mirror_reports(
    reports: List[Tuple[str, List[Tuple[int, int, str]]]],
) -> None:
    """对各镜像上报的 IXFR 清单做整体校验（422）。

    分两遍，先全局验合法性再查冲突，保证相同输入的报错确定可复现：

    1. 每条上报边的 ``to_serial`` 必须按 RFC 1989 严格新于 ``from_serial``
       （前向距离在 ``[1, 2^31-1]``），按镜像输入顺序逐条检查，违规抛出
       :class:`MirrorReportError`（error=delta_not_strictly_newer）；
    2. 每个镜像内部：完全相同的三元组重复上报去重；同一 ``from_serial``
       上报不同的完整版本（不同 ``to_serial`` 或不同内容摘要）即该镜像对
       同一起点给出冲突版本，抛出 :class:`MirrorReportError`
       （error=mirror_version_conflict），并给出冲突的两份完整上报。
    """
    # 第一遍：逐条拒绝非严格 newer 的边（保留输入顺序，报错确定可复现）
    for mirror, deltas in reports:
        for from_serial, to_serial, _digest in deltas:
            relation = compare_serial(from_serial, to_serial)
            if relation != NEWER:
                raise MirrorReportError(
                    {
                        "error": "delta_not_strictly_newer",
                        "message": (
                            f"镜像 {mirror} 上报的增量 to_serial 必须按 RFC 1982 "
                            "严格新于 from_serial（前向距离在 [1, 2^31-1]）"
                        ),
                        "mirror": mirror,
                        "from_serial": from_serial,
                        "to_serial": to_serial,
                        "relation": relation,
                    }
                )

    # 第二遍：逐镜像检查同一起点的版本冲突（完全相同的三元组去重）
    for mirror, deltas in reports:
        versions: dict = {}
        for from_serial, to_serial, digest in deltas:
            if from_serial in versions:
                existing = versions[from_serial]
                if existing == (to_serial, digest):
                    continue  # 完全相同的重复上报，去重
                raise MirrorReportError(
                    {
                        "error": "mirror_version_conflict",
                        "message": (
                            f"镜像 {mirror} 对同一 from_serial={from_serial} "
                            "上报了冲突版本（to_serial 或内容摘要不一致）"
                        ),
                        "mirror": mirror,
                        "from_serial": from_serial,
                        "versions": [
                            {
                                "from_serial": from_serial,
                                "to_serial": existing[0],
                                "digest": existing[1],
                            },
                            {
                                "from_serial": from_serial,
                                "to_serial": to_serial,
                                "digest": digest,
                            },
                        ],
                    }
                )
            versions[from_serial] = (to_serial, digest)


def _trusted_cycles(
    out_edges: dict,
) -> List[List[Tuple[int, int, str]]]:
    """枚举可信图中的全部简单环路（每个 from_serial 至多一个可信后继）。

    每个有唯一可信出边（唯一三元组）的节点至多属于一个环，故只需沿其后继
    指针行进：到达无出边 / 分叉（多个可信三元组，含同序号对多摘要）节点即
    停止，遇到已访问节点则定位出该环。环路规范化为从 **最小 from_serial**
    起、按行进方向排列的边序列，最后按边序列
    （``(from_serial, to_serial, digest)``）升序去重输出。
    """
    cycles: dict = {}
    for start in out_edges:
        path: List[int] = [start]
        index_of = {start: 0}
        node = start
        while True:
            outs = out_edges.get(node)
            if not outs or len(outs) != 1:
                break  # 缺口或分叉：该行进路径无环
            t, digest = outs[0]
            if t in index_of:
                cycle_nodes = path[index_of[t]:]
                min_pos = min(range(len(cycle_nodes)), key=lambda i: cycle_nodes[i])
                ordered_nodes = cycle_nodes[min_pos:] + cycle_nodes[:min_pos]
                edges = [
                    (frm, out_edges[frm][0][0], out_edges[frm][0][1])
                    for frm in ordered_nodes
                ]
                cycles[ordered_nodes[0]] = edges
                break
            index_of[t] = len(path)
            path.append(t)
            node = t
    return [cycles[key] for key in sorted(cycles)]


def evaluate_delta_consensus(
    target: int,
    quorum: int,
    reports: List[Tuple[str, List[Tuple[int, int, str]]]],
) -> dict:
    """对多镜像 IXFR 上报做三元组计票，并沿可信边组装到目标的唯一连续链。

    前置：*reports* 已通过 :func:`validate_mirror_reports` 校验（边严格推进、
    单镜像无同起点冲突版本、镜像名不重复）。

    1. **计票**：按完整三元组 ``(from_serial, to_serial, digest)`` 统计上报
       镜像数（同一镜像的完全重复只计一票）；票数 >= *quorum* 的三元组进入
       **可信集合**。``digest`` 不同的同序号对视为不同版本，各自计票。
    2. **组装**：可信集合以每个 from_serial 的后继集合论，只有唯一后继的
       节点才能续链；从每个无入边的根节点沿唯一后继行进，遇缺口 / 分叉 /
       回环即停。取其中最长的一条为最长可信链（长度并列时按
       ``(from_serial, to_serial, digest)`` 边序列字典序最小、再按根节点
       数值最小破平），**不静默选边**；可信集合中的全部分叉与环路另列。
    3. **可达性**：``target_reachable`` 当且仅当目标出现在某条唯一路径
       （根出发、沿途每个节点都只有唯一可信出边）所**确定到达**的节点集合
       中——分叉 / 回环发生时，分叉点 / 回环点本身已经到达，但两个分支之后、
       环的第二次循环都不计入。纯环（无根可入）中的节点不可达；组装链长不
       影响该判定（目标位于最长链中间同样可达）。
    4. **逐镜像证据**：以请求中的镜像顺序给出缺失可信边（该镜像未上报）、
       少数摘要（同序号对摘要未达法定人数，含票数）与额外边（任何不构成
       可信三元组的上报，含票数），各列表内部按确定顺序排列。

    返回可直接序列化为响应模型的 dict。
    """
    names = [name for name, _deltas in reports]

    # 三元组 -> 上报镜像名集合（同一镜像的完全重复只计一票）
    voters: dict = {}
    # 镜像 -> 该镜像上报过的去重三元组集合
    mirror_edges: List[set] = []
    for mirror, deltas in reports:
        unique = set(deltas)
        mirror_edges.append(unique)
        for edge in unique:
            voters.setdefault(edge, set()).add(mirror)

    trusted_edges = [
        edge for edge in sorted(voters) if len(voters[edge]) >= quorum
    ]
    trusted_set = set(trusted_edges)
    trusted_pairs = {(f, t) for f, t, _d in trusted_edges}

    # 可信图：from_serial -> 升序的可信出边 [(to_serial, digest)...]。
    # 同一序号对 (from,to) 有两个达到法定人数的摘要时同样产生多个出边，
    # 视为分叉（内容无法静默二选一）。
    out_edges: dict = {}
    trusted_digests: dict = {}
    incoming: set = set()
    for f, t, digest in trusted_edges:
        out_edges.setdefault(f, []).append((t, digest))
        trusted_digests.setdefault((f, t), []).append(digest)
        incoming.add(t)
    for f in out_edges:
        out_edges[f].sort()
    for pair in trusted_digests:
        trusted_digests[pair].sort()

    roots = sorted(f for f in out_edges if f not in incoming)

    # 从每个根沿唯一可信出边行进，返回 (边序列, 确定到达的节点集合,
    # 停止原因, 停止时所在节点)。分叉 / 回环点本身已确定到达（在 visited 中），
    # 但其之后不再有唯一路径。
    def walk(root: int) -> Tuple[List[Tuple[int, int, str]], set, str, int]:
        edges: List[Tuple[int, int, str]] = []
        visited = {root}
        node = root
        while True:
            outs = out_edges.get(node)
            if not outs:
                return edges, visited, CONSENSUS_GAP, node
            if len(outs) > 1:
                return edges, visited, CONSENSUS_FORK, node
            nxt, digest = outs[0]
            if nxt in visited:
                return edges, visited, CONSENSUS_LOOP, node
            edges.append((node, nxt, digest))
            visited.add(nxt)
            node = nxt

    walks = [walk(root) for root in roots]

    # 目标可达性：任一唯一路径确定到达的节点集合（含根 / 分叉点 / 回环点）
    reachable_nodes: set = set()
    for _edges, visited, _reason, _node in walks:
        reachable_nodes |= visited
    target_reachable = target in reachable_nodes

    best: Optional[Tuple[List[Tuple[int, int, str]], str, int]] = None
    for root, (edges, _visited, stop_reason, _stop_node) in zip(roots, walks):
        if not edges:
            # 根节点本身无法产生任何边（缺口、分叉或自环），不作为链候选
            continue
        candidate = (edges, stop_reason, root)
        if best is None or (-len(edges), edges, root) < (
            -len(best[0]),
            best[0],
            best[2],
        ):
            best = candidate

    if best is not None:
        chain_edges, stop_reason, _root = best
        chain_end = chain_edges[-1][1]
        # 终点恰为目标即到达（即使停止原因是其后缺口；目标之后有唯一可信
        # 延续也不影响它已被唯一连续链抵达）
        chain_reason = CONSENSUS_REACHED if chain_end == target else stop_reason
    else:
        chain_edges = []
        chain_end = None
        if any(len(outs) > 1 for outs in out_edges.values()):
            chain_reason = CONSENSUS_FORK
        elif _trusted_cycles(out_edges):
            chain_reason = CONSENSUS_LOOP
        else:
            chain_reason = CONSENSUS_GAP

    forks = []
    for frm in sorted(f for f, outs in out_edges.items() if len(outs) > 1):
        # 按 to_serial 分组列出其全部达到法定人数的摘要版本
        branches = [
            {
                "to_serial": to,
                "digests": [
                    {"digest": digest, "votes": len(voters[(frm, to, digest)])}
                    for digest in trusted_digests[(frm, to)]
                ],
            }
            for to in sorted({to for to, _d in out_edges[frm]})
        ]
        forks.append({"from_serial": frm, "branches": branches})

    loops = [
        [
            {
                "from_serial": f,
                "to_serial": t,
                "digest": digest,
                "votes": len(voters[(f, t, digest)]),
            }
            for f, t, digest in cycle
        ]
        for cycle in _trusted_cycles(out_edges)
    ]

    # 逐镜像证据（镜像顺序与请求一致；各列表内部稳定排序）
    mirror_evidence = []
    for name, reported in zip(names, mirror_edges):
        reported_pairs = {(f, t) for f, t, _d in reported}
        missing = [
            {
                "from_serial": f,
                "to_serial": t,
                "digests": [
                    {"digest": digest, "votes": len(voters[(f, t, digest)])}
                    for digest in trusted_digests[(f, t)]
                ],
            }
            for f, t, _d in trusted_edges
            if (f, t) not in reported_pairs
        ]

        minority = []
        extras = []
        for f, t, digest in sorted(reported):
            if (f, t, digest) in trusted_set:
                continue
            if (f, t) in trusted_pairs:
                minority.append(
                    {
                        "from_serial": f,
                        "to_serial": t,
                        "digest": digest,
                        "votes": len(voters[(f, t, digest)]),
                        "trusted_digests": [
                            {"digest": td, "votes": len(voters[(f, t, td)])}
                            for td in trusted_digests[(f, t)]
                        ],
                    }
                )
            else:
                extras.append(
                    {
                        "from_serial": f,
                        "to_serial": t,
                        "digest": digest,
                        "votes": len(voters[(f, t, digest)]),
                    }
                )
        minority.sort(key=lambda e: (e["from_serial"], e["to_serial"], e["digest"]))
        extras.sort(key=lambda e: (e["from_serial"], e["to_serial"], e["digest"]))
        mirror_evidence.append(
            {
                "mirror": name,
                "reported_edges": len(reported),
                "missing": missing,
                "minority_digests": minority,
                "extra_edges": extras,
            }
        )

    return {
        "target": target,
        "quorum": quorum,
        "mirror_count": len(reports),
        "trusted_chain": [
            {
                "from_serial": f,
                "to_serial": t,
                "digest": digest,
                "votes": len(voters[(f, t, digest)]),
                "voters": sorted(voters[(f, t, digest)]),
            }
            for f, t, digest in chain_edges
        ],
        "chain_length": len(chain_edges),
        "chain_end": chain_end,
        "chain_reason": chain_reason,
        "target_reachable": target_reachable,
        "trusted_edges": [
            {
                "from_serial": f,
                "to_serial": t,
                "digest": digest,
                "votes": len(voters[(f, t, digest)]),
                "voters": sorted(voters[(f, t, digest)]),
            }
            for f, t, digest in trusted_edges
        ],
        "forks": forks,
        "loops": loops,
        "mirrors": mirror_evidence,
    }


def evaluate_divergence_windows(
    replica_observations: List[List[Tuple[int, int]]],
) -> List[Tuple[int, Optional[int], Optional[int], List[Tuple[int, int]]]]:
    """沿时间线扫掠各副本最新序列号，合并连续的无共同后继状态为分裂窗口。

    *replica_observations* 与输入副本同序，每项为该副本的
    ``(timestamp, serial)`` 观测列表（已通过
    :func:`check_divergence_observation_order` 校验：时间戳非递减、同刻同副本
    序列号一致；同刻同值的重复观测自然去重）。

    处理流程：

    1. 把全部副本的观测汇成事件按 ``(时间戳, 副本下标)`` 排序；
    2. 逐时间戳**先原子合并该时刻的全部更新**（同一时刻任意多个副本一起生效，
       不存在“先看到谁”的中间状态），再沿时间线携带各副本最近一次观测值
       （尚无观测的副本不参与判定）；
    3. 每个时间戳的合并后状态用 :func:`has_common_successor` 按 **RFC 1982
       环形规则**判定：是否存在一个候选值能对全部已知副本同时严格推进；
    4. **连续**无可行候选的状态合并为一个分裂窗口；中间任一状态恢复可行即闭窗，
       之后再次发散另开新窗。

    窗口的 ``entries`` 记录各副本**进入窗口时**持有的序列号：窗口在某时间戳
    开启时对全部已知副本快照；窗口开启后才首次出现的副本在其首个观测时间戳
    （该时刻合并后仍无可行候选）加入，取其首条观测序列号。结果按副本下标升序。

    返回窗口列表（按开始时间升序），每项为
    ``(start_at, end_at, duration_seconds, entries)``：

    * ``end_at`` 为连续无候选状态之后**首个恢复可行状态**的时间戳，
      ``duration_seconds = end_at - start_at``；
    * 观测结束时仍无可行候选（窗口未恢复）时 ``end_at`` 与
      ``duration_seconds`` 均为 None；
    * ``entries`` 为 ``(副本下标, 进入窗口时的序列号)`` 列表。
    """
    # 全局事件：(timestamp, replica_index, serial)
    events: List[Tuple[int, int, int]] = []
    for index, observations in enumerate(replica_observations):
        for ts, serial in observations:
            events.append((ts, index, serial))
    events.sort()

    latest: dict = {}
    windows: List[
        Tuple[int, Optional[int], Optional[int], List[Tuple[int, int]]]
    ] = []
    open_start: Optional[int] = None
    entry_serial: dict = {}

    i = 0
    while i < len(events):
        ts = events[i][0]
        # 原子合并同一时间戳的全部更新后再判定状态
        while i < len(events) and events[i][0] == ts:
            _, replica_index, serial = events[i]
            latest[replica_index] = serial
            i += 1

        if has_common_successor(list(latest.values())):
            if open_start is not None:
                entries = sorted(entry_serial.items())
                windows.append(
                    (open_start, ts, ts - open_start, entries)
                )
                open_start = None
                entry_serial = {}
        else:
            if open_start is None:
                open_start = ts
                entry_serial = dict(latest)  # 开启时对全部已知副本快照
            else:
                # 窗口开启后才首次出现的副本在此时刻加入（取其首条观测值）
                for replica_index, serial in latest.items():
                    if replica_index not in entry_serial:
                        entry_serial[replica_index] = serial

    if open_start is not None:
        # 观测结束仍未恢复：窗口保持开启
        windows.append((open_start, None, None, sorted(entry_serial.items())))
    return windows
