"""POST /serial-scenarios/generate 确定性场景生成单元测试。

仅依赖标准库 + 本项目模块；路由层测试在 fastapi 可用时自动启用。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import itertools
import unittest

from app.scenarios import (
    DeterministicRandom,
    SCENARIO_LABELS,
    generate_serial_scenarios,
)
from app.serial import (
    HALF,
    MOD,
    MAX_SERIAL,
    DeltaValidationError,
    build_delta_chain,
    compare_serial,
    forward_distance,
    plan_transfer,
)


def trigger(out: dict, label: str) -> dict:
    return next(t for t in out["triggers"] if t["label"] == label)


def trigger_edges(out: dict, label: str):
    return [(d["from_serial"], d["to_serial"]) for d in trigger(out, label)["deltas"]]


def all_pairs(out: dict):
    return [(d["from_serial"], d["to_serial"]) for d in out["deltas"]]


def validation_error(out: dict):
    """对整组增量跑 build_delta_chain，返回 422 error 名（无错误为 None）。"""
    try:
        build_delta_chain(all_pairs(out))
    except DeltaValidationError as exc:
        return exc.detail["error"]
    return None


# ---------------------------------------------------------------------------
# 确定性伪随机数
# ---------------------------------------------------------------------------


class DeterministicRandomTests(unittest.TestCase):
    def test_same_seed_same_sequence(self):
        a = DeterministicRandom(123)
        b = DeterministicRandom(123)
        self.assertEqual([a.next_u64() for _ in range(50)],
                         [b.next_u64() for _ in range(50)])

    def test_different_seed_differs(self):
        a = DeterministicRandom(1)
        b = DeterministicRandom(2)
        self.assertNotEqual([a.next_u64() for _ in range(8)],
                            [b.next_u64() for _ in range(8)])

    def test_below_in_range_and_unbiased(self):
        rng = DeterministicRandom(77)
        counts = [0] * 10
        for _ in range(20000):
            value = rng.below(10)
            self.assertIn(value, range(10))
            counts[value] += 1
        # 均匀性粗检：每桶期望 2000，容忍区间 [1500, 2500]
        self.assertTrue(all(1500 <= c <= 2500 for c in counts), counts)

    def test_below_one(self):
        rng = DeterministicRandom(5)
        self.assertEqual([rng.below(1) for _ in range(20)], [0] * 20)

    def test_below_power_of_two_and_32bit_bound(self):
        rng = DeterministicRandom(9)
        for _ in range(1000):
            self.assertIn(rng.below(1 << 32), range(MOD))
        for _ in range(1000):
            self.assertIn(rng.below(1 << 16), range(1 << 16))

    def test_randrange_and_serial32_in_range(self):
        rng = DeterministicRandom(11)
        for _ in range(1000):
            value = rng.randrange(-50, 50)
            self.assertIn(value, range(-50, 50))
            self.assertIn(rng.serial32(), range(MOD))

    def test_shuffle_is_permutation_and_deterministic(self):
        a = DeterministicRandom(3)
        items = list(range(20))
        a.shuffle(items)
        self.assertEqual(sorted(items), list(range(20)))
        b = DeterministicRandom(3)
        again = list(range(20))
        b.shuffle(again)
        self.assertEqual(items, again)


# ---------------------------------------------------------------------------
# 场景生成：确定性 / 选择顺序
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):
    def test_same_request_identical_output(self):
        payload = dict(seed=20240921, replica_count=4, scenario_count=4,
                       include=["wrap", "ambiguous", "fork", "loop"])
        first = generate_serial_scenarios(**payload)
        second = generate_serial_scenarios(**payload)
        self.assertEqual(first, second)

    def test_include_order_does_not_matter(self):
        ordered = ["ambiguous", "fork", "loop", "wrap"]
        base = generate_serial_scenarios(42, 3, 4, ordered)
        reversed_out = generate_serial_scenarios(42, 3, 4, list(reversed(ordered)))
        self.assertEqual(reversed_out, base)
        shuffled = ["loop", "wrap", "ambiguous", "fork"]
        self.assertEqual(generate_serial_scenarios(42, 3, 4, shuffled), base)

    def test_seed_changes_output(self):
        args = dict(replica_count=3, scenario_count=2,
                    include=["wrap", "loop"])
        self.assertNotEqual(
            generate_serial_scenarios(seed=1, **args),
            generate_serial_scenarios(seed=2, **args),
        )

    def test_scenarios_canonical_selection_and_order(self):
        # 规范顺序 ambiguous < fork < loop < wrap；scenario_count 取前 n 个
        out = generate_serial_scenarios(1, 4, 1, ["wrap", "loop", "fork", "ambiguous"])
        self.assertEqual(out["scenarios"], ["ambiguous"])
        out = generate_serial_scenarios(1, 4, 2, ["wrap", "loop", "fork", "ambiguous"])
        self.assertEqual(out["scenarios"], ["ambiguous", "fork"])
        out = generate_serial_scenarios(1, 4, 3, ["wrap", "loop"])
        self.assertEqual(out["scenarios"], ["loop", "wrap"])
        # include 回显也按规范顺序
        self.assertEqual(out["include"], ["loop", "wrap"])

    def test_triggers_follow_scenario_order(self):
        out = generate_serial_scenarios(3, 2, 4, list(SCENARIO_LABELS))
        self.assertEqual([t["label"] for t in out["triggers"]], out["scenarios"])
        for trig in out["triggers"]:
            self.assertTrue(trig["title"])
            self.assertIn(trig["label"], trig["description"])


# ---------------------------------------------------------------------------
# 32 位范围与输出结构
# ---------------------------------------------------------------------------


class StructureTests(unittest.TestCase):
    def _all_out(self, seed_range=8):
        for seed in range(seed_range):
            for replica_count in (1, 2, 3, 5, 12):
                for scenario_count in range(1, 5):
                    for k in range(1, 5):
                        if scenario_count > k:
                            continue
                        for subset in itertools.combinations(SCENARIO_LABELS, k):
                            yield seed, replica_count, scenario_count, subset, \
                                generate_serial_scenarios(
                                    seed, replica_count, scenario_count, list(subset)
                                )

    def test_serials_within_32bit_range(self):
        for seed, rc, sc, subset, out in self._all_out():
            with self.subTest(seed=seed, rc=rc, sc=sc, subset=subset):
                self.assertIn(out["target"], range(MOD))
                for replica in out["replicas"]:
                    self.assertIn(replica["current"], range(MOD))
                for delta in out["deltas"]:
                    self.assertIn(delta["from_serial"], range(MOD))
                    self.assertIn(delta["to_serial"], range(MOD))
                    self.assertNotEqual(delta["from_serial"], delta["to_serial"])

    def test_replica_names_sequence_and_distinct_serials(self):
        for seed, rc, sc, subset, out in self._all_out():
            with self.subTest(seed=seed, rc=rc, sc=sc, subset=subset):
                self.assertEqual(
                    [r["name"] for r in out["replicas"]],
                    [f"ns-{i + 1}" for i in range(rc)],
                )
                currents = [r["current"] for r in out["replicas"]]
                self.assertEqual(len(set(currents)), rc)

    def test_deltas_unique_and_contain_trigger_edges(self):
        for seed, rc, sc, subset, out in self._all_out():
            with self.subTest(seed=seed, rc=rc, sc=sc, subset=subset):
                pairs = all_pairs(out)
                self.assertEqual(len(set(pairs)), len(pairs))
                for trig in out["triggers"]:
                    for edge in trig["deltas"]:
                        pair = (edge["from_serial"], edge["to_serial"])
                        self.assertIn(pair, pairs)

    def test_background_filler_chain_present(self):
        # 选中场景之外还应有至少一条普通背景链增量
        for subset in (("wrap",), ("ambiguous",), ("fork",), ("loop",)):
            out = generate_serial_scenarios(0, 2, 1, list(subset))
            structural = {
                pair for trig in out["triggers"]
                for pair in trigger_edges(out, trig["label"])
            }
            background = [p for p in all_pairs(out) if p not in structural]
            self.assertTrue(background, subset)
            for pair in background:
                self.assertEqual(compare_serial(*pair), "newer")


# ---------------------------------------------------------------------------
# 各场景边界语义
# ---------------------------------------------------------------------------


class WrapScenarioTests(unittest.TestCase):
    def test_wrap_edge_crosses_2_32(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                out = generate_serial_scenarios(seed, 1, 1, ["wrap"])
                frm, to = trigger_edges(out, "wrap")[0]
                self.assertGreaterEqual(frm, MAX_SERIAL - 7)
                distance = forward_distance(frm, to)
                self.assertIn(distance, range(1, 9))
                self.assertEqual(to, (frm + distance) % MOD)
                self.assertEqual(compare_serial(frm, to), "newer")

    def test_wrap_set_overall_valid(self):
        # 只有 wrap（加背景链）时整组增量通过 build_delta_chain
        out = generate_serial_scenarios(5, 1, 1, ["wrap"])
        self.assertIsNone(validation_error(out))


class AmbiguousScenarioTests(unittest.TestCase):
    def test_antipode_anchor(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                out = generate_serial_scenarios(seed, 2, 1, ["ambiguous"])
                frm, to = trigger_edges(out, "ambiguous")[0]
                self.assertEqual(to, out["target"])
                self.assertEqual(forward_distance(frm, to), HALF)
                self.assertEqual(compare_serial(frm, to), "ambiguous")
                # ns-1 锚定在正对点上
                self.assertEqual(out["replicas"][0]["current"], frm)
                self.assertEqual(trigger(out, "ambiguous")["replica"], "ns-1")

    def test_rejected_as_not_strictly_newer(self):
        out = generate_serial_scenarios(5, 1, 1, ["ambiguous"])
        self.assertEqual(validation_error(out), "delta_not_strictly_newer")

    def test_removing_ambiguous_edge_leaves_valid_set(self):
        out = generate_serial_scenarios(5, 3, 2, ["ambiguous", "wrap"])
        bad = set(trigger_edges(out, "ambiguous"))
        build_delta_chain([p for p in all_pairs(out) if p not in bad])


class ForkScenarioTests(unittest.TestCase):
    def test_fork_structure(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                out = generate_serial_scenarios(seed, 1, 1, ["fork"])
                edges = trigger_edges(out, "fork")
                self.assertEqual(len(edges), 2)
                self.assertEqual(edges[0][0], edges[1][0])
                self.assertNotEqual(edges[0][1], edges[1][1])
                for pair in edges:
                    self.assertEqual(compare_serial(*pair), "newer")

    def test_rejected_as_fork(self):
        # 无 ambiguous 边时，整组校验报 delta_fork
        out = generate_serial_scenarios(5, 2, 2, ["fork", "wrap"])
        self.assertEqual(validation_error(out), "delta_fork")

    def test_fork_anchor_replica(self):
        out = generate_serial_scenarios(5, 3, 2, ["fork", "wrap"])
        base = trigger_edges(out, "fork")[0][0]
        # fork 在规范顺序中排在 wrap 之前，锚定 ns-1
        self.assertEqual(out["replicas"][0]["current"], base)
        self.assertEqual(trigger(out, "fork")["replica"], "ns-1")


class LoopScenarioTests(unittest.TestCase):
    def test_three_edge_wrap_cycle(self):
        for seed in range(50):
            with self.subTest(seed=seed):
                out = generate_serial_scenarios(seed, 1, 1, ["loop"])
                edges = trigger_edges(out, "loop")
                self.assertEqual(len(edges), 3)
                s1, s2 = edges[0]
                self.assertEqual(edges[1][0], s2)
                s3 = edges[1][1]
                self.assertEqual(edges[2], (s3, s1))
                distances = [forward_distance(*e) for e in edges]
                for distance in distances:
                    self.assertIn(distance, range(1, HALF))
                self.assertEqual(sum(distances), MOD)
                for pair in edges:
                    self.assertEqual(compare_serial(*pair), "newer")

    def test_plan_transfer_reports_loop(self):
        out = generate_serial_scenarios(5, 1, 1, ["loop"])
        edges = trigger_edges(out, "loop")
        chain = {frm: to for frm, to in edges}
        s1 = edges[0][0]
        target = (s1 + 12345) % MOD
        mode, reason, _ = plan_transfer(s1, target, chain)
        self.assertEqual((mode, reason), ("axfr", "loop"))

    def test_loop_only_set_is_valid_but_cycles(self):
        # 三条边都严格 newer，build_delta_chain 接受（不校验环路），
        # 但沿链规划传输时以 loop 终止
        out = generate_serial_scenarios(6, 1, 1, ["loop"])
        self.assertIsNone(validation_error(out))


class CombinedScenariosTests(unittest.TestCase):
    def test_all_four_structures_coexist(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                out = generate_serial_scenarios(
                    seed, 4, 4, ["wrap", "ambiguous", "fork", "loop"]
                )
                self.assertEqual(
                    out["scenarios"], ["ambiguous", "fork", "loop", "wrap"]
                )
                currents = [r["current"] for r in out["replicas"]]
                # 锚点按规范顺序占据 ns-1..ns-4
                self.assertEqual(currents[0], trigger_edges(out, "ambiguous")[0][0])
                self.assertEqual(currents[1], trigger_edges(out, "fork")[0][0])
                self.assertEqual(currents[2], trigger_edges(out, "loop")[0][0])
                self.assertEqual(currents[3], trigger_edges(out, "wrap")[0][0])
                # 含 ambiguous 边时整组先被 422 delta_not_strictly_newer 拦截
                self.assertEqual(
                    validation_error(out), "delta_not_strictly_newer"
                )

    def test_fork_and_loop_without_ambiguous_reports_fork(self):
        out = generate_serial_scenarios(7, 3, 3, ["fork", "loop", "wrap"])
        self.assertEqual(validation_error(out), "delta_fork")
        # 去掉 fork 边后集合合法，且环节点规划报 loop
        fork_edges = set(trigger_edges(out, "fork"))
        chain = build_delta_chain(
            [p for p in all_pairs(out) if p not in fork_edges]
        )
        s1 = trigger_edges(out, "loop")[0][0]
        mode, reason, _ = plan_transfer(s1, (s1 + 999) % MOD, chain)
        self.assertEqual(mode, "axfr")
        self.assertIn(reason, ("loop", "gap"))

    def test_replica_slots_smaller_than_scenarios(self):
        out = generate_serial_scenarios(8, 1, 4, list(SCENARIO_LABELS))
        self.assertEqual(
            [t["replica"] for t in out["triggers"]],
            ["ns-1", None, None, None],
        )

    def test_extra_replica_slots_are_random_background(self):
        # 副本多于场景：多出的副本序列号不与任何锚点 / 目标重复
        out = generate_serial_scenarios(9, 12, 2, ["ambiguous", "fork"])
        anchor = {
            out["replicas"][0]["current"],
            out["replicas"][1]["current"],
            out["target"],
        }
        rest = [r["current"] for r in out["replicas"][2:]]
        for current in rest:
            self.assertNotIn(current, anchor)


# ---------------------------------------------------------------------------
# 路由层（fastapi 可用时）
# ---------------------------------------------------------------------------

try:
    from fastapi import HTTPException as _HTTPException

    from app.main import generate_serial_scenarios_endpoint
    from app.schemas import SerialScenarioGenerateRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class GenerateEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return generate_serial_scenarios_endpoint(
            SerialScenarioGenerateRequest.model_validate(payload)
        )

    def test_endpoint_happy_path(self):
        resp = self._request(
            {"seed": 20240921, "replica_count": 4, "scenario_count": 4,
             "include": ["wrap", "ambiguous", "fork", "loop"]}
        )
        self.assertEqual(resp.scenarios, ["ambiguous", "fork", "loop", "wrap"])
        self.assertEqual(resp.include, ["ambiguous", "fork", "loop", "wrap"])
        self.assertEqual(len(resp.replicas), 4)
        self.assertEqual(len(resp.triggers), 4)
        for delta in resp.deltas:
            self.assertIn(delta.from_serial, range(MOD))
            self.assertIn(delta.to_serial, range(MOD))

    def test_endpoint_reproducible(self):
        payload = {"seed": 20240921, "replica_count": 4, "scenario_count": 4,
                   "include": ["wrap", "ambiguous", "fork", "loop"]}
        first = self._request(payload).model_dump()
        second = self._request(payload).model_dump()
        self.assertEqual(first, second)
        payload["include"] = ["loop", "fork", "ambiguous", "wrap"]
        self.assertEqual(self._request(payload).model_dump(), first)

    def test_scenario_count_exceeds_include(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request({"seed": 1, "replica_count": 2, "scenario_count": 3,
                           "include": ["wrap", "loop"]})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "scenario_count_exceeds_include"
        )

    def test_duplicate_include_label(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request({"seed": 1, "replica_count": 2, "scenario_count": 1,
                           "include": ["wrap", "wrap"]})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "duplicate_scenario_label")

    def test_schema_level_422_cases(self):
        from pydantic import ValidationError

        bad_payloads = [
            {"seed": -1, "replica_count": 2, "scenario_count": 1,
             "include": ["wrap"]},
            {"seed": MOD, "replica_count": 2, "scenario_count": 1,
             "include": ["wrap"]},
            {"seed": 1.5, "replica_count": 2, "scenario_count": 1,
             "include": ["wrap"]},
            {"seed": "x", "replica_count": 2, "scenario_count": 1,
             "include": ["wrap"]},
            {"seed": True, "replica_count": 2, "scenario_count": 1,
             "include": ["wrap"]},
            {"seed": 1, "replica_count": 0, "scenario_count": 1,
             "include": ["wrap"]},
            {"seed": 1, "replica_count": 13, "scenario_count": 1,
             "include": ["wrap"]},
            {"seed": 1, "replica_count": 2, "scenario_count": 5,
             "include": ["wrap"]},
            {"seed": 1, "replica_count": 2, "scenario_count": 0,
             "include": ["wrap"]},
            {"seed": 1, "replica_count": 2, "scenario_count": 1,
             "include": []},
            {"seed": 1, "replica_count": 2, "scenario_count": 1,
             "include": ["bogus"]},
            {"seed": 1, "replica_count": 2, "scenario_count": "1",
             "include": ["wrap"]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    SerialScenarioGenerateRequest.model_validate(payload)

    def test_boundary_counts_accepted(self):
        resp = self._request(
            {"seed": 0, "replica_count": 12, "scenario_count": 4,
             "include": ["wrap", "ambiguous", "fork", "loop"]}
        )
        self.assertEqual(len(resp.replicas), 12)
        resp = self._request(
            {"seed": 0, "replica_count": 1, "scenario_count": 1,
             "include": ["wrap", "ambiguous", "fork", "loop"]}
        )
        self.assertEqual(resp.scenarios, ["ambiguous"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
