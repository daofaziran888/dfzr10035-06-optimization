"""分页试采边界校验的回归测试（标准库 unittest，无额外依赖）。

覆盖题目要求的四类情形：
1. 零行页带有效 nextToken 时，下一页的数据不能丢；
2. 空页与已确认尾页必须区分；
3. 重复 token 达到安全上限后停止循环取数，且不重复累计；
4. 中断后按上次核对的页和顺序续取，重复 token 判定不重复请求。
"""
from __future__ import annotations

import unittest

from app.services.pagination_trial import (
    PaginationTrial,
    PaginationTrialService,
    classify_boundary,
    extract_records,
    extract_token,
    probe_fetcher,
    run_pagination_check,
    run_pagination_trial,
)
from app.store import Store


class BoundaryClassificationTests(unittest.TestCase):
    def test_empty_with_token_is_not_tail(self):
        self.assertEqual(classify_boundary(0, "tok", None), "EMPTY_WITH_NEXT")

    def test_empty_without_token_is_confirmed_tail(self):
        self.assertEqual(classify_boundary(0, None, None), "EMPTY_TAIL")
        self.assertEqual(classify_boundary(0, None, False), "EMPTY_TAIL")

    def test_empty_claiming_more_without_token_is_unresolved(self):
        self.assertEqual(classify_boundary(0, None, True), "EMPTY_UNRESOLVED")

    def test_rows_without_token_is_data_tail(self):
        self.assertEqual(classify_boundary(3, None, None), "DATA_TAIL")


class HistoricalPaginationTests(unittest.TestCase):
    def test_legacy_token_and_data_fields(self):
        sample = {"rows": [{"id": 1}], "next_cursor": "c1"}
        rows, data_path = extract_records(sample)
        token, token_path = extract_token(sample)
        self.assertEqual(rows, [{"id": 1}])
        self.assertEqual(data_path, "rows")
        self.assertEqual(token, "c1")
        self.assertEqual(token_path, "next_cursor")

    def test_nested_envelope_and_header_token(self):
        sample = {"data": {"items": [{"id": 2}], "hasMore": True},
                  "headers": {"X-Next-Token": "h1"}}
        rows, _ = extract_records(sample)
        token, token_path = extract_token(sample)
        self.assertEqual(rows, [{"id": 2}])
        self.assertEqual(token, "h1")
        self.assertEqual(token_path, "headers.X-Next-Token")

    def test_single_object_is_one_record(self):
        sample = {"data": {"id": 9}, "nextToken": "n"}
        rows, _ = extract_records(sample)
        self.assertEqual(rows, [{"id": 9}])


class CollectionFlowTests(unittest.TestCase):
    def _samples(self):
        return [
            {"data": [{"id": 1}], "nextToken": "A"},
            {"data": [], "nextToken": "B"},              # 空页但 token 有效
            {"data": [{"id": 2}, {"id": 3}], "nextToken": "B"},  # token 重复
        ]

    def test_data_after_empty_page_is_collected(self):
        result = run_pagination_trial({"trial_id": "T1", "repeat_limit": 2}, self._samples())
        self.assertTrue(result["empty_page_seen"])
        self.assertFalse(result["tail_confirmed"])
        self.assertEqual(
            [row["data"]["id"] for row in result["records_after_first_empty"]],
            [2, 3],
            "第二页空页之后的数据必须落在可辨识的采集结果里",
        )
        self.assertEqual(result["row_count"], 3)
        self.assertEqual([p["boundary"] for p in result["pages"]],
                         ["DATA", "EMPTY_WITH_NEXT", "REPEAT_LIMIT"])

    def test_duplicate_token_stops_without_extra_requests(self):
        result = run_pagination_trial({"trial_id": "T2", "repeat_limit": 2}, self._samples())
        self.assertTrue(result["loop_stopped"])
        self.assertEqual(result["stop_reason"], "REPEAT_LIMIT")
        # 3 页样本只真正请求了前 2 页，重复 token 页被抑制。
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["duplicate_pages"], [3])
        self.assertEqual(result["token_requests"], {"A": 1})

    def test_duplicate_page_does_not_double_count(self):
        samples = [
            {"data": [{"id": 1}], "nextToken": "A"},
            {"data": [{"id": 2}, {"id": 3}], "nextToken": "B"},
            {"data": [{"id": 2}, {"id": 3}], "nextToken": "B"},
        ]
        result = run_pagination_trial({"trial_id": "T3", "repeat_limit": 2}, samples)
        self.assertEqual(result["row_count"], 3)

    def test_confirmed_tail_is_distinct_from_empty_page(self):
        samples = [
            {"data": [{"id": 1}], "nextToken": "A"},
            {"data": [], "nextToken": "B"},
            {"data": [{"id": 2}], "nextToken": None},
        ]
        result = run_pagination_trial({"trial_id": "T4"}, samples)
        self.assertTrue(result["tail_confirmed"])
        self.assertEqual(result["stop_reason"], "DATA_TAIL")
        self.assertEqual(result["final_source_page"], 3)

        empty_tail = run_pagination_trial({"trial_id": "T5"}, [
            {"data": [{"id": 1}], "nextToken": "A"},
            {"data": [], "nextToken": None},
        ])
        self.assertTrue(empty_tail["tail_confirmed"])
        self.assertEqual(empty_tail["empty_tail_pages"], [2])

    def test_unresolved_empty_page_keeps_credential(self):
        samples = [
            {"data": [{"id": 1}], "nextToken": "A"},
            {"data": [], "hasMore": True},
            {"data": [{"id": 2}], "nextToken": None},
        ]
        result = run_pagination_trial({"trial_id": "T6"}, samples)
        self.assertEqual(result["stop_reason"], "EMPTY_UNRESOLVED")
        self.assertFalse(result["tail_confirmed"])
        self.assertEqual(result["credential"]["next_page"], 2)
        self.assertEqual(result["credential"]["resume_from"]["page"], 2)
        self.assertEqual(result["row_count"], 1)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.service = PaginationTrialService(store_obj=self.store)

    def test_interrupted_trial_resumes_in_page_order(self):
        pages = [
            {"data": [{"id": 1}], "nextToken": "A"},
            {"data": [], "nextToken": "B"},
            {"data": [{"id": 2}, {"id": 3}], "nextToken": "C"},
            {"data": [{"id": 4}], "nextToken": "C"},
        ]
        calls: list[tuple[str | None, int]] = []

        def failing(token, page_no, credential):
            calls.append((token, page_no))
            if page_no == 3:
                raise RuntimeError("connection reset")
            return pages[page_no - 1]

        first = self.service.run_trial({"trial_id": "R1", "repeat_limit": 2}, fetcher=failing)
        self.assertEqual(first["status"], "待续采")
        self.assertEqual(first["下次页"], 3)
        self.assertEqual(first["token"], "B")
        credential = first["state"]["credential"]
        self.assertEqual(credential["next_page"], 3)
        self.assertEqual(credential["next_token"], "B")
        self.assertEqual(credential["next_sequence"], 3)
        self.assertEqual(calls, [(None, 1), ("A", 2), ("B", 3)])

        resumed = self.service.resume_trial("R1", samples=pages[2:])
        self.assertTrue(resumed["state"]["resumed"])
        # 从第 3 页、按原顺序续取，重复 token 只出现在第 4 页且未重复请求。
        self.assertEqual(resumed["raw_state"]["requested_set"], [1, 2, 3])
        self.assertEqual(resumed["raw_state"]["next_page"], 4)
        self.assertEqual(resumed["停止原因"], "REPEAT_LIMIT")
        self.assertEqual([t["页"] for t in resumed["todos"]], [1, 2, 3, 4])
        self.assertEqual(
            [(r["page"], r["data"]["id"]) for r in resumed["state"]["records"]],
            [(1, 1), (3, 2), (3, 3), (4, 4)],
        )
        # 恢复不能把中断占位页留成第二条待办。
        self.assertEqual(
            [(t["页"], t["边界判定"]) for t in resumed["todos"]],
            [(1, "DATA"), (2, "EMPTY_WITH_NEXT"), (3, "DATA"), (4, "REPEAT_LIMIT")],
        )

    def test_resume_keeps_duplicate_suppressed(self):
        pages = [
            {"data": [{"id": 1}], "nextToken": "A"},
            {"data": [{"id": 2}], "nextToken": "A"},
        ]
        first = self.service.run_trial(
            {"trial_id": "R2", "repeat_limit": 2},
            fetcher=lambda token, page, cred: pages[page - 1],
            )
        # 同一 token 第二次出现即判定循环，不再请求。
        self.assertEqual(first["state"]["requests"], 1)
        self.assertEqual(first["停止原因"], "REPEAT_LIMIT")
        self.assertEqual(first["state"]["duplicate_pages"], [2])
        self.assertEqual(first["state"]["row_count"], 2)

        # 再次 resume 不应把重复 token 的页当成待取页去请求。
        resumed = self.service.resume_trial("R2", samples=[])
        state = resumed["state"]
        self.assertEqual(state["requests"], 1)
        self.assertEqual(state["duplicate_pages"], [2])
        # 已熔断的试采恢复时不再取数，保留原判定。
        self.assertEqual(state["stop_reason"], "REPEAT_LIMIT")

    def test_pending_credential_survives_roundtrip(self):
        trial = PaginationTrial({"trial_id": "R3", "repeat_limit": 2})
        trial.step({"data": [], "nextToken": "keep-me"})
        exported = trial.export_state()
        restored = PaginationTrial.from_state(trial.config, exported)
        self.assertEqual(restored.credential()["next_token"], "keep-me")
        self.assertEqual(restored.credential()["next_page"], 2)
        self.assertEqual(restored.credential()["next_sequence"], 2)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.service = PaginationTrialService(store_obj=self.store)

    def test_field_diffs_and_todos_align(self):
        result = self.service.run_trial(
            {"trial_id": "ACC1", "endpoint_id": "ENDP-X", "repeat_limit": 2},
            [
                {"data": [{"id": 1}], "nextToken": "A"},
                {"data": [], "nextToken": "B"},
                {"data": [{"id": 2}], "nextToken": "B"},
            ],
        )
        checklist = self.service.pagination_checklist("ACC1")
        self.assertTrue(checklist["consistent"])
        self.assertEqual(len(checklist["checklist"]), len(result["state"]["pages"]))
        self.assertEqual(checklist["todo_count"], len(result["state"]["pages"]))

        accepted = self.service.accept_trial("ACC1")
        self.assertTrue(accepted["blocking"])
        self.assertEqual(accepted["schema_diff_created"], len(accepted["field_diffs"]))
        diff_fields = {d["变更字段"] for d in self.store.rows("schema_diff") if d.get("试采编号") == "ACC1"}
        self.assertIn("分页边界:零行页", diff_fields)
        self.assertIn("分页边界:重复 token", diff_fields)
        # 试采清单与投产待办对得上：每页都能找到对应待办。
        renamed = self.service.pagination_checklist("ACC1")
        self.assertTrue(renamed["consistent"])
        self.assertTrue(all(item["todo_id"] for item in renamed["checklist"]))

    def test_tail_confirmed_trial_can_be_accepted(self):
        result = self.service.run_trial(
            {"trial_id": "ACC2", "endpoint_id": "ENDP-Y"},
            [
                {"data": [{"id": 1}], "nextToken": "A"},
                {"data": [], "nextToken": "B"},
                {"data": [{"id": 2}], "nextToken": None},
            ],
        )
        self.assertTrue(result["state"]["tail_confirmed"])
        accepted = self.service.accept_trial("ACC2")
        self.assertFalse(accepted["blocking"])
        self.assertTrue(accepted["tail_confirmed"])
        self.assertEqual(self.service.describe_session("ACC2")["status"], "已验收")
        # 可辨识的采集结果包含空页之后的数据。
        self.assertEqual(
            [r["data"]["id"] for r in result["state"]["records_after_first_empty"]], [2]
        )


class ProbeEndpointTests(unittest.TestCase):
    def test_probe_sequence_one_row_empty_page_more_data_repeat_token(self):
        result = run_pagination_trial(
            {"trial_id": "PROBE", "endpoint_id": "ENDP-PAGE-PROBE", "repeat_limit": 2},
            fetcher=probe_fetcher,
        )
        self.assertEqual([p["page"] for p in result["pages"]], [1, 2, 3, 4])
        self.assertEqual(
            [p["boundary"] for p in result["pages"]],
            ["DATA", "EMPTY_WITH_NEXT", "DATA", "REPEAT_LIMIT"],
        )
        self.assertEqual(result["requests"], 3)
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(
            [r["data"]["id"] for r in result["records_after_first_empty"]],
            ["row-2", "row-3"],
        )
        self.assertTrue(result["loop_stopped"])
        self.assertFalse(result["tail_confirmed"])
        # 恢复位置能看到重复 token 所在的页。
        self.assertEqual(result["credential"]["resume_from"]["page"], 4)
        self.assertEqual(result["credential"]["resume_from"]["token"], "probe-token-tail")


if __name__ == "__main__":
    unittest.main()
