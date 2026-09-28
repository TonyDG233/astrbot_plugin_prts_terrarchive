"""prts_read 契约实现（上游 src/read.js 全量移植，去 legacy HMAC cursor）。

- 单篇 locator + around/range/document/section，或活动/任务合集 + 连续位置分页
- source_ref 内嵌行号在 around 模式下即中心行
- 全文行完整性校验（INDEX_CORRUPT）
- expected_data_version 版本绑定（PACKAGE_VERSION_MISMATCH）
- 旧版 HMAC cursor 链不再支持：模型续读一律使用 page.continuation
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from .constants import (
    CONTRACT_VERSION,
    DATA_VERSION_PATTERN,
    DEFAULT_READ_MAX_CHARS,
    DEFAULT_READ_MAX_LINES,
    END_FIELD_STORY_CONTENT_TYPES,
    ID_PATTERN,
    MAX_READ_MAX_CHARS,
    MAX_READ_MAX_LINES,
    SOURCE_REF_PATTERN,
    WIKI_SECTION_VALUES,
)
from .errors import ContractError
from .store import (
    assert_corpus_version,
    compute_lines_integrity,
    corpus_version_snapshot,
    document_game,
    document_uid,
    natural_document_title,
    operator_record_segment,
    public_character_material,
    public_story_part,
    public_story_stage_code,
)
from .wiki import wiki_section_ranges

_TOKEN_CHARS_PER_TOKEN = 2.5


def estimate_tokens(value) -> int:
    length = value if isinstance(value, (int, float)) else len(str(value or ""))
    return int(-(-length // _TOKEN_CHARS_PER_TOKEN))


def _new_request_id() -> str:
    return f"req-{secrets.token_hex(8)}"


def _is_aborted(runtime: dict | None) -> bool:
    signal = (runtime or {}).get("signal")
    if signal is None:
        return False
    checker = getattr(signal, "is_set", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(signal, "aborted", False))


_PUBLIC_FIELD_NAMES = {
    "start_line": "line",
    "center_line": "line",
    "start_position": "position",
    "limits.max_lines": "max_lines",
    "limits.max_chars": "max_chars",
}


def _require_int(value, *, minimum: int, maximum: int, field: str) -> int:
    """归一化整数值参数。

    工具调用链常把 JSON 数字表示为 double（如 101.0），部分宿主传数字字符串，
    语义上仍是整数，必须接受；只有真正非整数（101.5、布尔、乱码）才拒绝。
    """
    public_field = _PUBLIC_FIELD_NAMES.get(field, field)
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        number = int(value.strip())
    else:
        number = None
    if number is None:
        raise ContractError("INVALID_REQUEST", f"{public_field} must be an integer")
    if number < minimum or number > maximum:
        raise ContractError(
            "INVALID_REQUEST", f"{public_field} must be within [{minimum}, {maximum}]"
        )
    return number


def _require_id_string(value, field: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ContractError("INVALID_REQUEST", f"{field} must match {ID_PATTERN.pattern}")
    return value


# ---- 参数校验（跨字段规则由代码执行） ----


def normalize_read_request(raw: dict) -> tuple[dict, int | None]:
    """校验并归一化读取请求（填默认值，产出 normalized_request）。"""
    if not isinstance(raw, dict):
        raise ContractError("INVALID_REQUEST", "request must be an object")
    intent_id = raw.get("intent_id")
    request_id_input = raw.get("request_id")
    expected_version = raw.get("expected_data_version")

    intent_id_checked = _require_id_string(intent_id, "intent_id")
    request_id = (
        _new_request_id()
        if request_id_input is None
        else _require_id_string(request_id_input, "request_id")
    )
    if expected_version is not None and not DATA_VERSION_PATTERN.match(str(expected_version)):
        raise ContractError(
            "INVALID_REQUEST", "expected_data_version must be a lowercase sha256 hex string"
        )

    # locator：单篇文档、明日方舟活动或终末地集合恰好一个
    locator_raw = raw.get("locator")
    if not isinstance(locator_raw, dict):
        raise ContractError("INVALID_REQUEST", "locator must be an object")
    has_source_ref = "source_ref" in locator_raw and locator_raw["source_ref"] is not None
    has_document_id = "document_id" in locator_raw and locator_raw["document_id"] is not None
    has_document_uid = "document_uid" in locator_raw and locator_raw["document_uid"] is not None
    has_display_title = "display_title" in locator_raw and locator_raw["display_title"] is not None
    has_activity_id = "activity_id" in locator_raw and locator_raw["activity_id"] is not None
    has_activity_name = "activity_name" in locator_raw and locator_raw["activity_name"] is not None
    has_collection_name = (
        "collection_name" in locator_raw and locator_raw["collection_name"] is not None
    )
    has_stream_locator = has_activity_id or has_activity_name or has_collection_name
    locator_count = sum(
        (
            has_source_ref,
            has_document_id,
            has_document_uid,
            has_display_title,
            has_activity_id,
            has_activity_name,
            has_collection_name,
        )
    )
    if locator_count != 1:
        raise ContractError(
            "INVALID_REQUEST",
            "locator must contain exactly one of source_ref / document_id / document_uid / "
            "display_title / activity_id / activity_name / collection_name",
        )
    ref_line: int | None = None
    if has_source_ref:
        source_ref = locator_raw["source_ref"]
        if not isinstance(source_ref, str) or len(source_ref) > 1024:
            raise ContractError(
                "SOURCE_REF_INVALID", "source_ref must be a string of at most 1024 chars"
            )
        match = SOURCE_REF_PATTERN.match(source_ref)
        if not match:
            raise ContractError(
                "SOURCE_REF_INVALID", f"source_ref does not match contract pattern: {source_ref}"
            )
        ref_line = int(match.group(1), 10)
        locator = {"source_ref": source_ref}
    elif has_document_id:
        document_id = locator_raw["document_id"]
        if (
            not isinstance(document_id, str)
            or len(document_id) < 1
            or len(document_id) > 512
            or not re.search(r"\S", document_id)
        ):
            raise ContractError("INVALID_REQUEST", "document_id must be a non-empty identifier")
        locator = {"document_id": document_id}
    elif has_document_uid:
        uid = str(locator_raw["document_uid"] or "").strip()
        if not re.fullmatch(r"doc_[A-Za-z0-9_-]{16}", uid):
            raise ContractError(
                "INVALID_REQUEST", "document_uid must be a public doc_ locator"
            )
        locator = {"document_uid": uid}
    elif has_display_title:
        display_title = locator_raw["display_title"]
        if (
            not isinstance(display_title, str)
            or len(display_title) < 1
            or len(display_title) > 512
            or not re.search(r"\S", display_title)
        ):
            raise ContractError(
                "INVALID_REQUEST",
                "display_title must be a non-empty title of at most 512 chars",
            )
        locator = {"display_title": display_title}
    elif has_activity_id:
        activity_id = str(locator_raw["activity_id"]).strip()
        if not activity_id or len(activity_id) > 512:
            raise ContractError(
                "INVALID_REQUEST",
                "activity_id must be a non-empty identifier of at most 512 chars",
            )
        locator = {"activity_id": activity_id}
    elif has_activity_name:
        activity_name = str(locator_raw["activity_name"]).strip()
        if not activity_name or len(activity_name) > 512:
            raise ContractError(
                "INVALID_REQUEST",
                "activity_name must be a non-empty title of at most 512 chars",
            )
        locator = {"activity_name": activity_name}
    else:
        collection_name = str(locator_raw["collection_name"]).strip()
        if not collection_name or len(collection_name) > 512:
            raise ContractError(
                "INVALID_REQUEST",
                "collection_name must be a non-empty title of at most 512 chars",
            )
        locator = {"collection_name": collection_name}

    # selection：around / range / document / section / activity / collection
    selection_raw = raw.get("selection")
    if not isinstance(selection_raw, dict):
        raise ContractError("INVALID_REQUEST", "selection must be an object")
    mode = selection_raw.get("mode")
    cursor = selection_raw.get("cursor")
    if cursor is not None:
        raise ContractError(
            "CURSOR_INVALID",
            "旧版 cursor 不再支持；请原样提交上次结果的 page.continuation",
        )
    if mode == "around":
        if has_stream_locator:
            raise ContractError("INVALID_REQUEST", "around mode requires a document locator")
        if has_source_ref:
            if selection_raw.get("center_line") is not None:
                raise ContractError(
                    "INVALID_REQUEST", "around with source_ref locator must not set center_line"
                )
            selection = {
                "mode": "around",
                "before_lines": _require_int(
                    selection_raw.get("before_lines", 3), minimum=0, maximum=100, field="before_lines"
                ),
                "after_lines": _require_int(
                    selection_raw.get("after_lines", 3), minimum=0, maximum=100, field="after_lines"
                ),
            }
        else:
            # document_id：center_line 必填；display_title：center_line 可选（执行时缺失报错）
            if (has_document_id or has_document_uid) and selection_raw.get("center_line") is None:
                raise ContractError(
                    "INVALID_REQUEST",
                    "around with document_id/document_uid locator requires center_line",
                )
            center_line = None
            if selection_raw.get("center_line") is not None:
                center_line = _require_int(
                    selection_raw["center_line"], minimum=1, maximum=10**9, field="center_line"
                )
            selection = {
                "mode": "around",
                "before_lines": _require_int(
                    selection_raw.get("before_lines", 3), minimum=0, maximum=100, field="before_lines"
                ),
                "after_lines": _require_int(
                    selection_raw.get("after_lines", 3), minimum=0, maximum=100, field="after_lines"
                ),
            }
            if center_line is not None:
                selection["center_line"] = center_line
    elif mode == "range":
        if has_stream_locator:
            raise ContractError("INVALID_REQUEST", "range mode requires a document locator")
        start_line = _require_int(
            selection_raw.get("start_line"), minimum=1, maximum=10**9, field="start_line"
        )
        end_line = _require_int(
            selection_raw.get("end_line"), minimum=1, maximum=10**9, field="end_line"
        )
        if end_line < start_line:
            raise ContractError("LINE_RANGE_INVALID", "end_line must be >= start_line")
        selection = {"mode": "range", "start_line": start_line, "end_line": end_line}
    elif mode == "document":
        if has_stream_locator:
            raise ContractError("INVALID_REQUEST", "document mode requires a document locator")
        start_line = (
            1
            if selection_raw.get("start_line") is None
            else _require_int(
                selection_raw["start_line"], minimum=1, maximum=10**9, field="start_line"
            )
        )
        selection = {"mode": "document", "cursor": None, "start_line": start_line}
    elif mode == "section":
        if has_stream_locator:
            raise ContractError("INVALID_REQUEST", "section mode requires a document locator")
        section = str(selection_raw.get("section") or "").strip()
        if section not in WIKI_SECTION_VALUES:
            raise ContractError("INVALID_REQUEST", "section must be a supported Wiki field")
        selection = {"mode": "section", "section": section}
    elif mode in ("activity", "collection"):
        expected_locator = (
            (has_activity_id or has_activity_name or has_document_uid)
            if mode == "activity"
            else (has_collection_name or has_document_uid)
        )
        if not expected_locator:
            raise ContractError(
                "INVALID_REQUEST",
                f"{mode} mode requires "
                + ("activity_id or activity_name" if mode == "activity" else "collection_name")
                + " locator",
            )
        start_position = (
            1
            if selection_raw.get("start_position") is None
            else _require_int(
                selection_raw["start_position"], minimum=1, maximum=10**9, field="start_position"
            )
        )
        content_types: list[str] = []
        if selection_raw.get("content_types") is not None:
            raw_types = selection_raw["content_types"]
            if (
                mode != "collection"
                or not isinstance(raw_types, list)
                or len(raw_types) > len(END_FIELD_STORY_CONTENT_TYPES)
                or any(item not in END_FIELD_STORY_CONTENT_TYPES for item in raw_types)
            ):
                raise ContractError(
                    "INVALID_REQUEST",
                    "content_types is only available in collection mode and must contain "
                    "supported original-story types",
                )
            content_types = list(dict.fromkeys(raw_types))
        selection = {"mode": mode, "cursor": None, "start_position": start_position}
        if content_types:
            selection["content_types"] = content_types
    else:
        raise ContractError(
            "INVALID_REQUEST",
            "selection.mode must be around | range | document | section | activity | collection",
        )

    format_value = raw.get("format", "lines")
    if format_value not in ("lines", "plain_text"):
        raise ContractError("INVALID_REQUEST", "format must be lines | plain_text")

    include_adjacent = raw.get("include_adjacent_documents", True)
    if not isinstance(include_adjacent, bool):
        raise ContractError(
            "INVALID_REQUEST", "include_adjacent_documents must be a boolean"
        )

    limits_raw = raw.get("limits") or {}
    if not isinstance(limits_raw, dict):
        raise ContractError("INVALID_REQUEST", "limits must be an object")
    limits = {
        "max_lines": _require_int(
            limits_raw.get("max_lines", DEFAULT_READ_MAX_LINES),
            minimum=1,
            maximum=MAX_READ_MAX_LINES,
            field="limits.max_lines",
        ),
        "max_chars": _require_int(
            limits_raw.get("max_chars", DEFAULT_READ_MAX_CHARS),
            minimum=100,
            maximum=MAX_READ_MAX_CHARS,
            field="limits.max_chars",
        ),
    }

    normalized = {
        "intent_id": intent_id_checked,
        "request_id": request_id,
        "locator": locator,
        "selection": selection,
        "format": format_value,
        "include_adjacent_documents": include_adjacent,
        "limits": limits,
    }
    if expected_version is not None:
        normalized["expected_data_version"] = expected_version
    return normalized, ref_line


# ---- 文档摘要投影 ----

SUMMARY_FIELDS = (
    "game", "resource_type", "content_type", "collection_name", "collection_type",
    "document_id", "document_type", "document_category", "document_kind", "display_title",
    "collection_id", "activity_id", "activity_name", "source_story_id", "story_id", "story_code",
    "story_name", "part_type", "part_label", "char_id", "character_name", "path", "text_sha256",
    "line_count", "sequence_index", "sequence_source", "sequence_confidence",
    "previous_document_id", "next_document_id", "source_ref_prefix", "entity_id",
)


def to_document_summary(document: dict | None) -> dict:
    document = document or {}
    summary = {"document_uid": document_uid(document.get("document_id", "")),
               "game": document_game(document)}
    # 固定摘要字段缺失时输出空字符串，不能把 None 带过工具边界。
    for field in SUMMARY_FIELDS:
        summary[field] = document.get(field, "")
        if summary[field] is None:
            summary[field] = ""
    return summary


def record_summary(record: dict) -> dict:
    """由完整 record 构建文档摘要：实体文档的 entity_id 位于 record.entity。"""
    summary = to_document_summary(record.get("document"))
    document = record.get("document") or {}
    entity = record.get("entity") or {}
    if document.get("document_type") == "entity" and entity.get("entity_id"):
        summary["entity_id"] = str(entity["entity_id"])
    return summary


# ---- 执行 ----


async def execute_read(store, raw_args: dict, runtime: dict | None = None) -> dict:
    """执行 prts_read；返回契约响应（ok / error）。"""
    started_at = _now_ms()
    snapshot = None
    try:
        await store.ready()
        snapshot = corpus_version_snapshot(store)
        normalized, ref_line = normalize_read_request(raw_args)
        if _is_aborted(runtime):
            raise ContractError("CANCELLED", "aborted before execution")

        expected = normalized.get("expected_data_version")
        if expected is not None and expected != store.data_version:
            raise ContractError(
                "PACKAGE_VERSION_MISMATCH",
                f"expected data_version {expected} but active release is {store.data_version}",
                retryable=True,
            )

        # 合集通读：枚举活动/任务的全部官方剧情，按顺序跨文档读取与分页。
        if normalized["selection"]["mode"] in ("activity", "collection"):
            response = await _execute_story_stream_read(store, normalized, runtime, started_at)
            assert_corpus_version(store, snapshot)
            return response

        # 定位文档
        locator = normalized["locator"]
        if "document_id" in locator:
            found = await store.get_document(locator["document_id"])
            if found is None:
                raise ContractError(
                    "DOCUMENT_NOT_FOUND", f"document not found: {locator['document_id']}"
                )
        elif "document_uid" in locator:
            found = await store.get_document_by_uid(locator["document_uid"])
            if found is None:
                raise ContractError(
                    "DOCUMENT_NOT_FOUND",
                    f"本地资料包中找不到 document_uid={locator['document_uid']}",
                )
        elif "display_title" in locator:
            try:
                found = await store.get_document_by_title(locator["display_title"])
            except ContractError as error:
                if error.code == "DOCUMENT_AMBIGUOUS":
                    raise ContractError("DOCUMENT_AMBIGUOUS", error.message) from None
                raise
            if found is None:
                raise ContractError(
                    "DOCUMENT_NOT_FOUND", "本地资料包中找不到该完整标题对应的文档"
                )
        else:
            source_ref = locator["source_ref"]
            prefix = source_ref[: source_ref.rfind(":L")]
            document_id = store.get_document_id_by_prefix(prefix)
            if document_id is None:
                raise ContractError("DOCUMENT_NOT_FOUND", f"unknown source_ref prefix: {prefix}")
            found = await store.get_document(document_id)
            if found is None:
                raise ContractError("DOCUMENT_NOT_FOUND", f"document not found: {document_id}")
        assert_corpus_version(store, snapshot)

        # GameData 把 [uc]info/ 一行式简介排在 obt/ 对话正文之前；document 模式
        # 命中简介时优先换成可读全文。
        record = found["record"]
        if normalized["selection"]["mode"] == "document":
            source_story_id = str((record.get("document") or {}).get("source_story_id") or "")
            if (
                (record.get("document") or {}).get("document_kind") == "synopsis"
                and source_story_id.startswith("[uc]info/")
            ):
                full_story = await store.get_document_by_source_story_id(
                    source_story_id[len("[uc]info/") :]
                )
                if (
                    full_story
                    and (full_story.get("record", {}).get("document") or {}).get("document_kind")
                    == "story"
                ):
                    record = full_story["record"]

        pack_id = found["pack_id"]
        pack_manifest = store.packs.get(pack_id)
        document = record["document"]
        document_id = document["document_id"]
        line_count = document["line_count"]

        # 行完整性（全文）：行文本 \n 连接后 sha256 与 local_integrity 比对
        actual_integrity = compute_lines_integrity(record["lines"])
        expected_integrity = (record.get("local_integrity") or {}).get("sha256")
        if expected_integrity != actual_integrity:
            raise ContractError(
                "INDEX_CORRUPT",
                f"integrity mismatch for {document_id}: expected {expected_integrity}, "
                f"got {actual_integrity}",
            )
        if _is_aborted(runtime):
            raise ContractError("CANCELLED", "aborted after integrity check")

        # 解析选区 → [start_line, end_line]
        selection = normalized["selection"]
        if selection["mode"] == "around":
            center = ref_line if "source_ref" in locator else selection.get("center_line")
            if not center or center > line_count:
                raise ContractError(
                    "LINE_RANGE_INVALID",
                    f"center line {center if center else '(missing)'} is missing or beyond "
                    f"document length {line_count}",
                )
            start_line = max(1, center - selection["before_lines"])
            end_line = min(line_count, center + selection["after_lines"])
        elif selection["mode"] == "range":
            if selection["start_line"] > line_count:
                raise ContractError(
                    "LINE_RANGE_INVALID",
                    f"start_line {selection['start_line']} is beyond document length {line_count}",
                )
            start_line = selection["start_line"]
            end_line = min(line_count, selection["end_line"])
        elif selection["mode"] == "section":
            if document.get("document_type") != "knowledge" or document.get("document_kind") != "wiki":
                raise ContractError("INVALID_REQUEST", "section mode can only read Wiki documents")
            ranges = wiki_section_ranges(record, [selection["section"]])
            if not ranges:
                raise ContractError(
                    "DOCUMENT_NOT_FOUND",
                    f"Wiki 文档“{document.get('display_title')}”没有字段“{selection['section']}”",
                )
            if len(ranges) > 1:
                raise ContractError(
                    "DOCUMENT_AMBIGUOUS",
                    f"Wiki 文档“{document.get('display_title')}”包含多个“{selection['section']}”字段；"
                    "请用 prts_search 获取具体行范围",
                )
            start_line = ranges[0]["start_line"]
            end_line = ranges[0]["end_line"]
        else:
            start_line = selection["start_line"]
            end_line = line_count

        # 应用 limits 截取
        max_lines = normalized["limits"]["max_lines"]
        max_chars = normalized["limits"]["max_chars"]
        span_lines = end_line - start_line + 1
        truncated = False
        truncation_reason = None
        selected_end = end_line
        if span_lines > max_lines:
            selected_end = start_line + max_lines - 1
            truncated = True
            truncation_reason = "max_lines"

        selected_lines = []
        char_count = 0
        for line_number in range(start_line, selected_end + 1):
            line = record["lines"][line_number - 1]
            if char_count + len(line.get("text") or "") > max_chars:
                truncated = True
                if truncation_reason is None:
                    truncation_reason = "max_chars"
                break
            char_count += len(line.get("text") or "")
            selected_lines.append(line)
        # 首行即超过 max_chars 时不能返回“0 行 + 原地游标”的 ok 响应。
        if not selected_lines and truncated:
            raise ContractError(
                "BUDGET_EXCEEDED",
                f"读取范围内首行长度已超过 max_chars={max_chars}；请提高 max_chars 后重试",
            )
        returned = len(selected_lines)
        has_more = truncated or selected_end < end_line
        next_cursor = None

        # 内容投影
        if normalized["format"] == "plain_text":
            content = {
                "format": "plain_text",
                "text": "\n".join((line.get("text") or "") for line in selected_lines),
            }
        else:
            lines = []
            for line in selected_lines:
                item = {"line_number": line.get("line_number")}
                if line.get("source_line_id"):
                    item["source_line_id"] = line["source_line_id"]
                item["line_type"] = line.get("line_type") or ""
                item["speaker_raw"] = line.get("speaker_raw") or ""
                if line.get("speaker_id"):
                    item["speaker_id"] = line["speaker_id"]
                item["text"] = line.get("text") or ""
                if line.get("audio"):
                    item["audio"] = line["audio"]
                if line.get("hint"):
                    item["hint"] = line["hint"]
                item["source_ref"] = f"{document.get('source_ref_prefix', '')}:L{line.get('line_number')}"
                lines.append(item)
            content = {"format": "lines", "lines": lines}

        # 相邻文档摘要
        adjacent_documents = None
        if normalized["include_adjacent_documents"]:
            previous_id = document.get("previous_document_id") or None
            next_id = document.get("next_document_id") or None
            previous = await store.get_document(previous_id) if previous_id else None
            next_doc = await store.get_document(next_id) if next_id else None
            adjacent_documents = {
                "previous": to_document_summary(previous["record"]["document"]) if previous else None,
                "next": to_document_summary(next_doc["record"]["document"]) if next_doc else None,
            }

        assert_corpus_version(store, snapshot)
        response = {
            "contract_version": CONTRACT_VERSION,
            "status": "ok",
            "request_id": normalized["request_id"],
            "data_version": store.data_version,
            "package_schema_version": _manifest_field(pack_manifest, "package_schema_version", 1),
            "index_schema_version": _manifest_field(pack_manifest, "index_schema_version", 1),
            "normalized_request": normalized,
            "document": record_summary(record),
            "selection": {
                "mode": selection["mode"],
                "line_start": selected_lines[0]["line_number"] if returned > 0 else start_line,
                "line_end": selected_lines[returned - 1]["line_number"] if returned > 0 else start_line - 1,
                "line_count": returned,
                "character_count": char_count,
                "truncated": truncated,
            },
            "content": content,
            "page": {
                "limit": max_lines,
                "returned": returned,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "total": line_count if selection["mode"] == "document" else span_lines,
                "total_relation": "eq",
            },
            "integrity": {
                "verified": True,
                "expected_text_sha256": expected_integrity,
                "actual_text_sha256": actual_integrity,
            },
            "stats": {
                "elapsed_ms": _now_ms() - started_at,
                "scanned_documents": 1,
                "scanned_lines": line_count,
                "returned_chars": char_count,
                "estimated_input_tokens": estimate_tokens(char_count),
                "truncated": truncated,
            },
            "warnings": [],
        }
        if selection["mode"] == "section":
            response["selection"]["wiki_section"] = selection["section"]
        if truncation_reason:
            response["selection"]["truncation_reason"] = truncation_reason
        if adjacent_documents is not None:
            response["adjacent_documents"] = adjacent_documents
        return response
    except ContractError as error:
        if snapshot is not None:
            try:
                assert_corpus_version(store, snapshot)
            except ContractError as changed:
                error = changed
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "error",
            "request_id": (
                raw_args["request_id"]
                if isinstance(raw_args, dict) and isinstance(raw_args.get("request_id"), str)
                else _new_request_id()
            ),
            "data_version": getattr(store, "data_version", None),
            "error": {"code": error.code, "message": error.message, "retryable": error.retryable},
        }
    except Exception as error:  # 基础设施故障交给宿主 isError
        code = getattr(error, "code", None)
        if code == "PACKAGE_VERSION_MISMATCH" and snapshot is not None:
            try:
                assert_corpus_version(store, snapshot)
            except ContractError as changed:
                error = changed
                code = changed.code
        if code in ("PACKAGE_VERSION_MISMATCH", "ENOENT") or re.search(
            r"current\.json|release-manifest", str(error)
        ):
            return {
                "contract_version": CONTRACT_VERSION,
                "status": "error",
                "request_id": _new_request_id(),
                "data_version": None,
                "error": {
                    "code": "PACKAGE_NOT_INSTALLED",
                    "message": "本地资料包未安装或不完整，请在 PRTS 语料设置中重新下载或激活版本",
                    "retryable": True,
                },
            }
        raise


def _manifest_field(manifest: dict | None, key: str, default):
    value = (manifest or {}).get(key)
    return default if value is None else value


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# ---- 合集连续阅读 ----


def _stream_document_labels(docs: list[dict]) -> list[str]:
    return [natural_document_title(item["document"]) for item in docs]


async def _execute_story_stream_read(
    store, normalized: dict, runtime: dict | None, started_at: int
) -> dict:
    selection = normalized["selection"]
    is_activity = selection["mode"] == "activity"
    locator = normalized["locator"]
    anchor_document_id = (
        store.get_document_id_by_uid(locator["document_uid"]) or ""
        if "document_uid" in locator
        else ""
    )
    if "document_uid" in locator and not anchor_document_id:
        raise ContractError(
            "DOCUMENT_NOT_FOUND", f"本地资料包中找不到 document_uid={locator['document_uid']}"
        )
    try:
        docs = (
            store.activity_story_documents(
                activity_id=locator.get("activity_id", ""),
                activity_name=locator.get("activity_name", ""),
                anchor_document_id=anchor_document_id,
            )
            if is_activity
            else store.endfield_collection_documents(
                collection_name=locator.get("collection_name", ""),
                content_types=selection.get("content_types") or [],
                anchor_document_id=anchor_document_id,
            )
        )
    except ContractError as error:
        if error.code == "DOCUMENT_AMBIGUOUS":
            raise ContractError("DOCUMENT_AMBIGUOUS", error.message) from None
        raise
    target_name = str(
        locator.get("activity_name") or locator.get("activity_id") or ""
        if is_activity
        else locator.get("collection_name") or ""
    )
    if not docs:
        raise ContractError(
            "DOCUMENT_NOT_FOUND",
            f"本地资料包中找不到{'活动' if is_activity else '终末地集合'}“{target_name}”的剧情原文",
        )

    total_lines = sum(int((item["document"] or {}).get("line_count") or 0) for item in docs)
    collection_id = str(
        (docs[0]["document"] or {}).get("collection_id")
        or (docs[0]["document"] or {}).get("activity_id")
        or ""
    )
    stream_key = _json_dumps([selection["mode"], collection_id, selection.get("content_types") or []])
    doc_index = 0
    start_line = 1
    start_position = selection["start_position"]
    if start_position > total_lines:
        raise ContractError(
            "LINE_RANGE_INVALID",
            f"position {start_position} is beyond collection length {total_lines}",
        )
    remaining = start_position
    for index, item in enumerate(docs):
        line_count = int((item["document"] or {}).get("line_count") or 0)
        if remaining <= line_count:
            doc_index = index
            start_line = remaining
            break
        remaining -= line_count

    labels = _stream_document_labels(docs)
    max_lines = normalized["limits"]["max_lines"]
    max_chars = normalized["limits"]["max_chars"]
    selected: list[dict] = []
    char_count = 0
    current_position = start_position
    next_location = None
    truncation_reason = None
    for index in range(doc_index, len(docs)):
        if _is_aborted(runtime):
            raise ContractError("CANCELLED", "aborted during story-stream read")
        found = await store.get_document(docs[index]["document"]["document_id"])
        if not found:
            continue
        record = found["record"]
        document = record["document"]
        actual_integrity = compute_lines_integrity(record["lines"])
        if (record.get("local_integrity") or {}).get("sha256") != actual_integrity:
            raise ContractError(
                "INDEX_CORRUPT",
                f"integrity mismatch for {document['document_id']}: "
                f"expected {(record.get('local_integrity') or {}).get('sha256')}, got {actual_integrity}",
            )
        start = start_line if index == doc_index else 1
        for number in range(start, len(record["lines"]) + 1):
            line = record["lines"][number - 1]
            if len(selected) >= max_lines:
                truncation_reason = "max_lines"
                next_location = {"doc_index": index, "next_line": number}
                break
            if char_count + len(line.get("text") or "") > max_chars:
                truncation_reason = "max_chars"
                next_location = {"doc_index": index, "next_line": number}
                break
            char_count += len(line.get("text") or "")
            selected.append(
                {
                    "line": line,
                    "document": document,
                    "pack_id": found["pack_id"],
                    "document_title": labels[index],
                    "stream_position": current_position,
                }
            )
            current_position += 1
            next_location = (
                {"doc_index": index, "next_line": number + 1}
                if number < len(record["lines"])
                else ({"doc_index": index + 1, "next_line": 1}
                      if index + 1 < len(docs) else None)
            )
        else:
            continue
        break
    if not selected:
        raise ContractError(
            "BUDGET_EXCEEDED",
            f"读取范围内首行长度已超过 max_chars={max_chars}；请提高 max_chars 后重试",
        )

    next_stream_position = current_position if next_location else None
    first = selected[0]
    first_doc_summary = to_document_summary(first["document"])
    stream_sources: dict[str, dict] = {}
    for item in selected:
        document_id = str((item["document"] or {}).get("document_id") or "")
        current = stream_sources.get(document_id) or {
            "document_id": document_id,
            "document_uid": document_uid(document_id),
            "title": item["document_title"],
            "line_start": item["line"]["line_number"],
            "line_end": item["line"]["line_number"],
        }
        current["line_start"] = min(current["line_start"], item["line"]["line_number"])
        current["line_end"] = max(current["line_end"], item["line"]["line_number"])
        stream_sources[document_id] = current
    stream_name = str(
        (first["document"] or {}).get("activity_name") or target_name
        if is_activity
        else (first["document"] or {}).get("collection_name") or target_name
    )
    stream = {
        "mode": selection["mode"],
        "game": "arknights" if is_activity else "endfield",
        "name": stream_name,
        "document_count": len(docs),
        "total_lines": total_lines,
        "position_start": start_position,
        "position_end": current_position - 1,
        "next_position": next_stream_position,
        "order_kind": "source_sequence" if is_activity else "derived_content_grouping",
        "order_confidence": "source_backed" if is_activity else "derived",
        "order_note": (
            "按明日方舟活动资料的来源序列连续读取。"
            if is_activity
            else "终末地碎片缺少可证明的全局时间线；当前仅按内容类型分组，并在组内按自然编号排序，"
                 "不代表游戏内先后。"
        ),
        "anchor_document_uid": document_uid(first["document"].get("document_id", "")),
        "sources": list(stream_sources.values()),
    }
    if selection.get("content_types"):
        stream["content_types"] = selection["content_types"]
    activity = (
        {
            "activity_id": str(first_doc_summary.get("activity_id") or first_doc_summary.get("collection_id") or ""),
            "activity_name": stream_name,
            "story_count": len(docs),
            "total_lines": total_lines,
        }
        if is_activity
        else None
    )

    if normalized["format"] == "plain_text":
        content = {
            "format": "plain_text",
            "text": "\n".join((item["line"].get("text") or "") for item in selected),
        }
    else:
        content = {
            "format": "lines",
            "lines": [
                {
                    "line_number": item["line"].get("line_number"),
                    **({"source_line_id": item["line"]["source_line_id"]}
                       if item["line"].get("source_line_id") else {}),
                    "line_type": item["line"].get("line_type") or "",
                    "speaker_raw": item["line"].get("speaker_raw") or "",
                    **({"speaker_id": item["line"]["speaker_id"]}
                       if item["line"].get("speaker_id") else {}),
                    "text": item["line"].get("text") or "",
                    **({"audio": item["line"]["audio"]} if item["line"].get("audio") else {}),
                    **({"hint": item["line"]["hint"]} if item["line"].get("hint") else {}),
                    "source_ref": f"{item['document'].get('source_ref_prefix', '')}:L{item['line'].get('line_number')}",
                    "document_id": item["document"].get("document_id"),
                    "document_uid": document_uid(item["document"].get("document_id", "")),
                    "document_title": item["document_title"],
                    "stream_position": item["stream_position"],
                }
                for item in selected
            ],
        }

    first_pack_manifest = store.packs.get(first["pack_id"])
    returned_integrity = compute_lines_integrity([item["line"] for item in selected])
    response = {
        "contract_version": CONTRACT_VERSION,
        "status": "ok",
        "request_id": normalized["request_id"],
        "data_version": store.data_version,
        "package_schema_version": _manifest_field(first_pack_manifest, "package_schema_version", 1),
        "index_schema_version": _manifest_field(first_pack_manifest, "index_schema_version", 1),
        "normalized_request": normalized,
        "document": first_doc_summary,
        "selection": {
            "mode": selection["mode"],
            "line_start": start_position,
            "line_end": current_position - 1,
            "line_count": len(selected),
            "character_count": char_count,
            "truncated": bool(next_location),
        },
        "content": content,
        "page": {
            "limit": max_lines,
            "returned": len(selected),
            "has_more": bool(next_location),
            "next_cursor": None,
            "total": total_lines,
            "total_relation": "eq",
        },
        "stream": stream,
        "integrity": {
            "verified": True,
            "expected_text_sha256": returned_integrity,
            "actual_text_sha256": returned_integrity,
        },
        "stats": {
            "elapsed_ms": _now_ms() - started_at,
            "scanned_documents": len(docs),
            "scanned_lines": len(selected),
            "returned_chars": char_count,
            "estimated_input_tokens": estimate_tokens(
                "\n".join((item["line"].get("text") or "") for item in selected)
            ),
            "truncated": bool(next_location),
        },
        "warnings": [],
    }
    if truncation_reason:
        response["selection"]["truncation_reason"] = truncation_reason
    if activity:
        response["activity"] = activity
    return response


def _json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---- 模型可见文本渲染 ----


_WHITESPACE_TAIL = re.compile(r"\s+$")


def readable_rendered_line(line: dict, marker: str = "L") -> str:
    """模型可见的单行渲染格式；evidence 去重与模型 surface 必须共用本函数。"""
    speaker = str(line.get("speaker_raw") or "").strip()
    text = str(line.get("text") or "")
    if speaker:
        text = re.sub(rf"^{re.escape(speaker)}\s*[：:]\s*", "", text)
    rendered = (
        f"{marker}{line.get('line_number')} {line.get('line_type') or ''} "
        f"{speaker + ': ' if speaker else ''}{text}"
    )
    return _WHITESPACE_TAIL.sub("", rendered)


def _public_line(line: dict) -> dict:
    speaker = str(line.get("speaker_raw") or "").strip()
    text = str(line.get("text") or "")
    if speaker:
        text = re.sub(rf"^{re.escape(speaker)}\s*[：:]\s*", "", text)
    item = {"line": line.get("line_number")}
    if line.get("source_line_id"):
        item["source_line_id"] = line["source_line_id"]
    item["line_type"] = line.get("line_type") or ""
    item["speaker"] = speaker
    if line.get("speaker_id"):
        item["speaker_id"] = line["speaker_id"]
    item["text"] = text
    if line.get("document_title"):
        item["document_title"] = line["document_title"]
    if line.get("document_uid"):
        item["document_uid"] = line["document_uid"]
    if isinstance(line.get("stream_position"), int):
        item["stream_position"] = line["stream_position"]
    if line.get("document_title"):
        citation = f"《{line['document_title']}》"
        if line.get("document_uid"):
            citation += f"（document_uid={line['document_uid']}）"
        citation += f"第 {line.get('line_number')} 行"
        item["citation"] = citation
    if line.get("audio"):
        item["audio"] = line["audio"]
    if line.get("hint"):
        item["hint"] = line["hint"]
    return item


def project_read_public(value: dict) -> dict:
    """执行层富响应 → 模型/程序共用的自然定位 public result。"""
    if value.get("status") == "error":
        return value
    if (value.get("primary") or {}).get("kind") == "official_story_collection":
        return value
    if value.get("primary"):
        has_more = bool((value.get("page") or {}).get("has_more"))
        next_line = int((value["primary"].get("selection") or {}).get("line_end") or 0) + 1
        existing = (value.get("page") or {}).get("continuation")
        if isinstance(existing, dict):
            short_locator = dict(existing)
        elif value["primary"].get("stage_code") and value["primary"].get("story_part"):
            short_locator = {
                "stage_code": value["primary"]["stage_code"],
                "story_part": value["primary"]["story_part"],
            }
        else:
            short_locator = {"title": value["primary"].get("title")}
        data_version = str(
            (existing or {}).get("data_version")
            if isinstance(existing, dict)
            else (value.get("presentation") or {}).get("data_version")
            or value.get("data_version")
            or ""
        )
        projected = dict(value)
        projected["page"] = {
            "returned_lines": int(
                (value.get("page") or {}).get("returned_lines")
                or len(value["primary"].get("lines") or [])
                or 0
            ),
            "has_more": has_more,
            "continuation": (
                {**short_locator, "mode": "document", "line": next_line,
                 **({"data_version": data_version} if data_version else {})}
                if has_more and next_line > 0
                else None
            ),
        }
        return projected
    if value.get("stream"):
        lines = (
            [_public_line(line) for line in (value.get("content") or {}).get("lines") or []]
            if (value.get("content") or {}).get("format") == "lines"
            else []
        )
        stream = value["stream"]
        is_activity = stream.get("mode") == "activity"
        requested_locator = (value.get("normalized_request") or {}).get("locator") or {}
        continuation_locator = (
            {"document_uid": requested_locator["document_uid"]}
            if requested_locator.get("document_uid")
            else {("activity_name" if is_activity else "collection_name"): stream.get("name")}
        )
        continuation = (
            {
                **continuation_locator,
                "mode": stream.get("mode"),
                "position": stream.get("next_position"),
                **({"content_types": stream["content_types"]} if stream.get("content_types") else {}),
                "data_version": str(value.get("data_version") or ""),
            }
            if (value.get("page") or {}).get("has_more") and isinstance(stream.get("next_position"), int)
            else None
        )
        title = (
            f"{stream.get('name')} / 活动剧情连续阅读"
            if is_activity
            else f"{stream.get('name')} / 终末地任务连续阅读"
        )
        primary = {
            "game": stream.get("game"),
            "title": title,
            "kind": "official_story_collection",
            "selection": {
                "mode": stream.get("mode"),
                "line_start": stream.get("position_start"),
                "line_end": stream.get("position_end"),
                "truncated": bool((value.get("selection") or {}).get("truncated")),
            },
            "lines": lines,
            "ordering": stream.get("order_kind"),
            "ordering_note": stream.get("order_note"),
            "citation": "连续阅读结果按每行的 document_title 与 line 引用",
        }
        if (value.get("content") or {}).get("format") == "plain_text":
            primary["text"] = value["content"].get("text") or ""
        return {
            **value,
            "primary": primary,
            "page": {
                "returned_lines": int(
                    (value.get("page") or {}).get("returned") or len(lines)
                ),
                "has_more": bool((value.get("page") or {}).get("has_more")),
                "continuation": continuation,
            },
        }
    document = value.get("document") or {}
    selection = value.get("selection") or {}
    title = natural_document_title(document)
    stage_code = public_story_stage_code(document.get("story_code"))
    story_part = public_story_part(document.get("part_type"))
    has_story_stage = (
        document_game(document) == "arknights" and bool(stage_code) and bool(story_part)
    )
    record_segment = operator_record_segment(document)
    has_operator_record = (
        bool(record_segment)
        and document.get("document_kind") == "story"
        and document.get("part_type") == "body"
    )
    material = public_character_material(document)
    continuation_locator = (
        {"stage_code": stage_code, "story_part": story_part}
        if has_story_stage
        else (
            {
                "character_name": document.get("character_name"),
                "record_name": document.get("story_name"),
                "segment": record_segment,
            }
            if has_operator_record
            else (
                {"character_name": document.get("character_name"), "material": material,
                 "game": document_game(document)}
                if material
                else {"title": title}
            )
        )
    )
    lines = (
        [_public_line(line) for line in (value.get("content") or {}).get("lines") or []]
        if (value.get("content") or {}).get("format") == "lines"
        else []
    )
    kind = (
        "official_story"
        if document.get("document_type") == "story"
        else (
            "wiki_curated"
            if document.get("document_type") == "knowledge" and document.get("document_kind") == "wiki"
            else "local_document"
        )
    )
    primary = {
        "game": document_game(document),
        "title": title,
        "kind": kind,
        "selection": {
            "mode": selection.get("mode"),
            "line_start": selection.get("line_start"),
            "line_end": selection.get("line_end"),
            "truncated": bool(selection.get("truncated")),
        },
        "lines": lines,
        "citation": (
            f"《{title}》Wiki·{selection['wiki_section']}"
            if selection.get("wiki_section")
            else f"《{title}》第 "
            + (
                str(selection.get("line_start"))
                if selection.get("line_start") == selection.get("line_end")
                else f"{selection.get('line_start')}-{selection.get('line_end')}"
            )
            + " 行"
        ),
    }
    if has_story_stage:
        primary["stage_code"] = stage_code
        primary["story_part"] = story_part
    if has_operator_record:
        primary["character_name"] = document.get("character_name")
        primary["record_name"] = document.get("story_name")
        primary["segment"] = record_segment
    if material:
        primary["character_name"] = document.get("character_name")
        primary["material"] = material
    if selection.get("wiki_section"):
        primary["selection"]["section"] = selection["wiki_section"]
    if (value.get("content") or {}).get("format") == "plain_text":
        primary["text"] = value["content"].get("text") or ""
    projected = {**value, "primary": primary}
    if value.get("coverage"):
        projected["coverage"] = value["coverage"]
    if value.get("guidance"):
        projected["guidance"] = value["guidance"]
    projected["page"] = {
        "returned_lines": int(
            (value.get("page") or {}).get("returned") or len(lines)
        ),
        "has_more": bool((value.get("page") or {}).get("has_more")),
        "continuation": (
            {
                **continuation_locator,
                "mode": "document",
                "line": int(selection.get("line_end") or 0) + 1,
                "data_version": str(value.get("data_version") or ""),
            }
            if (value.get("page") or {}).get("has_more")
            else None
        ),
    }
    return projected


def render_read(_args: dict, value: dict) -> str:
    """将契约响应渲染为模型可见文本。"""
    if value.get("status") == "error":
        error = value.get("error") or {}
        return (
            f"[prts_read:error] code={error.get('code')} retryable={error.get('retryable')}\n"
            f"{error.get('message')}"
        )
    projected = project_read_public(value)
    parts: list[str] = []
    if projected.get("primary"):
        primary = projected["primary"]
        parts.append(f"# {primary['title']}")
        if primary.get("kind") == "official_story_collection":
            parts.append(
                f"连续位置：第 {primary['selection']['line_start']}-{primary['selection']['line_end']} 行"
            )
            if primary.get("ordering_note"):
                parts.append(f"顺序说明：{primary['ordering_note']}")
            active_title = ""
            active_uid = ""
            group_start = None
            group_end = None
            for line in primary.get("lines") or []:
                if line.get("document_title") != active_title or line.get("document_uid") != active_uid:
                    if active_title and group_start is not None:
                        parts.append(
                            f"引用：《{active_title}》"
                            + (f"（document_uid={active_uid}）" if active_uid else "")
                            + "第 "
                            + (
                                str(group_start)
                                if group_start == group_end
                                else f"{group_start}-{group_end}"
                            )
                            + " 行"
                        )
                    active_title = line.get("document_title") or primary["title"]
                    active_uid = line.get("document_uid") or ""
                    group_start = line["line"]
                    group_end = line["line"]
                    parts.append(
                        f"## {active_title}"
                        + (f"（document_uid={active_uid}）" if active_uid else "")
                    )
                else:
                    group_end = line["line"]
                parts.append(
                    readable_rendered_line(
                        {
                            "line_number": line["line"],
                            "line_type": line.get("line_type"),
                            "speaker_raw": line.get("speaker"),
                            "text": line.get("text"),
                        }
                    )
                )
            if active_title and group_start is not None:
                parts.append(
                    f"引用：《{active_title}》"
                    + (f"（document_uid={active_uid}）" if active_uid else "")
                    + "第 "
                    + (str(group_start) if group_start == group_end else f"{group_start}-{group_end}")
                    + " 行"
                )
            if primary.get("text"):
                parts.append(primary["text"])
            if projected["page"]["has_more"] and projected["page"]["continuation"]:
                parts.append(
                    f"继续阅读：prts_read({_json_dumps(projected['page']['continuation'])})"
                )
            return "\n".join(parts)
        if primary.get("kind") == "wiki_curated":
            parts.append(
                "引文状态：Wiki 为整理性资料；其中引号内容未核验为当前资料包官方原文，"
                "逐字引用前请回查原文。"
            )
        if (primary.get("selection") or {}).get("section"):
            parts.append(f"字段：{primary['selection']['section']}")
        else:
            parts.append(f"范围：第 {primary['selection']['line_start']}-{primary['selection']['line_end']} 行")
        coverage = projected.get("coverage") or {}
        if coverage.get("reused_ranges"):
            ranges = "、".join(
                str(item["line_start"])
                if item["line_start"] == item["line_end"]
                else f"{item['line_start']}-{item['line_end']}"
                for item in coverage["reused_ranges"]
            )
            parts.append(f"复用上文：第 {ranges} 行已在上方可见工具结果中；本次只展示尚未覆盖的行。")
        for line in primary.get("lines") or []:
            parts.append(
                readable_rendered_line(
                    {
                        "line_number": line["line"],
                        "line_type": line.get("line_type"),
                        "speaker_raw": line.get("speaker"),
                        "text": line.get("text"),
                    }
                )
            )
        if primary.get("text"):
            parts.append(primary["text"])
        parts.append(f"引用：{primary['citation']}")
        if projected["page"]["has_more"] and projected["page"]["continuation"]:
            next_locator = projected["page"]["continuation"]
            parts.append(f"继续阅读《{primary['title']}》，从第 {next_locator['line']} 行开始。")
            if next_locator.get("stage_code") and next_locator.get("story_part"):
                parts.append(
                    "调用：prts_read("
                    + _json_dumps(
                        {
                            "stage_code": next_locator["stage_code"],
                            "story_part": next_locator["story_part"],
                            "mode": "document",
                            "line": next_locator["line"],
                            "data_version": next_locator["data_version"],
                        }
                    )
                    + ")"
                )
            elif next_locator.get("record_name") or next_locator.get("material"):
                parts.append(f"调用：prts_read({_json_dumps(next_locator)})")
            else:
                parts.append(
                    "调用：prts_read("
                    + _json_dumps(
                        {
                            "title": str(next_locator.get("title") or ""),
                            "mode": "document",
                            "line": next_locator["line"],
                            "data_version": next_locator["data_version"],
                        }
                    )
                    + ")"
                )
        return "\n".join(parts)
    return "[prts_read:error] 无法投影读取结果"


# ---- 模型扁平参数 → 版本化 wire contract（上游 index.js modelReadToContract） ----

_READ_ALLOWED_KEYS = {
    "title", "document_uid", "stage_code", "story_part", "character_name", "record_name",
    "segment", "material", "game", "activity_name", "collection_name", "content_types",
    "line", "position", "mode", "section", "before", "after", "max_lines", "max_chars",
    "data_version",
}


async def model_read_to_contract(store, args: dict, enabled_games: list[str]) -> dict:
    """将模型使用的自然定位器转换为版本化读取契约。"""
    if not isinstance(args, dict):
        raise ContractError("INVALID_REQUEST", "读取参数必须是对象")
    expected_data_version = None
    if args.get("data_version") is not None:
        expected_data_version = str(args["data_version"])
        if not DATA_VERSION_PATTERN.match(expected_data_version):
            raise ContractError(
                "INVALID_REQUEST", "data_version 必须是续页结果给出的 64 位小写 SHA-256"
            )

    def require_enabled(found: dict | None) -> None:
        if not found:
            return
        game = document_game((found.get("record") or {}).get("document"))
        if game not in enabled_games:
            raise ContractError(
                "INVALID_REQUEST",
                f"当前未启用{'终末地' if game == 'endfield' else '明日方舟'}资料",
            )

    if args.get("cursor") is not None:
        raise ContractError(
            "CURSOR_INVALID", "旧会话 cursor 不再支持；请原样提交 page.continuation 续读"
        )

    if any(key not in _READ_ALLOWED_KEYS for key in args):
        raise ContractError("INVALID_REQUEST", "prts_read 包含不支持的参数")

    has_title = args.get("title") is not None
    has_document_uid = args.get("document_uid") is not None
    has_stage_locator = args.get("stage_code") is not None or args.get("story_part") is not None
    has_record_locator = args.get("record_name") is not None or args.get("segment") is not None
    has_material_locator = args.get("material") is not None
    has_activity_locator = args.get("activity_name") is not None
    has_collection_locator = args.get("collection_name") is not None
    locator_count = sum(
        (has_title, has_document_uid, has_stage_locator, has_record_locator,
         has_material_locator, has_activity_locator, has_collection_locator)
    )
    if locator_count != 1:
        raise ContractError(
            "INVALID_REQUEST",
            "必须且只能提供一种定位方式：title、document_uid、stage_code、角色密录、角色资料、"
            "activity_name 或 collection_name；document_uid 会替代 title，不要同时提交二者",
        )
    if args.get("character_name") is not None and not has_record_locator and not has_material_locator:
        raise ContractError("INVALID_REQUEST", "character_name 必须与 record_name 或 material 配合")
    if args.get("game") is not None and not has_material_locator:
        raise ContractError("INVALID_REQUEST", "game 只用于角色资料定位")
    if has_material_locator and args.get("material") not in (
        "profile", "module", "voice", "skin", "recruitment", "potential",
    ):
        raise ContractError(
            "INVALID_REQUEST", "material 仅支持 profile/module/voice/skin/recruitment/potential"
        )
    if args.get("game") is not None and args.get("game") not in ("arknights", "endfield"):
        raise ContractError("INVALID_REQUEST", "game 仅支持 arknights 或 endfield")
    uid_stream_mode = has_document_uid and args.get("mode") in ("activity", "collection")
    if args.get("content_types") is not None and not (
        has_collection_locator or (has_document_uid and args.get("mode") == "collection")
    ):
        raise ContractError(
            "INVALID_REQUEST", "content_types 只用于终末地 collection_name 连续阅读"
        )
    if args.get("position") is not None and not (
        has_activity_locator or has_collection_locator or uid_stream_mode
    ):
        raise ContractError("INVALID_REQUEST", "position 只用于活动或任务集合续读")

    if has_activity_locator or has_collection_locator or uid_stream_mode:
        expected_mode = (
            "activity" if has_activity_locator else ("collection" if has_collection_locator else args.get("mode"))
        )
        if args.get("mode") is not None and args.get("mode") != expected_mode:
            raise ContractError(
                "INVALID_REQUEST",
                f"{'activity_name' if has_activity_locator else 'collection_name'} "
                f'必须使用 mode="{expected_mode}"',
            )
        if any(args.get(key) is not None for key in ("line", "section", "before", "after")):
            raise ContractError(
                "INVALID_REQUEST", "活动/任务连续阅读不能与 line、section、before 或 after 同用"
            )
        required_game = "arknights" if expected_mode == "activity" else "endfield"
        if required_game not in enabled_games:
            raise ContractError(
                "INVALID_REQUEST",
                f"当前未启用{'明日方舟' if required_game == 'arknights' else '终末地'}资料",
            )
        name = str(
            (args.get("activity_name") if has_activity_locator else args.get("collection_name")) or ""
        ).strip()
        anchor_document_id = ""
        if has_document_uid:
            anchor = await store.get_document_by_uid(str(args.get("document_uid") or "").strip())
            if not anchor:
                raise ContractError("DOCUMENT_NOT_FOUND", "document_uid 对应的资料不存在")
            require_enabled(anchor)
            if document_game((anchor.get("record") or {}).get("document")) != required_game:
                raise ContractError(
                    "INVALID_REQUEST",
                    f"该 document_uid 不属于{'终末地任务' if required_game == 'endfield' else '明日方舟活动'}资料",
                )
            anchor_document_id = (anchor.get("record") or {}).get("document", {}).get("document_id", "")
        elif not name:
            raise ContractError("INVALID_REQUEST", "活动/任务集合名称不能为空")
        # 在进入执行层前先做一次歧义检查，让工具调用直接给出可操作的错误。
        try:
            if has_activity_locator:
                docs = store.activity_story_documents(activity_name=name)
            elif expected_mode == "activity":
                docs = store.activity_story_documents(anchor_document_id=anchor_document_id)
            else:
                docs = store.endfield_collection_documents(
                    collection_name=name,
                    anchor_document_id=anchor_document_id,
                    content_types=(args.get("content_types") if isinstance(args.get("content_types"), list) else []),
                )
            if not docs:
                raise ContractError(
                    "DOCUMENT_NOT_FOUND",
                    f"本地资料包中找不到{'活动' if expected_mode == 'activity' else '终末地集合'}"
                    + (f"“{name}”" if name else "")
                    + "的剧情原文",
                )
        except ContractError as error:
            if error.code == "DOCUMENT_NOT_FOUND":
                raise
            raise ContractError(error.code or "DOCUMENT_AMBIGUOUS", error.message) from None
        position_val = None
        if args.get("position") is not None:
            position_val = _require_int(
                args["position"], minimum=1, maximum=10**9, field="position"
            )
        max_lines_val = None
        if args.get("max_lines") is not None:
            max_lines_val = _require_int(
                args["max_lines"], minimum=1, maximum=MAX_READ_MAX_LINES, field="limits.max_lines"
            )
        max_chars_val = None
        if args.get("max_chars") is not None:
            max_chars_val = _require_int(
                args["max_chars"], minimum=100, maximum=MAX_READ_MAX_CHARS, field="limits.max_chars"
            )
        contract = {
            "locator": (
                {"document_uid": str(args.get("document_uid")).strip()}
                if has_document_uid
                else {("activity_name" if has_activity_locator else "collection_name"): name}
            ),
            "selection": {
                "mode": expected_mode,
                "cursor": None,
                **({"start_position": position_val} if position_val is not None else {}),
                **({"content_types": args["content_types"]} if args.get("content_types") is not None else {}),
            },
            "limits": {
                **({"max_lines": max_lines_val} if max_lines_val is not None else {}),
                **({"max_chars": max_chars_val} if max_chars_val is not None else {}),
            },
        }
        if expected_data_version:
            contract["expected_data_version"] = expected_data_version
        return contract

    locator: dict[str, Any]
    if has_stage_locator:
        if "arknights" not in enabled_games:
            raise ContractError("INVALID_REQUEST", "关卡代号定位仅用于明日方舟，但当前未启用明日方舟资料")
        stage_code = public_story_stage_code(args.get("stage_code"), relaxed_input=True)
        story_part = "" if args.get("story_part") is None else public_story_part(args["story_part"])
        if not stage_code:
            raise ContractError(
                "INVALID_REQUEST", "stage_code 必须是有效的明日方舟关卡代号，如 15-17、GT-3 或 BB-7"
            )
        if args.get("story_part") is not None and not story_part:
            raise ContractError(
                "INVALID_REQUEST",
                "story_part 仅支持 before（行动前）、after（行动后）或 story（纯剧情/幕间）",
            )
        if args.get("section") is not None:
            raise ContractError(
                "INVALID_REQUEST", "关卡代号定位只读取官方剧情原文，不能与 Wiki section 同用"
            )
        try:
            record = await store.get_document_by_story_stage(stage_code, story_part)
        except ContractError as error:
            raise ContractError(error.code or "DOCUMENT_AMBIGUOUS", error.message) from None
        if not record:
            part_labels = {"before": "行动前剧情", "after": "行动后剧情", "story": "纯剧情/幕间"}
            part_label = f"的{part_labels[story_part]}" if story_part else "剧情"
            raise ContractError(
                "DOCUMENT_NOT_FOUND", f"本地资料包中找不到关卡 {stage_code}{part_label}"
            )
        require_enabled(record)
        locator = {"document_id": record["record"]["document"]["document_id"]}
    elif has_record_locator:
        if "arknights" not in enabled_games:
            raise ContractError("INVALID_REQUEST", "干员密录定位仅用于明日方舟，但当前未启用明日方舟资料")
        character_name = str(args.get("character_name") or "").strip()
        record_name = str(args.get("record_name") or "").strip()
        if not character_name or not record_name:
            raise ContractError(
                "INVALID_REQUEST", "干员密录定位必须同时提供 character_name 和 record_name"
            )
        segment_val = None
        if args.get("segment") is not None:
            segment_val = _require_int(
                args["segment"], minimum=1, maximum=10**9, field="segment"
            )
        try:
            record = await store.get_operator_record(
                character_name, record_name, segment_val
            )
        except ContractError as error:
            raise ContractError(error.code or "DOCUMENT_AMBIGUOUS", error.message) from None
        if not record:
            raise ContractError(
                "DOCUMENT_NOT_FOUND",
                f"本地资料包中找不到干员“{character_name}”的密录“{record_name}”"
                + (f"第 {segment_val} 段" if segment_val else ""),
            )
        locator = {"document_id": record["record"]["document"]["document_id"]}
    elif has_material_locator:
        character_name = str(args.get("character_name") or "").strip()
        if not character_name:
            raise ContractError("INVALID_REQUEST", "角色资料定位必须提供 character_name")
        requested_games = [args["game"]] if args.get("game") else enabled_games
        if any(game not in enabled_games for game in requested_games):
            raise ContractError("INVALID_REQUEST", "指定的角色资料模块当前未启用")
        try:
            record = await store.get_character_material(character_name, args["material"], requested_games)
        except ContractError as error:
            raise ContractError(error.code or "DOCUMENT_AMBIGUOUS", error.message) from None
        if not record:
            raise ContractError(
                "DOCUMENT_NOT_FOUND", f"本地资料包中找不到角色“{character_name}”的 {args['material']} 资料"
            )
        require_enabled(record)
        locator = {"document_id": record["record"]["document"]["document_id"]}
    elif has_document_uid:
        uid = str(args.get("document_uid") or "").strip()
        record = await store.get_document_by_uid(uid)
        if not record:
            raise ContractError("DOCUMENT_NOT_FOUND", f"本地资料包中找不到 document_uid={uid}")
        require_enabled(record)
        locator = {"document_uid": uid}
    else:
        title = str(args.get("title") or "").strip()
        if not title:
            raise ContractError("INVALID_REQUEST", "title 不能为空")
        record = await store.get_document_by_title(title)
        require_enabled(record)
        locator = {"display_title": title}

    section = str(args.get("section") or "").strip()
    if section and not has_title:
        raise ContractError("INVALID_REQUEST", "section 只能与 title 定位器一起使用")
    natural_document_locator = has_document_uid or has_stage_locator or has_record_locator or has_material_locator
    has_line = args.get("line") is not None
    mode = args.get("mode") or (
        "section" if section
        else "around" if has_line
        else "document" if natural_document_locator
        else ""
    )
    if not mode:
        raise ContractError(
            "INVALID_REQUEST",
            '请提供 line、section 或 mode="document"；max_lines/max_chars 只限制输出量，不能代替读取方式',
        )
    if mode not in ("around", "section", "document"):
        raise ContractError("INVALID_REQUEST", "mode 仅支持 document；line/section 会自动选择模式")
    if mode == "around" and not has_line:
        raise ContractError("INVALID_REQUEST", "around 模式必须提供整数 line")
    if mode == "section" and not section:
        raise ContractError("INVALID_REQUEST", "section 模式必须提供 section")
    if section and mode != "section":
        raise ContractError("INVALID_REQUEST", "section 只能与 mode=section 一起使用")

    line_num: int | None = None
    if has_line:
        line_num = _require_int(args["line"], minimum=1, maximum=10**9, field="line")

    if mode == "document":
        selection = {
            "mode": mode,
            "cursor": None,
            **({"start_line": line_num} if line_num is not None else {}),
        }
    elif mode == "section":
        selection = {"mode": mode, "section": section}
    else:
        selection = {
            "mode": "around",
            "center_line": line_num,
            **(
                {"before_lines": _require_int(args["before"], minimum=0, maximum=100, field="before_lines")}
                if args.get("before") is not None
                else {}
            ),
            **(
                {"after_lines": _require_int(args["after"], minimum=0, maximum=100, field="after_lines")}
                if args.get("after") is not None
                else {}
            ),
        }
    contract = {
        "locator": locator,
        "selection": selection,
        "limits": {
            **(
                {
                    "max_lines": _require_int(
                        args["max_lines"], minimum=1, maximum=MAX_READ_MAX_LINES, field="limits.max_lines"
                    )
                }
                if args.get("max_lines") is not None
                else {}
            ),
            **(
                {
                    "max_chars": _require_int(
                        args["max_chars"], minimum=100, maximum=MAX_READ_MAX_CHARS, field="limits.max_chars"
                    )
                }
                if args.get("max_chars") is not None
                else {}
            ),
        },
    }
    if expected_data_version:
        contract["expected_data_version"] = expected_data_version
    return contract