"""Shared fixtures + integration-marker plumbing."""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit external APIs",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: mark test as hitting external services",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="needs --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
