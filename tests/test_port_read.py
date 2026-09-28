"""read.py：around/range/document/section、续读闭环、完整性与渲染透明度；evidence 语义。"""

import pytest

from conftest import run_async as run
from prts_corpus import read as read_module
from prts_corpus.errors import ContractError
from prts_corpus.evidence import EvidenceRegistry
from prts_corpus.store import CorpusStore

GAMES = ["arknights", "endfield"]
INTERNAL_NEEDLES = ("official:story:", "client:", "endfield:story:", "source_ref")


@pytest.fixture(scope="module")
def fixture_data(full_fixture):
    return full_fixture


@pytest.fixture(scope="module")
def store(module_store):
    return module_store


@pytest.fixture()
def fresh_store(fixture_data):
    instance = CorpusStore(fixture_data["releases_dir"])
    run(instance.ready())
    return instance


def _contract(store, args):
    contract = run(read_module.model_read_to_contract(store, args, GAMES))
    contract["intent_id"] = "intent-test-1"
    return contract


def _execute(store, contract):
    return run(read_module.execute_read(store, contract, {"signal": None}))


def test_around_window_and_citation(store, fixture_data):
    story_title = fixture_data["expected"]["titles"]["story"]
    contract = _contract(store, {"title": story_title, "line": 5, "before": 2, "after": 2})
    value = _execute(store, contract)
    assert value["status"] == "ok"
    assert value["selection"]["line_start"] == 3
    assert value["selection"]["line_end"] == 7
    projected = read_module.project_read_public(value)
    text = read_module.render_read({}, projected)
    assert f"《{story_title}》" in text and "第 3-7 行" in text


def test_range_mode_via_contract(store, fixture_data):
    story_title = fixture_data["expected"]["titles"]["story"]
    normalized, _ = read_module.normalize_read_request({
        "intent_id": "intent-test-2",
        "locator": {"display_title": story_title},
        "selection": {"mode": "range", "start_line": 1, "end_line": 3},
    })
    value = _execute(store, normalized)
    assert value["status"] == "ok"
    assert [line["line_number"] for line in value["content"]["lines"]] == [1, 2, 3]


def test_section_mode_reads_wiki_field(store, fixture_data):
    wiki_title = fixture_data["expected"]["titles"]["wiki"]
    value = _execute(store, _contract(store, {"title": wiki_title, "section": "简要介绍"}))
    assert value["status"] == "ok"
    assert value["selection"]["wiki_section"] == "简要介绍"
    assert value["selection"]["line_start"] >= 2
    text = read_module.render_read({}, read_module.project_read_public(value))
    assert "Wiki·简要介绍" in text


def test_invalid_section_rejected(store, fixture_data):
    with pytest.raises(ContractError) as excinfo:
        read_module.normalize_read_request({
            "intent_id": "intent-test-3",
            "locator": {"display_title": fixture_data["expected"]["titles"]["wiki"]},
            "selection": {"mode": "section", "section": "不存在的字段"},
        })
    assert excinfo.value.code == "INVALID_REQUEST"


def test_document_continuation_roundtrip(store):
    first = _execute(store, _contract(store, {"stage_code": "CW-ST-4", "mode": "document", "max_lines": 10}))
    assert first["status"] == "ok"
    projected = read_module.project_read_public(first)
    continuation = projected["page"]["continuation"]
    assert projected["page"]["has_more"] is True
    assert continuation["mode"] == "document" and continuation["line"] == 11

    resumed = _execute(store, _contract(store, dict(continuation)))
    assert resumed["status"] == "ok"
    assert resumed["content"]["lines"][0]["line_number"] == 11


def test_operator_record_read(store, fixture_data):
    value = _execute(store, _contract(store, {"character_name": "凯尔希", "record_name": "遗骨的低语"}))
    assert value["status"] == "ok"
    assert value["document"]["document_id"] == fixture_data["expected"]["document_ids"]["operator_record"]
    projected = read_module.project_read_public(value)
    assert projected["primary"]["kind"] == "official_story"
    assert projected["primary"].get("record_name") == "遗骨的低语"


def test_activity_stream_pagination_covers_each_line_once(store, fixture_data):
    first = _execute(store, _contract(store, {"activity_name": "孤星", "mode": "activity", "max_lines": 2}))
    assert first["status"] == "ok"
    projected = read_module.project_read_public(first)
    assert projected["primary"]["kind"] == "official_story_collection"
    assert projected["page"]["has_more"] is True
    continuation = projected["page"]["continuation"]
    assert continuation["position"] == 3

    second = _execute(store, _contract(store, dict(continuation)))
    assert second["status"] == "ok"
    stream = second["stream"]
    # continuation 不携带 max_lines，第二页按默认预算读到合集末尾
    assert stream["position_start"] == 3
    assert stream["position_end"] == stream["total_lines"]
    first_positions = [line["stream_position"] for line in first["content"]["lines"]]
    second_positions = [line["stream_position"] for line in second["content"]["lines"]]
    assert first_positions == [1, 2]
    assert second_positions == list(range(3, stream["total_lines"] + 1))
    assert not set(first_positions) & set(second_positions)
    assert (second["page"]["has_more"]) is False


def test_expected_data_version_binding(store, fixture_data):
    contract = _contract(store, {"title": fixture_data["expected"]["titles"]["story"], "mode": "document"})
    contract["expected_data_version"] = "0" * 64
    value = _execute(store, contract)
    assert value["status"] == "error"
    assert value["error"]["code"] == "PACKAGE_VERSION_MISMATCH"
    assert value["error"]["retryable"] is True


def test_index_corrupt_detected_on_read(fresh_store, fixture_data):
    document_id = fixture_data["expected"]["document_ids"]["story"]
    record = run(fresh_store.get_document(document_id))["record"]
    record["lines"][0]["text"] = record["lines"][0]["text"] + "（篡改）"
    contract = {
        "intent_id": "intent-test-corrupt",
        "locator": {"document_id": document_id},
        "selection": {"mode": "document", "cursor": None},
        "limits": {"max_lines": 5, "max_chars": 1000},
    }
    value = _execute(fresh_store, contract)
    assert value["status"] == "error"
    assert value["error"]["code"] == "INDEX_CORRUPT"


def test_rendered_read_has_no_internal_ids(store, fixture_data):
    value = _execute(store, _contract(store, {"title": fixture_data["expected"]["titles"]["story"], "line": 2}))
    text = read_module.render_read({}, read_module.project_read_public(value))
    for needle in INTERNAL_NEEDLES:
        assert needle not in text
    assert "D:\\" not in text and "C:\\" not in text


def test_evidence_partial_reuse_and_drop():
    registry = EvidenceRegistry()
    key = registry.session_key("platform:friend:1", "conv-1")
    registry.begin_session(key, "v1")

    first = registry.apply_read(
        key, data_version="v1", document_id="doc-1", line_start=1, line_end=5, history_lines=None,
    )
    assert first["reused_ranges"] == []
    assert first["new_ranges"] == [{"line_start": 1, "line_end": 5}]
    assert first["drop"] is False

    second = registry.apply_read(
        key, data_version="v1", document_id="doc-1", line_start=3, line_end=8,
        history_lines={1, 2, 3, 4, 5},
    )
    assert second["reused_ranges"] == [{"line_start": 3, "line_end": 5}]
    assert second["new_ranges"] == [{"line_start": 6, "line_end": 8}]
    assert second["drop"] is False
    assert second["guidance"]

    third = registry.apply_read(
        key, data_version="v1", document_id="doc-1", line_start=3, line_end=8,
        history_lines=set(range(1, 9)),
    )
    assert third["drop"] is True
    assert third["reused_ranges"] == [{"line_start": 3, "line_end": 8}]
    assert third["new_ranges"] == []


def test_evidence_conservative_without_history():
    registry = EvidenceRegistry()
    key = registry.session_key("platform:friend:2", "conv-2")
    registry.begin_session(key, "v1")
    registry.apply_read(key, data_version="v1", document_id="doc-2", line_start=1, line_end=3, history_lines={1, 2, 3})
    again = registry.apply_read(
        key, data_version="v1", document_id="doc-2", line_start=1, line_end=3, history_lines=None,
    )
    assert again["reused_ranges"] == []
    assert again["drop"] is False


def test_evidence_resets_on_data_version_change():
    registry = EvidenceRegistry()
    key = registry.session_key("platform:friend:3", "conv-3")
    registry.begin_session(key, "v1")
    registry.apply_read(key, data_version="v1", document_id="doc-3", line_start=1, line_end=4, history_lines={1, 2, 3, 4})
    registry.begin_session(key, "v2")
    after = registry.apply_read(
        key, data_version="v2", document_id="doc-3", line_start=1, line_end=4, history_lines={1, 2, 3, 4},
    )
    assert after["reused_ranges"] == []
    assert after["drop"] is False

def test_integral_float_and_string_integers_accepted(store, fixture_data):
    """工具链常把 JSON 整数传成 101.0 或 "101"，必须归一化接受。"""
    story_title = fixture_data["expected"]["titles"]["story"]
    contract = _contract(store, {"title": story_title, "line": 3.0, "max_lines": "50"})
    value = _execute(store, contract)
    assert value["status"] == "ok"
    assert value["normalized_request"]["selection"]["center_line"] == 3
    assert value["normalized_request"]["limits"]["max_lines"] == 50
    assert value["page"]["limit"] == 50

    contract_doc = _contract(store, {"title": story_title, "line": "3", "mode": "document", "max_lines": 50.0})
    value_doc = _execute(store, contract_doc)
    assert value_doc["status"] == "ok"
    assert value_doc["selection"]["line_start"] == 3
    assert value_doc["normalized_request"]["selection"]["start_line"] == 3
    assert value_doc["normalized_request"]["limits"]["max_lines"] == 50


def test_non_integral_float_rejected_with_public_field_name(store, fixture_data):
    story_title = fixture_data["expected"]["titles"]["story"]
    with pytest.raises(ContractError) as excinfo:
        _contract(store, {"title": story_title, "line": 2.5})
    assert excinfo.value.code == "INVALID_REQUEST"
    assert "line must be an integer" in excinfo.value.message
