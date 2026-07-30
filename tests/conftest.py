from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_repertoire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the default per-user Repertoire inside pytest's temporary root."""

    home = tmp_path.parent / f"{tmp_path.name}-home"
    monkeypatch.setenv("HOME", str(home))
