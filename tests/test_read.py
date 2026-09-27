"""Acceptance test suite for PRTS corpus reading and evidence tracking."""

from __future__ import annotations

import re
from typing import Any

import pytest

from prts_corpus.errors import ContractError
from prts_corpus.evidence import EvidenceRegistry
from prts_corpus.read import (
    execute_read,
    model_read_to_contract,
    project_read_public,
    render_read,
)
from prts_corpus.store import CorpusStore
from tests.conftest import run_async


def test_read_around_window(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """around mode by title + line returns ±before/after window around center line."""
    title = full_fixture["expected"]["titles"]["story"]
    raw_args = {
        "title": title,
        "line": 10,
        "before": 2,
        "after": 2,
    }
    contract = run_async(model_read_to_contract(module_store, raw_args, ["arknights"]))
    contract["intent_id"] = "intent-around-test"

    res = run_async(execute_read(module_store, contract))
    assert res["status"] == "ok"

    lines = res.get("content", {}).get("lines", [])
    assert len(lines) == 5
    line_numbers = [line["line_number"] for line in lines]
    assert line_numbers == [8, 9, 10, 11, 12]


def test_read_range(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """range mode returns exact line span."""
    contract = {
        "intent_id": "intent-range-test",
        "locator": {"document_id": full_fixture["expected"]["document_ids"]["story"]},
        "selection": {"mode": "range", "start_line": 5, "end_line": 9},
    }
    res = run_async(execute_read(module_store, contract))
    assert res["status"] == "ok"

    lines = res.get("content", {}).get("lines", [])
    assert len(lines) == 5
    assert [line["line_number"] for line in lines] == [5, 6, 7, 8, 9]


def test_read_document_mode_continuation_round_trip(
    full_fixture: dict[str, Any], module_store: CorpusStore
) -> None:
    """document mode with max_lines produces continuation that can be fed into next read."""
    title = full_fixture["expected"]["titles"]["story"]
    args = {"title": title, "mode": "document", "max_lines": 5}
    contract = run_async(model_read_to_contract(module_store, args, ["arknights"]))
    contract["intent_id"] = "intent-doc-p1"

    res = run_async(execute_read(module_store, contract))
    assert res["status"] == "ok"

    pub = project_read_public(res)
    assert pub["page"]["has_more"] is True
    continuation = pub["page"]["continuation"]
    assert continuation is not None
    assert continuation.get("line") == 6

    # Round-trip: Feed continuation back through model_read_to_contract
    contract2 = run_async(model_read_to_contract(module_store, continuation, ["arknights"]))
    contract2["intent_id"] = "intent-doc-p2"
    res2 = run_async(execute_read(module_store, contract2))
    assert res2["status"] == "ok"

    pub2 = project_read_public(res2)
    p2_lines = pub2["primary"]["lines"]
    assert len(p2_lines) > 0
    assert p2_lines[0]["line"] == 6


def test_read_wiki_section(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """Wiki section='简要介绍' returns only that section range."""
    wiki_title = full_fixture["expected"]["titles"]["wiki"]
    args = {"title": wiki_title, "section": "简要介绍"}
    contract = run_async(model_read_to_contract(module_store, args, ["arknights"]))
    contract["intent_id"] = "intent-wiki-section"

    res = run_async(execute_read(module_store, contract))
    assert res["status"] == "ok"

    lines = res.get("content", {}).get("lines", [])
    # Wiki section range parser excludes XML tags and returns only the section body
    assert len(lines) == 1
    assert lines[0]["line_number"] == 2
    assert "凯尔希的简要介绍正文内容" in lines[0]["text"]


def test_read_operator_record_by_model_args(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """Operator record read by model_read_to_contract character_name + record_name + segment."""
    args = {"character_name": "凯尔希", "record_name": "遗骨的低语", "segment": 1}
    contract = run_async(model_read_to_contract(module_store, args, ["arknights"]))
    contract["intent_id"] = "intent-op-record"

    res = run_async(execute_read(module_store, contract))
    assert res["status"] == "ok"
    assert res["document"]["document_id"] == full_fixture["expected"]["document_ids"]["operator_record"]
    assert len(res["content"]["lines"]) == full_fixture["expected"]["line_counts"]["operator_record"]


def test_read_activity_stream_paging_covers_all_lines_exactly_once(
    full_fixture: dict[str, Any], module_store: CorpusStore
) -> None:
    """Activity stream: model_read_to_contract with activity_name + mode='activity' pages over all lines."""
    args = {"activity_name": "孤星", "mode": "activity", "max_lines": 5}
    contract = run_async(model_read_to_contract(module_store, args, ["arknights"]))

    collected_line_texts: list[str] = []
    page_count = 0

    while True:
        contract["intent_id"] = f"intent-stream-{page_count}"
        res = run_async(execute_read(module_store, contract))
        assert res["status"] == "ok"

        pub = project_read_public(res)
        for line in res.get("content", {}).get("lines", []):
            collected_line_texts.append(line["text"])

        page_count += 1
        page_info = pub.get("page", {})
        if not page_info.get("has_more"):
            break

        cont = page_info.get("continuation")
        assert cont is not None
        assert "position" in cont

        contract = run_async(model_read_to_contract(module_store, cont, ["arknights"]))
        contract["limits"] = {"max_lines": 5}

    # Expected total lines = CW-1 before (2) + CW-1 after (2) + CW-ST-4 after (42) = 46
    expected_total = (
        full_fixture["expected"]["line_counts"]["activity_1"]
        + full_fixture["expected"]["line_counts"]["activity_2"]
        + full_fixture["expected"]["line_counts"]["story"]
    )
    assert len(collected_line_texts) == expected_total
    assert page_count > 1


def test_read_expected_data_version_mismatch(
    full_fixture: dict[str, Any], module_store: CorpusStore
) -> None:
    """Mismatch in expected_data_version raises or returns PACKAGE_VERSION_MISMATCH."""
    contract = {
        "intent_id": "intent-version-mismatch",
        "expected_data_version": "0" * 64,
        "locator": {"document_id": full_fixture["expected"]["document_ids"]["story"]},
        "selection": {"mode": "range", "start_line": 1, "end_line": 3},
    }
    res = run_async(execute_read(module_store, contract))
    assert res["status"] == "error"
    assert res["error"]["code"] == "PACKAGE_VERSION_MISMATCH"


def test_read_max_lines_returns_has_more(
    full_fixture: dict[str, Any], module_store: CorpusStore
) -> None:
    """max_lines=1 on a 2-line span returns has_more with continuation."""
    contract = {
        "intent_id": "intent-max-lines-test",
        "locator": {"document_id": full_fixture["expected"]["document_ids"]["story"]},
        "selection": {"mode": "range", "start_line": 1, "end_line": 2},
        "limits": {"max_lines": 1},
    }
    res = run_async(execute_read(module_store, contract))
    assert res["status"] == "ok"
    assert res["page"]["has_more"] is True

    pub = project_read_public(res)
    assert pub["page"]["has_more"] is True
    assert pub["page"]["continuation"] is not None


def test_read_citation_shape_in_rendered_output(
    full_fixture: dict[str, Any], module_store: CorpusStore
) -> None:
    """Rendered read output contains 《…》第 N 行 citation shape."""
    title = full_fixture["expected"]["titles"]["story"]
    args = {"title": title, "line": 5, "before": 1, "after": 1}
    contract = run_async(model_read_to_contract(module_store, args, ["arknights"]))
    contract["intent_id"] = "intent-citation"

    res = run_async(execute_read(module_store, contract))
    rendered = render_read(args, res)

    # Citation shape: 《...》第 ... 行
    citation_match = re.search(r"《.+?》第 \d+(?:-\d+)? 行", rendered)
    assert citation_match is not None, f"Expected citation shape not found in rendered read: {rendered}"


def test_evidence_registry_apply_read_deduplication() -> None:
    """EvidenceRegistry.apply_read handles first read, second read with history_lines, and None."""
    registry = EvidenceRegistry()
    session_key = registry.session_key("user123", "conv456")
    doc_id = "official:story:test_doc"
    data_version = "a" * 64

    # 1. First read: returns new_ranges covering everything, reused_ranges empty, drop=False
    r1 = registry.apply_read(
        session_key,
        data_version=data_version,
        document_id=doc_id,
        line_start=1,
        line_end=10,
        history_lines=set(),
    )
    assert r1["new_ranges"] == [{"line_start": 1, "line_end": 10}]
    assert r1["reused_ranges"] == []
    assert r1["drop"] is False

    # 2. Second read of same range with history_lines containing the lines -> reused_ranges non-empty, drop=True
    history = set(range(1, 11))
    r2 = registry.apply_read(
        session_key,
        data_version=data_version,
        document_id=doc_id,
        line_start=1,
        line_end=10,
        history_lines=history,
    )
    assert r2["reused_ranges"] == [{"line_start": 1, "line_end": 10}]
    assert r2["new_ranges"] == []
    assert r2["drop"] is True
    assert r2["guidance"] is not None

    # 3. Read with history_lines=None -> conservative full range
    r3 = registry.apply_read(
        session_key,
        data_version=data_version,
        document_id=doc_id,
        line_start=1,
        line_end=10,
        history_lines=None,
    )
    assert r3["new_ranges"] == [{"line_start": 1, "line_end": 10}]
    assert r3["reused_ranges"] == []
    assert r3["drop"] is False
