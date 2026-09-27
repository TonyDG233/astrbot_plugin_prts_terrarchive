"""终末地官方本地化文本检索与多语言对齐（corpus_i18n 契约实现）。

- 支持 query 反查（exact / literal）
- 支持完整 title 或 document_uid + 可选 line 定位
- 支持 text_ids 显式复查
- 游戏内富文本标记清洗（<image> <color> <size> <@...> <b> <i> <br> 等）并做 NFKC 规约
- 懒加载多语言字典并使用 LRU 缓存保持内存上限在 ~50MB 内
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import json
import re
import unicodedata
import weakref
from typing import Any

from .constants import CONTRACT_VERSION, LANGUAGE_CODES
from .errors import ContractError
from .store import (
    assert_corpus_version,
    corpus_version_snapshot,
    document_game,
    document_uid,
    natural_document_title,
)

PACK = "endfield_official_game"
LOCALIZATION_LANGUAGES = LANGUAGE_CODES
FIELDS = ("text", "name", "title", "actor", "hint")

LANGUAGE_ALIASES: dict[str, str] = {
    "zh-cn": "CN",
    "zh-hans": "CN",
    "en": "EN",
    "ja": "JP",
    "ko": "KR",
    "zh-tw": "TC",
    "zh-hant": "TC",
    "es-mx": "MX",
    "pt-br": "BR",
    "fr": "FR",
    "de": "DE",
    "ru": "RU",
    "it": "IT",
    "id": "ID",
    "th": "TH",
    "vi": "VN",
    "en-us": "EN",
    "ja-jp": "JP",
    "ko-kr": "KR",
    "fr-fr": "FR",
    "de-de": "DE",
    "ru-ru": "RU",
    "it-it": "IT",
    "id-id": "ID",
    "th-th": "TH",
    "vi-vn": "VN",
}

I18N_DESCRIPTION = [
    "查询终末地官方本地化文本，返回其他语言原文；不生成翻译。",
    "已有资料时优先使用完整 title 或 document_uid，可加 line；只有原句/名称时用 query 反查，source_language 默认 CN。",
    "languages 指定目标语言（EN 英语、JP 日语、KR 韩语、TC 繁中等）。query 默认 exact，片段用 match_mode=literal。",
    "相同原文可能对应多个不同译文，按来源消歧。角色档案按整条记录、档案库按整篇内容块对齐，中文行号不是目标语言行号。",
    "默认不显示内部文本 ID；仅核验时 include_ids=true，后续可用 text_ids 精确查询。续页原样提交 page.continuation。",
]

I18N_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["languages"],
    "properties": {
        "game": {"type": "string", "enum": ["endfield"], "description": "当前支持终末地官方资料"},
        "query": {"type": "string", "description": "名称或原句；仅有片段时使用 match_mode=literal"},
        "title": {"type": "string", "description": "已有检索结果的完整篇章标题，与 query/document_uid/text_ids 四选一"},
        "document_uid": {"type": "string", "description": "已有检索结果的文档定位，替代 title"},
        "line": {"type": "integer", "description": "已有中文结果中的行号，只用于定位对应文本/记录"},
        "text_ids": {"type": "array", "items": {"type": "string"}, "description": "显式请求 ID 后可用于精确复查；普通查询不需要"},
        "source_language": {"type": "string", "description": "原句的语言，默认 CN；也接受 en/ja/ko/zh-CN 等标准代码"},
        "languages": {"type": "array", "items": {"type": "string"}, "description": "1–4 种目标语言：CN/EN/JP/KR/TC/MX/BR/FR/DE/RU/IT/ID/TH/VN"},
        "match_mode": {"type": "string", "enum": ["exact", "literal"], "description": "原句精确匹配或字面量片段搜索，默认 exact"},
        "include_ids": {"type": "boolean", "description": "默认 false；仅调试、核验或精确复查时返回内部文本 ID 与来源字段"},
        "max_matches": {"type": "integer", "description": "每页候选数，默认 5，最多 20"},
        "max_chars": {"type": "integer", "description": "每页原文和译文的字符预算，默认 16000，范围 1000–48000"},
        "data_version": {"type": "string", "description": "绑定已有结果的资料版本，避免混用版本"},
        "after": {
            "type": "object",
            "additionalProperties": False,
            "required": ["data_version", "request_hash", "position", "text_offset"],
            "properties": {
                "data_version": {"type": "string"},
                "request_hash": {"type": "string"},
                "position": {"type": "integer"},
                "text_offset": {"type": "integer"},
            },
            "description": "仅原样提交工具返回的续页位置；长记录会明确分块返回",
        },
    },
}

I18N_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "not_found", "unmapped_source", "unavailable"]},
        "game": {"type": "string"},
        "data_version": {"type": "string"},
        "game_version": {"type": "string"},
        "source_language": {"type": "string"},
        "languages": {"type": "array", "items": {"type": "string"}},
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "object"},
                    "translations": {"type": "object"},
                    "references": {"type": "array", "items": {"type": "object"}},
                    "reference_count": {"type": "integer"},
                    "text_id": {"type": "string"},
                    "source_fields": {"type": "array", "items": {"type": "object"}},
                },
            },
        },
        "page": {"type": "object"},
        "message": {"type": "string"},
        "note": {"type": "string"},
    },
}

try:
    _states: Any = weakref.WeakKeyDictionary()
except Exception:
    _states = {}


def _state_for(store) -> dict:
    gen = getattr(store, "_generation", 0)
    data_ver = getattr(store, "data_version", getattr(store, "dataVersion", None))
    state = None
    try:
        state = _states.get(store)
    except (TypeError, KeyError):
        state = getattr(store, "_i18n_state", None)

    if not state or state.get("generation") != gen or state.get("version") != data_ver:
        state = {
            "generation": gen,
            "version": data_ver,
            "catalog": None,
            "languages": OrderedDict(),
            "pending": {},
        }
        try:
            _states[store] = state
        except (TypeError, KeyError):
            try:
                store._i18n_state = state
            except Exception:
                pass
    return state


def _is_aborted(runtime: dict | None) -> bool:
    if not runtime:
        return False
    signal = runtime.get("signal")
    if signal is None and isinstance(runtime.get("options"), dict):
        signal = runtime["options"].get("signal")
    if signal is None:
        return False
    checker = getattr(signal, "is_set", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(signal, "aborted", False))


def checkpoint(store, snapshot: dict | None, runtime: dict | None = None) -> None:
    assert_corpus_version(store, snapshot)
    if _is_aborted(runtime):
        raise ContractError("CANCELLED", "本地化查询已取消")


def normalize_text_i18n(value: Any) -> str:
    """清洗游戏内富文本标记并归一化为倒排搜索词。"""
    text = str(value if value is not None else "")
    text = re.sub(r"<image[^>]*>[\s\S]*?</image>|<image\s*=[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<@[^>]*>|</>|</?(?:color|b|i|size)(?:=[^>]*)?>", "", text, flags=re.IGNORECASE)
    normalized = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", normalized).strip()


def parse_language(value: Any) -> str:
    if not isinstance(value, str):
        raise ContractError("INVALID_REQUEST", "语言代码必须是字符串")
    result = LANGUAGE_ALIASES.get(value.lower(), value.upper())
    if result not in LANGUAGE_CODES:
        raise ContractError("INVALID_REQUEST", f"不支持的语言：{value}")
    return result


def check_integer(value: Any, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ContractError("INVALID_REQUEST", f"{name} 必须在 {minimum}–{maximum} 之间")
    return value


def normalize_i18n_request(args: dict) -> OrderedDict:
    """校验并归一化本地化请求参数。"""
    if not isinstance(args, dict):
        raise ContractError("INVALID_REQUEST", "prts_i18n 包含不支持的参数")
    allowed_props = set(I18N_PARAMETERS["properties"].keys())
    if any(k not in allowed_props for k in args.keys()):
        raise ContractError("INVALID_REQUEST", "prts_i18n 包含不支持的参数")

    if args.get("game") is not None and args["game"] != "endfield":
        raise ContractError("INVALID_REQUEST", "当前仅支持终末地官方本地化")

    locators = [k for k in ("query", "title", "document_uid", "text_ids") if args.get(k) is not None]
    if len(locators) != 1:
        raise ContractError("INVALID_REQUEST", "query/title/document_uid/text_ids 必须且只能提供一种")
    locator = locators[0]

    request = OrderedDict()
    request["game"] = "endfield"

    if locator == "text_ids":
        val = args["text_ids"]
        if (
            not isinstance(val, list)
            or len(val) == 0
            or len(val) > 20
            or any(not isinstance(tid, str) or not re.match(r"^-?[0-9]{1,20}$", tid) for tid in val)
        ):
            raise ContractError("INVALID_REQUEST", "text_ids 必须是 1–20 个十进制字符串 ID，不能使用数字")
        request["text_ids"] = list(dict.fromkeys(val))
    else:
        val = args[locator]
        if not isinstance(val, str) or not val.strip() or len(val) > 12000:
            raise ContractError("INVALID_REQUEST", f"{locator} 必须是非空字符串，最多 12000 字符")
        request[locator] = val.strip()
        if locator == "query" and not normalize_text_i18n(request["query"]):
            raise ContractError("INVALID_REQUEST", "query 必须包含可查询的文本")
        if locator == "document_uid" and not re.match(r"^doc_[A-Za-z0-9_-]{16}$", request["document_uid"]):
            raise ContractError("INVALID_REQUEST", "document_uid 必须来自检索结果")

    if "line" in args and args["line"] is not None:
        if locator not in ("title", "document_uid"):
            raise ContractError("INVALID_REQUEST", "line 只能与文档定位一起使用")
        request["line"] = check_integer(args["line"], 1, 100_000_000, "line")

    langs = args.get("languages")
    if not isinstance(langs, list) or len(langs) < 1 or len(langs) > 4:
        raise ContractError("INVALID_REQUEST", "languages 必须包含 1–4 种目标语言")

    request["source_language"] = parse_language(args.get("source_language", "CN"))
    request["languages"] = list(dict.fromkeys(parse_language(l) for l in langs))

    request["match_mode"] = args.get("match_mode", "exact")
    if request["match_mode"] not in ("exact", "literal"):
        raise ContractError("INVALID_REQUEST", "match_mode 只支持 exact/literal")

    if "include_ids" in args and args["include_ids"] is not None:
        if not isinstance(args["include_ids"], bool):
            raise ContractError("INVALID_REQUEST", "include_ids 必须是布尔值")
    request["include_ids"] = args.get("include_ids", False)

    request["max_matches"] = check_integer(args.get("max_matches", 5), 1, 20, "max_matches")
    request["max_chars"] = check_integer(args.get("max_chars", 16000), 1000, 48000, "max_chars")

    if "data_version" in args and args["data_version"] is not None:
        dv = args["data_version"]
        if not isinstance(dv, str) or not re.match(r"^[0-9a-f]{64}$", dv):
            raise ContractError("INVALID_REQUEST", "data_version 必须是有效的 SHA-256")
        request["data_version"] = dv

    if "after" in args and args["after"] is not None:
        after = args["after"]
        if (
            not isinstance(after, dict)
            or sorted(after.keys()) != ["data_version", "position", "request_hash", "text_offset"]
            or not isinstance(after.get("data_version"), str)
            or not re.match(r"^[0-9a-f]{64}$", after["data_version"])
            or not isinstance(after.get("request_hash"), str)
            or not re.match(r"^[0-9a-f]{64}$", after["request_hash"])
        ):
            raise ContractError("INVALID_REQUEST", "after 必须原样使用工具返回的续页位置")
        request["after"] = {
            **after,
            "position": check_integer(after.get("position"), 0, 1_000_000, "after.position"),
            "text_offset": check_integer(after.get("text_offset"), 0, 10_000_000, "after.text_offset"),
        }

    return request


def _parse_jsonl(plain_bytes: bytes) -> list:
    text = plain_bytes.decode("utf-8")
    result = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            result.append(json.loads(line))
    return result


async def load_rows(store, snapshot: dict, asset: dict, runtime: dict | None = None) -> list:
    try:
        reader = getattr(store, "_read_packed", getattr(store, "_readPacked", None))
        if reader is None:
            raise ContractError("INTERNAL_ERROR", "Store does not implement _read_packed")
        rel_id = getattr(store, "release_id", getattr(store, "releaseId", None))
        plain_bytes = await reader(PACK, asset["path"], rel_id, asset)
        assert_corpus_version(store, snapshot)
        return await asyncio.to_thread(_parse_jsonl, plain_bytes)
    except Exception as error:
        assert_corpus_version(store, snapshot)
        raise ContractError("INDEX_CORRUPT", f"本地化附件读取失败：{error}")


async def _build_catalog(store, meta: dict, snapshot: dict, runtime: dict | None = None) -> dict:
    rows = await load_rows(store, snapshot, meta.get("catalog") or {}, runtime)
    by_id: dict[str, dict] = {}
    by_document: dict[str, set[str]] = {}

    for entry in rows:
        text_id = entry.get("text_id")
        if (
            not isinstance(text_id, str)
            or not re.match(r"^-?[0-9]{1,20}$", text_id)
            or text_id in by_id
            or not isinstance(entry.get("references"), list)
            or len(entry["references"]) == 0
            or not isinstance(entry.get("sources"), list)
        ):
            raise ContractError("INDEX_CORRUPT", "本地化目录记录无效")
        by_id[text_id] = entry

        for ref in entry["references"]:
            doc_id = ref.get("document_id")
            doc_info = store.documents.get(doc_id) if hasattr(store, "documents") else None
            doc = doc_info.get("document", doc_info) if isinstance(doc_info, dict) else None
            pack_id = doc_info.get("pack_id") or doc_info.get("packId") if isinstance(doc_info, dict) else None
            line_start = ref.get("line_start")
            line_end = ref.get("line_end")
            source_order = ref.get("source_order")
            field = ref.get("field")
            alignment = ref.get("alignment")

            if (
                not doc
                or document_game(doc) != "endfield"
                or pack_id != PACK
                or not isinstance(line_start, int)
                or isinstance(line_start, bool)
                or not isinstance(line_end, int)
                or isinstance(line_end, bool)
                or line_start < 1
                or line_end < line_start
                or line_end > (doc.get("line_count") or 0)
                or (source_order is not None and (not isinstance(source_order, int) or isinstance(source_order, bool) or source_order < 1))
                or field not in FIELDS
                or alignment not in ("text", "record", "document")
            ):
                raise ContractError("INDEX_CORRUPT", "本地化目录指向无效的官方来源")

            ids = by_document.setdefault(doc_id, set())
            ids.add(text_id)

    if len(rows) != meta.get("text_count"):
        raise ContractError("INDEX_CORRUPT", "本地化目录数量不一致")
    return {"rows": rows, "by_id": by_id, "by_document": by_document}


async def catalog_for(store, state: dict, meta: dict, snapshot: dict, runtime: dict | None = None) -> dict:
    if state.get("catalog") is not None:
        return state["catalog"]

    if "_catalog_future" not in state or state["_catalog_future"] is None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        state["_catalog_future"] = future
        try:
            cat = await _build_catalog(store, meta, snapshot, runtime)
            state["catalog"] = cat
            future.set_result(cat)
            return cat
        except BaseException as exc:
            state["_catalog_future"] = None
            future.set_exception(exc)
            raise
    else:
        return await asyncio.shield(state["_catalog_future"])


def _parse_dictionary_rows(rows: list, catalog_by_id: dict, meta_text_count: int):
    by_id: dict[str, str] = {}
    exact: dict[str, list[str]] = {}
    search: list[tuple[str, str]] = []

    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not isinstance(row[0], str)
            or not isinstance(row[1], str)
            or row[0] not in catalog_by_id
            or row[0] in by_id
        ):
            raise ContractError("INDEX_CORRUPT", "本地化字典记录无效")
        text_id, text = row[0], row[1]
        by_id[text_id] = text
        key = normalize_text_i18n(text)
        if key:
            exact.setdefault(key, []).append(text_id)
            search.append((text_id, key))

    if len(by_id) != meta_text_count:
        raise ContractError("INDEX_CORRUPT", "本地化字典数量不一致")
    return by_id, exact, search


async def dictionary_for(store, state: dict, meta: dict, catalog: dict, lang: str, snapshot: dict, runtime: dict | None = None) -> dict | None:
    languages_meta = meta.get("languages") or {}
    if lang not in languages_meta:
        return None

    lang_cache = state["languages"]
    if lang in lang_cache:
        cached = lang_cache[lang]
        lang_cache.move_to_end(lang)
        return cached

    pending = state["pending"]
    if lang not in pending:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending[lang] = future
        try:
            asset = languages_meta[lang]
            rows = await load_rows(store, snapshot, asset, runtime)
            by_id, exact, search = await asyncio.to_thread(_parse_dictionary_rows, rows, catalog["by_id"], meta.get("text_count", -1))
            assert_corpus_version(store, snapshot)
            val = {"by_id": by_id, "exact": exact, "search": search}
            lang_cache[lang] = val
            while len(lang_cache) > 4:
                lang_cache.popitem(last=False)
            future.set_result(val)
            return val
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            pending.pop(lang, None)
    else:
        return await asyncio.shield(pending[lang])


def public_reference(store, ref: dict) -> dict:
    doc_info = store.documents.get(ref["document_id"]) if hasattr(store, "documents") else None
    doc = doc_info.get("document", doc_info) if isinstance(doc_info, dict) else {}
    title = natural_document_title(doc)
    uid = document_uid(doc.get("document_id", ""))
    line_start = ref["line_start"]
    line_end = ref["line_end"]
    range_part = f"–{line_end}" if line_end != line_start else ""
    citation = f"《{title}》中文定位第 {line_start}{range_part} 行"
    return {
        "title": title,
        "document_uid": uid,
        "line_start": line_start,
        "line_end": line_end,
        "field": ref.get("field"),
        "alignment": ref.get("alignment"),
        "citation": citation,
    }


def chunk(text: str | None, offset: int, width: int, available: bool) -> dict:
    if not available:
        return {"status": "unavailable"}
    if text is None or not text.strip():
        return {"status": "missing_localization"}
    chars = list(text)
    start = min(offset, len(chars))
    end = min(offset + width, len(chars))
    res: dict[str, Any] = {
        "status": "available",
        "text": "".join(chars[start:end]),
    }
    if offset > 0 or end < len(chars):
        res["character_start"] = start
        res["character_end"] = end
        res["total_characters"] = len(chars)
        res["truncated"] = end < len(chars)
    return res


async def execute_i18n(store, args: dict, runtime: dict | None = None) -> dict:
    """执行 corpus_i18n 查询。"""
    request = normalize_i18n_request(args)
    enabled_games = None
    if isinstance(runtime, dict):
        enabled_games = runtime.get("enabled_games") or runtime.get("enabledGames")
        if enabled_games is None and isinstance(runtime.get("options"), dict):
            enabled_games = runtime["options"].get("enabled_games") or runtime["options"].get("enabledGames")
    if enabled_games is None:
        enabled_games = ["endfield"]
    if "endfield" not in enabled_games:
        raise ContractError("INVALID_REQUEST", "当前未启用终末地资料")

    await store.ready()
    snapshot = corpus_version_snapshot(store)
    checkpoint(store, snapshot, runtime)

    snapshot_version = snapshot.get("data_version") or snapshot.get("dataVersion")
    if request.get("data_version") and request["data_version"] != snapshot_version:
        raise ContractError("PACKAGE_VERSION_MISMATCH", "本地化查询所属资料版本已变化，请重新查询", retryable=True)
    if request.get("after") and request["after"].get("data_version") != snapshot_version:
        raise ContractError("PACKAGE_VERSION_MISMATCH", "本地化查询所属资料版本已变化，请重新查询", retryable=True)

    after = request.get("after")
    identity = OrderedDict((k, v) for k, v in request.items() if k not in ("after", "data_version"))
    request_hash = hashlib.sha256(json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    if after and after.get("request_hash") != request_hash:
        raise ContractError("INVALID_REQUEST", "续页参数与原查询不一致")

    base = {
        "game": "endfield",
        "data_version": snapshot_version,
        "source_language": request["source_language"],
        "languages": request["languages"],
    }
    pack = store.packs.get(PACK) if hasattr(store, "packs") else None
    meta = pack.get("localization") if isinstance(pack, dict) else None
    if not meta:
        return {
            **base,
            "status": "unavailable",
            "matches": [],
            "message": "当前终末地官方资料包未包含多语言附件，请安装带本地化数据的版本。",
            "page": {"has_more": False, "continuation": None},
        }

    base["game_version"] = meta.get("game_version")
    state = _state_for(store)
    catalog = await catalog_for(store, state, meta, snapshot, runtime)
    checkpoint(store, snapshot, runtime)

    requested_languages = list(dict.fromkeys([request["source_language"]] + request["languages"]))
    dict_results = await asyncio.gather(*(
        dictionary_for(store, state, meta, catalog, lang, snapshot, runtime)
        for lang in requested_languages
    ))
    checkpoint(store, snapshot, runtime)

    dictionaries = dict(zip(requested_languages, dict_results))
    source = dictionaries.get(request["source_language"])
    if not source:
        return {
            **base,
            "status": "unavailable",
            "matches": [],
            "message": "当前附件没有查询所用的来源语言。",
            "page": {"has_more": False, "continuation": None},
        }

    ids: list[str] = []
    document_id: str | None = None

    if "query" in request:
        query_key = normalize_text_i18n(request["query"])
        if request["match_mode"] == "exact":
            ids = list(source["exact"].get(query_key, []))
        else:
            for i, (text_id, search_text) in enumerate(source["search"]):
                if (i & 2047) == 0:
                    await asyncio.sleep(0)
                    checkpoint(store, snapshot, runtime)
                if query_key in search_text:
                    ids.append(text_id)
    elif "text_ids" in request:
        ids = [tid for tid in request["text_ids"] if tid in catalog["by_id"]]
    else:
        if "document_uid" in request:
            getter = getattr(store, "get_document_by_uid", getattr(store, "getDocumentByUid", None))
            found = await getter(request["document_uid"]) if getter else None
        else:
            getter = getattr(store, "get_document_by_title", getattr(store, "getDocumentByTitle", None))
            found = await getter(request["title"]) if getter else None
        checkpoint(store, snapshot, runtime)

        if not found:
            raise ContractError("DOCUMENT_NOT_FOUND", "找不到指定资料，请使用检索结果中的完整定位")
        found_pack_id = found.get("pack_id") or found.get("packId")
        if found_pack_id != PACK:
            raise ContractError("INVALID_REQUEST", "该资料不是终末地官方原文")

        document_id = found["record"]["document"]["document_id"]
        doc_lines = found["record"].get("lines") or []
        req_line = request.get("line")
        if req_line and req_line > len(doc_lines):
            raise ContractError("LINE_RANGE_INVALID", "中文定位行号超出文档范围")

        raw_doc_ids = catalog["by_document"].get(document_id, set())
        for tid in raw_doc_ids:
            text = source["by_id"].get(tid, "")
            # An absent source translation still has a valid identity. Omit only
            # image/markup-only blocks, retaining explicit missing_localization.
            if not text.strip() or normalize_text_i18n(text):
                entry = catalog["by_id"][tid]
                matches_ref = any(
                    ref["document_id"] == document_id
                    and (not req_line or (ref["line_start"] <= req_line <= ref["line_end"]))
                    for ref in entry["references"]
                )
                if matches_ref:
                    ids.append(tid)

        def source_order_key(tid: str):
            entry = catalog["by_id"][tid]
            ref = next(
                r for r in entry["references"]
                if r["document_id"] == document_id
                and (not req_line or (r["line_start"] <= req_line <= r["line_end"]))
            )
            field_order = (
                FIELDS.index(ref["field"]) if ref["field"] in FIELDS else 99
            )
            if ref.get("alignment") != "document":
                return (ref["line_start"], field_order, "")
            return (ref.get("source_order") or 0, 0, "")

        ids.sort(key=source_order_key)

    position = after["position"] if after else 0
    offset = after["text_offset"] if after else 0
    if position > len(ids) or (position == len(ids) and offset):
        raise ContractError("INVALID_REQUEST", "续页位置超出查询结果")

    matches = []
    budget = request["max_chars"]

    while position < len(ids) and len(matches) < request["max_matches"] and budget >= 100:
        checkpoint(store, snapshot, runtime)
        tid = ids[position]
        entry = catalog["by_id"][tid]
        width = budget // (len(request["languages"]) + 1)
        source_val = chunk(source["by_id"].get(tid), offset, width, True)
        translations = {
            lang: chunk(
                dictionaries[lang]["by_id"].get(tid) if dictionaries.get(lang) else None,
                offset,
                width,
                dictionaries.get(lang) is not None,
            )
            for lang in request["languages"]
        }
        values = [source_val] + list(translations.values())
        max_len = max(
            len(list(dictionaries[l]["by_id"].get(tid) or "")) if dictionaries.get(l) else 0
            for l in requested_languages
        )
        if offset and offset >= max_len:
            raise ContractError("INVALID_REQUEST", "记录续页偏移超出正文")

        refs = [r for r in entry["references"] if not document_id or r.get("document_id") == document_id]
        item: dict[str, Any] = {
            "source": {"language": request["source_language"], **source_val},
            "translations": translations,
            "references": [public_reference(store, r) for r in refs[:8]],
            "reference_count": len(refs),
        }
        if request["include_ids"]:
            item["text_id"] = tid
            item["source_fields"] = entry.get("sources", [])[:8]
            item["source_field_count"] = len(entry.get("sources", []))
        matches.append(item)

        budget -= sum(len(list(v.get("text") or "")) for v in values)
        if any(v.get("truncated") for v in values):
            offset += width
            break
        position += 1
        offset = 0

    checkpoint(store, snapshot, runtime)
    has_more = position < len(ids)
    status = "ok" if ids else ("unmapped_source" if document_id else "not_found")
    res: dict[str, Any] = {
        **base,
        "status": status,
        "matches": matches,
        "total_matches": len(ids),
    }
    if "text_ids" in request:
        res["missing_text_ids"] = [tid for tid in request["text_ids"] if tid not in catalog["by_id"]]
    res["note"] = "返回官方本地化原文；引用请注明语言。中文行号仅用于定位，record/document 对齐不表示跨语言逐行对应。"
    res["page"] = {
        "has_more": has_more,
        "continuation": {
            **identity,
            "data_version": snapshot_version,
            "after": {
                "data_version": snapshot_version,
                "request_hash": request_hash,
                "position": position,
                "text_offset": offset,
            },
        } if has_more else None,
    }
    return res


def render_i18n(_args: dict, value: dict) -> str:
    """将契约响应序列化为模型可见的 JSON 字符串。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# 驼峰别名（兼容调用）
normalizeI18nRequest = normalize_i18n_request
executeI18n = execute_i18n
renderI18n = render_i18n
