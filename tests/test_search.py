"""Acceptance test suite for PRTS corpus search."""

from __future__ import annotations

import re
from typing import Any

import pytest

from prts_corpus.constants import SOURCE_REF_PATTERN
from prts_corpus.errors import ContractError
from prts_corpus.search import execute_search, render_search, safe_regex
import prts_corpus.search as search_mod
from tests.conftest import run_async
from prts_corpus.store import CorpusStore


def test_search_literal_fragment_and_title(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """Literal query for fixture line fragment returns the expected document title in render_search."""
    args = {"query": "风沙掠过"}
    res = run_async(execute_search(module_store, args))
    assert res["result_kind"] in ("passages", "text_matches")
    assert len(res["documents"]) >= 1

    rendered = render_search(args, res)
    expected_title = full_fixture["expected"]["titles"]["story"]
    assert "CW-ST-4" in rendered
    assert "后来" in rendered
    assert expected_title in rendered or "CW-ST-4 莱茵生命 · 后来" in rendered


def test_search_media_transparency(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """Rendered text must not expose document_id, source_ref, internal prefixes, or filesystem paths."""
    queries = ["风沙掠过", "凯尔希", "佩丽卡"]
    all_doc_ids = list(full_fixture["expected"]["document_ids"].values())
    releases_dir = full_fixture["releases_dir"]

    for q in queries:
        args = {"query": q}
        res = run_async(execute_search(module_store, args))
        rendered = render_search(args, res)

        # 1. No document_id values (e.g. official:story:cw_st_4_after)
        for doc_id in all_doc_ids:
            assert doc_id not in rendered, f"document_id {doc_id} leaked in rendered search output"

        # 2. No source_ref pattern (e.g. official_game:story:...:L1)
        assert SOURCE_REF_PATTERN.search(rendered) is None, "source_ref leaked in rendered search output"

        # 3. No internal ':"' JSON/structure prefixes
        assert ':"' not in rendered, 'Internal prefix :\\" leaked in rendered search output'

        # 4. No absolute filesystem paths
        assert releases_dir not in rendered, "Releases directory leaked in rendered search output"
        assert not re.search(r"[A-Za-z]:\\[^\n]+", rendered), "Filesystem path pattern leaked in rendered search output"


def test_search_next_after_pagination_round_trip(
    module_store: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page through results using page.next_after without duplicate documents covering all matches."""
    # Monkeypatch PAGE_DOCUMENTS to 2 so 6 matched documents require multiple pages
    monkeypatch.setattr(search_mod, "PAGE_DOCUMENTS", 2)

    seen_titles: list[str] = []
    current_args: dict[str, Any] = {"query": "凯尔希"}
    data_versions: set[str] = set()

    for page_idx in range(10):  # Safety bound
        res = run_async(execute_search(module_store, current_args))
        docs = res.get("documents", [])
        assert len(docs) > 0, f"Page {page_idx} returned empty documents before exhaustion"

        for doc in docs:
            title = doc["title"]
            assert title not in seen_titles, f"Duplicate document title '{title}' encountered across pages"
            seen_titles.append(title)

        page_meta = res.get("page", {})
        next_after = page_meta.get("next_after")

        if next_after is not None:
            assert "data_version" in next_after
            assert "resource_type" in next_after
            assert "title" in next_after
            assert "position" in next_after
            data_versions.add(next_after["data_version"])
            current_args = {"query": "凯尔希", "after": next_after}
        else:
            assert page_meta.get("exhausted") is True
            assert page_meta.get("has_more") is False
            break

    # 6 documents match '凯尔希' across packs
    assert len(seen_titles) == 6
    assert len(data_versions) == 1
    assert next(iter(data_versions)) == module_store.data_version


def test_search_resource_types_filter(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """resource_types filter narrows results to requested resource types only."""
    # Without filter: '特里蒙' matches stories and timeline reference
    res_unfiltered = run_async(execute_search(module_store, {"query": "特里蒙"}))
    assert any(d.get("resource_type") == "reference" for d in res_unfiltered["documents"])

    # With resource_types=['story']: reference is excluded, only stories returned
    args = {"query": "特里蒙", "resource_types": ["story"]}
    res = run_async(execute_search(module_store, args))
    assert res["result_kind"] in ("passages", "text_matches")

    returned_docs = res.get("documents", [])
    assert len(returned_docs) >= 1
    for d in returned_docs:
        assert d.get("resource_type") in ("story", "original_story")
    returned_titles = [d["title"] for d in returned_docs]
    assert any("CW-ST-4" in t for t in returned_titles)
    assert not any("时间线" in t or "activity_timelines" in t for t in returned_titles)


def test_search_speakers_filter(module_store: CorpusStore) -> None:
    """speakers filter restricts results to documents containing the speaker."""
    args = {"speakers": ["塞雷娅"]}
    res = run_async(execute_search(module_store, args))
    assert len(res.get("documents", [])) >= 1

    titles = [d["title"] for d in res["documents"]]
    # CW-ST-4 and CW-1 after have Saria as a speaker
    assert any("CW-ST-4" in t for t in titles) or any("CW-1" in t for t in titles)
    # Documents without Saria must not be included
    assert not any("谷地开端" in t for t in titles)


def test_search_context_terms_narrowing(module_store: CorpusStore) -> None:
    """context_terms narrows result count or filters candidates."""
    base_args = {"query": "源石能量监视器"}
    res_base = run_async(execute_search(module_store, base_args))
    assert len(res_base.get("documents", [])) == 1

    narrow_args = {"query": "源石能量监视器", "context_terms": ["缪尔赛思"]}
    res_narrow = run_async(execute_search(module_store, narrow_args))
    assert len(res_narrow.get("documents", [])) == 1

    miss_args = {"query": "源石能量监视器", "context_terms": ["不存在的上下文词汇"]}
    res_miss = run_async(execute_search(module_store, miss_args))
    assert len(res_miss.get("documents", [])) == 0


def test_search_unsupported_regex_rejected(module_store: CorpusStore) -> None:
    """Unsupported regex patterns are rejected via safe_regex and execute_search."""
    unsupported_patterns = [
        "a(b|c)",
        "a*",
        "a+",
        "a?",
        r"(?P=ref)",
        r"\1",
    ]

    for pat in unsupported_patterns:
        with pytest.raises(ContractError) as exc_info_direct:
            safe_regex(pat)
        assert exc_info_direct.value.code == "REGEX_REJECTED"

        res = run_async(execute_search(module_store, {"query": pat, "match_mode": "regex"}))
        assert res["status"] == "error"
        assert res["error"]["code"] == "REGEX_REJECTED"


def test_search_empty_query_wiki_metadata_listing(module_store: CorpusStore) -> None:
    """Empty query + resource_types=['wiki'] returns documents metadata listing."""
    args = {"query": "", "resource_types": ["wiki"]}
    res = run_async(execute_search(module_store, args))

    assert res["result_kind"] == "documents"
    docs = res.get("documents", [])
    assert len(docs) >= 1
    assert any("Wiki" in d["title"] for d in docs)

    rendered = render_search(args, res)
    assert "# 找到" in rendered
    assert "Wiki" in rendered
