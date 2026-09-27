"""store.py：清单装载、全类型定位、流式合集、完整性与路径安全。"""

import pytest

from conftest import run_async as run
from prts_corpus.errors import ContractError
from prts_corpus.store import compute_lines_integrity, validate_safe_relative_path

EXPECTED_DOCUMENT_COUNT = 13


@pytest.fixture(scope="module")
def fixture_data(full_fixture):
    return full_fixture


@pytest.fixture(scope="module")
def store(module_store):
    return module_store


def test_ready_and_manifest(store, fixture_data):
    assert store.loaded is True
    assert store.release_id == fixture_data["release_id"]
    assert store.data_version == fixture_data["data_version"]
    assert len(store.documents) == EXPECTED_DOCUMENT_COUNT
    expected_packs = {
        "references", "official_game", "reviewed_wiki", "entities",
        "endfield_official_game", "endfield_reviewed_knowledge",
    }
    assert set(store.packs) == expected_packs


def test_get_document_and_uid(store, fixture_data):
    ids = fixture_data["expected"]["document_ids"]
    expected = ids["story"]
    found = run(store.get_document(expected))
    assert found and found["record"]["document"]["document_id"] == expected
    assert found["pack_id"] == "official_game"

    uid = fixture_data["expected"]["uids"]["story"]
    by_uid = run(store.get_document_by_uid(uid))
    assert by_uid and by_uid["record"]["document"]["document_id"] == expected
    assert store.get_document_id_by_uid(uid) == expected
    assert run(store.get_document("official:story:missing")) is None


def test_get_document_by_title(store, fixture_data):
    titles = fixture_data["expected"]["titles"]
    found = run(store.get_document_by_title(titles["operator_record"]))
    assert found and found["record"]["document"]["document_id"] == fixture_data["expected"]["document_ids"]["operator_record"]


def test_ambiguous_title_raises(store, fixture_data):
    # activity_1 / activity_2 共用同一展示标题
    with pytest.raises(ContractError) as excinfo:
        run(store.get_document_by_title(fixture_data["expected"]["titles"]["activity_1"]))
    assert excinfo.value.code == "DOCUMENT_AMBIGUOUS"


def test_story_stage_lookup(store, fixture_data):
    ids = fixture_data["expected"]["document_ids"]
    found = run(store.get_document_by_story_stage("CW-ST-4", "after"))
    assert found and found["record"]["document"]["document_id"] == ids["story"]
    found = run(store.get_document_by_story_stage("CW-1", "before"))
    assert found and found["record"]["document"]["document_id"] == ids["activity_1"]
    assert run(store.get_document_by_story_stage("NOPE-1", "after")) is None


def test_operator_record_and_uniqueness(store, fixture_data):
    ids = fixture_data["expected"]["document_ids"]
    found = run(store.get_operator_record("凯尔希", "遗骨的低语", 1))
    assert found and found["record"]["document"]["document_id"] == ids["operator_record"]
    assert store.has_unique_operator_record(ids["operator_record"]) is True
    assert run(store.get_operator_record("凯尔希", "不存在的密录", 1)) is None


def test_character_material(store, fixture_data):
    ids = fixture_data["expected"]["document_ids"]
    profile = run(store.get_character_material("凯尔希", "profile", ["arknights", "endfield"]))
    assert profile and profile["record"]["document"]["document_id"] == ids["character_profile"]
    voice = run(store.get_character_material("凯尔希", "voice", ["arknights", "endfield"]))
    assert voice and voice["record"]["document"]["document_id"] == ids["character_voice"]
    assert run(store.get_character_material("凯尔希", "skin", ["arknights", "endfield"])) is None


def test_activity_story_stream(store, fixture_data):
    ids = fixture_data["expected"]["document_ids"]
    docs = store.activity_story_documents(activity_name="孤星")
    ordered = [item["document"]["document_id"] for item in docs]
    # 孤星活动同时包含两段 CW-1 剧情与 CW-ST-4 剧情；集合必须收敛且保持来源序列
    assert set(ordered) == {ids["activity_1"], ids["activity_2"], ids["story"]}
    assert ordered.index(ids["activity_1"]) < ordered.index(ids["activity_2"])
    by_id = store.activity_story_documents(activity_id="act25side")
    assert [item["document"]["document_id"] for item in by_id] == ordered
    # 由合集内任一 document_uid 锚定同样收敛
    anchor = fixture_data["expected"]["uids"]["activity_2"]
    anchored = store.activity_story_documents(anchor_document_id=store.get_document_id_by_uid(anchor))
    assert [item["document"]["document_id"] for item in anchored] == ordered
    assert store.activity_story_documents(activity_name="不存在的活动") == []


def test_endfield_collection_stream(store, fixture_data):
    ids = fixture_data["expected"]["document_ids"]
    docs = store.endfield_collection_documents(collection_name="谷地开端")
    assert [item["document"]["document_id"] for item in docs] == [
        ids["endfield_dlg"], ids["endfield_cutscene"],
    ]
    filtered = store.endfield_collection_documents(collection_name="谷地开端", content_types=["cutscene"])
    assert [item["document"]["document_id"] for item in filtered] == [ids["endfield_cutscene"]]
    assert store.endfield_collection_documents(collection_name="不存在的任务") == []


def test_ngram_support(store):
    assert store.supports_ngram_size(1) is True
    assert store.supports_ngram_size(3) is True
    assert store.supports_ngram_size(4) is False
    assert store.supports_ngram_size(2, ["references"]) is True
    assert store.supports_ngram_size(2, ["not_a_pack"]) is False


def test_local_integrity_matches_record(store, fixture_data):
    record = run(store.get_document(fixture_data["expected"]["document_ids"]["story"]))["record"]
    assert record["local_integrity"]["sha256"] == compute_lines_integrity(record["lines"])


def test_uid_index_roundtrip(store, fixture_data):
    for key, document_id in fixture_data["expected"]["document_ids"].items():
        uid = fixture_data["expected"]["uids"].get(key)
        if not uid:
            continue
        assert store.get_document_id_by_uid(uid) == document_id


def test_path_safety(tmp_path):
    with pytest.raises(Exception):
        validate_safe_relative_path(str(tmp_path), "../escape.jsonl")
    with pytest.raises(Exception):
        validate_safe_relative_path(str(tmp_path), "a/../../escape.jsonl")
    assert validate_safe_relative_path(str(tmp_path), "shards/00000.jsonl.gz")