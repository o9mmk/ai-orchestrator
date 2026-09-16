"""orc テスト共通fixture。"""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """実ホームを使わず、全テストの状態領域を一時ディレクトリへ隔離する。"""
    state_dir = tmp_path / "orc-state"
    monkeypatch.setenv("ORC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """最小のrepo pathを返す。"""
    path = tmp_path / "repo"
    path.mkdir()
    return path
