"""FastAPI 应用入口：DNS SOA 序列号推进审计 API。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .schemas import (
    AuditItem,
    AuditRequest,
    AuditResponse,
    ConsensusEdge,
    ConsensusFork,
    ConsensusForkBranch,
    ConsensusLoopEdge,
    ConsensusMirrorEvidence,
    ConsensusVoteDigest,
    DeltaConsensusRequest,
    DeltaConsensusResponse,
    DivergenceEntry,
    DivergenceWindow,
    DivergenceWindowsRequest,
    DivergenceWindowsResponse,
    DomainRetentionPlanItem,
    DomainRetentionPlanRequest,
    DomainRetentionPlanResponse,
    DomainSummary,
    ExtraConsensusEdge,
    MissingTrustedEdge,
    MinorityDigest,
    ObservationTransition,
    OrderedDelta,
    PropagationDomainStatus,
    PropagationReleaseItem,
    PropagationSloRequest,
    PropagationSloResponse,
    ProposeItem,
    ProposeRequest,
    ProposeResponse,
    RetainedDelta,
    RetentionPlanItem,
    RetentionPlanRequest,
    RetentionPlanResponse,
    RolloutGateRequest,
    RolloutGateResponse,
    RolloutInterval,
    RolloutReplicaItem,
    ScenarioDelta,
    ScenarioReplica,
    ScenarioTrigger,
    SerialScenarioGenerateRequest,
    SerialScenarioGenerateResponse,
    TransferPlanItem,
    TransferPlanRequest,
    TransferPlanResponse,
    TriggerDelta,
)
from .serial import (
    MAX_RETENTION_REPLICAS,
    build_delta_chain,
    build_sized_delta_chain,
    check_divergence_observation_order,
    check_observation_time_order,
    check_release_time_order,
    compare_serial,
    DeltaValidationError,
    evaluate_delta_consensus,
    evaluate_divergence_windows,
    evaluate_propagation_slo,
    evaluate_rollout_gate,
    forward_distance,
    MirrorReportError,
    ObservationOrderError,
    plan_domain_retention,
    plan_retention,
    plan_transfer,
    propose_serial,
    ReleaseOrderError,
    validate_mirror_reports,
)
from .scenarios import generate_serial_scenarios

app = FastAPI(
    title="DNS SOA 序列号推进审计 API",
    description=(
        "基于 RFC 1982 环形算术，对多个 DNS 副本的 SOA 序列号推进进行审计，"
        "推导对所有副本都严格更新的最佳拟发布值，规划增量 / 全量传输，"
        "在字节预算内选择要保留的增量集合（支持按故障域公平分配），"
        "并按时间序列观测对上线做 quorum 门禁判定、"
        "评估每次发布在各故障域间的传播收敛时延（SLO），"
        "沿时间线检测各副本序列号无可共同推进候选的分裂窗口，"
        "对多镜像上报的 IXFR 增量按完整三元组做法定人数共识审计，"
        "并确定性生成覆盖 RFC 1982 边界（回绕 / 正对点 / 分叉 / 环路）"
        "的可复现测试场景。"
    ),
    version="1.7.0",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _check_duplicate_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise HTTPException(
                status_code=422,
                detail={"error": "duplicate_replica_name", "name": name},
            )
        seen.add(name)


@app.post("/audit", response_model=AuditResponse)
def audit(request: AuditRequest) -> AuditResponse:
    """逐一判定拟发布值相对各副本当前序列号的关系。

    * newer：严格更新且可比较（1 <= 前向距离 < 2^31）
    * equal：相等（距离 0）
    * older：回退（2^31 < 距离 < 2^32）
    * ambiguous：差值正好 2^31（正对点，RFC 1982 规定不可比较）

    非 newer 的副本均为阻塞副本。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    results = []
    blocking_replicas = []
    for replica in request.replicas:
        relation = compare_serial(replica.current, request.proposed)
        distance = forward_distance(replica.current, request.proposed)
        blocking = relation != "newer"
        if blocking:
            blocking_replicas.append(replica.name)
        results.append(
            AuditItem(
                name=replica.name,
                current=replica.current,
                proposed=request.proposed,
                relation=relation,
                forward_distance=distance,
                blocking=blocking,
            )
        )

    return AuditResponse(
        proposed=request.proposed,
        results=results,
        blocked=bool(blocking_replicas),
        blocking_replicas=blocking_replicas,
    )


@app.post("/propose", response_model=ProposeResponse)
def propose(request: ProposeRequest) -> ProposeResponse:
    """推导对所有副本都严格更新且可比较的候选序列号。

    最小化最大前向距离；同分取数值最小者。
    若各副本当前序列号无法被任意单一值同时严格推进（例如存在相差 2^31
    的正对点，或序列跨越超过半环），返回 409。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    outcome = propose_serial([r.current for r in request.replicas])
    if outcome is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_valid_proposal",
                "message": (
                    "不存在能对所有副本都严格更新且可比较的候选值："
                    "副本当前序列号跨越半环（含相差 2^31 的正对点）"
                ),
            },
        )

    proposed, max_distance = outcome
    results = [
        ProposeItem(
            name=replica.name,
            current=replica.current,
            relation=compare_serial(replica.current, proposed),
            forward_distance=forward_distance(replica.current, proposed),
        )
        for replica in request.replicas
    ]

    return ProposeResponse(
        proposed=proposed,
        max_forward_distance=max_distance,
        results=results,
    )


@app.post("/transfer-plan", response_model=TransferPlanResponse)
def transfer_plan(request: TransferPlanRequest) -> TransferPlanResponse:
    """规划各副本到目标 SOA 序列号的增量 / 全量传输。

    先对无序增量集合做整体校验（422）：

    * 每条增量的 to_serial 必须按 RFC 1982 严格新于 from_serial；
    * 同一 from_serial 指向不同 to_serial 的分叉增量被拒绝；
    * 完全相同的重复增量去重。

    再为每个副本沿增量链逐跳追踪（遵循 RFC 1982 环形顺序，不做整数排序）：

    * 当前值已等于目标：``up_to_date``；
    * 存在唯一完整链抵达目标：``ixfr``，并返回按序排列的 ordered_deltas；
    * 存在缺口（gap）、环路（loop）或目标与当前值不可比较
      （target_incomparable，前向距离恰为 2^31）：``axfr`` 并给出 reason。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    raw_deltas = [(d.from_serial, d.to_serial) for d in request.deltas]
    try:
        chain = build_delta_chain(raw_deltas)
    except DeltaValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    results = []
    for replica in request.replicas:
        mode, reason, ordered = plan_transfer(
            replica.current, request.target, chain
        )
        results.append(
            TransferPlanItem(
                name=replica.name,
                current=replica.current,
                target=request.target,
                mode=mode,
                reason=reason,
                ordered_deltas=[
                    OrderedDelta(from_serial=frm, to_serial=to)
                    for frm, to in ordered
                ],
            )
        )

    return TransferPlanResponse(target=request.target, results=results)


@app.post("/retention-plan", response_model=RetentionPlanResponse)
def retention_plan(request: RetentionPlanRequest) -> RetentionPlanResponse:
    """在字节预算内选择要保留的增量集合（副本数不超过 12）。

    先对带字节数的无序增量集合做整体校验（422）：

    * 每条增量的 to_serial 必须按 RFC 1982 严格新于 from_serial；
    * 完全相同的重复增量去重；
    * 相同增量（同一 from_serial、to_serial）携带不同 bytes 即冲突，拒绝；
    * 同一 from_serial 指向不同 to_serial 的分叉增量被拒绝。

    再按环形链路规则为每个副本求出到目标的完整链（需求集），在
    ``used_bytes <= budget`` 的约束下选择保留集，依次：

    1. 最大化拥有到目标完整链的副本 priority 总和；
    2. 最大化被覆盖副本数；
    3. 最小化占用字节数；
    4. 取增量序列（按 from_serial、to_serial 升序）字典序最小者。

    逐副本标记：完整链被保留为 ``ixfr``；当前值已等于目标为
    ``up_to_date``；否则 ``axfr`` 并给出 ``gap`` / ``loop`` /
    ``target_incomparable`` / ``budget``（完整集合下有完整链但因预算
    未被保留）原因。预算无法保留任何完整链时仍返回 200（保留集为空）。
    """
    _check_duplicate_names([r.name for r in request.replicas])
    if len(request.replicas) > MAX_RETENTION_REPLICAS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "too_many_replicas",
                "message": f"副本数不得超过 {MAX_RETENTION_REPLICAS}",
                "limit": MAX_RETENTION_REPLICAS,
                "got": len(request.replicas),
            },
        )

    raw_deltas = [(d.from_serial, d.to_serial, d.bytes) for d in request.deltas]
    try:
        chain, sizes = build_sized_delta_chain(raw_deltas)
    except DeltaValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    retained, used_bytes, modes = plan_retention(
        request.target,
        [(r.current, r.priority) for r in request.replicas],
        chain,
        sizes,
        request.budget,
    )

    results = [
        RetentionPlanItem(
            name=replica.name,
            current=replica.current,
            target=request.target,
            mode=mode,
            reason=reason,
        )
        for replica, (mode, reason) in zip(request.replicas, modes)
    ]

    return RetentionPlanResponse(
        target=request.target,
        budget=request.budget,
        used_bytes=used_bytes,
        retained_deltas=[
            RetainedDelta(from_serial=frm, to_serial=to, bytes=sizes[(frm, to)])
            for frm, to in retained
        ],
        results=results,
    )


@app.post("/domain-retention-plan", response_model=DomainRetentionPlanResponse)
def domain_retention_plan(
    request: DomainRetentionPlanRequest,
) -> DomainRetentionPlanResponse:
    """在字节预算内按故障域（failure_domain）公平地选择要保留的增量集合。

    与 ``/retention-plan`` 复用同一套规则：增量整体校验（RFC 1982 严格 newer、
    去重、字节数冲突、分叉均为 422），副本数不超过 12（422），逐副本沿完整
    集合的增量链判定 ``ixfr`` / ``up_to_date`` / ``axfr``（``gap`` /
    ``loop`` / ``target_incomparable`` / ``budget``）。每个副本额外携带
    **非空** ``failure_domain``。

    对任一保留子集，先把各 failure_domain 已覆盖（``ixfr`` 与
    ``up_to_date``）副本的 priority 总和汇总后按**升序**排列成向量；在
    ``used_bytes <= budget`` 的可行子集中依次：

    1. 该升序向量**字典序最大**（优先抬高覆盖最差的域，即逐域 maximin）；
    2. 全体已覆盖副本的 **priority 总和最大**；
    3. 已覆盖**副本数最多**；
    4. **占用字节最少**；
    5. 取增量序列（按 from_serial、to_serial 升序）字典序最小者。

    响应除 ``retained_deltas`` 与逐副本模式（回显 ``failure_domain``）外，
    还按域给出 ``domain_summaries``：每个 failure_domain 的
    ``covered_priority``（已覆盖副本 priority 总和）与 ``total_priority``
    （该域全部副本 priority 总和）。相同输入恒得相同结果。
    """
    _check_duplicate_names([r.name for r in request.replicas])
    if len(request.replicas) > MAX_RETENTION_REPLICAS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "too_many_replicas",
                "message": f"副本数不得超过 {MAX_RETENTION_REPLICAS}",
                "limit": MAX_RETENTION_REPLICAS,
                "got": len(request.replicas),
            },
        )

    raw_deltas = [(d.from_serial, d.to_serial, d.bytes) for d in request.deltas]
    try:
        chain, sizes = build_sized_delta_chain(raw_deltas)
    except DeltaValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    retained, used_bytes, modes, domain_summaries = plan_domain_retention(
        request.target,
        [
            (r.current, r.priority, r.failure_domain)
            for r in request.replicas
        ],
        chain,
        sizes,
        request.budget,
    )

    results = [
        DomainRetentionPlanItem(
            name=replica.name,
            current=replica.current,
            target=request.target,
            failure_domain=replica.failure_domain,
            mode=mode,
            reason=reason,
        )
        for replica, (mode, reason) in zip(request.replicas, modes)
    ]

    return DomainRetentionPlanResponse(
        target=request.target,
        budget=request.budget,
        used_bytes=used_bytes,
        retained_deltas=[
            RetainedDelta(from_serial=frm, to_serial=to, bytes=sizes[(frm, to)])
            for frm, to in retained
        ],
        results=results,
        domain_summaries=[
            DomainSummary(
                failure_domain=domain,
                covered_priority=covered,
                total_priority=total,
            )
            for domain, covered, total in domain_summaries
        ],
    )


@app.post("/rollout-gate", response_model=RolloutGateResponse)
def rollout_gate(request: RolloutGateRequest) -> RolloutGateResponse:
    """上线门禁：按时间序列观测判定 quorum 是否在基线之上被满足。

    每个副本携带按时间排序的观测序列 ``(timestamp, serial)``；时间戳可缺失
    （null），但**存在的时间戳必须严格递增**——同一副本时间戳重复或倒序
    返回 422（``duplicate_observation_timestamp`` /
    ``observation_timestamp_out_of_order``）。

    相邻观测对按 RFC 1982 逐对判定并标记：

    * ``newer``：严格推进（允许跨 2^32 回绕，如 4294967295 -> 0）；
    * ``equal`` / ``older`` / ``ambiguous``：未变 / 回退 / 正对点不可比较；
    * ``missing_time_order``：任一时间戳缺失，无法建立时间顺序。

    逐副本输出**首次达到基线之后的有效推进区间**（闭区间）：从首个等于或
    严格新于基线的观测起，沿 ``newer`` / ``equal`` 且推进后仍达到基线的
    相邻对扩展；遇 ``older`` / ``ambiguous`` / ``missing_time_order`` 或
    推进后跌破基线即终止。从未达到基线（``baseline_not_reached``）或首次
    达到的观测无时间戳（``missing_time_order``）时区间为空并给出原因。

    再以**事件扫掠**在各区间端点上统计同时处于有效推进区间内的副本数，
    给出 quorum 首次满足时间 ``first_satisfied_at`` 与之后再次跌破时间
    ``dropped_below_at``（满足后未再跌破为 null）。quorum 从未满足时仍
    返回 200，``reason`` 给出明确原因：``quorum_exceeds_replicas``
    （quorum 超过副本总数）或 ``quorum_never_reached``（峰值并发不足）。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    try:
        for replica in request.replicas:
            check_observation_time_order(
                replica.name, [o.timestamp for o in replica.observations]
            )
    except ObservationOrderError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    outcomes, satisfied, first_satisfied_at, dropped_below_at, peak, reason = (
        evaluate_rollout_gate(
            request.baseline,
            request.quorum,
            [
                [(o.timestamp, o.serial) for o in replica.observations]
                for replica in request.replicas
            ],
        )
    )

    results = []
    for replica, outcome in zip(request.replicas, outcomes):
        reaches, first_reached_at, interval, replica_reason, transitions = outcome
        results.append(
            RolloutReplicaItem(
                name=replica.name,
                reaches_baseline=reaches,
                first_reached_at=first_reached_at,
                interval=(
                    RolloutInterval(start=interval[0], end=interval[1])
                    if interval is not None
                    else None
                ),
                reason=replica_reason,
                transitions=[
                    ObservationTransition(
                        index=index,
                        from_timestamp=replica.observations[index - 1].timestamp,
                        to_timestamp=replica.observations[index].timestamp,
                        from_serial=replica.observations[index - 1].serial,
                        to_serial=replica.observations[index].serial,
                        relation=relation,
                    )
                    for index, relation in transitions
                ],
            )
        )

    return RolloutGateResponse(
        baseline=request.baseline,
        quorum=request.quorum,
        satisfied=satisfied,
        first_satisfied_at=first_satisfied_at,
        dropped_below_at=dropped_below_at,
        peak_concurrent=peak,
        reason=reason,
        replicas=results,
    )


@app.post("/propagation-slo", response_model=PropagationSloResponse)
def propagation_slo(request: PropagationSloRequest) -> PropagationSloResponse:
    """传播 SLO：评估每次发布在其窗口内向各故障域的收敛时延。

    发布事件按 ``released_at`` **严格递增**（重复或倒序返回 422
    ``duplicate_release_timestamp`` / ``release_timestamp_out_of_order``）；
    每个副本的观测时间戳同样必须严格递增（复用
    ``duplicate_observation_timestamp`` / ``observation_timestamp_out_of_order``）。

    对第 i 次发布，窗口为左闭右开 ``[released_at[i], released_at[i+1])``
    （最后一次发布延伸到观测结束）；**只**用窗口内的观测判定，各窗口的
    到达状态相互独立。窗口内每条观测按 RFC 1982 相对本次发布版本分类：

    * ``equal`` / ``newer``：副本**到达**该版本——其故障域若尚未到达，
      以该观测时间为域的 ``reached_at``（首个有效到达）；
    * ``older`` / ``ambiguous``：若该副本**已到达**，其到达状态失效，
      且所在故障域的本窗口到达状态一并失效，须等待后续重新到达
      （同一时间戳的事件先处理回退再处理到达）。

    全部故障域都到达的最早时刻即 ``converged_at``（等于各域
    ``reached_at`` 的最晚值），之后的回退不再影响结果；
    ``latency_seconds = converged_at - released_at``，
    ``met_deadline = latency_seconds <= deadline_seconds``。
    窗口结束仍未覆盖全部域时 ``converged_at`` 与 ``latency_seconds``
    为 null、``met_deadline`` 为 false，并给出升序的 ``missing_domains``。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    try:
        check_release_time_order([r.released_at for r in request.releases])
        for replica in request.replicas:
            check_observation_time_order(
                replica.name, [o.timestamp for o in replica.observations]
            )
    except (ReleaseOrderError, ObservationOrderError) as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    outcomes = evaluate_propagation_slo(
        [(r.serial, r.released_at) for r in request.releases],
        [
            (
                replica.name,
                replica.failure_domain,
                [(o.timestamp, o.serial) for o in replica.observations],
            )
            for replica in request.replicas
        ],
        request.deadline_seconds,
    )

    return PropagationSloResponse(
        deadline_seconds=request.deadline_seconds,
        releases=[
            PropagationReleaseItem(
                serial=serial,
                released_at=released_at,
                converged_at=converged_at,
                latency_seconds=latency_seconds,
                met_deadline=met_deadline,
                domains=[
                    PropagationDomainStatus(
                        failure_domain=domain, reached_at=reached_at
                    )
                    for domain, reached_at in domain_status
                ],
                missing_domains=missing_domains,
            )
            for (
                serial,
                released_at,
                converged_at,
                latency_seconds,
                met_deadline,
                domain_status,
                missing_domains,
            ) in outcomes
        ],
    )


@app.post("/divergence-windows", response_model=DivergenceWindowsResponse)
def divergence_windows(
    request: DivergenceWindowsRequest,
) -> DivergenceWindowsResponse:
    """分裂窗口：沿时间线检测各副本不存在共同严格推进候选的连续区间。

    每个副本携带按时间排序的观测序列 ``(timestamp, serial)``，timestamp 必填
    非负。时间戳允许**同刻多条**（同一时刻原子合并），但必须非递减：倒序返回
    422（``observation_timestamp_out_of_order``）；同一副本在同一时间戳出现
    不同序列号返回 422（``observation_serial_conflict``）；同刻同值的重复观测
    合法并去重。副本名重复返回 422（``duplicate_replica_name``）。

    全部观测汇成事件按时间排序：每个时间戳**先原子合并该时刻的全部更新**
    （同一时刻任意多个副本一起生效），再沿时间线携带各副本最近一次观测值
    （尚无观测的副本不参与判定）。每个合并后状态按 **RFC 1982 环形规则**
    判定是否存在一个候选值能对所有副本**同时严格推进**（各开弧
    ``{c+1, ..., c+2^31-1}`` 模 2^32 的交集非空），**不用整数极差**：
    例如 ``4294967290`` 与 ``10`` 整数极差近 2^32 却存在共同后继，而
    ``0`` 与 ``2^31-1`` 整数极差不足 2^31 却已无候选。

    连续无可行候选的状态合并为一个分裂窗口：``start_at`` 为首个无候选状态的
    时间；``end_at`` 为其后**首个恢复可行状态**的时间，
    ``duration_seconds = end_at - start_at``；观测结束仍未恢复时
    ``end_at`` / ``duration_seconds`` 为 null。窗口给出涉及副本 ``replicas``
    及各副本**进入窗口时**的序列号 ``entries``（窗口开启时对全部已知副本快照，
    开启后才首次出现的副本取其窗口内首条观测值）。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    try:
        for replica in request.replicas:
            check_divergence_observation_order(
                replica.name,
                [(o.timestamp, o.serial) for o in replica.observations],
            )
    except ObservationOrderError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    outcomes = evaluate_divergence_windows(
        [
            [(o.timestamp, o.serial) for o in replica.observations]
            for replica in request.replicas
        ]
    )

    names = [replica.name for replica in request.replicas]
    return DivergenceWindowsResponse(
        windows=[
            DivergenceWindow(
                start_at=start_at,
                end_at=end_at,
                duration_seconds=duration_seconds,
                replicas=[names[index] for index, _serial in entries],
                entries=[
                    DivergenceEntry(name=names[index], serial=serial)
                    for index, serial in entries
                ],
            )
            for start_at, end_at, duration_seconds, entries in outcomes
        ]
    )


@app.post("/delta-consensus", response_model=DeltaConsensusResponse)
def delta_consensus(request: DeltaConsensusRequest) -> DeltaConsensusResponse:
    """IXFR 增量共识：多镜像上报按完整三元组计票，沿可信边组装到目标的唯一链。

    先做整体校验（422）：

    * 镜像名重复：``duplicate_replica_name``；
    * 每条上报边的 ``to_serial`` 必须按 RFC 1982 严格新于 ``from_serial``
      （``delta_not_strictly_newer``，含镜像名与关系）；
    * 单个镜像对**同一 from_serial** 上报冲突版本——不同 ``to_serial`` 或不同
      内容摘要 ``digest``——为 ``mirror_version_conflict``，返回冲突的两份
      完整上报；完全相同的三元组重复上报合法并去重。

    校验通过后按**完整三元组** ``(from_serial, to_serial, digest)`` 统计上报
    镜像数（同一镜像重复只计一票），票数达到 ``quorum`` 的边进入可信集合；
    同一序号对上两个不同摘要都达到法定人数时同样视为分叉，不静默二选一。

    可信集合中每个 from_serial 只有**唯一可信出边**才能续链：从无入边的根
    节点沿唯一出边组装，遇缺口（``gap``）/ 分叉（``fork``）/ 环路（``loop``）
    即停；取最长的一条为 ``trusted_chain``（长度并列按边序列、根节点字典序
    破平）。``target_reachable`` 当且仅当最长可信链终点恰为 ``target``。
    可信集合中的全部分叉与环路另列在 ``forks`` / ``loops`` 中作为证据，
    **绝不静默选边**。

    逐镜像（请求顺序）给出证据：``missing`` 未上报的可信序号对、
    ``minority_digests`` 序号对可信但摘要未达法定人数（含票数与可信摘要）、
    ``extra_edges`` 不构成可信三元组的额外上报（含票数）。
    quorum 超过镜像总数时不会报错，但没有任何边能达法定人数（链空、不可达）。
    """
    _check_duplicate_names([m.name for m in request.mirrors])

    reports = [
        (
            mirror.name,
            [(d.from_serial, d.to_serial, d.digest) for d in mirror.deltas],
        )
        for mirror in request.mirrors
    ]
    try:
        validate_mirror_reports(reports)
    except MirrorReportError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    outcome = evaluate_delta_consensus(request.target, request.quorum, reports)

    def vote_digests(items: list) -> list[ConsensusVoteDigest]:
        return [
            ConsensusVoteDigest(digest=item["digest"], votes=item["votes"])
            for item in items
        ]

    return DeltaConsensusResponse(
        target=outcome["target"],
        quorum=outcome["quorum"],
        mirror_count=outcome["mirror_count"],
        trusted_chain=[
            ConsensusEdge(
                from_serial=e["from_serial"],
                to_serial=e["to_serial"],
                digest=e["digest"],
                votes=e["votes"],
                voters=e["voters"],
            )
            for e in outcome["trusted_chain"]
        ],
        chain_length=outcome["chain_length"],
        chain_end=outcome["chain_end"],
        chain_reason=outcome["chain_reason"],
        target_reachable=outcome["target_reachable"],
        trusted_edges=[
            ConsensusEdge(
                from_serial=e["from_serial"],
                to_serial=e["to_serial"],
                digest=e["digest"],
                votes=e["votes"],
                voters=e["voters"],
            )
            for e in outcome["trusted_edges"]
        ],
        forks=[
            ConsensusFork(
                from_serial=fork["from_serial"],
                branches=[
                    ConsensusForkBranch(
                        to_serial=branch["to_serial"],
                        digests=vote_digests(branch["digests"]),
                    )
                    for branch in fork["branches"]
                ],
            )
            for fork in outcome["forks"]
        ],
        loops=[
            [
                ConsensusLoopEdge(
                    from_serial=e["from_serial"],
                    to_serial=e["to_serial"],
                    digest=e["digest"],
                    votes=e["votes"],
                )
                for e in loop
            ]
            for loop in outcome["loops"]
        ],
        mirrors=[
            ConsensusMirrorEvidence(
                mirror=item["mirror"],
                reported_edges=item["reported_edges"],
                missing=[
                    MissingTrustedEdge(
                        from_serial=e["from_serial"],
                        to_serial=e["to_serial"],
                        digests=vote_digests(e["digests"]),
                    )
                    for e in item["missing"]
                ],
                minority_digests=[
                    MinorityDigest(
                        from_serial=e["from_serial"],
                        to_serial=e["to_serial"],
                        digest=e["digest"],
                        votes=e["votes"],
                        trusted_digests=vote_digests(e["trusted_digests"]),
                    )
                    for e in item["minority_digests"]
                ],
                extra_edges=[
                    ExtraConsensusEdge(
                        from_serial=e["from_serial"],
                        to_serial=e["to_serial"],
                        digest=e["digest"],
                        votes=e["votes"],
                    )
                    for e in item["extra_edges"]
                ],
            )
            for item in outcome["mirrors"]
        ],
    )


@app.post(
    "/serial-scenarios/generate",
    response_model=SerialScenarioGenerateResponse,
)
def generate_serial_scenarios_endpoint(
    request: SerialScenarioGenerateRequest,
) -> SerialScenarioGenerateResponse:
    """确定性生成覆盖 RFC 1982 边界的可复现测试场景。

    给定非负 32 位 ``seed``、``replica_count``（1..12）、
    ``scenario_count``（1..4）与候选标签集合 ``include``
    （``wrap`` / ``ambiguous`` / ``fork`` / ``loop`` 的非空子集，
    重复或未知标签返回 422），
    以 seed 为唯一随机性来源（内置 64 位 SplitMix64，不读取系统随机源）
    生成：

    * ``scenarios``：按固定规范顺序（ambiguous → fork → loop → wrap）从
      include 中选出的场景标签——include 的给出顺序不影响结果；
    * ``replicas``：``ns-1`` 起的副本序列，靠前槽位依次锚定各场景的关键
      序列号（如 ambiguous 场景锚定在与目标相差正好 2³¹ 的正对点）；
    * ``target``：目标 32 位无符号 SOA 序列号；
    * ``deltas``：**无序** IXFR 增量样本（整体已乱序），包含选中场景的
      边界增量——``wrap`` 为跨 2³² 回绕的合法 newer 边，``ambiguous``
      为前向距离恰 2³¹ 的不可比较边，``fork`` 为同一 from_serial 的两条
      严格推进边，``loop`` 为三条各自严格 newer、距离之和恰 2³² 的
      回绕环——外加一条普通背景增量链；
    * ``triggers``：逐场景给出触发点说明（涉及增量、前向距离 / RFC 1982
      关系、锚定副本与在各接口上的预期判定）。

    相同请求（seed / replica_count / scenario_count / include 相同）恒得
    完全相同的输出，便于固化回归用例。非法参数（空 include、重复或未知
    标签、scenario_count 超过 include 去重标签数或 4、replica_count
    超过 12、seed / 数量越界或类型错误）返回 422。
    """
    include = list(request.include)

    if len(include) != len(set(include)):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "duplicate_scenario_label",
                "message": "include 中的场景标签不可重复（重复请去重）",
                "include": include,
            },
        )

    unique_count = len(set(include))
    if request.scenario_count > unique_count:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "scenario_count_exceeds_include",
                "message": (
                    "scenario_count 不得超过 include 去重后的标签数"
                ),
                "scenario_count": request.scenario_count,
                "include_count": unique_count,
            },
        )

    outcome = generate_serial_scenarios(
        seed=request.seed,
        replica_count=request.replica_count,
        scenario_count=request.scenario_count,
        include=include,
    )

    return SerialScenarioGenerateResponse(
        seed=outcome["seed"],
        replica_count=outcome["replica_count"],
        scenario_count=outcome["scenario_count"],
        include=outcome["include"],
        scenarios=outcome["scenarios"],
        replicas=[
            ScenarioReplica(name=item["name"], current=item["current"])
            for item in outcome["replicas"]
        ],
        target=outcome["target"],
        deltas=[
            ScenarioDelta(
                from_serial=item["from_serial"], to_serial=item["to_serial"]
            )
            for item in outcome["deltas"]
        ],
        triggers=[
            ScenarioTrigger(
                label=item["label"],
                title=item["title"],
                description=item["description"],
                deltas=[
                    TriggerDelta(
                        from_serial=edge["from_serial"],
                        to_serial=edge["to_serial"],
                        forward_distance=edge["forward_distance"],
                        relation=edge["relation"],
                    )
                    for edge in item["deltas"]
                ],
                replica=item["replica"],
            )
            for item in outcome["triggers"]
        ],
    )
