"""
File: test_registerable_list.py
Description: Frontend contract tests for Register Device catalog-list failures.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_registerable_list.py -v
"""

from pathlib import Path


APP_JS = (
    Path(__file__).resolve().parents[1]
    / "smart_locker" / "frontend" / "app.js"
)


def _function(name: str, next_marker: str) -> str:
    source = APP_JS.read_text(encoding="utf-8")
    return source.split(f"function {name}", 1)[1].split(next_marker, 1)[0]


def test_registerable_load_failure_is_retryable_not_empty_state():
    """A failed catalog request has its own Retry UI, distinct from no rows."""
    load = _function("loadRegisterableList", "/**\n * Render the registerable")
    render = _function("renderRegisterableList", "/**\n * Open the add step")
    assert "registerableError" in load
    assert "Could not load catalog units." in load
    assert "registerableError" in render
    assert "retry.textContent = 'Retry'" in render
    assert "No catalog units are waiting to be registered." in render
