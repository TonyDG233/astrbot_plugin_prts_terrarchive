"""设置页显式调用的资料包下载器与校验器。

下载完成并激活 release 后，CorpusStore 才会打开。
下载前先从 PRTS.chat current、release manifest 与 pack manifest 取得受信
release ID、data_version 和逐文件摘要；下载源只负责提供与摘要匹配的字节：
  1. modelscope —— PRTS.chat 登记的固定 release 镜像；
  2. site       —— PRTS.chat 站点资源接口（published/preview 匿名可取）。

任一源下载失败即切换下一源；分片只按 PRTS.chat 可信清单的 sha256 校验，
已存在且校验一致的文件跳过（跨源断点续传天然成立：同一构建的分片哈希相同）。
全部通过后才写 current.json 指针——中途失败不产生“半激活”状态。
"""

from __future__ import annotations

import asyncio
import datetime
import errno
import hashlib
import inspect
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import time
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import aiohttp

from .constants import (
    AGENT_VERSION,
    ASSET_PATH_PATTERN,
    CORPUS_RESOURCE_LIMITS,
    DEFAULT_RELEASE_ID,
    DEFAULT_SITE_BASE_URL,
    LANGUAGE_CODES,
    MANIFEST_TIMEOUT_SECONDS,
    MAX_CURRENT_POINTER_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_UNVERIFIED_BYTES,
    MODELSCOPE_REPOS,
    PACK_ALGORITHMS,
    PACK_IDS,
    RELEASE_ALGORITHM,
    RELEASE_ID_PATTERN,
    REQUIRED_GAME_PACK,
    SHA256_PATTERN,
    SUPPORTED_SEARCH_INDEX_ALGORITHMS,
)
from .errors import InstallerFault
from .normalize import canonical_json, sha256_hex

# 官方本地化算法标识
LOCALIZATION_ALGORITHM = "prts-official-localization-v1"

# ModelScope 历史发布组合
MODELSCOPE_RELEASE_COMPOSITIONS: dict[str, dict[str, Any]] = {
    "agent-corpus-v2-20260903-xuesong-youmeng-v1": {
        "layout": "legacy-three-dataset-v1",
        "data_version": "77df7c534525256af1dd36b68128cdd878ac2f3bc109636c5051fa85dd3dae09",
        "dataVersion": "77df7c534525256af1dd36b68128cdd878ac2f3bc109636c5051fa85dd3dae09",
        "releases": {
            "official": "agent-corpus-v1-20260826-timeline-v1",
            "endfield": "agent-corpus-v2-20260903-xuesong-youmeng-v1",
            "community": "agent-corpus-v1-20260826-timeline-v1",
        },
    },
    "agent-corpus-v2-20260905-character-activity-split-v1": {
        "layout": "legacy-three-dataset-v1",
        "data_version": "ebf6bec17dc40894c8bc1987197f34bd9800be77baa578de4a04f241c542fba9",
        "dataVersion": "ebf6bec17dc40894c8bc1987197f34bd9800be77baa578de4a04f241c542fba9",
        "releases": {
            "official": "agent-corpus-v2-20260904-retraveler-alias-fix-v1",
            "endfield": "agent-corpus-v2-20260904-retraveler-alias-fix-v1",
            "community": "agent-corpus-v2-20260905-character-activity-split-v1",
        },
    },
}

PACK_DESCRIPTOR_FIELDS = frozenset([
    "pack_id",
    "manifest_path",
    "authority",
    "data_version",
    "document_count",
    "line_count",
    "compressed_size",
    "uncompressed_size",
    "shard_count",
])

# 保存从 PRTS.chat current 解析出的受信快照私有副本
_trusted_current_snapshots: dict[int, dict[str, Any]] = {}


def _parse_identifier_list(value: str, prerelease: bool = False) -> list[str] | None:
    if value == "":
        return None
    identifiers = value.split(".")
    for identifier in identifiers:
        if not re.match(r"^[0-9A-Za-z-]+$", identifier):
            return None
        if prerelease and re.match(r"^\d+$", identifier) and len(identifier) > 1 and identifier.startswith("0"):
            return None
    return identifiers


def parse_semver(value: Any) -> dict[str, Any] | None:
    """严格解析符合 Semantic Version 2.0.0 的版本号，无数值精度损失。"""
    if not isinstance(value, str) or len(value) == 0 or len(value) > 256 or value.strip() != value:
        return None
    plus = value.find("+")
    if plus != -1 and value.find("+", plus + 1) != -1:
        return None
    core_and_prerelease = value[:plus] if plus != -1 else value
    build_text = value[plus + 1:] if plus != -1 else None
    dash = core_and_prerelease.find("-")
    core = core_and_prerelease[:dash] if dash != -1 else core_and_prerelease
    prerelease_text = core_and_prerelease[dash + 1:] if dash != -1 else None
    match = re.match(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$", core)
    prerelease = [] if prerelease_text is None else _parse_identifier_list(prerelease_text, prerelease=True)
    build = [] if build_text is None else _parse_identifier_list(build_text)
    if not match or prerelease is None or build is None:
        return None
    return {
        "raw": value,
        "core": match.groups(),
        "prerelease": tuple(prerelease),
        "build": tuple(build),
    }


def _compare_numeric_identifiers(left: str, right: str) -> int:
    if len(left) != len(right):
        return -1 if len(left) < len(right) else 1
    return 0 if left == right else (-1 if left < right else 1)


def compare_semver(left_value: str, right_value: str) -> int:
    """比较严格 SemVer 版本号。构建元数据不参与优先级比较。"""
    left = parse_semver(left_value)
    right = parse_semver(right_value)
    if not left or not right:
        raise TypeError("版本必须是有效的 Semantic Version 2.0.0")
    for index in range(3):
        comp = _compare_numeric_identifiers(left["core"][index], right["core"][index])
        if comp != 0:
            return comp
    if not left["prerelease"] or not right["prerelease"]:
        if len(left["prerelease"]) == len(right["prerelease"]):
            return 0
        return -1 if left["prerelease"] else 1
    count = max(len(left["prerelease"]), len(right["prerelease"]))
    for index in range(count):
        left_id = left["prerelease"][index] if index < len(left["prerelease"]) else None
        right_id = right["prerelease"][index] if index < len(right["prerelease"]) else None
        if left_id is None or right_id is None:
            return -1 if left_id is None else 1
        left_num = bool(re.match(r"^\d+$", left_id))
        right_num = bool(re.match(r"^\d+$", right_id))
        if left_num and right_num:
            comp = _compare_numeric_identifiers(left_id, right_id)
            if comp != 0:
                return comp
        elif left_num != right_num:
            return -1 if left_num else 1
        elif left_id != right_id:
            return -1 if left_id < right_id else 1
    return 0


def normalize_site_base_url(value: str = DEFAULT_SITE_BASE_URL) -> str:
    """字节回退站点必须是 HTTPS；仅本地开发允许环回 HTTP。它不参与选版或摘要签发。"""
    try:
        parts = urlsplit(str(value if value is not None else ""))
    except Exception:
        raise InstallerFault("INVALID_REQUEST", "siteBaseUrl 不是有效 URL")
    if not parts.scheme or not parts.netloc:
        raise InstallerFault("INVALID_REQUEST", "siteBaseUrl 不是有效 URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise InstallerFault(
            "INVALID_REQUEST",
            "siteBaseUrl 必须使用 HTTPS（本地环回可用 HTTP），且不能包含凭证、查询或片段",
        )
    hostname = (parts.hostname or "").lower()
    loopback = hostname in ("localhost", "127.0.0.1", "::1", "[::1]")
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise InstallerFault(
            "INVALID_REQUEST",
            "siteBaseUrl 必须使用 HTTPS（本地环回可用 HTTP），且不能包含凭证、查询或片段",
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")).rstrip("/")


def release_id_valid(value: Any) -> bool:
    """release id 白名单：以字母/数字开头，禁止路径分隔符或空段。"""
    return bool(isinstance(value, str) and RELEASE_ID_PATTERN.match(value))


def missing_enabled_game_packs(manifest: dict[str, Any] | None, enabled_games: Sequence[str] = ()) -> list[str]:
    """返回当前 release 相对启用游戏所缺的官方资料包。"""
    raw_packs = manifest.get("packs") if manifest and isinstance(manifest, dict) else []
    packs = {str(pack.get("pack_id") or "") for pack in raw_packs if isinstance(pack, dict)}
    res: list[str] = []
    seen = set()
    for game in enabled_games:
        if game not in seen:
            seen.add(game)
            required = REQUIRED_GAME_PACK.get(game)
            if required and required not in packs:
                res.append(game)
    return res


def is_contained_path(root: str, target: str) -> bool:
    """判断 target 是否在 root 的直接或间接子目录内，防止路径穿越。"""
    try:
        rel = os.path.relpath(target, root)
    except ValueError:
        return False
    if rel in ("", ".", ".."):
        return False
    if rel.startswith(".." + os.sep) or rel.startswith("../") or os.path.isabs(rel):
        return False
    return True


def _require_integer(
    value: Any,
    label: str,
    minimum: int = 0,
    maximum: int = sys.maxsize,
    code: str = "INVALID_MANIFEST",
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise InstallerFault(code, f"{label} 非法")
    return value


def _require_pack_id(pack_id: str, code: str = "INVALID_MANIFEST") -> str:
    if pack_id not in PACK_IDS:
        raise InstallerFault(code, f"pack_id 非法: {pack_id}")
    return pack_id


def _has_control_or_format(s: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in s)


async def require_contained_directory(path: str, root: str, label: str, code: str = "INVALID_RELEASE") -> str:
    try:
        st = os.lstat(path)
    except OSError:
        raise InstallerFault(code, f"{label} 不是受管目录")
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise InstallerFault(code, f"{label} 不是受管目录")
    actual = os.path.realpath(path)
    if not is_contained_path(root, actual):
        raise InstallerFault(code, f"{label} 越出 releases 目录")
    return actual


async def read_contained_json(path: str, root: str, label: str, code: str = "INVALID_RELEASE") -> dict[str, Any]:
    try:
        st = os.lstat(path)
    except OSError:
        raise InstallerFault(code, f"{label} 不是有效的受管清单")
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_size > MAX_MANIFEST_BYTES:
        raise InstallerFault(code, f"{label} 不是有效的受管清单")
    actual = os.path.realpath(path)
    if not is_contained_path(root, actual):
        raise InstallerFault(code, f"{label} 越出 release 目录")
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
    except Exception as error:
        if isinstance(error, InstallerFault):
            raise error
        raise InstallerFault(code, f"{label} 不是有效 JSON")
    if not isinstance(value, dict):
        raise InstallerFault(code, f"{label} 顶层必须是对象")
    return value


async def read_current_release_pointer(releases_dir: str) -> dict[str, Any]:
    """有界读取受管 current.json，避免符号链接或超大本地指针先于 release 校验生效。"""
    if not os.path.exists(releases_dir):
        raise InstallerFault("INVALID_RELEASE", "releases 目录不存在")
    root = os.path.realpath(os.path.abspath(releases_dir))
    path = os.path.join(root, "current.json")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise InstallerFault("INVALID_RELEASE", "current.json 不存在")
    except OSError as e:
        raise InstallerFault("INVALID_RELEASE", f"读取 current.json 失败: {e}")
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_size > MAX_CURRENT_POINTER_BYTES:
        raise InstallerFault("INVALID_RELEASE", "current.json 不是有效的受管指针")
    actual = os.path.realpath(path)
    if not is_contained_path(root, actual):
        raise InstallerFault("INVALID_RELEASE", "current.json 越出 releases 目录")
    try:
        with open(path, "r", encoding="utf-8") as f:
            pointer = json.load(f)
    except Exception:
        raise InstallerFault("INVALID_RELEASE", "current.json 不是有效 JSON")
    if not isinstance(pointer, dict):
        raise InstallerFault("INVALID_RELEASE", "current.json 内容非法")
    release_id = str(pointer.get("release_id") or "")
    if not RELEASE_ID_PATTERN.match(release_id):
        raise InstallerFault("INVALID_RELEASE", "current.json 内容非法")
    data_version = pointer.get("data_version")
    if data_version is not None and not SHA256_PATTERN.match(str(data_version)):
        raise InstallerFault("INVALID_RELEASE", "current.json 内容非法")
    res = dict(pointer)
    res["release_id"] = release_id
    return res


def normalize_pack_descriptor(descriptor: Any, label: str, code: str = "INVALID_MANIFEST") -> dict[str, Any]:
    if not isinstance(descriptor, dict) or any(key not in PACK_DESCRIPTOR_FIELDS for key in descriptor):
        raise InstallerFault(code, f"{label} pack 描述包含非法字段")
    pack_id = _require_pack_id(str(descriptor.get("pack_id") or ""), code)
    authority = "official" if descriptor.get("authority") is None else str(descriptor.get("authority"))
    data_version = str(descriptor.get("data_version") or "")
    manifest_path = descriptor.get("manifest_path")
    if (
        manifest_path != f"{pack_id}/pack-manifest.json"
        or not SHA256_PATTERN.match(data_version)
        or not authority
        or len(authority) > 128
        or _has_control_or_format(authority)
    ):
        raise InstallerFault(code, f"{label} pack 描述非法: {pack_id}")
    return {
        "pack_id": pack_id,
        "manifest_path": manifest_path,
        "authority": authority,
        "data_version": data_version,
        "document_count": _require_integer(
            descriptor.get("document_count"),
            f"{label}.{pack_id}.document_count",
            minimum=1,
            maximum=10_000_000,
            code=code,
        ),
        "line_count": _require_integer(
            descriptor.get("line_count"),
            f"{label}.{pack_id}.line_count",
            maximum=100_000_000,
            code=code,
        ),
        "compressed_size": _require_integer(
            descriptor.get("compressed_size"),
            f"{label}.{pack_id}.compressed_size",
            minimum=1,
            maximum=CORPUS_RESOURCE_LIMITS["maxReleaseCompressedBytes"],
            code=code,
        ),
        "uncompressed_size": _require_integer(
            descriptor.get("uncompressed_size"),
            f"{label}.{pack_id}.uncompressed_size",
            minimum=1,
            maximum=CORPUS_RESOURCE_LIMITS["maxReleaseUncompressedBytes"],
            code=code,
        ),
        "shard_count": _require_integer(
            descriptor.get("shard_count"),
            f"{label}.{pack_id}.shard_count",
            minimum=1,
            maximum=CORPUS_RESOURCE_LIMITS["maxAssets"],
            code=code,
        ),
    }


def validate_release_header(release_id: str, manifest: dict[str, Any], code: str = "INVALID_MANIFEST") -> dict[str, dict[str, Any]]:
    if (
        manifest.get("algorithm") != RELEASE_ALGORITHM
        or manifest.get("schema_version") != 1
        or manifest.get("release_id") != release_id
        or not SHA256_PATTERN.match(str(manifest.get("data_version") or ""))
        or not isinstance(manifest.get("required_packs"), list)
        or len(manifest["required_packs"]) == 0
        or len(manifest["required_packs"]) > len(PACK_IDS)
        or not isinstance(manifest.get("packs"), list)
        or len(manifest["packs"]) == 0
        or len(manifest["packs"]) > len(PACK_IDS)
    ):
        raise InstallerFault(code, "release-manifest 内容不完整或与 releaseId 不匹配")

    minimum_agent_version = manifest.get("minimum_agent_version")
    if not parse_semver(minimum_agent_version):
        raise InstallerFault(code, "release-manifest minimum_agent_version 缺失或不是有效 SemVer")
    if not parse_semver(AGENT_VERSION):
        raise InstallerFault("INCOMPATIBLE_RELEASE", f"插件自身版本不是有效 SemVer: {AGENT_VERSION}")
    if compare_semver(AGENT_VERSION, minimum_agent_version) < 0:
        raise InstallerFault(
            "INCOMPATIBLE_RELEASE",
            f"资料版本至少需要 prts-terrarchive {minimum_agent_version}，当前为 {AGENT_VERSION}",
        )

    descriptors: dict[str, dict[str, Any]] = {}
    for raw_descriptor in manifest["packs"]:
        descriptor = normalize_pack_descriptor(raw_descriptor, "release", code)
        pack_id = descriptor["pack_id"]
        if pack_id in descriptors:
            raise InstallerFault(code, f"release pack 描述非法: {pack_id}")
        descriptors[pack_id] = descriptor

    required: set[str] = set()
    for value in manifest["required_packs"]:
        pack_id = _require_pack_id(str(value or ""), code)
        if pack_id in required or pack_id not in descriptors:
            raise InstallerFault(code, f"required pack 描述非法: {pack_id}")
        required.add(pack_id)
    return descriptors


def localization_assets(pack: dict[str, Any], fail: Callable[[str], None]) -> list[dict[str, Any]]:
    value = pack.get("localization")
    if value is None:
        return []
    if (
        not isinstance(value, dict)
        or value.get("algorithm") != LOCALIZATION_ALGORITHM
        or value.get("schema_version") != 1
        or value.get("game") != "endfield"
        or value.get("source_language") != "CN"
        or not isinstance(value.get("game_version"), str)
        or not value.get("game_version")
        or value.get("game_version") != pack.get("game_version")
        or not isinstance(value.get("text_count"), int)
        or isinstance(value.get("text_count"), bool)
        or value["text_count"] < 1
        or value["text_count"] > 1_000_000
        or not isinstance(value.get("catalog"), dict)
        or value.get("catalog", {}).get("path") != "localization/catalog.jsonl.gz"
        or not isinstance(value.get("languages"), dict)
        or isinstance(value.get("languages"), list)
        or "CN" not in value["languages"]
        or any(
            lang not in LANGUAGE_CODES
            or not isinstance(value["languages"][lang], dict)
            or value["languages"][lang].get("path") != f"localization/{lang}.jsonl.gz"
            for lang in value["languages"]
        )
    ):
        fail("官方本地化附件元数据无效或版本不一致")
    return [value["catalog"]] + list(value["languages"].values())


def validate_pack_manifest(
    pack_id: str,
    pack: dict[str, Any],
    descriptor: dict[str, Any] | None,
    totals: dict[str, int],
    code: str = "INVALID_MANIFEST",
) -> list[dict[str, Any]]:
    expected_schema = PACK_ALGORITHMS.get(str(pack.get("algorithm") or ""))
    if (
        not expected_schema
        or pack.get("schema_version") != expected_schema
        or (pack.get("pack_id") is not None and pack.get("pack_id") != pack_id)
        or not SHA256_PATTERN.match(str(pack.get("data_version") or ""))
        or not isinstance(pack.get("shards"), list)
        or len(pack["shards"]) == 0
    ):
        raise InstallerFault(code, f"pack-manifest 内容不完整: {pack_id}")

    search_index = pack.get("search_index")
    if search_index is not None and (
        not isinstance(search_index, dict) or not isinstance(search_index.get("shards"), list)
    ):
        raise InstallerFault(code, f"pack-manifest search_index 非法: {pack_id}")

    if search_index and search_index.get("shards"):
        algo = search_index.get("algorithm")
        if not SUPPORTED_SEARCH_INDEX_ALGORITHMS.get(algo):
            raise InstallerFault(
                "INCOMPATIBLE_RELEASE",
                f"{pack_id} 使用当前插件不支持的检索索引算法: {algo or '未声明'}",
            )
        v1 = (
            algo == "prts-browser-trigram-postings-v1"
            and search_index.get("schema_version") == 1
            and search_index.get("normalization") == "unicode-nfc-casefold"
            and search_index.get("gram_size") == 3
            and search_index.get("format") == "varint-postings-le-v1"
        )
        v2 = (
            algo == "prts-browser-ngram-postings-v2"
            and search_index.get("schema_version") == 2
            and search_index.get("normalization")
            in ("unicode-nfkc-casefold-collapse-space", "unicode-nfkc-lower-collapse-space")
            and search_index.get("gram_sizes") == [1, 2, 3]
            and search_index.get("format") == "varint-postings-le-v2"
        )
        if not v1 and not v2:
            raise InstallerFault(code, f"pack-manifest search_index 版本或范围非法: {pack_id}")

        for shard in search_index["shards"]:
            first = shard.get("first_ngram") if v2 else shard.get("first_trigram")
            last = shard.get("last_ngram") if v2 else shard.get("last_trigram")
            minimum = 1 if v2 else 3
            if (
                not isinstance(first, str)
                or not isinstance(last, str)
                or len(first) < minimum
                or len(first) > 3
                or len(last) < minimum
                or len(last) > 3
                or first.encode("utf-8") > last.encode("utf-8")
            ):
                raise InstallerFault(code, f"pack-manifest search_index 版本或范围非法: {pack_id}")

    document_count = _require_integer(
        pack.get("document_count"),
        f"{pack_id}.document_count",
        minimum=1,
        maximum=10_000_000,
        code=code,
    )
    line_count = _require_integer(
        pack.get("line_count"),
        f"{pack_id}.line_count",
        maximum=100_000_000,
        code=code,
    )
    search_shards = search_index.get("shards") if search_index and isinstance(search_index, dict) else []
    search_shards = search_shards if isinstance(search_shards, list) else []

    catalog = pack.get("document_catalog")
    if catalog is not None and (
        not isinstance(catalog, dict)
        or catalog.get("algorithm") != "prts-browser-document-catalog-v1"
        or catalog.get("schema_version") != 1
        or catalog.get("document_count") != document_count
    ):
        raise InstallerFault(code, f"pack-manifest document_catalog 非法: {pack_id}")
    catalog_assets = [catalog] if catalog else []

    if pack.get("localization") and pack_id != "endfield_official_game":
        raise InstallerFault(code, "本地化附件只能属于终末地官方资料")
    localized_assets = localization_assets(pack, lambda msg: (_ for _ in ()).throw(InstallerFault(code, msg)))

    total_asset_count = len(pack["shards"]) + len(search_shards) + len(catalog_assets) + len(localized_assets)
    if (
        total_asset_count > CORPUS_RESOURCE_LIMITS["maxAssets"]
        or totals["assets"] + total_asset_count > CORPUS_RESOURCE_LIMITS["maxAssets"]
    ):
        raise InstallerFault(code, "资料 release 的资源文件数超过上限")

    assets_with_kind = (
        [(a, "shards/") for a in pack["shards"]]
        + [(a, "search-index/") for a in search_shards]
        + [(a, "catalog/") for a in catalog_assets]
        + [(a, "localization/") for a in localized_assets]
    )

    paths: set[str] = set()
    compressed_size = 0
    uncompressed_size = 0
    for asset, kind in assets_with_kind:
        asset_path = str(asset.get("path") or "")
        comp = asset.get("compressed_size")
        uncomp = asset.get("uncompressed_size")
        exp_hash = str(asset.get("sha256") or "")
        if (
            not ASSET_PATH_PATTERN.match(asset_path)
            or not asset_path.startswith(kind)
            or asset_path in paths
            or not SHA256_PATTERN.match(exp_hash)
        ):
            raise InstallerFault(code, f"分片描述非法: {pack_id}/{asset_path}")
        _require_integer(
            comp,
            f"{pack_id}/{asset_path}.compressed_size",
            minimum=1,
            maximum=CORPUS_RESOURCE_LIMITS["maxAssetCompressedBytes"],
            code=code,
        )
        _require_integer(
            uncomp,
            f"{pack_id}/{asset_path}.uncompressed_size",
            minimum=1,
            maximum=CORPUS_RESOURCE_LIMITS["maxAssetUncompressedBytes"],
            code=code,
        )
        paths.add(asset_path)
        compressed_size += comp
        uncompressed_size += uncomp

    totals["assets"] += len(assets_with_kind)
    totals["compressed"] += compressed_size
    totals["uncompressed"] += uncompressed_size
    totals["documents"] += document_count
    totals["lines"] += line_count

    if (
        totals["assets"] > CORPUS_RESOURCE_LIMITS["maxAssets"]
        or totals["compressed"] > CORPUS_RESOURCE_LIMITS["maxReleaseCompressedBytes"]
        or totals["uncompressed"] > CORPUS_RESOURCE_LIMITS["maxReleaseUncompressedBytes"]
    ):
        raise InstallerFault(code, "资料 release 超过本地资源上限")

    if pack.get("compressed_size") != compressed_size or pack.get("uncompressed_size") != uncompressed_size:
        raise InstallerFault(code, f"pack-manifest 资源汇总不一致: {pack_id}")

    if descriptor:
        for field in ("data_version", "document_count", "line_count", "compressed_size", "uncompressed_size"):
            if descriptor.get(field) is not None and descriptor[field] != pack.get(field):
                raise InstallerFault(code, f"release 与 pack-manifest 的 {field} 不一致: {pack_id}")
        pack_authority = str(pack.get("authority") or "official")
        if (
            not pack_authority
            or len(pack_authority) > 128
            or _has_control_or_format(pack_authority)
            or descriptor.get("authority") != pack_authority
        ):
            raise InstallerFault(code, f"release 与 pack-manifest 的 authority 不一致: {pack_id}")
        if descriptor.get("shard_count") is not None and descriptor["shard_count"] != len(assets_with_kind):
            raise InstallerFault(code, f"release 与 pack-manifest 的 shard_count 不一致: {pack_id}")

    return [a for a, _ in assets_with_kind]


def validate_release_totals(manifest: dict[str, Any], totals: dict[str, int], code: str = "INVALID_MANIFEST") -> None:
    fields = [
        ("document_count", "documents", 10_000_000),
        ("line_count", "lines", 100_000_000),
        ("compressed_size", "compressed", CORPUS_RESOURCE_LIMITS["maxReleaseCompressedBytes"]),
        ("uncompressed_size", "uncompressed", CORPUS_RESOURCE_LIMITS["maxReleaseUncompressedBytes"]),
    ]
    for field, total_key, maximum in fields:
        _require_integer(
            manifest.get(field),
            f"release.{field}",
            minimum=1 if field == "document_count" else 0,
            maximum=maximum,
            code=code,
        )
        if manifest.get(field) != totals[total_key]:
            raise InstallerFault(code, f"release.{field} 与 pack 汇总不一致")


def validate_trusted_release_root(
    manifest: dict[str, Any],
    pack_manifests: dict[str, dict[str, Any]],
    code: str = "INVALID_MANIFEST",
) -> None:
    """release.data_version 是构建器对 pack 逐文件哈希投影的内容根。"""
    compiler_version = str(manifest.get("compiler_version") or "")
    source_update_id = str(manifest.get("source_update_id") or "")
    snapshot_prefix = "local-snapshot:"
    if (
        not re.match(r"^[A-Za-z0-9._-]{1,128}$", compiler_version)
        or not source_update_id.startswith(snapshot_prefix)
        or not re.match(r"^[A-Za-z0-9._-]{1,160}$", source_update_id[len(snapshot_prefix):])
        or manifest.get("corpus_version") != manifest.get("data_version")
        or manifest.get("content_tree_sha256") != manifest.get("data_version")
    ):
        raise InstallerFault(code, "release 缺少可验证的内容根元数据")

    packs = []
    for descriptor in manifest.get("packs", []):
        pack_id = descriptor["pack_id"]
        pack = pack_manifests.get(pack_id)
        if not pack:
            raise InstallerFault(code, f"release 缺少 pack 清单: {pack_id}")
        authority = str(pack.get("authority") or "official")
        if pack.get("localization") and compare_semver(manifest.get("minimum_agent_version", "0.0.0"), "0.2.0") < 0:
            raise InstallerFault(code, "本地化资料必须声明 minimum_agent_version 至少为 0.2.0")
        if not authority or len(authority) > 128 or _has_control_or_format(authority):
            raise InstallerFault(code, f"pack authority 非法: {pack_id}")
        pack_item: dict[str, Any] = {
            "pack_id": pack_id,
            "data_version": pack["data_version"],
            "authority": authority,
            "shards": [{"path": asset["path"], "sha256": asset["sha256"]} for asset in pack["shards"]],
            "search_index_shards": [
                {"path": asset["path"], "sha256": asset["sha256"]}
                for asset in (pack.get("search_index", {}).get("shards") or [])
            ],
        }
        if pack.get("document_catalog"):
            pack_item["document_catalog"] = {
                "path": pack["document_catalog"]["path"],
                "sha256": pack["document_catalog"]["sha256"],
            }
        if pack.get("localization"):
            pack_item["localization"] = pack["localization"]
        packs.append(pack_item)

    root_obj = {
        "compiler_version": compiler_version,
        "source_snapshot": source_update_id[len(snapshot_prefix):],
        "packs": packs,
    }
    calculated = sha256_hex(canonical_json(root_obj))
    if calculated != manifest.get("data_version"):
        raise InstallerFault(code, "release data_version 无法约束 pack 逐文件哈希")


def sha256_file(path: str) -> str:
    """计算单个本地文件的 SHA-256 哈希值。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


async def validate_local_release(
    releases_dir: str,
    release_id: str,
    verify_hashes: bool = False,
    details: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    """验证一个本地 release 的全部声明 pack、资源路径、尺寸与汇总字段。"""
    if not release_id_valid(release_id):
        raise InstallerFault("INVALID_RELEASE", "releaseId 非法")
    releases_root = os.path.realpath(os.path.abspath(releases_dir))
    release_dir = await require_contained_directory(
        os.path.join(releases_root, release_id),
        releases_root,
        f"release {release_id}",
    )
    manifest = await read_contained_json(
        os.path.join(release_dir, "release-manifest.json"),
        release_dir,
        "release-manifest",
        "INVALID_RELEASE",
    )
    descriptors = validate_release_header(release_id, manifest, "INVALID_RELEASE")
    totals = {"assets": 0, "compressed": 0, "uncompressed": 0, "documents": 0, "lines": 0}
    pack_manifests: dict[str, dict[str, Any]] = {}
    loop = asyncio.get_running_loop()

    for pack_id, descriptor in descriptors.items():
        pack_dir = await require_contained_directory(
            os.path.join(release_dir, pack_id),
            release_dir,
            f"pack {pack_id}",
        )
        pack_path = os.path.join(pack_dir, "pack-manifest.json")
        pack = await read_contained_json(
            pack_path,
            release_dir,
            f"{pack_id}/pack-manifest.json",
            "INVALID_RELEASE",
        )
        assets = validate_pack_manifest(pack_id, pack, descriptor, totals, "INVALID_RELEASE")
        pack_manifests[pack_id] = pack

        for asset in assets:
            rel = str(asset.get("path") or "")
            category_name = rel.split("/")[0]
            category_dir = os.path.join(pack_dir, category_name)
            try:
                st_cat = os.lstat(category_dir)
                if not stat.S_ISDIR(st_cat.st_mode) or stat.S_ISLNK(st_cat.st_mode):
                    raise InstallerFault("INVALID_RELEASE", f"分片目录不是受管目录: {pack_id}/{rel}")
            except OSError:
                raise InstallerFault("INVALID_RELEASE", f"分片目录不是受管目录: {pack_id}/{rel}")

            asset_path = os.path.join(pack_dir, rel)
            try:
                st_asset = os.lstat(asset_path)
            except OSError:
                raise InstallerFault("INVALID_RELEASE", f"分片缺失或大小不符: {pack_id}/{rel}")

            if (
                not stat.S_ISREG(st_asset.st_mode)
                or stat.S_ISLNK(st_asset.st_mode)
                or st_asset.st_size != asset["compressed_size"]
            ):
                raise InstallerFault("INVALID_RELEASE", f"分片缺失或大小不符: {pack_id}/{rel}")

            actual_path = os.path.realpath(asset_path)
            if not is_contained_path(release_dir, actual_path):
                raise InstallerFault("INVALID_RELEASE", f"分片越出 release 目录: {pack_id}/{rel}")

            if verify_hashes:
                file_hash = await loop.run_in_executor(None, sha256_file, asset_path)
                if file_hash != asset["sha256"]:
                    raise InstallerFault("INVALID_RELEASE", f"分片 SHA-256 不符: {pack_id}/{rel}")

    validate_release_totals(manifest, totals, "INVALID_RELEASE")
    validate_trusted_release_root(manifest, pack_manifests, "INVALID_RELEASE")
    return (manifest, pack_manifests, release_dir) if details else manifest


class _LeaseInfo:
    def __init__(self, name: str, path: str, pid: int, process_start: str | None, ticket: int | None):
        self.name = name
        self.path = path
        self.pid = pid
        self.process_start = process_start
        self.ticket = ticket


LEASE_NAME_PATTERN = re.compile(r"^lease-(\d+)-(\d+|na)-([0-9a-f]{24})$")


def _linux_process_start(pid: int) -> str | None:
    if sys.platform != "linux" or pid < 1:
        return None
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            content = f.read()
        suffix = content[content.rfind(") ") + 2:].strip().split()
        return suffix[19] if len(suffix) > 19 else None
    except Exception:
        return None


def _process_gone(pid: int, recorded_start: str | None = None) -> bool | None:
    """探测对应进程是否已经退出；返回 None 表示无法探测。"""
    if sys.platform == "win32":
        try:
            os.kill(pid, 0)
            return False
        except OSError as e:
            if getattr(e, "winerror", None) == 87 or getattr(e, "errno", None) == errno.ESRCH:
                return True
            if getattr(e, "winerror", None) == 5 or getattr(e, "errno", None) == errno.EPERM:
                return False
            return None
    else:
        try:
            os.kill(pid, 0)
            if recorded_start and sys.platform == "linux":
                current_start = _linux_process_start(pid)
                if current_start and current_start != recorded_start:
                    return True
            return False
        except OSError as e:
            if e.errno == errno.ESRCH:
                return True
            if e.errno == errno.EPERM:
                return False
            return None


def _should_reclaim_lease(lease: _LeaseInfo) -> bool:
    probe = _process_gone(lease.pid, lease.process_start)
    if probe is True:
        return True
    if probe is False:
        return False
    # 无法探测时回退到时间戳超时（锁生命周期超过 120 秒认为陈旧）
    try:
        st = os.stat(lease.path)
        return (time.time() - st.st_mtime) > 120
    except OSError:
        return True


async def _read_lease(lock_dir: str, name: str) -> _LeaseInfo | None:
    match = LEASE_NAME_PATTERN.match(name)
    if not match:
        return None
    path = os.path.join(lock_dir, name)
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return None
        content = ""
        if st.st_size <= 64:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().strip()
        ticket = int(content) if re.match(r"^[1-9]\d{0,15}$", content) else None
        process_start = None if match.group(2) == "na" else match.group(2)
        return _LeaseInfo(name, path, int(match.group(1)), process_start, ticket)
    except FileNotFoundError:
        return None
    except OSError:
        return None


async def _active_leases(lock_dir: str) -> list[_LeaseInfo]:
    try:
        names = os.listdir(lock_dir)
    except OSError:
        return []
    leases = []
    for name in names:
        lease = await _read_lease(lock_dir, name)
        if lease:
            leases.append(lease)
    active = []
    for lease in leases:
        if _should_reclaim_lease(lease):
            try:
                os.unlink(lease.path)
            except OSError:
                pass
        else:
            active.append(lease)
    return active


async def acquire_release_mutation_lock(releases_dir: str) -> Callable[[], Any]:
    """获取 Lamport bakery 式的唯一 lease 文件变更锁。"""
    abs_releases_dir = os.path.realpath(os.path.abspath(releases_dir))
    os.makedirs(abs_releases_dir, mode=0o700, exist_ok=True)
    releases_root = os.path.realpath(abs_releases_dir)
    lock_dir_path = os.path.join(releases_root, ".release-mutation-locks")
    os.makedirs(lock_dir_path, mode=0o700, exist_ok=True)
    lock_dir = await require_contained_directory(lock_dir_path, releases_root, "资料变更锁目录", "INVALID_RELEASE")

    process_start = _linux_process_start(os.getpid())
    token = secrets.token_hex(12)
    name = f"lease-{os.getpid()}-{process_start or 'na'}-{token}"
    path = os.path.join(lock_dir, name)

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o600)
    file_obj = os.fdopen(fd, "r+b", buffering=0)

    deadline = time.time() + 30.0
    try:
        file_obj.write(b"choosing\n")
        file_obj.flush()
        try:
            os.fsync(file_obj.fileno())
        except OSError:
            pass

        existing = await _active_leases(lock_dir)
        maximum = max([l.ticket for l in existing if l.ticket is not None], default=0)
        ticket = maximum + 1

        file_obj.seek(0)
        file_obj.truncate(0)
        file_obj.write(f"{ticket}\n".encode("utf-8"))
        file_obj.flush()
        try:
            os.fsync(file_obj.fileno())
        except OSError:
            pass

        while True:
            leases = await _active_leases(lock_dir)
            blocked = any(
                l.name != name
                and (
                    l.ticket is None
                    or l.ticket < ticket
                    or (l.ticket == ticket and l.name < name)
                )
                for l in leases
            )
            if not blocked:
                async def release_fn():
                    try:
                        file_obj.close()
                    except Exception:
                        pass
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

                return release_fn

            if time.time() >= deadline:
                raise InstallerFault("DOWNLOAD_BUSY", "另一个进程正在下载、激活或删除资料版本")
            await asyncio.sleep(0.1)
    except Exception:
        try:
            file_obj.close()
        except Exception:
            pass
        try:
            os.unlink(path)
        except Exception:
            pass
        raise


class ReleaseMutationLockContext:
    """跨 Host 进程串行化 release 指针与目录变更的异步上下文管理器与 awaitable 封装。"""

    def __init__(self, releases_dir: str, operation: Any = None):
        self.releases_dir = releases_dir
        self.operation = operation
        self._release_callback: Callable[[], Any] | None = None

    async def __aenter__(self):
        self._release_callback = await acquire_release_mutation_lock(self.releases_dir)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._release_callback:
            callback = self._release_callback
            self._release_callback = None
            res = callback()
            if inspect.isawaitable(res):
                await res

    def __await__(self):
        async def _run():
            async with self:
                if callable(self.operation):
                    res = self.operation()
                    if inspect.isawaitable(res):
                        return await res
                    return res
                return None
        return _run().__await__()


def with_release_mutation_lock(releases_dir: str, operation: Any = None) -> ReleaseMutationLockContext:
    """跨 Host 进程串行化 release 指针与目录变更。"""
    return ReleaseMutationLockContext(releases_dir, operation)


async def current_release_ready(releases_dir: str, requested: str, expected_data_version: str | None = None) -> bool:
    """检查指定 release 是否已在本地完整校验并处于激活状态。"""
    try:
        pointer = await read_current_release_pointer(releases_dir)
        release_id = pointer.get("release_id")
        if release_id != requested:
            return False
        manifest = await validate_local_release(releases_dir, release_id, verify_hashes=True)
        pointer_dv = pointer.get("data_version")
        manifest_dv = manifest.get("data_version")
        if pointer_dv and pointer_dv != manifest_dv:
            return False
        if expected_data_version and manifest_dv != expected_data_version:
            return False
        return True
    except Exception:
        return False


def _is_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    if hasattr(signal, "is_set") and callable(signal.is_set):
        return signal.is_set()
    if hasattr(signal, "aborted"):
        return bool(signal.aborted)
    return False


@asynccontextmanager
async def _ensure_session(session: aiohttp.ClientSession | None):
    if session is not None:
        yield session
    else:
        async with aiohttp.ClientSession() as s:
            yield s


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    signal: Any = None,
    timeout_seconds: float = MANIFEST_TIMEOUT_SECONDS,
) -> Any:
    """请求 JSON 清单，实施超时与有界响应截断。"""
    if _is_aborted(signal):
        raise InstallerFault("CANCELLED", "资料下载已取消")

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    parsed = urlsplit(url)
    try:
        async with session.get(url, allow_redirects=False, timeout=timeout) as response:
            if response.status in (301, 302, 303, 307, 308):
                raise InstallerFault("DOWNLOAD_FAILED", f"清单请求重定向被禁止: {url}")
            if response.status != 200:
                code = "RELEASE_NOT_FOUND" if response.status == 404 else (
                    "ACCESS_DENIED" if response.status == 403 else "DOWNLOAD_FAILED"
                )
                raise InstallerFault(code, f"请求失败 HTTP {response.status}: {url}")

            content_length_str = response.headers.get("Content-Length")
            if content_length_str and content_length_str.isdigit():
                declared = int(content_length_str)
                if declared > MAX_MANIFEST_BYTES:
                    raise InstallerFault("INVALID_MANIFEST", f"清单超过大小上限（{MAX_MANIFEST_BYTES} 字节）: {url}")

            received = 0
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                if _is_aborted(signal):
                    raise InstallerFault("CANCELLED", "资料下载已取消")
                received += len(chunk)
                if received > MAX_MANIFEST_BYTES:
                    raise InstallerFault("INVALID_MANIFEST", f"响应超过大小上限（{MAX_MANIFEST_BYTES} 字节）: {url}")
                chunks.extend(chunk)

            try:
                return json.loads(chunks.decode("utf-8"))
            except Exception:
                raise InstallerFault("INVALID_MANIFEST", f"返回的不是有效 JSON: {url}")
    except asyncio.TimeoutError:
        raise InstallerFault("DOWNLOAD_FAILED", f"连接 {parsed.netloc} 超时（{int(timeout_seconds)}s）")
    except InstallerFault:
        raise
    except Exception as err:
        if _is_aborted(signal):
            raise InstallerFault("CANCELLED", "资料下载已取消")
        raise InstallerFault("DOWNLOAD_FAILED", f"无法连接 {parsed.netloc}（{err}）")


def safe_download_redirect(next_url: SplitResult, initial_url: SplitResult) -> bool:
    """验证重定向目标地址安全性。"""
    def modelscope_origin_host(hostname: str) -> bool:
        return bool(re.match(r"^(?:www\.)?modelscope\.cn$", hostname.lower()))

    def modelscope_lfs_host(hostname: str) -> bool:
        return bool(re.match(r"^cdn-lfs-[a-z0-9-]+\.modelscope\.cn$", hostname.lower()))

    if next_url.username or next_url.password or next_url.fragment:
        return False
    if next_url.scheme != initial_url.scheme:
        return False
    next_host = (next_url.hostname or "").lower()
    initial_host = (initial_url.hostname or "").lower()
    next_origin = (next_url.scheme, next_url.netloc)
    initial_origin = (initial_url.scheme, initial_url.netloc)

    if next_origin == initial_origin:
        return not next_url.query or modelscope_lfs_host(next_host)

    return (
        next_url.scheme == "https"
        and modelscope_origin_host(initial_host)
        and modelscope_lfs_host(next_host)
    )


def download_timeout_seconds(size: int | None) -> float:
    if isinstance(size, int) and size > 0:
        return min(15 * 60, max(60, 60 + math.ceil(size / 65536)))
    return 60.0


async def _fetch_download(session: aiohttp.ClientSession, url: str, timeout: aiohttp.ClientTimeout, signal: Any = None):
    initial = urlsplit(url)
    current_url = url
    for redirects in range(6):
        if _is_aborted(signal):
            raise InstallerFault("CANCELLED", "资料下载已取消")
        response = await session.get(current_url, allow_redirects=False, timeout=timeout)
        if response.status not in (301, 302, 303, 307, 308):
            return response

        location = response.headers.get("Location")
        if not location or redirects == 5:
            response.close()
            raise InstallerFault("DOWNLOAD_FAILED", f"下载跳转链非法或过长: {initial.netloc}")
        next_full_url = urljoin(current_url, location)
        next_parsed = urlsplit(next_full_url)
        if not safe_download_redirect(next_parsed, initial):
            response.close()
            raise InstallerFault("DOWNLOAD_FAILED", f"下载源试图跳转到不安全地址: {next_parsed.netloc}")
        response.close()
        current_url = next_full_url
    raise InstallerFault("DOWNLOAD_FAILED", f"下载跳转链过长: {initial.netloc}")


async def download_verified(
    session: aiohttp.ClientSession,
    url: str,
    target_path: str,
    expected_size: int | None,
    expected_sha256: str | None,
    signal: Any = None,
    timeout_seconds: float | None = None,
) -> int:
    """下载单个文件到临时路径并按预期哈希与体积校验，通过后原子重命名。"""
    timeout_sec = timeout_seconds if timeout_seconds is not None else download_timeout_seconds(expected_size)
    client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
    size_limit = expected_size if expected_size is not None else MAX_UNVERIFIED_BYTES

    for retry in range(2):
        temp_path = f"{target_path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        response = None
        file_handle = None
        try:
            response = await _fetch_download(session, url, client_timeout, signal=signal)
            if response.status != 200:
                raise InstallerFault("DOWNLOAD_FAILED", f"下载失败 HTTP {response.status}: {url}")

            content_length_str = response.headers.get("Content-Length")
            if content_length_str and content_length_str.isdigit():
                declared = int(content_length_str)
                if expected_size is not None and declared != expected_size:
                    raise InstallerFault(
                        "CHECKSUM_MISMATCH",
                        f"{url} 大小不符（期望 {expected_size}，Content-Length {declared}）",
                    )
                if expected_size is None and declared > size_limit:
                    raise InstallerFault(
                        "DOWNLOAD_FAILED",
                        f"{url} 超过未校验文件大小上限（{size_limit} 字节）",
                    )

            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(temp_path, flags, 0o600)
            file_handle = os.fdopen(fd, "wb", buffering=0)

            hasher = hashlib.sha256() if expected_sha256 else None
            received = 0
            async for chunk in response.content.iter_chunked(65536):
                if _is_aborted(signal):
                    raise InstallerFault("CANCELLED", "资料下载已取消")
                received += len(chunk)
                if received > size_limit:
                    if expected_size is not None:
                        raise InstallerFault(
                            "CHECKSUM_MISMATCH",
                            f"{url} 大小不符（期望 {expected_size}，已接收 {received}）",
                        )
                    else:
                        raise InstallerFault(
                            "DOWNLOAD_FAILED",
                            f"{url} 超过未校验文件大小上限（{size_limit} 字节，已接收 {received}）",
                        )
                file_handle.write(chunk)
                if hasher:
                    hasher.update(chunk)

            if expected_size is not None and received != expected_size:
                raise InstallerFault(
                    "CHECKSUM_MISMATCH",
                    f"{url} 大小不符（期望 {expected_size}，得到 {received}）",
                )
            if hasher and hasher.hexdigest() != expected_sha256:
                raise InstallerFault("CHECKSUM_MISMATCH", f"{url} sha256 不符")

            file_handle.close()
            file_handle = None
            response.close()
            response = None

            os.replace(temp_path, target_path)
            return received
        except Exception as error:
            if file_handle:
                try:
                    file_handle.close()
                except Exception:
                    pass
            if response:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            if _is_aborted(signal):
                raise InstallerFault("CANCELLED", "资料下载已取消")
            if isinstance(error, InstallerFault):
                raise error
            if retry == 1:
                if isinstance(error, asyncio.TimeoutError):
                    raise InstallerFault("DOWNLOAD_FAILED", f"下载超时（{int(timeout_sec)}s）: {url}")
                raise InstallerFault("DOWNLOAD_FAILED", f"下载失败（{error}）: {url}")
            await asyncio.sleep(0.5)

    raise InstallerFault("DOWNLOAD_FAILED", f"下载失败: {url}")


def validate_current_summary(data: Any) -> dict[str, Any]:
    release_id = str(data.get("release_id") or "")
    data_version = str(data.get("data_version") or "")
    if (
        not RELEASE_ID_PATTERN.match(release_id)
        or not SHA256_PATTERN.match(data_version)
        or not isinstance(data.get("packs"), list)
        or len(data["packs"]) == 0
        or len(data["packs"]) > len(PACK_IDS)
    ):
        raise InstallerFault("INVALID_MANIFEST", "PRTS.chat current 元数据不完整")

    minimum_agent_version = data.get("minimum_agent_version")
    if not parse_semver(minimum_agent_version):
        raise InstallerFault("INVALID_MANIFEST", "PRTS.chat current.minimum_agent_version 缺失或不是有效 SemVer")
    if compare_semver(AGENT_VERSION, minimum_agent_version) < 0:
        raise InstallerFault(
            "INCOMPATIBLE_RELEASE",
            f"最新资料至少需要 prts-terrarchive {minimum_agent_version}，当前为 {AGENT_VERSION}",
        )

    _require_integer(data.get("document_count"), "current.document_count", minimum=1, maximum=10_000_000)
    _require_integer(data.get("line_count"), "current.line_count", maximum=100_000_000)
    _require_integer(
        data.get("compressed_size"),
        "current.compressed_size",
        maximum=CORPUS_RESOURCE_LIMITS["maxReleaseCompressedBytes"],
    )
    _require_integer(
        data.get("uncompressed_size"),
        "current.uncompressed_size",
        maximum=CORPUS_RESOURCE_LIMITS["maxReleaseUncompressedBytes"],
    )

    pack_ids: set[str] = set()
    packs = []
    totals = {"document_count": 0, "line_count": 0, "compressed_size": 0, "uncompressed_size": 0}
    for raw_descriptor in data["packs"]:
        descriptor = normalize_pack_descriptor(raw_descriptor, "current")
        pack_id = descriptor["pack_id"]
        if pack_id in pack_ids:
            raise InstallerFault("INVALID_MANIFEST", f"PRTS.chat current pack 描述非法: {pack_id}")
        for field in totals:
            totals[field] += descriptor[field]
        pack_ids.add(pack_id)
        packs.append(descriptor)

    for field in totals:
        if totals[field] != data.get(field):
            raise InstallerFault("INVALID_MANIFEST", f"PRTS.chat current.{field} 与 pack 汇总不一致")

    return {
        "release_id": release_id,
        "data_version": data_version,
        "minimum_agent_version": minimum_agent_version,
        "packs": packs,
    }


def validate_mirror_descriptors(
    mirrors: Any,
    release_id: str,
    release_packs: set[str],
) -> list[dict[str, Any]]:
    if mirrors is None:
        return []
    if not isinstance(mirrors, list) or len(mirrors) > 16:
        raise InstallerFault("INVALID_MANIFEST", "PRTS.chat 镜像列表非法")

    accepted = []
    assigned_packs: set[str] = set()
    for mirror in mirrors:
        try:
            base = urlsplit(str(mirror.get("base_url") or mirror.get("baseUrl") or ""))
        except Exception:
            raise InstallerFault("INVALID_MANIFEST", "PRTS.chat 镜像地址非法")
        raw_pack_ids = mirror.get("pack_ids") if mirror.get("pack_ids") is not None else mirror.get("packIds")
        pack_ids = [_require_pack_id(str(v or "")) for v in raw_pack_ids] if isinstance(raw_pack_ids, list) else []

        if (
            mirror.get("provider") != "modelscope"
            or not pack_ids
            or len(set(pack_ids)) != len(pack_ids)
            or base.scheme != "https"
            or base.hostname not in ("modelscope.cn", "www.modelscope.cn")
            or base.username
            or base.password
            or base.query
            or base.fragment
            or not base.path.endswith(f"/releases/{release_id}/")
        ):
            raise InstallerFault("INVALID_MANIFEST", "PRTS.chat 镜像描述非法")

        if any(p not in release_packs or p in assigned_packs for p in pack_ids):
            raise InstallerFault("INVALID_MANIFEST", "PRTS.chat 镜像 pack 范围非法或重复")

        assigned_packs.update(pack_ids)
        clean_url = urlunsplit(base)
        accepted.append({
            "base_url": clean_url,
            "baseUrl": clean_url,
            "pack_ids": pack_ids,
            "packIds": pack_ids,
        })
    return accepted


async def resolve_trusted_current_release(
    session: aiohttp.ClientSession | None = None,
    site_base_url: str | None = None,
    signal: Any = None,
) -> dict[str, Any]:
    """从 PRTS.chat 的可变 current 指针读取唯一可信的最新 release 与内容摘要。"""
    trusted_origin = DEFAULT_SITE_BASE_URL
    if site_base_url is not None:
        normalized_site = normalize_site_base_url(site_base_url)
        parsed_site = urlsplit(normalized_site)
        is_loopback = (parsed_site.hostname or "").lower() in ("localhost", "127.0.0.1", "::1", "[::1]")
        if normalized_site != DEFAULT_SITE_BASE_URL and not is_loopback:
            raise InstallerFault(
                "INVALID_REQUEST",
                f"可信元数据源固定为 {DEFAULT_SITE_BASE_URL}；siteBaseUrl 仅可配置字节回退源",
            )
        if is_loopback:
            trusted_origin = normalized_site

    async with _ensure_session(session) as s:
        payload = await fetch_json(s, f"{trusted_origin}/api/agent/data/releases/current", signal=signal)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data or not isinstance(data, dict):
            raise InstallerFault("INVALID_MANIFEST", "PRTS.chat current 响应缺少 data 字段")

        summary = validate_current_summary(data)
        mirrors = validate_mirror_descriptors(
            data.get("mirrors"),
            summary["release_id"],
            {p["pack_id"] for p in summary["packs"]},
        )

        res = {
            "release_id": summary["release_id"],
            "releaseId": summary["release_id"],
            "data_version": summary["data_version"],
            "dataVersion": summary["data_version"],
            "minimum_agent_version": summary["minimum_agent_version"],
            "minimumAgentVersion": summary["minimum_agent_version"],
            "document_count": data["document_count"],
            "documentCount": data["document_count"],
            "line_count": data["line_count"],
            "lineCount": data["line_count"],
            "compressed_size": data["compressed_size"],
            "compressedSize": data["compressed_size"],
            "uncompressed_size": data["uncompressed_size"],
            "uncompressedSize": data["uncompressed_size"],
            "packs": summary["packs"],
            "mirrors": mirrors,
        }
        _trusted_current_snapshots[id(res)] = dict(res)
        return res


async def resolve_modelscope_current_release(
    session: aiohttp.ClientSession | None = None,
    site_base_url: str | None = None,
    signal: Any = None,
) -> dict[str, Any]:
    """向后兼容接口：获取当前发布 release_id 与 data_version。"""
    trusted = await resolve_trusted_current_release(session=session, site_base_url=site_base_url, signal=signal)
    return {
        "release_id": trusted["release_id"],
        "data_version": trusted["data_version"],
        "releaseId": trusted["release_id"],
        "dataVersion": trusted["data_version"],
    }


def modelscope_asset_url(
    release_id: str,
    data_version: str,
    relative_path: str,
    mirror_base_url: str | None = None,
) -> str:
    """按分仓组合关系生成 ModelScope 分片资源的下载 URL。"""
    pack_id = _require_pack_id(str(relative_path or "").split("/")[0])
    composition = MODELSCOPE_RELEASE_COMPOSITIONS.get(release_id)
    if composition and composition.get("layout") == "legacy-three-dataset-v1":
        if pack_id == "official_game":
            group = "official"
        elif pack_id.startswith("endfield_"):
            group = "endfield"
        else:
            group = "community"
    else:
        group = "endfield" if pack_id.startswith("endfield_") else "arknights"

    if composition:
        comp_dv = composition.get("data_version") or composition.get("dataVersion")
        if comp_dv != data_version:
            raise InstallerFault("INVALID_MANIFEST", f"ModelScope 分仓组合与 release data_version 不匹配: {release_id}")

    releases_map = composition.get("releases", {}) if composition else {}
    source_release_id = releases_map.get(group, release_id)
    if mirror_base_url:
        base_url = mirror_base_url
    else:
        repo = MODELSCOPE_REPOS[group]
        base_url = f"https://modelscope.cn/datasets/{repo}/resolve/master/releases/{source_release_id}/"
    return urljoin(base_url, relative_path)


async def load_trusted_release_metadata(
    release_id: str,
    session: aiohttp.ClientSession,
    snapshot: dict[str, Any],
    site_base_url: str | None = None,
    signal: Any = None,
) -> dict[str, Any]:
    current = _trusted_current_snapshots.get(id(snapshot), snapshot)
    if not current or current.get("release_id") != release_id:
        raise InstallerFault("INVALID_REQUEST", "远程 release 必须由同一次 PRTS.chat current 快照选定")

    trusted_base = DEFAULT_SITE_BASE_URL
    if site_base_url is not None:
        norm = normalize_site_base_url(site_base_url)
        parsed = urlsplit(norm)
        if (parsed.hostname or "").lower() in ("localhost", "127.0.0.1", "::1", "[::1]"):
            trusted_base = norm

    def url_for(path: str) -> str:
        return f"{trusted_base}/api/agent/data/releases/{release_id}/{path}"

    release_manifest = await fetch_json(session, url_for("release-manifest.json"), signal=signal)
    descriptors = validate_release_header(release_id, release_manifest)

    fields = [
        ("data_version", current["data_version"]),
        ("minimum_agent_version", current["minimum_agent_version"]),
        ("document_count", current["document_count"]),
        ("line_count", current["line_count"]),
        ("compressed_size", current["compressed_size"]),
        ("uncompressed_size", current["uncompressed_size"]),
    ]
    for field, expected in fields:
        if release_manifest.get(field) != expected:
            raise InstallerFault("INVALID_MANIFEST", "PRTS.chat current 与 release-manifest 摘要不一致")

    current_packs = {p["pack_id"]: p for p in current["packs"]}
    if len(current_packs) != len(descriptors):
        raise InstallerFault("INVALID_MANIFEST", "PRTS.chat current 与 release-manifest packs 不一致")

    for pack_id, descriptor in descriptors.items():
        summary = current_packs.get(pack_id)
        if not summary or any(descriptor.get(k) != summary.get(k) for k in descriptor):
            raise InstallerFault("INVALID_MANIFEST", f"PRTS.chat current 与 release pack 不一致: {pack_id}")

    totals = {"assets": 0, "compressed": 0, "uncompressed": 0, "documents": 0, "lines": 0}
    pack_manifests: dict[str, dict[str, Any]] = {}
    entries = []

    for pack_id, descriptor in descriptors.items():
        pack = await fetch_json(session, url_for(descriptor["manifest_path"]), signal=signal)
        assets = validate_pack_manifest(pack_id, pack, descriptor, totals)
        pack_manifests[pack_id] = pack
        for asset in assets:
            entries.append({
                "relative_path": f"{pack_id}/{asset['path']}",
                "sha256": asset["sha256"],
                "size": asset["compressed_size"],
                "uncompressed_size": asset["uncompressed_size"],
            })

    validate_release_totals(release_manifest, totals)
    validate_trusted_release_root(release_manifest, pack_manifests)

    if len({e["relative_path"] for e in entries}) != len(entries):
        raise InstallerFault("INVALID_MANIFEST", "release 包含重复资源路径")

    return {
        "release_id": release_id,
        "data_version": release_manifest["data_version"],
        "release_manifest": release_manifest,
        "pack_manifests": pack_manifests,
        "entries": entries,
        "mirrors": current.get("mirrors", []),
    }


def list_from_site(trusted: dict[str, Any], site_base_url: str) -> dict[str, Any]:
    base = normalize_site_base_url(site_base_url)
    prefix = f"{base}/api/agent/data/releases/{trusted['release_id']}/"
    entries = [
        {**entry, "url": f"{prefix}{entry['relative_path']}"}
        for entry in trusted["entries"]
    ]
    return {
        "data_version": trusted["data_version"],
        "entries": entries,
    }


def list_from_modelscope(trusted: dict[str, Any]) -> dict[str, Any]:
    mirror_by_pack = {}
    for mirror in trusted.get("mirrors", []):
        for pack_id in mirror.get("pack_ids", mirror.get("packIds", [])):
            mirror_by_pack[pack_id] = mirror.get("base_url") or mirror.get("baseUrl")
    entries = [
        {
            **entry,
            "url": modelscope_asset_url(
                trusted["release_id"],
                trusted["data_version"],
                entry["relative_path"],
                mirror_base_url=mirror_by_pack.get(entry["relative_path"].split("/")[0]),
            ),
        }
        for entry in trusted["entries"]
    ]
    return {
        "data_version": trusted["data_version"],
        "entries": entries,
    }


async def prepare_asset_target(release_dir: str, relative_path: str) -> str:
    parts = relative_path.split("/")
    if len(parts) != 3:
        raise InstallerFault("INVALID_MANIFEST", f"资源路径非法: {relative_path}")
    pack_id, category, filename = parts
    if pack_id not in PACK_IDS or category not in ("shards", "search-index", "catalog", "localization") or not filename:
        raise InstallerFault("INVALID_MANIFEST", f"资源路径非法: {relative_path}")

    parent = release_dir
    for segment in (pack_id, category):
        child = os.path.join(parent, segment)
        try:
            os.mkdir(child, 0o700)
        except FileExistsError:
            pass
        except OSError as e:
            raise InstallerFault("INVALID_RELEASE", f"创建资源父目录失败: {e}")
        st = os.lstat(child)
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise InstallerFault("INVALID_RELEASE", f"资源父目录不是受管目录: {relative_path}")
        actual = os.path.realpath(child)
        if not is_contained_path(release_dir, actual):
            raise InstallerFault("INVALID_RELEASE", f"资源父目录越出 release: {relative_path}")
        parent = child

    target = os.path.join(parent, filename)
    actual_target = os.path.realpath(target) if os.path.exists(target) else os.path.realpath(parent)
    if not is_contained_path(release_dir, actual_target):
        raise InstallerFault("INVALID_RELEASE", f"资源路径越出 release: {relative_path}")
    return target


def write_json_atomically(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{secrets.token_hex(6)}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(temp_path, path)


async def ensure_managed_release_directory(releases_dir: str, release_id: str) -> tuple[str, str]:
    abs_releases_dir = os.path.realpath(os.path.abspath(releases_dir))
    os.makedirs(abs_releases_dir, mode=0o700, exist_ok=True)
    releases_root = os.path.realpath(abs_releases_dir)
    requested = os.path.join(releases_root, release_id)
    os.makedirs(requested, mode=0o700, exist_ok=True)
    release_dir = await require_contained_directory(requested, releases_root, f"release {release_id}", "INVALID_RELEASE")
    return releases_root, release_dir


async def ensure_corpus_release(
    *,
    releases_dir: str,
    release_id: str | None = None,
    data_version: str | None = None,
    enabled_games: Sequence[str] = ("arknights", "endfield"),
    download_order: Sequence[str] = ("modelscope", "site"),
    site_base_url: str = DEFAULT_SITE_BASE_URL,
    session: aiohttp.ClientSession | None = None,
    signal: Any = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """确保本地资料就绪：完整校验、多源回退下载与原子激活。"""
    if release_id is not None and not release_id_valid(release_id):
        raise InstallerFault("INVALID_REQUEST", "releaseId 非法")

    normalized_site = normalize_site_base_url(site_base_url)
    if (
        not download_order
        or any(s not in ("modelscope", "site") for s in download_order)
        or len(set(download_order)) != len(download_order)
    ):
        raise InstallerFault("INVALID_REQUEST", "download order 非法")

    async with with_release_mutation_lock(releases_dir):
        # 1. 检查本地显式 pin 是否已就绪（零网络快速返回）
        if release_id and await current_release_ready(releases_dir, release_id, expected_data_version=data_version):
            manifest = await validate_local_release(releases_dir, release_id, verify_hashes=False)
            if not missing_enabled_game_packs(manifest, enabled_games):
                return {
                    "release_id": release_id,
                    "data_version": manifest["data_version"],
                    "manifest": manifest,
                    "downloaded_bytes": 0,
                    "skipped_bytes": manifest.get("compressed_size", 0),
                    "reused": True,
                    "status": "present",
                }

        async with _ensure_session(session) as http_session:
            trusted_snapshot = await resolve_trusted_current_release(
                session=http_session,
                site_base_url=normalized_site,
                signal=signal,
            )
            current_release_id = trusted_snapshot["release_id"]
            current_data_version = trusted_snapshot["data_version"]

            if release_id is not None and release_id != current_release_id:
                raise InstallerFault(
                    "RELEASE_NOT_CURRENT",
                    f"PRTS.chat 最新版本为 {current_release_id}，拒绝远程下载未被 current 选定的版本",
                )
            if data_version is not None and data_version != current_data_version:
                raise InstallerFault(
                    "RELEASE_NOT_CURRENT",
                    f"PRTS.chat 当前版本哈希为 {current_data_version}，与期望 {data_version} 不符",
                )

            target_release_id = current_release_id
            if await current_release_ready(releases_dir, target_release_id, expected_data_version=current_data_version):
                manifest = await validate_local_release(releases_dir, target_release_id, verify_hashes=False)
                if not missing_enabled_game_packs(manifest, enabled_games):
                    return {
                        "release_id": target_release_id,
                        "data_version": manifest["data_version"],
                        "manifest": manifest,
                        "downloaded_bytes": 0,
                        "skipped_bytes": manifest.get("compressed_size", 0),
                        "reused": True,
                        "status": "present",
                    }

            trusted = await load_trusted_release_metadata(
                target_release_id,
                http_session,
                trusted_snapshot,
                site_base_url=normalized_site,
                signal=signal,
            )
            missing = missing_enabled_game_packs(trusted["release_manifest"], enabled_games)
            if missing:
                raise InstallerFault("INVALID_MANIFEST", f"资料版本缺少启用游戏所需数据包: {missing}")

            # 磁盘空间预检：总压缩体积 * 2.2
            compressed_total = trusted["release_manifest"].get("compressed_size", 0)
            required_space = int(compressed_total * 2.2)
            check_path = releases_dir
            while not os.path.exists(check_path) and os.path.dirname(check_path) != check_path:
                check_path = os.path.dirname(check_path)
            try:
                usage = shutil.disk_usage(check_path)
                if usage.free < required_space:
                    raise InstallerFault(
                        "INSUFFICIENT_STORAGE",
                        f"磁盘空间不足，需要至少 {required_space} 字节，剩余 {usage.free} 字节",
                    )
            except OSError:
                pass

            _, release_dir = await ensure_managed_release_directory(releases_dir, target_release_id)
            loop = asyncio.get_running_loop()
            last_error: Exception | None = None

            for source in download_order:
                if _is_aborted(signal):
                    raise InstallerFault("CANCELLED", "资料下载已取消")

                try:
                    listing = (
                        list_from_site(trusted, normalized_site)
                        if source == "site"
                        else list_from_modelscope(trusted)
                    )

                    pending = []
                    skipped_bytes = 0
                    for entry in listing["entries"]:
                        target_path = await prepare_asset_target(release_dir, entry["relative_path"])
                        try:
                            st_f = os.lstat(target_path)
                            if not stat.S_ISREG(st_f.st_mode) or stat.S_ISLNK(st_f.st_mode):
                                raise InstallerFault("INVALID_RELEASE", f"现有资源不是受管普通文件: {entry['relative_path']}")
                            if st_f.st_size == entry["size"]:
                                file_hash = await loop.run_in_executor(None, sha256_file, target_path)
                                if file_hash == entry["sha256"]:
                                    skipped_bytes += entry["size"]
                                    continue
                        except (FileNotFoundError, OSError):
                            pass
                        pending.append({**entry, "target_path": target_path})

                    if on_progress:
                        on_progress({
                            "phase": "downloading",
                            "source": source,
                            "release_id": target_release_id,
                            "files_done": 0,
                            "files_total": len(pending),
                            "bytes_done": 0,
                            "bytes_total": sum(e["size"] for e in pending),
                        })

                    downloaded_bytes_total = 0
                    files_done_count = 0
                    if pending:
                        queue: asyncio.Queue = asyncio.Queue()
                        for p in pending:
                            queue.put_nowait(p)

                        lock = asyncio.Lock()
                        failure_holder: list[Exception] = []
                        total_pending_bytes = sum(e["size"] for e in pending)

                        async def worker():
                            nonlocal downloaded_bytes_total, files_done_count
                            while not queue.empty():
                                if failure_holder or _is_aborted(signal):
                                    return
                                try:
                                    item = queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    return
                                try:
                                    dl_bytes = await download_verified(
                                        http_session,
                                        item["url"],
                                        item["target_path"],
                                        expected_size=item["size"],
                                        expected_sha256=item["sha256"],
                                        signal=signal,
                                        timeout_seconds=timeout,
                                    )
                                    async with lock:
                                        downloaded_bytes_total += dl_bytes
                                        files_done_count += 1
                                        cur_files = files_done_count
                                        cur_bytes = downloaded_bytes_total
                                    if on_progress:
                                        on_progress({
                                            "phase": "downloading",
                                            "source": source,
                                            "release_id": target_release_id,
                                            "files_done": cur_files,
                                            "files_total": len(pending),
                                            "bytes_done": cur_bytes,
                                            "bytes_total": total_pending_bytes,
                                        })
                                except Exception as e:
                                    failure_holder.append(e)
                                    return

                        worker_count = min(6, len(pending))
                        tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
                        await asyncio.gather(*tasks)

                        if failure_holder:
                            raise failure_holder[0]

                    if _is_aborted(signal):
                        raise InstallerFault("CANCELLED", "资料下载已取消")

                    if on_progress:
                        on_progress({
                            "phase": "done",
                            "source": source,
                            "release_id": target_release_id,
                            "files_done": files_done_count,
                            "files_total": len(pending),
                            "bytes_done": downloaded_bytes_total,
                            "bytes_total": sum(e["size"] for e in pending),
                        })

                    # 只落盘 PRTS.chat 预先验证过的清单；镜像返回的清单从不进入信任链
                    for pack_id, pack_manifest in trusted["pack_manifests"].items():
                        write_json_atomically(
                            os.path.join(release_dir, pack_id, "pack-manifest.json"),
                            pack_manifest,
                        )
                    write_json_atomically(
                        os.path.join(release_dir, "release-manifest.json"),
                        trusted["release_manifest"],
                    )

                    await validate_local_release(releases_dir, target_release_id, verify_hashes=True)

                    pointer_temp = os.path.join(releases_dir, f"current.json.{secrets.token_hex(6)}.tmp")
                    pointer_data = {
                        "release_id": target_release_id,
                        "data_version": trusted["data_version"],
                        "channel": source,
                        "public_download": True,
                        "schema_version": 1,
                        "downloaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    }
                    with open(pointer_temp, "w", encoding="utf-8") as f:
                        json.dump(pointer_data, f, indent=2)
                        f.write("\n")
                    os.replace(pointer_temp, os.path.join(releases_dir, "current.json"))

                    return {
                        "release_id": target_release_id,
                        "data_version": trusted["data_version"],
                        "manifest": trusted["release_manifest"],
                        "downloaded_bytes": downloaded_bytes_total,
                        "skipped_bytes": skipped_bytes,
                        "reused": False,
                        "status": "downloaded",
                    }
                except Exception as err:
                    if isinstance(err, InstallerFault) and err.code == "CANCELLED":
                        raise
                    if _is_aborted(signal):
                        raise InstallerFault("CANCELLED", "资料下载已取消")
                    last_error = err

            raise last_error or InstallerFault("DOWNLOAD_FAILED", "没有可用的下载源")
