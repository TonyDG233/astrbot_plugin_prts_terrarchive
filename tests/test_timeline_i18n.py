"""Acceptance test suite for PRTS timeline search and Endfield i18n localization."""

from __future__ import annotations

from typing import Any

import pytest

from prts_corpus.i18n import execute_i18n, render_i18n
from prts_corpus.store import CorpusStore
from prts_corpus.timeline import execute_timeline_search, render_timeline
from tests.conftest import run_async


def test_timeline_search_activity(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """execute_timeline_search with activity_names=['孤星'] returns the fixture event."""
    args = {"activity_names": ["孤星"]}
    res = run_async(execute_timeline_search(module_store, args))

    assert res["status"] == "ok"
    events = res.get("events", [])
    assert len(events) >= 1

    matched = next((e for e in events if "万星园" in e.get("event", "")), None)
    assert matched is not None
    assert "特里蒙科技博览会开幕" in matched["event"]
    assert matched.get("source_marker") == "年表出处:tle_0123456789abcdef01234567"


def test_timeline_source_marker_reverse_lookup(module_store: CorpusStore) -> None:
    """source_marker reverse lookup returns provenance and source references."""
    marker = "年表出处:tle_0123456789abcdef01234567"
    args = {"source_marker": marker}
    res = run_async(execute_timeline_search(module_store, args))

    assert res["status"] == "ok"
    assert res["mode"] == "source"
    assert "event" in res
    assert "provenance" in res

    prov = res["provenance"]
    assert prov.get("source_title") == "PRTS Wiki《泰拉年表》"
    sources = prov.get("sources", [])
    assert len(sources) >= 1
    assert any(s.get("story_name") == "万星之始" for s in sources)


def test_timeline_rendered_text_header(module_store: CorpusStore) -> None:
    """render_timeline output starts with [timeline_search:search] data_version=."""
    args = {"activity_names": ["孤星"]}
    res = run_async(execute_timeline_search(module_store, args))
    rendered = render_timeline(args, res)

    assert rendered.startswith("[timeline_search:search] data_version=")
    assert "万星园" in rendered
    assert "[年表出处:tle_0123456789abcdef01234567]" in rendered


def test_i18n_reverse_query_by_cn_text(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """execute_i18n reverse query by CN text returns EN translation and reference title."""
    args = {
        "query": "确认周围环境安全。",
        "source_language": "CN",
        "languages": ["EN"],
        "match_mode": "exact",
    }
    res = run_async(execute_i18n(module_store, args))

    assert res["status"] == "ok"
    matches = res.get("matches", [])
    assert len(matches) >= 1

    first_match = matches[0]
    en_trans = first_match["translations"]["EN"]["text"]
    assert en_trans == "Confirm that the surrounding environment is secure."

    refs = first_match.get("references", [])
    assert len(refs) >= 1
    expected_title = full_fixture["expected"]["titles"]["endfield_dlg"]
    assert refs[0]["title"] == expected_title


def test_i18n_title_and_line_lookup(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """execute_i18n title+line lookup returns translation for the specified line."""
    title = full_fixture["expected"]["titles"]["endfield_dlg"]
    args = {
        "title": title,
        "line": 1,
        "languages": ["EN"],
    }
    res = run_async(execute_i18n(module_store, args))

    assert res["status"] == "ok"
    matches = res.get("matches", [])
    assert len(matches) >= 1

    first_match = matches[0]
    assert first_match["translations"]["EN"]["text"] == "Administrator, we have arrived at Valley IV."


def test_timeline_and_i18n_rendered_text_media_transparency(
    full_fixture: dict[str, Any], module_store: CorpusStore
) -> None:
    """Rendered text for timeline and i18n must not leak document_id values."""
    all_doc_ids = list(full_fixture["expected"]["document_ids"].values())

    # 1. Timeline rendered output
    tl_args = {"activity_names": ["孤星"]}
    tl_res = run_async(execute_timeline_search(module_store, tl_args))
    rendered_tl = render_timeline(tl_args, tl_res)
    for doc_id in all_doc_ids:
        assert doc_id not in rendered_tl, f"document_id {doc_id} leaked in timeline rendering"

    # 2. i18n rendered output (query mode)
    i18n_query_args = {
        "query": "确认周围环境安全。",
        "source_language": "CN",
        "languages": ["EN"],
    }
    i18n_q_res = run_async(execute_i18n(module_store, i18n_query_args))
    rendered_i18n_q = render_i18n(i18n_query_args, i18n_q_res)
    for doc_id in all_doc_ids:
        assert doc_id not in rendered_i18n_q, f"document_id {doc_id} leaked in i18n query rendering"

    # 3. i18n rendered output (title+line mode)
    i18n_title_args = {
        "title": full_fixture["expected"]["titles"]["endfield_dlg"],
        "line": 1,
        "languages": ["EN"],
    }
    i18n_t_res = run_async(execute_i18n(module_store, i18n_title_args))
    rendered_i18n_t = render_i18n(i18n_title_args, i18n_t_res)
    for doc_id in all_doc_ids:
        assert doc_id not in rendered_i18n_t, f"document_id {doc_id} leaked in i18n title rendering"
    # Ensure raw "document_id" key is not exposed in public references
    assert '"document_id"' not in rendered_i18n_t
