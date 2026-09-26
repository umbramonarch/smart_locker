"""
File: test_app_args.py
Description: The server entry point rejects stray flags — the removed --cli
             mode must error instead of silently starting the server.
Project: smart_locker/tests/api
"""

import pytest

from smart_locker.app import _parse_args


def test_no_args_accepted():
    assert _parse_args([]) is not None


def test_removed_cli_flag_is_rejected():
    with pytest.raises(SystemExit) as exc:
        _parse_args(["--cli"])
    assert exc.value.code == 2


def test_unknown_flag_is_rejected():
    with pytest.raises(SystemExit) as exc:
        _parse_args(["--bogus"])
    assert exc.value.code == 2
