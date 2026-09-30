"""
File: test_dev.py
Description: Tests for POST /api/dev/tap — fake-reader 404, loopback when on.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_dev.py -v
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from smart_locker.api.app_context import PendingRegistration, PendingTagBind
from smart_locker.api.routes import router
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, RegistrantRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac

import smart_locker.api.app_context as ctx_module

class TestDevEndpoints:
    """Tests for the no-hardware simulation endpoints -- inert unless the fake
    reader is enabled AND the running reader is actually the fake one, so
    production is unaffected regardless of who can reach the kiosk's HTTP port.
    """

    def test_dev_status_inactive_by_default(self, client, mock_context, monkeypatch):
        """GET /api/dev/status reports inactive when SMART_LOCKER_FAKE_READER is unset.

        Forces the flag unset via monkeypatch rather than relying on the ambient
        host .env -- a dev box left with SMART_LOCKER_FAKE_READER=1 from a prior
        simulation session must not silently make this test pass for the wrong
        reason (production kiosks must never have this flag on either).
        """
        monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        mock_context.reader = None
        resp = client.get("/api/dev/status")
        assert resp.status_code == 200
        assert resp.json()["fake_reader"] is False

    def test_dev_tap_404_by_default(self, client, mock_context, monkeypatch):
        """POST /api/dev/tap 404s when the simulation harness is not active (production posture)."""
        monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        mock_context.reader = None
        resp = client.post("/api/dev/tap", json={"uid": "AABBCCDD"})
        assert resp.status_code == 404

    def test_dev_tap_404_when_flag_set_but_reader_not_fake(self, client, mock_context, monkeypatch):
        """The env flag alone is not enough -- the running reader must actually be the fake one."""
        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        mock_context.reader = object()  # no simulate_tap -- not a fake reader
        resp = client.post("/api/dev/tap", json={"uid": "AABBCCDD"})
        assert resp.status_code == 404

    def test_dev_tap_enqueues_card_event(self, client, mock_context, monkeypatch):
        """POST /api/dev/tap simulates a real tap: the fake reader enqueues a CardEvent(INSERTED)."""
        from smart_locker.nfc.card_observer import CardEvent, CardEventType
        from smart_locker.nfc.fake_reader import FakeNFCReader

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        reader = FakeNFCReader()
        mock_context.reader = reader

        resp = client.post("/api/dev/tap", json={"uid": "AABBCCDD"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        event = reader.poll_event()
        assert isinstance(event, CardEvent)
        assert event.event_type == CardEventType.INSERTED
        assert event.uid == "AABBCCDD"

    def test_dev_tap_no_uid_available(self, client, mock_context, monkeypatch):
        """POST /api/dev/tap 400s when no UID is supplied and no default is configured."""
        from smart_locker.nfc.fake_reader import FakeNFCReader

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        monkeypatch.delenv("SMART_LOCKER_FAKE_DEFAULT_UID", raising=False)
        mock_context.reader = FakeNFCReader()

        resp = client.post("/api/dev/tap", json={})
        assert resp.status_code == 400

    def test_dev_tap_lan_403_when_fake_on(self, lan_client, mock_context, monkeypatch):
        """Fake reader on still refuses LAN hosts so they cannot inject taps."""
        from smart_locker.nfc.fake_reader import FakeNFCReader

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        mock_context.reader = FakeNFCReader()
        resp = lan_client.post("/api/dev/tap", json={"uid": "AABBCCDD"})
        assert resp.status_code == 403
        assert mock_context.reader.poll_event() is None

    def test_dev_status_lan_403(self, lan_client, mock_context, monkeypatch):
        """GET /api/dev/status is loopback-only — a LAN host must not learn
        the harness is on or see its preset UIDs."""
        from smart_locker.nfc.fake_reader import FakeNFCReader

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        mock_context.reader = FakeNFCReader()
        resp = lan_client.get("/api/dev/status")
        assert resp.status_code == 403

    def test_dev_tap_strips_uid_whitespace(self, client, mock_context, monkeypatch):
        """A typed UID with interior spaces must HMAC identically to the
        compact form — the tap normalizes all whitespace out."""
        from smart_locker.nfc.card_observer import CardEvent, CardEventType
        from smart_locker.nfc.fake_reader import FakeNFCReader

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        reader = FakeNFCReader()
        mock_context.reader = reader

        resp = client.post("/api/dev/tap", json={"uid": "AA BB CC DD"})
        assert resp.status_code == 200

        event = reader.poll_event()
        assert isinstance(event, CardEvent)
        assert event.event_type == CardEventType.INSERTED
        assert event.uid == "AABBCCDD"

    def test_dev_status_exposes_presets_only_when_active(
        self, client, mock_context, monkeypatch
    ):
        """The dev-panel key map ships with the harness — the seed script
        enrolls/binds these exact UIDs, so they come from one source."""
        from smart_locker.nfc.fake_reader import (
            FAKE_CARD_UIDS,
            FAKE_TAG_UIDS,
            FakeNFCReader,
        )

        monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        mock_context.reader = None
        assert client.get("/api/dev/status").json()["presets"] is None

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        mock_context.reader = FakeNFCReader()
        presets = client.get("/api/dev/status").json()["presets"]
        assert presets["card_uids"] == list(FAKE_CARD_UIDS)
        assert presets["tag_uids"] == list(FAKE_TAG_UIDS)

