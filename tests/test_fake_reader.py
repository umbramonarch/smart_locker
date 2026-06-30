"""
File: test_fake_reader.py
Description: Tests for the simulated NFC reader and the reader factory used by
             the no-hardware simulation harness. Verifies the factory selects
             the real NFCReader by default and the FakeNFCReader only when
             SMART_LOCKER_FAKE_READER is enabled, that the fake exposes the same
             interface, that simulate_tap() emits a proper INSERTED CardEvent,
             and that the raw card UID is never logged.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_fake_reader.py -v
"""

import logging

import pytest

from smart_locker.nfc.card_observer import CardEvent, CardEventType
from smart_locker.nfc.factory import create_reader, fake_reader_enabled
from smart_locker.nfc.fake_reader import FakeNFCReader


class TestReaderFactory:
    """Tests for env-gated selection of the real vs. simulated reader."""

    def test_off_by_default_returns_real_reader(self, monkeypatch):
        """With the flag unset, the factory returns the real NFCReader."""
        monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        assert fake_reader_enabled() is False
        reader = create_reader()
        assert type(reader).__name__ == "NFCReader"

    def test_flag_returns_fake_reader(self, monkeypatch):
        """With the flag set, the factory returns a FakeNFCReader."""
        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        assert fake_reader_enabled() is True
        assert isinstance(create_reader(), FakeNFCReader)

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True), ("true", True), ("YES", True), ("on", True),
            ("0", False), ("", False), ("off", False), ("nope", False),
        ],
    )
    def test_truthy_spellings(self, monkeypatch, value, expected):
        """The flag accepts common truthy spellings and rejects everything else."""
        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", value)
        assert fake_reader_enabled() is expected


class TestFakeNFCReader:
    """Tests for the simulated reader's behaviour and interface parity."""

    def test_interface_parity_with_real_reader(self):
        """The fake exposes the same public surface the bridge/app depend on."""
        reader = FakeNFCReader()
        for method in ("start", "stop", "wait_for_event", "poll_event", "simulate_tap"):
            assert callable(getattr(reader, method))
        assert hasattr(type(reader), "is_running")

    def test_start_returns_name_and_toggles_running(self):
        """start() reports a synthetic reader name and flips is_running; stop() clears it."""
        reader = FakeNFCReader()
        assert reader.is_running is False
        name = reader.start()
        assert reader.is_running is True
        assert "FAKE" in name
        reader.stop()
        assert reader.is_running is False

    def test_simulate_tap_emits_inserted_card_event(self):
        """simulate_tap enqueues a proper INSERTED CardEvent retrievable via the queue."""
        reader = FakeNFCReader()
        reader.start()
        reader.simulate_tap("04A1B2C3D4")

        event = reader.wait_for_event(timeout=1.0)
        assert isinstance(event, CardEvent)
        assert event.event_type == CardEventType.INSERTED
        assert event.uid == "04A1B2C3D4"
        assert "FAKE" in event.reader_name
        # Queue drains — no phantom duplicate event.
        assert reader.poll_event() is None

    def test_wait_for_event_times_out_when_idle(self):
        """wait_for_event returns None on timeout when no tap was injected."""
        reader = FakeNFCReader()
        reader.start()
        assert reader.wait_for_event(timeout=0.05) is None

    def test_simulate_tap_never_logs_uid(self, caplog):
        """Security invariant: the raw card UID is never written to logs."""
        reader = FakeNFCReader()
        reader.start()
        with caplog.at_level(logging.INFO):
            reader.simulate_tap("SECRETUID1234")
        assert "SECRETUID1234" not in caplog.text
