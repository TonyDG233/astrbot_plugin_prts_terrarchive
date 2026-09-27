"""将 public canonical corpus_search 结果渲染成紧凑的 grep 风格文本。

上游：prts-terrarchive/src/search-projection.js
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

RESOURCE_LABELS: dict[str, str] = {
    "original_story": "官方剧情原文",
    "archive": "官方档案库",
    "knowledge": "审校资料",
    "wiki": "整理性 Wiki",
    "character_story": "角色剧情",
    "entity_profile": "实体资料",
    "reference": "引用资料",
    "timeline": "时间线",
    "story": "官方剧情原文",
    "operator_record": "干员密录原文",
    "character_profile": "官方角色档案",
    "character_module": "官方模组文案",
    "character_voice": "官方干员语音",
    "character_skin": "官方时装文案",
    "character_wiki": "整理性角色 Wiki",
    "story_wiki": "整理性活动／密录 Wiki",
    "character_activity_wiki": "整理性角色×活动 Wiki",
    "terra_journey": "大地巡旅",
}

EVIDENCE_LABELS: dict[str, str] = {
    "official_canonical": "官方原文",
    "official_structured": "官方结构化资料",
    "wiki_curated": "整理性 Wiki",
    "derived_summary": "整理性总结",
    "derived_timeline": "整理性时间线",
    "entity_projection": "实体关联入口",
    "catalog": "资料目录",
}


def normalize_text(value: Any) -> str:
    s = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    return re.sub(r"\s+", " ", s).strip()


def render_line(line: dict[str, Any]) -> str:
    role = line.get("role")
    marker = ">" if role == "match" else "+" if role == "constraint" else " "
    line_val = line.get("line")
    number = "" if line_val is None else str(line_val).rjust(4, " ")
    speaker = f"{line['speaker']}：" if line.get("speaker") else ""
    text = line.get("rendered_text") if line.get("rendered_text") is not None else line.get("text", "")
    trunc = "（本行已截断）" if line.get("truncated") else ""
    return f"{marker} {number} {speaker}{text}{trunc}".rstrip()


def highlight_literal(text: str, query: str) -> str:
    needle = normalize_text(query)
    if not needle:
        return text
    haystack = text.lower()
    lowered_needle = needle.lower()
    parts: list[str] = []
    cursor = 0
    offset = haystack.find(lowered_needle)
    while offset >= 0:
        parts.append(text[cursor:offset])
        parts.append(f"【{text[offset:offset + len(needle)]}】")
        cursor = offset + len(needle)
        offset = haystack.find(lowered_needle, cursor)
    return "".join(parts) + text[cursor:] if parts else text


def highlight_regex(text: str, query: str) -> str:
    try:
        regex = re.compile(query, re.IGNORECASE)
        return regex.sub(lambda m: f"【{m.group(0)}】", text)
    except Exception:
        return text


def rendered_excerpt_line(line: dict[str, Any], match: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    if line.get("role") != "match" or not options.get("query"):
        return line
    match_kind = match.get("match_kind")
    rendered_text = (
        highlight_regex(line.get("text", ""), options["query"])
        if match_kind == "regex"
        else highlight_literal(line.get("text", ""), options["query"])
    )
    if rendered_text == line.get("text"):
        return line
    return {**line, "rendered_text": rendered_text}


def render_match(match: dict[str, Any], options: dict[str, Any]) -> str:
    line_start = match.get("line_start")
    line_end = match.get("line_end")
    if line_start is None:
        range_str = ""
    else:
        range_str = f"命中：第 {line_start} 行" if line_start == line_end else f"命中：第 {line_start}-{line_end} 行"
    ev_kind = match.get("evidence_kind", "")
    ev_str = f"证据：{EVIDENCE_LABELS.get(ev_kind, ev_kind)}"
    rendered_lines = [
        render_line(rendered_excerpt_line(line, match, options))
        for line in (match.get("excerpt") or [])
    ]
    citation_str = f"引用：{match.get('citation', '')}"
    items = [range_str, ev_str, *rendered_lines, citation_str]
    return "\n".join(item for item in items if item)


def render_section(section: dict[str, Any]) -> str:
    comp = "完整" if section.get("completeness") == "complete" else "部分"
    header = f"字段：{section.get('section', '')}（{comp}）"
    blocks: list[str] = []
    for block in section.get("blocks") or []:
        if block.get("label"):
            blocks.append(f"{block['label']}：{block.get('text', '')}")
        else:
            blocks.append(block.get("text", ""))
    citation = f"引用：{section.get('citation', '')}"
    return "\n".join([header, *blocks, citation])


def render_entity_summary(summary: dict[str, Any]) -> str:
    items = [
        f"实体：{summary.get('canonical_name', '')}",
        f"概述：{summary['description']}" if summary.get("description") else "",
        f"历史：{summary['history_summary']}" if summary.get("history_summary") else "",
        "实体摘要已按模型上下文预算截断；需要更多内容时可按该自然标题继续读取。" if summary.get("truncated") else "",
        f"引用：{summary.get('citation', '')}",
    ]
    return "\n".join(item for item in items if item)


def render_document(document: dict[str, Any], options: dict[str, Any]) -> str:
    if document.get("entity_summary"):
        body = render_entity_summary(document["entity_summary"])
    elif document.get("section_content"):
        body = render_section(document["section_content"])
    elif document.get("matches"):
        body = "\n\n".join(render_match(m, options) for m in document["matches"])
    elif document.get("available_sections"):
        body = f"可用字段：{'、'.join(document['available_sections'])}"
    else:
        body = "资料入口"

    wiki_notice = ""
    if document.get("resource_type") in ("character_wiki", "story_wiki", "character_activity_wiki"):
        wiki_notice = "引文状态：Wiki 为整理性资料；其中引号内容未核验为当前资料包官方原文，逐字引用前请回查原文。"

    game = ""
    if document.get("game") == "endfield":
        game = "游戏：终末地"
    elif document.get("game") == "arknights":
        game = "游戏：明日方舟"

    uid_str = ""
    if document.get("document_uid"):
        uid_str = f"同名消歧定位：读取时仅提交 document_uid={document['document_uid']}，它替代 title，不要同时提交二者。"

    res_type = document.get("resource_type", "")
    res_label = f"资料类型：{RESOURCE_LABELS.get(res_type, res_type)}"

    trunc_str = "本篇仍有其他命中；请增加过滤条件或按已知行号读取。" if document.get("matches_truncated") else ""

    items = [
        f"## {document.get('title', '')}",
        game,
        uid_str,
        res_label,
        wiki_notice,
        body,
        trunc_str,
    ]
    return "\n".join(item for item in items if item)


def project_search(value: dict[str, Any] | None, options: dict[str, Any] | None = None) -> str:
    """将 corpus_search 响应渲染成 grep 风格文本（上游 projectSearch 全量移植）。"""
    options = options or {}
    documents = (value or {}).get("documents") or []
    matches = sum(1 if doc.get("entity_summary") else len(doc.get("matches") or []) for doc in documents)
    result_kind = (value or {}).get("result_kind")
    if result_kind == "documents":
        heading = f"# 找到 {len(documents)} 篇资料入口"
    elif result_kind == "complete_sections":
        heading = f"# 返回 {len(documents)} 篇资料的完整字段"
    else:
        heading = f"# 找到 {len(documents)} 篇资料，共展示 {matches} 处命中"

    page = (value or {}).get("page") or {}
    exhausted = page.get("exhausted") is True
    next_after = page.get("next_after")
    next_cursor = page.get("next_cursor")

    if next_after:
        after_json = json.dumps(next_after, separators=(",", ":"), ensure_ascii=False)
        next_str = f"扫描尚未穷尽。继续时保留本次搜索词和过滤条件，并设置 after: {after_json}。"
    elif next_cursor:
        cursor_json = json.dumps(next_cursor, separators=(",", ":"), ensure_ascii=False)
        next_str = f"旧版分页尚未结束。继续时仅提交 cursor: {cursor_json}。"
    else:
        next_str = ""

    if documents:
        zero = ""
    elif exhausted:
        zero = "已检查完整检索范围，没有找到。请检查展示名、缩短连续字面串或移除冲突过滤条件。"
    elif next_after:
        zero = "本页没有发现命中文档，但扫描位置已经推进；这不是全库零命中。"
    else:
        zero = "该旧版分页链已经结束，但不能证明资料范围已经穷尽；需要完整性时请重新搜索。"

    if exhausted and documents:
        total_docs = page.get("total_documents")
        if isinstance(total_docs, int) and not isinstance(total_docs, bool):
            complete = f"已检查完整检索范围，共匹配 {total_docs} 篇资料。"
        else:
            complete = "已扫描至当前检索范围末尾。"
    else:
        complete = ""

    warnings = (value or {}).get("warnings") or []
    if warnings:
        warn_lines = "\n".join(f"- {w.get('message', w) if isinstance(w, dict) else w}" for w in warnings)
        warnings_str = f"## 资料提示\n{warn_lines}"
    else:
        warnings_str = ""

    relations = (value or {}).get("retraveler_relations") or []
    if relations:
        rel_lines = "\n".join(
            f"- 终末地角色：{item.get('endfield_name')}；泰拉记忆原型：{item.get('terra_memory_prototype') or '未登记'}；"
            f"状态：{item.get('relation_status')}。这是跨游戏关系，不是人物别名。"
            for item in relations
        )
        relations_str = f"## 再旅者对应关系（人工审校附属字段）\n{rel_lines}"
    else:
        relations_str = ""

    doc_strings = [render_document(doc, options) for doc in documents]
    all_parts = [heading, warnings_str, relations_str, *doc_strings, zero, complete, next_str]
    return "\n\n".join(part for part in all_parts if part)


# 上游命名兼容
projectSearch = project_search
