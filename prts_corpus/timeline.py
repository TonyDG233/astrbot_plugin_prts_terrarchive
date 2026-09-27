"""PRTS Wiki《泰拉年表》本地投影的时间线检索与出处反查。

数据来自资料包 references/activity_timelines.jsonl（每行一个活动：
{activity_id, activity_name, events:[{event_id, time, event, sources}], source}）。
entity_names 先经别名图鉴（entities 包 + references/char_alias.txt）裂变再匹配；
source_marker（年表出处:tle_xxx）可反查单条事件的完整来源。
"""

from __future__ import annotations

import json
import re
import secrets
import time
import unicodedata
from typing import Any

from .constants import CONTRACT_VERSION
from .errors import ContractError

TIMELINE_CONTRACT_VERSION = CONTRACT_VERSION
MARKER_PATTERN = re.compile(r"^年表出处:(tle_[0-9a-f]{24})$")

TIMELINE_DESCRIPTION = [
    "按活动名、年份及自动展开的实体别名检索活动时间线（PRTS Wiki《泰拉年表》本地投影）。",
    "人物放 entity_names 以自动裂变别名；结果只给时间、事件正文和“年表出处”标记，把标记原样传回 source_marker 可反查完整来源。",
]

TIMELINE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "description": "可选的事件正文短语（≤200 字符）。人物应优先放入 entity_names 以自动展开别名"},
        "activity_names": {"type": "array", "items": {"type": "string"}, "description": "活动展示名，例如“孤星”（≤20 项）"},
        "entity_names": {"type": "array", "items": {"type": "string"}, "description": "角色或实体展示名；工具会用别名图鉴自动裂变后检索（≤20 项）"},
        "year_start": {"type": "integer", "description": "起始年份（含）；可单独使用"},
        "year_end": {"type": "integer", "description": "结束年份（含）；可单独使用"},
        "source_marker": {"type": "string", "description": "反查模式：原样复制时间线结果方括号内的年表出处标记（年表出处:tle_ 开头）"},
        "max_results": {"type": "integer", "description": "最多返回事件数，默认 20，上限 100"},
    },
}

# 插件内置的最小跨游戏实体路由表回退（与 resources/entity-routing.json 一致）
FALLBACK_RETRAVELERS = [
    {"endfield_name": "莱万汀", "terra_memory_prototype": "史尔特尔", "relation_status": "confirmed"},
    {"endfield_name": "洁尔佩塔", "terra_memory_prototype": "安洁莉娜", "relation_status": "confirmed"},
    {"endfield_name": "艾尔黛拉", "terra_memory_prototype": "艾雅法拉", "relation_status": "confirmed"},
    {"endfield_name": "骏卫", "terra_memory_prototype": "赫拉格", "relation_status": "confirmed"},
    {"endfield_name": "提弗洛斯", "terra_memory_prototype": "提丰", "relation_status": "confirmed"},
    {"endfield_name": "昼雪", "terra_memory_prototype": "极光", "relation_status": "confirmed"},
    {"endfield_name": "煌", "terra_memory_prototype": None, "relation_status": "retraveler_only"},
]


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


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", " ", normalized).strip()


def chinese_integer(value: int) -> str:
    digits = "零一二三四五六七八九"
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 99:
        return str(value)
    if value < 10:
        return digits[value]
    tens = value // 10
    ones = value % 10
    tens_str = "" if tens == 1 else digits[tens]
    ones_str = digits[ones] if ones else ""
    return f"{tens_str}十{ones_str}"


def activity_aliases(value: dict | None = None) -> list[str]:
    """活动展示名之外，再接受主线章节号的常见自然写法。"""
    val = value or {}
    act_name = normalize_text(val.get("activity_name"))
    aliases: list[str] = [act_name] if act_name else []
    seen = set(aliases)

    source = "\n".join(str(val.get(k) or "") for k in ("activity_id", "collection_id"))
    match = re.search(r"(?:^|event:|\b)main[_:](\d+)(?:\b|$)", source)
    if match:
        chapter = int(match.group(1))
        candidates = [
            f"第{chapter}章",
            f"第{chinese_integer(chapter)}章",
            f"{chapter}章",
            f"主线{chapter}章",
            f"主线第{chapter}章",
        ]
        for c in candidates:
            if c not in seen:
                seen.add(c)
                aliases.append(c)
    return aliases


def activity_matches(value: dict, requested: str, *, exact: bool = False) -> bool:
    needle = normalize_text(requested)
    if not needle:
        return False
    aliases = activity_aliases(value)
    if exact:
        return any(alias == needle for alias in aliases)
    return any(needle in alias or alias in needle for alias in aliases)


def useful_entity_aliases(group: dict) -> list[str]:
    aliases = list(dict.fromkeys(normalize_text(a) for a in group.get("aliases", []) if normalize_text(a)))
    longer = [a for a in aliases if len(a) > 1]
    return longer if longer else aliases


def display_event_time(time: dict | None = None) -> str:
    t = time or {}
    label = str(t.get("text") or t.get("section") or "时间不详").strip()
    if re.search(r"年|世纪|纪元", label):
        return label
    start = t.get("year_start")
    start = start if isinstance(start, int) and not isinstance(start, bool) else None
    end = t.get("year_end")
    end = end if isinstance(end, int) and not isinstance(end, bool) else start
    if start is None or start < 0:
        return label
    year = f"{start}-{end} 年" if end is not None and end != start else f"{start} 年"
    return year if label == "时间不详" else f"{year} {label}"


def normalized_time_bounds(time: dict | None = None) -> dict[str, int | None]:
    t = time or {}
    label = normalize_text(t.get("text") or t.get("section"))
    century = re.search(r"(\d+)\s*世纪(?:\s*(\d+)\s*年代)?", label)
    if century:
        base = (int(century.group(1)) - 1) * 100
        start = base + (int(century.group(2)) if century.group(2) else 1)
        end = start + 9 if century.group(2) else base + 100
        return {"start": start, "end": end}
    start = t.get("year_start")
    start = start if isinstance(start, int) and not isinstance(start, bool) else None
    if start is None:
        explicit = re.search(r"(?:^|\D)(\d{1,4})\s*年", label)
        if explicit:
            start = int(explicit.group(1))
    end = t.get("year_end")
    end = end if isinstance(end, int) and not isinstance(end, bool) else start
    return {"start": start, "end": end}


def numeric_time_order(time: dict | None = None, sequence: int = 0) -> list[float | int]:
    t = time or {}
    label = normalize_text(t.get("text") or t.get("section"))
    bounds = normalized_time_bounds(t)
    if bounds["start"] is None:
        return [float("inf"), 13, 32, 24 * 60, sequence]
    month_match = re.search(r"(\d{1,2})\s*月", label)
    if "春" in label:
        season_month = 3
    elif "夏" in label:
        season_month = 6
    elif "秋" in label:
        season_month = 9
    elif "冬" in label:
        season_month = 12
    else:
        season_month = 0
    month = int(month_match.group(1)) if month_match else season_month
    day_match = re.search(r"(\d{1,2})\s*日", label)
    day = int(day_match.group(1)) if day_match else 0
    clock = re.search(r"(\d{1,2})\s*[：:]\s*(\d{1,2})", label)
    if clock:
        minutes = int(clock.group(1)) * 60 + int(clock.group(2))
    else:
        minutes = 0
        if "深夜" in label:
            minutes = 23 * 60
        elif "晚上" in label:
            minutes = 19 * 60
        elif "下午" in label:
            minutes = 14 * 60
        elif "中午" in label:
            minutes = 12 * 60
        elif "上午" in label:
            minutes = 9 * 60
        elif "早上" in label:
            minutes = 7 * 60
        elif "清晨" in label:
            minutes = 5 * 60
        elif "凌晨" in label:
            minutes = 1 * 60
    return [bounds["start"], month, day, minutes, sequence]


def timeline_event(row: dict, event: dict) -> dict:
    t = event.get("time") or {}
    res: dict[str, Any] = {
        "activity_name": row.get("activity_name", ""),
        "time": display_event_time(t),
    }
    ys = t.get("year_start")
    if isinstance(ys, int) and not isinstance(ys, bool):
        res["year_start"] = ys
    ye = t.get("year_end")
    if isinstance(ye, int) and not isinstance(ye, bool):
        res["year_end"] = ye
    res["event"] = event.get("event", "")
    res["source_marker"] = f"年表出处:{event.get('event_id', '')}"
    return res


def merge_timeline_matches(candidates: list[dict]) -> list[dict]:
    merged: list[dict] = []
    by_key: dict[str, dict] = {}
    sorted_candidates = sorted(candidates, key=lambda c: tuple(c["order"]))
    for candidate in sorted_candidates:
        projected = timeline_event(candidate["row"], candidate["event"])
        fallback_key = f"{normalize_text(projected['time'])}\0{normalize_text(projected['event'])}"
        event_id = str(candidate["event"].get("event_id") or "")
        key = event_id if event_id else fallback_key
        existing = by_key.get(key)
        if existing:
            act_name = projected.get("activity_name")
            if act_name and act_name not in existing["activity_names"]:
                existing["activity_names"].append(act_name)
            continue
        projected["activity_names"] = [projected["activity_name"]] if projected.get("activity_name") else []
        by_key[key] = projected
        merged.append(projected)
    return merged


async def load_entity_relation_catalog(store) -> dict:
    data_version = getattr(store, "data_version", getattr(store, "dataVersion", None)) or "no-store"
    cached = getattr(store, "_endfield_relation_catalog", None)
    if isinstance(cached, dict) and cached.get("data_version") == data_version:
        return cached.get("value", {})
    installed = {}
    try:
        getter = getattr(store, "get_document_by_path", getattr(store, "getDocumentByPath", None))
        if getter:
            loaded = await getter("config/retravelers.json")
            record = loaded.get("record") if isinstance(loaded, dict) else loaded
            lines = record.get("lines") if isinstance(record, dict) else []
            text = "\n".join(str(line.get("text", "")) for line in lines)
            if text:
                installed = json.loads(text)
    except Exception:
        installed = {}

    def merge_rows(field: str, identity_fn):
        rows = {}
        for row in list(FALLBACK_RETRAVELERS if field == "retravelers" else []) + list(installed.get(field) or []):
            k = identity_fn(row)
            if k:
                rows[k] = row
        return list(rows.values())

    value = {
        "retravelers": merge_rows("retravelers", lambda r: str(r.get("endfield_name") or "")),
        "visual_parallels_without_lore_relation": merge_rows(
            "visual_parallels_without_lore_relation",
            lambda r: f"{r.get('endfield_name') or ''}\0{r.get('arknights_name') or ''}",
        ),
    }
    if store is not None:
        try:
            store._endfield_relation_catalog = {"data_version": data_version, "value": value}
        except Exception:
            pass
    return value


def relation_endfield_names(catalog: dict) -> set[str]:
    return {
        str(row.get("endfield_name") or "").strip()
        for row in catalog.get("retravelers", [])
        if str(row.get("endfield_name") or "").strip()
    }


async def build_alias_groups(store) -> list[dict]:
    """构建全量别名组（entities 包 + references/char_alias.txt）。"""
    groups: dict[str, dict] = {}
    catalog = await load_entity_relation_catalog(store)
    cross_game_names = relation_endfield_names(catalog)

    def remember(canonical_val, alias_vals=None, game_val=""):
        canonical = str(canonical_val or "").strip()
        if not canonical:
            return
        if canonical not in groups:
            groups[canonical] = {
                "canonical": canonical,
                "aliases": {canonical},
                "games": set(),
            }
        group = groups[canonical]
        for val in alias_vals or []:
            alias = str(val or "").strip()
            if alias and (alias == canonical or alias not in cross_game_names):
                group["aliases"].add(alias)
        game = str(game_val or "").strip().lower()
        if game in ("arknights", "endfield"):
            group["games"].add(game)

    # 1. entities 包每条实体记录
    iterator = getattr(store, "iterate_documents", getattr(store, "iterateDocuments", None))
    if iterator:
        try:
            async for record in iterator(predicate=lambda doc, *_: doc.get("document_type") == "entity"):
                entity = record.get("entity") or {}
                document = record.get("document") or {}
                identity = f"{document.get('document_id', '')} {document.get('source_ref_prefix', '')}".lower()
                game = document.get("game") or ("endfield" if "endfield:" in identity else "arknights")
                canonical_name = entity.get("canonical_name") or document.get("display_title")
                aliases = entity.get("aliases") or record.get("aliases") or []
                remember(canonical_name, aliases, game)
        except Exception:
            pass

    # 2. references/char_alias.txt 的分号分隔行
    path_getter = getattr(store, "get_document_by_path", getattr(store, "getDocumentByPath", None))
    if path_getter:
        alias_reference = await path_getter("char_alias.txt")
        lines = (
            alias_reference.get("record", {}).get("lines", [])
            if isinstance(alias_reference, dict)
            else []
        )
        for line in lines:
            raw_text = str(line.get("text") or "")
            aliases = list(dict.fromkeys(item.strip() for item in raw_text.split(";") if item.strip()))
            if not aliases:
                continue
            first = aliases[0]
            existing = next(
                (g for g in groups.values() if g["canonical"] == first or first in g["aliases"]),
                None,
            )
            target_canonical = existing["canonical"] if existing else first
            remember(target_canonical, aliases)

    return [
        {
            "canonical": g["canonical"],
            "aliases": list(g["aliases"]),
            "games": list(g["games"]),
        }
        for g in groups.values()
    ]


async def aliases_for(store, entity_names: list[str]) -> list[dict]:
    """把请求中的实体名展开为别名组；未知名字原样返回单别名组。"""
    if not entity_names:
        return []
    cached_groups = getattr(store, "_alias_groups", getattr(store, "_aliasGroups", None))
    if cached_groups is None:
        cached_groups = await build_alias_groups(store)
        try:
            store._alias_groups = cached_groups
            store._aliasGroups = cached_groups
        except Exception:
            pass
    result = []
    for name in entity_names:
        group = next((item for item in cached_groups if name in item.get("aliases", [])), None)
        if group:
            result.append({
                "canonical": group["canonical"],
                "aliases": list(group["aliases"]),
                "games": list(group["games"]),
            })
        else:
            result.append({
                "canonical": name,
                "aliases": [name],
                "games": [],
            })
    return result


async def timeline_rows(store) -> list[dict]:
    """读取并解析 activity_timelines.jsonl（每行一个活动，含 events 数组）。"""
    cached = getattr(store, "_timeline_rows", getattr(store, "_timelineRows", None))
    if cached is not None:
        return cached
    getter = getattr(store, "get_document_by_path", getattr(store, "getDocumentByPath", None))
    found = await getter("activity_timelines.jsonl") if getter else None
    lines = (
        found.get("record", {}).get("lines", [])
        if isinstance(found, dict)
        else []
    )
    rows = []
    for line in lines:
        try:
            row = json.loads(str(line.get("text") or ""))
            if isinstance(row, dict) and isinstance(row.get("events"), list):
                rows.append(row)
        except Exception:
            continue
    try:
        store._timeline_rows = rows
        store._timelineRows = rows
    except Exception:
        pass
    return rows


def normalized_timeline_request(raw: dict | None = None) -> dict:
    raw_dict = raw if isinstance(raw, dict) else {}
    query = normalize_text(raw_dict.get("query"))
    if len(query) > 200:
        raise ContractError("INVALID_REQUEST", "query 最长 200 字符")

    def name_filter(val: Any, field_name: str) -> list[str]:
        if val is None:
            return []
        if not isinstance(val, list):
            raise ContractError("INVALID_REQUEST", f"{field_name} 必须是字符串数组")
        res = []
        for item in val:
            n = normalize_text(item)
            if n:
                res.append(n)
        return res

    activities = name_filter(raw_dict.get("activity_names"), "activity_names")
    if len(activities) > 20:
        raise ContractError("INVALID_REQUEST", "activity_names 最多 20 项")

    entities = name_filter(raw_dict.get("entity_names"), "entity_names")
    if len(entities) > 20:
        raise ContractError("INVALID_REQUEST", "entity_names 最多 20 项")

    marker = str(raw_dict.get("source_marker") or "").strip()
    if marker and not MARKER_PATTERN.match(marker):
        raise ContractError("INVALID_REQUEST", "时间线出处标记格式无效（应为 年表出处:tle_<24位十六进制>）")

    def parse_year(val: Any, name: str) -> int | None:
        if val is None:
            return None
        if isinstance(val, bool):
            raise ContractError("INVALID_REQUEST", f"{name} 必须是整数")
        if isinstance(val, int):
            return val
        if isinstance(val, float) and val.is_integer():
            return int(val)
        raise ContractError("INVALID_REQUEST", f"{name} 必须是整数")

    year_start = parse_year(raw_dict.get("year_start"), "year_start")
    year_end = parse_year(raw_dict.get("year_end"), "year_end")

    if not query and not activities and not entities and year_start is None and year_end is None and not marker:
        raise ContractError("INVALID_REQUEST", "请提供 query / activity_names / entity_names / 年份范围 / source_marker 之一")

    max_res = raw_dict.get("max_results")
    if max_res is None:
        limit = 20
    else:
        if isinstance(max_res, bool) or not isinstance(max_res, (int, float)) or (isinstance(max_res, float) and not max_res.is_integer()):
            raise ContractError("INVALID_REQUEST", "max_results 必须在 1..100")
        limit = int(max_res)
        if limit < 1 or limit > 100:
            raise ContractError("INVALID_REQUEST", "max_results 必须在 1..100")

    return {
        "query": query,
        "activities": activities,
        "entities": entities,
        "marker": marker,
        "year_start": year_start,
        "year_end": year_end,
        "limit": limit,
    }


async def execute_timeline_search(store, args: dict, runtime: dict | None = None) -> dict:
    """执行 timeline_search（活动时间线检索与反查）。"""
    started = int(time.time() * 1000)
    request_id = f"req-{secrets.token_hex(8)}"
    try:
        await store.ready()

        # expected_data_version 版本绑定检查
        expected_ver = args.get("expected_data_version") or args.get("data_version")
        store_ver = getattr(store, "data_version", getattr(store, "dataVersion", None))
        if expected_ver and store_ver and expected_ver != store_ver:
            raise ContractError("PACKAGE_VERSION_MISMATCH", "资料版本在读取期间发生变化，请重试", retryable=True)

        request = normalized_timeline_request(args)
        rows = await timeline_rows(store)
        if not rows:
            raise ContractError("TIMELINE_NOT_INSTALLED", "活动时间线资料尚未安装（资料包缺少 activity_timelines.jsonl）", retryable=True)

        if _is_aborted(runtime):
            raise ContractError("CANCELLED", "时间线检索已取消")

        # 反查模式：source_marker → 单条事件完整来源
        if request["marker"]:
            match = MARKER_PATTERN.match(request["marker"])
            event_id = match.group(1) if match else ""
            for row in rows:
                for event in row.get("events", []):
                    if event.get("event_id") == event_id:
                        prov: dict[str, Any] = {
                            "source_title": "PRTS Wiki《泰拉年表》",
                            "source": row.get("source"),
                            "sources": event.get("sources") or [],
                        }
                        if event.get("source_location") is not None:
                            prov["source_location"] = event["source_location"]
                        return {
                            "contract_version": TIMELINE_CONTRACT_VERSION,
                            "status": "ok",
                            "request_id": request_id,
                            "data_version": store_ver,
                            "mode": "source",
                            "event": timeline_event(row, event),
                            "provenance": prov,
                        }
            raise ContractError("TIMELINE_SOURCE_NOT_FOUND", "没有找到该时间线出处标记")

        # 检索模式：活动 / 实体别名 / 年份 / 正文短语 四维交集
        alias_groups = await aliases_for(store, request["entities"])
        entity_aliases = list(dict.fromkeys(
            normalize_text(alias).lower()
            for group in alias_groups
            for alias in useful_entity_aliases(group)
            if normalize_text(alias)
        ))

        inferred_activity = None
        if not request["activities"] and request["query"]:
            inferred_activity = next(
                (row for row in rows if activity_matches(row, request["query"], exact=True)),
                None,
            )

        requested_activities = [inferred_activity["activity_name"]] if inferred_activity else request["activities"]
        query = "" if inferred_activity else request["query"].lower()
        candidates = []
        sequence = 0

        for row in rows:
            if requested_activities and not any(activity_matches(row, name) for name in requested_activities):
                continue
            for event in row.get("events", []):
                bounds = normalized_time_bounds(event.get("time"))
                y_start = bounds["start"]
                y_end = bounds["end"]
                if request["year_start"] is not None and (y_end is None or y_end < request["year_start"]):
                    continue
                if request["year_end"] is not None and (y_start is None or y_start > request["year_end"]):
                    continue

                sources_text = [
                    s.get("text")
                    for s in (event.get("sources") or [])
                    if isinstance(s, dict) and s.get("text")
                ]
                haystack_parts = [
                    event.get("event"),
                    (event.get("time") or {}).get("text"),
                    *sources_text,
                ]
                haystack = normalize_text("\n".join(str(p) for p in haystack_parts if p)).lower()

                if query and query not in haystack:
                    continue
                if entity_aliases and not any(alias in haystack for alias in entity_aliases):
                    continue

                candidates.append({
                    "row": row,
                    "event": event,
                    "order": numeric_time_order(event.get("time"), sequence),
                })
                sequence += 1

        matches = merge_timeline_matches(candidates)
        normalized_filters: dict[str, Any] = {
            "query": "" if inferred_activity else request["query"],
            "activity_names": requested_activities,
            "entity_alias_groups": alias_groups,
            "year_start": request["year_start"],
            "year_end": request["year_end"],
        }
        if inferred_activity:
            normalized_filters["inferred_activity_from_query"] = request["query"]

        returned_count = min(len(matches), request["limit"])
        elapsed = int(time.time() * 1000) - started
        return {
            "contract_version": TIMELINE_CONTRACT_VERSION,
            "status": "ok",
            "request_id": request_id,
            "data_version": store_ver,
            "mode": "search",
            "normalized_filters": normalized_filters,
            "events": matches[:request["limit"]],
            "page": {
                "returned": returned_count,
                "total": len(matches),
                "has_more": len(matches) > request["limit"],
            },
            "stats": {
                "elapsed_ms": elapsed,
                "scanned_activities": len(rows),
                "matched_events": len(matches),
                "matched_occurrences": len(candidates),
            },
            "guidance": (
                "时间线是可采纳的整理性证据；若与原文冲突则以原文为准。方括号内的出处标记可原样传给 source_marker 反查完整来源。"
                if matches
                else "没有命中；可放宽年份、换用实体展示名，或省略实体只按活动浏览。"
            ),
        }
    except Exception as error:
        code = getattr(error, "code", "INTERNAL_ERROR")
        message = getattr(error, "message", str(error))
        retryable = getattr(error, "retryable", False)
        return {
            "contract_version": TIMELINE_CONTRACT_VERSION,
            "status": "error",
            "request_id": request_id,
            "data_version": getattr(store, "data_version", getattr(store, "dataVersion", None)),
            "error": {"code": code, "message": message, "retryable": retryable},
        }


def render_timeline(_args: dict, value: dict) -> str:
    """模型可见文本渲染。"""
    if value.get("status") != "ok":
        error = value.get("error") or {}
        return f"[timeline_search:error] {error.get('code')}: {error.get('message')}"
    data_ver = str(value.get("data_version") or "")[:12]
    lines = [f"[timeline_search:{value.get('mode')}] data_version={data_ver}…"]
    if value.get("mode") == "source":
        event = value.get("event") or {}
        lines.append(f"{event.get('time')}：{event.get('event')}")
        lines.append(f"activity: {event.get('activity_name')}")
        provenance = value.get("provenance") or {}
        src_title = provenance.get("source_title", "")
        src = provenance.get("source")
        src_str = f" ({src})" if src else ""
        lines.append(f"source: {src_title}{src_str}")
        for s in provenance.get("sources") or []:
            parts = [s.get("text"), s.get("story_name"), s.get("activity_name")]
            lines.append(f"  - {' | '.join(p for p in parts if p)}")
        return "\n".join(lines)

    for index, event in enumerate(value.get("events") or []):
        act_names = event.get("activity_names") or ([event.get("activity_name")] if event.get("activity_name") else [])
        act_str = "、".join(a for a in act_names if a)
        lines.append(f"{index + 1}. [{act_str}] {event.get('time')}：{event.get('event')} [{event.get('source_marker')}]")

    page = value.get("page") or {}
    has_more_str = "true" if page.get("has_more") else "false"
    lines.append(f"page: returned={page.get('returned')} total={page.get('total')} has_more={has_more_str}")
    if value.get("guidance"):
        lines.append(value["guidance"])
    return "\n".join(lines)


# 驼峰别名（兼容调用）
activityAliases = activity_aliases
activityMatches = activity_matches
buildAliasGroups = build_alias_groups
aliasesFor = aliases_for
timelineRows = timeline_rows
executeTimelineSearch = execute_timeline_search
renderTimeline = render_timeline
