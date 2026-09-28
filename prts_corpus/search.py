"""grep 风格 corpus_search：复用资料包索引，公开结果只使用自然标题与行号。

上游：prts-terrarchive/src/search.js 全量移植。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import unicodedata
from typing import Any

from .constants import (
    LOCAL_CORPUS_MISSING_MESSAGE,
    RESOURCE_TYPES,
)
from .errors import ContractError
from .search_projection import normalize_text, project_search
from .store import (
    DOCUMENT_ORDERING_VERSION,
    assert_corpus_version,
    corpus_version_snapshot,
    document_game,
    document_uid,
    natural_document_title,
)
from .wiki import (
    wiki_activity_name,
    wiki_activity_ranges,
    wiki_character_name,
    wiki_document_role,
    wiki_section_at,
    wiki_section_ranges,
)

SEARCH_CONTRACT_VERSION = "prts-corpus-tools-v1"

SEARCH_ERROR_CODES = frozenset([
    "INVALID_REQUEST",
    "PACKAGE_NOT_INSTALLED",
    "PACKAGE_VERSION_MISMATCH",
    "INDEX_UNAVAILABLE",
    "INDEX_CORRUPT",
    "DOCUMENT_NOT_FOUND",
    "SOURCE_REF_INVALID",
    "LINE_RANGE_INVALID",
    "CURSOR_INVALID",
    "CURSOR_VERSION_MISMATCH",
    "CURSOR_POLICY_MISMATCH",
    "PAGE_ANCHOR_INVALID",
    "PAGE_ANCHOR_MISMATCH",
    "PAGE_ANCHOR_NOT_FOUND",
    "PAGE_ANCHOR_VERSION_MISMATCH",
    "REGEX_REJECTED",
    "TIMEOUT",
    "BUDGET_EXCEEDED",
    "CANCELLED",
    # 额外兼容上游错误码
    "QUERY_TOO_LONG",
    "REGEX_UNSUPPORTED",
    "RESOURCE_TYPE_INVALID",
])

PAGE_DOCUMENTS = 12
MAX_PASSAGES_PER_DOCUMENT = 3
PASSAGE_CLUSTER_GAP = 2
RANK_POOL_CAP = 500
SHORT_LITERAL_RANK_POOL_CAP = 128
SIMPLE_LITERAL_MATCH_CAP_PER_DOCUMENT = 24
SCAN_DOCUMENTS_PER_PAGE = 256
SEARCH_TIMEOUT_MS = 15000
PREVIEW_OPTIONS = {
    "before_lines": 1,
    "after_lines": 1,
    "max_chars_per_line": 2000,
    "max_total_chars": 12000,
}
FILTER_LIMIT = 16
DATA_VERSION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FILTER_ITEM_LIMIT = 512
ENTITY_QUERY_MAX_DISTANCE = 256
PROFILE_CATEGORIES = frozenset(["干员档案", "招聘合同", "潜能与信物"])
FILTER_FIELDS = {
    "story_names": "story_name",
}


def public_search_error(error: Exception) -> dict[str, Any]:
    candidate = str(getattr(error, "code", "") or "")
    code = candidate if candidate in SEARCH_ERROR_CODES else "INTERNAL_ERROR"
    if code == "INTERNAL_ERROR":
        msg = "本地语料搜索失败；详情请查看 DSH Host 日志"
        retryable = False
    else:
        msg = getattr(error, "message", None) or str(error)
        retryable = bool(getattr(error, "retryable", False))
    return {
        "code": code,
        "message": msg,
        "retryable": retryable,
    }


def assert_search_active(signal: Any = None, deadline: float = float("inf"), phase: str = "本地语料搜索") -> None:
    if signal is not None:
        aborted = getattr(signal, "aborted", False)
        if callable(aborted):
            aborted = aborted()
        if not aborted and hasattr(signal, "is_set"):
            aborted = signal.is_set()
        if aborted:
            raise ContractError("CANCELLED", "搜索已取消")
    if not math.isinf(deadline) and (time.time() * 1000.0) >= deadline:
        raise ContractError("TIMEOUT", f"{phase}超时", retryable=True)


def ngrams_for(value: str) -> list[str]:
    chars = list(normalize_text(value).lower())
    if not chars:
        return []
    size = min(3, len(chars))
    result: list[str] = []
    seen: set[str] = set()
    for index in range(len(chars) - size + 1):
        gram = "".join(chars[index : index + size])
        if gram not in seen:
            seen.add(gram)
            result.append(gram)
    return result


def resource_matches(document: dict[str, Any], requested: list[str]) -> bool:
    if not requested:
        return True
    explicit = str(document.get("resource_type") or "")
    doc_type = str(document.get("document_type") or "")
    kind = str(document.get("document_kind") or "")
    category = str(document.get("document_category") or "")
    wiki_role = wiki_document_role(document)

    for resource in requested:
        if resource == explicit:
            return True
        if resource == "story" and explicit == "original_story":
            return True
        if resource == "character_bundle" and explicit in (
            "character_profile",
            "character_module",
            "character_voice",
            "operator_record",
        ):
            return True
        if resource in ("reviewed_wiki", "wiki") and explicit in (
            "character_wiki",
            "story_wiki",
            "character_activity_wiki",
            "knowledge",
        ):
            return True

        if resource == "original_story":
            if doc_type == "story" and category != "memory" and kind != "synopsis":
                return True
        elif resource == "character_story":
            if doc_type == "story" and category == "memory":
                return True
        elif resource == "archive":
            if doc_type == "knowledge" and kind == "official_archive":
                return True
        elif resource in ("knowledge", "wiki"):
            if doc_type == "knowledge" and kind == "wiki":
                return True
        elif resource == "timeline":
            if doc_type == "reference":
                return True
        elif resource == "story":
            if doc_type == "story" and category != "memory" and kind != "synopsis":
                return True
        elif resource == "operator_record":
            if doc_type == "story" and category == "memory":
                return True
        elif resource == "character_profile":
            if doc_type == "character" and category in PROFILE_CATEGORIES:
                return True
        elif resource == "character_module":
            if doc_type == "character" and category == "模组文案":
                return True
        elif resource == "character_voice":
            if doc_type == "character" and category == "干员语音":
                return True
        elif resource == "character_skin":
            if doc_type == "character" and category == "时装文案":
                return True
        elif resource == "character_bundle":
            if (
                doc_type == "character"
                and (category in PROFILE_CATEGORIES or category in ("模组文案", "干员语音"))
            ) or (doc_type == "story" and category == "memory"):
                return True
        elif resource == "character_wiki":
            if doc_type == "knowledge" and kind == "wiki" and wiki_role == "character":
                return True
        elif resource == "story_wiki":
            if doc_type == "knowledge" and kind == "wiki" and wiki_document_role(document) == "story":
                return True
        elif resource == "character_activity_wiki":
            if doc_type == "knowledge" and kind == "wiki" and wiki_document_role(document) == "character_activity":
                return True
        elif resource == "reviewed_wiki":
            if doc_type == "knowledge" and kind == "wiki":
                return True
        elif resource == "terra_journey":
            if doc_type == "knowledge" and kind == "terra_journey":
                return True
        elif resource == "entity_profile":
            if doc_type == "entity":
                return True
        elif resource == "reference":
            if doc_type == "reference":
                return True
    return False


def safe_regex(pattern: str) -> re.Pattern:
    """受限安全线性正则子集。
    
    仅允许字面量、^/$、点号、字符类、转义和固定次数 {n}（n<=64）。
    """
    def rejected(reason: str) -> ContractError:
        return ContractError(
            "REGEX_REJECTED",
            f"正则表达式超出安全线性子集（{reason}）；仅允许字面量、^/$、点号、字符类、转义和固定次数 {{n}}",
        )

    if len(pattern) > 256:
        raise rejected("长度超过 256")

    in_class = False
    can_repeat = False
    idx = 0
    p_len = len(pattern)

    while idx < p_len:
        character = pattern[idx]
        if character == "\\":
            if idx + 1 >= p_len:
                break
            escaped = pattern[idx + 1]
            if re.match(r"[1-9]", escaped) or escaped == "k":
                raise rejected("不允许反向引用")
            if (escaped in ("p", "P")) and idx + 2 < p_len and pattern[idx + 2] == "{":
                raise rejected("不允许 Unicode 属性转义")
            if escaped == "u" and idx + 2 < p_len and pattern[idx + 2] == "{":
                raise rejected("不允许带花括号的 Unicode 转义")
            idx += 2
            if not in_class:
                can_repeat = escaped not in ("b", "B")
            continue

        if in_class:
            if character == "]":
                in_class = False
                can_repeat = True
            idx += 1
            continue

        if character == "[":
            in_class = True
            can_repeat = False
            idx += 1
            continue

        if character in "()|*+?":
            raise rejected(f"不允许 {character}")

        if character == "{":
            if not can_repeat:
                raise rejected("固定次数前缺少可重复字符")
            fixed = re.match(r"^\{([0-9]{1,2})\}", pattern[idx:])
            if not fixed:
                raise rejected("只允许 {n}，不允许范围或开放式量词")
            count = int(fixed.group(1))
            if count > 64:
                raise rejected("固定次数不能超过 64")
            idx += len(fixed.group(0))
            can_repeat = False
            continue

        if character == "}":
            raise rejected("未转义的 }")

        can_repeat = character not in ("^", "$")
        idx += 1

    try:
        return re.compile(pattern, re.UNICODE)
    except Exception as exc:
        raise ContractError("REGEX_REJECTED", f"正则表达式无法编译: {exc}") from exc


def normalized_request(raw: Any = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError("INVALID_REQUEST", "搜索参数必须是对象")

    # Difference: No HMAC/legacy cursor chain; pagination is exclusively next_after.
    if raw.get("cursor") is not None:
        raise ContractError("INVALID_REQUEST", "不再支持旧版 cursor 分页，请使用 after 分页")

    after = None
    if raw.get("after") is not None:
        raw_after = raw["after"]
        if not isinstance(raw_after, dict):
            raise ContractError("INVALID_REQUEST", "after 必须包含 data_version、resource_type、title 和 position")
        data_version = raw_after.get("data_version")
        resource_type = normalize_text(raw_after.get("resource_type"))
        title = normalize_text(raw_after.get("title"))
        position = raw_after.get("position")
        allowed_after_keys = {"data_version", "resource_type", "title", "position"}
        if (
            not isinstance(data_version, str)
            or not DATA_VERSION_PATTERN.match(data_version)
            or not resource_type
            or not title
            or not isinstance(position, int)
            or isinstance(position, bool)
            or position < 0
            or position > 10_000_000
            or any(k not in allowed_after_keys for k in raw_after.keys())
        ):
            raise ContractError(
                "INVALID_REQUEST",
                "after 必须包含且只能包含 64 位小写 data_version、resource_type、title 和 0..10000000 的安全整数 position",
            )
        after = {
            "data_version": data_version,
            "resource_type": resource_type,
            "title": title,
            "position": position,
        }

    query = normalize_text(raw.get("query"))
    if len(list(query)) > 512:
        raise ContractError("INVALID_REQUEST", "literal query 最多 512 个字符")

    filter_names = [
        "resource_types",
        "character_names",
        "story_names",
        "activity_names",
        "entity_names",
        "speakers",
        "wiki_sections",
        "games",
        "content_types",
        "collection_names",
    ]
    filters: dict[str, list[str]] = {}
    for field in filter_names:
        val = raw.get(field)
        if val is not None:
            if not isinstance(val, list) or not val:
                raise ContractError("INVALID_REQUEST", f"{field} 必须是非空数组")
            norm_list: list[str] = []
            seen_items: set[str] = set()
            for item in val:
                norm_item = normalize_text(item)
                if norm_item not in seen_items:
                    seen_items.add(norm_item)
                    norm_list.append(norm_item)
            filters[field] = norm_list
            if any(not v for v in filters[field]):
                raise ContractError("INVALID_REQUEST", f"{field} 不能包含空字符串")
            if len(filters[field]) > FILTER_LIMIT:
                raise ContractError("INVALID_REQUEST", f"{field} 最多 {FILTER_LIMIT} 项")
            if any(len(list(v)) > FILTER_ITEM_LIMIT for v in filters[field]):
                raise ContractError("INVALID_REQUEST", f"{field} 单项最长 {FILTER_ITEM_LIMIT} 个字符")
        else:
            filters[field] = []

    if any(g.lower() not in ("arknights", "endfield") for g in filters["games"]):
        raise ContractError("INVALID_REQUEST", "games 仅支持 arknights / endfield")
    filters["games"] = [g.lower() for g in filters["games"]]

    if not query and not any(len(items) for items in filters.values()):
        raise ContractError("INVALID_REQUEST", "请提供 query 或至少一个资料/人物/篇章/说话人过滤条件")

    match_mode = raw.get("match_mode", "literal")
    if match_mode not in ("literal", "regex"):
        raise ContractError("INVALID_REQUEST", "match_mode 仅支持 literal / regex")
    if match_mode == "regex":
        if not query:
            raise ContractError("INVALID_REQUEST", "regex 模式必须提供 query")
        safe_regex(query)

    context_terms_raw = raw.get("context_terms", [])
    if context_terms_raw is None:
        context_terms_raw = []
    if (
        not isinstance(context_terms_raw, list)
        or len(context_terms_raw) > 8
        or any(not normalize_text(item) for item in context_terms_raw)
    ):
        raise ContractError("INVALID_REQUEST", "context_terms 必须是最多 8 项的非空字符串数组")
    if any(len(list(normalize_text(item))) > FILTER_ITEM_LIMIT for item in context_terms_raw):
        raise ContractError("INVALID_REQUEST", f"context_terms 单项最长 {FILTER_ITEM_LIMIT} 个字符")
    if context_terms_raw and not query and not filters["speakers"] and not filters["entity_names"]:
        raise ContractError("INVALID_REQUEST", "context_terms 需要 query、speakers 或 entity_names 作为主条件")

    context_terms: list[str] = []
    seen_ctx: set[str] = set()
    for item in context_terms_raw:
        n = normalize_text(item)
        if n not in seen_ctx:
            seen_ctx.add(n)
            context_terms.append(n)

    if not query and len(filters["wiki_sections"]) > 1:
        raise ContractError("INVALID_REQUEST", "无 query 的完整字段查询一次只能选择一个 wiki_sections 值")

    return {
        "query": query,
        "filters": filters,
        "match_mode": match_mode,
        "context_terms": context_terms,
        "after": after,
    }


def without_after(request: dict[str, Any]) -> dict[str, Any]:
    copy_req = dict(request)
    copy_req.pop("after", None)
    return copy_req


def chinese_integer(value: int) -> str:
    digits = "零一二三四五六七八九"
    if not isinstance(value, int) or value < 0 or value > 99:
        return str(value)
    if value < 10:
        return digits[value]
    tens = value // 10
    ones = value % 10
    return f"{'' if tens == 1 else digits[tens]}十{digits[ones] if ones else ''}"


def activity_aliases(value: dict[str, Any] | None = None) -> list[str]:
    value = value or {}
    act_name = normalize_text(value.get("activity_name"))
    aliases = {act_name} if act_name else set()
    source = "\n".join(str(value.get(k) or "") for k in ("activity_id", "collection_id"))
    match = re.search(r"(?:^|event:|\b)main[_:](\d+)(?:\b|$)", source)
    if match:
        chapter = int(match.group(1))
        aliases.add(f"第{chapter}章")
        aliases.add(f"第{chinese_integer(chapter)}章")
        aliases.add(f"{chapter}章")
        aliases.add(f"主线{chapter}章")
        aliases.add(f"主线第{chapter}章")
    return list(aliases)


def activity_matches(value: dict[str, Any], requested: str, exact: bool = False) -> bool:
    needle = normalize_text(requested)
    if not needle:
        return False
    aliases = activity_aliases(value)
    for alias in aliases:
        if exact:
            if alias == needle:
                return True
        else:
            if alias == needle or needle in alias or alias in needle:
                return True
    return False


async def load_entity_relation_catalog(store: Any) -> dict[str, Any]:
    cached = getattr(store, "_endfield_relation_catalog", None)
    data_version = getattr(store, "data_version", None) or "no-store"
    if isinstance(cached, dict) and cached.get("data_version") == data_version:
        return cached.get("value") or {}
    installed = {}
    try:
        loaded = await store.get_document_by_path("config/retravelers.json")
        rec = loaded.get("record") if isinstance(loaded, dict) else loaded
        lines_text = "\n".join(line.get("text", "") for line in (rec.get("lines") or []))
        installed = json.loads(lines_text) if lines_text.strip() else {}
    except Exception:
        installed = {}
    value = {
        "retravelers": installed.get("retravelers") or [],
        "visual_parallels_without_lore_relation": installed.get("visual_parallels_without_lore_relation") or [],
    }
    if store is not None:
        store._endfield_relation_catalog = {"data_version": data_version, "value": value}
    return value


def relevant_retraveler_relations(catalog: dict[str, Any], *values: Any) -> list[dict[str, Any]]:
    parts = []
    for val in values:
        if isinstance(val, str):
            parts.append(val)
        else:
            try:
                parts.append(json.dumps(val, ensure_ascii=False))
            except Exception:
                pass
    text = normalize_text("\n".join(parts)).lower()
    if not text:
        return []
    result = []
    for row in catalog.get("retravelers") or []:
        names = [
            normalize_text(row.get("endfield_name")).lower(),
            normalize_text(row.get("terra_memory_prototype")).lower(),
        ]
        names = [n for n in names if n]
        if any(n in text for n in names):
            entry: dict[str, Any] = {
                "relation_kind": "endfield_retraveler_memory_prototype",
                "endfield_name": str(row.get("endfield_name") or ""),
                "relation_status": str(row.get("relation_status") or "reviewed"),
                "not_alias": True,
            }
            if row.get("terra_memory_prototype"):
                entry["terra_memory_prototype"] = str(row["terra_memory_prototype"])
            result.append(entry)
    return result


async def attach_retraveler_relations(
    store: Any, response: dict[str, Any], request: dict[str, Any], enabled_games: list[str]
) -> dict[str, Any]:
    if (
        "arknights" not in enabled_games
        or "endfield" not in enabled_games
        or response.get("status") == "error"
        or response.get("error")
    ):
        return response
    catalog = await load_entity_relation_catalog(store)
    relations = relevant_retraveler_relations(catalog, request, response.get("documents"))
    if relations:
        return {**response, "retraveler_relations": relations}
    return response


async def build_alias_groups(store: Any) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    catalog = await load_entity_relation_catalog(store)
    cross_game_names = {
        str(row.get("endfield_name") or "").strip()
        for row in (catalog.get("retravelers") or [])
        if str(row.get("endfield_name") or "").strip()
    }

    def remember(canonical_val: Any, alias_vals: Any = None, game_val: Any = "") -> None:
        canonical = str(canonical_val or "").strip()
        if not canonical:
            return
        group = groups.setdefault(
            canonical,
            {"canonical": canonical, "aliases": {canonical}, "games": set()},
        )
        for val in alias_vals or []:
            alias = str(val or "").strip()
            if alias and (alias == canonical or alias not in cross_game_names):
                group["aliases"].add(alias)
        game = str(game_val or "").strip().lower()
        if game in ("arknights", "endfield"):
            group["games"].add(game)

    async for record in store.iterate_documents(predicate=lambda doc, _: doc.get("document_type") == "entity"):
        doc = record.get("document") or {}
        entity = record.get("entity") or {}
        identity = f"{doc.get('document_id') or ''} {doc.get('source_ref_prefix') or ''}".lower()
        game = doc.get("game") or ("endfield" if "endfield:" in identity else "arknights")
        remember(
            entity.get("canonical_name") or doc.get("display_title"),
            entity.get("aliases") or record.get("aliases"),
            game,
        )

    alias_ref = await store.get_document_by_path("char_alias.txt")
    if alias_ref and alias_ref.get("record"):
        for line in alias_ref["record"].get("lines") or []:
            text = str(line.get("text") or "")
            parts = [p.strip() for p in text.split(";") if p.strip()]
            if not parts:
                continue
            first = parts[0]
            existing = next((g for g in groups.values() if g["canonical"] == first or first in g["aliases"]), None)
            canonical = existing["canonical"] if existing else first
            remember(canonical, parts)

    return [
        {
            "canonical": g["canonical"],
            "aliases": list(g["aliases"]),
            "games": list(g["games"]),
        }
        for g in groups.values()
    ]


async def aliases_for(store: Any, entity_names: list[str]) -> list[dict[str, Any]]:
    if not entity_names:
        return []
    if not hasattr(store, "_alias_groups") or store._alias_groups is None:
        store._alias_groups = await build_alias_groups(store)
    result = []
    for name in entity_names:
        group = next((g for g in store._alias_groups if name in g["aliases"]), None)
        if group:
            result.append({
                "canonical": group["canonical"],
                "aliases": list(group["aliases"]),
                "games": list(group["games"]),
            })
        else:
            result.append({"canonical": name, "aliases": [name], "games": []})
    return result


def public_resource_type(document: dict[str, Any]) -> str:
    if document.get("resource_type"):
        return str(document["resource_type"])
    doc_type = str(document.get("document_type") or "")
    kind = str(document.get("document_kind") or "")
    category = str(document.get("document_category") or "")
    if doc_type == "story":
        return "operator_record" if category == "memory" else "story"
    if doc_type == "character":
        if category == "模组文案":
            return "character_module"
        if category == "干员语音":
            return "character_voice"
        if category == "时装文案":
            return "character_skin"
        return "character_profile"
    if doc_type == "knowledge" and kind == "wiki":
        role = wiki_document_role(document)
        if role == "story":
            return "story_wiki"
        if role == "character_activity":
            return "character_activity_wiki"
        return "character_wiki"
    if doc_type == "knowledge" and kind == "terra_journey":
        return "terra_journey"
    if doc_type == "entity":
        return "entity_profile"
    return "reference"


def public_content_type(document: dict[str, Any]) -> str:
    if document.get("content_type"):
        return normalize_text(document["content_type"])
    kind = normalize_text(document.get("kind") or document.get("document_kind")).lower()
    mapping = {
        "dlg": "dialogue",
        "story": "dialogue",
        "cutscene": "cutscene",
        "radio": "radio",
        "remotecomm": "remote_comm",
        "black": "black_screen",
        "env": "environment_talk",
        "sns": "sns_chat",
        "topic": "sns_topic",
        "wiki": "knowledge",
        "reference": "knowledge",
    }
    return mapping.get(kind, "dialogue" if document.get("document_type") == "story" else "knowledge")


def public_collection_name(document: dict[str, Any]) -> str:
    return normalize_text(
        document.get("collection_name")
        or document.get("mission_title")
        or document.get("activity_name")
        or document.get("story_name")
    )


def needs_wiki_activity_hydration(document: dict[str, Any]) -> bool:
    return wiki_document_role(document) == "character_activity" and not normalize_text(document.get("activity_name"))


def requested_wiki_activity_ranges(record: dict[str, Any], filters: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not filters.get("activity_names") or not needs_wiki_activity_hydration(record.get("document") or {}):
        return None
    return [
        r
        for r in wiki_activity_ranges(record)
        if any(activity_matches({"activity_name": r["name"]}, name, exact=True) for name in filters["activity_names"])
    ]


def document_matches(document: dict[str, Any], speakers: list[str], filters: dict[str, Any]) -> bool:
    if filters.get("games") and document_game(document) not in filters["games"]:
        return False
    if not resource_matches(document, filters.get("resource_types") or []):
        return False
    if filters.get("content_types") and public_content_type(document) not in filters["content_types"]:
        return False
    if filters.get("collection_names") and public_collection_name(document) not in filters["collection_names"]:
        return False
    for filter_name, field in FILTER_FIELDS.items():
        if filters.get(filter_name) and normalize_text(document.get(field)) not in filters[filter_name]:
            return False
    wiki_role = wiki_document_role(document)
    if (
        filters.get("character_names")
        and (wiki_role != "character_activity" or normalize_text(document.get("character_name")))
        and normalize_text(document.get("character_name")) not in filters["character_names"]
    ):
        return False
    if filters.get("activity_names") and not needs_wiki_activity_hydration(document):
        act_name = wiki_activity_name(document) or normalize_text(document.get("activity_name"))
        activity = {**document, "activity_name": act_name}
        exact = wiki_role in ("story", "character_activity")
        if not any(activity_matches(activity, name, exact=exact) for name in filters["activity_names"]):
            return False
    if filters.get("wiki_sections") and not wiki_role:
        return False
    if filters.get("speakers"):
        return any(spk in speakers for spk in filters["speakers"])
    return True


def pack_ids_for_document_ids(store: Any, document_ids: list[str]) -> list[str] | None:
    if not document_ids:
        return []
    pack_ids = set()
    for doc_id in document_ids:
        item = store.documents.get(doc_id)
        pack_id = item.get("pack_id") or item.get("packId") if item else None
        if not pack_id:
            return None
        pack_ids.add(pack_id)
    return list(pack_ids)


def hydrated_record_matches(record: dict[str, Any], filters: dict[str, Any]) -> bool:
    doc = record.get("document") or {}
    if (
        filters.get("character_names")
        and wiki_document_role(doc) == "character_activity"
        and not normalize_text(doc.get("character_name"))
        and normalize_text(wiki_character_name(record)) not in filters["character_names"]
    ):
        return False
    return True


def line_content(line: dict[str, Any]) -> str:
    text = normalize_text(line.get("text", ""))
    speaker_raw = normalize_text(line.get("speaker_raw", ""))
    prefix = f"{speaker_raw}:"
    if line.get("line_type") == "dialogue" and prefix != ":" and text.startswith(prefix):
        return text[len(prefix) :].lstrip()
    return text


def matches_text(text: str, query: str, mode: str, regex: re.Pattern | None) -> bool:
    if not query:
        return True
    if mode == "regex":
        return bool(regex.search(normalize_text(text))) if regex else False
    return query.lower() in normalize_text(text).lower()


def alias_near_query(text: str, alias: str, query_range: dict[str, int] | None) -> bool:
    haystack = normalize_text(text)
    if not query_range:
        return alias in haystack
    offset = haystack.find(alias)
    while offset >= 0:
        end = offset + len(alias)
        distance = (
            query_range["start"] - end
            if end < query_range["start"]
            else offset - query_range["end"]
            if offset > query_range["end"]
            else 0
        )
        if distance <= ENTITY_QUERY_MAX_DISTANCE:
            return True
        offset = haystack.find(alias, offset + 1)
    return False


def text_aliases(entity: dict[str, Any]) -> list[str]:
    raw_aliases = [normalize_text(a) for a in (entity.get("aliases") or []) if normalize_text(a)]
    seen = set()
    aliases = []
    for a in raw_aliases:
        if a not in seen:
            seen.add(a)
            aliases.append(a)
    longer = [a for a in aliases if len(list(a)) > 1]
    return longer if longer else aliases


def entity_occurrence(
    record: dict[str, Any],
    line: dict[str, Any],
    entity_groups: list[dict[str, Any]],
    query_present: bool,
    query_range: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    if not entity_groups:
        return None
    for stored in line.get("entity_occurrences") or []:
        entity = next(
            (
                item
                for item in entity_groups
                if item["canonical"] == stored.get("canonical_name") or stored.get("raw_name") in item["aliases"]
            ),
            None,
        )
        if not entity:
            continue
        raw_name = str(stored.get("raw_name") or "")
        if query_present and stored.get("evidence_kind") != "speaker" and not alias_near_query(line.get("text", ""), raw_name, query_range):
            continue
        res: dict[str, Any] = {
            "entity_id": str(stored.get("entity_id") or ""),
            "canonical_entity": entity["canonical"],
            "matched_alias": raw_name,
            "presence_status": str(stored.get("presence_status") or ""),
            "evidence_kind": str(stored.get("evidence_kind") or ""),
            "occurrence_id": str(stored.get("occurrence_id") or ""),
            "confidence": float(stored.get("confidence") or 0.0),
        }
        if stored.get("ambiguity_candidates"):
            res["ambiguity_candidates"] = [str(c) for c in stored["ambiguity_candidates"]]
        return res

    doc = record.get("document") or {}
    for entity in entity_groups:
        speaker_alias = next((alias for alias in entity["aliases"] if line.get("speaker_raw") == alias), None)
        if speaker_alias:
            return {
                "canonical_entity": entity["canonical"],
                "matched_alias": speaker_alias,
                "presence_status": "explicit",
                "evidence_kind": "speaker",
            }
        text_alias = next(
            (
                alias
                for alias in text_aliases(entity)
                if alias_near_query(line.get("text", ""), alias, query_range if query_present else None)
            ),
            None,
        )
        if text_alias:
            return {
                "canonical_entity": entity["canonical"],
                "matched_alias": text_alias,
                "presence_status": "mentioned",
                "evidence_kind": "text_mention",
            }
        if (
            not query_present
            and str(doc.get("character_name") or "") in entity["aliases"]
            and line.get("line_number") == 1
        ):
            return {
                "canonical_entity": entity["canonical"],
                "matched_alias": str(doc.get("character_name") or entity["canonical"]),
                "presence_status": "explicit",
                "evidence_kind": "metadata_link",
            }
    return None


def line_match(
    record: dict[str, Any],
    index: int,
    request: dict[str, Any],
    regex: re.Pattern | None,
    entity_groups: list[dict[str, Any]],
    bounds: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    lines = record.get("lines") or []
    line = lines[index]
    if request["filters"]["speakers"] and line.get("speaker_raw") not in request["filters"]["speakers"]:
        return None
    content = line_content(line)
    if not matches_text(content, request["query"], request["match_mode"], regex):
        return None
    start = None
    end = None
    if request["query"]:
        if regex:
            match = regex.search(normalize_text(content))
            if match:
                start = match.start()
                end = match.end()
        else:
            haystack = normalize_text(content).lower()
            needle = request["query"].lower()
            start = haystack.find(needle)
            if start >= 0:
                end = start + len(needle)
            else:
                start = None
    occ = entity_occurrence(
        record,
        line,
        entity_groups,
        bool(request["query"]),
        {"start": start, "end": end} if start is not None and end is not None else None,
    )
    if entity_groups and not occ:
        return None
    if not request["context_terms"]:
        return {"occurrence": occ, "start": start, "end": end}

    start_bound = (bounds.get("start_line", 1) if bounds else 1) - 1
    end_bound = bounds.get("end_line", len(lines)) if bounds else len(lines)
    nearby = lines[max(start_bound, index - 3) : min(end_bound, index + 4)]
    constraint_lines = []
    for term in request["context_terms"]:
        term_lower = term.lower()
        found = next((item for item in nearby if term_lower in normalize_text(item.get("text", "")).lower()), None)
        if not found:
            return None
        constraint_lines.append(found.get("line_number"))

    seen_cl = set()
    dedup_cl = []
    for cl in constraint_lines:
        if cl not in seen_cl:
            seen_cl.add(cl)
            dedup_cl.append(cl)

    return {"occurrence": occ, "start": start, "end": end, "constraint_lines": dedup_cl}


def relevance_score(match: dict[str, Any], request: dict[str, Any], field: str) -> float:
    if field == "title":
        return 1.0 if match.get("exact") else 0.96
    if not request["query"]:
        score = 0.5
    elif request["match_mode"] == "regex":
        score = 0.7
    else:
        score = 1.0
    start = match.get("start")
    if start is not None and start > 0:
        score -= min(0.2, float(start) / 200.0)
    occ = match.get("occurrence")
    if occ:
        if occ.get("evidence_kind") == "speaker":
            score += 0.3
        elif occ.get("presence_status") == "explicit":
            score += 0.2
        elif occ.get("presence_status") == "mentioned":
            score += 0.05
    return max(0.0, min(1.0, round(score * 1000.0) / 1000.0))


def cluster_passages(candidates: list[dict[str, Any]], forced_truncated: bool = False) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for candidate in candidates:
        constraint_lines = (candidate.get("match") or {}).get("constraint_lines") or []
        cand_line_num = candidate["line"]["line_number"]
        all_lines = [cand_line_num, *constraint_lines]
        cand_start = min(all_lines)
        cand_end = max(all_lines)
        previous = clusters[-1] if clusters else None
        if (
            previous
            and (cand_start - previous["end"]) <= PASSAGE_CLUSTER_GAP
            and previous["best"].get("activity_range") == candidate.get("activity_range")
            and previous["best"].get("wiki_section") == candidate.get("wiki_section")
        ):
            previous["end"] = max(previous["end"], cand_end)
            previous["candidates"].append(candidate)
            if candidate["score"] > previous["best"]["score"]:
                previous["best"] = candidate
        else:
            clusters.append({
                "start": cand_start,
                "end": cand_end,
                "candidates": [candidate],
                "best": candidate,
            })

    # Difference: Deterministic sorting without JS localeCompare.
    clusters.sort(key=lambda c: (-c["best"]["score"], c["start"]))
    ranked = clusters

    result = []
    for cluster in ranked[:MAX_PASSAGES_PER_DOCUMENT]:
        match_lines_seen = set()
        match_lines = []
        for item in cluster["candidates"]:
            ln = item["line"]["line_number"]
            if ln not in match_lines_seen:
                match_lines_seen.add(ln)
                match_lines.append(ln)

        constraint_lines_seen = set()
        constraint_lines = []
        for item in cluster["candidates"]:
            for cl in (item.get("match") or {}).get("constraint_lines") or []:
                if cl not in constraint_lines_seen:
                    constraint_lines_seen.add(cl)
                    constraint_lines.append(cl)

        result.append({
            **cluster["best"],
            "passage_start": cluster["start"],
            "passage_end": cluster["end"],
            "passage_match_count": len(cluster["candidates"]),
            "match_lines": match_lines,
            "constraint_lines": constraint_lines,
            "document_passages_truncated": forced_truncated or len(ranked) > MAX_PASSAGES_PER_DOCUMENT,
        })
    return result


def searchable_title_text(document: dict[str, Any]) -> str:
    fields = [
        document.get("display_title"),
        document.get("story_name"),
        document.get("activity_name"),
        document.get("character_name"),
        document.get("story_code"),
        document.get("part_label"),
        document.get("collection_name"),
        document.get("mission_title"),
        document.get("story_key"),
    ]
    parts = [normalize_text(item) for item in fields if item is not None]
    return "\n".join(p for p in parts if p)


def is_exact_official_archive_title(document: dict[str, Any], request: dict[str, Any]) -> bool:
    if not request.get("query") or public_resource_type(document) != "archive":
        return False
    query = normalize_text(request["query"]).lower()
    return any(
        normalize_text(val).lower() == query
        for val in (document.get("display_title"), document.get("story_name"))
        if val is not None
    )


def line_allowed(line: dict[str, Any], filters: dict[str, Any]) -> bool:
    if not filters.get("speakers"):
        return True
    return line.get("speaker_raw") in filters["speakers"]


def readable_anchor(
    record: dict[str, Any], filters: dict[str, Any], ranges: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    lines = record.get("lines") or []

    def eligible(line: dict[str, Any]) -> bool:
        return (
            bool(normalize_text(line.get("text")))
            and line_allowed(line, filters)
            and (not ranges or wiki_section_at(ranges, line["line_number"]) is not None)
        )

    for line in lines:
        if line.get("line_type") == "dialogue" and eligible(line):
            return line
    for line in lines:
        if eligible(line):
            return line
    return None


def bounded_summary_text(value: Any, maximum: int) -> dict[str, Any]:
    text = normalize_text(value)
    if not text or maximum <= 0:
        return {"text": "", "truncated": bool(text)}
    chars = list(text)
    if len(chars) <= maximum:
        return {"text": text, "truncated": False}
    return {
        "text": f"{''.join(chars[:max(0, maximum - 1)])}…",
        "truncated": True,
    }


def public_entity_summary(record: dict[str, Any], maximum: int = 2400) -> dict[str, Any]:
    entity = record.get("entity") or {}
    attributes = entity.get("attributes") or {}
    doc = record.get("document") or {}
    canonical_name = normalize_text(entity.get("canonical_name") or doc.get("display_title"))
    desc = bounded_summary_text(attributes.get("description") or entity.get("description"), min(800, maximum))
    remaining = max(0, maximum - len(list(desc["text"])))
    history = bounded_summary_text(
        attributes.get("history_summary") or entity.get("history_summary"),
        min(1600, remaining),
    )
    res: dict[str, Any] = {
        "canonical_name": canonical_name,
        "truncated": desc["truncated"] or history["truncated"],
        "citation": f"《{natural_document_title(doc)}》",
    }
    if desc["text"]:
        res["description"] = desc["text"]
    if history["text"] and history["text"] != desc["text"]:
        res["history_summary"] = history["text"]
    return res


def evidence_kind(document: dict[str, Any], field: str, occurrence: dict[str, Any] | None) -> str:
    res = public_resource_type(document)
    if field in ("catalog", "title"):
        return "catalog"
    if occurrence and occurrence.get("evidence_kind") == "metadata_link":
        return "entity_projection"
    if res.endswith("_wiki"):
        return "wiki_curated"
    if res in ("story", "original_story", "operator_record"):
        return "official_canonical"
    if res == "archive" or res.startswith("character_"):
        return "official_structured"
    return "wiki_curated"


def match_kind(item: dict[str, Any], request: dict[str, Any]) -> str:
    if item.get("field") == "title":
        return "title"
    if item.get("field") == "catalog":
        return "catalog"
    if item.get("wiki_section"):
        return "section"
    occ = (item.get("match") or {}).get("occurrence")
    if occ and occ.get("evidence_kind") == "speaker":
        return "entity_speaker"
    if occ and occ.get("evidence_kind") == "text_mention":
        return "entity_mention"
    if occ:
        return "entity_projection"
    if not request["query"] and request["filters"]["speakers"]:
        return "speaker"
    return "regex" if request["match_mode"] == "regex" else "literal"


def excerpt(record: dict[str, Any], item: dict[str, Any], bounds: dict[str, int] | None = None) -> dict[str, Any]:
    lines = record.get("lines") or []
    start = max(bounds.get("start_line", 1) if bounds else 1, item["passage_start"] - PREVIEW_OPTIONS["before_lines"])
    end = min(bounds.get("end_line", len(lines)) if bounds else len(lines), item["passage_end"] + PREVIEW_OPTIONS["after_lines"])
    truncated = False
    excerpt_lines = []
    for line in lines[start - 1 : end]:
        raw = str(line_content(line) or "")
        raw_chars = list(raw)
        if len(raw_chars) > PREVIEW_OPTIONS["max_chars_per_line"]:
            text = f"{''.join(raw_chars[:PREVIEW_OPTIONS['max_chars_per_line']])}…"
            truncated = True
        else:
            text = raw
        role = (
            "match"
            if line["line_number"] in (item.get("match_lines") or [])
            else "constraint"
            if line["line_number"] in (item.get("constraint_lines") or [])
            else "context"
        )
        excerpt_lines.append({
            "line": line["line_number"],
            "role": role,
            "line_type": line.get("line_type") or "",
            "speaker": line.get("speaker_raw") or "",
            "text": text,
            "truncated": text != raw,
        })
    return {
        "lines": excerpt_lines,
        "characters": sum(len(line["text"]) for line in excerpt_lines),
        "truncated": truncated,
    }


def result_kind_for(request: dict[str, Any]) -> str:
    if request["query"]:
        return "text_matches"
    if request["filters"]["speakers"] or request["filters"]["entity_names"]:
        return "structured_matches"
    return "complete_sections" if request["filters"]["wiki_sections"] else "documents"


def line_scope_for(request: dict[str, Any]) -> bool:
    return bool(
        request["filters"]["speakers"]
        or request["filters"]["entity_names"]
        or request["filters"]["wiki_sections"]
        or request["context_terms"]
    )


async def candidate_document_ids(
    store: Any,
    request: dict[str, Any],
    regex: re.Pattern | None,
    signal: Any = None,
    deadline: float = float("inf"),
) -> list[str]:
    assert_search_active(signal, deadline, "候选文档发现")
    has_line_scope = line_scope_for(request)
    scoped_ids: list[str] = []
    ordered_ids = store.ordered_document_ids()
    for index, document_id in enumerate(ordered_ids):
        if (index & 255) == 0:
            assert_search_active(signal, deadline, "候选文档发现")
            await asyncio.sleep(0)
        item = store.documents.get(document_id)
        if (
            item
            and document_matches(item["document"], item.get("speakers") or [], request["filters"])
            and (
                getattr(store, "is_preferred_natural_document", None) is None
                or store.is_preferred_natural_document(document_id) is not False
            )
        ):
            scoped_ids.append(document_id)

    scoped = set(scoped_ids)
    scoped_pack_ids = pack_ids_for_document_ids(store, scoped_ids)
    title_ids: list[str] = []
    if request["query"] and not has_line_scope:
        for index, doc_id in enumerate(scoped_ids):
            if (index & 255) == 0:
                assert_search_active(signal, deadline, "候选标题发现")
                await asyncio.sleep(0)
            doc_item = store.documents.get(doc_id)
            if doc_item and matches_text(
                searchable_title_text(doc_item["document"]),
                request["query"],
                request["match_mode"],
                regex,
            ):
                title_ids.append(doc_id)

    def prioritize_official_title(ids: list[str]) -> list[str]:
        exact: list[str] = []
        for index, doc_id in enumerate(ids):
            if (index & 255) == 0:
                assert_search_active(signal, deadline, "候选文档排序")
            doc_item = store.documents.get(doc_id)
            if doc_item and is_exact_official_archive_title(doc_item.get("document") or {}, request):
                exact.append(doc_id)
        if not exact:
            return interleave_game_candidates(store, ids, request, signal=signal, deadline=deadline)
        selected = set(exact)
        remaining = [doc_id for doc_id in ids if doc_id not in selected]
        return exact + interleave_game_candidates(store, remaining, request, signal=signal, deadline=deadline)

    if not request["query"] or regex:
        return prioritize_official_title(scoped_ids)

    query_ngrams = ngrams_for(request["query"])
    indexed = None
    if query_ngrams and (hasattr(store, "find_documents_by_ngrams") or hasattr(store, "find_documents_by_trigrams")):
        lookup = getattr(store, "find_documents_by_ngrams", None) or getattr(store, "find_documents_by_trigrams", None)
        try:
            indexed = await lookup(query_ngrams, signal=signal, deadline=deadline, pack_ids=scoped_pack_ids)
        except TypeError:
            indexed = await lookup(query_ngrams)
    if indexed is None and query_ngrams and len(list(query_ngrams[0])) < 3:
        short_lookup = getattr(store, "find_documents_by_short_literal", None)
        if short_lookup:
            try:
                indexed = await short_lookup(
                    request["query"],
                    signal=signal,
                    deadline=deadline,
                    pack_ids=scoped_pack_ids,
                    document_ids=scoped_ids,
                )
            except TypeError:
                indexed = await short_lookup(request["query"])

    assert_search_active(signal, deadline, "候选文档发现")
    if indexed is None:
        return prioritize_official_title(scoped_ids)

    combined = [doc_id for doc_id in indexed if doc_id in scoped]
    for tid in title_ids:
        if tid not in combined:
            combined.append(tid)
    return prioritize_official_title(store.ordered_document_ids(combined))


def interleave_game_candidates(
    store: Any,
    document_ids: list[str],
    request: dict[str, Any],
    signal: Any = None,
    deadline: float = float("inf"),
) -> list[str]:
    requested = request["filters"]["games"] if request["filters"].get("games") else ["arknights", "endfield"]
    if len(requested) < 2:
        return document_ids
    queues: dict[str, list[str]] = {game: [] for game in requested}
    other: list[str] = []
    for index, doc_id in enumerate(document_ids):
        if (index & 255) == 0:
            assert_search_active(signal, deadline, "候选文档排序")
        loc = store.documents.get(doc_id)
        game = document_game(loc["document"]) if loc else "arknights"
        if game in queues:
            queues[game].append(doc_id)
        else:
            other.append(doc_id)

    nonempty = [q for q in queues.values() if q]
    if len(nonempty) < 2:
        return document_ids

    result: list[str] = []
    max_len = max(len(q) for q in nonempty)
    for idx in range(max_len):
        if (idx & 255) == 0:
            assert_search_active(signal, deadline, "候选文档排序")
        for game in requested:
            q = queues.get(game)
            if q and idx < len(q):
                result.append(q[idx])
    return result + other


def collect_document_group(
    record: dict[str, Any],
    request: dict[str, Any],
    regex: re.Pattern | None,
    entity_alias_groups: list[dict[str, Any]],
    deadline: float,
    signal: Any = None,
) -> dict[str, Any] | None:
    if not hydrated_record_matches(record, request["filters"]):
        return None
    metadata = record.get("document") or {}
    activity_ranges = requested_wiki_activity_ranges(record, request["filters"])
    if activity_ranges is not None and not activity_ranges:
        return None

    section_ranges = (
        wiki_section_ranges(record, request["filters"]["wiki_sections"])
        if request["filters"].get("wiki_sections")
        else []
    )
    if activity_ranges:
        intersected = []
        for sec in section_ranges:
            for act in activity_ranges:
                s_line = max(sec["start_line"], act["start_line"])
                e_line = min(sec["end_line"], act["end_line"])
                if s_line <= e_line:
                    intersected.append({**sec, "start_line": s_line, "end_line": e_line})
        section_ranges = intersected

    if request["filters"].get("wiki_sections") and not section_ranges:
        return None

    has_line_scope = line_scope_for(request) or bool(activity_ranges)
    catalog_mode = not request["query"] and not request["filters"]["speakers"] and not entity_alias_groups
    items: list[dict[str, Any]] = []
    title_text = searchable_title_text(metadata)

    if request["query"] and not has_line_scope and matches_text(title_text, request["query"], request["match_mode"], regex):
        first_section = section_ranges[0] if section_ranges else None
        lines = record.get("lines") or []
        if first_section:
            anchor = next((l for l in lines[first_section["start_line"] - 1 : first_section["end_line"]] if normalize_text(l.get("text"))), None)
        else:
            anchor = readable_anchor(record, request["filters"])
        if anchor:
            exact = (
                normalize_text(metadata.get("story_name")) == request["query"]
                or normalize_text(metadata.get("display_title")) == request["query"]
            )
            item_entry: dict[str, Any] = {
                "record": record,
                "line": anchor,
                "score": relevance_score({"exact": exact}, request, "title"),
                "field": "title",
                "passage_start": anchor["line_number"],
                "passage_end": anchor["line_number"],
                "passage_match_count": 1,
                "match": {"occurrence": None, "start": None, "end": None},
            }
            if first_section:
                item_entry["wiki_section"] = first_section
            items.append(item_entry)

    if catalog_mode:
        lines = record.get("lines") or []
        if section_ranges:
            for sec in section_ranges:
                anchor = next((l for l in lines[sec["start_line"] - 1 : sec["end_line"]] if normalize_text(l.get("text"))), None)
                if anchor:
                    items.append({
                        "record": record,
                        "line": anchor,
                        "score": 1.0,
                        "field": "wiki_section",
                        "passage_start": sec["start_line"],
                        "passage_end": sec["end_line"],
                        "passage_match_count": 1,
                        "match": {"occurrence": None, "start": None, "end": None},
                        "wiki_section": sec,
                    })
        else:
            anchor = readable_anchor(record, request["filters"], activity_ranges)
            if anchor:
                items.append({
                    "record": record,
                    "line": anchor,
                    "score": 1.0,
                    "field": "catalog",
                    "passage_start": anchor["line_number"],
                    "passage_end": anchor["line_number"],
                    "passage_match_count": 1,
                    "match": {"occurrence": None, "start": None, "end": None},
                })
    else:
        document_matches: list[dict[str, Any]] = []
        document_matches_truncated = False
        lines = record.get("lines") or []
        for index in range(len(lines)):
            if (index & 255) == 0:
                assert_search_active(signal, deadline)
            line_item = lines[index]
            act_range = wiki_section_at(activity_ranges, line_item["line_number"]) if activity_ranges else None
            if activity_ranges and not act_range:
                continue
            wiki_sec = wiki_section_at(section_ranges, line_item["line_number"]) if section_ranges else None
            if section_ranges and not wiki_sec:
                continue
            match_res = line_match(record, index, request, regex, entity_alias_groups, act_range)
            if not match_res:
                continue
            field_name = "content" if request["query"] else "entity" if match_res.get("occurrence") else "speaker_raw"
            cand_item: dict[str, Any] = {
                "record": record,
                "line": line_item,
                "score": relevance_score(match_res, request, "content"),
                "field": field_name,
                "match": match_res,
            }
            if wiki_sec:
                cand_item["wiki_section"] = wiki_sec
            if act_range:
                cand_item["activity_range"] = act_range
            document_matches.append(cand_item)
            if not has_line_scope and not regex and len(document_matches) >= SIMPLE_LITERAL_MATCH_CAP_PER_DOCUMENT:
                document_matches_truncated = True
                break
        items.extend(cluster_passages(document_matches, document_matches_truncated))

    if not items:
        return None

    # Difference: Deterministic sorting without JS localeCompare.
    # 上游比较器 (left.field==='title') - (right.field==='title') 使 title 命中排在最后，
    # 因而文档级“资料目录”条目不会挤占 MAX_PASSAGES_PER_DOCUMENT 的行命中名额。
    items.sort(key=lambda item: (
        1 if item.get("field") == "title" else 0,
        -item.get("score", 0.0),
        item.get("passage_start", 0),
    ))
    return {
        "record": record,
        "score": max(item.get("score", 0.0) for item in items),
        "items": items,
    }


def preview_lines_cost(lines: list[dict[str, Any]]) -> int:
    return sum(len(list(str(line.get("text") or ""))) for line in lines)


def preview_excerpt(group: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    if not item.get("preview"):
        item["preview"] = excerpt(group["record"], item, item.get("wiki_section") or item.get("activity_range"))
    return item["preview"]


def section_preview_lines(group: dict[str, Any]) -> list[dict[str, Any]]:
    if not group.get("sectionLines"):
        sections = [item.get("wiki_section") for item in group.get("items", []) if item.get("wiki_section")]
        lines = (group.get("record") or {}).get("lines") or []
        group["sectionLines"] = [
            line
            for line in lines
            if normalize_text(line.get("text"))
            and any(sec["start_line"] <= line["line_number"] <= sec["end_line"] for sec in sections)
        ]
    return group["sectionLines"]


def fit_lines_to_budget(lines: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    fitted = [dict(line) for line in lines]
    while preview_lines_cost(fitted) > budget and any(line.get("role") == "context" for line in fitted):
        last_context = max(i for i, line in enumerate(fitted) if line.get("role") == "context")
        fitted.pop(last_context)
    if preview_lines_cost(fitted) <= budget:
        return fitted
    target = next((line for line in fitted if line.get("role") == "match"), fitted[0] if fitted else None)
    if not target:
        return []
    other_cost = preview_lines_cost(fitted) - len(list(str(target.get("text") or "")))
    allowed = max(80, budget - other_cost)
    target_chars = list(str(target.get("text") or ""))
    if len(target_chars) > allowed:
        target["text"] = f"{''.join(target_chars[:max(1, allowed - 1)])}…"
        target["truncated"] = True
    return fitted


def measure_document(group: dict[str, Any], result_kind: str, request: dict[str, Any]) -> int:
    doc = (group.get("record") or {}).get("document") or {}
    if result_kind != "documents" and public_resource_type(doc) == "entity_profile":
        if not group.get("entitySummary"):
            group["entitySummary"] = public_entity_summary(group["record"], 2400)
        sum_obj = group["entitySummary"]
        return len(list(str(sum_obj.get("description") or ""))) + len(list(str(sum_obj.get("history_summary") or "")))
    if result_kind == "complete_sections":
        return sum(len(list(str(l.get("text") or ""))) for l in section_preview_lines(group))
    if result_kind == "documents":
        return 0
    total = 0
    for item in group.get("items", [])[:MAX_PASSAGES_PER_DOCUMENT]:
        if item.get("field") == "title":
            continue
        total += preview_lines_cost(preview_excerpt(group, item).get("lines") or [])
    return total


def build_public_document(
    store: Any, group: dict[str, Any], result_kind: str, request: dict[str, Any], budget: float = float("inf")
) -> dict[str, Any]:
    metadata = (group.get("record") or {}).get("document") or {}
    doc_id = metadata.get("document_id", "")
    title = natural_document_title(metadata)
    result: dict[str, Any] = {
        "game": document_game(metadata),
        "title": title,
        "resource_type": public_resource_type(metadata),
        "content_type": public_content_type(metadata),
        "matches": [],
        "matches_truncated": any(item.get("document_passages_truncated") for item in group.get("items", [])),
    }
    if getattr(store, "requires_document_uid", None) and store.requires_document_uid(doc_id):
        result["document_uid"] = document_uid(doc_id)
    coll_name = public_collection_name(metadata)
    if coll_name:
        result["collection_name"] = coll_name
    if metadata.get("activity_name"):
        result["activity_name"] = metadata["activity_name"]
    if metadata.get("character_name"):
        result["character_name"] = metadata["character_name"]

    reasons: set[str] = set()
    characters = 0
    if result["matches_truncated"]:
        reasons.add("document_passages")

    if result_kind != "documents" and result["resource_type"] == "entity_profile":
        cap = int(min(2400, budget))
        if cap == 2400 and group.get("entitySummary"):
            result["entity_summary"] = group["entitySummary"]
        else:
            result["entity_summary"] = public_entity_summary(group["record"], cap)
        result["matches_truncated"] = False
        characters = len(list(str(result["entity_summary"].get("description") or ""))) + len(
            list(str(result["entity_summary"].get("history_summary") or ""))
        )
        if result["entity_summary"].get("truncated"):
            reasons.add("entity_summary")
    elif result_kind == "complete_sections":
        lines = section_preview_lines(group)
        blocks = []
        for line in lines:
            text = str(line.get("text") or "")
            width = len(list(text))
            if characters + width > budget:
                if not blocks and budget > 0:
                    blocks.append({"type": "text", "text": f"{''.join(list(text)[:max(1, int(budget) - 1)])}…"})
                    characters = min(width, int(budget))
                reasons.add("section_content")
                break
            blocks.append({"type": "text", "text": text})
            characters += width
        first_item = group["items"][0] if group.get("items") else {}
        sec_obj = first_item.get("wiki_section")
        section_name = sec_obj.get("name") if sec_obj else request["filters"]["wiki_sections"][0]
        completeness = "complete" if ("section_content" not in reasons and len(blocks) == len(lines)) else "partial"
        result["section_content"] = {
            "section": section_name,
            "completeness": completeness,
            "blocks": blocks,
            "citation": f"《{title}》Wiki·{section_name}",
        }
    elif result_kind != "documents":
        for item in group.get("items", [])[:MAX_PASSAGES_PER_DOCUMENT]:
            title_only = item.get("field") == "title"
            shown = (
                {"lines": [], "characters": 0, "truncated": False}
                if title_only
                else preview_excerpt(group, item)
            )
            if result["matches"] and characters + shown["characters"] > budget:
                result["matches_truncated"] = True
                reasons.add("output_chars")
                reasons.add("document_passages")
                break
            available = max(0, int(budget - characters))
            shown_lines = (
                fit_lines_to_budget(shown["lines"], available)
                if shown["characters"] > available
                else shown["lines"]
            )
            shown_chars = preview_lines_cost(shown_lines)
            if shown.get("truncated") or shown_chars < shown["characters"]:
                reasons.add("line_chars")
            line_start = item.get("passage_start")
            line_end = item.get("passage_end")
            sec_name = (item.get("wiki_section") or {}).get("name")
            match_entry: dict[str, Any] = {
                "match_kind": match_kind(item, request),
                "evidence_kind": evidence_kind(metadata, item.get("field", ""), (item.get("match") or {}).get("occurrence")),
                "excerpt": shown_lines,
                "citation": (
                    f"《{title}》"
                    if title_only
                    else f"《{title}》Wiki·{sec_name}"
                    if sec_name
                    else f"《{title}》第 {line_start} 行"
                    if line_start == line_end
                    else f"《{title}》第 {line_start}-{line_end} 行"
                ),
            }
            if not sec_name and not title_only:
                match_entry["line_start"] = line_start
                match_entry["line_end"] = line_end
            result["matches"].append(match_entry)
            characters += shown_chars

    return {"document": result, "characters": characters, "reasons": reasons}


async def checkpoint_after_title(
    store: Any,
    after: dict[str, Any],
    request: dict[str, Any],
    signal: Any = None,
    deadline: float = float("inf"),
) -> dict[str, Any]:
    assert_search_active(signal, deadline, "分页锚点校验")
    if after.get("data_version") != store.data_version:
        raise ContractError("PAGE_ANCHOR_VERSION_MISMATCH", "分页锚点绑定到另一个资料版本，请重新搜索", retryable=False)
    regex = safe_regex(request["query"]) if request["match_mode"] == "regex" else None
    candidates = await candidate_document_ids(store, request, regex, signal=signal, deadline=deadline)
    assert_search_active(signal, deadline, "分页锚点校验")
    position = after["position"]
    document_id = candidates[position] if 0 <= position < len(candidates) else None
    location = store.documents.get(document_id) if document_id else None
    if not location:
        raise ContractError("PAGE_ANCHOR_NOT_FOUND", "找不到分页锚点，请重新搜索")
    doc = location.get("document") or {}
    if (
        public_resource_type(doc) != after["resource_type"]
        or normalize_text(natural_document_title(doc)) != normalize_text(after["title"])
    ):
        raise ContractError("PAGE_ANCHOR_MISMATCH", "分页锚点与当前资料版本不匹配，请重新搜索")
    return {
        "next_candidate_index": position + 1,
        "matched_documents_so_far": 0,
        "matched_count_known": False,
    }


async def execute_scan_search(
    store: Any,
    request: dict[str, Any],
    checkpoint: dict[str, Any],
    signal: Any = None,
    request_id: str | None = None,
    deadline: float = float("inf"),
) -> dict[str, Any]:
    resolved_request_id = str(request_id or f"req-{int(time.time() * 1000)}")
    try:
        assert_search_active(signal, deadline)
        regex = safe_regex(request["query"]) if request["match_mode"] == "regex" else None
        entity_alias_groups = await aliases_for(store, request["filters"]["entity_names"])
        assert_search_active(signal, deadline)
        candidate_ids = await candidate_document_ids(store, request, regex, signal=signal, deadline=deadline)
        assert_search_active(signal, deadline, "本地语料搜索初始化")
        result_kind = result_kind_for(request)
        documents: list[dict[str, Any]] = []
        reasons: set[str] = set()
        characters = 0
        scanned = 0
        matched_documents = checkpoint["matched_documents_so_far"]
        next_candidate_index = checkpoint["next_candidate_index"]
        stopped = False

        for candidate_index in range(len(candidate_ids)):
            if (candidate_index & 255) == 0:
                assert_search_active(signal, deadline)
            document_id = candidate_ids[candidate_index]
            if candidate_index < checkpoint["next_candidate_index"]:
                continue
            if len(documents) >= PAGE_DOCUMENTS or scanned >= SCAN_DOCUMENTS_PER_PAGE:
                next_candidate_index = candidate_index
                stopped = True
                break
            assert_search_active(signal, deadline)
            location = store.documents.get(document_id)
            scanned += 1
            if (
                not location
                or not document_matches(location["document"], location.get("speakers") or [], request["filters"])
                or (
                    getattr(store, "is_preferred_natural_document", None) is not None
                    and store.is_preferred_natural_document(document_id) is False
                )
            ):
                next_candidate_index = candidate_index + 1
                continue
            found = await store.get_document(document_id)
            assert_search_active(signal, deadline)
            if not found:
                next_candidate_index = candidate_index + 1
                continue
            group = collect_document_group(found["record"], request, regex, entity_alias_groups, deadline, signal)
            if not group:
                next_candidate_index = candidate_index + 1
                continue
            remaining = max(0, PREVIEW_OPTIONS["max_total_chars"] - characters)
            measured = measure_document(group, result_kind, request)
            if documents and measured > remaining:
                next_candidate_index = candidate_index
                reasons.add("output_chars")
                stopped = True
                break
            built = build_public_document(
                store,
                group,
                result_kind,
                request,
                budget=float(remaining) if documents else float(PREVIEW_OPTIONS["max_total_chars"]),
            )
            documents.append(built["document"])
            characters += built["characters"]
            for r in built["reasons"]:
                reasons.add(r)
            matched_documents += 1
            next_candidate_index = candidate_index + 1

        exhausted = not stopped
        count_known = checkpoint.get("matched_count_known") is not False
        if not exhausted:
            reasons.add("scan_incomplete")

        # Difference: No HMAC/legacy cursor chain; pagination is exclusively next_after.
        next_after = None
        if not exhausted:
            anchor_ordinal = next_candidate_index - 1
            anchor_doc_id = candidate_ids[anchor_ordinal] if 0 <= anchor_ordinal < len(candidate_ids) else None
            anchor_loc = store.documents.get(anchor_doc_id) if anchor_doc_id else None
            anchor_doc = anchor_loc.get("document") if anchor_loc else None
            if not anchor_doc:
                raise ContractError("PAGE_ANCHOR_INVALID", "无法生成下一页的标题锚点")
            next_after = {
                "data_version": store.data_version,
                "resource_type": public_resource_type(anchor_doc),
                "title": natural_document_title(anchor_doc),
                "position": anchor_ordinal,
            }

        page: dict[str, Any] = {
            "returned_documents": len(documents),
            "total_relation": "eq" if (exhausted and count_known) else "unknown",
            "has_more": not exhausted,
            "exhausted": exhausted,
            "next_after": next_after,
        }
        if exhausted and count_known:
            page["total_documents"] = matched_documents

        return {
            "result_kind": result_kind,
            "documents": documents,
            "page": page,
            "truncated": len(reasons) > 0,
            "truncation_reasons": list(reasons),
        }
    except Exception as error:
        return {
            "contract_version": SEARCH_CONTRACT_VERSION,
            "status": "error",
            "request_id": resolved_request_id,
            "data_version": getattr(store, "data_version", None),
            "error": public_search_error(error),
        }


async def execute_search(store: Any, args: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """主搜索入口（对应上游 executeSearch）。"""
    runtime = runtime or {}
    signal = runtime.get("signal")
    request_id = runtime.get("request_id") or runtime.get("requestId")
    allowed_games = runtime.get("allowed_games") or runtime.get("allowedGames") or ["arknights", "endfield"]

    deadline = float("inf")
    snapshot = None
    timeout_ms = runtime.get("timeout_ms")
    try:
        timeout_ms = float(timeout_ms)
    except (TypeError, ValueError):
        timeout_ms = float(SEARCH_TIMEOUT_MS)
    if not (timeout_ms > 0):
        timeout_ms = float(SEARCH_TIMEOUT_MS)
    timeout_ms = min(timeout_ms, 600_000.0)
    try:
        assert_search_active(signal, deadline)
        await store.ready()
        if getattr(store, "data_version", None) is None:
            raise ContractError("PACKAGE_NOT_INSTALLED", LOCAL_CORPUS_MISSING_MESSAGE, retryable=False)
        snapshot = corpus_version_snapshot(store)
        deadline = (time.time() * 1000.0) + timeout_ms
        assert_search_active(signal, deadline)

        normalized = normalized_request(args)
        requested_games = normalized["filters"]["games"] or allowed_games
        games = [g for g in requested_games if g in allowed_games]
        if not games:
            raise ContractError("INVALID_REQUEST", "请求的游戏资料库当前未启用，请先在 PRTS 资料设置中勾选", retryable=False)
        normalized["filters"]["games"] = games

        request = without_after(normalized)
        if normalized.get("after"):
            checkpoint = await checkpoint_after_title(store, normalized["after"], request, signal=signal, deadline=deadline)
        else:
            checkpoint = {
                "next_candidate_index": 0,
                "matched_documents_so_far": 0,
                "matched_count_known": True,
            }

        result = await execute_scan_search(
            store, request, checkpoint, signal=signal, request_id=request_id, deadline=deadline
        )
        assert_corpus_version(store, snapshot)

        # 检查未安装模块警告
        if result.get("status") != "error" and isinstance(store.packs, dict):
            pack_by_game = {"arknights": "official_game", "endfield": "endfield_official_game"}
            warnings = [
                {
                    "code": "CORPUS_GAME_NOT_INSTALLED",
                    "game": game,
                    "message": f"{'终末地' if game == 'endfield' else '明日方舟'}本地语料未安装，本次只检索其余已安装资料。",
                }
                for game in games
                if pack_by_game.get(game) not in store.packs
            ]
            if warnings:
                result = {**result, "warnings": warnings}

        result = await attach_retraveler_relations(store, result, request, allowed_games)
        assert_corpus_version(store, snapshot)
        return result
    except Exception as error:
        try:
            if snapshot:
                assert_corpus_version(store, snapshot)
        except Exception as changed:
            error = changed
        return {
            "contract_version": SEARCH_CONTRACT_VERSION,
            "status": "error",
            "request_id": str(request_id or ""),
            "data_version": getattr(store, "data_version", None),
            "error": public_search_error(error),
        }


def render_search(args: dict[str, Any], value: dict[str, Any]) -> str:
    """模型可见文本渲染（对应上游 renderSearch，各 content 块以 \\n 拼接）。"""
    if value and value.get("error"):
        err = value["error"]
        return f"[prts_search:error] {err.get('code')}: {err.get('message')}"
    return project_search(value, {"query": normalize_text((args or {}).get("query"))})


# 上游命名兼容
executeSearch = execute_search
renderSearch = render_search
safeRegex = safe_regex
