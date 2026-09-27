"""跨游戏实体路由与再旅者（Retraveler）关系判定。

移植自 upstream `src/entity-routing.js`。
用于资料包跨游戏消歧（终末地再旅者与泰拉记忆原型对应关系，属于关系，非别名）。
"""

from __future__ import annotations

import json
from typing import Any

from .normalize import normalize_search_text

# 插件内置的最小跨游戏实体路由表。用于资料包过旧或未启用时消歧；关系不是别名。
BUNDLED_RELATIONS: dict[str, Any] = {
    "schema_version": 1,
    "description": "插件内置的最小跨游戏实体路由表。用于资料包过旧或未启用时消歧；关系不是别名。",
    "retravelers": [
        {"endfield_name": "莱万汀", "terra_memory_prototype": "史尔特尔", "relation_status": "confirmed"},
        {"endfield_name": "洁尔佩塔", "terra_memory_prototype": "安洁莉娜", "relation_status": "confirmed"},
        {"endfield_name": "艾尔黛拉", "terra_memory_prototype": "艾雅法拉", "relation_status": "confirmed"},
        {"endfield_name": "骏卫", "terra_memory_prototype": "赫拉格", "relation_status": "confirmed"},
        {"endfield_name": "提弗洛斯", "terra_memory_prototype": "提丰", "relation_status": "confirmed"},
        {"endfield_name": "昼雪", "terra_memory_prototype": "极光", "relation_status": "confirmed"},
        {"endfield_name": "煌", "terra_memory_prototype": None, "relation_status": "retraveler_only"},
    ],
    "visual_parallels_without_lore_relation": [
        {
            "endfield_name": "安塔尔",
            "arknights_name": "12F",
            "note": "两者外观高度相似，但现有游戏剧情没有建立关系。",
            "must_not_infer_retraveler_or_memory_prototype": True,
        }
    ],
}


def merge_relation_catalog(fallback: dict[str, Any] | None, installed: dict[str, Any] | None) -> dict[str, Any]:
    fallback = fallback or {}
    installed = installed or {}

    def merge_rows(field: str, identity) -> list[dict[str, Any]]:
        rows = {}
        for row in (fallback.get(field) or []) + (installed.get(field) or []):
            if not isinstance(row, dict):
                continue
            key = identity(row)
            if key:
                rows[key] = row
        return list(rows.values())

    return {
        "retravelers": merge_rows("retravelers", lambda r: str(r.get("endfield_name") or "")),
        "visual_parallels_without_lore_relation": merge_rows(
            "visual_parallels_without_lore_relation",
            lambda r: f"{r.get('endfield_name') or ''}\0{r.get('arknights_name') or ''}",
        ),
    }


async def load_entity_relation_catalog(store: Any) -> dict[str, Any]:
    """资料包内关系表优先，插件内置表只为旧包提供最小兼容路由。"""
    data_version = getattr(store, "data_version", getattr(store, "dataVersion", "no-store"))
    cached = getattr(store, "_endfield_relation_catalog", None) or getattr(store, "_endfieldRelationCatalog", None)
    if isinstance(cached, dict) and cached.get("data_version") == data_version:
        return cached.get("value", {})

    installed = {}
    if store is not None:
        try:
            get_doc = getattr(store, "get_document_by_path", None) or getattr(store, "getDocumentByPath", None)
            if get_doc is not None:
                loaded = await get_doc("config/retravelers.json")
                record = loaded.get("record", loaded) if isinstance(loaded, dict) else loaded
                lines = record.get("lines", []) if isinstance(record, dict) else []
                text = "\n".join(str(line.get("text", "")) for line in lines if isinstance(line, dict))
                if text.strip():
                    installed = json.loads(text)
        except Exception:
            installed = {}

    value = merge_relation_catalog(BUNDLED_RELATIONS, installed)
    if store is not None:
        cache_entry = {"data_version": data_version, "value": value}
        setattr(store, "_endfield_relation_catalog", cache_entry)
        setattr(store, "_endfieldRelationCatalog", cache_entry)
    return value


def relation_endfield_names(catalog: dict[str, Any] | None) -> set[str]:
    """提取目录中登记的全部终末地再旅者名字。"""
    return {
        str(row.get("endfield_name") or "").strip()
        for row in (catalog or {}).get("retravelers", [])
        if str(row.get("endfield_name") or "").strip()
    }


def relevant_retraveler_relations(catalog: dict[str, Any] | None, *values: Any) -> list[dict[str, Any]]:
    """找出本次请求或可见命中涉及的再旅者；它们是关系，不进入普通 aliases。"""
    parts = []
    for val in values:
        if isinstance(val, str):
            parts.append(val)
        else:
            try:
                parts.append(json.dumps(val, ensure_ascii=False))
            except Exception:
                parts.append(str(val or ""))
    text = normalize_search_text("\n".join(parts))
    if not text:
        return []
    results = []
    for row in (catalog or {}).get("retravelers", []):
        names = [
            normalize_search_text(row.get(k))
            for k in ("endfield_name", "terra_memory_prototype")
            if row.get(k)
        ]
        if any(name and name in text for name in names):
            item: dict[str, Any] = {
                "relation_kind": "endfield_retraveler_memory_prototype",
                "endfield_name": str(row.get("endfield_name")),
                "relation_status": str(row.get("relation_status") or "reviewed"),
                "not_alias": True,
            }
            if row.get("terra_memory_prototype"):
                item["terra_memory_prototype"] = str(row["terra_memory_prototype"])
            results.append(item)
    return results


async def attach_retraveler_relations(
    store: Any,
    response: dict[str, Any] | None,
    request: Any,
    enabled_games: list[str] | set[str] | None,
) -> dict[str, Any] | None:
    """仅双模块启用时，把人工审校关系作为独立附属字段挂到检索结果。"""
    if not response or not isinstance(response, dict):
        return response
    games = set(enabled_games or [])
    if "arknights" not in games or "endfield" not in games:
        return response
    if response.get("status") == "error" or response.get("error"):
        return response
    catalog = await load_entity_relation_catalog(store)
    relations = relevant_retraveler_relations(catalog, request, response.get("documents"))
    if relations:
        res = dict(response)
        res["retraveler_relations"] = relations
        return res
    return response


# Upstream camelCase aliases
loadEntityRelationCatalog = load_entity_relation_catalog
relationEndfieldNames = relation_endfield_names
relevantRetravelerRelations = relevant_retraveler_relations
attachRetravelerRelations = attach_retraveler_relations
