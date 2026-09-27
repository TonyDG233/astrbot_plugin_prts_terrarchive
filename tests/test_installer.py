"""Acceptance test suite for PRTS corpus installer and release verification."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from prts_corpus.constants import MAX_CURRENT_POINTER_BYTES
from prts_corpus.errors import InstallerFault
from prts_corpus.installer import (
    ensure_corpus_release,
    read_current_release_pointer,
    validate_local_release,
)
from tests.conftest import run_async
import tests.fixtures as fixtures_mod


def test_installer_validate_local_release_passes(full_fixture: dict[str, Any]) -> None:
    """validate_local_release(verify_hashes=True) passes on fixture release."""
    releases_dir = full_fixture["releases_dir"]
    release_id = full_fixture["release_id"]

    manifest, pack_manifests, rdir = run_async(
        validate_local_release(releases_dir, release_id, verify_hashes=True, details=True)
    )

    assert manifest["release_id"] == release_id
    assert manifest["data_version"] == full_fixture["data_version"]
    assert len(pack_manifests) == 6
    assert os.path.isdir(rdir)


def test_installer_flip_byte_shard_validation_fails(tmp_path: Path, full_fixture: dict[str, Any]) -> None:
    """Flipping one byte in a shard file causes checksum validation to fail."""
    mutated_dir = tmp_path / "corrupted_release"
    shutil.copytree(full_fixture["releases_dir"], mutated_dir)

    # Corrupt a single byte in official_game shard
    shard_path = mutated_dir / full_fixture["release_id"] / "official_game" / "shards" / "00000.jsonl.gz"
    with open(shard_path, "r+b") as sf:
        byte = sf.read(1)
        sf.seek(0)
        sf.write(bytes([byte[0] ^ 0xFF]))

    with pytest.raises(InstallerFault) as exc_info:
        run_async(
            validate_local_release(
                str(mutated_dir),
                full_fixture["release_id"],
                verify_hashes=True,
                details=True,
            )
        )
    assert exc_info.value.code in ("INVALID_RELEASE", "CHECKSUM_MISMATCH")


def test_installer_pack_manifest_gram_sizes_validation(full_fixture: dict[str, Any]) -> None:
    """Pack-manifest with search_index.gram_sizes=[1,2,3] passes validation (regression)."""
    manifest, pack_manifests, _ = run_async(
        validate_local_release(
            full_fixture["releases_dir"],
            full_fixture["release_id"],
            verify_hashes=True,
            details=True,
        )
    )
    official_pack = pack_manifests["official_game"]
    gram_sizes = official_pack.get("search_index", {}).get("gram_sizes")
    assert gram_sizes == [1, 2, 3]


def test_installer_read_current_release_pointer_oversized(tmp_path: Path) -> None:
    """read_current_release_pointer rejects an oversized current.json pointer."""
    pointer_path = tmp_path / "current.json"
    oversized_data = {
        "release_id": "test-release",
        "data_version": "0" * 64,
        "padding": "x" * (MAX_CURRENT_POINTER_BYTES + 1024),
    }
    with open(pointer_path, "w", encoding="utf-8") as f:
        json.dump(oversized_data, f)

    with pytest.raises(InstallerFault) as exc_info:
        run_async(read_current_release_pointer(str(tmp_path)))
    assert exc_info.value.code == "INVALID_RELEASE"


def test_installer_read_current_release_pointer_symlink_or_junction(tmp_path: Path) -> None:
    """read_current_release_pointer rejects a symlinked/junction current.json."""
    real_target_dir = tmp_path / "target_dir"
    real_target_dir.mkdir(parents=True, exist_ok=True)
    real_file = real_target_dir / "real.json"
    with open(real_file, "w", encoding="utf-8") as f:
        json.dump({"release_id": "test-id", "data_version": "0" * 64}, f)

    pointer_path = tmp_path / "current.json"

    # Attempt os.symlink first
    symlink_created = False
    try:
        os.symlink(str(real_file), str(pointer_path))
        symlink_created = True
    except (OSError, NotImplementedError):
        # On Windows, try creating a directory junction named current.json
        try:
            import _winapi
            _winapi.CreateJunction(str(real_target_dir), str(pointer_path))
            symlink_created = True
        except Exception:
            pass

    if not symlink_created:
        pytest.skip("Neither symlinks nor directory junctions are permitted in this environment")

    with pytest.raises(InstallerFault) as exc_info:
        run_async(read_current_release_pointer(str(tmp_path)))
    assert exc_info.value.code == "INVALID_RELEASE"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_installer_ensure_corpus_release_remote_flow(tmp_path: Path, full_fixture: dict[str, Any]) -> None:
    """ensure_corpus_release reuses files, writes atomically, and rejects path traversal."""
    try:
        import aiohttp
        import aiohttp.web
    except ImportError:
        pytest.skip("aiohttp not installed")

    release_id = full_fixture["release_id"]
    releases_dir = full_fixture["releases_dir"]
    rel_dir = os.path.join(releases_dir, release_id)

    with open(os.path.join(rel_dir, "release-manifest.json"), "r", encoding="utf-8") as f:
        rm = json.load(f)

    app = aiohttp.web.Application()

    async def handle_current(request: aiohttp.web.Request) -> aiohttp.web.Response:
        current_data = {
            "release_id": release_id,
            "data_version": full_fixture["data_version"],
            "minimum_agent_version": "0.2.0",
            "document_count": rm["document_count"],
            "line_count": rm["line_count"],
            "compressed_size": rm["compressed_size"],
            "uncompressed_size": rm["uncompressed_size"],
            "packs": rm["packs"],
            "mirrors": [],
        }
        return aiohttp.web.json_response({"data": current_data})

    async def handle_release_file(request: aiohttp.web.Request) -> aiohttp.web.Response:
        rel_path = request.match_info["tail"]
        full_path = os.path.join(rel_dir, rel_path)
        if not os.path.exists(full_path):
            return aiohttp.web.Response(status=404)
        if full_path.endswith(".json"):
            with open(full_path, "r", encoding="utf-8") as jf:
                return aiohttp.web.json_response(json.load(jf))
        with open(full_path, "rb") as bf:
            return aiohttp.web.Response(body=bf.read())

    app.router.add_get("/api/agent/data/releases/current", handle_current)
    app.router.add_get(f"/api/agent/data/releases/{release_id}/{{tail:.*}}", handle_release_file)

    async def run_server_tests() -> None:
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        server_url = f"http://127.0.0.1:{port}"

        try:
            client_dir = str(tmp_path / "client_releases")
            os.makedirs(client_dir, exist_ok=True)

            # (a) First download: should download assets
            res1 = await ensure_corpus_release(
                releases_dir=client_dir,
                site_base_url=server_url,
                download_order=("site",),
            )
            assert res1["status"] == "downloaded"
            assert res1["downloaded_bytes"] > 0

            # (b) Atomic write: current.json must exist and be valid
            current_json_path = os.path.join(client_dir, "current.json")
            assert os.path.isfile(current_json_path)
            with open(current_json_path, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            assert cdata["release_id"] == release_id
            assert cdata["data_version"] == full_fixture["data_version"]

            # (a) Second run: reuses existing valid files without downloading again
            res2 = await ensure_corpus_release(
                releases_dir=client_dir,
                site_base_url=server_url,
                download_order=("site",),
            )
            assert res2["status"] in ("present", "downloaded")
            assert res2.get("reused") is True
            assert res2["downloaded_bytes"] == 0

            # (c) Server serving manifest with '../' asset path is rejected
            escape_app = aiohttp.web.Application()

            async def handle_escape_current(request: aiohttp.web.Request) -> aiohttp.web.Response:
                return await handle_current(request)

            async def handle_escape_file(request: aiohttp.web.Request) -> aiohttp.web.Response:
                rel_path = request.match_info["tail"]
                # Intercept pack-manifest.json to inject path traversal
                if "pack-manifest.json" in rel_path:
                    pack_id = rel_path.split("/")[0]
                    orig_path = os.path.join(rel_dir, rel_path)
                    with open(orig_path, "r", encoding="utf-8") as f:
                        pm_data = json.load(f)
                    # Insert malicious path
                    pm_data["shards"][0]["path"] = "shards/../evil.jsonl.gz"
                    return aiohttp.web.json_response(pm_data)
                return await handle_release_file(request)

            escape_app.router.add_get("/api/agent/data/releases/current", handle_escape_current)
            escape_app.router.add_get(f"/api/agent/data/releases/{release_id}/{{tail:.*}}", handle_escape_file)

            escape_runner = aiohttp.web.AppRunner(escape_app)
            await escape_runner.setup()
            escape_site = aiohttp.web.TCPSite(escape_runner, "127.0.0.1", 0)
            await escape_site.start()
            escape_port = escape_site._server.sockets[0].getsockname()[1]
            escape_url = f"http://127.0.0.1:{escape_port}"

            try:
                escape_client_dir = str(tmp_path / "escape_client")
                with pytest.raises(InstallerFault) as exc_info:
                    await ensure_corpus_release(
                        releases_dir=escape_client_dir,
                        site_base_url=escape_url,
                        download_order=("site",),
                    )
                assert exc_info.value.code in ("INVALID_RELEASE", "INVALID_MANIFEST")
            finally:
                await escape_runner.cleanup()

        finally:
            await runner.cleanup()

    run_async(run_server_tests())
