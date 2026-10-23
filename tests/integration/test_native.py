"""Real-browser checks; regular pytest runs skip these."""

from pathlib import Path

import pytest

from tests.integration import boosts, sidebar


pytestmark = pytest.mark.integration


def test_sidebar_roundtrip(tmp_path: Path, zen_binary: Path) -> None:
    sidebar.main(["--artifacts", str(tmp_path / "sidebar"), "--zen-binary", str(zen_binary)])


def test_boosts_roundtrip(tmp_path: Path, zen_binary: Path) -> None:
    boosts.main(["--artifacts", str(tmp_path / "boosts"), "--zen-binary", str(zen_binary)])
