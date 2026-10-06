import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shturman_core.state import Store  # noqa: E402


class Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "state")


@pytest.fixture
def clock() -> Clock:
    return Clock()
