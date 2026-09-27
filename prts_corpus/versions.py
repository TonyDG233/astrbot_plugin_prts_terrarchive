"""本地语料版本管理模块。

提供本地资料版本的枚举、状态检查、激活与安全删除。
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import stat
from typing import Any

from .constants import RELEASE_ID_PATTERN
from .errors import InstallerFault
from .installer import (
    is_contained_path,
    read_current_release_pointer,
    release_id_valid,
    validate_local_release,
    with_release_mutation_lock,
)


async def list_local_releases(releases_dir: str) -> list[dict[str, Any]]:
    """枚举 releases 目录下所有版本并防守性读取清单元数据。

    返回按 [active, release_id] 确定性排序的列表。
    """
    if not os.path.isdir(releases_dir):
        return []
    releases_root = os.path.realpath(os.path.abspath(releases_dir))
    active_id: str | None = None
    try:
        pointer = await read_current_release_pointer(releases_dir)
        active_id = pointer.get("release_id")
    except Exception:
        active_id = None

    try:
        entries = os.listdir(releases_root)
    except OSError:
        return []

    results: list[dict[str, Any]] = []
    for name in entries:
        if not RELEASE_ID_PATTERN.match(name):
            continue
        rel_path = os.path.join(releases_root, name)
        try:
            st = os.lstat(rel_path)
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                continue
            actual = os.path.realpath(rel_path)
            if not is_contained_path(releases_root, actual):
                continue
        except OSError:
            continue

        manifest_path = os.path.join(rel_path, "release-manifest.json")
        raw_manifest: dict[str, Any] | None = None
        modified_at: str | None = None
        try:
            if os.path.isfile(manifest_path):
                mtime = os.path.getmtime(manifest_path)
                modified_at = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).isoformat()
                with open(manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        raw_manifest = data
            else:
                mtime = os.path.getmtime(rel_path)
                modified_at = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).isoformat()
        except Exception:
            pass

        valid = False
        reason: str | None = None
        manifest_data: dict[str, Any] = raw_manifest if isinstance(raw_manifest, dict) else {}
        try:
            validated = await validate_local_release(releases_dir, name, verify_hashes=False)
            if isinstance(validated, dict):
                manifest_data = validated
            valid = True
        except Exception as e:
            valid = False
            reason = str(e)

        item = {
            "release_id": name,
            "active": (name == active_id),
            "valid": valid,
            "data_version": manifest_data.get("data_version"),
            "document_count": manifest_data.get("document_count"),
            "line_count": manifest_data.get("line_count"),
            "compressed_size": manifest_data.get("compressed_size"),
            "uncompressed_size": manifest_data.get("uncompressed_size"),
            "minimum_agent_version": manifest_data.get("minimum_agent_version"),
            "modified_at": modified_at,
            "reason": reason,
        }
        results.append(item)

    # 确定性排序：active 优先，随后按 release_id 字典序
    results.sort(key=lambda r: (not r["active"], r["release_id"]))
    return results


async def activate_release(releases_dir: str, release_id: str, *, verify_hashes: bool = False) -> dict[str, Any]:
    """在变更锁保护下验证版本并原子写入 current.json。返回指针字典。"""
    if not release_id_valid(release_id):
        raise InstallerFault("INVALID_REQUEST", "releaseId 非法")

    async with with_release_mutation_lock(releases_dir):
        manifest = await validate_local_release(releases_dir, release_id, verify_hashes=verify_hashes)
        if not isinstance(manifest, dict):
            raise InstallerFault("INVALID_RELEASE", "版本校验返回异常")
        pointer = {
            "release_id": release_id,
            "data_version": manifest["data_version"],
            "channel": "manual",
            "public_download": True,
            "schema_version": 1,
            "activated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        pointer_temp = os.path.join(releases_dir, f"current.json.{secrets.token_hex(6)}.tmp")
        with open(pointer_temp, "w", encoding="utf-8") as f:
            json.dump(pointer, f, indent=2)
            f.write("\n")
        os.replace(pointer_temp, os.path.join(releases_dir, "current.json"))
        return pointer


async def delete_release(releases_dir: str, release_id: str) -> None:
    """在变更锁保护下安全递归删除指定的非激活资料版本。"""
    if not release_id_valid(release_id):
        raise InstallerFault("INVALID_REQUEST", "releaseId 非法")

    async with with_release_mutation_lock(releases_dir):
        active_id: str | None = None
        try:
            pointer = await read_current_release_pointer(releases_dir)
            active_id = pointer.get("release_id")
        except Exception:
            pass
        if release_id == active_id:
            raise InstallerFault("INVALID_REQUEST", "不能删除当前激活版本")

        releases_root = os.path.realpath(os.path.abspath(releases_dir))
        target_dir = os.path.join(releases_root, release_id)
        if not os.path.exists(target_dir):
            return

        st = os.lstat(target_dir)
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise InstallerFault("INVALID_REQUEST", "目标不是受管目录")
        actual = os.path.realpath(target_dir)
        if not is_contained_path(releases_root, actual):
            raise InstallerFault("INVALID_REQUEST", "目标越出 releases 目录")

        # 递归检查越界与符号链接后自底向上删除
        for root, dirs, files in os.walk(actual, topdown=False, followlinks=False):
            for name in files:
                file_path = os.path.join(root, name)
                fst = os.lstat(file_path)
                if stat.S_ISLNK(fst.st_mode):
                    raise InstallerFault("INVALID_REQUEST", f"检测到符号链接，拒绝删除: {file_path}")
                os.remove(file_path)
            for name in dirs:
                dir_path = os.path.join(root, name)
                dst = os.lstat(dir_path)
                if stat.S_ISLNK(dst.st_mode):
                    raise InstallerFault("INVALID_REQUEST", f"检测到符号链接，拒绝删除: {dir_path}")
                os.rmdir(dir_path)
        os.rmdir(actual)


async def local_release_status(releases_dir: str) -> dict[str, Any]:
    """获取本地语料库全局状态摘要（供 /prts 状态 使用）。"""
    releases = await list_local_releases(releases_dir)
    releases_count = len(releases)
    active_release = next((r for r in releases if r.get("active")), None)

    if not active_release or not active_release.get("valid"):
        return {
            "installed": False,
            "active_release_id": None,
            "data_version": None,
            "documents": None,
            "lines": None,
            "packs": None,
            "compressed_bytes": None,
            "releases_count": releases_count,
        }

    release_id = active_release["release_id"]
    try:
        manifest = await validate_local_release(releases_dir, release_id, verify_hashes=False)
        if isinstance(manifest, dict):
            raw_packs = manifest.get("packs")
            pack_count = len(raw_packs) if isinstance(raw_packs, list) else 0
            return {
                "installed": True,
                "active_release_id": release_id,
                "data_version": manifest.get("data_version"),
                "documents": manifest.get("document_count"),
                "lines": manifest.get("line_count"),
                "packs": pack_count,
                "compressed_bytes": manifest.get("compressed_size"),
                "releases_count": releases_count,
            }
    except Exception:
        pass

    return {
        "installed": False,
        "active_release_id": None,
        "data_version": None,
        "documents": None,
        "lines": None,
        "packs": None,
        "compressed_bytes": None,
        "releases_count": releases_count,
    }
