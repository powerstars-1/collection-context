"""The benchmark is tested against actual local Store/Gateway, not a replacement search."""

import copy
import socket
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.gateway import ReadGateway
from collection_context.application.service import ContextService
from collection_context.infrastructure.files import SafeFiles
from collection_context.library.store import LibraryStore
from tools import retrieval_acceptance as benchmark


@pytest.fixture(autouse=True)
def no_cloud_source_or_subprocess(monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("The acceptance unit suite must stay offline and use no subprocess")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture(scope="module")
def fixture_library(tmp_path_factory):
    root = tmp_path_factory.mktemp("original-p02") / "library 中文"
    built = benchmark.build_library(root)
    assert built["record_count"] == 1000
    assert built["artifact_count"] == 11
    return root


def test_deterministic_frozen_1000_records_and_30_answers():
    first, second = benchmark.corpus(), benchmark.corpus()
    assert first == second and len(first) == 1000
    assert len({record.key for record in first}) == 1000
    assert len(set(benchmark.identities().values())) == 1000
    assert benchmark.fixture_identity() == benchmark.fixture_identity()
    assert len(benchmark.tasks()) == 30
    assert len({task.id for task in benchmark.tasks()}) == 30
    assert all(set(task.expected_keys) <= set(benchmark.identities()) for task in benchmark.tasks())
    assert sum(task.category == "paraphrase" for task in benchmark.tasks()) == 5
    with pytest.raises(FrozenInstanceError):
        setattr(first[0], "title", "changed")
    with pytest.raises(FrozenInstanceError):
        setattr(benchmark.tasks()[0], "query", "easier")


def test_required_categories_and_uninvented_action_time():
    categories = {task.category for task in benchmark.tasks()}
    assert {
        "tool",
        "screen_prompt",
        "scattered_terms",
        "source",
        "recent_relationship",
        "missing",
        "no_result_cross_record",
        "paraphrase",
    } <= categories
    records = {record.key: record for record in benchmark.corpus()}
    assert records["unknown_time"].published_at > "2026-02-01"
    assert all(relation.action_at is None for relation in records["unknown_time"].relations)
    assert records["creator"].relations[0].kind == "creator"
    assert records["creator"].relations[1].kind == "liked"
    assert records["missing_audio"].artifacts == ()
    assert records["missing_screen"].artifacts == ()


@pytest.mark.parametrize(
    "values,expected", [([], None), ([1], 1), (list(range(1, 21)), 19), (list(range(1, 31)), 29)]
)
def test_nearest_rank_p95_no_interpolation(values, expected):
    original = list(values)
    assert benchmark.p95(values) == expected
    assert values == original


def test_real_gateway_fixed_answers_and_honest_paraphrase_failures(fixture_library, monkeypatch):
    handle = LibraryStore(fixture_library)
    try:
        snapshot = handle.snapshot()
        assert len(snapshot["items"]) == 1000

        def no_write(*_, **__):
            pytest.fail("Search/read/status must not write benchmark or reindex")

        monkeypatch.setattr(SafeFiles, "write", no_write)
        access = ReadGateway(ContextService(handle))
        rows = [
            benchmark.evaluate(task, access.dispatch("search_collections", task.arguments()), access)
            for task in benchmark.tasks()
        ]
        assert all(row["passed"] for row in rows[:25])
        # The saved baseline records today's five misses. Future legitimate
        # retrieval improvements may hit those unchanged expected answers.
        assert all(not row["passed"] for row in rows[25:] if row["missing_refs"])
        assert sum(row["top5_hit"] is True for row in rows) >= 23
        assert sum(row["no_result_correct"] is True for row in rows) == 2
        assert sum(len(row["unexpected_refs"]) for row in rows) == 0
        assert all(row["zero_model_calls"] for row in rows)
        assert handle.snapshot() == snapshot
    finally:
        handle.close()


def test_positive_lexical_miss_is_failed_without_changing_expected_answers(fixture_library):
    handle = LibraryStore(fixture_library)
    try:
        task = benchmark.tasks()[25]
        response = {
            "ok": True,
            "data": {"items": [], "total_matches": 0, "model_requests": 0, "semantic_search": False},
        }
        row = benchmark.evaluate(task, response, ReadGateway(ContextService(handle)))
        assert row["passed"] is False and row["top5_hit"] is False
        assert row["missing_refs"] == row["expected_refs"] == [benchmark.identities()["figma"]]
        assert row["unexpected_refs"] == [] and row["search_error"] is None
    finally:
        handle.close()


def test_missing_and_not_applicable_evidence_never_presented_complete(fixture_library):
    handle = LibraryStore(fixture_library)
    try:
        access = ReadGateway(ContextService(handle))
        selected = [task for task in benchmark.tasks() if task.id in {"q15", "q23", "q24"}]
        rows = [
            benchmark.evaluate(task, access.dispatch("search_collections", task.arguments()), access)
            for task in selected
        ]
        checks = [check for row in rows for check in row["evidence_checks"]]
        assert {check["error"] for check in checks} >= {"artifact_missing", "artifact_not_applicable"}
        assert all(row["passed"] for row in rows)
    finally:
        handle.close()


def test_wrong_association_is_counted_not_dropped(fixture_library):
    handle = LibraryStore(fixture_library)
    try:
        access = ReadGateway(ContextService(handle))
        task = benchmark.tasks()[0]
        result = access.dispatch("search_collections", task.arguments())
        unexpected = benchmark.identities()["comfy"]
        altered = copy.deepcopy(result)
        altered["data"]["items"].append({"material_ref": unexpected})
        row = benchmark.evaluate(task, altered, access)
        assert row["top5_hit"] is True
        assert row["unexpected_refs"] == [unexpected] and row["passed"] is False
    finally:
        handle.close()


@pytest.mark.parametrize(
    "field,value",
    [("source_url", "https://www.douyin.com/video/1"), ("title", "别条资料的标题"), ("relations", [])],
)
def test_correct_ref_with_wrong_source_metadata_fails(fixture_library, field, value):
    handle = LibraryStore(fixture_library)
    try:
        access = ReadGateway(ContextService(handle))
        task = benchmark.tasks()[0]
        altered = access.dispatch("search_collections", task.arguments())
        altered["data"]["items"][0][field] = value
        row = benchmark.evaluate(task, altered, access)
        assert row["top5_hit"] is True and row["passed"] is False
        assert row["metadata_association_errors"] == [benchmark.identities()["figma"]]
    finally:
        handle.close()


def test_duplicate_same_ref_does_not_count_as_valid_dedup(fixture_library):
    handle = LibraryStore(fixture_library)
    try:
        access = ReadGateway(ContextService(handle))
        task = benchmark.tasks()[19]
        altered = access.dispatch("search_collections", task.arguments())
        altered["data"]["items"].append(copy.deepcopy(altered["data"]["items"][0]))
        row = benchmark.evaluate(task, altered, access)
        assert row["top5_hit"] is True and row["passed"] is False
        assert row["duplicate_refs"] == [benchmark.identities()["creator"]]
    finally:
        handle.close()


def test_evidence_failure_changes_score_not_expected_answers(fixture_library):
    handle = LibraryStore(fixture_library)
    try:
        access = ReadGateway(ContextService(handle))
        task = benchmark.tasks()[3]
        response = access.dispatch("search_collections", task.arguments())

        class BrokenRead:
            def dispatch(self, action, arguments):
                if action == "read_collection":
                    return {
                        "ok": True,
                        "data": {"material_ref": arguments["material_ref"], "text": "wrong body"},
                        "error": None,
                    }
                return access.dispatch(action, arguments)

        row = benchmark.evaluate(task, response, cast(ReadGateway, BrokenRead()))
        assert row["top5_hit"] is True
        assert row["evidence_passed"] is False and row["passed"] is False
        assert row["expected_refs"] == [benchmark.identities()["whisper"]]
    finally:
        handle.close()


def test_gateway_error_fails_task_and_no_result_control(fixture_library):
    handle = LibraryStore(fixture_library)
    try:
        access = ReadGateway(ContextService(handle))
        response = {"ok": False, "data": None, "error": {"code": "index_unavailable"}}
        for task in (benchmark.tasks()[0], benchmark.tasks()[24]):
            row = benchmark.evaluate(task, response, access)
            assert row["passed"] is False and row["search_error"] == "index_unavailable"
            assert row["zero_model_calls"] is False
    finally:
        handle.close()


@pytest.mark.parametrize("relative", ["existing", "existing-library"])
def test_never_reads_or_overwrites_existing_output(tmp_path, relative, monkeypatch):
    output = tmp_path / relative
    output.mkdir()
    private = output / "unrelated.txt"
    private.write_text("untouched sentinel")

    def no_open(*_, **__):
        pytest.fail("Existing output must be rejected before LibraryStore reads")

    monkeypatch.setattr(benchmark, "build_library", no_open)
    with pytest.raises(ContextError) as caught:
        benchmark.run_acceptance(output, cold_processes=False)
    assert caught.value.code == "benchmark_output_not_new"
    assert private.read_text() == "untouched sentinel"


@pytest.mark.parametrize("output", [Path("relative"), Path("/")])
def test_invalid_output_has_no_directory_side_effect(output):
    with pytest.raises(ContextError) as caught:
        benchmark.run_acceptance(output, cold_processes=False)
    assert caught.value.code == "benchmark_output_not_new"


@pytest.mark.parametrize("rounds", [0, 11, True, "3"])
def test_invalid_rounds_before_build(tmp_path, rounds):
    output = tmp_path / "new"
    with pytest.raises(ContextError) as caught:
        benchmark.run_acceptance(output, warm_rounds=rounds, cold_processes=False)
    assert caught.value.code == "benchmark_argument" and not output.exists()


def test_unknown_cold_task_or_foreign_directory_rejected(tmp_path):
    output = tmp_path / "foreign"
    output.mkdir()
    with pytest.raises(ContextError):
        benchmark._cold_child(output, "not-a-fixed-task")
    with pytest.raises(ContextError):
        benchmark._cold_child(output, "q01")
    assert list(output.iterdir()) == []


def test_cold_measurement_reports_process_errors_without_fake_success(tmp_path, monkeypatch):
    class Failure:
        returncode = 1
        stdout = "private underlying error"

    calls = []

    def failed(args, **kwargs):
        calls.append((args, kwargs))
        return Failure()

    monkeypatch.setattr(subprocess, "run", failed)
    report = benchmark.measure_cold(tmp_path)
    assert report["process_count"] == report["errors"] == 30
    assert report["first_query_p95_ms"] is None
    assert "private underlying error" not in str(report)
    assert len(calls) == 30
    assert all(call[0][0] == benchmark.sys.executable for call in calls)
    assert all("DASHSCOPE_API_KEY" not in call[1]["env"] for call in calls)
    assert "OS/page cache not purged" in report["method"]


@pytest.mark.parametrize("cold_state", ["skipped", "error", "mismatch", "incomplete", "complete"])
def test_overall_pass_requires_complete_matching_cold_measurement(tmp_path, monkeypatch, cold_state):
    """Isolate report/exit scoring; real Store/Gateway behavior is covered above.

    Perfect synthetic warm rows cannot turn incomplete or failing cold evidence
    into an overall pass. This test does not rerun or replace the saved baseline.
    """

    class Handle:
        def __init__(self, _root):
            pass

        def snapshot(self):
            return {}

        def close(self):
            pass

    class Access:
        def __init__(self, _service):
            pass

        def dispatch(self, _name, _arguments):
            return {"ok": True, "data": {"items": []}}

    def perfect_row(task, _response, _access):
        return {
            "id": task.id,
            "passed": True,
            "expected_refs": ["fixed_scoring_only"],
            "actual_top5": [],
            "unexpected_refs": [],
            "metadata_association_errors": [],
            "duplicate_refs": [],
            "top5_hit": True,
            "no_result_correct": None,
        }

    samples = [{"id": task.id, "ok": True, "actual_top5": []} for task in benchmark.tasks()]
    errors = 0
    if cold_state == "error":
        samples[0]["ok"] = False
        errors = 1
    elif cold_state == "mismatch":
        samples[0]["actual_top5"] = ["different_result"]
    elif cold_state == "incomplete":
        samples.pop()
    monkeypatch.setattr(benchmark, "LibraryStore", Handle)
    monkeypatch.setattr(benchmark, "ReadGateway", Access)
    monkeypatch.setattr(benchmark, "evaluate", perfect_row)
    # platform.processor can spawn uname on macOS; scoring tests stay fully
    # in-process, so hardware metadata is explicitly synthetic here only.
    monkeypatch.setattr(benchmark.platform, "processor", lambda: "scoring_only")
    monkeypatch.setattr(
        benchmark, "build_library", lambda _root: {"record_count": 1000, "workspace_id": "scoring_only"}
    )
    monkeypatch.setattr(
        benchmark, "measure_cold", lambda _output: {"state": "measured", "errors": errors, "samples": samples}
    )
    report = benchmark.run_acceptance(tmp_path / "report", cold_processes=cold_state != "skipped")
    assert report["accuracy"]["tasks_correct"] == 30
    assert report["acceptance_complete"] is (cold_state == "complete")
    assert report["targets_met"] is (cold_state == "complete")


def test_diagnostic_skip_cold_cannot_exit_as_accepted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        benchmark,
        "run_acceptance",
        lambda *_args, **_kwargs: {"targets_met": False, "acceptance_complete": False},
    )
    assert benchmark.main(["--output-dir", str(tmp_path / "new"), "--skip-cold"]) == 2
    assert '"targets_met": false' in capsys.readouterr().out
