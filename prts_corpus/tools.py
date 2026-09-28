"""prts_* LLM 工具：JSON Schema、AstrBot 事件适配、证据去重与模型可见渲染。

与宿主约定：
- 每个工具的 handler 接收 `event`（AstrBot 本地工具调用约定），返回 `str`。
- `handler_module_path` 记为插件 main 模块路径，卸载时由 star_manager 自动回收。
- handler 定义在 `prts_corpus.tools`，`__module__` 与插件模块不同，因此不会被
  star_manager 重新包装为 star_cls partial（保持 `handler(event, **args)` 直调）。
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any, Awaitable, Callable

from astrbot.api import FunctionTool

from . import constants
from .errors import ContractError
from .i18n import (
    I18N_DESCRIPTION,
    I18N_PARAMETERS,
    execute_i18n,
    render_i18n,
)
from .read import execute_read, model_read_to_contract, project_read_public, render_read
from .search import execute_search, render_search
from .store import document_uid
from .timeline import execute_timeline_search, render_timeline

# ---- 模型可见描述（index.js 逐字移植；去掉旧 cursor 说明） ----

SEARCH_DESCRIPTION = " ".join([
    "像 grep 一样搜索 PRTS.chat 本地语料；命中立即返回原行及上下各一行，并按文档归并。",
    "query 使用短实体名、篇章展示名或原句片段；也可省略 query，仅按过滤条件列出资料入口。",
    "角色个人页用 character_wiki；活动/密录整理页用 story_wiki；角色在单个活动中的辅助整理用 character_activity_wiki。"
    "wiki_sections 可精确限定相关活动、相关角色、剧情总结、角色剧情概括等标签字段。",
    "literal 是默认连续字面匹配；只有特殊模式才使用受限 regex。新查询的下一页保留原搜索条件，"
    "并把返回的 next_after 原样放入 after；锚点由完整资料版本、资料类型与自然标题组成，不再暴露内部 cursor。",
])

READ_DESCRIPTION = " ".join([
    "读取 PRTS.chat 本地资料。明日方舟关卡用 stage_code，只有多篇时才填 story_part；"
    "干员密录用 character_name + record_name + segment；角色资料用 character_name + material。",
    "整个明日方舟活动用 activity_name + mode=activity，终末地任务用 collection_name + mode=collection；"
    "合集续页原样提交 page.continuation 的 position。其他资料使用完整 title。",
    "搜索结果若给出 document_uid，说明标题或合集同名：它替代 title，读取时只提交 document_uid，"
    "不得同时提交 title；单篇可配 line 或 mode=document，所属合集可配 mode=activity/collection。",
    "line 扩大单篇原文上下文，section 读取 Wiki 字段，mode=document 分页全文。所有续页都原样提交 page.continuation。",
    "引用原文使用“《篇章名》第 N 行”；同名结果保留工具给出的 document_uid 作为唯一定位器。"
    "不要使用内部代号、路径或自造篇章名。",
])

TIMELINE_DESCRIPTION = " ".join([
    "按活动名、年份及自动展开的实体别名检索活动时间线（PRTS Wiki《泰拉年表》本地投影）。",
    "人物放 entity_names 以自动裂变别名；结果只给时间、事件正文和“年表出处”标记，"
    "把标记原样传回 source_marker 可反查完整来源。",
])

# ---- 模型可见 JSON Schema（index.js 移植；无 legacy cursor） ----

RESOURCE_TYPE_VALUES = list(constants.RESOURCE_TYPES)
CONTENT_TYPE_VALUES = list(constants.CONTENT_TYPES)

_SEARCH_PROPERTIES = {
    "query": {
        "type": "string",
        "description": "短搜索词：实体名、篇章展示名、活动名或原句片段；不要直接提交整句研究问题",
    },
    "resource_types": {
        "type": "array",
        "items": {"type": "string", "enum": RESOURCE_TYPE_VALUES},
        "description": "资料类型；character_bundle 可一次查看角色档案、模组、语音和密录",
    },
    "games": {
        "type": "array",
        "items": {"type": "string", "enum": ["arknights", "endfield"]},
        "description": "可选游戏过滤；省略时在同一次调用中同时检索明日方舟与终末地",
    },
    "content_types": {
        "type": "array",
        "items": {"type": "string", "enum": CONTENT_TYPE_VALUES},
        "description": "统一内容形式，例如 dialogue、cutscene、radio、sns_chat；两款游戏使用相同参数",
    },
    "collection_names": {"type": "array", "items": {"type": "string"},
                         "description": "上级资料集合展示名；明日方舟活动与终末地任务都使用此字段"},
    "character_names": {"type": "array", "items": {"type": "string"},
                        "description": "角色展示名，如“凯尔希”"},
    "story_names": {"type": "array", "items": {"type": "string"},
                    "description": "剧情篇章的展示名，如“晶簇之内”；不要填写内部 story_id 或路径"},
    "activity_names": {"type": "array", "items": {"type": "string"}, "description": "活动展示名"},
    "wiki_sections": {
        "type": "array",
        "items": {"type": "string", "enum": list(constants.WIKI_SECTION_VALUES)},
        "description": "Wiki 标签字段，可与资料类型、角色、活动和 query 组合；角色页常用相关活动/相关角色/剧情高光，"
                       "活动页常用剧情总结/关键人物/角色剧情概括",
    },
    "entity_names": {"type": "array", "items": {"type": "string"},
                     "description": "只返回出现指定实体的行"},
    "speakers": {"type": "array", "items": {"type": "string"},
                 "description": "结构化说话人展示名，只匹配亲口台词；适合查某人亲口说过什么"},
    "match_mode": {"type": "string", "enum": ["literal", "regex"],
                   "description": "默认 literal 连续字面匹配；除非必须，不使用受限 regex"},
    "context_terms": {"type": "array", "items": {"type": "string"},
                      "description": "要求命中附近同时出现的语境词（最多 8 个）"},
    "after": {
        "type": "object",
        "additionalProperties": False,
        "description": "版本绑定的下一页锚点；与原 query 和过滤条件一起原样提交上次返回的 next_after",
        "required": ["data_version", "resource_type", "title", "position"],
        "properties": {
            "data_version": {"type": "string",
                             "description": "上一页所用资料包的完整 SHA-256 版本；版本切换后必须重新搜索"},
            "resource_type": {"type": "string", "enum": RESOURCE_TYPE_VALUES},
            "title": {"type": "string", "description": "上一页扫描到的资料自然标题"},
            "position": {"type": "integer",
                         "description": "该标题在当前资料版本中的顺序位置（0..10000000）"},
        },
    },
}

SEARCH_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": _SEARCH_PROPERTIES,
}

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "title": {"type": "string",
                  "description": "未使用其他定位器时，填写资料完整展示标题；不得与 document_uid 同时提交"},
        "document_uid": {
            "type": "string",
            "description": "仅在搜索结果提示同名歧义时原样复制 doc_ 开头的稳定定位；它替代 title，"
                           "不得与 title 同时提交；可配合 mode=activity/collection 选择所属合集",
        },
        "stage_code": {"type": "string", "description": "明日方舟游戏内关卡代号，如 15-17、GT-3、TW-ST-1"},
        "story_part": {"type": "string", "enum": ["before", "after", "story"],
                       "description": "关卡存在多篇剧情时用于消歧：before=行动前，after=行动后，story=纯剧情/幕间；"
                                      "单篇关卡可省略"},
        "character_name": {"type": "string", "description": "角色展示名；与 record_name 或 material 配合"},
        "record_name": {"type": "string", "description": "明日方舟干员密录名称；多段密录再提供 segment"},
        "segment": {"type": "integer", "description": "干员密录段号，如 1、2"},
        "material": {"type": "string",
                     "enum": ["profile", "module", "voice", "skin", "recruitment", "potential"],
                     "description": "角色资料类别；profile=档案、module=模组、voice=语音、skin=时装"},
        "game": {"type": "string", "enum": ["arknights", "endfield"],
                 "description": "角色资料在双模块同名时用于消歧；其他定位器不要填写"},
        "activity_name": {"type": "string", "description": "明日方舟活动展示名；按活动连续阅读全部剧情"},
        "collection_name": {"type": "string", "description": "终末地任务或剧情集合展示名；跨碎片连续阅读"},
        "content_types": {"type": "array", "items": {"type": "string", "enum": list(constants.END_FIELD_STORY_CONTENT_TYPES)},
                          "description": "终末地集合可选内容形式过滤；续页时原样保留"},
        "line": {"type": "integer", "description": "around 的中心官方行号；与 mode=document 同用时表示续读起始行"},
        "position": {"type": "integer",
                     "description": "活动/任务连续阅读的下一位置；只从 page.continuation 原样复制"},
        "mode": {"type": "string", "enum": ["document", "activity", "collection"],
                 "description": "单篇全文用 document；活动用 activity；终末地任务集合用 collection。"
                                "title 不会自动推断，必须配 line、section 或 mode=document；"
                                "document_uid、关卡、密录和角色资料可自动推断单篇全文"},
        "section": {"type": "string", "enum": list(constants.WIKI_SECTION_VALUES),
                    "description": "读取 Wiki 标签字段"},
        "before": {"type": "integer", "description": "around 前文行数，默认 3，上限 100"},
        "after": {"type": "integer", "description": "around 后文行数，默认 3，上限 100"},
        "data_version": {"type": "string",
                         "description": "续页时原样提交 page.continuation.data_version，防止版本切换后混读"},
        "max_lines": {"type": "integer",
                      "description": "最多返回行数，默认 100，上限 500；只限制输出量，不能代替 line、section 或 mode"},
        "max_chars": {"type": "integer",
                      "description": "最多返回字符数，默认 12000，上限 100000；只限制输出量，"
                                     "不能代替 line、section 或 mode"},
    },
    "additionalProperties": False,
}

TIMELINE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string",
                  "description": "可选的事件正文短语（≤200 字符）。人物应优先放入 entity_names 以自动展开别名"},
        "activity_names": {"type": "array", "items": {"type": "string"},
                           "description": "活动展示名，例如“孤星”（≤20 项）"},
        "entity_names": {"type": "array", "items": {"type": "string"},
                         "description": "角色或实体展示名；工具会用别名图鉴自动裂变后检索（≤20 项）"},
        "year_start": {"type": "integer", "description": "起始年份（含）；可单独使用"},
        "year_end": {"type": "integer", "description": "结束年份（含）；可单独使用"},
        "source_marker": {"type": "string",
                          "description": "反查模式：原样复制时间线结果方括号内的年表出处标记（年表出处:tle_ 开头）"},
        "max_results": {"type": "integer", "description": "最多返回事件数，默认 20，上限 100"},
    },
}

# ---- 通用适配 ----

_LINE_MARKER = re.compile(r"(?<![0-9A-Za-z_])L([0-9]{1,7})(?![0-9])")


def _setting(plugin, key: str, default=None):
    settings = getattr(plugin, "settings", None) or {}
    value = settings.get(key, default)
    return default if value is None else value


def _enabled_games(plugin) -> list[str]:
    games = [g for g in (_setting(plugin, "enabled_games", ["arknights", "endfield"]) or [])
             if g in ("arknights", "endfield")]
    return games or ["arknights", "endfield"]


def _budget(text: str) -> str:
    limit = constants.MAX_TOOL_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return (
        text[:cut]
        + "\n…（输出已达工具预算上限，已截断；请用 max_lines/max_chars、line 或 section 缩小范围）"
    )


def _clean_args(args: dict) -> dict:
    cleaned = {}
    for key, value in (args or {}).items():
        if value is None:
            continue
        if isinstance(value, list):
            value = [item for item in value if item is not None]
        cleaned[key] = value
    return cleaned


async def _ensure_store(plugin) -> str | None:
    store = plugin.store
    if getattr(store, "data_version", None):
        return None
    try:
        await store.ready()
    except Exception as error:  # 未安装/损坏都视为“本地数据包缺失”
        plugin.logger.debug(f"corpus store not ready: {error}")
        return constants.LOCAL_CORPUS_MISSING_MESSAGE
    if getattr(store, "data_version", None):
        return None
    return constants.LOCAL_CORPUS_MISSING_MESSAGE


async def _conversation_id(plugin, event) -> str:
    try:
        manager = plugin.context.conversation_manager
        conversation_id = await manager.get_curr_conversation_id(event.unified_msg_origin)
        return str(conversation_id or "")
    except Exception:
        return ""


async def _history_lines(plugin, event) -> set[int] | None:
    """扫描当前会话历史中仍可见的 `L{n}` 行号；不可得时返回 None（保守全量）。"""
    try:
        manager = plugin.context.conversation_manager
        umo = event.unified_msg_origin
        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            return None
        conversation = await manager.get_conversation(umo, conversation_id)
        history = getattr(conversation, "history", None) or "[]"
        if isinstance(history, str):
            history = json.loads(history)
        if not isinstance(history, list):
            return None
        texts: list[str] = []
        for message in history:
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") not in ("assistant", "tool"):
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        found: set[int] = set()
        for text in texts:
            found.update(int(match) for match in _LINE_MARKER.findall(text))
        return found
    except Exception:
        return None


# ---- read：证据去重与续页定位器修正 ----


def _drop_covered_lines(value: dict, new_ranges: list) -> None:
    content = value.get("content") or {}
    if content.get("format") != "lines":
        return
    ranges = [
        (int(item["line_start"]), int(item["line_end"]))
        for item in new_ranges
        if isinstance(item, dict) and isinstance(item.get("line_start"), int)
        and isinstance(item.get("line_end"), int)
    ]
    if not ranges:
        content["lines"] = []
        return
    content["lines"] = [
        line for line in content.get("lines") or []
        if any(start <= int(line.get("line_number") or 0) <= end for start, end in ranges)
    ]


def _fix_continuation(store, response: dict, projected: dict) -> None:
    """index.js projectReadToolValue：同名歧义时续页改用 document_uid/标题。"""
    page = projected.get("page") or {}
    continuation = page.get("continuation")
    if not isinstance(continuation, dict) or continuation.get("mode") != "document":
        return
    document_id = str((response.get("document") or {}).get("document_id") or "")
    line = continuation.get("line")
    data_version = continuation.get("data_version")
    has_natural_locator = bool(
        continuation.get("stage_code") or continuation.get("record_name") or continuation.get("material")
    )
    if not has_natural_locator:
        if document_id and store.requires_document_uid(document_id):
            projected["page"]["continuation"] = {
                "document_uid": document_uid(document_id),
                "mode": "document",
                "line": line,
                "data_version": data_version,
            }
        return
    unique_stage = bool(
        continuation.get("stage_code") and continuation.get("story_part")
        and store.has_unique_story_stage(document_id)
    )
    unique_record = bool(continuation.get("record_name") and store.has_unique_operator_record(document_id))
    unique_material = bool(continuation.get("material") and store.has_unique_character_material(document_id))
    if not continuation.get("document_uid") and not (unique_stage or unique_record or unique_material):
        projected["page"]["continuation"] = {
            "title": str((projected.get("primary") or {}).get("title") or ""),
            "mode": "document",
            "line": line,
            "data_version": data_version,
        }


# ---- 各工具执行体 ----


def _search_runtime(plugin) -> dict:
    """按插件配置生成搜索运行时参数（单核 VPS 可调大 search_timeout_seconds）。"""
    runtime: dict[str, Any] = {"signal": None}
    try:
        seconds = float((getattr(plugin, "settings", None) or {}).get("search_timeout_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        runtime["timeout_ms"] = seconds * 1000.0
    return runtime


async def _handle_search(plugin, event, **raw_args) -> str:
    missing = await _ensure_store(plugin)
    if missing:
        return missing
    args = _clean_args(raw_args)
    enabled = _enabled_games(plugin)
    if args.get("games"):
        args["games"] = [game for game in args["games"] if game in enabled]
        if not args["games"]:
            return "[prts_search:error] code=INVALID_REQUEST\n指定的资料库未启用（" + "、".join(enabled) + "）"
    try:
        await _maybe_note_search(plugin, event, args)
        value = await execute_search(plugin.store, args, _search_runtime(plugin))
    except ContractError as error:
        return f"[prts_search:error] code={error.code} retryable={error.retryable}\n{error.message}"
    except Exception as error:
        plugin.logger.error(f"prts_search failed: {error}")
        return "[prts_search:error] code=INTERNAL_ERROR\n检索执行失败，请稍后重试或缩小范围"
    return _budget(render_search(args, value))


async def _maybe_note_search(plugin, event, args: dict) -> None:
    evidence = getattr(plugin, "evidence", None)
    if evidence is None:
        return
    signature = json.dumps(
        {key: args[key] for key in sorted(args) if key != "after"}, ensure_ascii=False
    ) + "|" + json.dumps(args.get("after") or {}, ensure_ascii=False, sort_keys=True)
    key = evidence.session_key(event.unified_msg_origin, await _conversation_id(plugin, event))
    evidence.begin_session(key, getattr(plugin.store, "data_version", None))
    evidence.note_search(key, signature)


async def _handle_read(plugin, event, **raw_args) -> str:
    missing = await _ensure_store(plugin)
    if missing:
        return missing
    store = plugin.store
    args = _clean_args(raw_args)
    try:
        contract = await model_read_to_contract(store, args, _enabled_games(plugin))
    except ContractError as error:
        return f"[prts_read:error] code={error.code} retryable={error.retryable}\n{error.message}"
    except Exception as error:
        plugin.logger.error(f"prts_read locator failed: {error}")
        return "[prts_read:error] code=INTERNAL_ERROR\n读取定位失败，请改用 title 或搜索结果给出的定位器"
    contract["intent_id"] = f"intent-{secrets.token_hex(8)}"
    try:
        value = await execute_read(store, contract, {"signal": None})
    except Exception as error:
        plugin.logger.error(f"prts_read failed: {error}")
        return "[prts_read:error] code=INTERNAL_ERROR\n读取执行失败，请稍后重试"
    if value.get("status") != "ok":
        return render_read(args, value)
    try:
        await _apply_read_evidence(plugin, event, value)
    except Exception as error:
        plugin.logger.debug(f"evidence skipped: {error}")
    projected = project_read_public(value)
    try:
        _fix_continuation(store, value, projected)
    except Exception as error:
        plugin.logger.debug(f"continuation fix skipped: {error}")
    return _budget(render_read(args, projected))


async def _apply_read_evidence(plugin, event, value: dict) -> None:
    evidence = getattr(plugin, "evidence", None)
    if evidence is None:
        return
    selection = value.get("selection") or {}
    document_id = str((value.get("document") or {}).get("document_id") or "")
    line_start = selection.get("line_start")
    line_end = selection.get("line_end")
    if not document_id or not isinstance(line_start, int) or not isinstance(line_end, int):
        return
    key = evidence.session_key(event.unified_msg_origin, await _conversation_id(plugin, event))
    evidence.begin_session(key, getattr(plugin.store, "data_version", None))
    history_lines = await _history_lines(plugin, event)
    outcome = evidence.apply_read(
        key,
        data_version=getattr(plugin.store, "data_version", None),
        document_id=document_id,
        line_start=line_start,
        line_end=line_end,
        history_lines=history_lines,
    ) or {}
    reused = outcome.get("reused_ranges") or []
    if outcome.get("drop"):
        title = str((value.get("document") or {}).get("display_title") or "")
        raise _ReadReused(
            f"[prts_read:reused] 《{title}》第 {line_start}-{line_end} 行已在上文可见工具结果中，本次未重复展示。"
            + (f"{outcome.get('guidance')}" if outcome.get("guidance") else "")
        )
    if reused:
        _drop_covered_lines(value, outcome.get("new_ranges") or [])
        value["coverage"] = {"reused_ranges": reused}
        if outcome.get("guidance"):
            value["guidance"] = outcome["guidance"]


class _ReadReused(Exception):
    """全量复用上文时短路返回（tools 内部信号）。"""


async def _handle_timeline(plugin, event, **raw_args) -> str:
    missing = await _ensure_store(plugin)
    if missing:
        return missing
    args = _clean_args(raw_args)
    try:
        value = await execute_timeline_search(plugin.store, args, {"signal": None})
    except ContractError as error:
        return f"[prts_timeline:error] code={error.code} retryable={error.retryable}\n{error.message}"
    except Exception as error:
        plugin.logger.error(f"prts_timeline failed: {error}")
        return "[prts_timeline:error] code=INTERNAL_ERROR\n年表检索失败，请稍后重试"
    return _budget(render_timeline(args, value))


async def _handle_i18n(plugin, event, **raw_args) -> str:
    missing = await _ensure_store(plugin)
    if missing:
        return missing
    args = _clean_args(raw_args)
    try:
        value = await execute_i18n(plugin.store, args, {"signal": None})
    except ContractError as error:
        return f"[prts_i18n:error] code={error.code} retryable={error.retryable}\n{error.message}"
    except Exception as error:
        plugin.logger.error(f"prts_i18n failed: {error}")
        return "[prts_i18n:error] code=INTERNAL_ERROR\n多语言查询失败，请稍后重试"
    return _budget(render_i18n(args, value))


# ---- 工具工厂 ----

_IMPLS: dict[str, Callable[..., Awaitable[str]]] = {
    constants.TOOL_SEARCH: _handle_search,
    constants.TOOL_READ: _handle_read,
    constants.TOOL_TIMELINE: _handle_timeline,
    constants.TOOL_I18N: _handle_i18n,
}

_ENABLE_FLAGS = {
    constants.TOOL_SEARCH: "enable_search_tool",
    constants.TOOL_READ: "enable_read_tool",
    constants.TOOL_TIMELINE: "enable_timeline_tool",
    constants.TOOL_I18N: "enable_i18n_tool",
}


def _make_handler(plugin, impl):
    async def handler(event, **args) -> str:
        try:
            return await impl(plugin, event, **args)
        except _ReadReused as reused:
            return str(reused)
        except Exception as error:
            tool_name = getattr(impl, "__name__", "prts_tool")
            plugin.logger.error(f"{tool_name} unexpected failure: {error}", exc_info=True)
            return "[prts_tool:error] code=INTERNAL_ERROR\n工具执行出现未预期错误，请稍后重试"

    return handler


def make_tools(plugin, module_path: str, tool_cls: type = FunctionTool) -> list[FunctionTool]:
    """按配置构造启用的 FunctionTool；module_path 为插件 main 模块路径。

    tool_cls 由 main.py 传入本插件内定义的 FunctionTool 子类：AstrBot 通过
    tool.__module__ 解析归属模块，子类定义在插件主模块才能被正确回收。
    """
    descriptions = {
        constants.TOOL_SEARCH: SEARCH_DESCRIPTION,
        constants.TOOL_READ: READ_DESCRIPTION,
        constants.TOOL_TIMELINE: TIMELINE_DESCRIPTION,
        constants.TOOL_I18N: " ".join(I18N_DESCRIPTION),
    }
    parameters = {
        constants.TOOL_SEARCH: SEARCH_PARAMETERS,
        constants.TOOL_READ: READ_PARAMETERS,
        constants.TOOL_TIMELINE: TIMELINE_PARAMETERS,
        constants.TOOL_I18N: I18N_PARAMETERS,
    }
    tools: list[FunctionTool] = []
    for name, impl in _IMPLS.items():
        if not _setting(plugin, _ENABLE_FLAGS[name], True):
            continue
        tools.append(
            tool_cls(
                name=name,
                description=descriptions[name],
                parameters=parameters[name],
                handler=_make_handler(plugin, impl),
                handler_module_path=module_path,
                active=True,
            )
        )
    return tools