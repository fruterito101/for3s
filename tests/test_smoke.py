"""Smoke test de C1: el monorepo está vivo y los guardianes lo verifican."""

from for3s_core import __version__, heartbeat


def test_heartbeat() -> None:
    assert heartbeat() == "for3s-os alive"


def test_version() -> None:
    assert __version__ == "0.0.1"
