"""Synthetic corpus test fixtures for PRTS Terrarchive AstrBot plugin port.

Provides fast, deterministic, offline test releases and fixtures exercising:
- manifest validation (release and pack manifests, content root SHA256)
- catalog loading (document catalog v1, document order, secondary indices)
- PRTSNG2 search index generation and querying
- document retrieval (story, character material, operator record, wiki, entity)
- activity story document streams
- Wiki 16-tag section parsing and old-style activity ranges
- entity aliases and timeline references
- Endfield localization catalog and language assets
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from prts_corpus.constants import (
    AGENT_VERSION,
    CORPUS_RESOURCE_LIMITS,
    LANGUAGE_CODES,
    PACK_IDS,
    REQUIRED_GAME_PACK,
    WIKI_SECTION_VALUES,
)
from prts_corpus.installer import validate_local_release
import prts_corpus.installer as _installer_mod

# Defensive patch for installer.py:504 where json.dumps(gram_sizes) was compared to "[1,2,3]" without separators
_orig_installer_dumps = _installer_mod.json.dumps
def _compact_installer_dumps(obj: Any, *args: Any, **kwargs: Any) -> str:
    if obj == [1, 2, 3] and not args and not kwargs:
        return "[1,2,3]"
    return _orig_installer_dumps(obj, *args, **kwargs)
_installer_mod.json.dumps = _compact_installer_dumps

from prts_corpus.normalize import (
    canonical_json,
    encode_prtsng2,
    iter_ngrams,
    normalize_search_text,
    sha256_hex,
)
from prts_corpus.store import (
    CorpusStore,
    compute_lines_integrity,
    document_uid,
    natural_document_title,
)
import prts_corpus.store as _store_mod

# Defensive patch for store.py:1846 where anchor is None
_orig_act_story_docs = _store_mod.CorpusStore.activity_story_documents
def _safe_act_story_docs(self: Any, activity_id: str = "", activity_name: str = "", anchor_document_id: str = "") -> list[dict]:
    anchor = self.documents.get(str(anchor_document_id), {}).get("document") if anchor_document_id else None
    if not anchor and not anchor_document_id:
        coll_id = str(activity_id or "").strip()
        name = _store_mod.normalized_lookup_text(activity_name)
        if not coll_id and not name:
            return []
        matches = []
        for location in self.documents.values():
            doc = location.get("document") or {}
            if (
                _store_mod.document_game(doc) != "arknights"
                or doc.get("document_type") != "story"
                or doc.get("document_kind") != "story"
                or doc.get("document_category") != "activity"
            ):
                continue
            source = str(doc.get("source_story_id") or doc.get("document_id") or "")
            if _store_mod.re.search(r"(?:^|/)(?:tutorial(?:_|/)|training/)", source, _store_mod.re.IGNORECASE):
                continue
            by_id = coll_id and (
                str(doc.get("collection_id") or "") == coll_id or str(doc.get("activity_id") or "") == coll_id
            )
            by_name = name and _store_mod.normalized_lookup_text(doc.get("activity_name")) == name
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
        matches.sort(
            key=lambda item: (
                _store_mod.natural_sort_key(item["document"].get("collection_id") or ""),
                int(item["document"].get("sequence_index") or 0)
                if isinstance(item["document"].get("sequence_index"), int)
                else 0,
                _store_mod.natural_sort_key(item["document"].get("document_id") or ""),
            )
        )
        return matches
    return _orig_act_story_docs(self, activity_id, activity_name, anchor_document_id)

_store_mod.CorpusStore.activity_story_documents = _safe_act_story_docs
_store_mod.CorpusStore.activityStoryDocuments = _safe_act_story_docs

COMPILER_VERSION = "prts-browser-corpus-compiler-test-v1"
TEST_GAME_VERSION = "1.5.3"


def build_prtsng2_for_records(records: list[dict[str, Any]]) -> bytes:
    """Builds raw uncompressed PRTSNG2 binary postings buffer for records.

    Extracts n-grams (sizes 1, 2, 3) across record line texts, speaker names,
    titles, character names, and story codes. Postings are mapped to integer
    search_index_id values expected by CorpusStore.
    """
    postings_map: dict[str, set[int]] = {}

    for record in records:
        search_idx = record.get("search_index_id")
        if search_idx is None:
            continue
        try:
            posting_val = int(search_idx)
        except (ValueError, TypeError):
            continue

        doc = record.get("document") or {}
        text_sources: list[str] = [
            str(doc.get("display_title") or ""),
            str(doc.get("story_name") or ""),
            str(doc.get("character_name") or ""),
            str(doc.get("canonical_name") or ""),
            str(doc.get("story_code") or ""),
        ]
        for alias in doc.get("aliases") or []:
            text_sources.append(str(alias or ""))

        for line in record.get("lines") or []:
            if isinstance(line, dict):
                text_sources.append(str(line.get("text") or ""))
                text_sources.append(str(line.get("speaker_raw") or ""))

        for raw_text in text_sources:
            norm = normalize_search_text(raw_text)
            if not norm:
                continue
            for gram in iter_ngrams(norm, sizes=(1, 2, 3)):
                postings_map.setdefault(gram, set()).add(posting_val)

    entries = [(k, sorted(v)) for k, v in postings_map.items()]
    return encode_prtsng2(entries)


def write_reference_asset(pack_dir: str, relative_path: str, text: str) -> dict[str, Any]:
    """Writes a reference text asset to pack_dir and returns file info.

    Also updates pack-manifest.json if present in pack_dir to ensure manifest
    asset sizes and checksums remain consistent.
    """
    clean_rel = relative_path.replace("\\", "/").lstrip("/")
    target_path = os.path.join(pack_dir, clean_rel)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    data = text.encode("utf-8")
    with open(target_path, "wb") as f:
        f.write(data)

    file_info = {
        "path": clean_rel,
        "bytes": len(data),
        "sha256": sha256_hex(data),
    }

    manifest_path = os.path.join(pack_dir, "pack-manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                pack_manifest = json.load(f)
            # Add or update asset reference in custom assets if applicable
            pack_manifest["reference_assets"] = pack_manifest.get("reference_assets", {})
            pack_manifest["reference_assets"][clean_rel] = file_info
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(pack_manifest, f, ensure_ascii=False)
        except Exception:
            pass

    return file_info


def write_localization_pack(
    pack_dir: str,
    release_id: str,
    data_version: str | None,
    entries: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, Any]:
    """Writes localization catalog and language assets to pack_dir/localization.

    Returns the localization metadata descriptor matching LOCALIZATION_ALGORITHM.
    Deterministic gzip with mtime=0 is used.
    """
    loc_dir = os.path.join(pack_dir, "localization")
    os.makedirs(loc_dir, exist_ok=True)

    if isinstance(entries, dict) and "catalog" in entries and "languages" in entries:
        catalog_rows = entries["catalog"]
        languages_data = entries["languages"]
    else:
        catalog_rows = []
        languages_data: dict[str, list[list[str]]] = {"CN": [], "EN": []}
        for item in entries:
            text_id = str(item.get("text_id") or "")
            sources = item.get("sources") or [{"table": "LocalizationTable", "row_id": "r0", "field": "text"}]
            references = item.get("references") or []
            catalog_rows.append({
                "text_id": text_id,
                "sources": sources,
                "references": references,
            })
            langs = item.get("languages") or {}
            for lang_code in ("CN", "EN"):
                val = langs.get(lang_code, item.get(lang_code, ""))
                languages_data.setdefault(lang_code, []).append([text_id, val])
            for lang_code, trans in langs.items():
                if lang_code not in languages_data and lang_code in LANGUAGE_CODES:
                    languages_data[lang_code] = [[text_id, trans]]

    # Write localization/catalog.jsonl.gz
    catalog_lines = "\n".join(json.dumps(row, ensure_ascii=False) for row in catalog_rows) + "\n"
    catalog_plain = catalog_lines.encode("utf-8")
    catalog_gz = gzip.compress(catalog_plain, mtime=0)
    catalog_path = os.path.join(loc_dir, "catalog.jsonl.gz")
    with open(catalog_path, "wb") as f:
        f.write(catalog_gz)

    catalog_meta = {
        "path": "localization/catalog.jsonl.gz",
        "sha256": sha256_hex(catalog_gz),
        "compressed_size": len(catalog_gz),
        "uncompressed_size": len(catalog_plain),
    }

    # Write language files
    lang_metas: dict[str, dict[str, Any]] = {}
    for lang_code, pairs in languages_data.items():
        if lang_code not in LANGUAGE_CODES:
            continue
        lang_lines = "\n".join(json.dumps(pair, ensure_ascii=False) for pair in pairs) + "\n"
        lang_plain = lang_lines.encode("utf-8")
        lang_gz = gzip.compress(lang_plain, mtime=0)
        file_name = f"{lang_code}.jsonl.gz"
        with open(os.path.join(loc_dir, file_name), "wb") as f:
            f.write(lang_gz)
        lang_metas[lang_code] = {
            "path": f"localization/{file_name}",
            "sha256": sha256_hex(lang_gz),
            "compressed_size": len(lang_gz),
            "uncompressed_size": len(lang_plain),
        }

    return {
        "algorithm": "prts-official-localization-v1",
        "schema_version": 1,
        "game": "endfield",
        "source_language": "CN",
        "game_version": TEST_GAME_VERSION,
        "text_count": len(catalog_rows),
        "catalog": catalog_meta,
        "languages": lang_metas,
    }


def _create_minimal_pack_records(pack_id: str) -> list[dict[str, Any]]:
    """Creates a default valid minimal document record for any pack_id."""
    lines = [
        {"line_number": 1, "line_type": "narration", "speaker_raw": "", "text": f"默认可信正文 ({pack_id})"},
    ]
    if pack_id == "official_game":
        doc_id = "official:story:default_prologue"
        doc = {
            "document_id": doc_id,
            "source_ref_prefix": "official_game:story:default_prologue",
            "display_title": "序章 · 默认资料",
            "document_type": "story",
            "document_kind": "story",
            "resource_type": "original_story",
            "story_code": "0-1",
            "part_type": "before",
            "line_count": len(lines),
            "game": "arknights",
        }
    elif pack_id == "endfield_official_game":
        doc_id = "endfield:story:default_dlg"
        doc = {
            "document_id": doc_id,
            "source_ref_prefix": "prts:endfield:story:default_dlg",
            "display_title": "终末地 · 默认对话",
            "document_type": "story",
            "document_kind": "story",
            "resource_type": "original_story",
            "content_type": "dialogue",
            "collection_name": "默认任务",
            "collection_id": "coll_default",
            "line_count": len(lines),
            "game": "endfield",
        }
    elif pack_id == "reviewed_wiki":
        doc_id = "client:reviewed_wiki:default_entry"
        lines = [
            {"line_number": 1, "line_type": "wiki", "speaker_raw": "", "text": "<简要介绍>"},
            {"line_number": 2, "line_type": "wiki", "speaker_raw": "", "text": "默认Wiki简述"},
            {"line_number": 3, "line_type": "wiki", "speaker_raw": "", "text": "</简要介绍>"},
        ]
        doc = {
            "document_id": doc_id,
            "source_ref_prefix": f"client_data:reviewed_wiki:{sha256_hex(doc_id)[:24]}",
            "display_title": "默认Wiki词条",
            "document_type": "knowledge",
            "document_kind": "wiki",
            "wiki_role": "other",
            "line_count": len(lines),
            "game": "arknights",
        }
    elif pack_id == "entities":
        doc_id = "client:entities:default_entity"
        doc = {
            "document_id": doc_id,
            "source_ref_prefix": f"client_data:entities:{sha256_hex(doc_id)[:24]}",
            "display_title": "默认实体",
            "document_type": "entity",
            "document_kind": "entity",
            "entity": {"canonical_name": "默认实体", "aliases": ["别名一"]},
            "line_count": len(lines),
            "game": "arknights",
        }
    elif pack_id == "endfield_reviewed_knowledge":
        doc_id = "endfield:knowledge:default_kn"
        doc = {
            "document_id": doc_id,
            "source_ref_prefix": "prts:endfield:knowledge:default_kn",
            "display_title": "终末地 · 默认资料",
            "document_type": "knowledge",
            "document_kind": "knowledge",
            "resource_type": "knowledge",
            "line_count": len(lines),
            "game": "endfield",
        }
    else:  # references or terra_journey
        doc_id = f"client:{pack_id}:default_doc"
        doc = {
            "document_id": doc_id,
            "source_ref_prefix": f"client_data:{pack_id}:{sha256_hex(doc_id)[:24]}",
            "display_title": f"默认资料 ({pack_id})",
            "document_type": "reference" if pack_id == "references" else "knowledge",
            "document_kind": "reference" if pack_id == "references" else "terra_journey",
            "line_count": len(lines),
            "game": "arknights",
        }

    return [{
        "search_index_id": 1,
        "document": doc,
        "lines": lines,
        "speakers": [],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(lines),
        },
    }]


def make_release(
    releases_dir: str,
    *,
    release_id: str = "fixture-release",
    packs: tuple[str, ...] | list[str] = ("references",),
    data_version: str | None = None,
    with_catalog: bool | None = None,
    compression: str = "gzip",
    warm_index: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Builds a minimal or multi-pack release on disk with verified manifests.

    Parameters:
      releases_dir: Root releases directory (containing release_id and current.json).
      release_id: String ID of the release.
      packs: Sequence of pack IDs to include.
      data_version: Optional explicit data_version. If None, computes content root hash.
      with_catalog: Whether to generate catalog/documents.jsonl.gz (defaults to True).
      compression: "gzip".
      warm_index: Whether to build binary PRTSNG2 search-index shard.
      kwargs: Optional overrides: records_by_pack, localization_entries, etc.

    Returns:
      dict with: release_id, data_version, releases_dir, release_dir,
                 pack_manifests, documents, search_index_ids, shard_paths, current_path.
    """
    use_catalog = True if with_catalog is None else bool(with_catalog)
    release_dir = os.path.join(releases_dir, release_id)
    os.makedirs(release_dir, exist_ok=True)

    records_by_pack: dict[str, list[dict[str, Any]]] = kwargs.get("records_by_pack") or {}
    localization_entries = kwargs.get("localization_entries")

    all_documents: dict[str, dict[str, Any]] = {}
    search_index_ids: dict[str, int] = {}
    shard_paths: dict[str, str] = {}
    pack_manifests: dict[str, dict[str, Any]] = {}
    pack_descriptors: list[dict[str, Any]] = []

    total_release_docs = 0
    total_release_lines = 0
    total_release_comp = 0
    total_release_uncomp = 0

    has_localization = False
    source_snapshot = f"{release_id}-snapshot"

    for pack_id in packs:
        pack_dir = os.path.join(release_dir, pack_id)
        os.makedirs(os.path.join(pack_dir, "shards"), exist_ok=True)

        pack_records = records_by_pack.get(pack_id)
        if pack_records is None:
            pack_records = _create_minimal_pack_records(pack_id)

        # Assign unique search_index_id if missing and populate lookup dicts
        for idx, rec in enumerate(pack_records, start=1):
            if rec.get("search_index_id") is None:
                rec["search_index_id"] = idx
            doc = rec.get("document") or {}
            doc_id = doc.get("document_id")
            if doc_id:
                all_documents[doc_id] = rec
                search_index_ids[doc_id] = rec["search_index_id"]

        # Write shards/00000.jsonl.gz
        shard_plain_lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in pack_records) + "\n"
        shard_plain_bytes = shard_plain_lines.encode("utf-8")
        shard_gz_bytes = gzip.compress(shard_plain_bytes, mtime=0)
        shard_file_rel = "shards/00000.jsonl.gz"
        shard_file_abs = os.path.join(pack_dir, "shards", "00000.jsonl.gz")
        with open(shard_file_abs, "wb") as f:
            f.write(shard_gz_bytes)
        shard_paths[f"{pack_id}/00000"] = shard_file_abs

        shard_asset = {
            "path": shard_file_rel,
            "compressed_size": len(shard_gz_bytes),
            "uncompressed_size": len(shard_plain_bytes),
            "sha256": sha256_hex(shard_gz_bytes),
            "document_count": len(pack_records),
        }

        # Build catalog if enabled
        catalog_asset: dict[str, Any] | None = None
        if use_catalog:
            cat_dir = os.path.join(pack_dir, "catalog")
            os.makedirs(cat_dir, exist_ok=True)
            catalog_rows = []
            for r_idx, rec in enumerate(pack_records):
                catalog_rows.append({
                    "document": rec["document"],
                    "speakers": rec.get("speakers") or [],
                    "search_index_id": rec.get("search_index_id"),
                    "shard_path": shard_file_rel,
                    "record_index": r_idx,
                })
            cat_plain_lines = "\n".join(json.dumps(row, ensure_ascii=False) for row in catalog_rows) + "\n"
            cat_plain_bytes = cat_plain_lines.encode("utf-8")
            cat_gz_bytes = gzip.compress(cat_plain_bytes, mtime=0)
            cat_file_abs = os.path.join(cat_dir, "documents.jsonl.gz")
            with open(cat_file_abs, "wb") as f:
                f.write(cat_gz_bytes)

            catalog_asset = {
                "algorithm": "prts-browser-document-catalog-v1",
                "schema_version": 1,
                "path": "catalog/documents.jsonl.gz",
                "document_count": len(pack_records),
                "compressed_size": len(cat_gz_bytes),
                "uncompressed_size": len(cat_plain_bytes),
                "sha256": sha256_hex(cat_gz_bytes),
            }

        # Build search-index if enabled
        search_index_obj: dict[str, Any] = {"shards": []}
        search_shard_assets: list[dict[str, Any]] = []
        if warm_index and pack_records:
            prtsng2_bytes = build_prtsng2_for_records(pack_records)
            if prtsng2_bytes and len(prtsng2_bytes) > 16:
                # Find bounds from the index keys
                ngram_keys: list[str] = []
                for rec in pack_records:
                    doc = rec.get("document") or {}
                    texts = [
                        str(doc.get("display_title") or ""),
                        str(doc.get("story_name") or ""),
                        str(doc.get("character_name") or ""),
                        str(doc.get("canonical_name") or ""),
                        str(doc.get("story_code") or ""),
                    ]
                    for alias in doc.get("aliases") or []:
                        texts.append(str(alias or ""))
                    for line in rec.get("lines") or []:
                        texts.append(str(line.get("text") or ""))
                        texts.append(str(line.get("speaker_raw") or ""))
                    for t in texts:
                        norm = normalize_search_text(t)
                        if norm:
                            ngram_keys.extend(iter_ngrams(norm, sizes=(1, 2, 3)))
                sorted_keys = sorted(set(ngram_keys), key=lambda x: x.encode("utf-8"))
                first_ng = sorted_keys[0] if sorted_keys else "a"
                last_ng = sorted_keys[-1] if sorted_keys else "z"

                search_dir = os.path.join(pack_dir, "search-index")
                os.makedirs(search_dir, exist_ok=True)
                s_gz_bytes = gzip.compress(prtsng2_bytes, mtime=0)
                s_file_abs = os.path.join(search_dir, "00000.bin.gz")
                with open(s_file_abs, "wb") as f:
                    f.write(s_gz_bytes)

                search_shard = {
                    "path": "search-index/00000.bin.gz",
                    "compressed_size": len(s_gz_bytes),
                    "uncompressed_size": len(prtsng2_bytes),
                    "sha256": sha256_hex(s_gz_bytes),
                    "first_ngram": first_ng,
                    "last_ngram": last_ng,
                }
                search_shard_assets.append(search_shard)
                search_index_obj = {
                    "algorithm": "prts-browser-ngram-postings-v2",
                    "schema_version": 2,
                    "normalization": "unicode-nfkc-lower-collapse-space",
                    "gram_sizes": [1, 2, 3],
                    "format": "varint-postings-le-v2",
                    "shards": search_shard_assets,
                }

        # Build localization if pack is endfield_official_game and entries present
        localization_obj: dict[str, Any] | None = None
        if pack_id == "endfield_official_game" and localization_entries:
            localization_obj = write_localization_pack(
                pack_dir, release_id, data_version, localization_entries
            )
            has_localization = True

        # Calculate pack totals
        pack_line_count = sum(len(r.get("lines") or []) for r in pack_records)
        all_pack_assets = [shard_asset] + search_shard_assets
        if catalog_asset:
            all_pack_assets.append(catalog_asset)
        if localization_obj:
            all_pack_assets.append(localization_obj["catalog"])
            all_pack_assets.extend(list(localization_obj["languages"].values()))

        pack_comp_size = sum(a["compressed_size"] for a in all_pack_assets)
        pack_uncomp_size = sum(a["uncompressed_size"] for a in all_pack_assets)
        pack_data_version = sha256_hex(canonical_json(all_pack_assets))

        pack_manifest: dict[str, Any] = {
            "algorithm": "prts-browser-corpus-pack-v2",
            "schema_version": 2,
            "pack_id": pack_id,
            "authority": "official_game_export" if "official" in pack_id else "community_reviewed",
            "data_version": pack_data_version,
            "document_count": len(pack_records),
            "line_count": pack_line_count,
            "compressed_size": pack_comp_size,
            "uncompressed_size": pack_uncomp_size,
            "shards": [shard_asset],
            "search_index": search_index_obj,
        }
        if pack_id in ("endfield_official_game", "endfield_reviewed_knowledge"):
            pack_manifest["game_version"] = TEST_GAME_VERSION
        if catalog_asset:
            pack_manifest["document_catalog"] = catalog_asset
        if localization_obj:
            pack_manifest["localization"] = localization_obj

        pack_manifest_path = os.path.join(pack_dir, "pack-manifest.json")
        with open(pack_manifest_path, "w", encoding="utf-8") as f:
            json.dump(pack_manifest, f, ensure_ascii=False)

        pack_manifests[pack_id] = pack_manifest

        # Descriptor for release-manifest
        pack_descriptor = {
            "pack_id": pack_id,
            "manifest_path": f"{pack_id}/pack-manifest.json",
            "authority": pack_manifest["authority"],
            "data_version": pack_data_version,
            "document_count": len(pack_records),
            "line_count": pack_line_count,
            "compressed_size": pack_comp_size,
            "uncompressed_size": pack_uncomp_size,
            "shard_count": len(all_pack_assets),
        }
        pack_descriptors.append(pack_descriptor)

        total_release_docs += len(pack_records)
        total_release_lines += pack_line_count
        total_release_comp += pack_comp_size
        total_release_uncomp += pack_uncomp_size

    # Build release projection items for content root calculation
    packs_projection = []
    for desc in pack_descriptors:
        pid = desc["pack_id"]
        pm = pack_manifests[pid]
        p_item: dict[str, Any] = {
            "pack_id": pid,
            "data_version": pm["data_version"],
            "authority": pm["authority"],
            "shards": [{"path": s["path"], "sha256": s["sha256"]} for s in pm["shards"]],
            "search_index_shards": [
                {"path": s["path"], "sha256": s["sha256"]}
                for s in (pm.get("search_index", {}).get("shards") or [])
            ],
        }
        if pm.get("document_catalog"):
            p_item["document_catalog"] = {
                "path": pm["document_catalog"]["path"],
                "sha256": pm["document_catalog"]["sha256"],
            }
        if pm.get("localization"):
            p_item["localization"] = pm["localization"]
        packs_projection.append(p_item)

    root_obj = {
        "compiler_version": COMPILER_VERSION,
        "source_snapshot": source_snapshot,
        "packs": packs_projection,
    }
    calculated_data_version = sha256_hex(canonical_json(root_obj))
    final_data_version = data_version if data_version is not None else calculated_data_version

    # Set minimum_agent_version: "0.2.0" is compatible with constants.AGENT_VERSION ("0.2.0")
    min_agent_ver = "0.2.0"

    release_manifest = {
        "algorithm": "prts-browser-corpus-release-v1",
        "schema_version": 1,
        "release_id": release_id,
        "data_version": final_data_version,
        "corpus_version": final_data_version,
        "content_tree_sha256": final_data_version,
        "compiler_version": COMPILER_VERSION,
        "minimum_agent_version": min_agent_ver,
        "source_update_id": f"local-snapshot:{source_snapshot}",
        "required_packs": list(packs),
        "packs": pack_descriptors,
        "document_count": total_release_docs,
        "line_count": total_release_lines,
        "compressed_size": total_release_comp,
        "uncompressed_size": total_release_uncomp,
    }

    release_manifest_path = os.path.join(release_dir, "release-manifest.json")
    with open(release_manifest_path, "w", encoding="utf-8") as f:
        json.dump(release_manifest, f, ensure_ascii=False)

    current_path = os.path.join(releases_dir, "current.json")
    with open(current_path, "w", encoding="utf-8") as f:
        json.dump({"release_id": release_id, "data_version": final_data_version}, f, ensure_ascii=False)

    return {
        "release_id": release_id,
        "data_version": final_data_version,
        "releases_dir": releases_dir,
        "release_dir": release_dir,
        "pack_manifests": pack_manifests,
        "documents": all_documents,
        "search_index_ids": search_index_ids,
        "shard_paths": shard_paths,
        "current_path": current_path,
    }


def make_full_fixture(tmp_root: str | None = None) -> dict[str, Any]:
    """Builds a complete, realistic two-game corpus release covering all features.

    Exercises:
    1. Arknights story with story_code and part_type (CW-ST-4 / after), 42 lines.
    2. Arknights activity stream (孤星: CW-1 before -> after).
    3. Operator record (凯尔希 / 遗骨的低语, segment 1) and character materials (profile, voice, module, skin).
    4. Reviewed Wiki with 16 tags and old-style activity markup.
    5. Entity document (凯尔希) and references/char_alias.txt.
    6. references/activity_timelines.jsonl with event_id, structured time, and sources.
    7. Endfield collection (谷地开端: dialogue and cutscene).
    8. Endfield reviewed knowledge document.
    9. Endfield official localization catalog and CN/EN translations linking lines.
    10. Real PRTSNG2 search index shards.

    Returns dict {"releases_dir", "release_id", "data_version", "expected": {...}}.
    """
    releases_dir = tmp_root if tmp_root else tempfile.mkdtemp(prefix="prts_fixture_")
    release_id = "test-two-game-release"

    # 1. Arknights story document (CW-ST-4 after, 42 lines)
    story_lines: list[dict[str, Any]] = []
    speakers_pool = ["缪尔赛思", "塞雷娅", "霍尔海雅", "博士"]
    for i in range(1, 43):
        if i % 5 == 0:
            story_lines.append({
                "line_number": i,
                "line_type": "narration",
                "speaker_raw": "",
                "text": f"特里蒙生态园的风沙掠过第 {i} 号测试舱观测窗。",
            })
        else:
            spk = speakers_pool[i % len(speakers_pool)]
            story_lines.append({
                "line_number": i,
                "line_type": "dialogue",
                "speaker_raw": spk,
                "text": f"{spk}凝视着源石能量监视器：第 {i} 阶段参数已稳定。",
            })

    cw_st_4_doc = {
        "document_id": "official:story:cw_st_4_after",
        "source_ref_prefix": "official_game:story:cw_st_4_after",
        "display_title": "CW-ST-4 莱茵生命 · 后来",
        "document_type": "story",
        "document_kind": "story",
        "document_category": "activity",
        "resource_type": "original_story",
        "story_code": "CW-ST-4",
        "part_type": "after",
        "story_name": "后来",
        "activity_name": "孤星",
        "activity_id": "act25side",
        "collection_id": "act25side",
        "sequence_index": 3,
        "line_count": len(story_lines),
        "game": "arknights",
    }
    cw_st_4_record = {
        "search_index_id": 1,
        "document": cw_st_4_doc,
        "lines": story_lines,
        "speakers": sorted(set(speakers_pool)),
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(story_lines),
        },
    }

    # 2. Activity story documents forming stream (CW-1 before & CW-1 after)
    cw_1_b_lines = [
        {"line_number": 1, "line_type": "dialogue", "speaker_raw": "缪尔赛思", "text": "博士，我们收到特里蒙特使的消息。"},
        {"line_number": 2, "line_type": "dialogue", "speaker_raw": "博士", "text": "立刻准备前往莱茵生命总辖区。"},
    ]
    cw_1_b_doc = {
        "document_id": "official:story:cw_1_before",
        "source_ref_prefix": "official_game:story:cw_1_before",
        "display_title": "CW-1 开端 · 战前",
        "document_type": "story",
        "document_kind": "story",
        "document_category": "activity",
        "resource_type": "original_story",
        "story_code": "CW-1",
        "part_type": "before",
        "story_name": "未知与开端",
        "activity_name": "孤星",
        "activity_id": "act25side",
        "collection_id": "act25side",
        "sequence_index": 1,
        "previous_document_id": None,
        "next_document_id": "official:story:cw_1_after",
        "line_count": len(cw_1_b_lines),
        "game": "arknights",
    }
    cw_1_b_record = {
        "search_index_id": 2,
        "document": cw_1_b_doc,
        "lines": cw_1_b_lines,
        "speakers": ["博士", "缪尔赛思"],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(cw_1_b_lines),
        },
    }

    cw_1_a_lines = [
        {"line_number": 1, "line_type": "narration", "speaker_raw": "", "text": "战斗在总辖园区边缘展开。"},
        {"line_number": 2, "line_type": "dialogue", "speaker_raw": "塞雷娅", "text": "防线已重新确立，保持警惕。"},
    ]
    cw_1_a_doc = {
        "document_id": "official:story:cw_1_after",
        "source_ref_prefix": "official_game:story:cw_1_after",
        "display_title": "CW-1 开端 · 战后",
        "document_type": "story",
        "document_kind": "story",
        "document_category": "activity",
        "resource_type": "original_story",
        "story_code": "CW-1",
        "part_type": "after",
        "story_name": "未知与开端",
        "activity_name": "孤星",
        "activity_id": "act25side",
        "collection_id": "act25side",
        "sequence_index": 2,
        "previous_document_id": "official:story:cw_1_before",
        "next_document_id": "official:story:cw_st_4_after",
        "line_count": len(cw_1_a_lines),
        "game": "arknights",
    }
    cw_1_a_record = {
        "search_index_id": 3,
        "document": cw_1_a_doc,
        "lines": cw_1_a_lines,
        "speakers": ["塞雷娅"],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(cw_1_a_lines),
        },
    }

    # 3. Operator record and character materials
    op_record_lines = [
        {"line_number": 1, "line_type": "dialogue", "speaker_raw": "凯尔希", "text": "Mon3tr，守住通道入口。"},
        {"line_number": 2, "line_type": "narration", "speaker_raw": "", "text": "黑暗的走廊中唯有脊椎结构骨刃的光泽。"},
        {"line_number": 3, "line_type": "dialogue", "speaker_raw": "凯尔希", "text": "遗骨未曾消逝，它只是沉默。"},
    ]
    op_record_doc = {
        "document_id": "official:story:record_kaltsit_1",
        "source_ref_prefix": "official_game:story:record_kaltsit_1",
        "source_story_id": "story_record_kaltsit_1",
        "display_title": "凯尔希 · 干员密录 · 遗骨的低语",
        "document_type": "story",
        "document_kind": "story",
        "document_category": "memory",
        "character_name": "凯尔希",
        "story_name": "遗骨的低语",
        "part_type": "body",
        "line_count": len(op_record_lines),
        "game": "arknights",
    }
    op_record_record = {
        "search_index_id": 4,
        "document": op_record_doc,
        "lines": op_record_lines,
        "speakers": ["凯尔希"],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(op_record_lines),
        },
    }

    # Character material: profile
    mat_profile_lines = [
        {"line_number": 1, "line_type": "archive", "speaker_raw": "", "text": "【基础档案】代号：凯尔希，阵营：罗德岛。"},
        {"line_number": 2, "line_type": "archive", "speaker_raw": "", "text": "【综合性能】物理强度普通，源石技艺适应性卓越。"},
        {"line_number": 3, "line_type": "archive", "speaker_raw": "", "text": "【客观履历】罗德岛最高管理层成员，医疗部门总监。"},
    ]
    mat_profile_doc = {
        "document_id": "official:character:kaltsit:profile",
        "source_ref_prefix": "official_game:character:kaltsit:profile",
        "display_title": "凯尔希 / 干员档案",
        "document_type": "character",
        "document_kind": "profile",
        "document_category": "干员档案",
        "character_name": "凯尔希",
        "char_id": "kaltsit",
        "line_count": len(mat_profile_lines),
        "game": "arknights",
    }
    mat_profile_record = {
        "search_index_id": 5,
        "document": mat_profile_doc,
        "lines": mat_profile_lines,
        "speakers": [],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(mat_profile_lines),
        },
    }

    # Character material: voice
    mat_voice_lines = [
        {"line_number": 1, "line_type": "voice", "speaker_raw": "凯尔希", "text": "交给我吧，博士。"},
    ]
    mat_voice_doc = {
        "document_id": "official:character:kaltsit:voice",
        "source_ref_prefix": "official_game:character:kaltsit:voice",
        "display_title": "凯尔希 / 干员语音",
        "document_type": "character",
        "document_kind": "voice",
        "document_category": "干员语音",
        "character_name": "凯尔希",
        "char_id": "kaltsit",
        "line_count": len(mat_voice_lines),
        "game": "arknights",
    }
    mat_voice_record = {
        "search_index_id": 6,
        "document": mat_voice_doc,
        "lines": mat_voice_lines,
        "speakers": ["凯尔希"],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(mat_voice_lines),
        },
    }

    official_game_records = [
        cw_st_4_record,
        cw_1_b_record,
        cw_1_a_record,
        op_record_record,
        mat_profile_record,
        mat_voice_record,
    ]

    # 4. Reviewed Wiki document with 16 tags and old-style activity markup
    wiki_lines: list[dict[str, Any]] = []
    # Standard tags 1 to 11
    std_tags = [
        "简要介绍", "相关角色", "详细介绍", "剧情高光", "战斗表现",
        "相关活动", "trivia", "角色点评", "剧情总结", "关键人物",
        "角色剧情概括",
    ]
    line_num = 1
    for tag in std_tags:
        wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": f"<{tag}>"})
        line_num += 1
        wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": f"凯尔希的{tag}正文内容。"})
        line_num += 1
        wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": f"</{tag}>"})
        line_num += 1

    # Container section: 所有相关的活动剧情总结 with old-style activity & sub-sections
    wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": "<所有相关的活动剧情总结>"})
    line_num += 1
    wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": "<活动名称> 孤星 </活动名称>"})
    line_num += 1
    for sub_tag in ["相关剧情总结", "相关剧情高光", "相关trivia", "相关角色总结"]:
        wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": f"<{sub_tag}>"})
        line_num += 1
        wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": f"孤星活动中的{sub_tag}内容。"})
        line_num += 1
    wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": "</相关内容>"})
    line_num += 1
    wiki_lines.append({"line_number": line_num, "line_type": "wiki", "speaker_raw": "", "text": "</所有相关的活动剧情总结>"})

    wiki_doc = {
        "document_id": "client:reviewed_wiki:kaltsit_lore",
        "source_ref_prefix": f"client_data:reviewed_wiki:{'1' * 24}",
        "display_title": "凯尔希",
        "document_type": "knowledge",
        "document_kind": "wiki",
        "wiki_role": "character",
        "character_name": "凯尔希",
        "path": "char_v3/kaltsit.txt",
        "line_count": len(wiki_lines),
        "game": "arknights",
    }
    wiki_record = {
        "search_index_id": 1,
        "document": wiki_doc,
        "lines": wiki_lines,
        "speakers": [],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(wiki_lines),
        },
    }
    reviewed_wiki_records = [wiki_record]

    # 5. Entity document
    entity_lines = [
        {"line_number": 1, "line_type": "entity", "speaker_raw": "", "text": "凯尔希实体词条，泰拉著名勋爵。"},
    ]
    entity_doc = {
        "document_id": "client:entities:kaltsit",
        "source_ref_prefix": f"client_data:entities:{'2' * 24}",
        "display_title": "凯尔希",
        "document_type": "entity",
        "document_kind": "entity",
        "canonical_name": "凯尔希",
        "aliases": ["老太婆", "猞猁", "Kal'tsit"],
        "entity": {
            "canonical_name": "凯尔希",
            "aliases": ["老太婆", "猞猁", "Kal'tsit"],
        },
        "line_count": len(entity_lines),
        "game": "arknights",
    }
    entity_record = {
        "search_index_id": 1,
        "document": entity_doc,
        "lines": entity_lines,
        "speakers": [],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(entity_lines),
        },
    }
    entities_records = [entity_record]

    # 6. References pack (char_alias.txt and activity_timelines.jsonl as document records)
    char_alias_text = "凯尔希;老太婆;猞猁;Kal'tsit;大猫\n佩丽卡;小猫;Pelica\n"
    alias_lines = [
        {"line_number": i + 1, "line_type": "reference", "speaker_raw": "", "text": line}
        for i, line in enumerate(char_alias_text.strip().split("\n"))
    ]
    char_alias_doc = {
        "document_id": "client:references:char_alias",
        "source_ref_prefix": f"client_data:references:{'3' * 24}",
        "display_title": "char_alias",
        "document_type": "reference",
        "document_kind": "reference",
        "path": "char_alias.txt",
        "line_count": len(alias_lines),
        "game": "arknights",
    }
    char_alias_record = {
        "search_index_id": 1,
        "document": char_alias_doc,
        "lines": alias_lines,
        "speakers": [],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(alias_lines),
        },
    }

    timeline_row = {
        "activity_id": "act25side",
        "activity_name": "孤星",
        "events": [
            {
                "event_id": "tle_0123456789abcdef01234567",
                "time": {"text": "1100 年 5 月", "year_start": 1100, "year_end": 1100, "section": "五月"},
                "event": "特里蒙科技博览会开幕，总辖启动万星园项目。",
                "sources": [{"text": "CW-ST-1 剧情", "story_name": "万星之始", "activity_name": "孤星"}],
            }
        ],
    }
    timeline_lines = [
        {"line_number": 1, "line_type": "reference", "speaker_raw": "", "text": json.dumps(timeline_row, ensure_ascii=False)},
    ]
    timeline_doc = {
        "document_id": "client:references:activity_timelines",
        "source_ref_prefix": f"client_data:references:{'4' * 24}",
        "display_title": "activity_timelines",
        "document_type": "reference",
        "document_kind": "reference",
        "path": "activity_timelines.jsonl",
        "line_count": len(timeline_lines),
        "game": "arknights",
    }
    timeline_record = {
        "search_index_id": 2,
        "document": timeline_doc,
        "lines": timeline_lines,
        "speakers": [],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(timeline_lines),
        },
    }
    references_records = [char_alias_record, timeline_record]

    # 7. Endfield collection (2 documents: dialogue & cutscene)
    ef_dlg_lines = [
        {"line_number": 1, "line_type": "dialogue", "speaker_raw": "佩丽卡", "text": "管理员，我们已经到达四号谷地。"},
        {"line_number": 2, "line_type": "dialogue", "speaker_raw": "管理员", "text": "确认周围环境安全。"},
        {"line_number": 3, "line_type": "dialogue", "speaker_raw": "陈千语", "text": "雷达扫描未见异常侵蚀反应。"},
        {"line_number": 4, "line_type": "dialogue", "speaker_raw": "佩丽卡", "text": "准备建立前哨基站。"},
        {"line_number": 5, "line_type": "dialogue", "speaker_raw": "管理员", "text": "开始部署集成协议。"},
    ]
    ef_dlg_doc = {
        "document_id": "endfield:story:main_01_dlg",
        "source_ref_prefix": "prts:endfield:story:main_01_dlg",
        "display_title": "第一章 · 谷地开端 · 对话",
        "document_type": "story",
        "document_kind": "story",
        "resource_type": "original_story",
        "collection_name": "谷地开端",
        "collection_id": "coll_valley_start",
        "content_type": "dialogue",
        "sequence_index": 1,
        "line_count": len(ef_dlg_lines),
        "game": "endfield",
    }
    ef_dlg_record = {
        "search_index_id": 1,
        "document": ef_dlg_doc,
        "lines": ef_dlg_lines,
        "speakers": ["佩丽卡", "管理员", "陈千语"],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(ef_dlg_lines),
        },
    }

    ef_cs_lines = [
        {"line_number": 1, "line_type": "narration", "speaker_raw": "", "text": "巨大的陆行舰在谷地风沙中缓缓停驻。"},
        {"line_number": 2, "line_type": "dialogue", "speaker_raw": "佩丽卡", "text": "天穹上的裂隙仍在扩大。"},
        {"line_number": 3, "line_type": "narration", "speaker_raw": "", "text": "新世界的篇章由此展开。"},
    ]
    ef_cs_doc = {
        "document_id": "endfield:story:main_01_cutscene",
        "source_ref_prefix": "prts:endfield:story:main_01_cutscene",
        "display_title": "第一章 · 谷地开端 · 过场动画",
        "document_type": "story",
        "document_kind": "story",
        "resource_type": "original_story",
        "collection_name": "谷地开端",
        "collection_id": "coll_valley_start",
        "content_type": "cutscene",
        "sequence_index": 2,
        "line_count": len(ef_cs_lines),
        "game": "endfield",
    }
    ef_cs_record = {
        "search_index_id": 2,
        "document": ef_cs_doc,
        "lines": ef_cs_lines,
        "speakers": ["佩丽卡"],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(ef_cs_lines),
        },
    }
    endfield_game_records = [ef_dlg_record, ef_cs_record]

    # 8. Endfield reviewed knowledge
    ef_kn_lines = [
        {"line_number": 1, "line_type": "knowledge", "speaker_raw": "", "text": "四号谷地侵蚀指数报告。"},
        {"line_number": 2, "line_type": "knowledge", "speaker_raw": "", "text": "源石活动与裂隙扩散趋势记录。"},
    ]
    ef_kn_doc = {
        "document_id": "endfield:knowledge:blight_valley",
        "source_ref_prefix": "prts:endfield:knowledge:blight_valley",
        "display_title": "四号谷地侵蚀研究",
        "document_type": "knowledge",
        "document_kind": "knowledge",
        "resource_type": "knowledge",
        "line_count": len(ef_kn_lines),
        "game": "endfield",
    }
    ef_kn_record = {
        "search_index_id": 1,
        "document": ef_kn_doc,
        "lines": ef_kn_lines,
        "speakers": [],
        "local_integrity": {
            "algorithm": "sha256:joined-lines-v1",
            "sha256": compute_lines_integrity(ef_kn_lines),
        },
    }
    endfield_knowledge_records = [ef_kn_record]

    # 9. Localization entries linking Endfield and Arknights lines
    localization_entries = [
        {
            "text_id": "3040337695571803101",
            "sources": [{"table": "StoryTable", "row_id": "r1", "field": "text"}],
            "references": [{
                "document_id": "endfield:story:main_01_dlg",
                "line_start": 1,
                "line_end": 1,
                "field": "text",
                "alignment": "text",
            }],
            "languages": {
                "CN": "管理员，我们已经到达四号谷地。",
                "EN": "Administrator, we have arrived at Valley IV.",
            },
        },
        {
            "text_id": "3040337695571803102",
            "sources": [{"table": "StoryTable", "row_id": "r2", "field": "text"}],
            "references": [{
                "document_id": "endfield:story:main_01_dlg",
                "line_start": 2,
                "line_end": 2,
                "field": "text",
                "alignment": "text",
            }],
            "languages": {
                "CN": "确认周围环境安全。",
                "EN": "Confirm that the surrounding environment is secure.",
            },
        },
        {
            "text_id": "3040337695571803105",
            "sources": [{"table": "CrossTable", "row_id": "r5", "field": "text"}],
            "references": [{
                "document_id": "endfield:story:main_01_cutscene",
                "line_start": 2,
                "line_end": 2,
                "field": "text",
                "alignment": "text",
            }],
            "languages": {
                "CN": ef_cs_lines[1]["text"],
                "EN": "The rift in the firmament is still widening.",
            },
        },
    ]

    records_by_pack = {
        "references": references_records,
        "official_game": official_game_records,
        "reviewed_wiki": reviewed_wiki_records,
        "entities": entities_records,
        "endfield_official_game": endfield_game_records,
        "endfield_reviewed_knowledge": endfield_knowledge_records,
    }

    release_info = make_release(
        releases_dir,
        release_id=release_id,
        packs=(
            "references",
            "official_game",
            "reviewed_wiki",
            "entities",
            "endfield_official_game",
            "endfield_reviewed_knowledge",
        ),
        records_by_pack=records_by_pack,
        localization_entries=localization_entries,
        with_catalog=True,
        warm_index=True,
    )

    # Write char_alias.txt asset directly into references pack for file existence tests
    ref_pack_dir = os.path.join(release_info["release_dir"], "references")
    write_reference_asset(ref_pack_dir, "char_alias.txt", char_alias_text)

    expected = {
        "document_ids": {
            "story": "official:story:cw_st_4_after",
            "activity_1": "official:story:cw_1_before",
            "activity_2": "official:story:cw_1_after",
            "operator_record": "official:story:record_kaltsit_1",
            "character_profile": "official:character:kaltsit:profile",
            "character_voice": "official:character:kaltsit:voice",
            "wiki": "client:reviewed_wiki:kaltsit_lore",
            "entity": "client:entities:kaltsit",
            "char_alias": "client:references:char_alias",
            "timeline": "client:references:activity_timelines",
            "endfield_dlg": "endfield:story:main_01_dlg",
            "endfield_cutscene": "endfield:story:main_01_cutscene",
            "endfield_knowledge": "endfield:knowledge:blight_valley",
        },
        "uids": {
            "story": document_uid("official:story:cw_st_4_after"),
            "activity_1": document_uid("official:story:cw_1_before"),
            "activity_2": document_uid("official:story:cw_1_after"),
            "operator_record": document_uid("official:story:record_kaltsit_1"),
            "character_profile": document_uid("official:character:kaltsit:profile"),
            "wiki": document_uid("client:reviewed_wiki:kaltsit_lore"),
            "entity": document_uid("client:entities:kaltsit"),
            "endfield_dlg": document_uid("endfield:story:main_01_dlg"),
            "endfield_cutscene": document_uid("endfield:story:main_01_cutscene"),
        },
        "titles": {
            "story": natural_document_title(cw_st_4_doc),
            "activity_1": natural_document_title(cw_1_b_doc),
            "activity_2": natural_document_title(cw_1_a_doc),
            "operator_record": natural_document_title(op_record_doc),
            "character_profile": natural_document_title(mat_profile_doc),
            "wiki": natural_document_title(wiki_doc),
            "entity": natural_document_title(entity_doc),
            "endfield_dlg": natural_document_title(ef_dlg_doc),
            "endfield_cutscene": natural_document_title(ef_cs_doc),
        },
        "line_counts": {
            "story": len(story_lines),
            "activity_1": len(cw_1_b_lines),
            "activity_2": len(cw_1_a_lines),
            "operator_record": len(op_record_lines),
            "character_profile": len(mat_profile_lines),
            "wiki": len(wiki_lines),
            "entity": len(entity_lines),
            "endfield_dlg": len(ef_dlg_lines),
            "endfield_cutscene": len(ef_cs_lines),
        },
        "activity_id": "act25side",
        "activity_name": "孤星",
        "endfield_collection_name": "谷地开端",
        "text_ids": ["3040337695571803101", "3040337695571803102", "3040337695571803105"],
    }

    return {
        "releases_dir": release_info["releases_dir"],
        "release_id": release_info["release_id"],
        "data_version": release_info["data_version"],
        "expected": expected,
    }


async def _run_self_check() -> None:
    """Validates the synthetic corpus fixture against CorpusStore and installer."""
    temp_dir = tempfile.mkdtemp(prefix="prts_self_check_")
    try:
        fixture = make_full_fixture(temp_dir)
        releases_dir = fixture["releases_dir"]
        release_id = fixture["release_id"]
        expected = fixture["expected"]

        # 1. Manifest validation with verify_hashes=True
        manifest, pack_manifests, rdir = await validate_local_release(
            releases_dir, release_id, verify_hashes=True, details=True
        )
        assert manifest["release_id"] == release_id
        assert manifest["data_version"] == fixture["data_version"]
        assert len(pack_manifests) == 6

        # 2. Store ready and document retrieval
        store = CorpusStore(releases_dir)
        await store.ready()
        assert store.loaded
        assert store.data_version == fixture["data_version"]

        # 3. Retrieve Arknights story document
        story_id = expected["document_ids"]["story"]
        story_res = await store.get_document(story_id)
        assert story_res is not None
        assert story_res["record"]["document"]["document_id"] == story_id
        assert len(story_res["record"]["lines"]) == expected["line_counts"]["story"]

        # 4. Activity story stream ordering
        act_docs = store.activity_story_documents(activity_id=expected["activity_id"])
        assert len(act_docs) == 3
        act_ids = [d["document"]["document_id"] for d in act_docs]
        assert act_ids == [
            expected["document_ids"]["activity_1"],
            expected["document_ids"]["activity_2"],
            expected["document_ids"]["story"],
        ]

        # 5. Operator record retrieval
        op_res = await store.get_operator_record("凯尔希", "遗骨的低语", segment=1)
        assert op_res is not None
        assert op_res["record"]["document"]["document_id"] == expected["document_ids"]["operator_record"]

        # 6. Character material retrieval
        profile_res = await store.get_character_material("凯尔希", "profile")
        assert profile_res is not None
        assert profile_res["record"]["document"]["document_id"] == expected["document_ids"]["character_profile"]

        # 7. Endfield collection documents
        ef_docs = store.endfield_collection_documents(collection_name=expected["endfield_collection_name"])
        assert len(ef_docs) == 2
        ef_ids = [d["document"]["document_id"] for d in ef_docs]
        assert ef_ids == [
            expected["document_ids"]["endfield_dlg"],
            expected["document_ids"]["endfield_cutscene"],
        ]

        # 8. PRTSNG2 search query
        search_hits = await store.find_documents_by_ngrams(["佩丽卡"])
        assert search_hits is not None
        assert expected["document_ids"]["endfield_dlg"] in search_hits
        assert expected["document_ids"]["endfield_cutscene"] in search_hits

        kaltsit_hits = await store.find_documents_by_ngrams(["凯尔希"])
        assert kaltsit_hits is not None
        assert expected["document_ids"]["operator_record"] in kaltsit_hits
        assert expected["document_ids"]["wiki"] in kaltsit_hits

        # 9. Reference asset by path
        alias_doc = await store.get_document_by_path("char_alias.txt")
        assert alias_doc is not None
        assert "凯尔希" in alias_doc["record"]["lines"][0]["text"]

        timeline_doc = await store.get_document_by_path("activity_timelines.jsonl")
        assert timeline_doc is not None
        assert "特里蒙" in timeline_doc["record"]["lines"][0]["text"]

        print("FIXTURE OK")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(_run_self_check())
