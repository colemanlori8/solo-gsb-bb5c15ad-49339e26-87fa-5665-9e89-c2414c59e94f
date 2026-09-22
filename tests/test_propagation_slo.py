"""POST /propagation-slo 传播 SLO 单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    MOD,
    DUPLICATE_RELEASE_TIMESTAMP,
    RELEASE_TIMESTAMP_OUT_OF_ORDER,
    ReleaseOrderError,
    check_release_time_order,
    evaluate_propagation_slo,
    evaluate_release_window,
)


def _window(serial, released_at, window_end, replicas, domains=None):
    """便捷封装：domains 缺省时按副本中的故障域升序推导。"""
    if domains is None:
        domains = sorted({domain for _name, domain, _obs in replicas})
    return evaluate_release_window(
        serial, released_at, window_end, domains, replicas
    )


class CheckReleaseTimeOrderTests(unittest.TestCase):
    def test_strictly_increasing_ok(self):
        check_release_time_order([1, 2, 3])
        check_release_time_order([0])
        check_release_time_order([])

    def test_duplicate_release_timestamp_rejected(self):
        with self.assertRaises(ReleaseOrderError) as ctx:
            check_release_time_order([10, 20, 20])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], DUPLICATE_RELEASE_TIMESTAMP)
        self.assertEqual(detail["index"], 2)
        self.assertEqual(detail["timestamp"], 20)

    def test_out_of_order_release_timestamp_rejected(self):
        with self.assertRaises(ReleaseOrderError) as ctx:
            check_release_time_order([10, 30, 25])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], RELEASE_TIMESTAMP_OUT_OF_ORDER)
        self.assertEqual(detail["index"], 2)
        self.assertEqual(detail["timestamp"], 25)
        self.assertEqual(detail["previous_timestamp"], 30)


class EvaluateReleaseWindowTests(unittest.TestCase):
    def test_basic_convergence_is_latest_domain_arrival(self):
        converged, status, missing = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(110, 100)]),
                ("ns-b", "dc-west", [(120, 100)]),
            ],
        )
        self.assertEqual(converged, 120)
        self.assertEqual(status, [("dc-east", 110), ("dc-west", 120)])
        self.assertEqual(missing, [])

    def test_newer_observation_counts_as_reach(self):
        # 观测值严格新于发布版本（含跨 2^32 回绕）同样算到达
        converged, status, _m = _window(
            100, 100, None, [("ns-a", "dc-east", [(105, 150)])]
        )
        self.assertEqual(converged, 105)
        self.assertEqual(status, [("dc-east", 105)])

    def test_older_observation_does_not_reach(self):
        converged, status, missing = _window(
            100, 100, None, [("ns-a", "dc-east", [(105, 99)])]
        )
        self.assertIsNone(converged)
        self.assertEqual(status, [("dc-east", None)])
        self.assertEqual(missing, ["dc-east"])

    def test_ambiguous_observation_does_not_reach(self):
        # 与发布版本相差正好 2^31（正对点）不算到达
        converged, status, missing = _window(
            100, 100, None, [("ns-a", "dc-east", [(105, (100 + HALF) % MOD)])]
        )
        self.assertIsNone(converged)
        self.assertEqual(status, [("dc-east", None)])
        self.assertEqual(missing, ["dc-east"])

    def test_domain_reached_at_is_earliest_replica_arrival(self):
        converged, status, _m = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(130, 100)]),
                ("ns-b", "dc-east", [(110, 100)]),
                ("ns-c", "dc-west", [(120, 100)]),
            ],
        )
        self.assertEqual(converged, 120)
        self.assertEqual(status, [("dc-east", 110), ("dc-west", 120)])

    def test_observations_outside_window_ignored(self):
        # 窗口 [100, 200)：早于 100 与 >= 200 的观测都不参与
        converged, status, missing = _window(
            100,
            100,
            200,
            [
                ("ns-a", "dc-east", [(50, 100), (150, 100)]),
                ("ns-b", "dc-west", [(200, 100)]),
            ],
        )
        self.assertIsNone(converged)
        self.assertEqual(status, [("dc-east", 150), ("dc-west", None)])
        self.assertEqual(missing, ["dc-west"])

    def test_observation_at_release_time_counts(self):
        # 窗口左闭：发布时间点的观测计入，latency 可为 0
        converged, status, _m = _window(
            100, 100, None, [("ns-a", "dc-east", [(100, 100)])]
        )
        self.assertEqual(converged, 100)
        self.assertEqual(status, [("dc-east", 100)])

    def test_regress_from_arrived_replica_invalidates_domain(self):
        # ns-a 到达后在收敛前回退（older）：dc-east 到达状态失效，须重新到达
        converged, status, missing = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(110, 100), (120, 99), (150, 100)]),
                ("ns-b", "dc-west", [(130, 100)]),
            ],
        )
        self.assertEqual(converged, 150)
        self.assertEqual(status, [("dc-east", 150), ("dc-west", 130)])
        self.assertEqual(missing, [])

    def test_ambiguous_regress_invalidates_domain(self):
        # 已到达副本在收敛前出现 ambiguous 同样使该域失效
        converged, status, _m = _window(
            100,
            100,
            None,
            [
                (
                    "ns-a",
                    "dc-east",
                    [(110, 100), (120, (100 + HALF) % MOD), (140, 100)],
                ),
                ("ns-b", "dc-west", [(150, 100)]),
            ],
        )
        self.assertEqual(converged, 150)
        self.assertEqual(status, [("dc-east", 140), ("dc-west", 150)])

    def test_regress_from_non_arrived_replica_no_effect(self):
        # 未到达副本的 older 观测只是普通落后，不影响域状态
        converged, status, _m = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(105, 99), (110, 100)]),
                ("ns-b", "dc-west", [(120, 100)]),
            ],
        )
        self.assertEqual(converged, 120)
        self.assertEqual(status, [("dc-east", 110), ("dc-west", 120)])

    def test_repeated_regress_invalidates_only_once(self):
        # 副本回退后不再是已到达状态，持续落后不会反复失效
        converged, status, _m = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(110, 100), (120, 99), (130, 98)]),
                ("ns-b", "dc-west", [(140, 100)]),
                ("ns-c", "dc-east", [(150, 100)]),
            ],
        )
        # dc-east 在 120 失效后由 ns-c 在 150 重新到达
        self.assertEqual(converged, 150)
        self.assertEqual(status, [("dc-east", 150), ("dc-west", 140)])

    def test_invalidation_requires_fresh_observation(self):
        # 域失效后，同域其他副本仍处于已到达状态并不能使域自动恢复，
        # 必须等待新的到达观测
        converged, status, missing = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(110, 100), (120, 99)]),
                ("ns-b", "dc-east", [(115, 100)]),
                ("ns-c", "dc-west", [(130, 100)]),
            ],
        )
        self.assertIsNone(converged)
        self.assertEqual(status, [("dc-east", None), ("dc-west", 130)])
        self.assertEqual(missing, ["dc-east"])

    def test_same_timestamp_regress_processed_before_reach(self):
        # 同一时间戳先处理回退再处理到达：同时刻的到达仍然有效
        converged, status, _m = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(110, 100), (140, 99)]),
                ("ns-b", "dc-east", [(140, 100)]),
                ("ns-c", "dc-west", [(150, 100)]),
            ],
        )
        self.assertEqual(converged, 150)
        self.assertEqual(status, [("dc-east", 140), ("dc-west", 150)])

    def test_regress_after_convergence_ignored(self):
        # 收敛之后的回退不再影响结果
        converged, status, missing = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", [(110, 100), (900, 99)]),
                ("ns-b", "dc-west", [(120, 100)]),
            ],
        )
        self.assertEqual(converged, 120)
        self.assertEqual(status, [("dc-east", 110), ("dc-west", 120)])
        self.assertEqual(missing, [])

    def test_wraparound_serials(self):
        # 跨 2^32 回绕：0 相对 4294967295 是 newer（距离 1）
        converged, status, _m = _window(
            MAX_SERIAL,
            0,
            None,
            [
                ("ns-a", "dc-east", [(10, 0)]),
                ("ns-b", "dc-west", [(20, MAX_SERIAL)]),
            ],
        )
        self.assertEqual(converged, 20)
        self.assertEqual(status, [("dc-east", 10), ("dc-west", 20)])

    def test_empty_observations_domain_never_arrives(self):
        converged, status, missing = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-east", []),
                ("ns-b", "dc-west", [(110, 100)]),
            ],
        )
        self.assertIsNone(converged)
        self.assertEqual(status, [("dc-east", None), ("dc-west", 110)])
        self.assertEqual(missing, ["dc-east"])

    def test_missing_domains_sorted(self):
        converged, status, missing = _window(
            100,
            100,
            None,
            [
                ("ns-a", "dc-zulu", [(110, 99)]),
                ("ns-b", "dc-alpha", [(110, 99)]),
                ("ns-c", "dc-mid", [(110, 100)]),
            ],
        )
        self.assertIsNone(converged)
        self.assertEqual(
            status,
            [("dc-alpha", None), ("dc-mid", 110), ("dc-zulu", None)],
        )
        self.assertEqual(missing, ["dc-alpha", "dc-zulu"])


class EvaluatePropagationSloTests(unittest.TestCase):
    def test_multiple_releases_independent_windows(self):
        results = evaluate_propagation_slo(
            [(100, 1000), (101, 2000)],
            [
                ("ns-a", "dc-east", [(1100, 100), (2100, 101)]),
                ("ns-b", "dc-west", [(1200, 100), (2050, 100), (2200, 101)]),
            ],
            200,
        )
        self.assertEqual(len(results), 2)

        serial, released_at, converged, latency, met, status, missing = results[0]
        self.assertEqual((serial, released_at), (100, 1000))
        self.assertEqual(converged, 1200)
        self.assertEqual(latency, 200)
        self.assertTrue(met)  # latency == deadline 边界：达标
        self.assertEqual(status, [("dc-east", 1100), ("dc-west", 1200)])
        self.assertEqual(missing, [])

        serial, released_at, converged, latency, met, status, missing = results[1]
        self.assertEqual((serial, released_at), (101, 2000))
        # 窗口 [2000, +∞)：ns-b 在 2050 的旧版本观测（older）不影响，
        # 2200 到达 101；ns-a 在 2100 到达
        self.assertEqual(converged, 2200)
        self.assertEqual(latency, 200)
        self.assertTrue(met)
        self.assertEqual(status, [("dc-east", 2100), ("dc-west", 2200)])
        self.assertEqual(missing, [])

    def test_state_resets_between_windows(self):
        # 窗口 1 的到达状态不带入窗口 2
        results = evaluate_propagation_slo(
            [(100, 1000), (101, 2000)],
            [("ns-a", "dc-east", [(1100, 100)])],
            300,
        )
        first, second = results
        self.assertEqual(first[2], 1100)  # converged_at
        self.assertEqual(first[3], 100)  # latency_seconds
        self.assertTrue(first[4])  # met_deadline
        self.assertIsNone(second[2])
        self.assertIsNone(second[3])
        self.assertFalse(second[4])
        self.assertEqual(second[6], ["dc-east"])

    def test_observation_at_next_release_belongs_to_next_window(self):
        results = evaluate_propagation_slo(
            [(100, 1000), (101, 2000)],
            [
                ("ns-a", "dc-east", [(1500, 100)]),
                ("ns-b", "dc-west", [(2000, 101)]),
            ],
            300,
        )
        first, second = results
        # 窗口 [1000, 2000)：ns-b 在 2000 的观测不属于本窗口
        self.assertIsNone(first[2])
        self.assertEqual(first[5], [("dc-east", 1500), ("dc-west", None)])
        self.assertEqual(first[6], ["dc-west"])
        # 窗口 [2000, +∞)：ns-a 无观测
        self.assertIsNone(second[2])
        self.assertEqual(second[5], [("dc-east", None), ("dc-west", 2000)])
        self.assertEqual(second[6], ["dc-east"])

    def test_last_release_window_unbounded(self):
        results = evaluate_propagation_slo(
            [(100, 1000)],
            [("ns-a", "dc-east", [(5000, 100)])],
            3000,
        )
        (_serial, _at, converged, latency, met, _status, missing) = results[0]
        self.assertEqual(converged, 5000)
        self.assertEqual(latency, 4000)
        self.assertFalse(met)  # 4000 > 3000
        self.assertEqual(missing, [])

    def test_met_deadline_boundary(self):
        replicas = [("ns-a", "dc-east", [(1100, 100)])]
        (met_at_boundary,) = evaluate_propagation_slo(
            [(100, 1000)], replicas, 100
        )
        self.assertTrue(met_at_boundary[4])  # latency 100 <= 100
        (missed,) = evaluate_propagation_slo([(100, 1000)], replicas, 99)
        self.assertFalse(missed[4])

    def test_zero_latency_meets_zero_deadline(self):
        (result,) = evaluate_propagation_slo(
            [(100, 1000)],
            [("ns-a", "dc-east", [(1000, 100)])],
            0,
        )
        self.assertEqual(result[2], 1000)
        self.assertEqual(result[3], 0)
        self.assertTrue(result[4])

    def test_not_converged_outputs(self):
        (result,) = evaluate_propagation_slo(
            [(100, 1000)],
            [
                ("ns-a", "dc-east", [(1100, 100)]),
                ("ns-b", "dc-west", [(1100, 99)]),
            ],
            300,
        )
        serial, released_at, converged, latency, met, status, missing = result
        self.assertEqual((serial, released_at), (100, 1000))
        self.assertIsNone(converged)
        self.assertIsNone(latency)
        self.assertFalse(met)
        self.assertEqual(status, [("dc-east", 1100), ("dc-west", None)])
        self.assertEqual(missing, ["dc-west"])

    def test_readme_example(self):
        results = evaluate_propagation_slo(
            [(2024092101, 1000), (2024092102, 2000)],
            [
                (
                    "ns-a",
                    "dc-east",
                    [(1000, 2024092100), (1120, 2024092101), (2050, 2024092102)],
                ),
                ("ns-b", "dc-west", [(1180, 2024092101), (1900, 2024092101)]),
                ("ns-c", "dc-west", [(1250, 2024092101)]),
            ],
            300,
        )
        first, second = results
        # 第一次发布：dc-east 1120、dc-west 1180 到达，converged 1180，latency 180
        self.assertEqual(first[2], 1180)
        self.assertEqual(first[3], 180)
        self.assertTrue(first[4])
        self.assertEqual(first[5], [("dc-east", 1120), ("dc-west", 1180)])
        self.assertEqual(first[6], [])
        # 第二次发布：dc-west 在窗口 [2000, +∞) 内无任何观测
        self.assertIsNone(second[2])
        self.assertIsNone(second[3])
        self.assertFalse(second[4])
        self.assertEqual(second[5], [("dc-east", 2050), ("dc-west", None)])
        self.assertEqual(second[6], ["dc-west"])


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException
    from pydantic import ValidationError as _ValidationError

    from app.main import app, propagation_slo
    from app.schemas import PropagationSloRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class PropagationSloEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return propagation_slo(PropagationSloRequest.model_validate(payload))

    def test_endpoint_happy_path(self):
        resp = self._request(
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
            }
        )
        self.assertEqual(resp.deadline_seconds, 300)
        self.assertEqual(len(resp.releases), 2)

        first, second = resp.releases
        self.assertEqual((first.serial, first.released_at), (2024092101, 1000))
        self.assertEqual(first.converged_at, 1180)
        self.assertEqual(first.latency_seconds, 180)
        self.assertTrue(first.met_deadline)
        self.assertEqual(
            [(d.failure_domain, d.reached_at) for d in first.domains],
            [("dc-east", 1120), ("dc-west", 1180)],
        )
        self.assertEqual(first.missing_domains, [])

        self.assertEqual((second.serial, second.released_at), (2024092102, 2000))
        self.assertIsNone(second.converged_at)
        self.assertIsNone(second.latency_seconds)
        self.assertFalse(second.met_deadline)
        self.assertEqual(
            [(d.failure_domain, d.reached_at) for d in second.domains],
            [("dc-east", 2050), ("dc-west", None)],
        )
        self.assertEqual(second.missing_domains, ["dc-west"])

    def test_endpoint_invalidation_flow(self):
        resp = self._request(
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
            }
        )
        (release,) = resp.releases
        # dc-east 在 10 到达、20 回退失效、50 重新到达；dc-west 30 到达
        self.assertEqual(release.converged_at, 50)
        self.assertEqual(release.latency_seconds, 50)
        self.assertTrue(release.met_deadline)
        self.assertEqual(
            [(d.failure_domain, d.reached_at) for d in release.domains],
            [("dc-east", 50), ("dc-west", 30)],
        )
        self.assertEqual(release.missing_domains, [])

    def test_endpoint_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "deadline_seconds": 60,
                    "releases": [{"serial": 1, "released_at": 0}],
                    "replicas": [
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-east",
                            "observations": [{"timestamp": 1, "serial": 1}],
                        },
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-west",
                            "observations": [{"timestamp": 2, "serial": 1}],
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_duplicate_release_timestamp_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "deadline_seconds": 60,
                    "releases": [
                        {"serial": 1, "released_at": 100},
                        {"serial": 2, "released_at": 100},
                    ],
                    "replicas": [
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-east",
                            "observations": [{"timestamp": 1, "serial": 1}],
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_release_timestamp"
        )

    def test_endpoint_out_of_order_release_timestamp_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "deadline_seconds": 60,
                    "releases": [
                        {"serial": 1, "released_at": 200},
                        {"serial": 2, "released_at": 100},
                    ],
                    "replicas": [
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-east",
                            "observations": [{"timestamp": 1, "serial": 1}],
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "release_timestamp_out_of_order"
        )

    def test_endpoint_duplicate_observation_timestamp_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "deadline_seconds": 60,
                    "releases": [{"serial": 1, "released_at": 0}],
                    "replicas": [
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-east",
                            "observations": [
                                {"timestamp": 10, "serial": 1},
                                {"timestamp": 10, "serial": 1},
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

    def test_endpoint_out_of_order_observation_timestamp_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "deadline_seconds": 60,
                    "releases": [{"serial": 1, "released_at": 0}],
                    "replicas": [
                        {
                            "name": "ns-a",
                            "failure_domain": "dc-east",
                            "observations": [
                                {"timestamp": 20, "serial": 1},
                                {"timestamp": 10, "serial": 1},
                            ],
                        },
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "observation_timestamp_out_of_order"
        )

    def test_request_validation_422(self):
        base = {
            "deadline_seconds": 60,
            "releases": [{"serial": 1, "released_at": 0}],
            "replicas": [
                {
                    "name": "ns-a",
                    "failure_domain": "dc-east",
                    "observations": [{"timestamp": 1, "serial": 1}],
                },
            ],
        }

        def mutated(**changes):
            import copy

            payload = copy.deepcopy(base)
            for key, value in changes.items():
                payload[key] = value
            return payload

        def mutated_replica(**replica_changes):
            import copy

            payload = copy.deepcopy(base)
            payload["replicas"] = [
                dict(payload["replicas"][0], **replica_changes)
            ]
            return payload

        invalid_payloads = [
            mutated(deadline_seconds=-1),
            mutated(deadline_seconds=True),
            mutated(deadline_seconds="60"),
            mutated(deadline_seconds=1.5),
            mutated(releases=[]),
            mutated(releases=[{"serial": -1, "released_at": 0}]),
            mutated(releases=[{"serial": 2**32, "released_at": 0}]),
            mutated(releases=[{"serial": True, "released_at": 0}]),
            mutated(releases=[{"serial": 1, "released_at": -1}]),
            mutated(releases=[{"serial": 1, "released_at": 1.5}]),
            mutated(releases=[{"serial": 1}]),
            mutated(replicas=[]),
            mutated_replica(name="  "),
            mutated_replica(failure_domain=""),
            mutated_replica(failure_domain="   "),
            mutated_replica(
                observations=[{"timestamp": -1, "serial": 1}],
            ),
            mutated_replica(
                observations=[{"timestamp": 1.5, "serial": 1}],
            ),
            mutated_replica(
                observations=[{"timestamp": True, "serial": 1}],
            ),
            mutated_replica(
                observations=[{"timestamp": None, "serial": 1}],
            ),
            mutated_replica(
                observations=[{"serial": 1}],
            ),
            mutated_replica(
                observations=[{"timestamp": 1, "serial": -1}],
            ),
            mutated_replica(
                observations=[{"timestamp": 1, "serial": 2**32}],
            ),
        ]
        for payload in invalid_payloads:
            with self.assertRaises(
                _ValidationError, msg=f"payload={payload}"
            ):
                PropagationSloRequest.model_validate(payload)

    def test_openapi_lists_propagation_slo_with_examples(self):
        schema = app.openapi()
        self.assertIn("/propagation-slo", schema["paths"])
        self.assertIn("post", schema["paths"]["/propagation-slo"])
        request_schema = schema["components"]["schemas"]["PropagationSloRequest"]
        self.assertIn("examples", request_schema)
        self.assertGreaterEqual(len(request_schema["examples"]), 1)
        # 示例本身必须能通过请求模型校验
        for example in request_schema["examples"]:
            PropagationSloRequest.model_validate(example)


if __name__ == "__main__":
    unittest.main()
