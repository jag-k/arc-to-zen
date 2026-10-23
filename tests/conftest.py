"""Keep native browser execution an explicit opt-in."""

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("zen")
    group.addoption("--run-integration", action="store_true", help="Run native Zen in NEW temporary profiles")
    group.addoption(
        "--zen-binary", default="/Applications/Zen.app/Contents/MacOS/zen", help="Zen executable for native tests"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-integration"):
        skip = pytest.mark.skip(reason="Use --run-integration to launch an isolated Zen")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)


@pytest.fixture
def zen_binary(pytestconfig: pytest.Config) -> Path:
    binary = Path(pytestconfig.getoption("--zen-binary")).expanduser()
    if not binary.is_file():
        pytest.fail(f"Zen executable does not exist: {binary}")
    return binary
