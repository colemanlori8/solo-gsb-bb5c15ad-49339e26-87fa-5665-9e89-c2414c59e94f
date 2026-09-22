"""POST /delta-consensus 增量共识单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    CONSENSUS_FORK,
    CONSENSUS_GAP,
    CONSENSUS_LOOP,
    CONSENSUS_REACHED,
    MirrorReportError,
    evaluate_delta_consensus,
    validate_mirror_reports,
)


def edges(out):
    """从响应 dict 提取可信链的三元组列表。"""
    return [(e["from_serial"], e["to_serial"], e["digest"])
            for e in out["trusted_chain"]]


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


class ValidateMirrorReportsTests(unittest.TestCase):
    def test_accept_empty_and_duplicates(self):
        validate_mirror_reports([])
        validate_mirror_reports([("a", [])])
        # 完全相同的三元组重复上报合法
        validate_mirror_reports([("a", [(1, 2, "x"), (1, 2, "x")])])
        # 不同起点各自一个版本合法
        validate_mirror_reports([("a", [(1, 2, "x"), (2, 3, "y")])])

    def test_reject_non_newer(self):
        for to_serial, relation in [
            (0, "equal"),
            (HALF, "ambiguous"),
            (HALF + 1, "older"),
        ]:
            with self.subTest(to_serial=to_serial):
                with self.assertRaises(MirrorReportError) as ctx:
                    validate_mirror_reports([("a", [(0, to_serial, "x")])])
                detail = ctx.exception.detail
                self.assertEqual(detail["error"], "delta_not_strictly_newer")
                self.assertEqual(detail["mirror"], "a")
                self.assertEqual(detail["relation"], relation)

    def test_wrap_around_edge_is_newer(self):
        validate_mirror_reports([("a", [(MAX_SERIAL, 0, "wrap")])])

    def test_reject_conflicting_to_serial(self):
        with self.assertRaises(MirrorReportError) as ctx:
            validate_mirror_reports([("a", [(1, 2, "x"), (1, 3, "x")])])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "mirror_version_conflict")
        self.assertEqual(detail["mirror"], "a")
        self.assertEqual(detail["from_serial"], 1)
        self.assertEqual(
            [(v["to_serial"], v["digest"]) for v in detail["versions"]],
            [(2, "x"), (3, "x")],
        )

    def test_reject_conflicting_digest_same_pair(self):
        with self.assertRaises(MirrorReportError) as ctx:
            validate_mirror_reports([("a", [(1, 2, "x"), (1, 2, "y")])])
        self.assertEqual(ctx.exception.detail["error"], "mirror_version_conflict")

    def test_same_conflict_in_different_mirrors_is_ok(self):
        # 冲突只在单个镜像内部拒绝；不同镜像可各执一词（计票后再裁决）
        validate_mirror_reports(
            [("a", [(1, 2, "x")]), ("b", [(1, 2, "y")])]
        )

    def test_non_newer_checked_before_conflict(self):
        # 非法边优先于冲突报错（两遍校验的第一遍）
        with self.assertRaises(MirrorReportError) as ctx:
            validate_mirror_reports(
                [("a", [(1, 2, "x"), (1, 2, "y"), (0, HALF, "z")])]
            )
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_conflict_reported_deterministically_by_input_order(self):
        # 后出现的冲突版本总是在 versions 的第二位
        with self.assertRaises(MirrorReportError) as ctx:
            validate_mirror_reports([("a", [(5, 9, "p"), (5, 7, "q")])])
        versions = ctx.exception.detail["versions"]
        self.assertEqual(versions[0], {"from_serial": 5, "to_serial": 9,
                                       "digest": "p"})
        self.assertEqual(versions[1], {"from_serial": 5, "to_serial": 7,
                                       "digest": "q"})


# ---------------------------------------------------------------------------
# 计票与可信集合
# ---------------------------------------------------------------------------


class QuorumVotingTests(unittest.TestCase):
    def test_triple_voting_basic(self):
        out = evaluate_delta_consensus(
            30, 2,
            [
                ("ns-a", [(10, 20, "h10"), (20, 30, "h20")]),
                ("ns-b", [(20, 30, "h20"), (10, 20, "h10")]),  # 乱序
                ("ns-c", [(10, 20, "h10"), (20, 30, "h20-bad")]),
            ],
        )
        self.assertEqual(
            edges(out),
            [(10, 20, "h10"), (20, 30, "h20")],
        )
        self.assertEqual(out["chain_reason"], CONSENSUS_REACHED)
        self.assertTrue(out["target_reachable"])
        self.assertEqual(out["chain_end"], 30)
        by_triple = {(e["from_serial"], e["to_serial"], e["digest"]): e
                     for e in out["trusted_edges"]}
        self.assertEqual(by_triple[(10, 20, "h10")]["votes"], 3)
        self.assertEqual(by_triple[(20, 30, "h20")]["votes"], 2)
        self.assertEqual(by_triple[(10, 20, "h10")]["voters"],
                         ["ns-a", "ns-b", "ns-c"])

    def test_same_mirror_duplicate_counts_once(self):
        out = evaluate_delta_consensus(
            20, 2,
            [
                ("a", [(10, 20, "x"), (10, 20, "x")]),  # 同镜像重复
                ("b", [(10, 20, "x")]),
            ],
        )
        self.assertEqual(out["trusted_edges"][0]["votes"], 2)

    def test_digest_split_blocks_pair(self):
        # 同序号对两个摘要各 1 票，quorum=2：没有任一版本达法定人数，
        # 序号对整体不可信，双方上报都记为额外边（而非少数摘要）
        out = evaluate_delta_consensus(
            20, 2,
            [("a", [(10, 20, "x")]), ("b", [(10, 20, "y")])],
        )
        self.assertEqual(out["trusted_edges"], [])
        self.assertEqual(out["chain_reason"], CONSENSUS_GAP)
        self.assertFalse(out["target_reachable"])
        for item in out["mirrors"]:
            self.assertEqual(item["minority_digests"], [])
            self.assertEqual(len(item["extra_edges"]), 1)
            self.assertEqual(item["extra_edges"][0]["votes"], 1)

    def test_quorum_exceeds_mirrors_yields_empty_set(self):
        out = evaluate_delta_consensus(
            1, 3, [("a", [(0, 1, "x")]), ("b", [(0, 1, "x")])]
        )
        self.assertEqual(out["trusted_edges"], [])
        self.assertFalse(out["target_reachable"])
        self.assertEqual(out["chain_reason"], CONSENSUS_GAP)
        # 两票的边对每个上报镜像都是"额外边"（票数 2 < quorum 3）
        for item in out["mirrors"]:
            self.assertEqual(item["extra_edges"][0]["votes"], 2)

    def test_quorum_one_all_edges_trusted(self):
        out = evaluate_delta_consensus(
            2, 1, [("a", [(1, 2, "x"), (3, 4, "y")])]
        )
        self.assertEqual(len(out["trusted_edges"]), 2)


# ---------------------------------------------------------------------------
# 链组装：缺口 / 分叉 / 环路 / 回绕
# ---------------------------------------------------------------------------


class ChainAssemblyTests(unittest.TestCase):
    def test_gap_reason_and_prefix(self):
        out = evaluate_delta_consensus(
            30, 2,
            [
                ("a", [(10, 20, "x")]),
                ("b", [(10, 20, "x")]),
            ],
        )
        self.assertEqual(edges(out), [(10, 20, "x")])
        self.assertEqual(out["chain_reason"], CONSENSUS_GAP)
        self.assertEqual(out["chain_end"], 20)
        self.assertFalse(out["target_reachable"])

    def test_target_interior_is_reachable(self):
        # 10->20->30 全可信，目标 20 在链中间：可达，但链终点不是目标
        out = evaluate_delta_consensus(
            20, 2,
            [
                ("a", [(10, 20, "x"), (20, 30, "y")]),
                ("b", [(10, 20, "x"), (20, 30, "y")]),
            ],
        )
        self.assertTrue(out["target_reachable"])
        self.assertEqual(out["chain_reason"], CONSENSUS_GAP)  # 链终 30 后缺口
        self.assertEqual(out["chain_end"], 30)

    def test_to_serial_fork_not_selected(self):
        out = evaluate_delta_consensus(
            21, 2,
            [
                ("a", [(10, 20, "x")]),
                ("b", [(10, 20, "x")]),
                ("c", [(10, 21, "y")]),
                ("d", [(10, 21, "y")]),
            ],
        )
        # 根 10 即分叉点，无可沿根组装的边：链空
        self.assertEqual(edges(out), [])
        self.assertEqual(out["chain_reason"], CONSENSUS_FORK)
        self.assertFalse(out["target_reachable"])
        fork = out["forks"][0]
        self.assertEqual(fork["from_serial"], 10)
        self.assertEqual([b["to_serial"] for b in fork["branches"]], [20, 21])

    def test_chain_stops_before_fork(self):
        # 1->2 可信，2->3 与 2->4 各达 quorum：链在 2 处终止
        reports = [
            ("m1", [(1, 2, "a")]),
            ("m2", [(1, 2, "a"), (2, 3, "b"), (2, 4, "c")]),
            ("m3", [(1, 2, "a"), (2, 3, "b")]),
            ("m4", [(1, 2, "a"), (2, 4, "c")]),
        ]
        out = evaluate_delta_consensus(4, 2, reports)
        self.assertEqual(edges(out), [(1, 2, "a")])
        self.assertEqual(out["chain_end"], 2)
        self.assertEqual(out["chain_reason"], CONSENSUS_FORK)
        # 分叉点 2 之后分支上的目标 4 不可达（不静默选边）
        self.assertFalse(out["target_reachable"])
        fork = out["forks"][0]
        self.assertEqual(fork["from_serial"], 2)
        self.assertEqual([b["to_serial"] for b in fork["branches"]], [3, 4])

    def test_fork_node_itself_is_reachable(self):
        reports = [
            ("m1", [(1, 2, "a"), (2, 3, "b")]),
            ("m2", [(1, 2, "a"), (2, 4, "c")]),
        ]
        out = evaluate_delta_consensus(2, 2, reports)
        # 1->2 唯一可信；2 处分叉。目标 2 已到达
        self.assertTrue(out["target_reachable"])
        self.assertEqual(edges(out), [(1, 2, "a")])
        # 分支后的目标 3 / 4 都不可达
        for target in (3, 4):
            self.assertFalse(
                evaluate_delta_consensus(target, 2, reports)["target_reachable"]
            )

    def test_double_trusted_digest_is_fork(self):
        # 同序号对 (10,20) 两个不同摘要都达到 quorum：内容分叉，不静默二选一
        out = evaluate_delta_consensus(
            20, 2,
            [
                ("a", [(10, 20, "x")]),
                ("b", [(10, 20, "x")]),
                ("c", [(10, 20, "y")]),
                ("d", [(10, 20, "y")]),
            ],
        )
        self.assertEqual(out["chain_reason"], CONSENSUS_FORK)
        self.assertFalse(out["target_reachable"])
        fork = out["forks"][0]
        self.assertEqual(len(fork["branches"]), 1)
        self.assertEqual(fork["branches"][0]["to_serial"], 20)
        self.assertEqual(
            [d["digest"] for d in fork["branches"][0]["digests"]],
            ["x", "y"],
        )
        self.assertTrue(all(d["votes"] == 2
                            for d in fork["branches"][0]["digests"]))
        # 上报两个摘要的镜像互为"少数摘要"之外：两者都已在可信集合，无证据
        for item in out["mirrors"]:
            self.assertEqual(item["minority_digests"], [])
            self.assertEqual(item["extra_edges"], [])

    def test_loop_pure_cycle(self):
        cycle = [(0, HALF - 1, "p"), (HALF - 1, MAX_SERIAL - 1, "q"),
                 (MAX_SERIAL - 1, 0, "r")]
        out = evaluate_delta_consensus(5, 1, [("a", cycle)])
        # 纯环无根：最长链为空，环路证据完整列出
        self.assertEqual(edges(out), [])
        self.assertEqual(out["chain_reason"], CONSENSUS_LOOP)
        self.assertIsNone(out["chain_end"])
        self.assertFalse(out["target_reachable"])
        self.assertEqual(len(out["loops"]), 1)
        loop = [(e["from_serial"], e["to_serial"]) for e in out["loops"][0]]
        self.assertEqual(
            loop,
            [(0, HALF - 1), (HALF - 1, MAX_SERIAL - 1),
             (MAX_SERIAL - 1, 0)],
        )

    def test_loop_with_tail_targets_on_path_reachable(self):
        # 1 -> 2 -> 0 -> HALF-1 -> 2（2 回环）
        reports = [(
            "a",
            [(1, 2, "a"), (2, 0, "b"), (0, HALF - 1, "c"),
             (HALF - 1, 2, "d")],
        )]
        out = evaluate_delta_consensus(HALF - 1, 1, reports)
        # 路径 1,2,0,HALF-1 确定可达，之后 HALF-1->2 回到已访问的 2；
        # 最长链终点恰为目标，故 chain_reason=reached（环路另列证据）
        self.assertTrue(out["target_reachable"])
        self.assertEqual(out["chain_reason"], CONSENSUS_REACHED)
        self.assertEqual(out["chain_end"], HALF - 1)
        self.assertEqual(
            [(e["from_serial"], e["to_serial"]) for e in out["trusted_chain"]],
            [(1, 2), (2, 0), (0, HALF - 1)],
        )
        # 环路证据（规范化为从最小 from_serial 起）：0,H-1,2
        loop_nodes = [e["from_serial"] for e in out["loops"][0]]
        self.assertEqual(loop_nodes, [0, HALF - 1, 2])

    def test_loop_stop_reason_when_target_elsewhere(self):
        # 同一尾+环，目标不在确定到达的路径前缀上：chain_reason=loop
        reports = [(
            "a",
            [(1, 2, "a"), (2, 0, "b"), (0, HALF - 1, "c"),
             (HALF - 1, 2, "d")],
        )]
        out = evaluate_delta_consensus(999, 1, reports)
        self.assertFalse(out["target_reachable"])
        self.assertEqual(out["chain_reason"], CONSENSUS_LOOP)

    def test_wrap_around_chain(self):
        out = evaluate_delta_consensus(
            0, 2,
            [
                ("a", [(MAX_SERIAL, 0, "w")]),
                ("b", [(MAX_SERIAL, 0, "w")]),
            ],
        )
        self.assertEqual(edges(out), [(MAX_SERIAL, 0, "w")])
        self.assertTrue(out["target_reachable"])
        self.assertEqual(out["chain_reason"], CONSENSUS_REACHED)

    def test_longest_chain_picked_across_components(self):
        # 两个不相交分量：长链 1->2->3 与短链 10->11
        out = evaluate_delta_consensus(
            3, 1,
            [
                ("a", [(1, 2, "a"), (2, 3, "b"), (10, 11, "c")]),
            ],
        )
        self.assertEqual(edges(out), [(1, 2, "a"), (2, 3, "b")])

    def test_tie_break_deterministic(self):
        # 两条等长链：取边序列字典序最小（from_serial 较小）者
        reports = [("a", [(2, 9, "z"), (10, 20, "z")])]
        out1 = evaluate_delta_consensus(9, 1, reports)
        self.assertEqual(edges(out1), [(2, 9, "z")])
        # 与输入顺序无关：打乱后结果一致
        out2 = evaluate_delta_consensus(9, 1, [("a", list(reversed(reports[0][1])))])
        self.assertEqual(edges(out1), edges(out2))

    def test_merged_paths_no_fork(self):
        # 1->3 与 2->3 汇入同一节点 3->4：3 只有唯一出边，不构成分叉
        out = evaluate_delta_consensus(
            4, 1,
            [("a", [(1, 3, "x"), (2, 3, "y"), (3, 4, "z")])],
        )
        self.assertEqual(out["forks"], [])
        # 根为 1、2；两条链都可达 4
        self.assertTrue(out["target_reachable"])
        self.assertEqual(len(out["trusted_chain"]), 2)


# ---------------------------------------------------------------------------
# 逐镜像证据
# ---------------------------------------------------------------------------


class MirrorEvidenceTests(unittest.TestCase):
    def test_minority_digest_evidence(self):
        out = evaluate_delta_consensus(
            30, 2,
            [
                ("ns-a", [(10, 20, "h10"), (20, 30, "h20")]),
                ("ns-b", [(10, 20, "h10"), (20, 30, "h20")]),
                ("ns-c", [(10, 20, "h10"), (20, 30, "h20-bad")]),
            ],
        )
        a, b, c = out["mirrors"]
        for item in (a, b):
            self.assertEqual(item["missing"], [])
            self.assertEqual(item["minority_digests"], [])
            self.assertEqual(item["extra_edges"], [])
        self.assertEqual(c["missing"], [])
        minority = c["minority_digests"][0]
        self.assertEqual(
            (minority["from_serial"], minority["to_serial"],
             minority["digest"], minority["votes"]),
            (20, 30, "h20-bad", 1),
        )
        self.assertEqual(
            [(d["digest"], d["votes"])
             for d in minority["trusted_digests"]],
            [("h20", 2)],
        )

    def test_missing_edge_evidence(self):
        # ns-c 只上报了后半段：前半段可信序号对记为缺失
        out = evaluate_delta_consensus(
            30, 2,
            [
                ("ns-a", [(10, 20, "h10"), (20, 30, "h20")]),
                ("ns-b", [(10, 20, "h10"), (20, 30, "h20")]),
                ("ns-c", [(20, 30, "h20")]),
            ],
        )
        c = out["mirrors"][2]
        self.assertEqual(len(c["missing"]), 1)
        missing = c["missing"][0]
        self.assertEqual((missing["from_serial"], missing["to_serial"]),
                         (10, 20))
        self.assertEqual(missing["digests"][0]["digest"], "h10")
        self.assertEqual(missing["digests"][0]["votes"], 2)

    def test_extra_edge_evidence_with_votes(self):
        # quorum=3：两票的 (40,50) 不达法定人数，记为额外边并给出票数
        out = evaluate_delta_consensus(
            20, 3,
            [
                ("a", [(10, 20, "x"), (40, 50, "z")]),
                ("b", [(10, 20, "x"), (40, 50, "z")]),
                ("c", [(10, 20, "x")]),
            ],
        )
        a = out["mirrors"][0]
        self.assertEqual(len(a["extra_edges"]), 1)
        self.assertEqual(a["extra_edges"][0]["from_serial"], 40)
        self.assertEqual(a["extra_edges"][0]["votes"], 2)
        # (10,20) 三票可信：a 已上报，无缺失
        self.assertEqual(a["missing"], [])

    def test_reported_edges_dedup_count_and_mirror_order(self):
        out = evaluate_delta_consensus(
            20, 1,
            [
                ("second", [(10, 20, "x"), (10, 20, "x")]),
                ("first", []),
            ],
        )
        self.assertEqual([m["mirror"] for m in out["mirrors"]],
                         ["second", "first"])
        self.assertEqual(out["mirrors"][0]["reported_edges"], 1)
        self.assertEqual(out["mirrors"][1]["reported_edges"], 0)

    def test_evidence_order_is_stable(self):
        # 同一镜像乱序上报多个额外边 / 少数摘要，输出必须按三元组升序
        out = evaluate_delta_consensus(
            20, 3,
            [
                ("a", [(30, 40, "c"), (10, 20, "a"), (20, 30, "b")]),
                ("b", []),
            ],
        )
        extras = out["mirrors"][0]["extra_edges"]
        keys = [(e["from_serial"], e["to_serial"], e["digest"]) for e in extras]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(
            [(e["from_serial"], e["to_serial"]) for e in extras],
            [(10, 20), (20, 30), (30, 40)],
        )

    def test_minority_digest_not_counted_as_missing_or_extra(self):
        # 序号对 (10,20) 的 x 摘要达法定人数（2 票），y 仅 1 票：
        # y 上报方记为少数摘要，既不算缺失也不算额外边
        out = evaluate_delta_consensus(
            20, 2,
            [
                ("a", [(10, 20, "x")]),
                ("b", [(10, 20, "x")]),
                ("c", [(10, 20, "y")]),
            ],
        )
        c = out["mirrors"][2]
        self.assertEqual(c["missing"], [])
        self.assertEqual(len(c["minority_digests"]), 1)
        self.assertEqual(c["extra_edges"], [])
        # x 的两个上报方无任何偏差证据
        for item in out["mirrors"][:2]:
            self.assertEqual(item["missing"], [])
            self.assertEqual(item["minority_digests"], [])
            self.assertEqual(item["extra_edges"], [])


# ---------------------------------------------------------------------------
# 路由层（FastAPI 可用时）
# ---------------------------------------------------------------------------

try:
    from fastapi import HTTPException as _HTTPException

    from app.main import delta_consensus
    from app.schemas import DeltaConsensusRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class DeltaConsensusEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return delta_consensus(
            DeltaConsensusRequest.model_validate(payload)
        )

    def test_endpoint_full_consensus(self):
        payload = {
            "target": 30,
            "quorum": 2,
            "mirrors": [
                {"name": "ns-a", "deltas": [
                    {"from_serial": 20, "to_serial": 30, "digest": "h20"},
                    {"from_serial": 10, "to_serial": 20, "digest": "h10"}]},
                {"name": "ns-b", "deltas": [
                    {"from_serial": 10, "to_serial": 20, "digest": "h10"},
                    {"from_serial": 20, "to_serial": 30, "digest": "h20"}]},
                {"name": "ns-c", "deltas": [
                    {"from_serial": 10, "to_serial": 20, "digest": "h10"},
                    {"from_serial": 20, "to_serial": 30,
                     "digest": "h20-bad"}]},
            ],
        }
        resp = self._request(payload)
        self.assertTrue(resp.target_reachable)
        self.assertEqual(resp.chain_reason, "reached")
        self.assertEqual(
            [(e.from_serial, e.to_serial, e.digest)
             for e in resp.trusted_chain],
            [(10, 20, "h10"), (20, 30, "h20")],
        )
        self.assertEqual([m.mirror for m in resp.mirrors],
                         ["ns-a", "ns-b", "ns-c"])
        minority = resp.mirrors[2].minority_digests[0]
        self.assertEqual(minority.digest, "h20-bad")
        self.assertEqual(minority.votes, 1)

    def test_endpoint_non_newer_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request({
                "target": 1,
                "quorum": 1,
                "mirrors": [{"name": "a", "deltas": [
                    {"from_serial": 0, "to_serial": HALF, "digest": "x"}]}],
            })
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_endpoint_mirror_conflict_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request({
                "target": 3,
                "quorum": 1,
                "mirrors": [{"name": "a", "deltas": [
                    {"from_serial": 1, "to_serial": 2, "digest": "x"},
                    {"from_serial": 1, "to_serial": 3, "digest": "x"}]}],
            })
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "mirror_version_conflict"
        )

    def test_endpoint_duplicate_mirror_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request({
                "target": 1,
                "quorum": 1,
                "mirrors": [
                    {"name": "a", "deltas": []},
                    {"name": "a", "deltas": []},
                ],
            })
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_fork_response_shape(self):
        resp = self._request({
            "target": 21,
            "quorum": 2,
            "mirrors": [
                {"name": "a", "deltas": [
                    {"from_serial": 10, "to_serial": 20, "digest": "x"}]},
                {"name": "b", "deltas": [
                    {"from_serial": 10, "to_serial": 20, "digest": "x"}]},
                {"name": "c", "deltas": [
                    {"from_serial": 10, "to_serial": 21, "digest": "y"}]},
                {"name": "d", "deltas": [
                    {"from_serial": 10, "to_serial": 21, "digest": "y"}]},
            ],
        })
        self.assertFalse(resp.target_reachable)
        self.assertEqual(resp.chain_reason, "fork")
        self.assertEqual(resp.trusted_chain, [])
        self.assertEqual(resp.forks[0].from_serial, 10)

    def test_endpoint_invalid_quorum_422(self):
        with self.assertRaises(Exception):
            DeltaConsensusRequest.model_validate({
                "target": 1,
                "quorum": 0,
                "mirrors": [{"name": "a", "deltas": []}],
            })

    def test_endpoint_blank_digest_422(self):
        with self.assertRaises(Exception):
            DeltaConsensusRequest.model_validate({
                "target": 1,
                "quorum": 1,
                "mirrors": [{"name": "a", "deltas": [
                    {"from_serial": 0, "to_serial": 1, "digest": "   "}],
                }],
            })


if __name__ == "__main__":
    unittest.main()
