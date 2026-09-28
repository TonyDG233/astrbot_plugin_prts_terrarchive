"""search.py：字面检索、过滤、渲染透明度、正则子集与 next_after 锚点语义。"""

import json

import pytest

from conftest import run_async as run
from prts_corpus import search as search_module
from prts_corpus.errors import ContractError

INTERNAL_NEEDLES = (
    "official:story:", "official:character:", "client:reviewed_wiki:", "client:entities:",
    "endfield:story:", "endfield:knowledge:", "source_ref", "search_index",
)


@pytest.fixture(scope="module")
def fixture_data(full_fixture):
    return full_fixture


@pytest.fixture(scope="module")
def store(module_store):
    return module_store


def _render(store, args):
    value = run(search_module.execute_search(store, args, {"signal": None}))
    if "error" in value:
        raise AssertionError(f"search error: {value['error']}")
    return value, search_module.render_search(args, value)


def test_literal_search_hit_and_header(store):
    value, text = _render(store, {"query": "凯尔希"})
    assert "error" not in value
    assert "# 找到" in text and "凯尔希" in text
    assert "引用：" in text


def test_rendered_output_has_no_internal_ids(store):
    value, text = _render(store, {"query": "凯尔希"})
    assert "error" not in value
    for needle in INTERNAL_NEEDLES:
        assert needle not in text
    assert "D:\\" not in text and "C:\\" not in text


def test_resource_type_filter(store, fixture_data):
    wiki_title = fixture_data["expected"]["titles"]["wiki"]
    value, text = _render(store, {"query": "凯尔希", "resource_types": ["story"]})
    assert "error" not in value
    assert wiki_title not in text
    value, text = _render(store, {"query": "凯尔希", "resource_types": ["character_wiki"]})
    assert "error" not in value
    assert wiki_title in text


def test_speakers_filter(store, fixture_data):
    record = run(store.get_document(fixture_data["expected"]["document_ids"]["story"]))["record"]
    line = next(item for item in record["lines"] if item.get("speaker_raw") and len(item.get("text") or "") >= 6)
    speaker = line["speaker_raw"]
    value, text = _render(store, {"query": line["text"][:4], "speakers": [speaker]})
    assert "error" not in value
    assert speaker in text


def test_context_terms_narrow_results(store, fixture_data):
    record = run(store.get_document(fixture_data["expected"]["document_ids"]["story"]))["record"]
    first = record["lines"][0]["text"]
    second = record["lines"][1]["text"]
    token = first[:4]
    value, text = _render(store, {"query": token, "context_terms": [second[:4]]})
    assert "error" not in value
    assert "命中" in text
    value_empty, text_empty = _render(store, {"query": token, "context_terms": ["绝不可能共现的词组甲乙丙"]})
    assert "error" not in value_empty
    assert "命中" not in text_empty or "0 处命中" in text_empty


def test_safe_regex_subset():
    assert search_module.safe_regex("凯尔希") is not None
    for pattern in ("a(b|c)", "a*", "\\1", "x{100}", "\\p{L}"):
        with pytest.raises(ContractError):
            search_module.safe_regex(pattern)


def test_regex_mode_executes(store, fixture_data):
    record = run(store.get_document(fixture_data["expected"]["document_ids"]["story"]))["record"]
    token = record["lines"][0]["text"][:3]
    value, text = _render(store, {"query": token + ".", "match_mode": "regex"})
    assert "error" not in value


def test_empty_query_lists_metadata(store, fixture_data):
    value, text = _render(store, {"resource_types": ["wiki"]})
    assert "error" not in value
    assert fixture_data["expected"]["titles"]["wiki"] in text
    assert "Wiki" in text


def test_deterministic_rendering(store):
    _, first = _render(store, {"query": "凯尔希"})
    _, second = _render(store, {"query": "凯尔希"})
    assert first == second


def test_bad_after_anchor_rejected(store):
    value = run(search_module.execute_search(store, {
        "query": "凯尔希",
        "after": {"data_version": "0" * 64, "resource_type": "story", "title": "x", "position": 0},
    }, {"signal": None}))
    assert value["status"] == "error"
    assert value["error"]["code"] == "PAGE_ANCHOR_VERSION_MISMATCH"

    value = run(search_module.execute_search(store, {
        "query": "凯尔希",
        "after": {"data_version": store.data_version, "resource_type": "story", "title": "不存在的锚点标题", "position": 0},
    }, {"signal": None}))
    assert value["status"] == "error"
    assert value["error"]["code"] == "PAGE_ANCHOR_MISMATCH"


def test_page_contract_shape(store):
    value, _ = _render(store, {"query": "的"})
    page = value["page"]
    assert "returned_documents" in page and "has_more" in page and "exhausted" in page
    if page["has_more"]:
        anchor = page["next_after"]
        assert set(anchor) >= {"data_version", "resource_type", "title", "position"}
        assert anchor["data_version"] == store.data_version
    else:
        assert page["next_after"] is None
        assert page["exhausted"] is True


def test_cursor_field_rejected(store):
    value = run(search_module.execute_search(store, {"query": "凯尔希", "cursor": "abc"}, {"signal": None}))
    assert value["status"] == "error"
    assert value["error"]["code"] in ("INVALID_REQUEST", "CURSOR_INVALID")


def test_project_search_returns_text(store):
    value = run(search_module.execute_search(store, {"query": "凯尔希"}, {"signal": None}))
    projected = search_module.render_search({"query": "凯尔希"}, value)
    assert isinstance(projected, str) and projected.strip()

def test_search_timeout_runtime_override(store):
    """runtime.timeout_ms 覆盖内置预算；极小预算必须快速返回 TIMEOUT。"""
    value = run(search_module.execute_search(
        store, {"query": "凯尔希"}, {"signal": None, "timeout_ms": 0.001}
    ))
    assert value["status"] == "error"
    assert value["error"]["code"] == "TIMEOUT"
    assert value["error"]["retryable"] is True
