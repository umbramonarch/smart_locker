"""
File: appliance.py
Description: Raspberry Pi appliance actions for the hidden admin panel: stop
             the Chromium kiosk (backend stays up) and power off the board.
Project: smart_locker/services
Notes: Exit kiosk sends SIGTERM only to processes whose command line contains
       the start-kiosk.sh profile marker (smart-locker-kiosk), not every
       Chromium. Shut down runs the exact sudoers-whitelisted command
       ``sudo -n /usr/bin/systemctl poweroff``. Both refuse cleanly on a
       Windows/dev host where the tools are absent.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess

logger = logging.getLogger(__name__)

# Must match ``--user-data-dir=.../smart-locker-kiosk`` in start-kiosk.sh.
KIOSK_PROFILE_MARKER = "smart-locker-kiosk"

# Exact argv the sudoers drop-in allows — no wildcard, no ``systemctl`` from PATH.
_POWEROFF_CMD = ["sudo", "-n", "/usr/bin/systemctl", "poweroff"]


class ApplianceUnavailable(Exception):
    """This host cannot perform the requested appliance action (not the Pi)."""


class ApplianceError(Exception):
    """The action was attempted on the Pi but the OS command failed."""


def exit_kiosk() -> None:
    """Stop the Chromium kiosk browser. The FastAPI service stays running.

    Chromium is started by ``deploy/kiosk/start-kiosk.sh`` via XDG autostart.
    Killing it does not restart it until the next graphical login or reboot.

    Raises:
        ApplianceUnavailable: ``pgrep`` is missing or no kiosk process is running.
        ApplianceError: ``pgrep`` failed unexpectedly.
    """
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        raise ApplianceUnavailable(
            "Kiosk exit runs on the Raspberry Pi appliance only."
        )

    try:
        result = subprocess.run(
            [pgrep, "-f", KIOSK_PROFILE_MARKER],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ApplianceError(f"Could not look up kiosk processes: {e}") from e

    # pgrep: 0 = match, 1 = no match, other = error.
    if result.returncode == 1:
        raise ApplianceUnavailable("Kiosk browser is not running.")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"pgrep exit {result.returncode}"
        raise ApplianceError(f"Could not look up kiosk processes: {detail}")

    pids = [int(tok) for tok in result.stdout.split() if tok.isdigit()]
    if not pids:
        raise ApplianceUnavailable("Kiosk browser is not running.")

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as e:
            raise ApplianceError(f"Could not stop kiosk process {pid}: {e}") from e

    # Hide-cursor helper from start-kiosk.sh; ignore if it was never started.
    pkill = shutil.which("pkill")
    if pkill is not None:
        subprocess.run(
            [pkill, "-x", "unclutter"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    logger.info("Kiosk Chromium stopped (%s pid(s)).", len(pids))


def shutdown() -> None:
    """Power off the appliance via ``systemctl poweroff``.

    Raises:
        ApplianceUnavailable: systemd is not present (dev/Windows).
        ApplianceError: sudo/systemctl refused or timed out. Typical cause is
            a missing ``/etc/sudoers.d/smart-locker`` poweroff rule — run
            ``sudo bash deploy/install/apply-sudoers.sh`` once.
    """
    if shutil.which("systemctl") is None:
        raise ApplianceUnavailable(
            "Shut down runs on the Raspberry Pi appliance only."
        )

    try:
        subprocess.run(
            _POWEROFF_CMD,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise ApplianceError(f"Could not power off: {detail}") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ApplianceError(f"Could not power off: {e}") from e

    logger.info("Appliance poweroff started.")
