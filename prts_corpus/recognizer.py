"""用户问题的实体别名预识别与检索上下文构建。

移植自 upstream `src/entity-recognizer.js` 与 `src/timeline.js`。
包含纯 Python Aho-Corasick 多模式串匹配器，构建于实体元数据与角色别名表（char_alias.txt）之上。
生成的 `<prts:retrieval-context>` 块向 Agent 提示规范实体与跨游戏再旅者关系，不泄漏内部 document_id。
"""

from __future__ import annotations

import collections
import re
from typing import Any

from .normalize import normalize_search_text
from .routing import (
    load_entity_relation_catalog,
    relation_endfield_names,
)

GAME_LABELS: dict[str, str] = {
    "arknights": "明日方舟",
    "endfield": "终末地",
}


def prompt_data(value: Any, maximum: int = 160) -> str:
    """实体表属于可更新资料，不是受信任的 prompt。
    所有进入 plugin user notice 的字段必须保持单行、限长并转义标签边界，避免恶意资料闭合结构标签后伪造指令。
    """
    s = str(value if value is not None else "")
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    chars = list(s)[:maximum]
    clipped = "".join(chars)
    return (
        clipped.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


class _ACNode:
    __slots__ = ("next", "fail", "outputs")

    def __init__(self) -> None:
        self.next: dict[str, int] = {}
        self.fail: int = 0
        self.outputs: list[dict[str, Any]] = []


class EntityAliasAutomaton:
    """Browser 同源的纯 Python Aho-Corasick 实体别名匹配器。"""

    def __init__(self, groups: list[dict[str, Any]] | None = None) -> None:
        self.nodes: list[_ACNode] = [_ACNode()]
        for group in groups or []:
            canonical = str(group.get("canonical") or "").strip()
            if not canonical:
                continue
            for raw_alias in group.get("aliases") or []:
                alias = normalize_search_text(raw_alias)
                if not canonical or not alias:
                    continue
                node = 0
                for char in alias:
                    if char not in self.nodes[node].next:
                        self.nodes[node].next[char] = len(self.nodes)
                        self.nodes.append(_ACNode())
                    node = self.nodes[node].next[char]
                self.nodes[node].outputs.append({
                    "canonical": canonical,
                    "alias": str(raw_alias).strip(),
                    "games": sorted(list(set(group.get("games") or []))),
                    "length": len(alias),
                })

        queue: collections.deque[int] = collections.deque()
        for child in self.nodes[0].next.values():
            queue.append(child)
        while queue:
            parent = queue.popleft()
            for char, child in self.nodes[parent].next.items():
                queue.append(child)
                failure = self.nodes[parent].fail
                while failure and char not in self.nodes[failure].next:
                    failure = self.nodes[failure].fail
                self.nodes[child].fail = self.nodes[failure].next.get(char, 0)
                self.nodes[child].outputs.extend(self.nodes[self.nodes[child].fail].outputs)

    def match(self, text: str) -> list[dict[str, Any]]:
        norm = normalize_search_text(text)
        characters = list(norm)
        found: list[dict[str, Any]] = []
        node = 0
        for index, char in enumerate(characters):
            while node and char not in self.nodes[node].next:
                node = self.nodes[node].fail
            node = self.nodes[node].next.get(char, 0)
            for output in self.nodes[node].outputs:
                start = index - output["length"] + 1
                # 单字别名在普通句子中误报率过高；仅当整个输入就是该字时保留。
                if output["length"] == 1 and len(characters) > 1:
                    continue
                found.append({
                    "canonical": output["canonical"],
                    "canonical_name": output["canonical"],
                    "alias": output["alias"],
                    "matched_alias": output["alias"],
                    "games": output["games"],
                    "game": output["games"][0] if output["games"] else "",
                    "start": start,
                    "end": index + 1,
                    "span": (start, index + 1),
                })

        found.sort(key=lambda m: (m["start"], -(m["end"] - m["start"]), m["canonical"]))
        selected: list[dict[str, Any]] = []
        for match in found:
            # 最长区间覆盖去重
            if any(
                match["start"] >= item["start"]
                and match["end"] <= item["end"]
                and not (match["start"] == item["start"] and match["end"] == item["end"])
                for item in selected
            ):
                continue
            # 同区间同名去重
            if any(
                match["start"] == item["start"]
                and match["end"] == item["end"]
                and match["canonical"] == item["canonical"]
                for item in selected
            ):
                continue
            selected.append(match)
        return selected


class EntityRecognizer:
    """实体预识别器：集成实体包投影与 char_alias.txt 别名表。"""

    def __init__(
        self,
        entities: list[dict[str, Any]] | None = None,
        alias_lines: list[Any] | None = None,
        catalog: dict[str, Any] | None = None,
    ) -> None:
        self.groups: list[dict[str, Any]] = []
        self._automaton: EntityAliasAutomaton = EntityAliasAutomaton([])
        if entities is not None or alias_lines is not None:
            self._init_from_sources(entities or [], alias_lines or [], catalog)

    def _init_from_sources(
        self,
        entities: list[dict[str, Any]],
        alias_lines: list[Any],
        catalog: dict[str, Any] | None = None,
    ) -> None:
        groups_map: dict[str, dict[str, Any]] = {}
        cross_game_names = relation_endfield_names(catalog) if catalog else set()

        def remember(canonical_val: Any, alias_vals: Any = None, game_val: Any = "") -> None:
            canonical = str(canonical_val or "").strip()
            if not canonical:
                return
            group = groups_map.get(canonical)
            if not group:
                group = {
                    "canonical": canonical,
                    "aliases": {canonical},
                    "games": set(),
                }
                groups_map[canonical] = group

            if alias_vals:
                if isinstance(alias_vals, (list, tuple, set)):
                    vals = alias_vals
                else:
                    vals = [alias_vals]
                for v in vals:
                    alias = str(v or "").strip()
                    # 旧 PRTS 数据曾把终末地再旅者名塞进泰拉人物 aliases 方便联搜。
                    # 关系现在由独立附属字段承载；跨游戏名不再改变实体身份。
                    if alias and (alias == canonical or alias not in cross_game_names):
                        group["aliases"].add(alias)

            game = str(game_val or "").strip().lower()
            if game in ("arknights", "endfield"):
                group["games"].add(game)

        # 1. entities 包每条实体记录的 canonical_name + aliases（权威源）
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            record = ent.get("record", ent) if isinstance(ent.get("record"), dict) else ent
            entity = record.get("entity", {}) if isinstance(record.get("entity"), dict) else {}
            doc = record.get("document", {}) if isinstance(record.get("document"), dict) else record

            canonical = (
                entity.get("canonical_name")
                or record.get("canonical_name")
                or record.get("canonical")
                or doc.get("canonical_name")
                or doc.get("display_title")
            )
            aliases = (
                entity.get("aliases")
                or record.get("aliases")
                or doc.get("aliases")
                or []
            )
            identity = f"{doc.get('document_id', '')} {doc.get('source_ref_prefix', '')}".lower()
            game = doc.get("game") or record.get("game") or ("endfield" if "endfield:" in identity else "arknights")
            remember(canonical, aliases, game)

        # 2. references/char_alias.txt 的分号分隔行（挂到同名 canonical 组，找不到则自建）
        for raw_line in alias_lines:
            line_text = raw_line.get("text", "") if isinstance(raw_line, dict) else str(raw_line or "")
            parts = [a.strip() for a in re.split(r"[;；]", line_text) if a.strip()]
            if not parts:
                continue
            # 首个别名查同名 canonical 组
            primary = parts[0]
            existing = next(
                (g for g in groups_map.values() if g["canonical"] == primary or primary in g["aliases"]),
                None,
            )
            target_canonical = existing["canonical"] if existing else primary
            remember(target_canonical, parts)

        self.groups = [
            {
                "canonical": g["canonical"],
                "aliases": sorted(list(g["aliases"])),
                "games": sorted(list(g["games"])),
            }
            for g in groups_map.values()
        ]
        self._automaton = EntityAliasAutomaton(self.groups)

    @classmethod
    def build(
        cls_or_self,
        entities: list[dict[str, Any]] | None = None,
        alias_lines: list[Any] | None = None,
        catalog: dict[str, Any] | None = None,
    ) -> EntityRecognizer:
        instance = cls_or_self() if isinstance(cls_or_self, type) else cls_or_self
        instance._init_from_sources(entities or [], alias_lines or [], catalog)
        return instance

    def recognize(self, text: str) -> list[dict[str, Any]]:
        """识别文本中的实体别名并去重，返回规范名、游戏归属、匹配别名与 span。"""
        if not text:
            return []
        return self._automaton.match(text)


def detect_entities(recognizer: EntityRecognizer, catalog: dict[str, Any] | None, text: str) -> dict[str, Any]:
    matches = recognizer.recognize(text)
    norm_text = normalize_search_text(text)
    relation_hints = []
    for row in (catalog or {}).get("retravelers", []):
        names = [str(row.get(k) or "").strip() for k in ("endfield_name", "terra_memory_prototype") if row.get(k)]
        if any(name and normalize_search_text(name) in norm_text for name in names):
            terms = list(dict.fromkeys(names + ["再旅者", "记忆原型"]))
            hint = dict(row)
            hint["kind"] = "retraveler_memory_prototype"
            hint["query_terms"] = terms
            relation_hints.append(hint)

    for row in (catalog or {}).get("visual_parallels_without_lore_relation", []):
        names = [str(row.get(k) or "").strip() for k in ("endfield_name", "arknights_name") if row.get(k)]
        if any(name and normalize_search_text(name) in norm_text for name in names):
            hint = dict(row)
            hint["kind"] = "visual_parallel_without_lore_relation"
            hint["query_terms"] = names
            relation_hints.append(hint)

    return {
        "matches": matches,
        "entities": list(dict.fromkeys(m["canonical"] for m in matches)),
        "relation_hints": relation_hints,
    }


def recognition_context(result: dict[str, Any], enabled_games: list[str] | set[str] | tuple[str, ...]) -> str:
    aliases_map: dict[str, dict[str, Any]] = {}
    for match in result.get("matches", []):
        canonical = match["canonical"]
        item = aliases_map.setdefault(canonical, {"aliases": set(), "games": set()})
        item["aliases"].add(match["alias"])
        for g in match.get("games") or []:
            item["games"].add(g)

    for hint in result.get("relation_hints", []):
        if hint.get("endfield_name"):
            name = hint["endfield_name"]
            item = aliases_map.setdefault(name, {"aliases": set(), "games": set()})
            item["aliases"].add(name)
            item["games"].add("endfield")
        arknights_name = hint.get("terra_memory_prototype") or hint.get("arknights_name")
        if arknights_name:
            item = aliases_map.setdefault(arknights_name, {"aliases": set(), "games": set()})
            item["aliases"].add(arknights_name)
            item["games"].add("arknights")

    lines = []
    for canonical, value in aliases_map.items():
        ownership = [GAME_LABELS[g] for g in value["games"] if g in GAME_LABELS]
        ownership_str = " + ".join(ownership) if ownership else "归属未确定"
        hit_aliases = "、".join(prompt_data(a) for a in value["aliases"])
        lines.append(f"- {prompt_data(canonical)} — {ownership_str}（问题中命中：{hit_aliases}）")

    enabled = [GAME_LABELS[g] for g in enabled_games if g in GAME_LABELS]
    relation_lines = []
    for hint in result.get("relation_hints", []):
        if hint.get("kind") == "retraveler_memory_prototype":
            ef = prompt_data(hint.get("endfield_name"))
            proto = prompt_data(hint.get("terra_memory_prototype") or "未登记")
            terms = "、".join(prompt_data(t) for t in (hint.get("query_terms") or []))
            relation_lines.append(f"- {ef}：再旅者；泰拉记忆原型={proto}。检索词：{terms}。两者不是别名。")
        else:
            ef = prompt_data(hint.get("endfield_name"))
            ak = prompt_data(hint.get("arknights_name"))
            relation_lines.append(f"- {ef} / {ak}：仅登记外观相似；现有剧情没有关系证据，不得推断为再旅者或记忆原型。")

    output_lines = [
        "<prts:retrieval-context>",
        f"当前搭载资料：{'、'.join(enabled) or '无'}。",
    ]
    if lines:
        output_lines.append("用户问题中识别到的规范实体与游戏归属：")
        output_lines.extend(lines)
    output_lines.extend(relation_lines)
    output_lines.append("</prts:retrieval-context>")
    return "\n".join(output_lines)


# 缓存已准备的识别器，按 store 对象与 data_version 缓存
_prepared_recognizers: dict[int, tuple[str | None, EntityRecognizer]] = {}


async def prepare_entity_recognition(store: Any, data_version: str | None = None) -> EntityRecognizer | None:
    """按 store + data_version 预热并缓存只读 AC 自动机。"""
    if store is None:
        return None
    ready_method = getattr(store, "ready", None)
    if ready_method and callable(ready_method):
        await ready_method()

    dv = data_version or getattr(store, "data_version", getattr(store, "dataVersion", None))
    store_id = id(store)
    cached = _prepared_recognizers.get(store_id)
    if cached and cached[0] == dv and cached[1] is not None:
        return cached[1]

    catalog = await load_entity_relation_catalog(store)

    entities: list[dict[str, Any]] = []
    iter_docs = getattr(store, "iterate_documents", None) or getattr(store, "iterateDocuments", None)
    if iter_docs:
        try:
            async for record in iter_docs(predicate=lambda doc, *_: doc.get("document_type") == "entity"):
                entities.append(record)
        except Exception:
            pass

    if not entities and hasattr(store, "documents") and isinstance(store.documents, dict):
        for doc_id, item in store.documents.items():
            doc = item.get("document", {}) if isinstance(item, dict) else {}
            if doc.get("document_type") == "entity":
                entities.append(item)

    alias_lines: list[Any] = []
    get_doc = getattr(store, "get_document_by_path", None) or getattr(store, "getDocumentByPath", None)
    if get_doc:
        try:
            loaded = await get_doc("char_alias.txt")
            record = loaded.get("record", loaded) if isinstance(loaded, dict) else loaded
            if isinstance(record, dict):
                alias_lines = record.get("lines", [])
        except Exception:
            alias_lines = []

    recognizer = EntityRecognizer.build(entities, alias_lines, catalog=catalog)
    _prepared_recognizers[store_id] = (dv, recognizer)
    return recognizer


async def build_retrieval_context(
    store: Any,
    recognizer: EntityRecognizer | None,
    text: str,
    enabled_games: list[str] | set[str] | tuple[str, ...],
    data_version: str | None = None,
) -> str | None:
    """生成 <prts:retrieval-context> 块向模型提示规范实体、游戏归属与再旅者关系。"""
    if not text or not str(text).strip():
        return None

    if recognizer is None:
        recognizer = await prepare_entity_recognition(store, data_version)
    if recognizer is None:
        return None

    catalog = await load_entity_relation_catalog(store)
    result = detect_entities(recognizer, catalog, text)
    return recognition_context(result, enabled_games)
