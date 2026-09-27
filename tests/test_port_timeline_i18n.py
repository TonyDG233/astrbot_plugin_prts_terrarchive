"""timeline.py / i18n.py：过滤、别名裂变、出处反查、多语言定位与渲染透明度。"""

import pytest

from conftest import run_async as run
from prts_corpus import i18n as i18n_module
from prts_corpus import timeline as timeline_module

SOURCE_MARKER = "年表出处:tle_0123456789abcdef01234567"
INTERNAL_NEEDLES = ("official:story:", "client:", "endfield:story:", "endfield:knowledge:", "source_ref")


@pytest.fixture(scope="module")
def fixture_data(full_fixture):
    return full_fixture


@pytest.fixture(scope="module")
def store(module_store):
    return module_store


def test_timeline_activity_search_and_render(store, fixture_data):
    value = run(timeline_module.execute_timeline_search(store, {"activity_names": ["孤星"]}, {"signal": None}))
    assert value["status"] == "ok"
    events = value["events"]
    assert len(events) == 1
    event = events[0]
    assert event["activity_name"] == fixture_data["expected"]["activity_name"]
    assert event["year_start"] == 1100 and event["year_end"] == 1100
    assert SOURCE_MARKER in event["source_marker"]
    text = timeline_module.render_timeline({"activity_names": ["孤星"]}, value)
    assert text.startswith("[timeline_search:search] data_version=")
    assert "年表出处:tle_" in text


def test_timeline_source_marker_reverse_lookup(store):
    value = run(timeline_module.execute_timeline_search(store, {"source_marker": SOURCE_MARKER}, {"signal": None}))
    assert value["status"] == "ok"
    assert value.get("mode") == "source"
    text = timeline_module.render_timeline({"source_marker": SOURCE_MARKER}, value)
    assert text.startswith("[timeline_search:source]")
    assert "activity:" in text and "source:" in text and "孤星" in text


def test_timeline_year_filter(store):
    value = run(timeline_module.execute_timeline_search(store, {"year_start": 1200}, {"signal": None}))
    assert value["status"] == "ok"
    assert value["events"] == []
    value = run(timeline_module.execute_timeline_search(store, {"year_start": 1000, "year_end": 1200}, {"signal": None}))
    assert len(value["events"]) == 1


def test_timeline_entity_alias_expansion(store):
    value = run(timeline_module.execute_timeline_search(store, {"entity_names": ["凯尔希"]}, {"signal": None}))
    assert value["status"] == "ok"
    groups = value["normalized_filters"]["entity_alias_groups"]
    aliases = {alias for group in groups for alias in group["aliases"]}
    assert {"凯尔希", "老太婆", "猞猁", "Kal'tsit"} <= aliases


def test_timeline_no_internal_leaks(store):
    value = run(timeline_module.execute_timeline_search(store, {"activity_names": ["孤星"]}, {"signal": None}))
    text = timeline_module.render_timeline({}, value)
    for needle in INTERNAL_NEEDLES:
        assert needle not in text


def test_i18n_query_reverse_lookup(store):
    value = run(i18n_module.execute_i18n(store, {
        "query": "确认周围环境安全。", "languages": ["EN"],
    }, {"signal": None}))
    assert value["status"] == "ok"
    match = value["matches"][0]
    assert match["source"]["text"] == "确认周围环境安全。"
    assert match["translations"]["EN"]["text"] == "Confirm that the surrounding environment is secure."
    assert match["references"][0]["title"]


def test_i18n_title_and_line_lookup(store, fixture_data):
    value = run(i18n_module.execute_i18n(store, {
        "title": fixture_data["expected"]["titles"]["endfield_dlg"], "line": 2, "languages": ["EN"],
    }, {"signal": None}))
    assert value["status"] == "ok"
    assert value["matches"][0]["source"]["text"] == "确认周围环境安全。"


def test_i18n_text_ids_exact(store):
    value = run(i18n_module.execute_i18n(store, {
        "text_ids": ["3040337695571803101"], "languages": ["EN"],
    }, {"signal": None}))
    assert value["status"] == "ok"
    match = value["matches"][0]
    assert match["source"]["text"] == "管理员，我们已经到达四号谷地。"
    assert match["translations"]["EN"]["text"] == "Administrator, we have arrived at Valley IV."


def test_i18n_missing_language_reports_status(store):
    value = run(i18n_module.execute_i18n(store, {
        "query": "确认周围环境安全。", "languages": ["JP"],
    }, {"signal": None}))
    assert value["status"] == "ok"
    assert value["matches"][0]["translations"]["JP"]["status"] in ("missing_localization", "unavailable")


def test_i18n_no_internal_leaks(store):
    value = run(i18n_module.execute_i18n(store, {"query": "确认周围环境安全。", "languages": ["EN"]}, {"signal": None}))
    text = i18n_module.render_i18n({}, value)
    for needle in INTERNAL_NEEDLES:
        assert needle not in text
    assert "languages" in text and "references" in text