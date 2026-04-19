"""
Pytest configuration — skip integration tests by default.

Tests marked @pytest.mark.integration hit live external APIs (Steam Web API,
Steam News API) and would flake in CI. Run with --integration to include them:

    python -m pytest -v --integration
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.integration (hit live external APIs).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: test that hits a live external API (skip by default)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    skip_marker = pytest.mark.skip(reason="needs --integration flag (live external API)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)
