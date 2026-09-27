"""Pytest configuration and shared fixtures for PRTS Terrarchive acceptance tests."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Coroutine

import pytest

# Insert workspace root into sys.path
WORKSPACE_ROOT = str(Path(__file__).resolve().parent.parent)
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from tests.fixtures import make_full_fixture
from prts_corpus.store import CorpusStore


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Helper to run coroutines synchronously in tests without pytest-asyncio."""
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def full_fixture(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Session-scoped fixture wrapping fixtures.make_full_fixture()."""
    session_dir = str(tmp_path_factory.mktemp("full_fixture_session"))
    fixture_data = make_full_fixture(session_dir)
    return fixture_data


@pytest.fixture(scope="module")
def module_store(full_fixture: dict[str, Any]) -> CorpusStore:
    """Module-scoped CorpusStore ready instance initialized on the shared fixture."""
    store = CorpusStore(full_fixture["releases_dir"])
    run_async(store.ready())
    return store


@pytest.fixture
def run_coro():
    """Test fixture helper providing run_async."""
    return run_async
