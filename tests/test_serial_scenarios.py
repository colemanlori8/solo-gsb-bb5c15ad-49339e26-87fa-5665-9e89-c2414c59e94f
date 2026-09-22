"""POST /serial-scenarios/generate 确定性边界场景生成单元测试。

仅依赖标准库 + 本项目模块（fastapi 可用时追加路由层测试，风格同
test_delta_consensus.py：直接调用路由函数 / 模型校验，不依赖 httpx）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import unittest

from app.scenarios import (
    AMBIGUOUS,
    FORK,
    LOOP,
    WRAP,
    MAX_SCENARIO_COUNT,
    MAX_SCENARIO_REPLICAS,
    SCENARIO_LABELS,
    generate_serial_scenarios,
)
from app.serial import (
    HALF,
    MAX_SERIAL,
    MOD,
    DeltaValidationError,
    build_delta_chain,
    compare_serial,
    forward_distance,
    plan_transfer,
)

ALL_LABELS = [WRAP, AMBIGUOUS, FORK, LOOP]


def _edge_set(scenario):
    return {
        (d["from_serial"], d["to_serial"]) for d in scenario["deltas"]
    }


# ---------------------------------------------------------------------------
# 确定性与参数
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output(self):
        kwargs = dict(
            seed=20240921, replica_count=4, scenario_count=9,
            include=[WRAP, FORK, LOOP],
        )
        self.assertEqual(
            generate_serial_scenarios(**kwargs),
            generate_serial_scenarios(**kwargs),
        )

    def test_independent_of_process_hash_seed(self):
        """播种只做整数混合：不使用按进程加盐的 str / tuple 哈希。

        这里通过 golden 值锁定 Python 规范的 Mersenne Twister 输出
        （random.Random 的 MT19937 序列跨平台 / CPython 3.x 稳定）。
        """
        scenarios = generate_serial_scenarios(
            seed=20240921, replica_count=3, scenario_count=4,
            include=ALL_LABELS,
        )
        wrap = scenarios[0]
        self.assertEqual(wrap["label"], WRAP)
        self.assertEqual(wrap["target"], 5)
        self.assertEqual(
            [r["current"] for r in wrap["replicas"]],
            [4294967288, 4294967242, 4294967229],
        )
        self.assertEqual(
            _edge_set(wrap),
            {(4294967288, MAX_SERIAL), (MAX_SERIAL, 0), (0, 5)},
        )

    def test_different_seed_changes_values(self):
        # 标签序列与 seed 无关，但数值应随 seed 改变（试多个 seed 避免偶然）
        baseline = generate_serial_scenarios(
            1, 3, 4, ALL_LABELS,
        )
        self.assertTrue(
            any(
                generate_serial_scenarios(seed, 3, 4, ALL_LABELS) != baseline
                for seed in range(2, 30)
            )
        )

    def test_label_sequence_is_seed_independent_rotation(self):
        for seed in (0, 1, 99, MAX_SERIAL):
            scenarios = generate_serial_scenarios(
                seed, 2, 7, [WRAP, LOOP],
            )
            self.assertEqual(
                [s["label"] for s in scenarios],
                [WRAP, LOOP, WRAP, LOOP, WRAP, LOOP, WRAP],
            )

    def test_prefix_stable_when_scenario_count_grows(self):
        # 每个场景用 (seed, index, label) 独立播种：追加场景不改变靠前场景
        short = generate_serial_scenarios(42, 3, 3, ALL_LABELS)
        longer = generate_serial_scenarios(42, 3, 32, ALL_LABELS)
        self.assertEqual(short, longer[:3])

    def test_only_requested_labels_appear(self):
        for include in ([FORK], [WRAP, AMBIGUOUS], [LOOP, FORK]):
            scenarios = generate_serial_scenarios(7, 2, 8, include)
            self.assertTrue(
                {s["label"] for s in scenarios}.issubset(set(include))
            )
            # 轮转保证每个请求标签在前 len(include) 个场景中出现
            self.assertEqual(
                sorted({s["label"] for s in scenarios[: len(include)]}),
                sorted(include),
            )

    def test_replica_names_stable(self):
        scenarios = generate_serial_scenarios(3, 3, 1, [WRAP])
        self.assertEqual(
            [r["name"] for r in scenarios[0]["replicas"]],
            ["ns-1", "ns-2", "ns-3"],
        )


class ParameterValidationTests(unittest.TestCase):
    def _expect_value_error(self, **kwargs):
        with self.assertRaises(ValueError):
            generate_serial_scenarios(**kwargs)

    def test_invalid_seed(self):
        self._expect_value_error(
            seed=-1, replica_count=1, scenario_count=1, include=[WRAP]
        )
        self._expect_value_error(
            seed=MOD, replica_count=1, scenario_count=1, include=[WRAP]
        )

    def test_replica_count_bounds(self):
        self._expect_value_error(
            seed=0, replica_count=0, scenario_count=1, include=[WRAP]
        )
        self._expect_value_error(
            seed=0,
            replica_count=MAX_SCENARIO_REPLICAS + 1,
            scenario_count=1,
            include=[WRAP],
        )

    def test_scenario_count_bounds(self):
        self._expect_value_error(
            seed=0, replica_count=1, scenario_count=0, include=[WRAP]
        )
        self._expect_value_error(
            seed=0,
            replica_count=1,
            scenario_count=MAX_SCENARIO_COUNT + 1,
            include=[WRAP],
        )

    def test_include_empty_unknown_or_duplicate(self):
        self._expect_value_error(
            seed=0, replica_count=1, scenario_count=1, include=[]
        )
        self._expect_value_error(
            seed=0, replica_count=1, scenario_count=1, include=["bogus"]
        )
        self._expect_value_error(
            seed=0, replica_count=1, scenario_count=1,
            include=[WRAP, WRAP],
        )

    def test_limits_match_module_constants(self):
        self.assertEqual(MAX_SCENARIO_REPLICAS, 12)
        self.assertEqual(MAX_SCENARIO_COUNT, 32)
        self.assertEqual(SCENARIO_LABELS, (WRAP, AMBIGUOUS, FORK, LOOP))


# ---------------------------------------------------------------------------
# 32 位范围与无序样本
# ---------------------------------------------------------------------------


class ValueRangeTests(unittest.TestCase):
    def test_all_serials_are_32bit(self):
        for seed in range(50):
            for scenario in generate_serial_scenarios(
                seed, 5, 4, ALL_LABELS
            ):
                self.assertTrue(0 <= scenario["target"] <= MAX_SERIAL)
                for replica in scenario["replicas"]:
                    self.assertTrue(0 <= replica["current"] <= MAX_SERIAL)
                for delta in scenario["deltas"]:
                    self.assertTrue(0 <= delta["from_serial"] <= MAX_SERIAL)
                    self.assertTrue(0 <= delta["to_serial"] <= MAX_SERIAL)
                self.assertEqual(
                    len(scenario["replicas"]), 5,
                )

    def test_deltas_are_an_unordered_sample(self):
        # 样本刻意洗牌：多 seed 下至少出现一次与链顺序不一致的排列；
        # 但作为集合恒包含所需边（具体行为按集合断言，见各场景测试）
        seen_shuffled = False
        for seed in range(100):
            scenario = generate_serial_scenarios(
                seed, 2, 1, [WRAP]
            )[0]
            ordered_pairs = [
                (d["from_serial"], d["to_serial"]) for d in scenario["deltas"]
            ]
            # 链序为 ..., 4294967295->0, ...；该边排在首位即仍是某随机排列
            if ordered_pairs[0] != (
                scenario["replicas"][0]["current"], MAX_SERIAL
            ):
                seen_shuffled = True
                break
        self.assertTrue(seen_shuffled, "100 个 seed 内增量从未以乱序出现")


# ---------------------------------------------------------------------------
# 各标签场景与现有 RFC 1982 逻辑的交叉验证
# ---------------------------------------------------------------------------


class WrapScenarioTests(unittest.TestCase):
    def test_chain_crosses_wrap_point(self):
        for seed in range(60):
            scenario = generate_serial_scenarios(
                seed, 4, 1, [WRAP]
            )[0]
            currents = [r["current"] for r in scenario["replicas"]]
            target = scenario["target"]
            edges = _edge_set(scenario)

            self.assertIn((MAX_SERIAL, 0), edges)
            self.assertEqual(
                forward_distance(MAX_SERIAL, 0), 1,
            )
            self.assertEqual(compare_serial(MAX_SERIAL, 0), "newer")

            chain = build_delta_chain(
                [(d["from_serial"], d["to_serial"])
                 for d in scenario["deltas"]]
            )
            # ns-1 停在链起点，沿链 IXFR 跨过回绕点到达 target
            mode, reason, ordered = plan_transfer(
                currents[0], target, chain,
            )
            self.assertEqual(mode, "ixfr", scenario)
            self.assertIsNone(reason)
            self.assertEqual(ordered[0][0], currents[0])
            self.assertEqual(ordered[-1][1], target)

            # 所有副本相对 target 都在严格 newer 弧内（/audit 不阻塞）
            for current in currents:
                self.assertIn(
                    compare_serial(current, target),
                    ("newer", "equal"),
                    scenario,
                )

            kinds = {tp["kind"] for tp in scenario["trigger_points"]}
            self.assertIn("wrap_edge", kinds)

    def test_single_replica_shape(self):
        scenario = generate_serial_scenarios(5, 1, 1, [WRAP])[0]
        self.assertEqual(len(scenario["replicas"]), 1)
        kinds = {tp["kind"] for tp in scenario["trigger_points"]}
        # ns-1 停在回绕点前（整数上远大于 0..7 的 target），
        # 其前向路径同样跨过回绕点，故 wrap_edge 与 wrap_advance 都可能出现
        self.assertIn("wrap_edge", kinds)
        self.assertTrue(
            kinds.issubset({"wrap_edge", "wrap_advance"}),
        )


class AmbiguousScenarioTests(unittest.TestCase):
    def test_antipodal_distance_is_half(self):
        for seed in range(60):
            for count in (1, 2, 6):
                scenario = generate_serial_scenarios(
                    seed, count, 1, [AMBIGUOUS]
                )[0]
                currents = [r["current"] for r in scenario["replicas"]]
                target = scenario["target"]

                self.assertEqual(
                    forward_distance(currents[0], target), HALF,
                )
                self.assertEqual(
                    compare_serial(currents[0], target), "ambiguous",
                )
                # 即使存在连通边，正对点也必须 AXFR
                mode, reason, _ = plan_transfer(
                    currents[0], target, {},
                )
                self.assertEqual(mode, "axfr")
                self.assertEqual(reason, "target_incomparable")

                for current in currents[1:]:
                    self.assertIn(
                        compare_serial(current, target),
                        ("newer", "equal"),
                    )

                trigger = scenario["trigger_points"][0]
                self.assertEqual(trigger["kind"], "antipodal_pair")
                self.assertEqual(trigger["replica"], "ns-1")
                self.assertEqual(trigger["forward_distance"], HALF)

    def test_reference_replica_has_ixfr_chain(self):
        scenario = generate_serial_scenarios(11, 3, 1, [AMBIGUOUS])[0]
        currents = [r["current"] for r in scenario["replicas"]]
        chain = build_delta_chain(
            [(d["from_serial"], d["to_serial"])
             for d in scenario["deltas"]]
        )
        mode, reason, _ = plan_transfer(
            currents[1], scenario["target"], chain,
        )
        self.assertEqual(mode, "ixfr")
        self.assertIsNone(reason)
        kinds = {tp["kind"] for tp in scenario["trigger_points"]}
        self.assertIn("comparable_reference", kinds)

    def test_single_replica_has_no_reference_delta(self):
        scenario = generate_serial_scenarios(11, 1, 1, [AMBIGUOUS])[0]
        self.assertEqual(scenario["deltas"], [])
        self.assertEqual(
            [tp["kind"] for tp in scenario["trigger_points"]],
            ["antipodal_pair"],
        )


class ForkScenarioTests(unittest.TestCase):
    def test_two_edges_share_from_serial(self):
        for seed in range(60):
            scenario = generate_serial_scenarios(
                seed, 4, 1, [FORK]
            )[0]
            raw = [
                (d["from_serial"], d["to_serial"])
                for d in scenario["deltas"]
            ]
            # 每条边单独看都是严格 newer
            for frm, to in raw:
                self.assertEqual(compare_serial(frm, to), "newer")
            # 整体校验以 delta_fork 拒绝
            with self.assertRaises(DeltaValidationError) as ctx:
                build_delta_chain(raw)
            self.assertEqual(ctx.exception.detail["error"], "delta_fork")

            trigger = scenario["trigger_points"][0]
            self.assertEqual(trigger["kind"], "fork")
            self.assertIn(trigger["from_serial"],
                          [frm for frm, _ in raw])
            self.assertEqual(len(trigger["to_serials"]), 2)
            self.assertNotEqual(
                trigger["to_serials"][0], trigger["to_serials"][1],
            )

    def test_ns2_sits_at_target(self):
        scenario = generate_serial_scenarios(3, 3, 1, [FORK])[0]
        self.assertEqual(
            scenario["replicas"][1]["current"], scenario["target"],
        )


class LoopScenarioTests(unittest.TestCase):
    def test_three_newer_edges_sum_to_mod(self):
        for seed in range(80):
            for count in (1, 2, 8):
                scenario = generate_serial_scenarios(
                    seed, count, 1, [LOOP]
                )[0]
                currents = [r["current"] for r in scenario["replicas"]]
                target = scenario["target"]
                raw = [
                    (d["from_serial"], d["to_serial"])
                    for d in scenario["deltas"]
                ]

                self.assertEqual(len(raw), 3)
                # raw 已洗牌，环序距离以 loop 触发点的 nodes 序列为准
                loop_trigger = next(
                    tp for tp in scenario["trigger_points"]
                    if tp["kind"] == "loop"
                )
                nodes = loop_trigger["nodes"]
                distances = loop_trigger["forward_distances"]
                self.assertEqual(len(nodes), 3)
                for i, dist in enumerate(distances):
                    self.assertGreaterEqual(dist, 2)
                    self.assertLessEqual(dist, HALF - 1)
                    self.assertEqual(
                        forward_distance(nodes[i], nodes[(i + 1) % 3]),
                        dist,
                    )
                self.assertEqual(sum(distances), MOD)
                self.assertEqual(
                    sorted(distances),
                    sorted(
                        forward_distance(frm, to) for frm, to in raw
                    ),
                )

                # 三条边各自合法 -> 整体校验通过；沿链行进回到已访问节点
                chain = build_delta_chain(raw)
                mode, reason, _ = plan_transfer(
                    currents[0], target, chain,
                )
                self.assertEqual(mode, "axfr", scenario)
                self.assertEqual(reason, "loop")

                edge_kinds = [
                    tp["kind"] for tp in scenario["trigger_points"]
                ]
                self.assertEqual(edge_kinds.count("loop_edge"), 3)

    def test_ns2_up_to_date(self):
        scenario = generate_serial_scenarios(3, 3, 1, [LOOP])[0]
        chain = build_delta_chain(
            [(d["from_serial"], d["to_serial"])
             for d in scenario["deltas"]]
        )
        mode, reason, _ = plan_transfer(
            scenario["replicas"][1]["current"],
            scenario["target"],
            chain,
        )
        self.assertEqual(mode, "up_to_date")
        self.assertIsNone(reason)


class RngSeedingTests(unittest.TestCase):
    def test_derived_stream_is_integer_only(self):
        """相同 (seed,index,label) 两次派生的 Random 产出一致。"""
        from app.scenarios import _scenario_rng

        first = _scenario_rng(123, 4, LOOP)
        second = _scenario_rng(123, 4, LOOP)
        self.assertEqual(
            [first.getrandbits(64) for _ in range(8)],
            [second.getrandbits(64) for _ in range(8)],
        )

    def test_different_labels_derive_different_streams(self):
        from app.scenarios import _scenario_rng

        streams = {
            label: [
                _scenario_rng(0, 0, label).getrandbits(64)
                for _ in range(4)
            ]
            for label in ALL_LABELS
        }
        self.assertEqual(len({tuple(v) for v in streams.values()}), 4)

    def test_shuffle_uses_deterministic_random(self):
        # 生成器内部洗牌用的是派生 Random，而非全局 random 模块状态
        random.seed(0xDEADBEEF)
        a = generate_serial_scenarios(9, 2, 1, [WRAP])
        random.seed(0)
        b = generate_serial_scenarios(9, 2, 1, [WRAP])
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# 路由层（FastAPI 可用时）
# ---------------------------------------------------------------------------

try:
    from fastapi import HTTPException as _HTTPException

    from app.main import serial_scenarios_generate
    from app.schemas import (
        SerialScenarioGenerateRequest,
        SerialScenarioGenerateResponse,
    )

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class SerialScenarioEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return serial_scenarios_generate(
            SerialScenarioGenerateRequest.model_validate(payload)
        )

    def _payload(self, **overrides):
        payload = {
            "seed": 20240921,
            "replica_count": 3,
            "scenario_count": 4,
            "include": ALL_LABELS,
        }
        payload.update(overrides)
        return payload

    def test_endpoint_returns_typed_response(self):
        resp = self._request(self._payload())
        self.assertIsInstance(resp, SerialScenarioGenerateResponse)
        self.assertEqual(resp.seed, 20240921)
        self.assertEqual(resp.replica_count, 3)
        self.assertEqual(resp.scenario_count, 4)
        self.assertEqual(resp.include, ALL_LABELS)
        self.assertEqual(
            [s.label for s in resp.scenarios], ALL_LABELS,
        )
        self.assertEqual([s.index for s in resp.scenarios], [0, 1, 2, 3])
        for scenario in resp.scenarios:
            self.assertEqual(len(scenario.replicas), 3)
            self.assertTrue(scenario.trigger_points)
            self.assertTrue(scenario.deltas or scenario.label == AMBIGUOUS)

    def test_endpoint_is_reproducible(self):
        resp1 = self._request(self._payload())
        resp2 = self._request(self._payload())
        self.assertEqual(
            resp1.model_dump(), resp2.model_dump(),
        )

    def test_endpoint_echoes_include_order(self):
        resp = self._request(self._payload(include=[LOOP, WRAP]))
        self.assertEqual(resp.include, [LOOP, WRAP])
        self.assertEqual(
            [s.label for s in resp.scenarios], [LOOP, WRAP, LOOP, WRAP],
        )

    def test_endpoint_seed_boundary_values_ok(self):
        for seed in (0, MAX_SERIAL):
            resp = self._request(
                self._payload(seed=seed, scenario_count=1)
            )
            self.assertEqual(resp.seed, seed)

    def test_schema_rejects_invalid_params(self):
        # 与现有端点一致：非法参数在模型层即 ValidationError
        # （FastAPI 装配后自动返回 422）
        invalid_payloads = [
            self._payload(seed=-1),
            self._payload(seed=MOD),
            self._payload(seed=1.5),
            self._payload(seed=True),
            self._payload(seed="1"),
            self._payload(replica_count=0),
            self._payload(replica_count=MAX_SCENARIO_REPLICAS + 1),
            self._payload(scenario_count=0),
            self._payload(scenario_count=MAX_SCENARIO_COUNT + 1),
            self._payload(include=[]),
            self._payload(include=["bogus"]),
            self._payload(include=[WRAP, WRAP]),
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(Exception):
                    SerialScenarioGenerateRequest.model_validate(payload)

    def test_missing_required_field_rejected(self):
        with self.assertRaises(Exception):
            SerialScenarioGenerateRequest.model_validate(
                {"seed": 0, "replica_count": 1, "scenario_count": 1},
            )

    def test_generated_payload_drives_transfer_plan_scenarios(self):
        """端到端：生成结果原样喂给 /transfer-plan 路由函数，触发行为符合说明。"""
        from app.main import transfer_plan
        from app.schemas import TransferPlanRequest

        resp = self._request(self._payload())
        by_label = {s.label: s for s in resp.scenarios}

        # wrap：ns-1 沿跨回绕链 ixfr
        wrap = by_label[WRAP]
        tp = transfer_plan(
            TransferPlanRequest.model_validate({
                "target": wrap.target,
                "replicas": [r.model_dump() for r in wrap.replicas],
                "deltas": [d.model_dump() for d in wrap.deltas],
            })
        )
        self.assertEqual(tp.results[0].mode, "ixfr")

        # ambiguous：ns-1 即使有边也 axfr / target_incomparable
        amb = by_label[AMBIGUOUS]
        tp = transfer_plan(
            TransferPlanRequest.model_validate({
                "target": amb.target,
                "replicas": [r.model_dump() for r in amb.replicas],
                "deltas": [d.model_dump() for d in amb.deltas],
            })
        )
        self.assertEqual(tp.results[0].mode, "axfr")
        self.assertEqual(tp.results[0].reason, "target_incomparable")

        # fork：整体校验 422 delta_fork
        fork = by_label[FORK]
        with self.assertRaises(_HTTPException) as ctx:
            transfer_plan(
                TransferPlanRequest.model_validate({
                    "target": fork.target,
                    "replicas": [r.model_dump() for r in fork.replicas],
                    "deltas": [d.model_dump() for d in fork.deltas],
                })
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")

        # loop：ns-1 axfr / loop
        loop = by_label[LOOP]
        tp = transfer_plan(
            TransferPlanRequest.model_validate({
                "target": loop.target,
                "replicas": [r.model_dump() for r in loop.replicas],
                "deltas": [d.model_dump() for d in loop.deltas],
            })
        )
        self.assertEqual(tp.results[0].mode, "axfr")
        self.assertEqual(tp.results[0].reason, "loop")


if __name__ == "__main__":
    unittest.main()
