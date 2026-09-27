"""Acceptance test suite for CorpusStore."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from prts_corpus.errors import ContractError, InstallerFault
from prts_corpus.store import (
    CorpusStore,
    validate_safe_relative_path,
)
from tests.conftest import run_async
import tests.fixtures as fixtures_mod


def test_store_ready(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """store.ready() succeeds on verified fixture release."""
    assert module_store.loaded is True
    assert module_store.data_version == full_fixture["data_version"]
    assert module_store.release_id == full_fixture["release_id"]
    assert len(module_store.document_order) > 0


def test_store_get_document_and_aliases(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """get_document, get_document_by_uid, and get_document_by_title retrieve matching records."""
    story_id = full_fixture["expected"]["document_ids"]["story"]
    story_uid = full_fixture["expected"]["uids"]["story"]
    story_title = full_fixture["expected"]["titles"]["story"]

    doc_by_id = run_async(module_store.get_document(story_id))
    assert doc_by_id is not None
    assert doc_by_id["record"]["document"]["document_id"] == story_id
    assert len(doc_by_id["record"]["lines"]) == full_fixture["expected"]["line_counts"]["story"]

    doc_by_uid = run_async(module_store.get_document_by_uid(story_uid))
    assert doc_by_uid is not None
    assert doc_by_uid["record"]["document"]["document_id"] == story_id

    doc_by_title = run_async(module_store.get_document_by_title(story_title))
    assert doc_by_title is not None
    assert doc_by_title["record"]["document"]["document_id"] == story_id


def test_store_get_document_by_story_stage(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """get_document_by_story_stage returns document and handles part ambiguity."""
    expected_story_id = full_fixture["expected"]["document_ids"]["story"]

    doc = run_async(module_store.get_document_by_story_stage("CW-ST-4", "after"))
    assert doc is not None
    assert doc["record"]["document"]["document_id"] == expected_story_id

    # Omitting part when both 'before' and 'after' exist raises DOCUMENT_AMBIGUOUS
    with pytest.raises(ContractError) as exc_info:
        run_async(module_store.get_document_by_story_stage("CW-1"))
    assert exc_info.value.code == "DOCUMENT_AMBIGUOUS"

    # Specific part succeeds
    doc_cw1_b = run_async(module_store.get_document_by_story_stage("CW-1", "before"))
    assert doc_cw1_b is not None
    assert doc_cw1_b["record"]["document"]["document_id"] == full_fixture["expected"]["document_ids"]["activity_1"]


def test_store_get_operator_record(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """get_operator_record returns operator record document."""
    expected_op_id = full_fixture["expected"]["document_ids"]["operator_record"]

    record_doc = run_async(module_store.get_operator_record("凯尔希", "遗骨的低语", 1))
    assert record_doc is not None
    assert record_doc["record"]["document"]["document_id"] == expected_op_id


def test_store_get_character_material(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """get_character_material retrieves profile and voice materials."""
    profile_id = full_fixture["expected"]["document_ids"]["character_profile"]
    voice_id = full_fixture["expected"]["document_ids"]["character_voice"]

    profile_doc = run_async(module_store.get_character_material("凯尔希", "profile"))
    assert profile_doc is not None
    assert profile_doc["record"]["document"]["document_id"] == profile_id

    voice_doc = run_async(module_store.get_character_material("凯尔希", "voice"))
    assert voice_doc is not None
    assert voice_doc["record"]["document"]["document_id"] == voice_id


def test_store_activity_story_documents(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """activity_story_documents returns documents in sequence order."""
    docs = module_store.activity_story_documents(activity_name="孤星")
    assert len(docs) >= 2

    # Verify sequence indices are strictly non-decreasing
    indices = [d["document"]["sequence_index"] for d in docs]
    assert indices == sorted(indices)

    doc_ids = [d["document"]["document_id"] for d in docs]
    assert doc_ids == [
        full_fixture["expected"]["document_ids"]["activity_1"],
        full_fixture["expected"]["document_ids"]["activity_2"],
        full_fixture["expected"]["document_ids"]["story"],
    ]


def test_store_endfield_collection_documents(full_fixture: dict[str, Any], module_store: CorpusStore) -> None:
    """endfield_collection_documents returns dialogue and cutscene documents."""
    docs = module_store.endfield_collection_documents(collection_name="谷地开端")
    assert len(docs) == 2

    content_types = [d["document"]["content_type"] for d in docs]
    assert "dialogue" in content_types
    assert "cutscene" in content_types

    expected_ids = [
        full_fixture["expected"]["document_ids"]["endfield_dlg"],
        full_fixture["expected"]["document_ids"]["endfield_cutscene"],
    ]
    assert [d["document"]["document_id"] for d in docs] == expected_ids


def test_store_supports_ngram_size(module_store: CorpusStore) -> None:
    """supports_ngram_size reflects V2 index gram sizes."""
    assert module_store.supports_ngram_size(3) is True
    assert module_store.supports_ngram_size(1) is True
    assert module_store.supports_ngram_size(2) is True
    assert module_store.supports_ngram_size(4) is False


def test_store_unknown_search_algorithm_fails(tmp_path: Path, full_fixture: dict[str, Any]) -> None:
    """Unknown search-index algorithm in mutated copy raises InstallerFault."""
    mutated_dir = tmp_path / "mutated_release"
    shutil.copytree(full_fixture["releases_dir"], mutated_dir)

    # Mutate pack-manifest of official_game to have an unknown search-index algorithm
    pm_path = mutated_dir / full_fixture["release_id"] / "official_game" / "pack-manifest.json"
    with open(pm_path, "r", encoding="utf-8") as f:
        pm_data = json.load(f)

    pm_data["search_index"]["algorithm"] = "unsupported-search-algo-v999"

    with open(pm_path, "w", encoding="utf-8") as f:
        json.dump(pm_data, f, ensure_ascii=False)

    store = CorpusStore(str(mutated_dir))
    with pytest.raises(InstallerFault) as exc_info:
        run_async(store.ready())
    assert exc_info.value.code in ("INVALID_RELEASE", "INVALID_MANIFEST", "INCOMPATIBLE_RELEASE")


def test_store_tampered_shard_line_fails_integrity(tmp_path: Path) -> None:
    """Tampered shard line text raises ContractError('INDEX_CORRUPT') on get_document."""
    rec = fixtures_mod._create_minimal_pack_records("official_game")
    doc_id = rec[0]["document"]["document_id"]

    # Tamper line text while keeping original local_integrity
    rec[0]["lines"][0]["text"] += " TAMPERED_CONTENT"

    release_info = fixtures_mod.make_release(
        str(tmp_path),
        packs=("official_game",),
        records_by_pack={"official_game": rec},
    )

    store = CorpusStore(str(tmp_path))
    run_async(store.ready())

    with pytest.raises(ContractError) as exc_info:
        run_async(store.get_document(doc_id))
    assert exc_info.value.code == "INDEX_CORRUPT"


def test_store_workspace_escape_shard_path_rejected(tmp_path: Path) -> None:
    """Workspace-escape shard paths containing '../' are strictly rejected."""
    base_dir = str(tmp_path / "corpus")
    os.makedirs(base_dir, exist_ok=True)

    with pytest.raises(ValueError, match="invalid asset path|outside release dir"):
        validate_safe_relative_path(base_dir, "../evil_shard.jsonl.gz")

    with pytest.raises(ValueError, match="invalid asset path|outside release dir"):
        validate_safe_relative_path(base_dir, "shards/../../secret.txt")
