"""POST /divergence-windows 分裂窗口单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    OBSERVATION_SERIAL_CONFLICT,
    ObservationOrderError,
    check_divergence_observation_order,
    evaluate_divergence_windows,
    has_common_successor,
)


class HasCommonSuccessorTests(unittest.TestCase):
    def test_equal_values_feasible(self):
        self.assertTrue(has_common_successor([100, 100]))

    def test_forward_spread_within_half_feasible(self):
        # 前向跨度恰为 2^31 - 2：仍有唯一共同后继附近的候选
        self.assertTrue(has_common_successor([0, HALF - 2]))

    def test_forward_spread_half_minus_one_infeasible(self):
        # 前向跨度 2^31 - 1：开弧 (0, 2^31) 与 (2^31-1, 2^32-1) 交集为空
        self.assertFalse(has_common_successor([0, HALF - 1]))

    def test_antipodal_pair_infeasible(self):
        self.assertFalse(has_common_successor([0, HALF]))

    def test_integer_range_misleading_wrap_feasible(self):
        # 整数极差接近 2^32，但前向距离只有 16：存在共同后继
        self.assertTrue(has_common_successor([MAX_SERIAL - 5, 10]))

    def test_integer_range_misleading_non_wrap_infeasible(self):
        # 整数极差只有 2^31 - 1（远小于 2^32），却已无候选
        self.assertFalse(has_common_successor([10, 10 + HALF - 1]))

    def test_single_value_always_feasible(self):
        self.assertTrue(has_common_successor([0]))
        self.assertTrue(has_common_successor([MAX_SERIAL]))

    def test_three_replicas_span_over_half_infeasible(self):
        self.assertFalse(has_common_successor([0, HALF // 2, HALF - 1]))
        self.assertTrue(has_common_successor([0, HALF // 2, HALF - 2]))


class CheckDivergenceObservationOrderTests(unittest.TestCase):
    def test_non_decreasing_ok(self):
        check_divergence_observation_order("ns-a", [(1, 10), (2, 11)])
        check_divergence_observation_order("ns-a", [])
        check_divergence_observation_order("ns-a", [(5, 9)])

    def test_same_timestamp_same_serial_allowed(self):
        # 同刻同值的重复观测允许（后续按去重处理）
        check_divergence_observation_order(
            "ns-a", [(10, 100), (10, 100), (20, 101)]
        )

    def test_out_of_order_rejected(self):
        with self.assertRaises(ObservationOrderError) as ctx:
            check_divergence_observation_order("ns-b", [(10, 1), (5, 2)])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "observation_timestamp_out_of_order")
        self.assertEqual(detail["replica"], "ns-b")
        self.assertEqual(detail["index"], 1)
        self.assertEqual(detail["timestamp"], 5)
        self.assertEqual(detail["previous_timestamp"], 10)

    def test_same_timestamp_different_serial_rejected(self):
        with self.assertRaises(ObservationOrderError) as ctx:
            check_divergence_observation_order(
                "ns-c", [(10, 100), (10, 101)]
            )
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], OBSERVATION_SERIAL_CONFLICT)
        self.assertEqual(detail["replica"], "ns-c")
        self.assertEqual(detail["index"], 1)
        self.assertEqual(detail["timestamp"], 10)
        self.assertEqual(detail["serials"], [100, 101])

    def test_conflict_only_compared_against_adjacent_same_ts(self):
        # 同刻的冲突观测被同值重复“隔开”时，每对相邻比较仍能捕获
        with self.assertRaises(ObservationOrderError) as ctx:
            check_divergence_observation_order(
                "ns-a", [(10, 1), (10, 1), (10, 2)]
            )
        self.assertEqual(
            ctx.exception.detail["error"], OBSERVATION_SERIAL_CONFLICT
        )


class EvaluateDivergenceWindowsTests(unittest.TestCase):
    @staticmethod
    def _plain(outcomes):
        """把 entries 中的副本下标转换为便于断言的形态。"""
        return [
            (start, end, duration, list(entries))
            for start, end, duration, entries in outcomes
        ]

    def test_no_divergence_returns_empty(self):
        outcomes = evaluate_divergence_windows(
            [[(0, 100), (10, 200)], [(5, 150), (10, 200)]]
        )
        self.assertEqual(outcomes, [])

    def test_single_replica_never_diverges(self):
        outcomes = evaluate_divergence_windows(
            [[(0, 0), (10, HALF - 1)]]
        )
        self.assertEqual(outcomes, [])

    def test_basic_window_opens_and_recovers(self):
        # t=0 只有 a=100（单副本可行）；t=10 b=100 同值可行；
        # t=20 b 推进到正对点 100+2^31，无共同候选 -> 开窗；
        # t=50 两者同为 200 -> 恢复，闭窗
        outcomes = evaluate_divergence_windows(
            [
                [(0, 100), (20, 100), (50, 200)],
                [(10, 100), (20, (100 + HALF) % (1 << 32)), (50, 200)],
            ]
        )
        self.assertEqual(len(outcomes), 1)
        start, end, duration, entries = outcomes[0]
        self.assertEqual((start, end, duration), (20, 50, 30))
        self.assertEqual(entries, [(0, 100), (1, (100 + HALF) % (1 << 32))])

    def test_unrecovered_window_has_null_end(self):
        outcomes = evaluate_divergence_windows(
            [[(0, 0)], [(10, HALF)]]
        )
        self.assertEqual(len(outcomes), 1)
        start, end, duration, entries = outcomes[0]
        self.assertEqual(start, 10)
        self.assertIsNone(end)
        self.assertIsNone(duration)
        self.assertEqual(entries, [(0, 0), (1, HALF)])

    def test_contiguous_bad_states_merge(self):
        # 连续多个无候选时间戳合并为一个窗口，end 取首个可行状态
        outcomes = evaluate_divergence_windows(
            [
                [(0, 0)],
                [(0, HALF), (10, HALF - 1), (20, 0)],
            ]
        )
        self.assertEqual(len(outcomes), 1)
        start, end, duration, _entries = outcomes[0]
        self.assertEqual(start, 0)
        self.assertEqual(end, 20)
        self.assertEqual(duration, 20)

    def test_recovery_splits_into_two_windows(self):
        outcomes = evaluate_divergence_windows(
            [
                [(0, 0), (10, 0), (20, 0), (30, 0)],
                [
                    (0, HALF),
                    (10, 0),
                    (20, HALF),
                    (30, 0),
                ],
            ]
        )
        self.assertEqual(len(outcomes), 2)
        (s1, e1, d1, _en1), (s2, e2, d2, _en2) = outcomes
        self.assertEqual((s1, e1, d1), (0, 10, 10))
        self.assertEqual((s2, e2, d2), (20, 30, 10))

    def test_atomic_merge_same_timestamp(self):
        # t=10：a 与 b 在同一时刻更新，原子合并后为 100/100，可行——
        # 即使“先应用 a”会与 b 的旧值成正对点也不产生窗口
        outcomes = evaluate_divergence_windows(
            [
                [(0, HALF), (10, 100)],
                [(0, HALF), (10, 100)],
            ]
        )
        self.assertEqual(outcomes, [])

    def test_entries_snapshot_latest_values_at_open(self):
        # 开窗时 entries 取各副本当时携带的最新值（不是观测初值）
        outcomes = evaluate_divergence_windows(
            [
                [(0, 1), (5, 7), (20, 7), (30, 100)],
                [(10, 7 + HALF - 1), (20, 7 + HALF - 1), (30, 100)],
            ]
        )
        self.assertEqual(len(outcomes), 1)
        start, end, duration, entries = outcomes[0]
        self.assertEqual((start, end, duration), (10, 30, 20))
        self.assertEqual(entries, [(0, 7), (1, 7 + HALF - 1)])

    def test_late_replica_enters_open_window(self):
        # t=0 单副本 a 可行；t=10 a 与“自己将来”不涉及——构造：
        # t=10 a=0, b=HALF 开窗（此时只有 a、b 已知）；
        # t=20 新副本 c 首次观测（此时 a 仍 0、b 仍 HALF），c 加入 entries；
        # t=30 三方一致，闭窗
        outcomes = evaluate_divergence_windows(
            [
                [(0, 100), (10, 0), (30, 5)],
                [(10, HALF), (30, 5)],
                [(20, 99), (30, 5)],
            ]
        )
        self.assertEqual(len(outcomes), 1)
        start, end, duration, entries = outcomes[0]
        self.assertEqual((start, end, duration), (10, 30, 20))
        self.assertEqual(entries, [(0, 0), (1, HALF), (2, 99)])

    def test_late_replica_not_added_after_close(self):
        # 窗口在 t=20 关闭；t=30 才出现的副本不进入已关闭窗口
        outcomes = evaluate_divergence_windows(
            [
                [(0, 0), (20, 0)],
                [(0, HALF), (20, 0)],
                [(30, 42)],
            ]
        )
        self.assertEqual(len(outcomes), 1)
        _start, _end, _duration, entries = outcomes[0]
        self.assertEqual(entries, [(0, 0), (1, HALF)])

    def test_replica_without_observations_never_known(self):
        outcomes = evaluate_divergence_windows(
            [[], [(0, 1), (10, 2)]]
        )
        self.assertEqual(outcomes, [])

    def test_wrap_around_feasible_despite_integer_spread(self):
        # 整数极差近 2^32 但前向距离 16：始终可行，无窗口
        outcomes = evaluate_divergence_windows(
            [
                [(0, MAX_SERIAL - 5), (10, MAX_SERIAL - 5)],
                [(0, 10), (10, 0)],
            ]
        )
        self.assertEqual(outcomes, [])

    def test_integer_spread_half_minus_one_opens_window(self):
        # 整数极差仅 2^31-1 也无共同候选（不能用极差 < 2^31 当可行判据）
        outcomes = evaluate_divergence_windows(
            [
                [(0, 0)],
                [(5, HALF - 1), (15, 0)],
            ]
        )
        self.assertEqual(len(outcomes), 1)
        start, end, duration, entries = outcomes[0]
        self.assertEqual((start, end, duration), (5, 15, 10))
        self.assertEqual(entries, [(0, 0), (1, HALF - 1)])

    def test_duplicate_same_timestamp_same_value_dedup(self):
        # 同刻同值重复观测不应产生额外状态 / 窗口
        outcomes = evaluate_divergence_windows(
            [
                [(10, 100), (10, 100), (20, 100)],
                [(10, 100), (20, 200)],
            ]
        )
        self.assertEqual(outcomes, [])

    def test_window_starts_from_global_second_timestamp(self):
        # t=0 只 a 已知（可行）；t=0 同刻 b 也有观测则一起合并。
        # 这里验证两副本首个观测时刻不同时，窗口起点以全局事件时间为准
        outcomes = evaluate_divergence_windows(
            [
                [(0, 5)],
                [(3, 5 + HALF - 1), (8, 5)],
            ]
        )
        self.assertEqual(len(outcomes), 1)
        start, end, duration, entries = outcomes[0]
        self.assertEqual((start, end, duration), (3, 8, 5))
        self.assertEqual(entries, [(0, 5), (1, 5 + HALF - 1)])


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException

    from app.main import app, divergence_windows
    from app.schemas import DivergenceWindowsRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class DivergenceWindowsEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return divergence_windows(
            DivergenceWindowsRequest.model_validate(payload)
        )

    def test_endpoint_happy_path(self):
        resp = self._request(
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
                ]
            }
        )
        self.assertEqual(len(resp.windows), 1)
        window = resp.windows[0]
        self.assertEqual(
            (window.start_at, window.end_at, window.duration_seconds),
            (20, 50, 30),
        )
        self.assertEqual(window.replicas, ["ns-a", "ns-b"])
        self.assertEqual(
            [(e.name, e.serial) for e in window.entries],
            [("ns-a", 100), ("ns-b", 2147483748)],
        )

    def test_endpoint_unrecovered_window_nulls(self):
        resp = self._request(
            {
                "replicas": [
                    {"name": "ns-a", "observations": [{"timestamp": 0, "serial": 0}]},
                    {
                        "name": "ns-b",
                        "observations": [{"timestamp": 5, "serial": 2147483648}],
                    },
                ]
            }
        )
        (window,) = resp.windows
        self.assertEqual(window.start_at, 5)
        self.assertIsNone(window.end_at)
        self.assertIsNone(window.duration_seconds)

    def test_endpoint_no_divergence_empty(self):
        resp = self._request(
            {
                "replicas": [
                    {"name": "ns-a", "observations": [{"timestamp": 0, "serial": 1}]},
                    {"name": "ns-b", "observations": [{"timestamp": 0, "serial": 1}]},
                ]
            }
        )
        self.assertEqual(resp.windows, [])

    def test_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "replicas": [
                        {"name": "ns-a", "observations": []},
                        {"name": "ns-a", "observations": []},
                    ]
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "duplicate_replica_name")

    def test_out_of_order_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 10, "serial": 1},
                                {"timestamp": 9, "serial": 2},
                            ],
                        }
                    ]
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "observation_timestamp_out_of_order"
        )

    def test_same_timestamp_different_serial_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "replicas": [
                        {
                            "name": "ns-a",
                            "observations": [
                                {"timestamp": 10, "serial": 1},
                                {"timestamp": 10, "serial": 2},
                            ],
                        }
                    ]
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], OBSERVATION_SERIAL_CONFLICT
        )

    def test_same_timestamp_same_serial_accepted(self):
        resp = self._request(
            {
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [
                            {"timestamp": 10, "serial": 5},
                            {"timestamp": 10, "serial": 5},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(resp.windows, [])

    def test_atomic_merge_no_spurious_window(self):
        # 两副本在同一时刻一起更新到相同值：原子合并后可行，无窗口
        resp = self._request(
            {
                "replicas": [
                    {
                        "name": "ns-a",
                        "observations": [
                            {"timestamp": 0, "serial": 2147483648},
                            {"timestamp": 10, "serial": 100},
                        ],
                    },
                    {
                        "name": "ns-b",
                        "observations": [
                            {"timestamp": 0, "serial": 2147483648},
                            {"timestamp": 10, "serial": 100},
                        ],
                    },
                ]
            }
        )
        self.assertEqual(resp.windows, [])

    def test_route_registered(self):
        paths = {route.path for route in app.routes}
        self.assertIn("/divergence-windows", paths)


if __name__ == "__main__":
    unittest.main()
