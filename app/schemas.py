"""请求 / 响应 Pydantic 模型。"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .serial import (
    MAX_SERIAL,
    MAX_RETENTION_REPLICAS,
    NEWER,
    EQUAL,
    OLDER,
    AMBIGUOUS,
    AXFR,
    IXFR,
    UP_TO_DATE,
)

Serial32 = Annotated[int, Field(ge=0, le=MAX_SERIAL)]
Relation = Literal["newer", "equal", "older", "ambiguous"]
RELATIONS = (NEWER, EQUAL, OLDER, AMBIGUOUS)


def _coerce_serial32(value: object) -> int:
    """严格的 32 位无符号整数校验（拒绝 bool / 浮点 / 字符串）。"""
    # bool 是 int 的子类，需显式排除
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("序列号必须是 32 位无符号整数")
    if not 0 <= value <= MAX_SERIAL:
        raise ValueError(f"序列号必须在 [0, {MAX_SERIAL}] 范围内")
    return value


def _coerce_non_negative_int(value: object, label: str) -> int:
    """严格的非负整数校验（拒绝 bool / 浮点 / 字符串）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}必须是非负整数")
    if value < 0:
        raise ValueError(f"{label}必须是非负整数")
    return value


class ReplicaCurrent(BaseModel):
    """单个副本当前持有的序列号。"""

    name: str = Field(..., min_length=1, description="副本名称（不可重复）")
    current: int = Field(..., description="当前 32 位无符号 SOA 序列号")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("副本名称不能为空")
        return value

    @field_validator("current", mode="before")
    @classmethod
    def _current_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class AuditRequest(BaseModel):
    """POST /audit 请求体。"""

    proposed: int = Field(..., description="拟发布的 32 位无符号 SOA 序列号")
    replicas: List[ReplicaCurrent] = Field(..., min_length=1)

    @field_validator("proposed", mode="before")
    @classmethod
    def _proposed_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class AuditItem(BaseModel):
    """单个副本的审计结果。"""

    name: str
    current: int
    proposed: int
    relation: Relation
    forward_distance: int = Field(
        ..., ge=0, le=MAX_SERIAL,
        description="前向模距离 (proposed - current) mod 2^32",
    )
    blocking: bool = Field(
        ..., description="是否阻塞发布：relation 不是 newer 即为 true",
    )


class AuditResponse(BaseModel):
    """POST /audit 响应体。"""

    proposed: int
    results: List[AuditItem]
    blocked: bool = Field(..., description="是否存在任一阻塞副本")
    blocking_replicas: List[str] = Field(
        ..., description="阻塞发布的副本名列表（输入顺序）",
    )


class ProposeRequest(BaseModel):
    """POST /propose 请求体。"""

    replicas: List[ReplicaCurrent] = Field(..., min_length=1)


class ProposeItem(BaseModel):
    """候选值对单个副本的推进情况。"""

    name: str
    current: int
    relation: Relation
    forward_distance: int


class ProposeResponse(BaseModel):
    """POST /propose 响应体（全部副本对该候选均为 newer）。"""

    proposed: int
    max_forward_distance: int = Field(
        ..., ge=1, le=2**31 - 1,
        description="所有副本中最大的前向模距离（被最小化的目标）",
    )
    results: List[ProposeItem]


# ---------------------------------------------------------------------------
# POST /transfer-plan
# ---------------------------------------------------------------------------

TransferMode = Literal["up_to_date", "ixfr", "axfr"]
TRANSFER_MODES = (UP_TO_DATE, IXFR, AXFR)
TransferReason = Literal["gap", "loop", "target_incomparable"]


class IxfrDelta(BaseModel):
    """一条无序 IXFR 增量：from_serial 到 to_serial（须严格 newer）。"""

    from_serial: int = Field(..., description="增量起始 32 位无符号 SOA 序列号")
    to_serial: int = Field(..., description="增量目标 32 位无符号 SOA 序列号")

    @field_validator("from_serial", "to_serial", mode="before")
    @classmethod
    def _serial32_fields(cls, value: object) -> int:
        return _coerce_serial32(value)


class TransferPlanRequest(BaseModel):
    """POST /transfer-plan 请求体。"""

    target: int = Field(..., description="目标 32 位无符号 SOA 序列号")
    replicas: List[ReplicaCurrent] = Field(..., min_length=1)
    deltas: List[IxfrDelta] = Field(
        ..., description="一组无序 IXFR 增量（允许重复，分叉将被拒绝）",
    )

    @field_validator("target", mode="before")
    @classmethod
    def _target_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class OrderedDelta(BaseModel):
    """按链路顺序排列好的单条 IXFR 增量。"""

    from_serial: int
    to_serial: int


class TransferPlanItem(BaseModel):
    """单个副本的传输规划结果（顺序与请求中的副本一致）。"""

    name: str
    current: int
    target: int
    mode: TransferMode = Field(
        ...,
        description=(
            "up_to_date：当前值已等于目标；ixfr：存在唯一完整增量链；"
            "axfr：存在缺口/环路或目标不可比较"
        ),
    )
    reason: Optional[TransferReason] = Field(
        ..., description="仅 axfr 时给出原因：gap / loop / target_incomparable",
    )
    ordered_deltas: List[OrderedDelta] = Field(
        ..., description="ixfr 时按从当前值到目标的顺序给出增量；其余模式为空",
    )


class TransferPlanResponse(BaseModel):
    """POST /transfer-plan 响应体。"""

    target: int
    results: List[TransferPlanItem]


# ---------------------------------------------------------------------------
# POST /retention-plan
# ---------------------------------------------------------------------------

RetentionReason = Literal["gap", "loop", "target_incomparable", "budget"]


class SizedIxfrDelta(BaseModel):
    """一条带字节数的无序 IXFR 增量：from_serial 到 to_serial（须严格 newer）。"""

    from_serial: int = Field(..., description="增量起始 32 位无符号 SOA 序列号")
    to_serial: int = Field(..., description="增量目标 32 位无符号 SOA 序列号")
    bytes: int = Field(..., description="增量占用的字节数（非负整数）")

    @field_validator("from_serial", "to_serial", mode="before")
    @classmethod
    def _serial32_fields(cls, value: object) -> int:
        return _coerce_serial32(value)

    @field_validator("bytes", mode="before")
    @classmethod
    def _bytes_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "增量字节数")


class RetentionReplica(ReplicaCurrent):
    """单个副本：当前序列号 + 非负 priority。"""

    priority: int = Field(..., description="副本优先级（非负整数）")

    @field_validator("priority", mode="before")
    @classmethod
    def _priority_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "副本 priority")


class RetentionPlanRequest(BaseModel):
    """POST /retention-plan 请求体。"""

    target: int = Field(..., description="目标 32 位无符号 SOA 序列号")
    replicas: List[RetentionReplica] = Field(
        ...,
        min_length=1,
        description=f"副本列表（至多 {MAX_RETENTION_REPLICAS} 个）",
    )
    deltas: List[SizedIxfrDelta] = Field(
        ...,
        description=(
            "一组带字节数的无序 IXFR 增量"
            "（允许完全相同的重复，分叉 / 字节数冲突将被拒绝）"
        ),
    )
    budget: int = Field(..., description="总字节预算（非负整数）")

    @field_validator("target", mode="before")
    @classmethod
    def _target_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)

    @field_validator("budget", mode="before")
    @classmethod
    def _budget_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "字节预算")


class RetainedDelta(BaseModel):
    """一条被保留的增量（按 (from_serial, to_serial) 升序输出）。"""

    from_serial: int
    to_serial: int
    bytes: int


class RetentionPlanItem(BaseModel):
    """单个副本的保留规划结果（顺序与请求中的副本一致）。"""

    name: str
    current: int
    target: int
    mode: TransferMode = Field(
        ...,
        description=(
            "up_to_date：当前值已等于目标；ixfr：保留的增量覆盖了到目标的"
            "完整链；axfr：完整链不可达或未被保留"
        ),
    )
    reason: Optional[RetentionReason] = Field(
        ...,
        description=(
            "仅 axfr 时给出原因：gap / loop / target_incomparable / budget"
            "（完整集合下存在完整链，但因预算未被保留）"
        ),
    )


class RetentionPlanResponse(BaseModel):
    """POST /retention-plan 响应体。"""

    target: int
    budget: int
    used_bytes: int = Field(..., description="被保留增量占用的总字节数")
    retained_deltas: List[RetainedDelta] = Field(
        ..., description="被保留的增量，按 (from_serial, to_serial) 升序",
    )
    results: List[RetentionPlanItem]


# ---------------------------------------------------------------------------
# POST /domain-retention-plan
# ---------------------------------------------------------------------------

class DomainRetentionReplica(ReplicaCurrent):
    """单个副本：当前序列号 + 非负 priority + 非空 failure_domain。"""

    priority: int = Field(..., description="副本优先级（非负整数）")
    failure_domain: str = Field(
        ..., min_length=1, description="故障域标识（非空字符串，如机房 / 可用区）",
    )

    @field_validator("priority", mode="before")
    @classmethod
    def _priority_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "副本 priority")

    @field_validator("failure_domain")
    @classmethod
    def _failure_domain_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("failure_domain 不能为空")
        return value


class DomainRetentionPlanRequest(BaseModel):
    """POST /domain-retention-plan 请求体。"""

    target: int = Field(..., description="目标 32 位无符号 SOA 序列号")
    replicas: List[DomainRetentionReplica] = Field(
        ...,
        min_length=1,
        description=f"副本列表（至多 {MAX_RETENTION_REPLICAS} 个）",
    )
    deltas: List[SizedIxfrDelta] = Field(
        ...,
        description=(
            "一组带字节数的无序 IXFR 增量"
            "（允许完全相同的重复，分叉 / 字节数冲突将被拒绝）"
        ),
    )
    budget: int = Field(..., description="总字节预算（非负整数）")

    @field_validator("target", mode="before")
    @classmethod
    def _target_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)

    @field_validator("budget", mode="before")
    @classmethod
    def _budget_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "字节预算")


class DomainRetentionPlanItem(BaseModel):
    """单个副本的按域保留规划结果（顺序与请求中的副本一致）。"""

    name: str
    current: int
    target: int
    failure_domain: str = Field(..., description="副本所属故障域（回显请求值）")
    mode: TransferMode = Field(
        ...,
        description=(
            "up_to_date：当前值已等于目标；ixfr：保留的增量覆盖了到目标的"
            "完整链；axfr：完整链不可达或未被保留"
        ),
    )
    reason: Optional[RetentionReason] = Field(
        ...,
        description=(
            "仅 axfr 时给出原因：gap / loop / target_incomparable / budget"
            "（完整集合下存在完整链，但因预算未被保留）"
        ),
    )


class DomainSummary(BaseModel):
    """单个 failure_domain 的覆盖汇总（按 failure_domain 名称升序）。"""

    failure_domain: str
    covered_priority: int = Field(
        ..., ge=0,
        description="该域已覆盖（ixfr 与 up_to_date）副本的 priority 总和",
    )
    total_priority: int = Field(
        ..., ge=0, description="该域全部副本的 priority 总和",
    )


class DomainRetentionPlanResponse(BaseModel):
    """POST /domain-retention-plan 响应体。"""

    target: int
    budget: int
    used_bytes: int = Field(..., description="被保留增量占用的总字节数")
    retained_deltas: List[RetainedDelta] = Field(
        ..., description="被保留的增量，按 (from_serial, to_serial) 升序",
    )
    results: List[DomainRetentionPlanItem]
    domain_summaries: List[DomainSummary] = Field(
        ...,
        description=(
            "按 failure_domain 汇总的 covered_priority / total_priority"
            "（按 failure_domain 名称升序）"
        ),
    )


# ---------------------------------------------------------------------------
# POST /rollout-gate
# ---------------------------------------------------------------------------

TransitionRelation = Literal[
    "newer", "equal", "older", "ambiguous", "missing_time_order"
]
RolloutReplicaReason = Literal["baseline_not_reached", "missing_time_order"]
RolloutGateReason = Literal["quorum_exceeds_replicas", "quorum_never_reached"]


class RolloutObservation(BaseModel):
    """一条观测：时间戳（可缺失）+ 观测到的 SOA 序列号。"""

    timestamp: Optional[int] = Field(
        None,
        description=(
            "观测时间戳（Unix 秒，非负整数）；缺失（null）时该相邻对标记 "
            "missing_time_order，且不能作为有效推进区间的端点"
        ),
    )
    serial: int = Field(..., description="观测到的 32 位无符号 SOA 序列号")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _timestamp_non_negative(cls, value: object) -> Optional[int]:
        if value is None:
            return None
        return _coerce_non_negative_int(value, "观测时间戳")

    @field_validator("serial", mode="before")
    @classmethod
    def _serial_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class RolloutReplica(BaseModel):
    """单个副本：名称 + 按时间排序的观测序列。"""

    name: str = Field(..., min_length=1, description="副本名称（不可重复）")
    observations: List[RolloutObservation] = Field(
        ...,
        min_length=1,
        description=(
            "按时间排序的观测序列（至少一条）；存在的时间戳必须严格递增，"
            "重复或倒序返回 422"
        ),
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("副本名称不能为空")
        return value


class RolloutGateRequest(BaseModel):
    """POST /rollout-gate 请求体。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "baseline": 2024092101,
                    "quorum": 2,
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 1727000000, "serial": 2024092100},
                                {"timestamp": 1727000060, "serial": 2024092101},
                                {"timestamp": 1727000120, "serial": 2024092102},
                            ],
                        },
                        {
                            "name": "ns-b",
                            "observations": [
                                {"timestamp": 1727000030, "serial": 2024092101},
                                {"timestamp": 1727000090, "serial": 2024092101},
                            ],
                        },
                        {
                            "name": "ns-c",
                            "observations": [
                                {"timestamp": 1727000200, "serial": 2024092101},
                            ],
                        },
                    ],
                },
                {
                    "baseline": 4294967294,
                    "quorum": 2,
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 1000, "serial": 4294967294},
                                {"timestamp": 1010, "serial": 4294967295},
                                {"timestamp": 1020, "serial": 0},
                            ],
                        },
                        {
                            "name": "ns-b",
                            "observations": [
                                {"timestamp": 1005, "serial": 4294967295},
                                {"timestamp": None, "serial": 0},
                            ],
                        },
                    ],
                },
            ]
        }
    )

    baseline: int = Field(..., description="基线 32 位无符号 SOA 序列号")
    quorum: int = Field(
        ..., description="所需 quorum：需同时达到基线的副本数下限（正整数）",
    )
    replicas: List[RolloutReplica] = Field(..., min_length=1)

    @field_validator("baseline", mode="before")
    @classmethod
    def _baseline_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)

    @field_validator("quorum", mode="before")
    @classmethod
    def _quorum_positive(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("quorum 必须是正整数")
        if value < 1:
            raise ValueError("quorum 必须是正整数")
        return value


class ObservationTransition(BaseModel):
    """相邻观测对的判定结果（按输入顺序，index 为后一条观测的下标）。"""

    index: int = Field(..., ge=1, description="后一条观测在 observations 中的下标")
    from_timestamp: Optional[int] = Field(
        ..., description="前一条观测的时间戳（缺失为 null）",
    )
    to_timestamp: Optional[int] = Field(
        ..., description="后一条观测的时间戳（缺失为 null）",
    )
    from_serial: int
    to_serial: int
    relation: TransitionRelation = Field(
        ...,
        description=(
            "newer：严格推进（允许跨 2^32 回绕）；equal：未变；older：回退；"
            "ambiguous：正对点（差值恰为 2^31，不可比较）；"
            "missing_time_order：任一时间戳缺失，无法建立时间顺序"
        ),
    )


class RolloutInterval(BaseModel):
    """首次达到基线之后的有效推进闭区间 [start, end]（Unix 秒）。"""

    start: int = Field(..., description="首次达到基线的观测时间戳")
    end: int = Field(
        ..., description="最后一个有效观测的时间戳（区间外状态未知，不计入）",
    )


class RolloutReplicaItem(BaseModel):
    """单个副本的上线门禁评估结果（顺序与请求中的副本一致）。"""

    name: str
    reaches_baseline: bool = Field(..., description="是否有任一观测达到基线")
    first_reached_at: Optional[int] = Field(
        ...,
        description="首次达到基线的观测时间戳；未达到或该观测无时间戳时为 null",
    )
    interval: Optional[RolloutInterval] = Field(
        ..., description="首次达到基线之后的有效推进闭区间；无区间时为 null",
    )
    reason: Optional[RolloutReplicaReason] = Field(
        ...,
        description=(
            "无区间时的原因：baseline_not_reached（从未达到基线）或 "
            "missing_time_order（达到了但首次达到的观测无时间戳，无法定位区间）"
        ),
    )
    transitions: List[ObservationTransition] = Field(
        ..., description="相邻观测对的 RFC 1982 判定，按输入顺序",
    )


class RolloutGateResponse(BaseModel):
    """POST /rollout-gate 响应体。"""

    baseline: int
    quorum: int
    satisfied: bool = Field(..., description="quorum 是否曾被满足")
    first_satisfied_at: Optional[int] = Field(
        ..., description="quorum 首次满足的时间戳；从未满足为 null",
    )
    dropped_below_at: Optional[int] = Field(
        ..., description="首次满足之后 quorum 再次跌破的时间戳；未再跌破为 null",
    )
    peak_concurrent: int = Field(
        ..., ge=0, description="事件时间点上同时处于有效推进区间内的副本数峰值",
    )
    reason: Optional[RolloutGateReason] = Field(
        ...,
        description=(
            "未满足时的原因：quorum_exceeds_replicas（quorum 超过副本总数）或 "
            "quorum_never_reached（峰值并发不足）"
        ),
    )
    replicas: List[RolloutReplicaItem]


# ---------------------------------------------------------------------------
# POST /propagation-slo
# ---------------------------------------------------------------------------


class PropagationObservation(BaseModel):
    """一条副本观测：时间戳（必填）+ 观测到的 SOA 序列号。"""

    timestamp: int = Field(
        ...,
        description="观测时间戳（Unix 秒，非负整数）；同一副本内必须严格递增",
    )
    serial: int = Field(..., description="观测到的 32 位无符号 SOA 序列号")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _timestamp_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "观测时间戳")

    @field_validator("serial", mode="before")
    @classmethod
    def _serial_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class PropagationReplica(BaseModel):
    """单个副本：名称 + 非空 failure_domain + 按时间严格递增的观测序列。"""

    name: str = Field(..., min_length=1, description="副本名称（不可重复）")
    failure_domain: str = Field(
        ..., min_length=1, description="故障域标识（非空字符串，如机房 / 可用区）",
    )
    observations: List[PropagationObservation] = Field(
        ...,
        description=(
            "按时间排序的观测序列（可为空）；时间戳必须严格递增，"
            "重复或倒序返回 422"
        ),
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("副本名称不能为空")
        return value

    @field_validator("failure_domain")
    @classmethod
    def _failure_domain_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("failure_domain 不能为空")
        return value


class ReleaseEvent(BaseModel):
    """一次发布事件：发布的 SOA 序列号 + 发布时间（发布时间必须严格递增）。"""

    serial: int = Field(..., description="本次发布的 32 位无符号 SOA 序列号")
    released_at: int = Field(
        ...,
        description="发布时间戳（Unix 秒，非负整数）；各发布之间必须严格递增",
    )

    @field_validator("serial", mode="before")
    @classmethod
    def _serial_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)

    @field_validator("released_at", mode="before")
    @classmethod
    def _released_at_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "发布时间戳")


class PropagationSloRequest(BaseModel):
    """POST /propagation-slo 请求体。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "deadline_seconds": 300,
                    "releases": [
                        {"serial": 2024092101, "released_at": 1000},
                        {"serial": 2024092102, "released_at": 2000},
                    ],
                    "replicas": [
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-east",
                            "observations": [
                                {"timestamp": 1000, "serial": 2024092100},
                                {"timestamp": 1120, "serial": 2024092101},
                                {"timestamp": 2050, "serial": 2024092102},
                            ],
                        },
                        {
                            "name": "ns-b",
                            "failure_domain": "dc-west",
                            "observations": [
                                {"timestamp": 1180, "serial": 2024092101},
                                {"timestamp": 1900, "serial": 2024092101},
                            ],
                        },
                        {
                            "name": "ns-c",
                            "failure_domain": "dc-west",
                            "observations": [
                                {"timestamp": 1250, "serial": 2024092101},
                            ],
                        },
                    ],
                },
                {
                    "deadline_seconds": 100,
                    "releases": [{"serial": 10, "released_at": 0}],
                    "replicas": [
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-east",
                            "observations": [
                                {"timestamp": 10, "serial": 10},
                                {"timestamp": 20, "serial": 9},
                                {"timestamp": 50, "serial": 10},
                            ],
                        },
                        {
                            "name": "ns-b",
                            "failure_domain": "dc-west",
                            "observations": [
                                {"timestamp": 30, "serial": 10},
                            ],
                        },
                    ],
                },
            ]
        }
    )

    deadline_seconds: int = Field(
        ...,
        description=(
            "传播 SLO 时限（非负整数，秒）："
            "converged_at - released_at 不超过它即达标"
        ),
    )
    releases: List[ReleaseEvent] = Field(
        ...,
        min_length=1,
        description="按时间严格递增的发布事件序列（至少一条）",
    )
    replicas: List[PropagationReplica] = Field(..., min_length=1)

    @field_validator("deadline_seconds", mode="before")
    @classmethod
    def _deadline_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "deadline_seconds")


class PropagationDomainStatus(BaseModel):
    """单个故障域在一次发布窗口内的到达结果（按 failure_domain 名称升序）。"""

    failure_domain: str
    reached_at: Optional[int] = Field(
        ...,
        description="该域首个有效到达时间（Unix 秒）；窗口结束时仍未到达为 null",
    )


class PropagationReleaseItem(BaseModel):
    """单次发布的传播 SLO 评估结果（顺序与请求中的发布一致）。"""

    serial: int
    released_at: int
    converged_at: Optional[int] = Field(
        ...,
        description=(
            "全部故障域均到达的最早时间（等于各域 reached_at 的最晚值）；"
            "窗口结束仍未覆盖全部域为 null"
        ),
    )
    latency_seconds: Optional[int] = Field(
        ..., description="converged_at - released_at；未收敛为 null",
    )
    met_deadline: bool = Field(
        ...,
        description="latency_seconds <= deadline_seconds；未收敛为 false",
    )
    domains: List[PropagationDomainStatus] = Field(
        ..., description="逐故障域到达结果（按 failure_domain 名称升序）",
    )
    missing_domains: List[str] = Field(
        ..., description="窗口结束时仍未到达的故障域（按名称升序）；收敛时为空",
    )


class PropagationSloResponse(BaseModel):
    """POST /propagation-slo 响应体。"""

    deadline_seconds: int
    releases: List[PropagationReleaseItem]


# ---------------------------------------------------------------------------
# POST /divergence-windows
# ---------------------------------------------------------------------------


class DivergenceObservation(BaseModel):
    """一条副本观测：时间戳（必填）+ 观测到的 SOA 序列号。"""

    timestamp: int = Field(
        ...,
        description=(
            "观测时间戳（Unix 秒，非负整数）；同一副本内必须非递减，"
            "倒序返回 422"
        ),
    )
    serial: int = Field(..., description="观测到的 32 位无符号 SOA 序列号")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _timestamp_non_negative(cls, value: object) -> int:
        return _coerce_non_negative_int(value, "观测时间戳")

    @field_validator("serial", mode="before")
    @classmethod
    def _serial_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class DivergenceReplica(BaseModel):
    """单个副本：名称 + 按时间非递减的观测序列。"""

    name: str = Field(..., min_length=1, description="副本名称（不可重复）")
    observations: List[DivergenceObservation] = Field(
        ...,
        description=(
            "按时间排序的观测序列（可为空）；时间戳必须非递减（允许同刻多条，"
            "但同一副本同刻序列号必须一致，否则 422 observation_serial_conflict），"
            "倒序返回 422 observation_timestamp_out_of_order"
        ),
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("副本名称不能为空")
        return value


class DivergenceWindowsRequest(BaseModel):
    """POST /divergence-windows 请求体。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 0, "serial": 100},
                                {"timestamp": 20, "serial": 100},
                                {"timestamp": 50, "serial": 200},
                            ],
                        },
                        {
                            "name": "ns-b",
                            "observations": [
                                {"timestamp": 10, "serial": 100},
                                {"timestamp": 20, "serial": 2147483748},
                                {"timestamp": 40, "serial": 2147483748},
                                {"timestamp": 50, "serial": 200},
                            ],
                        },
                    ],
                },
                {
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 0, "serial": 4294967290},
                                {"timestamp": 10, "serial": 4294967290},
                                {"timestamp": 20, "serial": 2147483647},
                            ],
                        },
                        {
                            "name": "ns-b",
                            "observations": [
                                {"timestamp": 0, "serial": 10},
                                {"timestamp": 10, "serial": 0},
                            ],
                        },
                    ],
                },
            ]
        }
    )

    replicas: List[DivergenceReplica] = Field(..., min_length=1)


class DivergenceEntry(BaseModel):
    """分裂窗口内单个副本进入窗口时的状态。"""

    name: str = Field(..., description="副本名称（输入顺序）")
    serial: int = Field(
        ..., description="该副本进入窗口时（窗口开启时刻或其窗口内首个观测）持有的序列号",
    )


class DivergenceWindow(BaseModel):
    """一个连续无共同后继状态的分裂窗口。"""

    start_at: int = Field(
        ..., description="窗口起始时间戳：首个无可行候选状态的时间（Unix 秒）",
    )
    end_at: Optional[int] = Field(
        ...,
        description=(
            "窗口结束时间戳：之后首个恢复可行候选状态的时间（Unix 秒）；"
            "观测结束仍未恢复为 null"
        ),
    )
    duration_seconds: Optional[int] = Field(
        ...,
        description="end_at - start_at；窗口尚未恢复时为 null",
    )
    replicas: List[str] = Field(
        ..., description="窗口涉及的副本名列表（按请求中的副本顺序）",
    )
    entries: List[DivergenceEntry] = Field(
        ..., description="各副本进入窗口时持有的序列号（按请求中的副本顺序）",
    )


class DivergenceWindowsResponse(BaseModel):
    """POST /divergence-windows 响应体。"""

    windows: List[DivergenceWindow] = Field(
        ..., description="分裂窗口列表，按 start_at 升序；无分裂时为空",
    )


# ---------------------------------------------------------------------------
# POST /delta-consensus
# ---------------------------------------------------------------------------

ConsensusChainReason = Literal["reached", "gap", "fork", "loop"]


class ConsensusReportDelta(BaseModel):
    """单个镜像上报的一条无序 IXFR 增量（含内容摘要）。"""

    from_serial: int = Field(..., description="增量起始 32 位无符号 SOA 序列号")
    to_serial: int = Field(..., description="增量目标 32 位无符号 SOA 序列号")
    digest: str = Field(
        ...,
        min_length=1,
        description="增量内容摘要（非空字符串，如十六进制哈希）；不同内容即不同版本",
    )

    @field_validator("from_serial", "to_serial", mode="before")
    @classmethod
    def _serial32_fields(cls, value: object) -> int:
        return _coerce_serial32(value)

    @field_validator("digest")
    @classmethod
    def _digest_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("内容摘要不能为空")
        return value


class ConsensusMirror(BaseModel):
    """单个镜像：名称 + 上报的无序 IXFR 清单。"""

    name: str = Field(..., min_length=1, description="镜像名称（不可重复）")
    deltas: List[ConsensusReportDelta] = Field(
        ...,
        description=(
            "该镜像上报的无序 IXFR 增量清单（允许完全相同的三元组重复）；"
            "同一 from_serial 上报不同 to_serial 或不同 digest 返回 422"
        ),
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("镜像名称不能为空")
        return value


class DeltaConsensusRequest(BaseModel):
    """POST /delta-consensus 请求体。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "target": 30,
                    "quorum": 2,
                    "mirrors": [
                        {
                            "name": "ns-a",
                            "deltas": [
                                {"from_serial": 20, "to_serial": 30, "digest": "h20"},
                                {"from_serial": 10, "to_serial": 20, "digest": "h10"},
                            ],
                        },
                        {
                            "name": "ns-b",
                            "deltas": [
                                {"from_serial": 10, "to_serial": 20, "digest": "h10"},
                                {"from_serial": 20, "to_serial": 30, "digest": "h20"},
                            ],
                        },
                        {
                            "name": "ns-c",
                            "deltas": [
                                {"from_serial": 10, "to_serial": 20, "digest": "h10"},
                                {"from_serial": 20, "to_serial": 30, "digest": "h20-bad"},
                            ],
                        },
                    ],
                },
                {
                    "target": 0,
                    "quorum": 2,
                    "mirrors": [
                        {
                            "name": "ns-a",
                            "deltas": [
                                {
                                    "from_serial": 4294967295,
                                    "to_serial": 0,
                                    "digest": "wrap",
                                }
                            ],
                        },
                        {
                            "name": "ns-b",
                            "deltas": [
                                {
                                    "from_serial": 4294967295,
                                    "to_serial": 0,
                                    "digest": "wrap",
                                }
                            ],
                        },
                    ],
                },
            ]
        }
    )

    target: int = Field(..., description="目标 32 位无符号 SOA 序列号")
    quorum: int = Field(
        ..., description="法定人数：完整三元组被多少个不同镜像上报才可信（正整数）",
    )
    mirrors: List[ConsensusMirror] = Field(..., min_length=1)

    @field_validator("target", mode="before")
    @classmethod
    def _target_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)

    @field_validator("quorum", mode="before")
    @classmethod
    def _quorum_positive(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("quorum 必须是正整数")
        if value < 1:
            raise ValueError("quorum 必须是正整数")
        return value


class ConsensusVoteDigest(BaseModel):
    """某个可信序号对下一个达到法定人数的内容摘要版本。"""

    digest: str
    votes: int = Field(..., ge=1, description="上报该完整三元组的不同镜像数")


class ConsensusEdge(BaseModel):
    """一条达到法定人数的可信三元组边。"""

    from_serial: int
    to_serial: int
    digest: str
    votes: int = Field(..., ge=1, description="上报该完整三元组的不同镜像数")
    voters: List[str] = Field(
        ..., description="上报该三元组的镜像名（按名称升序）",
    )


class ConsensusForkBranch(BaseModel):
    """可信分叉点处的一个目标分支（同一 to_serial 可有多个可信摘要版本）。"""

    to_serial: int
    digests: List[ConsensusVoteDigest] = Field(
        ..., description="该目标上达到法定人数的摘要版本（digest 升序）",
    )


class ConsensusFork(BaseModel):
    """可信集合中的分叉证据（不静默选边）。"""

    from_serial: int
    branches: List[ConsensusForkBranch] = Field(
        ..., description="全部可信出边（to_serial 升序；含同序号对多摘要版本）",
    )


class ConsensusLoopEdge(BaseModel):
    """可信环路中的一条边。"""

    from_serial: int
    to_serial: int
    digest: str
    votes: int


class MissingTrustedEdge(BaseModel):
    """镜像缺失的可信序号对（含该序号对上的全部可信摘要版本）。"""

    from_serial: int
    to_serial: int
    digests: List[ConsensusVoteDigest] = Field(
        ..., description="该可信序号对上达到法定人数的摘要版本（digest 升序）",
    )


class MinorityDigest(BaseModel):
    """镜像上报的少数摘要：序号对可信但该摘要未达法定人数。"""

    from_serial: int
    to_serial: int
    digest: str = Field(..., description="该镜像上报的少数摘要")
    votes: int = Field(..., ge=0, description="上报该少数三元组的镜像数")
    trusted_digests: List[ConsensusVoteDigest] = Field(
        ..., description="同一序号对上达到法定人数的摘要版本（digest 升序）",
    )


class ExtraConsensusEdge(BaseModel):
    """镜像上报的额外边：该完整三元组未达法定人数，且其序号对整体也不可信。"""

    from_serial: int
    to_serial: int
    digest: str
    votes: int = Field(..., ge=0, description="上报该三元组的镜像数")


class ConsensusMirrorEvidence(BaseModel):
    """单个镜像相对可信集合的偏差证据（顺序与请求中的镜像一致）。"""

    mirror: str
    reported_edges: int = Field(
        ..., ge=0, description="该镜像上报的去重三元组数量",
    )
    missing: List[MissingTrustedEdge] = Field(
        ...,
        description=(
            "该镜像未上报的可信序号对（按 (from_serial, to_serial) 升序）"
        ),
    )
    minority_digests: List[MinorityDigest] = Field(
        ...,
        description=(
            "少数摘要上报：序号对可信但该摘要未达法定人数"
            "（按 (from_serial, to_serial, digest) 升序）"
        ),
    )
    extra_edges: List[ExtraConsensusEdge] = Field(
        ...,
        description=(
            "额外边：任何不构成可信三元组的上报"
            "（按 (from_serial, to_serial, digest) 升序）"
        ),
    )


class DeltaConsensusResponse(BaseModel):
    """POST /delta-consensus 响应体。"""

    target: int
    quorum: int
    mirror_count: int = Field(..., ge=1)
    trusted_chain: List[ConsensusEdge] = Field(
        ...,
        description=(
            "最长可信链：从无入边的根节点沿唯一可信出边组装的最长连续链，"
            "按行进方向给出（长度并列时取边序列字典序最小者）"
        ),
    )
    chain_length: int = Field(..., ge=0, description="最长可信链的边数")
    chain_end: Optional[int] = Field(
        ..., description="最长可信链终点的序列号；无任何可信边时为 null",
    )
    chain_reason: ConsensusChainReason = Field(
        ...,
        description=(
            "reached：最长可信链终点恰为目标；gap：链在缺口处终止且未抵达目标；"
            "fork：可信集合存在分叉，链在分叉点终止（无可沿根组装的链时整体为 fork）；"
            "loop：可信集合存在环路，链在回环点终止（无可沿根组装的链时整体为 loop）"
        ),
    )
    target_reachable: bool = Field(
        ...,
        description=(
            "目标是否可达：当且仅当存在到目标的唯一连续可信链"
            "（最长可信链终点恰为目标；分叉 / 缺口 / 环路均为 false）"
        ),
    )
    trusted_edges: List[ConsensusEdge] = Field(
        ...,
        description="全部达到法定人数的可信三元组边（按 (from_serial, to_serial, digest) 升序）",
    )
    forks: List[ConsensusFork] = Field(
        ..., description="可信集合中的全部分叉（按 from_serial 升序）",
    )
    loops: List[List[ConsensusLoopEdge]] = Field(
        ...,
        description=(
            "可信集合中的全部环路（每条按行进方向、从最小 from_serial 起；"
            "环路之间按首边排序）"
        ),
    )
    mirrors: List[ConsensusMirrorEvidence] = Field(
        ..., description="逐镜像证据（顺序与请求中的镜像一致）",
    )
