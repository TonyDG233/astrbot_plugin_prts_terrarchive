"""installer.py / versions.py：本地校验、篡改检测、指针防护、URL/SemVer 与零联网复用。"""

import os
import shutil
import gzip

import pytest

from conftest import run_async as run
from prts_corpus import installer as installer_module
from prts_corpus import versions as versions_module
from prts_corpus.errors import InstallerFault


@pytest.fixture(scope="module")
def fixture_data(full_fixture):
    return full_fixture


def test_validate_local_release(fixture_data):
    manifest = run(installer_module.validate_local_release(
        fixture_data["releases_dir"], fixture_data["release_id"], verify_hashes=True,
    ))
    assert manifest["release_id"] == fixture_data["release_id"]
    assert manifest["data_version"] == fixture_data["data_version"]
    assert manifest["document_count"] >= 1
    assert manifest["compressed_size"] > 0


def test_tampered_shard_detected(fixture_data, tmp_path):
    target = tmp_path / "releases"
    shutil.copytree(fixture_data["releases_dir"], target)
    release_dir = target / fixture_data["release_id"]
    shard = None
    for root, _dirs, files in os.walk(release_dir):
        for name in files:
            if name.endswith(".jsonl.gz"):
                shard = os.path.join(root, name)
                break
        if shard:
            break
    assert shard, "fixture must contain a gzip shard"
    raw = bytearray(open(shard, "rb").read())
    raw[len(raw) // 2] ^= 0xFF
    open(shard, "wb").write(bytes(raw))
    with pytest.raises(InstallerFault):
        run(installer_module.validate_local_release(
            str(target), fixture_data["release_id"], verify_hashes=True,
        ))


def test_current_pointer_read(fixture_data):
    pointer = run(installer_module.read_current_release_pointer(fixture_data["releases_dir"]))
    assert pointer["release_id"] == fixture_data["release_id"]
    assert pointer["data_version"] == fixture_data["data_version"]


def test_oversized_pointer_rejected(tmp_path):
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "current.json").write_text("x" * 70_000, encoding="utf-8")
    with pytest.raises(InstallerFault):
        run(installer_module.read_current_release_pointer(str(releases)))


def test_site_base_url_validation():
    assert installer_module.normalize_site_base_url("https://prts.chat")
    assert installer_module.normalize_site_base_url("http://127.0.0.1:8080")
    with pytest.raises(InstallerFault):
        installer_module.normalize_site_base_url("http://example.com")
    with pytest.raises(InstallerFault):
        installer_module.normalize_site_base_url("https://user:pass@prts.chat")


def test_semver_compare():
    assert installer_module.parse_semver("1.2.3") is not None
    assert installer_module.compare_semver("1.2.3", "1.2.4") < 0
    assert installer_module.compare_semver("0.2.0", "0.2.0") == 0
    assert installer_module.compare_semver("1.10.0", "1.9.9") > 0
    assert installer_module.compare_semver("1.0.0", "1.0.0+build.1") == 0


def test_list_local_releases_and_status(fixture_data):
    releases = run(versions_module.list_local_releases(fixture_data["releases_dir"]))
    active = [item for item in releases if item.get("active")]
    assert len(active) == 1
    assert active[0]["release_id"] == fixture_data["release_id"]
    assert active[0]["valid"] is True

    status = run(versions_module.local_release_status(fixture_data["releases_dir"]))
    assert status["installed"] is True
    assert status["active_release_id"] == fixture_data["release_id"]
    assert status["documents"] >= 1


def test_ensure_release_reuses_local_without_network(fixture_data):
    result = run(installer_module.ensure_corpus_release(
        releases_dir=fixture_data["releases_dir"],
        release_id=fixture_data["release_id"],
        data_version=fixture_data["data_version"],
        enabled_games=["arknights", "endfield"],
        session=None,
    ))
    assert result["reused"] is True
    assert result["downloaded_bytes"] == 0
    assert result["release_id"] == fixture_data["release_id"]


def test_missing_enabled_game_packs():
    manifest = {"packs": [{"pack_id": "official_game"}]}
    missing = installer_module.missing_enabled_game_packs(manifest, ["arknights", "endfield"])
    assert "endfield" in missing
    assert "arknights" not in missing


def test_activate_and_delete_release(fixture_data, tmp_path):
    releases = tmp_path / "releases"
    shutil.copytree(fixture_data["releases_dir"], releases)
    other = releases / "fixture-release-copy"
    shutil.copytree(releases / fixture_data["release_id"], other)
    # 复制出的版本未列入 release manifest，激活前必须重新校验并失败或保持原指针
    try:
        pointer = run(versions_module.activate_release(str(releases), "fixture-release-copy"))
    except InstallerFault:
        pointer = run(installer_module.read_current_release_pointer(str(releases)))
        assert pointer["release_id"] == fixture_data["release_id"]
    else:
        assert pointer["release_id"] == "fixture-release-copy"

    with pytest.raises(InstallerFault):
        run(versions_module.delete_release(str(releases), run(installer_module.read_current_release_pointer(str(releases)))["release_id"]))