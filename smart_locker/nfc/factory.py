"""
File: factory.py
Description: Runtime selector for the NFC reader implementation. Returns the
             real pyscard-backed NFCReader by default, or a FakeNFCReader
             (simulated taps, no hardware) when SMART_LOCKER_FAKE_READER is
             enabled. This is the single seam the no-hardware simulation
             harness plugs into.
Project: smart_locker/nfc
Notes: The fake path is OFF by default and must never be enabled in
       production. The real NFCReader is imported lazily (only on the real
       branch) so callers that defer NFC imports to avoid requiring a working
       PC/SC stack — e.g. AppContext — keep that property.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Environment flag that switches in the simulated reader. Accepts common
# truthy spellings so a .env value of 1/true/yes/on all work.
_FAKE_READER_ENV_VAR = "SMART_LOCKER_FAKE_READER"
_TRUTHY = {"1", "true", "yes", "on"}


def fake_reader_enabled() -> bool:
    """Return whether the simulated NFC reader is enabled via env flag.

    Returns:
        True if ``SMART_LOCKER_FAKE_READER`` is set to a truthy value.
    """
    return os.getenv(_FAKE_READER_ENV_VAR, "").strip().lower() in _TRUTHY


def create_reader(reader_filter: str | None = None):
    """Create the active NFC reader implementation.

    Returns a ``FakeNFCReader`` when ``SMART_LOCKER_FAKE_READER`` is set
    (simulation harness — no hardware), otherwise the real pyscard-backed
    ``NFCReader``. Both expose an identical duck-typed surface, so callers do
    not branch on which one they got.

    Args:
        reader_filter: Optional reader-name substring forwarded to the reader.

    Returns:
        An ``NFCReader`` or ``FakeNFCReader`` instance.
    """
    if fake_reader_enabled():
        from smart_locker.nfc.fake_reader import FakeNFCReader
        logger.warning(
            "%s is enabled — using the SIMULATED NFC reader (no hardware). "
            "This must never be set in production.",
            _FAKE_READER_ENV_VAR,
        )
        return FakeNFCReader(reader_filter)

    from smart_locker.nfc.reader import NFCReader
    return NFCReader(reader_filter)
