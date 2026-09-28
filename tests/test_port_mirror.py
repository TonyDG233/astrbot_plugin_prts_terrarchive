"""ModelScope 元数据回退：本地 mock 镜像（tree API + dataset-manifest + pack 清单 + 资产）端到端。"""

import hashlib
import json
import os

import pytest

from aiohttp import web

from conftest import run_async as run
from prts_corpus import installer as installer_module
from prts_corpus.store import CorpusStore

ARK_PACKS = {"official_game", "reviewed_wiki", "terra_journey", "entities", "references"}
EF_PACKS = {"endfield_official_game", "endfield_reviewed_knowledge"}


@pytest.fixture(scope="module")
def fixture_data(full_fixture):
    return full_fixture


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _pack_files(release_dir, pack_id):
    pack_dir = os.path.join(release_dir, pack_id)
    files = {}
    for root, _dirs, names in os.walk(pack_dir):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, release_dir).replace("\\", "/")
            files[rel] = {"sha256": _sha256(full), "size": os.path.getsize(full)}
    return files


def _dataset_manifest(release_dir, release_id, data_version, packs):
    files = {}
    present = []
    for pack_id in sorted(packs):
        pack_dir = os.path.join(release_dir, pack_id)
        if not os.path.isdir(pack_dir):
            continue
        pack_files = _pack_files(release_dir, pack_id)
        if not pack_files:
            continue
        present.append(pack_id)
        for rel, meta in pack_files.items():
            files[f"releases/{release_id}/{rel}"] = meta
    total = sum(meta["size"] for meta in files.values())
    return {
        "schema_version": 1,
        "kind": installer_module.MIRROR_DATASET_KIND,
        "release_id": release_id,
        "data_version": data_version,
        "pack_ids": present,
        "file_count": len(files),
        "compressed_bytes": total,
        "files": files,
    }


def test_mirror_resolve_and_install(fixture_data, tmp_path, monkeypatch):
    release_id = fixture_data["release_id"]
    release_dir = os.path.join(fixture_data["releases_dir"], release_id)
    data_version = fixture_data["data_version"]
    repo_packs = {
        installer_module.MODELSCOPE_REPOS["arknights"]: ARK_PACKS,
        installer_module.MODELSCOPE_REPOS["endfield"]: EF_PACKS,
    }
    datasets = {
        repo: _dataset_manifest(release_dir, release_id, data_version, packs)
        for repo, packs in repo_packs.items()
    }

    async def tree_handler(_request):
        return web.json_response(
            {"Code": 200, "Data": {"Files": [{"Name": release_id, "Type": "tree", "CommittedDate": 1}]}}
        )

    async def resolver_handler(request):
        parts = request.match_info["tail"].split("/")
        repo = f"{parts[0]}/{parts[1]}"
        tail = "/".join(parts[6:])
        if repo not in datasets:
            raise web.HTTPNotFound()
        if tail == "dataset-manifest.json":
            return web.json_response(datasets[repo])
        full = os.path.join(release_dir, *tail.split("/"))
        if not os.path.isfile(full):
            raise web.HTTPNotFound()
        return web.FileResponse(full)

    app = web.Application()
    app.router.add_get("/api/v1/datasets/{tail:.*}", tree_handler)
    app.router.add_get("/datasets/{tail:.*}", resolver_handler)

    async def scenario():
        import aiohttp

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        monkeypatch.setattr(installer_module, "MODELSCOPE_BASE_URL", f"http://127.0.0.1:{port}")
        try:
            fresh = tmp_path / "fresh-releases"
            fresh.mkdir()
            async with aiohttp.ClientSession() as session:
                snapshot = await installer_module.resolve_mirror_current_release(session=session, release_id=release_id)
                assert snapshot["mirror_snapshot"] is True
                assert snapshot["release_id"] == release_id
                assert snapshot["release_manifest"]["mirror_declared_data_version"] == data_version
                assert snapshot["data_version"] != data_version
                assert len(snapshot["data_version"]) == 64

                result = await installer_module.ensure_corpus_release(
                    releases_dir=str(fresh),
                    release_id=release_id,
                    metadata_source="mirror",
                    download_order=("modelscope",),
                    enabled_games=("arknights", "endfield"),
                    session=session,
                )
                assert result["reused"] is False
                assert result["release_id"] == release_id
                pointer = json.loads((fresh / "current.json").read_text(encoding="utf-8"))
                assert pointer["release_id"] == release_id
                assert pointer["data_version"] == result["data_version"]

                manifest = await installer_module.validate_local_release(
                    str(fresh), release_id, verify_hashes=True
                )
                assert manifest["data_version"] == result["data_version"]

                store = CorpusStore(str(fresh))
                await store.ready()
                found = await store.get_document(fixture_data["expected"]["document_ids"]["story"])
                assert found is not None
                store.reset()

                again = await installer_module.ensure_corpus_release(
                    releases_dir=str(fresh),
                    release_id=release_id,
                    metadata_source="mirror",
                    download_order=("modelscope",),
                    enabled_games=("arknights", "endfield"),
                    session=session,
                )
                assert again["reused"] is True
                assert again["downloaded_bytes"] == 0
        finally:
            await runner.cleanup()

    run(scenario())


def test_mirror_resolver_rejects_hash_mismatch(fixture_data, tmp_path, monkeypatch):
    """dataset-manifest 与 pack 清单哈希不一致时必须 fail loud。"""
    release_id = fixture_data["release_id"]
    release_dir = os.path.join(fixture_data["releases_dir"], release_id)
    repo = installer_module.MODELSCOPE_REPOS["arknights"]
    manifest = _dataset_manifest(release_dir, release_id, fixture_data["data_version"], {"references"})
    # 篡改一个已登记文件的哈希
    for rel in manifest["files"]:
        if rel.endswith(".jsonl.gz"):
            manifest["files"][rel]["sha256"] = "0" * 64
            break

    async def tree_handler(_request):
        return web.json_response(
            {"Code": 200, "Data": {"Files": [{"Name": release_id, "Type": "tree", "CommittedDate": 1}]}}
        )

    async def resolver_handler(request):
        parts = request.match_info["tail"].split("/")
        repo = f"{parts[0]}/{parts[1]}"
        tail = "/".join(parts[6:])
        if repo != repo_name:
            raise web.HTTPNotFound()
        if tail == "dataset-manifest.json":
            return web.json_response(manifest)
        full = os.path.join(release_dir, *tail.split("/"))
        if not os.path.isfile(full):
            raise web.HTTPNotFound()
        return web.FileResponse(full)

    app = web.Application()
    app.router.add_get("/api/v1/datasets/{tail:.*}", tree_handler)
    app.router.add_get("/datasets/{tail:.*}", resolver_handler)

    async def scenario():
        import aiohttp

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        monkeypatch.setattr(installer_module, "MODELSCOPE_BASE_URL", f"http://127.0.0.1:{port}")
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(installer_module.InstallerFault):
                    await installer_module.resolve_mirror_current_release(session=session, release_id=release_id)
        finally:
            await runner.cleanup()

    run(scenario())

def test_auto_falls_back_when_site_blocked(fixture_data, monkeypatch):
    """auto 模式：prts.chat 返回 403（WAF/地域拦截）时必须回退 ModelScope。"""
    release_id = fixture_data["release_id"]
    release_dir = os.path.join(fixture_data["releases_dir"], release_id)
    data_version = fixture_data["data_version"]
    repo_packs = {
        installer_module.MODELSCOPE_REPOS["arknights"]: ARK_PACKS,
        installer_module.MODELSCOPE_REPOS["endfield"]: EF_PACKS,
    }
    datasets = {
        repo: _dataset_manifest(release_dir, release_id, data_version, packs)
        for repo, packs in repo_packs.items()
    }

    async def site_handler(_request):
        return web.Response(status=403, text="blocked by waf")

    async def tree_handler(_request):
        return web.json_response(
            {"Code": 200, "Data": {"Files": [{"Name": release_id, "Type": "tree", "CommittedDate": 1}]}}
        )

    async def resolver_handler(request):
        parts = request.match_info["tail"].split("/")
        repo_name = f"{parts[0]}/{parts[1]}"
        if repo_name not in datasets:
            raise web.HTTPNotFound()
        tail = "/".join(parts[6:])
        if tail == "dataset-manifest.json":
            return web.json_response(datasets[repo_name])
        full = os.path.join(release_dir, *tail.split("/"))
        if not os.path.isfile(full):
            raise web.HTTPNotFound()
        return web.FileResponse(full)

    app = web.Application()
    app.router.add_get("/api/agent/data/releases/current", site_handler)
    app.router.add_get("/api/v1/datasets/{tail:.*}", tree_handler)
    app.router.add_get("/datasets/{tail:.*}", resolver_handler)

    async def scenario():
        import aiohttp

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        monkeypatch.setattr(installer_module, "MODELSCOPE_BASE_URL", f"http://127.0.0.1:{port}")
        try:
            async with aiohttp.ClientSession() as session:
                snapshot = await installer_module.resolve_trusted_current_release(
                    session=session,
                    metadata_source="auto",
                    site_base_url=f"http://127.0.0.1:{port}",
                    release_id=release_id,
                )
                assert snapshot["mirror_snapshot"] is True
                assert "ACCESS_DENIED" in snapshot.get("fallback_reason", "")
        finally:
            await runner.cleanup()

    run(scenario())
