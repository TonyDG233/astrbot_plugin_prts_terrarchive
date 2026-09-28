"""CorpusStore：对 prts-browser-corpus-release-v1 资料包的只读访问层。

Public store interface used by search.py and read.py:
------------------------------------------------------
- corpus_version_snapshot(store) -> dict: {"data_version": str|None, "release_id": str|None}
- assert_corpus_version(store, snapshot: dict|None) -> None
- document_uid(document_id: str) -> str
- document_game(document: dict|None) -> str
- normalize_story_stage_code(value) -> str
- public_story_stage_code(value, relaxed_input: bool = False) -> str
- public_metadata_text(value, maximum: int = 256) -> str
- public_story_part(value) -> str
- operator_record_segment(document: dict|None) -> str
- public_character_material(document: dict|None) -> str
- natural_document_title(document: dict|None) -> str
- compute_lines_integrity(lines: list[dict]) -> str

Class CorpusStore:
- __init__(self, releases_dir: str, content_cache_bytes: int = 64*1024*1024, index_cache_bytes: int = 32*1024*1024, cursor_secret_path: str|None = None, ...)
- async def ready(self) -> None
- def reset(self) -> None
- property loaded: bool
- property data_version: str | None (alias: dataVersion)
- property release_id: str | None (alias: releaseId)
- property packs: dict[str, dict]
- property documents: dict[str, dict]
- property document_order: list[str] (alias: documentOrder)
- property prefix_index: dict[str, str] (alias: prefixIndex)
- property uid_index: dict[str, str] (alias: uidIndex)
- property title_index: dict[str, list[str]] (alias: titleIndex)
- property natural_title_index: dict[str, list[str]] (alias: naturalTitleIndex)
- property story_stage_index: dict[str, list[str]] (alias: storyStageIndex)
- property story_stage_code_index: dict[str, list[str]] (alias: storyStageCodeIndex)
- property operator_record_index: dict[str, list[str]] (alias: operatorRecordIndex)
- property character_material_index: dict[str, list[str]] (alias: characterMaterialIndex)
- property path_index: dict[str, str] (alias: pathIndex)
- property source_story_index: dict[str, str] (alias: sourceStoryIndex)
- property search_index_documents: dict[str, str] (alias: searchIndexDocuments)
- property has_trigram_index: bool (alias: hasTrigramIndex)
- def cache_stats(self) -> dict[str, int]
- async def get_document(self, document_id: str) -> dict | None (alias: getDocument)
- async def get_document_by_uid(self, uid: str) -> dict | None (alias: getDocumentByUid)
- def get_document_id_by_uid(self, uid: str) -> str (alias: getDocumentIdByUid)
- def get_document_id_by_prefix(self, prefix: str) -> str | None (alias: getDocumentIdByPrefix)
- async def get_document_by_source_story_id(self, source_story_id: str) -> dict | None (alias: getDocumentBySourceStoryId)
- async def get_document_by_path(self, path: str) -> dict | None (alias: getDocumentByPath)
- async def get_document_by_title(self, title: str) -> dict | None (alias: getDocumentByTitle)
- async def get_document_by_story_stage(self, stage_code: str, story_part: str = "") -> dict | None (alias: getDocumentByStoryStage)
- async def get_operator_record(self, character_name: str, record_name: str, segment=None) -> dict | None (alias: getOperatorRecord)
- async def get_character_material(self, character_name: str, material: str, games: list[str] | None = None) -> dict | None (alias: getCharacterMaterial)
- def document_ordinal(self, document_id: str) -> int | None (alias: documentOrdinal)
- def ordered_document_ids(self, document_ids: list[str] | None = None) -> list[str] (alias: orderedDocumentIds)
- def requires_document_uid(self, document_id: str) -> bool (alias: requiresDocumentUid)
- def has_unique_story_stage(self, document_id: str) -> bool (alias: hasUniqueStoryStage)
- def has_unique_operator_record(self, document_id: str) -> bool (alias: hasUniqueOperatorRecord)
- def has_unique_character_material(self, document_id: str) -> bool (alias: hasUniqueCharacterMaterial)
- def is_preferred_natural_document(self, document_id: str) -> bool (alias: isPreferredNaturalDocument)
- def activity_story_documents(self, activity_id: str = "", activity_name: str = "", anchor_document_id: str = "") -> list[dict] (alias: activityStoryDocuments)
- def endfield_collection_documents(self, collection_name: str = "", content_types: list[str] | None = None, anchor_document_id: str = "") -> list[dict] (alias: endfieldCollectionDocuments)
- async def get_or_create_cursor_secret(self) -> bytes (alias: getOrCreateCursorSecret)
- def supports_ngram_size(self, size: int, pack_ids: list[str] | None = None) -> bool (alias: supportsNgramSize)
- async def find_documents_by_ngrams(self, trigrams: list[str], signal=None, deadline=float("inf"), pack_ids: list[str] | None = None) -> list[str] | None (alias: findDocumentsByNgrams)
- async def find_documents_by_trigrams(self, trigrams: list[str], runtime=None, **kwargs) -> list[str] | None (alias: findDocumentsByTrigrams)
- async def find_documents_by_short_literal(self, value: str, signal=None, deadline=float("inf"), pack_ids: list[str] | None = None, document_ids: list[str] | None = None) -> list[str] | None (alias: findDocumentsByShortLiteral)
- async def iterate_documents(self, document_ids: list[str] | None = None, predicate=None) (aliases: iterateDocuments, iter_search_documents)
"""

import asyncio
import base64
import hashlib
import os
import pathlib
import re
import secrets
import stat
import struct
import time
import unicodedata
import zlib

try:
    import orjson

    def json_loads(data: str | bytes):
        return orjson.loads(data)
except ImportError:
    import json

    def json_loads(data: str | bytes):
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        return json.loads(data)

from .constants import CORPUS_RESOURCE_LIMITS
from .errors import ContractError, InstallerFault
from .normalize import (
    PRTSNG2_MAGIC,
    PRTSTG1_MAGIC,
    compare_ngram_keys,
    decode_varint,
)

DOCUMENT_ORDERING_VERSION = 2

PACK_ORDER = (
    "official_game",
    "endfield_official_game",
    "reviewed_wiki",
    "terra_journey",
    "endfield_reviewed_knowledge",
    "entities",
    "references",
)

MAX_SHARD_CACHE_BYTES = 96 * 1024 * 1024
MAX_SEARCH_CACHE_BYTES = 64 * 1024 * 1024
MAX_SHORT_LITERAL_CACHE_CANDIDATES = 65536
MAX_SHORT_LITERAL_SCAN_QUEUE = 16
SHORT_LITERAL_SCAN_WORKERS = 4

END_FIELD_CONTENT_TYPE_ORDER = (
    "dialogue",
    "cutscene",
    "black_screen",
    "radio",
    "remote_comm",
    "environment_talk",
    "sns_topic",
    "sns_chat",
    "narration",
)
END_FIELD_NARRATIVE_CONTENT_TYPES = set(END_FIELD_CONTENT_TYPE_ORDER)

CHARACTER_MATERIALS = {
    "干员档案": "profile",
    "角色档案": "profile",
    "模组文案": "module",
    "干员语音": "voice",
    "角色语音": "voice",
    "时装文案": "skin",
    "招聘合同": "recruitment",
    "潜能与信物": "potential",
}


def corpus_version_snapshot(store) -> dict:
    """同时绑定 reset 代次与资料版本，避免跨 await 的读取链混用新旧记录。"""
    return {
        "data_version": store.data_version,
        "release_id": store.release_id,
        "generation": getattr(store, "_generation", 0),
    }


def assert_corpus_version(store, snapshot: dict | None) -> None:
    """检查资料版本快照；若在读取期间发生变化则抛出重试异常。"""
    if store.data_version is None and not snapshot:
        return
    if not snapshot:
        raise ContractError("PACKAGE_VERSION_MISMATCH", "资料版本在读取期间发生变化，请重试", retryable=True)

    generation = snapshot.get("generation")
    if generation is not None and generation != getattr(store, "_generation", 0):
        raise ContractError("PACKAGE_VERSION_MISMATCH", "资料版本在读取期间发生变化，请重试", retryable=True)

    data_version = snapshot.get("data_version", snapshot.get("dataVersion"))
    if data_version != store.data_version:
        raise ContractError("PACKAGE_VERSION_MISMATCH", "资料版本在读取期间发生变化，请重试", retryable=True)

    release_id = snapshot.get("release_id", snapshot.get("releaseId"))
    if release_id is not None and release_id != store.release_id:
        raise ContractError("PACKAGE_VERSION_MISMATCH", "资料版本在读取期间发生变化，请重试", retryable=True)


def sha256_hex(text: str | bytes) -> str:
    """计算 SHA-256 十六进制值。"""
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def document_uid(document_id: str) -> str:
    """面向模型与 UI 的短篇章标识（由 canonical document_id 确定性派生，96-bit 摘要 base64url）。"""
    value = str(document_id or "")
    if not value:
        return ""
    digest = hashlib.sha256(f"prts-document\0{value}".encode("utf-8")).digest()
    b64url = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"doc_{b64url[:16]}"


def document_game(document: dict | None = None) -> str:
    """模型/界面使用的自然语言资料定位：arknights 或 endfield。"""
    if not document:
        return "arknights"
    explicit = str(document.get("game") or "").strip().lower()
    if explicit in ("arknights", "endfield"):
        return explicit
    identity = f"{document.get('document_id') or ''} {document.get('source_ref_prefix') or ''}".lower()
    return "endfield" if "endfield:" in identity else "arknights"


def normalize_story_stage_code(value) -> str:
    """将玩家可见的明日方舟关卡代号归一化为索引键（如 gt－3 → GT-3）。"""
    s = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", s)
    s = re.sub(r"\s+", "", s)
    return s.upper()


def public_story_stage_code(value, relaxed_input: bool = False) -> str:
    """可安全放进模型工具参数的公开关卡代号；非法资料值不进入短定位索引。"""
    raw = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    if not relaxed_input:
        if any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in raw):
            return ""
    normalized = normalize_story_stage_code(value)
    if len(normalized) <= 32 and re.match(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$", normalized):
        return normalized
    return ""


def public_metadata_text(value, maximum: int = 256) -> str:
    """清除元数据中可改写工具渲染结构的控制/换行/双向字符。"""
    s = unicodedata.normalize("NFC", str(value if value is not None else ""))
    chars = [" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in s]
    s = "".join(chars)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:maximum]


def story_stage_key(stage_code: str, story_part: str) -> str:
    return f"{normalize_story_stage_code(stage_code)}\0{str(story_part or '').strip().lower()}"


def public_story_part(value) -> str:
    """将资料内部的 part_type 映射成 Agent 使用的玩家语义。"""
    part = str(value if value is not None else "").strip().lower()
    if part in ("before", "after"):
        return part
    if part in ("story", "interlude", "body"):
        return "story"
    return ""


def operator_record_segment(document: dict | None = None) -> str:
    """GameData 干员密录 source_story_id 末段即页面段号（如 1、2 等）；无则返回空字符串。"""
    if not document or document_game(document) != "arknights" or document.get("document_category") != "memory":
        return ""
    source = str(document.get("source_story_id") or document.get("document_id") or "")
    match = re.search(r"_([1-9][0-9]*)$", source)
    return match.group(1) if match else ""


def public_character_material(document: dict | None = None) -> str:
    """获取公开角色资料类型映射。"""
    if not document or document.get("document_type") != "character":
        return ""
    return CHARACTER_MATERIALS.get(str(document.get("document_category") or ""), "")


def normalized_lookup_text(value) -> str:
    s = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    return re.sub(r"\s+", " ", s).strip()


def operator_record_key(character_name: str, record_name: str, segment: str | int = "") -> str:
    return f"{normalized_lookup_text(character_name)}\0{normalized_lookup_text(record_name)}\0{segment}"


def character_material_key(game: str, character_name: str, material: str) -> str:
    return f"{str(game or '')}\0{normalized_lookup_text(character_name)}\0{str(material or '')}"


def natural_sort_key(s: str | None) -> list:
    """自然排序键（数字段以整数比较，文本段忽略大小写）。"""
    if s is None:
        return []
    parts = re.split(r"(\d+)", str(s))
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def natural_document_title_base(document: dict | None = None, include_operator_record_segment: bool = True) -> str:
    if not document:
        return ""
    display_title = public_metadata_text(document.get("display_title"))
    if document_game(document) == "endfield" and document.get("game"):
        return display_title
    doc_type = document.get("document_type")
    if doc_type == "entity":
        return f"{display_title} / 实体资料" if display_title else ""
    if doc_type == "character":
        return display_title
    if doc_type == "knowledge":
        kind = document.get("document_kind")
        if kind == "terra_journey":
            return f"{display_title} / 大地巡旅"
        if kind == "wiki":
            explicit_role = str(document.get("wiki_role") or "")
            if explicit_role == "story":
                return f"{display_title} / 活动 Wiki"
            if explicit_role == "character_activity":
                seq = document.get("sequence_index")
                sequence = f" · 第 {seq} 篇" if isinstance(seq, int) and not isinstance(seq, bool) else ""
                return f"{display_title} / 角色活动 Wiki{sequence}"
            if explicit_role == "other":
                return f"{display_title} / 审校资料"
            path = str(document.get("path") or "")
            if path.startswith("stories/"):
                return f"{display_title} / 活动 Wiki"
            if path.startswith("char_v3/prompt_"):
                seq = document.get("sequence_index")
                sequence = f" · 第 {seq} 篇" if isinstance(seq, int) and not isinstance(seq, bool) else ""
                return f"{display_title} / 角色活动 Wiki{sequence}"
            if path.startswith("char_v3/extended_"):
                return f"{display_title} / 角色补充 Wiki"
            return f"{display_title} / 角色 Wiki"
        return display_title
    if doc_type == "reference":
        labels = {
            "activity_timelines": "活动时间线",
            "char_alias": "角色别名表",
            "terra_timeline": "泰拉年表",
        }
        return labels.get(display_title, display_title)
    if doc_type != "story":
        return display_title

    source = str(document.get("source_story_id") or document.get("document_id") or "")
    variation_match = re.search(r"_variation0*(\d+)(?:\.[^./]+)?$", source, re.IGNORECASE)
    identity_items = []
    for item in (document.get("activity_name"), document.get("story_code"), document.get("story_name")):
        t = public_metadata_text(item)
        if t and t not in identity_items:
            identity_items.append(t)
    if not identity_items:
        labels = {"rogue": "集成战略文本", "system": "游戏系统文本", "guide": "游戏教程文本"}
        label = labels.get(str(document.get("document_category") or ""), "游戏内原文")
        coll_id = str(document.get("collection_id") or "").split(":")[-1].split("/")[-1]
        collection = public_metadata_text(coll_id, 128)
        seq = document.get("sequence_index")
        sequence = f"第 {seq} 篇" if isinstance(seq, int) and not isinstance(seq, bool) else ""
        part_lbl = public_metadata_text(document.get("part_label"), 128)
        parts = [label]
        if collection and collection not in ("other", "obt"):
            parts.append(collection)
        if sequence:
            parts.append(sequence)
        if part_lbl:
            parts.append(part_lbl)
        fallback = " · ".join(parts)
        return fallback or display_title

    memory_prefix = []
    if document.get("document_category") == "memory" and document.get("character_name"):
        char_name = public_metadata_text(document.get("character_name"))
        if char_name:
            memory_prefix = [char_name, "干员密录"]

    memory_segment_part = []
    if include_operator_record_segment and memory_prefix:
        seg = operator_record_segment(document)
        if seg:
            memory_segment_part = [f"第 {seg} 段"]

    sequence_suffix = []
    if (
        document.get("document_category") == "activity"
        and not document.get("story_code")
        and isinstance(document.get("sequence_index"), int)
        and not isinstance(document.get("sequence_index"), bool)
    ):
        sequence_suffix = [f"第 {document.get('sequence_index')} 篇"]

    variation_suffix = []
    if variation_match:
        variation_suffix = [f"分支{int(variation_match.group(1))}"]

    combined = (
        memory_prefix
        + identity_items
        + memory_segment_part
        + ([public_metadata_text(document.get("part_label"), 128)] if document.get("part_label") else [])
        + sequence_suffix
        + variation_suffix
    )
    res = " · ".join(str(x).strip() for x in combined if str(x).strip())
    return res or display_title


def natural_document_title(document: dict | None = None) -> str:
    """v2 联合语料使用带游戏前缀的自然标题消除跨游戏同名歧义。"""
    if not document:
        return ""
    title = public_metadata_text(natural_document_title_base(document), 480)
    if not title or not document.get("game"):
        return title
    label = "终末地" if document_game(document) == "endfield" else "明日方舟"
    prefix = f"{label} · "
    full = title if title.startswith(prefix) else f"{prefix}{title}"
    return public_metadata_text(full, 512)


def legacy_operator_record_title(document: dict | None = None) -> str:
    if not document or not operator_record_segment(document):
        return ""
    title = public_metadata_text(
        natural_document_title_base(document, include_operator_record_segment=False), 480
    )
    if not title or not document.get("game"):
        return title
    label = "终末地" if document_game(document) == "endfield" else "明日方舟"
    prefix = f"{label} · "
    full = title if title.startswith(prefix) else f"{prefix}{title}"
    return public_metadata_text(full, 512)


def compute_lines_integrity(lines: list[dict]) -> str:
    """行完整性规则：sha256(全部行文本以 \\n 连接) === local_integrity.sha256。"""
    joined = "\n".join(str(line.get("text", "")) for line in lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def collect_unstable_chars(text: str, stable_chars: set[str], unstable_chars: dict[str, str]) -> None:
    if not isinstance(text, str) or not text:
        return
    for character in text:
        if character in stable_chars:
            continue
        expansion = unicodedata.normalize("NFKC", character).lower()
        if expansion == character:
            stable_chars.add(character)
        else:
            unstable_chars[character] = expansion


def canonical_variants(query: str, limit: int = 32) -> set[str]:
    variants = {query}
    decomposed = unicodedata.normalize("NFD", query)
    if decomposed != query:
        variants.add(decomposed)
    agenda = [decomposed]
    while agenda and len(variants) < limit:
        current = agenda.pop()
        chars = list(current)
        for index in range(len(chars) - 1):
            if len(variants) >= limit:
                break
            pair = chars[index] + chars[index + 1]
            composed = unicodedata.normalize("NFC", pair)
            if len(composed) != 1 or composed == pair:
                continue
            next_str = "".join(chars[:index] + [composed] + chars[index + 2 :])
            if next_str in variants:
                continue
            variants.add(next_str)
            agenda.append(next_str)
    return variants


def pack_sort_key(entry: dict) -> tuple[int, str]:
    name = entry.get("name") or entry.get("pack_id") or ""
    try:
        rank = PACK_ORDER.index(name)
    except ValueError:
        rank = len(PACK_ORDER)
    return (rank, name)


def federated_order(documents: dict, source_order: list[str]) -> list[str]:
    by_game = {"arknights": [], "endfield": []}
    other = []
    for document_id in source_order:
        item = documents.get(document_id)
        game = document_game(item.get("document")) if item else ""
        if game in by_game:
            by_game[game].append(document_id)
        else:
            other.append(document_id)
    if not by_game["arknights"] or not by_game["endfield"]:
        return list(source_order)
    ordered = []
    maximum = max(len(by_game["arknights"]), len(by_game["endfield"]))
    for index in range(maximum):
        for game in ("arknights", "endfield"):
            game_list = by_game[game]
            if index < len(game_list):
                ordered.append(game_list[index])
    return ordered + other


def search_index_range(descriptor: dict | None = None) -> dict[str, str]:
    if not descriptor:
        return {"first": "", "last": ""}
    return {
        "first": str(descriptor.get("first_ngram") or descriptor.get("first_trigram") or ""),
        "last": str(descriptor.get("last_ngram") or descriptor.get("last_trigram") or ""),
    }


def lookup_ngram_index(data: bytes, target: str) -> list[int]:
    """查询 PRTSTG1 / PRTSNG2 二进制倒排分片。"""
    if len(data) < 12:
        raise ValueError("CorpusStore: invalid ngram index magic")
    magic = data[:8]
    if magic != PRTSNG2_MAGIC and magic != PRTSTG1_MAGIC:
        raise ValueError("CorpusStore: invalid ngram index magic")
    count = struct.unpack_from("<I", data, 8)[0]
    payload_start = 12 + (count + 1) * 4
    if payload_start > len(data):
        raise ValueError("CorpusStore: invalid trigram offset table")

    target_bytes = target.encode("utf-8")

    def offset_at(idx: int) -> int:
        return struct.unpack_from("<I", data, 12 + idx * 4)[0]

    def record_at(idx: int):
        start = payload_start + offset_at(idx)
        end = payload_start + offset_at(idx + 1)
        if start > len(data) or end > len(data) or start > end:
            raise ValueError("CorpusStore: malformed trigram record")
        text_length, pos = decode_varint(data, start)
        key_bytes = data[pos : pos + text_length]
        pos += text_length
        posting_count, pos = decode_varint(data, pos)
        indexes = []
        current = 0
        for _ in range(posting_count):
            delta, pos = decode_varint(data, pos)
            current += delta
            indexes.append(current)
        if pos != end:
            raise ValueError("CorpusStore: malformed trigram record")
        return key_bytes, indexes

    low = 0
    high = count - 1
    while low <= high:
        middle = (low + high) >> 1
        key_bytes, indexes = record_at(middle)
        if key_bytes == target_bytes:
            return indexes
        if key_bytes < target_bytes:
            low = middle + 1
        else:
            high = middle - 1
    return []


def validate_safe_relative_path(base_dir: str, rel_path: str) -> str:
    """验证分片相对路径安全规范并限制在基准目录内。"""
    norm_rel = rel_path.replace("\\", "/")
    p = pathlib.PurePosixPath(norm_rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"CorpusStore: invalid asset path {rel_path}")
    base_resolved = os.path.realpath(base_dir)
    joined = os.path.join(base_dir, *p.parts)
    joined_resolved = os.path.realpath(joined)
    try:
        common = os.path.commonpath([base_resolved, joined_resolved])
        if common != base_resolved:
            raise ValueError(f"CorpusStore: asset path outside release dir: {rel_path}")
    except ValueError as e:
        raise ValueError(f"CorpusStore: asset path outside release dir: {rel_path}") from e
    return joined


def _gunzip_bounded_sync(path: str, descriptor: dict | None) -> bytes:
    """带严格上限和校验的同步 gzip 解压缩（gzip 炸弹防御）。"""
    expected_size = descriptor.get("uncompressed_size") if descriptor else None
    expected_compressed_size = descriptor.get("compressed_size") if descriptor else None
    expected_hash = str(descriptor.get("sha256") or "") if descriptor else ""
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 1
        or expected_size > CORPUS_RESOURCE_LIMITS["maxAssetUncompressedBytes"]
        or not isinstance(expected_compressed_size, int)
        or isinstance(expected_compressed_size, bool)
        or expected_compressed_size < 1
        or expected_compressed_size > CORPUS_RESOURCE_LIMITS["maxAssetCompressedBytes"]
        or not re.match(r"^[0-9a-f]{64}$", expected_hash)
    ):
        raise ValueError("CorpusStore: invalid declared packed-asset metadata")

    try:
        st = os.lstat(path)
    except OSError as e:
        raise ValueError("CorpusStore: packed asset changed after release validation") from e

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size != expected_compressed_size:
        raise ValueError("CorpusStore: packed asset changed after release validation")

    with open(path, "rb") as f:
        compressed = f.read()

    if len(compressed) != expected_compressed_size or hashlib.sha256(compressed).hexdigest() != expected_hash:
        raise ValueError("CorpusStore: packed asset checksum changed after release validation")

    decompressor = zlib.decompressobj(wbits=31)
    chunk_size = 64 * 1024
    decompressed = bytearray()
    for i in range(0, len(compressed), chunk_size):
        chunk = compressed[i : i + chunk_size]
        out = decompressor.decompress(chunk, expected_size - len(decompressed) + 1)
        decompressed.extend(out)
        if len(decompressed) > expected_size:
            raise ValueError("CorpusStore: packed asset decompressed-size mismatch")

    out = decompressor.flush(expected_size - len(decompressed) + 1)
    decompressed.extend(out)
    if len(decompressed) != expected_size:
        raise ValueError("CorpusStore: packed asset decompressed-size mismatch")
    return bytes(decompressed)


def _decode_shard(buffer: bytes, checkpoint=None) -> list[dict]:
    """解析 JSONL 分片明文字节。"""
    records = []
    lines = buffer.split(b"\n")
    for index, line in enumerate(lines):
        if (index & 63) == 0 and checkpoint is not None:
            checkpoint()
        line = line.strip()
        if line:
            records.append(json_loads(line))
    return records


class CorpusStore:
    """对 prts-browser-corpus-release-v1 资料包的只读访问层。"""

    def __init__(
        self,
        releases_dir: str,
        content_cache_bytes: int = 64 * 1024 * 1024,
        index_cache_bytes: int = 32 * 1024 * 1024,
        cursor_secret_path: str | None = None,
        ensure=None,
        cache_shards: int = 24,
        search_cache_shards: int = 32,
    ):
        if not releases_dir:
            raise ValueError("CorpusStore: releasesDir is required")
        self.releases_dir = releases_dir
        self.cache_shards = cache_shards
        self.search_cache_shards = search_cache_shards
        self._max_content_cache_bytes = content_cache_bytes
        self._max_index_cache_bytes = index_cache_bytes
        self.ensure = ensure
        self.cursor_secret_path = cursor_secret_path or os.path.join(
            os.path.dirname(releases_dir), "cursor-secret.bin"
        )

        self._generation = 0
        self._loaded = False
        self.release_id = None
        self.data_version = None

        self.documents = {}
        self.document_order = []
        self.prefix_index = {}
        self.uid_index = {}
        self.title_index = {}
        self.natural_title_index = {}
        self.story_stage_index = {}
        self.story_stage_code_index = {}
        self.operator_record_index = {}
        self.character_material_index = {}
        self.path_index = {}
        self.source_story_index = {}
        self.packs = {}
        self.search_index_documents = {}
        self.unstable_chars = {}

        self._shard_cache = {}
        self._shard_cache_sizes = {}
        self._shard_cache_bytes = 0

        self._search_cache = {}
        self._search_cache_sizes = {}
        self._search_cache_bytes = 0

        self._short_literal_cache = {}
        self._short_literal_cache_candidate_count = 0

        self._init_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._secret_lock = asyncio.Lock()
        self._short_literal_scan_lock = asyncio.Lock()
        self._cursor_secret = None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def releaseId(self) -> str | None:
        return self.release_id

    @property
    def dataVersion(self) -> str | None:
        return self.data_version

    @property
    def documentOrder(self) -> list[str]:
        return self.document_order

    @property
    def prefixIndex(self) -> dict[str, str]:
        return self.prefix_index

    @property
    def uidIndex(self) -> dict[str, str]:
        return self.uid_index

    @property
    def titleIndex(self) -> dict[str, list[str]]:
        return self.title_index

    @property
    def naturalTitleIndex(self) -> dict[str, list[str]]:
        return self.natural_title_index

    @property
    def storyStageIndex(self) -> dict[str, list[str]]:
        return self.story_stage_index

    @property
    def storyStageCodeIndex(self) -> dict[str, list[str]]:
        return self.story_stage_code_index

    @property
    def operatorRecordIndex(self) -> dict[str, list[str]]:
        return self.operator_record_index

    @property
    def characterMaterialIndex(self) -> dict[str, list[str]]:
        return self.character_material_index

    @property
    def pathIndex(self) -> dict[str, str]:
        return self.path_index

    @property
    def sourceStoryIndex(self) -> dict[str, str]:
        return self.source_story_index

    @property
    def searchIndexDocuments(self) -> dict[str, str]:
        return self.search_index_documents

    @property
    def content_cache_bytes(self) -> int:
        return self._max_content_cache_bytes

    @content_cache_bytes.setter
    def content_cache_bytes(self, value: int) -> None:
        self._max_content_cache_bytes = value
        self._prune_shard_cache()

    @property
    def index_cache_bytes(self) -> int:
        return self._max_index_cache_bytes

    @index_cache_bytes.setter
    def index_cache_bytes(self, value: int) -> None:
        self._max_index_cache_bytes = value
        self._prune_search_cache()

    @property
    def has_trigram_index(self) -> bool:
        return self.supports_ngram_size(3)

    @property
    def hasTrigramIndex(self) -> bool:
        return self.has_trigram_index

    def cache_stats(self) -> dict[str, int]:
        """返回缓存统计信息。"""
        return {
            "content_bytes": self._shard_cache_bytes,
            "index_bytes": self._search_cache_bytes,
            "entries": len(self._shard_cache) + len(self._search_cache),
        }

    def reset(self) -> None:
        """丢弃全部已解析状态与缓存。"""
        self._generation += 1
        self._loaded = False
        self.release_id = None
        self.data_version = None
        self.documents.clear()
        self.document_order.clear()
        self.prefix_index.clear()
        self.uid_index.clear()
        self.title_index.clear()
        self.natural_title_index.clear()
        self.story_stage_index.clear()
        self.story_stage_code_index.clear()
        self.operator_record_index.clear()
        self.character_material_index.clear()
        self.path_index.clear()
        self.source_story_index.clear()
        self.packs.clear()
        self.search_index_documents.clear()
        self.unstable_chars.clear()

        self._shard_cache.clear()
        self._shard_cache_sizes.clear()
        self._shard_cache_bytes = 0

        self._search_cache.clear()
        self._search_cache_sizes.clear()
        self._search_cache_bytes = 0

        self._short_literal_cache.clear()
        self._short_literal_cache_candidate_count = 0

    async def ready(self) -> None:
        """初始化（幂等）：验证 release、读轻量目录建索引。"""
        if self._loaded:
            return
        async with self._init_lock:
            if self._loaded:
                return
            while True:
                gen = self._generation
                if self.ensure:
                    if asyncio.iscoroutinefunction(self.ensure):
                        await self.ensure()
                    else:
                        res = self.ensure()
                        if asyncio.iscoroutine(res):
                            await res
                committed = await self._init(gen)
                if committed:
                    break

    async def _init(self, generation: int) -> bool:
        try:
            from . import installer
        except ImportError:
            raise InstallerFault("MISSING_INSTALLER", "installer module is not available")

        current = await installer.read_current_release_pointer(self.releases_dir)
        release_id = current.get("release_id") or current.get("releaseId")
        if not release_id:
            raise InstallerFault("INVALID_RELEASE_POINTER", "CorpusStore: release_id is missing in current pointer")

        validated = await installer.validate_local_release(
            self.releases_dir, release_id, verify_hashes=True, details=True
        )

        if isinstance(validated, dict):
            release_manifest = validated.get("manifest") or {}
            pack_manifests = validated.get("pack_manifests") or validated.get("packManifests") or {}
            release_dir = validated.get("release_dir") or validated.get("releaseDir") or os.path.join(self.releases_dir, release_id)
        elif isinstance(validated, (tuple, list)):
            release_manifest = validated[0]
            pack_manifests = validated[1]
            release_dir = validated[2] if len(validated) > 2 else os.path.join(self.releases_dir, release_id)
        else:
            raise InstallerFault("INVALID_VALIDATION_RESULT", "CorpusStore: unexpected validate_local_release result")

        if isinstance(pack_manifests, list):
            pack_manifests = {p.get("pack_id") or p.get("name"): p for p in pack_manifests}

        current_data_version = current.get("data_version") or current.get("dataVersion")
        manifest_data_version = release_manifest.get("data_version") or release_manifest.get("dataVersion")
        if current_data_version is not None and current_data_version != manifest_data_version:
            raise ValueError("CorpusStore: current.json data_version does not match release-manifest")

        next_data = {
            "release_id": release_id,
            "data_version": manifest_data_version,
            "documents": {},
            "document_order": [],
            "prefix_index": {},
            "uid_index": {},
            "title_index": {},
            "natural_title_index": {},
            "story_stage_index": {},
            "story_stage_code_index": {},
            "operator_record_index": {},
            "character_material_index": {},
            "path_index": {},
            "source_story_index": {},
            "packs": {},
            "search_index_documents": {},
            "unstable_chars": {},
        }
        stable_chars = set()

        raw_packs = release_manifest.get("packs") or []
        entries = [{"name": p.get("pack_id") or p.get("name"), "manifestPath": p.get("manifest_path")} for p in raw_packs]
        entries.sort(key=pack_sort_key)

        for entry in entries:
            pack_id = entry["name"]
            trusted_manifest = pack_manifests.get(pack_id)
            if not trusted_manifest:
                raise ValueError(f"CorpusStore: missing validated pack {pack_id}")

            manifest = dict(trusted_manifest)
            manifest["pack_id"] = pack_id
            manifest["packId"] = pack_id
            next_data["packs"][pack_id] = manifest

            catalog = manifest.get("document_catalog")
            sources = [catalog] if catalog else (manifest.get("shards") or [])
            body_shards = {s["path"]: s for s in (manifest.get("shards") or [])}
            seen_locations = set()

            for shard in sources:
                raw_bytes = await self._read_packed(pack_id, shard["path"], release_id, shard)
                records = await asyncio.to_thread(_decode_shard, raw_bytes)
                if catalog and len(records) != int(manifest.get("document_count", 0)):
                    raise ValueError(f"CorpusStore: document catalog count mismatch: {pack_id}")
                await asyncio.sleep(0)

                for index, record in enumerate(records):
                    shard_path = str(record.get("shard_path") or "") if catalog else shard["path"]
                    record_index = record.get("record_index") if catalog else index
                    body_shard = body_shards.get(shard_path)
                    if (
                        not body_shard
                        or not isinstance(record_index, int)
                        or isinstance(record_index, bool)
                        or record_index < 0
                        or record_index >= int(body_shard.get("document_count", 0))
                        or not record.get("document")
                        or not isinstance(record["document"], dict)
                        or not isinstance(record.get("speakers"), list)
                    ):
                        raise ValueError(f"CorpusStore: invalid document catalog row: {pack_id}#{index}")

                    loc_key = f"{shard_path}\0{record_index}"
                    if loc_key in seen_locations:
                        raise ValueError(f"CorpusStore: duplicate document catalog location: {pack_id}#{index}")
                    seen_locations.add(loc_key)

                    doc = record["document"]
                    document_id = doc.get("document_id")
                    prefix = doc.get("source_ref_prefix")
                    ordinal = len(next_data["document_order"])
                    next_data["document_order"].append(document_id)
                    next_data["documents"][document_id] = {
                        "pack_id": pack_id,
                        "packId": pack_id,
                        "shard_path": shard_path,
                        "shardPath": shard_path,
                        "index": record_index,
                        "document": doc,
                        "speakers": record.get("speakers") or [],
                        "search_index_id": record.get("search_index_id"),
                        "searchIndexId": record.get("search_index_id"),
                        "ordinal": ordinal,
                    }

                    if prefix:
                        next_data["prefix_index"][prefix] = document_id

                    uid = document_uid(document_id)
                    existing_uid = next_data["uid_index"].get(uid)
                    if existing_uid and existing_uid != document_id:
                        raise ValueError(f"CorpusStore: document_uid collision: {uid}")
                    next_data["uid_index"][uid] = document_id

                    title = doc.get("display_title")
                    if title:
                        next_data["title_index"].setdefault(title, []).append(document_id)

                    natural_title = natural_document_title(doc)
                    if natural_title:
                        next_data["natural_title_index"].setdefault(natural_title, []).append(document_id)
                        if doc.get("game"):
                            legacy_title = natural_document_title_base(doc)
                            if legacy_title and legacy_title != natural_title:
                                next_data["natural_title_index"].setdefault(legacy_title, []).append(document_id)

                    legacy_memory = legacy_operator_record_title(doc)
                    if legacy_memory and legacy_memory != natural_title:
                        aliases = [legacy_memory]
                        if doc.get("game"):
                            aliases.append(natural_document_title_base(doc, include_operator_record_segment=False))
                        for alias in aliases:
                            if alias:
                                next_data["natural_title_index"].setdefault(alias, []).append(document_id)

                    stage_code = public_story_stage_code(doc.get("story_code"))
                    story_part = public_story_part(doc.get("part_type"))
                    if (
                        document_game(doc) == "arknights"
                        and doc.get("document_type") == "story"
                        and doc.get("document_kind") == "story"
                        and stage_code
                        and story_part
                    ):
                        k = story_stage_key(stage_code, story_part)
                        next_data["story_stage_index"].setdefault(k, []).append(document_id)
                        next_data["story_stage_code_index"].setdefault(stage_code, []).append(document_id)

                    rec_segment = operator_record_segment(doc)
                    if rec_segment and doc.get("document_kind") == "story" and doc.get("part_type") == "body":
                        k1 = operator_record_key(doc.get("character_name"), doc.get("story_name"), rec_segment)
                        next_data["operator_record_index"].setdefault(k1, []).append(document_id)
                        k2 = operator_record_key(doc.get("character_name"), doc.get("story_name"))
                        next_data["operator_record_index"].setdefault(k2, []).append(document_id)

                    material = public_character_material(doc)
                    if material:
                        k = character_material_key(document_game(doc), doc.get("character_name"), material)
                        next_data["character_material_index"].setdefault(k, []).append(document_id)

                    path = doc.get("path")
                    if path and path not in next_data["path_index"]:
                        next_data["path_index"][path] = document_id

                    source_story_id = doc.get("source_story_id")
                    if source_story_id and source_story_id not in next_data["source_story_index"]:
                        next_data["source_story_index"][source_story_id] = document_id

                    search_idx = record.get("search_index_id")
                    if search_idx is not None:
                        next_data["search_index_documents"][f"{pack_id}\0{search_idx}"] = document_id

                    for line in record.get("lines") or []:
                        collect_unstable_chars(
                            line.get("text") if isinstance(line, dict) else "",
                            stable_chars,
                            next_data["unstable_chars"],
                        )

            if len(seen_locations) != int(manifest.get("document_count", 0)):
                raise ValueError(f"CorpusStore: document directory count mismatch: {pack_id}")

        if not next_data["documents"]:
            raise ValueError(f"CorpusStore: no documents found under {release_dir}")

        next_data["document_order"] = federated_order(next_data["documents"], next_data["document_order"])
        for ordinal, doc_id in enumerate(next_data["document_order"]):
            next_data["documents"][doc_id]["ordinal"] = ordinal

        if generation != self._generation:
            return False

        self.release_id = next_data["release_id"]
        self.data_version = next_data["data_version"]
        self.documents = next_data["documents"]
        self.document_order = next_data["document_order"]
        self.prefix_index = next_data["prefix_index"]
        self.uid_index = next_data["uid_index"]
        self.title_index = next_data["title_index"]
        self.natural_title_index = next_data["natural_title_index"]
        self.story_stage_index = next_data["story_stage_index"]
        self.story_stage_code_index = next_data["story_stage_code_index"]
        self.operator_record_index = next_data["operator_record_index"]
        self.character_material_index = next_data["character_material_index"]
        self.path_index = next_data["path_index"]
        self.source_story_index = next_data["source_story_index"]
        self.packs = next_data["packs"]
        self.search_index_documents = next_data["search_index_documents"]
        self.unstable_chars = next_data["unstable_chars"]

        self._shard_cache.clear()
        self._shard_cache_sizes.clear()
        self._shard_cache_bytes = 0

        self._search_cache.clear()
        self._search_cache_sizes.clear()
        self._search_cache_bytes = 0

        self._short_literal_cache.clear()
        self._short_literal_cache_candidate_count = 0
        self._loaded = True
        return True

    async def _read_packed(
        self,
        pack_id: str,
        shard_path: str,
        release_id: str | None = None,
        descriptor: dict | None = None,
    ) -> bytes:
        rid = release_id or self.release_id or ""
        base_dir = os.path.join(self.releases_dir, rid, pack_id)
        if descriptor is None:
            pack = self.packs.get(pack_id) or {}
            shards = pack.get("shards") or []
            descriptor = next((s for s in shards if s.get("path") == shard_path), None)
        safe_path = validate_safe_relative_path(base_dir, shard_path)
        return await asyncio.to_thread(_gunzip_bounded_sync, safe_path, descriptor)

    async def _load_shard(self, pack_id: str, shard_path: str) -> list[dict]:
        snapshot = corpus_version_snapshot(self)
        key = f"{pack_id}\0{shard_path}"
        async with self._cache_lock:
            if key in self._shard_cache:
                cached = self._shard_cache.pop(key)
                self._shard_cache[key] = cached
                return cached
        pack = self.packs.get(pack_id) or {}
        shards = pack.get("shards") or []
        descriptor = next((item for item in shards if item.get("path") == shard_path), None)
        try:
            plain = await self._read_packed(pack_id, shard_path, self.release_id, descriptor)
        finally:
            assert_corpus_version(self, snapshot)
        records = await asyncio.to_thread(_decode_shard, plain)
        async with self._cache_lock:
            assert_corpus_version(self, snapshot)
            self._remember_shard(key, records, len(plain))
        return records

    async def _load_search_shard(self, pack_id: str, shard_path: str) -> bytes:
        snapshot = corpus_version_snapshot(self)
        key = f"{pack_id}\0{shard_path}"
        async with self._cache_lock:
            if key in self._search_cache:
                cached = self._search_cache.pop(key)
                self._search_cache[key] = cached
                return cached
        pack = self.packs.get(pack_id) or {}
        search_index = pack.get("search_index") or {}
        shards = search_index.get("shards") or []
        descriptor = next((item for item in shards if item.get("path") == shard_path), None)
        base_dir = os.path.join(self.releases_dir, self.release_id or "", pack_id)
        safe_path = validate_safe_relative_path(base_dir, shard_path)
        try:
            bytes_data = await asyncio.to_thread(_gunzip_bounded_sync, safe_path, descriptor)
        finally:
            assert_corpus_version(self, snapshot)
        async with self._cache_lock:
            assert_corpus_version(self, snapshot)
            self._remember_search_shard(key, bytes_data)
        return bytes_data

    def _remember_shard(self, key: str, records: list[dict], byte_length: int) -> None:
        if key in self._shard_cache:
            prev = self._shard_cache_sizes.pop(key, 0)
            self._shard_cache.pop(key, None)
            self._shard_cache_bytes -= prev
        self._shard_cache[key] = records
        self._shard_cache_sizes[key] = byte_length
        self._shard_cache_bytes += byte_length
        self._prune_shard_cache()

    def _prune_shard_cache(self) -> None:
        while self._shard_cache and (
            len(self._shard_cache) > self.cache_shards
            or self._shard_cache_bytes > self._max_content_cache_bytes
        ):
            oldest = next(iter(self._shard_cache))
            self._shard_cache.pop(oldest)
            size = self._shard_cache_sizes.pop(oldest, 0)
            self._shard_cache_bytes -= size

    def _remember_search_shard(self, key: str, data: bytes) -> None:
        if key in self._search_cache:
            prev = self._search_cache_sizes.pop(key, 0)
            self._search_cache.pop(key, None)
            self._search_cache_bytes -= prev
        self._search_cache[key] = data
        self._search_cache_sizes[key] = len(data)
        self._search_cache_bytes += len(data)
        self._prune_search_cache()

    def _prune_search_cache(self) -> None:
        while self._search_cache and (
            len(self._search_cache) > self.search_cache_shards
            or self._search_cache_bytes > self._max_index_cache_bytes
        ):
            oldest = next(iter(self._search_cache))
            self._search_cache.pop(oldest)
            size = self._search_cache_sizes.pop(oldest, 0)
            self._search_cache_bytes -= size

    async def get_document(self, document_id: str) -> dict | None:
        """按 document_id 取完整文档记录，并重新比对 local_integrity。"""
        snapshot = corpus_version_snapshot(self)
        location = self.documents.get(document_id)
        if not location:
            return None
        pack_id = location.get("pack_id") or location.get("packId")
        shard_path = location.get("shard_path") or location.get("shardPath")
        records = await self._load_shard(pack_id, shard_path)
        assert_corpus_version(self, snapshot)

        record_idx = location["index"]
        if record_idx >= len(records):
            return None
        record = records[record_idx]
        if not record or (record.get("document") or {}).get("document_id") != document_id:
            return None

        lines = record.get("lines") or []
        actual_integrity = compute_lines_integrity(lines)
        expected_integrity = (record.get("local_integrity") or {}).get("sha256")
        if expected_integrity != actual_integrity:
            raise ContractError(
                "INDEX_CORRUPT",
                f"integrity mismatch for {document_id}: expected {expected_integrity}, got {actual_integrity}",
            )

        return {
            "record": record,
            "pack_id": pack_id,
            "packId": pack_id,
        }

    getDocument = get_document

    def get_document_id_by_prefix(self, prefix: str) -> str | None:
        """source_ref_prefix → document_id。未知返回 None。"""
        return self.prefix_index.get(str(prefix or "")) or None

    getDocumentIdByPrefix = get_document_id_by_prefix

    def document_ordinal(self, document_id: str) -> int | None:
        """当前不可变资料版本中的稳定全局文档序号（0-based）。"""
        item = self.documents.get(str(document_id or ""))
        return item.get("ordinal") if item else None

    documentOrdinal = document_ordinal

    def ordered_document_ids(self, document_ids: list[str] | None = None) -> list[str]:
        """将候选文档恢复成全局稳定顺序；未知 ID 被忽略。"""
        if document_ids is None:
            return list(self.document_order)
        requested = set(document_ids)
        return [did for did in self.document_order if did in requested]

    orderedDocumentIds = ordered_document_ids

    def get_document_id_by_uid(self, uid: str) -> str:
        """短 document_uid → canonical document_id。缺失时返回空字符串。"""
        return self.uid_index.get(str(uid or "").strip()) or ""

    getDocumentIdByUid = get_document_id_by_uid

    async def get_document_by_uid(self, uid: str) -> dict | None:
        """按短 document_uid 取完整文档记录。"""
        doc_id = self.get_document_id_by_uid(uid)
        return await self.get_document(doc_id) if doc_id else None

    getDocumentByUid = get_document_by_uid

    async def get_document_by_source_story_id(self, source_story_id: str) -> dict | None:
        """按 GameData 原始 source_story_id 取文档记录（synopsis → 可读全文的桥）。"""
        doc_id = self.source_story_index.get(str(source_story_id or ""))
        return await self.get_document(doc_id) if doc_id else None

    getDocumentBySourceStoryId = get_document_by_source_story_id

    async def get_document_by_path(self, path: str) -> dict | None:
        """按资料内路径取文档记录。"""
        doc_id = self.path_index.get(str(path or ""))
        return await self.get_document(doc_id) if doc_id else None

    getDocumentByPath = get_document_by_path

    async def get_or_create_cursor_secret(self) -> bytes:
        """获取跨重启持久的游标签名密钥（32 字节）。"""
        if self._cursor_secret is not None:
            return self._cursor_secret
        async with self._secret_lock:
            if self._cursor_secret is not None:
                return self._cursor_secret

            def _read_or_create():
                path = self.cursor_secret_path
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        data = f.read()
                        if len(data) == 32:
                            return data
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                created = secrets.token_bytes(32)
                try:
                    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    if hasattr(os, "O_BINARY"):
                        flags |= os.O_BINARY
                    fd = os.open(path, flags, 0o600)
                    try:
                        with os.fdopen(fd, "wb") as f:
                            f.write(created)
                    except Exception:
                        os.close(fd)
                        raise
                    return created
                except FileExistsError:
                    with open(path, "rb") as f:
                        existing = f.read()
                        if len(existing) != 32:
                            raise ValueError("CorpusStore: invalid cursor secret")
                        return existing

            self._cursor_secret = await asyncio.to_thread(_read_or_create)
            return self._cursor_secret

    getOrCreateCursorSecret = get_or_create_cursor_secret

    async def get_document_by_title(self, title: str) -> dict | None:
        """按自然语言完整篇章标题或 display_title 取文档；歧义时抛出 DOCUMENT_AMBIGUOUS。"""
        normalized = str(title or "").strip()
        natural_ids = self.natural_title_index.get(normalized, [])
        document_ids = natural_ids if natural_ids else self.title_index.get(normalized, [])
        if len(document_ids) > 1:
            locations = [self.documents.get(did) for did in document_ids]
            locations = [loc for loc in locations if loc is not None]
            if len(locations) == len(document_ids) and all(
                loc["document"].get("document_type") == "entity" for loc in locations
            ):
                preferred = sorted(
                    locations,
                    key=lambda loc: (
                        -int(loc["document"].get("line_count") or 0),
                        str(loc["document"].get("document_id") or ""),
                    ),
                )[0]
                return await self.get_document(preferred["document"]["document_id"])
            raise ContractError(
                "DOCUMENT_AMBIGUOUS",
                f"标题“{normalized}”对应 {len(document_ids)} 篇资料；请使用 prts_search 返回的带资料类型完整标题",
            )
        return await self.get_document(document_ids[0]) if document_ids else None

    getDocumentByTitle = get_document_by_title

    def requires_document_uid(self, document_id: str) -> bool:
        """搜索结果若无法仅靠自然标题/合集名稳定回读，就公开短 document_uid。"""
        location = self.documents.get(str(document_id or ""))
        doc = location.get("document") if location else None
        if not doc:
            return False
        title = natural_document_title(doc)
        if len(self.natural_title_index.get(title, [])) > 1:
            return True
        game = document_game(doc)
        stream_name = normalized_lookup_text(
            doc.get("collection_name") if game == "endfield" else doc.get("activity_name")
        )
        if not stream_name or doc.get("document_type") != "story":
            return False
        collections = set()
        for item in self.documents.values():
            cand = item.get("document") or {}
            if document_game(cand) != game or cand.get("document_type") != "story":
                continue
            cand_name = normalized_lookup_text(
                cand.get("collection_name") if game == "endfield" else cand.get("activity_name")
            )
            if cand_name != stream_name:
                continue
            collections.add(str(cand.get("collection_id") or cand.get("activity_id") or ""))
            if len(collections) > 1:
                return True
        return False

    requiresDocumentUid = requires_document_uid

    async def get_document_by_story_stage(self, stage_code: str, story_part: str = "") -> dict | None:
        """按玩家可见关卡代号定位；省略部分时仅在全局唯一的情况下成功。"""
        normalized_code = public_story_stage_code(stage_code)
        part_supplied = story_part != "" and story_part is not None
        normalized_part = public_story_part(story_part) if part_supplied else ""
        if not normalized_code or (part_supplied and not normalized_part):
            return None
        document_ids = (
            self.story_stage_index.get(story_stage_key(normalized_code, normalized_part), [])
            if normalized_part
            else self.story_stage_code_index.get(normalized_code, [])
        )
        if len(document_ids) > 1:
            label_map = {"before": "行动前", "after": "行动后", "story": "剧情"}
            choices = []
            for did in document_ids:
                d = (self.documents.get(did) or {}).get("document") or {}
                part = public_story_part(d.get("part_type"))
                lbl = label_map.get(part, part)
                choices.append(f"{lbl}《{natural_document_title(d)}》")
            choices_str = "、".join(choices)
            req_lbl = f" 的{label_map.get(normalized_part, normalized_part)}" if normalized_part else ""
            raise ContractError(
                "DOCUMENT_AMBIGUOUS",
                f"关卡 {normalized_code}{req_lbl}对应 {len(document_ids)} 篇资料：{choices_str}；请明确 story_part",
            )
        return await self.get_document(document_ids[0]) if document_ids else None

    getDocumentByStoryStage = get_document_by_story_stage

    def has_unique_story_stage(self, document_id: str) -> bool:
        """该文档能否用公开 stage_code + story_part 无歧义地再次定位。"""
        item = self.documents.get(str(document_id or ""))
        doc = item.get("document") if item else None
        if not doc or document_game(doc) != "arknights":
            return False
        stage_code = public_story_stage_code(doc.get("story_code"))
        story_part = public_story_part(doc.get("part_type"))
        if not stage_code or not story_part:
            return False
        ids = self.story_stage_index.get(story_stage_key(stage_code, story_part), [])
        return len(ids) == 1 and ids[0] == doc.get("document_id")

    hasUniqueStoryStage = has_unique_story_stage

    async def get_operator_record(self, character_name: str, record_name: str, segment=None) -> dict | None:
        """按角色、密录名和可选段号读取密录正文。"""
        norm_char = normalized_lookup_text(character_name)
        norm_record = normalized_lookup_text(record_name)
        if segment is None or segment == "":
            norm_seg = ""
        else:
            try:
                seg_int = int(segment)
                if seg_int < 1:
                    return None
                norm_seg = str(seg_int)
            except (ValueError, TypeError):
                return None

        if not norm_char or not norm_record:
            return None

        ids = self.operator_record_index.get(operator_record_key(norm_char, norm_record, norm_seg), [])
        if len(ids) > 1:
            seg_list = []
            for i in ids:
                d = (self.documents.get(i) or {}).get("document") or {}
                s = operator_record_segment(d)
                if s:
                    try:
                        seg_list.append(int(s))
                    except ValueError:
                        pass
            segs = sorted(set(seg_list))
            seg_str = f"（{'、'.join(str(s) for s in segs)}）" if segs else ""
            raise ContractError(
                "DOCUMENT_AMBIGUOUS",
                f"干员“{norm_char}”的密录“{norm_record}”包含 {len(ids)} 段正文{seg_str}；请提供 segment",
            )
        return await self.get_document(ids[0]) if ids else None

    getOperatorRecord = get_operator_record

    def has_unique_operator_record(self, document_id: str) -> bool:
        item = self.documents.get(str(document_id or ""))
        doc = item.get("document") if item else None
        segment = operator_record_segment(doc or {})
        if not segment or doc.get("document_kind") != "story" or doc.get("part_type") != "body":
            return False
        ids = self.operator_record_index.get(
            operator_record_key(doc.get("character_name"), doc.get("story_name"), segment), []
        )
        return len(ids) == 1 and ids[0] == doc.get("document_id")

    hasUniqueOperatorRecord = has_unique_operator_record

    async def get_character_material(
        self, character_name: str, material: str, games: list[str] | None = None
    ) -> dict | None:
        """按玩家可见角色名与资料类别定位官方角色资料。"""
        if games is None:
            games = ["arknights", "endfield"]
        ids = []
        for game in games:
            k = character_material_key(game, character_name, material)
            ids.extend(self.character_material_index.get(k, []))
        if len(ids) > 1:
            docs = [(self.documents.get(i) or {}).get("document") for i in ids]
            docs = [d for d in docs if d is not None]
            hashes = set(d.get("text_sha256") for d in docs if d.get("text_sha256"))
            if len(hashes) == 1 and len(docs) == len(ids):
                preferred_id = sorted(ids)[0]
                return await self.get_document(preferred_id)
            titles = [natural_document_title(d) for d in docs]
            titles_str = "、".join(titles)
            norm_char = normalized_lookup_text(character_name)
            raise ContractError(
                "DOCUMENT_AMBIGUOUS",
                f"角色“{norm_char}”的 {material} 资料对应 {len(ids)} 篇不同内容：{titles_str}",
            )
        return await self.get_document(ids[0]) if ids else None

    getCharacterMaterial = get_character_material

    def has_unique_character_material(self, document_id: str) -> bool:
        item = self.documents.get(str(document_id or ""))
        doc = item.get("document") if item else None
        material = public_character_material(doc or {})
        if not material:
            return False
        ids = self.character_material_index.get(
            character_material_key(document_game(doc), doc.get("character_name"), material), []
        )
        if len(ids) == 1:
            return ids[0] == doc.get("document_id")
        docs = [(self.documents.get(i) or {}).get("document") for i in ids]
        docs = [d for d in docs if d is not None]
        hashes = set(d.get("text_sha256") for d in docs if d.get("text_sha256"))
        preferred = sorted(ids)[0]
        return len(docs) == len(ids) and len(hashes) == 1 and preferred == doc.get("document_id")

    hasUniqueCharacterMaterial = has_unique_character_material

    def is_preferred_natural_document(self, document_id: str) -> bool:
        """同名实体投影只让内容最完整的一份进入模型搜索结果。"""
        location = self.documents.get(str(document_id or ""))
        if not location or location["document"].get("document_type") != "entity":
            return True
        title = natural_document_title(location["document"])
        ids = self.natural_title_index.get(title, [])
        if len(ids) <= 1:
            return True
        entity_locations = [self.documents.get(i) for i in ids]
        entity_locations = [loc for loc in entity_locations if loc is not None]
        if len(entity_locations) != len(ids) or any(
            loc["document"].get("document_type") != "entity" for loc in entity_locations
        ):
            return True
        entity_locations.sort(
            key=lambda loc: (
                -int(loc["document"].get("line_count") or 0),
                str(loc["document"].get("document_id") or ""),
            )
        )
        return entity_locations[0]["document"].get("document_id") == location["document"].get("document_id")

    isPreferredNaturalDocument = is_preferred_natural_document

    def _search_pack_entries(self, pack_ids: list[str] | None = None) -> list[tuple[str, dict]] | None:
        if pack_ids is None:
            return list(self.packs.items())
        if not isinstance(pack_ids, list):
            return None
        entries = []
        seen = set()
        for raw_id in pack_ids:
            pid = str(raw_id or "")
            if not pid or pid in seen:
                continue
            manifest = self.packs.get(pid)
            if not manifest:
                return None
            seen.add(pid)
            entries.append((pid, manifest))
        return entries

    def supports_ngram_size(self, size: int, pack_ids: list[str] | None = None) -> bool:
        """指定范围内的 pack 都支持该 gram 宽度时返回 True。"""
        if not isinstance(size, int) or isinstance(size, bool) or size < 1 or size > 3 or not self.packs:
            return False
        entries = self._search_pack_entries(pack_ids)
        if not entries:
            return False
        for _, manifest in entries:
            index = manifest.get("search_index") or {}
            shards = index.get("shards") or []
            if not shards:
                return False
            algo = index.get("algorithm")
            sizes = index.get("gram_sizes") if algo == "prts-browser-ngram-postings-v2" else [3]
            if not isinstance(sizes, list) or size not in sizes:
                return False
        return True

    supportsNgramSize = supports_ngram_size

    def _assert_scan_active(self, signal=None, deadline: float = float("inf"), generation: int | None = None) -> None:
        if signal is not None and getattr(signal, "aborted", False):
            raise ContractError("CANCELLED", "短字面量候选扫描已取消")
        if deadline != float("inf"):
            now = time.time() * 1000 if deadline >= 1e11 else time.time()
            if now >= deadline:
                raise ContractError("TIMEOUT", "短字面量候选扫描超时", retryable=True)
        if generation is not None and generation != self._generation:
            raise ContractError(
                "PACKAGE_VERSION_MISMATCH", "资料版本在短字面量候选扫描期间发生变化，请重试", retryable=True
            )

    async def find_documents_by_ngrams(
        self,
        trigrams: list[str],
        signal=None,
        deadline: float = float("inf"),
        pack_ids: list[str] | None = None,
    ) -> list[str] | None:
        """使用各 pack 的 ngram 倒排分片求文档交集。"""
        generation = self._generation
        if not trigrams:
            return None
        entries = self._search_pack_entries(pack_ids)
        if entries is None:
            return None
        if not entries:
            return []
        gram_size = len(str(trigrams[0]))
        if not self.supports_ngram_size(gram_size, [p[0] for p in entries]):
            return None

        self._assert_scan_active(signal, deadline, generation)
        per_trigram = {tg: set() for tg in trigrams}

        for pack_id, manifest in entries:
            shards = (manifest.get("search_index") or {}).get("shards") or []
            for descriptor in shards:
                self._assert_scan_active(signal, deadline, generation)
                rng = search_index_range(descriptor)
                relevant = [
                    tg
                    for tg in trigrams
                    if compare_ngram_keys(rng["first"], tg) <= 0 and compare_ngram_keys(tg, rng["last"]) <= 0
                ]
                if not relevant:
                    continue
                bytes_data = await self._load_search_shard(pack_id, descriptor["path"])
                self._assert_scan_active(signal, deadline, generation)
                for tg in relevant:
                    candidates = per_trigram[tg]
                    indexes = lookup_ngram_index(bytes_data, tg)
                    for offset, idx in enumerate(indexes):
                        if (offset & 1023) == 0:
                            self._assert_scan_active(signal, deadline, generation)
                        doc_id = self.search_index_documents.get(f"{pack_id}\0{idx}")
                        if doc_id:
                            candidates.add(doc_id)

        intersection = None
        for tg in trigrams:
            self._assert_scan_active(signal, deadline, generation)
            candidates = per_trigram[tg]
            if intersection is None:
                intersection = set(candidates)
            else:
                intersection &= candidates
            if not intersection:
                break

        self._assert_scan_active(signal, deadline, generation)
        return list(intersection or [])

    findDocumentsByNgrams = find_documents_by_ngrams

    async def find_documents_by_trigrams(self, trigrams: list[str], runtime=None, **kwargs) -> list[str] | None:
        """v1 API alias；兼容三元倒排查询。"""
        if runtime is None:
            runtime = {}
        if isinstance(runtime, dict):
            signal = runtime.get("signal", kwargs.get("signal"))
            deadline = runtime.get("deadline", kwargs.get("deadline", float("inf")))
            pack_ids = runtime.get("pack_ids", kwargs.get("pack_ids", runtime.get("packIds", kwargs.get("packIds"))))
        else:
            signal = kwargs.get("signal")
            deadline = kwargs.get("deadline", float("inf"))
            pack_ids = kwargs.get("pack_ids", kwargs.get("packIds"))
        return await self.find_documents_by_ngrams(trigrams, signal=signal, deadline=deadline, pack_ids=pack_ids)

    findDocumentsByTrigrams = find_documents_by_trigrams

    def _short_literal_cache_hit(self, query: str) -> list[str] | None:
        if query not in self._short_literal_cache:
            return None
        cached = self._short_literal_cache.pop(query)
        self._short_literal_cache[query] = cached
        return list(cached)

    def _remember_short_literal_candidates(self, query: str, candidates: list[str]) -> None:
        prev = self._short_literal_cache.pop(query, None)
        if prev:
            self._short_literal_cache_candidate_count -= len(prev)
        if len(candidates) > MAX_SHORT_LITERAL_CACHE_CANDIDATES:
            return
        self._short_literal_cache[query] = candidates
        self._short_literal_cache_candidate_count += len(candidates)
        while (
            len(self._short_literal_cache) > 32
            or self._short_literal_cache_candidate_count > MAX_SHORT_LITERAL_CACHE_CANDIDATES
        ):
            oldest = next(iter(self._short_literal_cache))
            evicted = self._short_literal_cache.pop(oldest, None)
            self._short_literal_cache_candidate_count -= len(evicted) if evicted else 0

    async def find_documents_by_short_literal(
        self,
        value: str,
        signal=None,
        deadline: float = float("inf"),
        pack_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[str] | None:
        """为 1—2 个无大小写字符的字面量提供 grep 式候选预筛。

        document_ids 给定时只扫描包含这些文档的分片：调用方随后会把结果与候选集
        求交，语义不变，但冷启动扫描范围可缩小到少量分片。
        """
        query = str(value if value is not None else "")
        characters = list(query)
        if (
            not characters
            or len(characters) > 2
            or query != unicodedata.normalize("NFC", query)
            or query != unicodedata.normalize("NFKC", query)
            or query.lower() != query.upper()
            or re.search(r"[\s\"\\\x00-\x1f]", query)
        ):
            return None
        if not self.unstable_chars:
            return None

        entries = self._search_pack_entries(pack_ids)
        if entries is None:
            return None
        if not entries:
            return []

        allowed_shards: set[tuple[str, str]] | None = None
        scope_suffix = ""
        if document_ids is not None:
            allowed_shards = set()
            for document_id in document_ids:
                location = self.documents.get(document_id)
                if not location:
                    continue
                location_pack = str(location.get("pack_id") or "")
                location_shard = str(location.get("shard_path") or "")
                if location_pack and location_shard:
                    allowed_shards.add((location_pack, location_shard))
            if not allowed_shards:
                return []
            fingerprint = "\0".join(sorted(f"{p}:{s}" for p, s in allowed_shards))
            scope_suffix = "\x02" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]

        sorted_pack_ids = sorted(p[0] for p in entries)
        cache_key = f"{chr(0).join(sorted_pack_ids)}\x01{query}{scope_suffix}"

        self._assert_scan_active(signal, deadline, self._generation)
        cached = self._short_literal_cache_hit(cache_key)
        if cached is not None:
            return cached

        async with self._short_literal_scan_lock:
            cached = self._short_literal_cache_hit(cache_key)
            if cached is not None:
                return cached

            generation = self._generation
            self._assert_scan_active(signal, deadline, generation)

            needles = canonical_variants(query)
            first = characters[0]
            last = characters[-1]
            unstable_idx = 0
            for char, expansion in self.unstable_chars.items():
                if (unstable_idx & 255) == 0:
                    self._assert_scan_active(signal, deadline, generation)
                unstable_idx += 1
                if query in expansion or expansion.endswith(first) or expansion.startswith(last):
                    needles.add(char)

            needle_strings = list(needles)
            needle_buffers = [n.encode("utf-8") for n in needle_strings]

            jobs = []
            for pack_id, manifest in entries:
                self._assert_scan_active(signal, deadline, generation)
                for descriptor in manifest.get("shards") or []:
                    if allowed_shards is not None and (
                        pack_id,
                        str(descriptor.get("path") or ""),
                    ) not in allowed_shards:
                        continue
                    jobs.append({"pack_id": pack_id, "descriptor": descriptor})

            found = []
            for job in jobs:
                self._assert_scan_active(signal, deadline, generation)
                bytes_data = await self._read_packed(
                    job["pack_id"], job["descriptor"]["path"], self.release_id, job["descriptor"]
                )
                self._assert_scan_active(signal, deadline, generation)

                possible_match = False
                for nb in needle_buffers:
                    if nb in bytes_data:
                        possible_match = True
                        break
                if not possible_match:
                    continue

                await asyncio.sleep(0)
                self._assert_scan_active(signal, deadline, generation)

                records = await asyncio.to_thread(_decode_shard, bytes_data)
                key = f"{job['pack_id']}\0{job['descriptor']['path']}"
                self._remember_shard(key, records, len(bytes_data))

                for r_idx, record in enumerate(records):
                    if (r_idx & 31) == 0:
                        self._assert_scan_active(signal, deadline, generation)
                    doc = record.get("document") or {}
                    title = False
                    for item in (
                        doc.get("display_title"),
                        doc.get("story_name"),
                        doc.get("activity_name"),
                        doc.get("character_name"),
                    ):
                        if item is not None and any(n in str(item) for n in needle_strings):
                            title = True
                            break

                    content = False
                    if not title:
                        for line in record.get("lines") or []:
                            txt = line.get("text") if isinstance(line, dict) else ""
                            if txt and any(n in txt for n in needle_strings):
                                content = True
                                break

                    if title or content:
                        doc_id = doc.get("document_id")
                        if doc_id:
                            found.append(doc_id)

            self._assert_scan_active(signal, deadline, generation)
            found.sort(key=lambda did: self.documents.get(did, {}).get("ordinal", 0))
            self._remember_short_literal_candidates(cache_key, found)
            return list(found)

    findDocumentsByShortLiteral = find_documents_by_short_literal

    async def iterate_documents(self, document_ids: list[str] | None = None, predicate=None):
        """先按轻量元数据过滤，再按需解压并校验正文分片（异步生成器）。"""
        snapshot = corpus_version_snapshot(self)
        ids = self.ordered_document_ids(document_ids)
        for document_id in ids:
            assert_corpus_version(self, snapshot)
            location = self.documents.get(document_id)
            if not location or (predicate and not predicate(location["document"], location.get("speakers", []))):
                continue
            found = await self.get_document(document_id)
            assert_corpus_version(self, snapshot)
            if found:
                yield found["record"]
        assert_corpus_version(self, snapshot)

    iterateDocuments = iterate_documents
    iter_search_documents = iterate_documents

    def activity_story_documents(
        self, activity_id: str = "", activity_name: str = "", anchor_document_id: str = ""
    ) -> list[dict]:
        """枚举某个活动下的全部剧情文档，按 collection_id + sequence_index 排序。"""
        anchor = self.documents.get(str(anchor_document_id), {}).get("document") if anchor_document_id else None
        if anchor_document_id and (
            not anchor
            or document_game(anchor) != "arknights"
            or anchor.get("document_type") != "story"
            or anchor.get("document_category") != "activity"
        ):
            return []

        coll_id = str(
            (anchor.get("collection_id") or anchor.get("activity_id"))
            if anchor
            else (activity_id or "")
        ).strip()
        name = "" if anchor else normalized_lookup_text(activity_name)
        if not coll_id and not name:
            return []

        matches = []
        for location in self.documents.values():
            doc = location.get("document") or {}
            if (
                document_game(doc) != "arknights"
                or doc.get("document_type") != "story"
                or doc.get("document_kind") != "story"
                or doc.get("document_category") != "activity"
            ):
                continue
            source = str(doc.get("source_story_id") or doc.get("document_id") or "")
            if re.search(r"(?:^|/)(?:tutorial(?:_|/)|training/)", source, re.IGNORECASE):
                continue
            by_id = coll_id and (
                str(doc.get("collection_id") or "") == coll_id or str(doc.get("activity_id") or "") == coll_id
            )
            by_name = name and normalized_lookup_text(doc.get("activity_name")) == name
            if not by_id and not by_name:
                continue
            matches.append(
                {
                    "document": doc,
                    "speakers": location.get("speakers") or [],
                    "pack_id": location.get("pack_id"),
                    "packId": location.get("pack_id"),
                }
            )

        if not coll_id:
            collections = {}
            for item in matches:
                key = str(item["document"].get("collection_id") or item["document"].get("activity_id") or "")
                collections.setdefault(key, []).append(item)
            if len(collections) > 1:
                choices = []
                for docs in collections.values():
                    first = sorted(
                        docs,
                        key=lambda x: (
                            int(x["document"].get("sequence_index") or 0)
                            if isinstance(x["document"].get("sequence_index"), int)
                            else 0
                        ),
                    )[0]
                    first_doc = first["document"]
                    choices.append(
                        f"document_uid={document_uid(first_doc.get('document_id'))}（《{natural_document_title(first_doc)}》等 {len(docs)} 篇）"
                    )
                choices_str = "、".join(choices)
                raise ContractError(
                    "DOCUMENT_AMBIGUOUS",
                    f"活动名“{name}”对应 {len(collections)} 个不同剧情合集：{choices_str}；请先检索具体篇章，再用其 document_uid + mode=\"activity\" 通读，不能自动合并",
                )

        matches.sort(
            key=lambda item: (
                natural_sort_key(item["document"].get("collection_id") or ""),
                int(item["document"].get("sequence_index") or 0)
                if isinstance(item["document"].get("sequence_index"), int)
                else 0,
                natural_sort_key(item["document"].get("document_id") or ""),
            )
        )
        return matches

    activityStoryDocuments = activity_story_documents

    def endfield_collection_documents(
        self,
        collection_name: str = "",
        content_types: list[str] | None = None,
        anchor_document_id: str = "",
    ) -> list[dict]:
        """按终末地任务/集合展示名枚举官方剧情碎片；同名多集合时抛出 DOCUMENT_AMBIGUOUS。"""
        anchor = self.documents.get(str(anchor_document_id), {}).get("document") if anchor_document_id else None
        if anchor_document_id and (
            not anchor
            or document_game(anchor) != "endfield"
            or anchor.get("document_type") != "story"
            or anchor.get("resource_type") != "original_story"
        ):
            return []

        name = normalized_lookup_text(anchor.get("collection_name")) if anchor else normalized_lookup_text(collection_name)
        collection_id = str(anchor.get("collection_id") or "") if anchor else ""
        if not name and not collection_id:
            return []

        allowed_types = set(content_types) if content_types else END_FIELD_NARRATIVE_CONTENT_TYPES
        matches = []
        for location in self.documents.values():
            doc = location.get("document") or {}
            if (
                document_game(doc) != "endfield"
                or doc.get("document_type") != "story"
                or doc.get("document_kind") != "story"
                or doc.get("resource_type") != "original_story"
            ):
                continue
            if collection_id:
                if str(doc.get("collection_id") or "") != collection_id:
                    continue
            else:
                if normalized_lookup_text(doc.get("collection_name")) != name:
                    continue
            if str(doc.get("content_type") or "") not in allowed_types:
                continue
            matches.append(
                {
                    "document": doc,
                    "speakers": location.get("speakers") or [],
                    "pack_id": location.get("pack_id"),
                    "packId": location.get("pack_id"),
                }
            )

        collections = {}
        for item in matches:
            key = str(item["document"].get("collection_id") or "")
            collections.setdefault(key, []).append(item)

        if len(collections) > 1:
            choices = []
            for docs in collections.values():
                types = []
                for itm in docs:
                    ct = itm["document"].get("content_type")
                    if ct and ct not in types:
                        types.append(ct)
                first = sorted(
                    docs,
                    key=lambda x: natural_sort_key(
                        x["document"].get("source_story_id") or x["document"].get("display_title") or ""
                    ),
                )[0]
                first_doc = first["document"]
                choices.append(
                    f"document_uid={document_uid(first_doc.get('document_id'))}（{'/'.join(types)} {len(docs)} 篇）"
                )
            choices_str = "、".join(choices)
            raise ContractError(
                "DOCUMENT_AMBIGUOUS",
                f"终末地集合名“{name}”对应 {len(collections)} 个不同内容集合：{choices_str}；content_types 不能保证消歧，请先检索具体篇章，再用其 document_uid + mode=\"collection\" 通读",
            )

        def endfield_sort_key(item):
            ct = str(item["document"].get("content_type") or "")
            try:
                ct_idx = END_FIELD_CONTENT_TYPE_ORDER.index(ct)
            except ValueError:
                ct_idx = len(END_FIELD_CONTENT_TYPE_ORDER)
            story_or_title = (
                item["document"].get("source_story_id") or item["document"].get("display_title") or ""
            )
            doc_id = item["document"].get("document_id") or ""
            return (ct_idx, natural_sort_key(story_or_title), natural_sort_key(doc_id))

        matches.sort(key=endfield_sort_key)
        return matches

    endfieldCollectionDocuments = endfield_collection_documents
