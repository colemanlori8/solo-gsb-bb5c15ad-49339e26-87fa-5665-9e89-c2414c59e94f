"""POST /rollout-gate 上线门禁单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    MOD,
    AMBIGUOUS,
    BASELINE_NOT_REACHED,
    EQUAL,
    MISSING_TIME_ORDER,
    NEWER,
    OLDER,
    QUORUM_EXCEEDS_REPLICAS,
    QUORUM_NEVER_REACHED,
    ObservationOrderError,
    check_observation_time_order,
    evaluate_replica_rollout,
    evaluate_rollout_gate,
    sweep_quorum,
)


class CheckObservationTimeOrderTests(unittest.TestCase):
    def test_strictly_increasing_ok(self):
        check_observation_time_order("ns-a", [1, 2, 3])
        check_observation_time_order("ns-a", [0])
        check_observation_time_order("ns-a", [])

    def test_missing_timestamps_skipped(self):
        # None 不参与排序校验
        check_observation_time_order("ns-a", [None, 10, None, 20, None])
        check_observation_time_order("ns-a", [None, None])

    def test_duplicate_timestamp_rejected(self):
        with self.assertRaises(ObservationOrderError) as ctx:
            check_observation_time_order("ns-a", [10, 20, 20])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "duplicate_observation_timestamp")
        self.assertEqual(detail["replica"], "ns-a")
        self.assertEqual(detail["index"], 2)
        self.assertEqual(detail["timestamp"], 20)

    def test_out_of_order_timestamp_rejected(self):
        with self.assertRaises(ObservationOrderError) as ctx:
            check_observation_time_order("ns-b", [10, 30, 25])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "observation_timestamp_out_of_order")
        self.assertEqual(detail["replica"], "ns-b")
        self.assertEqual(detail["index"], 2)
        self.assertEqual(detail["timestamp"], 25)
        self.assertEqual(detail["previous_timestamp"], 30)

    def test_order_checked_ignoring_missing(self):
        # 缺失时间戳不掩盖已存在时间戳之间的倒序 / 重复
        with self.assertRaises(ObservationOrderError) as ctx:
            check_observation_time_order("ns-a", [10, None, 5])
        self.assertEqual(
            ctx.exception.detail["error"], "observation_timestamp_out_of_order"
        )
        with self.assertRaises(ObservationOrderError) as ctx:
            check_observation_time_order("ns-a", [10, None, 10])
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_observation_timestamp"
        )


class EvaluateReplicaRolloutTests(unittest.TestCase):
    def test_never_reaches_baseline(self):
        reaches, first_at, interval, reason, transitions = (
            evaluate_replica_rollout(100, [(1, 50), (2, 60)])
        )
        self.assertFalse(reaches)
        self.assertIsNone(first_at)
        self.assertIsNone(interval)
        self.assertEqual(reason, BASELINE_NOT_REACHED)
        self.assertEqual(transitions, [(1, NEWER)])

    def test_ambiguous_against_baseline_does_not_reach(self):
        # 与基线相差正好 2^31（正对点）不算达到
        reaches, _first_at, interval, reason, _t = evaluate_replica_rollout(
            0, [(1, HALF)]
        )
        self.assertFalse(reaches)
        self.assertIsNone(interval)
        self.assertEqual(reason, BASELINE_NOT_REACHED)

    def test_reaches_at_first_observation(self):
        reaches, first_at, interval, reason, transitions = (
            evaluate_replica_rollout(100, [(1, 100), (2, 101)])
        )
        self.assertTrue(reaches)
        self.assertEqual(first_at, 1)
        self.assertEqual(interval, (1, 2))
        self.assertIsNone(reason)
        self.assertEqual(transitions, [(1, NEWER)])

    def test_reaches_later_and_extends(self):
        reaches, first_at, interval, reason, _t = evaluate_replica_rollout(
            100, [(1, 90), (5, 100), (9, 105)]
        )
        self.assertTrue(reaches)
        self.assertEqual(first_at, 5)
        self.assertEqual(interval, (5, 9))
        self.assertIsNone(reason)

    def test_equal_transition_continues_interval(self):
        _r, _f, interval, _reason, transitions = evaluate_replica_rollout(
            100, [(1, 100), (2, 100), (3, 101)]
        )
        self.assertEqual(interval, (1, 3))
        self.assertEqual(transitions, [(1, EQUAL), (2, NEWER)])

    def test_older_transition_ends_interval(self):
        _r, _f, interval, _reason, transitions = evaluate_replica_rollout(
            100, [(1, 100), (2, 150), (3, 90)]
        )
        self.assertEqual(interval, (1, 2))
        self.assertEqual(transitions, [(1, NEWER), (2, OLDER)])

    def test_ambiguous_transition_ends_interval(self):
        _r, _f, interval, _reason, transitions = evaluate_replica_rollout(
            100, [(1, 100), (2, (100 + HALF) % MOD)]
        )
        self.assertEqual(interval, (1, 1))
        self.assertEqual(transitions, [(1, AMBIGUOUS)])

    def test_wrap_around_advancement_allowed(self):
        # 4294967294 -> 4294967295 -> 0 -> 1 是合法的连续推进（跨 2^32 回绕）
        baseline = MAX_SERIAL - 1
        reaches, first_at, interval, reason, transitions = (
            evaluate_replica_rollout(
                baseline,
                [
                    (1, MAX_SERIAL - 1),
                    (2, MAX_SERIAL),
                    (3, 0),
                    (4, 1),
                ],
            )
        )
        self.assertTrue(reaches)
        self.assertEqual(first_at, 1)
        self.assertEqual(interval, (1, 4))
        self.assertIsNone(reason)
        self.assertEqual(
            transitions, [(1, NEWER), (2, NEWER), (3, NEWER)]
        )

    def test_missing_timestamp_marks_and_breaks_interval(self):
        _r, _f, interval, _reason, transitions = evaluate_replica_rollout(
            100, [(1, 100), (None, 101), (3, 102)]
        )
        # 涉及缺失时间戳的两个相邻对都标记 missing_time_order
        self.assertEqual(
            transitions,
            [(1, MISSING_TIME_ORDER), (2, MISSING_TIME_ORDER)],
        )
        # 缺失时间顺序的相邻对终止区间：区间只覆盖首条观测
        self.assertEqual(interval, (1, 1))

    def test_first_reaching_observation_without_timestamp(self):
        reaches, first_at, interval, reason, _t = evaluate_replica_rollout(
            100, [(None, 100), (2, 101)]
        )
        self.assertTrue(reaches)
        self.assertIsNone(first_at)
        self.assertIsNone(interval)
        self.assertEqual(reason, MISSING_TIME_ORDER)

    def test_newer_step_can_fall_below_baseline(self):
        # 基线 10：10 -> 10+2^31-1 -> 8；第二步仍是 newer（距离 2^31-1），
        # 但 8 相对基线为 older，推进后跌破基线，区间终止
        _r, _f, interval, _reason, transitions = evaluate_replica_rollout(
            10, [(1, 10), (2, 10 + HALF - 1), (3, 8)]
        )
        self.assertEqual(interval, (1, 2))
        self.assertEqual(transitions, [(1, NEWER), (2, NEWER)])

    def test_single_reaching_observation_is_point_interval(self):
        reaches, first_at, interval, reason, transitions = (
            evaluate_replica_rollout(100, [(7, 100)])
        )
        self.assertTrue(reaches)
        self.assertEqual(first_at, 7)
        self.assertEqual(interval, (7, 7))
        self.assertIsNone(reason)
        self.assertEqual(transitions, [])

    def test_single_non_reaching_observation(self):
        reaches, _f, interval, reason, transitions = evaluate_replica_rollout(
            100, [(7, 99)]
        )
        self.assertFalse(reaches)
        self.assertIsNone(interval)
        self.assertEqual(reason, BASELINE_NOT_REACHED)
        self.assertEqual(transitions, [])


class SweepQuorumTests(unittest.TestCase):
    def test_no_intervals(self):
        self.assertEqual(sweep_quorum([], 1), (None, None, 0))

    def test_single_interval_never_drops(self):
        self.assertEqual(sweep_quorum([(5, 10)], 1), (5, None, 1))

    def test_quorum_never_met(self):
        self.assertEqual(sweep_quorum([(5, 10)], 2), (None, None, 1))

    def test_overlap_satisfies_then_drops(self):
        # A=[100,200]，B=[150,250]，quorum=2：150 满足，250 跌破
        first, dropped, peak = sweep_quorum([(100, 200), (150, 250)], 2)
        self.assertEqual((first, dropped, peak), (150, 250, 2))

    def test_closed_interval_endpoints_count(self):
        # 闭区间：A=[5,10] 与 B=[10,20] 在 t=10 同时计入
        first, dropped, peak = sweep_quorum([(5, 10), (10, 20)], 2)
        self.assertEqual((first, dropped, peak), (10, 20, 2))

    def test_point_intervals_aligning_satisfy(self):
        first, dropped, peak = sweep_quorum([(5, 5), (5, 5)], 2)
        self.assertEqual((first, dropped, peak), (5, None, 2))

    def test_only_first_episode_reported(self):
        # A=[1,10]，B=[2,3]，C=[4,5]，quorum=2：
        # t=2 满足；t=3、4、5 因闭区间端点仍为 2；t=10 首次跌破
        first, dropped, peak = sweep_quorum([(1, 10), (2, 3), (4, 5)], 2)
        self.assertEqual((first, dropped, peak), (2, 10, 2))

    def test_disjoint_intervals_peak(self):
        first, dropped, peak = sweep_quorum([(1, 2), (3, 4), (5, 6)], 2)
        self.assertEqual((first, dropped, peak), (None, None, 1))


class EvaluateRolloutGateTests(unittest.TestCase):
    def test_satisfied_window(self):
        results, satisfied, first_at, dropped, peak, reason = (
            evaluate_rollout_gate(
                100,
                2,
                [
                    [(10, 90), (20, 100), (30, 110)],   # 区间 [20, 30]
                    [(15, 100), (25, 100)],             # 区间 [15, 25]
                    [(40, 100)],                        # 区间 [40, 40]
                ],
            )
        )
        self.assertTrue(satisfied)
        self.assertEqual(first_at, 20)
        self.assertEqual(dropped, 30)
        self.assertEqual(peak, 2)
        self.assertIsNone(reason)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0][2], (20, 30))
        self.assertEqual(results[1][2], (15, 25))
        self.assertEqual(results[2][2], (40, 40))

    def test_quorum_exceeds_replicas(self):
        _r, satisfied, first_at, _d, peak, reason = evaluate_rollout_gate(
            100, 3, [[(1, 100)], [(2, 100)]]
        )
        self.assertFalse(satisfied)
        self.assertIsNone(first_at)
        self.assertEqual(peak, 1)
        self.assertEqual(reason, QUORUM_EXCEEDS_REPLICAS)

    def test_quorum_never_reached(self):
        _r, satisfied, first_at, dropped, peak, reason = evaluate_rollout_gate(
            100,
            2,
            [
                [(1, 100), (2, 101)],   # 区间 [1, 2]
                [(5, 100)],             # 区间 [5, 5]
                [(9, 50)],              # 从未达到
            ],
        )
        self.assertFalse(satisfied)
        self.assertIsNone(first_at)
        self.assertIsNone(dropped)
        self.assertEqual(peak, 1)
        self.assertEqual(reason, QUORUM_NEVER_REACHED)

    def test_replica_without_interval_excluded_from_sweep(self):
        # ns-b 首次达到的观测无时间戳 -> 无区间，不参与扫掠
        results, satisfied, first_at, _d, peak, _reason = evaluate_rollout_gate(
            100,
            2,
            [
                [(1, 100), (5, 101)],
                [(None, 100), (3, 101)],
            ],
        )
        self.assertFalse(satisfied)
        self.assertIsNone(first_at)
        self.assertEqual(peak, 1)
        self.assertEqual(results[1][3], MISSING_TIME_ORDER)


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException
    from pydantic import ValidationError as _ValidationError

    from app.main import app, rollout_gate
    from app.schemas import RolloutGateRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class RolloutGateEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return rollout_gate(RolloutGateRequest.model_validate(payload))

    def test_endpoint_happy_path(self):
        resp = self._request(
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
            }
        )
        self.assertTrue(resp.satisfied)
        self.assertEqual(resp.first_satisfied_at, 1727000060)
        self.assertEqual(resp.dropped_below_at, 1727000120)
        self.assertEqual(resp.peak_concurrent, 2)
        self.assertIsNone(resp.reason)
        self.assertEqual(
            [r.name for r in resp.replicas], ["ns-a", "ns-b", "ns-c"]
        )

        a, b, c = resp.replicas
        self.assertTrue(a.reaches_baseline)
        self.assertEqual(a.first_reached_at, 1727000060)
        self.assertEqual((a.interval.start, a.interval.end),
                         (1727000060, 1727000120))
        self.assertIsNone(a.reason)
        self.assertEqual(
            [(t.index, t.relation) for t in a.transitions],
            [(1, "newer"), (2, "newer")],
        )
        self.assertEqual(a.transitions[0].from_timestamp, 1727000000)
        self.assertEqual(a.transitions[0].to_timestamp, 1727000060)
        self.assertEqual(a.transitions[0].from_serial, 2024092100)
        self.assertEqual(a.transitions[0].to_serial, 2024092101)

        self.assertEqual((b.interval.start, b.interval.end),
                         (1727000030, 1727000090))
        self.assertEqual(
            [(t.index, t.relation) for t in b.transitions], [(1, "equal")]
        )

        self.assertEqual((c.interval.start, c.interval.end),
                         (1727000200, 1727000200))
        self.assertEqual(c.transitions, [])

    def test_endpoint_wrap_and_missing_time_order(self):
        resp = self._request(
            {
                "baseline": MAX_SERIAL - 1,
                "quorum": 2,
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [
                            {"timestamp": 1000, "serial": MAX_SERIAL - 1},
                            {"timestamp": 1010, "serial": MAX_SERIAL},
                            {"timestamp": 1020, "serial": 0},
                        ],
                    },
                    {
                        "name": "ns-b",
                        "observations": [
                            {"timestamp": 1005, "serial": MAX_SERIAL},
                            {"timestamp": None, "serial": 0},
                        ],
                    },
                ],
            }
        )
        # 回绕推进合法：ns-a 区间 [1000, 1020]；ns-b 因缺失时间顺序区间
        # 止于 [1005, 1005]
        a, b = resp.replicas
        self.assertEqual((a.interval.start, a.interval.end), (1000, 1020))
        self.assertEqual((b.interval.start, b.interval.end), (1005, 1005))
        self.assertEqual(
            [(t.index, t.relation) for t in b.transitions],
            [(1, "missing_time_order")],
        )
        self.assertIsNone(b.transitions[0].to_timestamp)
        self.assertTrue(resp.satisfied)
        self.assertEqual(resp.first_satisfied_at, 1005)
        self.assertEqual(resp.dropped_below_at, 1020)

    def test_endpoint_never_reached_reason(self):
        resp = self._request(
            {
                "baseline": 100,
                "quorum": 2,
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [{"timestamp": 1, "serial": 100}],
                    },
                    {
                        "name": "ns-b",
                        "observations": [{"timestamp": 2, "serial": 50}],
                    },
                ],
            }
        )
        self.assertFalse(resp.satisfied)
        self.assertIsNone(resp.first_satisfied_at)
        self.assertIsNone(resp.dropped_below_at)
        self.assertEqual(resp.reason, "quorum_never_reached")
        self.assertEqual(resp.peak_concurrent, 1)
        a, b = resp.replicas
        self.assertIsNone(a.reason)
        self.assertEqual(b.reason, "baseline_not_reached")
        self.assertIsNone(b.interval)
        self.assertFalse(b.reaches_baseline)

    def test_endpoint_quorum_exceeds_replicas(self):
        resp = self._request(
            {
                "baseline": 100,
                "quorum": 3,
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [{"timestamp": 1, "serial": 100}],
                    },
                    {
                        "name": "ns-b",
                        "observations": [{"timestamp": 1, "serial": 100}],
                    },
                ],
            }
        )
        self.assertFalse(resp.satisfied)
        self.assertEqual(resp.reason, "quorum_exceeds_replicas")
        self.assertEqual(resp.peak_concurrent, 2)

    def test_endpoint_first_reaching_observation_without_timestamp(self):
        resp = self._request(
            {
                "baseline": 100,
                "quorum": 1,
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [
                            {"timestamp": None, "serial": 100},
                            {"timestamp": 5, "serial": 101},
                        ],
                    },
                ],
            }
        )
        item = resp.replicas[0]
        self.assertTrue(item.reaches_baseline)
        self.assertIsNone(item.first_reached_at)
        self.assertIsNone(item.interval)
        self.assertEqual(item.reason, "missing_time_order")
        self.assertFalse(resp.satisfied)
        self.assertEqual(resp.reason, "quorum_never_reached")

    def test_endpoint_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "baseline": 100,
                    "quorum": 1,
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [{"timestamp": 1, "serial": 100}],
                        },
                        {
                            "name": "ns-a",
                            "observations": [{"timestamp": 2, "serial": 100}],
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_duplicate_timestamp_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "baseline": 100,
                    "quorum": 1,
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 10, "serial": 99},
                                {"timestamp": 10, "serial": 100},
                            ],
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_observation_timestamp"
        )
        self.assertEqual(ctx.exception.detail["replica"], "ns-a")

    def test_endpoint_out_of_order_timestamp_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "baseline": 100,
                    "quorum": 1,
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 20, "serial": 99},
                                {"timestamp": 10, "serial": 100},
                            ],
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "observation_timestamp_out_of_order"
        )

    def test_endpoint_same_timestamp_across_replicas_ok(self):
        # 只校验同一副本内的时间戳，副本之间允许相同
        resp = self._request(
            {
                "baseline": 100,
                "quorum": 2,
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [{"timestamp": 7, "serial": 100}],
                    },
                    {
                        "name": "ns-b",
                        "observations": [{"timestamp": 7, "serial": 100}],
                    },
                ],
            }
        )
        self.assertTrue(resp.satisfied)
        self.assertEqual(resp.first_satisfied_at, 7)

    def test_request_validation_422(self):
        base = {
            "baseline": 100,
            "quorum": 1,
            "replicas": [
                {"name": "ns-a",
                 "observations": [{"timestamp": 1, "serial": 100}]},
            ],
        }

        def mutated(**changes):
            import copy

            payload = copy.deepcopy(base)
            for key, value in changes.items():
                payload[key] = value
            return payload

        invalid_payloads = [
            mutated(baseline=-1),
            mutated(baseline=2**32),
            mutated(baseline=True),
            mutated(baseline="100"),
            mutated(quorum=0),
            mutated(quorum=-1),
            mutated(quorum=True),
            mutated(quorum="2"),
            mutated(quorum=1.5),
            mutated(replicas=[]),
            mutated(
                replicas=[{"name": "ns-a", "observations": []}],
            ),
            mutated(
                replicas=[{"name": "  ",
                            "observations": [{"timestamp": 1, "serial": 1}]}],
            ),
            mutated(
                replicas=[{"name": "ns-a",
                           "observations": [{"timestamp": -1, "serial": 1}]}],
            ),
            mutated(
                replicas=[{"name": "ns-a",
                           "observations": [{"timestamp": 1.5, "serial": 1}]}],
            ),
            mutated(
                replicas=[{"name": "ns-a",
                           "observations": [{"timestamp": True, "serial": 1}]}],
            ),
            mutated(
                replicas=[{"name": "ns-a",
                           "observations": [{"timestamp": 1, "serial": -1}]}],
            ),
            mutated(
                replicas=[{"name": "ns-a",
                           "observations": [{"timestamp": 1,
                                             "serial": 2**32}]}],
            ),
        ]
        for payload in invalid_payloads:
            with self.assertRaises(
                _ValidationError, msg=f"payload={payload}"
            ):
                RolloutGateRequest.model_validate(payload)

    def test_openapi_lists_rollout_gate_with_examples(self):
        schema = app.openapi()
        self.assertIn("/rollout-gate", schema["paths"])
        self.assertIn("post", schema["paths"]["/rollout-gate"])
        request_schema = schema["components"]["schemas"]["RolloutGateRequest"]
        self.assertIn("examples", request_schema)
        self.assertGreaterEqual(len(request_schema["examples"]), 1)
        # 示例本身必须能通过请求模型校验
        for example in request_schema["examples"]:
            RolloutGateRequest.model_validate(example)


if __name__ == "__main__":
    unittest.main()
