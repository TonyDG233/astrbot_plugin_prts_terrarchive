"""Wiki 文档角色与标签字段的确定性解析（上游 src/wiki.js 全量移植）。"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .constants import WIKI_SECTION_VALUES

WIKI_SECTION_SET = frozenset(WIKI_SECTION_VALUES)
WIKI_CONTAINER_SECTIONS = frozenset(["所有相关的活动剧情总结"])
OPEN_TAG = re.compile(r"^<([^>/]+)>$")
CLOSE_TAG = re.compile(r"^</([^>]+)>$")


def wiki_document_role(document: dict[str, Any] | None = None) -> str:
    document = document or {}
    if document.get("document_type") != "knowledge" or document.get("document_kind") != "wiki":
        return ""
    explicit = str(document.get("wiki_role") or "").strip()
    if explicit in ("story", "character", "character_activity", "other"):
        return explicit
    path = str(document.get("path") or "")
    if path.startswith("stories/"):
        return "story"
    if path.startswith("char_v3/prompt_"):
        return "character_activity"
    if path.startswith("char_v3/") and document.get("character_name"):
        return "character"
    return "other"


def wiki_character_name(record: dict[str, Any] | None = None) -> str:
    record = record or {}
    metadata = str((record.get("document") or {}).get("character_name") or "").strip()
    if metadata:
        return metadata
    # 新版 character_activity 记录直接携带 character_name；旧版未拆分资料包
    # 仍需从 prompt 首行的“名称:角色名”恢复，以保证升级前后都可检索。
    for line in record.get("lines") or []:
        match = re.match(r"^名称[：:]\s*(.+)$", str(line.get("text") or "").strip())
        if match and match.group(1):
            return match.group(1).strip()
    return ""


def wiki_activity_name(document: dict[str, Any] | None = None) -> str:
    document = document or {}
    metadata = str(document.get("activity_name") or "").strip()
    if metadata:
        return metadata
    role = wiki_document_role(document)
    return (
        str(document.get("display_title") or "").strip()
        if role in ("story", "character_activity")
        else ""
    )


def wiki_activity_ranges(record: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """旧版角色活动 Wiki 将多个活动放在同一篇文档，以活动名称标签划分正文。"""
    record = record or {}
    ranges: list[dict[str, Any]] = []
    lines = record.get("lines") or []
    current: dict[str, Any] | None = None
    for index, line in enumerate(lines):
        text = str((line or {}).get("text") or "").strip()
        marker = re.match(r"^<活动名称>\s*(.+?)\s*</活动名称>$", text)
        if marker:
            if current:
                current["end_line"] = index
            current = {"name": marker.group(1).strip(), "start_line": index + 1, "end_line": len(lines)}
            ranges.append(current)
        elif current and text == "</相关内容>":
            current["end_line"] = index + 1
            current = None
        elif current and text == "</所有相关的活动剧情总结>":
            current["end_line"] = index
            current = None
    return ranges


def wiki_section_ranges(
    record: dict[str, Any] | None = None, requested: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """返回标签内部的 1-based 闭区间；标签行本身不包含在范围内。"""
    record = record or {}
    wanted = {str(item or "").strip() for item in (requested or []) if str(item or "").strip()}
    ranges: list[dict[str, Any]] = []
    lines = record.get("lines") or []
    for marker_index, line in enumerate(lines):
        opened = OPEN_TAG.match(str((line or {}).get("text") or "").strip())
        name = opened.group(1) if opened else None
        if not name or name not in WIKI_SECTION_SET or (wanted and name not in wanted):
            continue

        # 角色×活动辅助 Wiki 的子字段是半结构化的：它们没有各自的闭合标签，
        # 而由下一个字段标签或外层 </相关内容> 隐式结束。容器字段仍优先寻找同名闭合。
        boundary_index = len(lines)
        for index in range(marker_index + 1, len(lines)):
            text = str((lines[index] or {}).get("text") or "").strip()
            closed = CLOSE_TAG.match(text)
            if closed and closed.group(1) == name:
                boundary_index = index
                break
            if name in WIKI_CONTAINER_SECTIONS:
                continue
            next_open = OPEN_TAG.match(text)
            if (next_open and next_open.group(1) in WIKI_SECTION_SET) or closed:
                boundary_index = index
                break
        start_line = marker_index + 2
        end_line = boundary_index
        if end_line < start_line:
            continue
        ranges.append(
            {
                "name": name,
                "start_line": start_line,
                "end_line": end_line,
                "marker_start_line": marker_index + 1,
                "marker_end_line": boundary_index + 1,
            }
        )
    ranges.sort(key=lambda item: (item["start_line"], item["end_line"]))
    return ranges


def wiki_section_at(ranges: list[dict[str, Any]] | None, line_number: int) -> dict[str, Any] | None:
    containing = [
        item for item in (ranges or [])
        if item["start_line"] <= line_number <= item["end_line"]
    ]
    containing.sort(key=lambda item: item["end_line"] - item["start_line"])
    return containing[0] if containing else None