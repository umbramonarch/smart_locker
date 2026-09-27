"""
File: appliance.py
Description: Raspberry Pi appliance actions for the hidden admin panel: stop
             the Chromium kiosk, launch an update, and power off the board.
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
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Must match ``--user-data-dir=.../smart-locker-kiosk`` in start-kiosk.sh.
KIOSK_PROFILE_MARKER = "smart-locker-kiosk"

# Exact argv the sudoers drop-in allows — no wildcard, no ``systemctl`` from PATH.
_POWEROFF_CMD = ["sudo", "-n", "/usr/bin/systemctl", "poweroff"]
_STOP_SERVICE_CMD = ["sudo", "-n", "/usr/bin/systemctl", "stop", "smart-locker"]
_STOP_UPDATE_CMD = ["sudo", "-n", "/usr/bin/systemctl", "stop", "smart-locker-update"]
SYSTEMD_RUN = shutil.which("systemd-run")
SYSTEMCTL = shutil.which("systemctl")

# Beat between the HTTP response and the self-stop so the reply can flush
# before the listener drops.
_STOP_RESPONSE_DELAY_SECONDS = 0.5


class ApplianceUnavailable(Exception):
    """This host cannot perform the requested appliance action (not the Pi)."""


class ApplianceError(Exception):
    """The action was attempted on the Pi but the OS command failed."""


def launch_update(base_dir: Path, systemd_run: str | None = SYSTEMD_RUN) -> None:
    """Launch the update script in its own systemd unit, surviving service restart."""
    script = base_dir / "deploy" / "install" / "update.sh"
    if not script.exists():
        raise ApplianceUnavailable("Update script not found on this host.")
    if systemd_run is None:
        raise ApplianceUnavailable(
            "Software updates run on the Raspberry Pi appliance only."
        )
    cmd = [
        "sudo", "-n", "systemd-run", "--collect",
        "--unit=smart-locker-update",
        "/bin/bash", str(script),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        detail = (getattr(e, "stderr", "") or str(e)).strip()
        logger.error("Failed to launch update unit: %s", detail)
        raise ApplianceError(f"Could not start update: {detail}") from e
    logger.info("Software update launched.")


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
    # A failing pkill (binary vanished, timeout) must not escape — callers
    # treat exit_kiosk failures as best-effort and still stop the service.
    pkill = shutil.which("pkill")
    if pkill is not None:
        try:
            subprocess.run(
                [pkill, "-x", "unclutter"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("Could not stop the unclutter helper: %s", e)

    logger.info("Kiosk Chromium stopped (%s pid(s)).", len(pids))


def stop_service() -> None:
    """Stop the ``smart-locker`` service via ``systemctl stop``.

    An explicit ``systemctl stop`` stays stopped — ``Restart=always`` does not
    start it again. The next boot starts the service normally.

    Raises:
        ApplianceUnavailable: systemd is not present (dev/Windows).
        ApplianceError: sudo/systemctl refused or timed out.
    """
    if SYSTEMCTL is None:
        raise ApplianceUnavailable(
            "Stop system runs on the Raspberry Pi appliance only."
        )
    try:
        subprocess.run(
            _STOP_SERVICE_CMD,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise ApplianceError(f"Could not stop the service: {detail}") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ApplianceError(f"Could not stop the service: {e}") from e

    logger.info("smart-locker service stop requested.")


def _stop_update_unit() -> None:
    """Best-effort stop of an in-flight update so it cannot restart the service.

    ``update.sh`` runs in its own ``smart-locker-update`` systemd unit and ends
    with ``systemctl start smart-locker`` — left running, it resurrects a box
    the admin just stopped. Stopping the unit first aborts the script before
    it reaches that restart. Every failure (no such unit, sudo refused,
    timeout) is logged and ignored: the service stop below must still run.
    """
    if SYSTEMCTL is None:
        return
    try:
        subprocess.run(
            _STOP_UPDATE_CMD,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("Could not stop a running update unit: %s", e)


def stop_system() -> None:
    """Close the kiosk browser, then stop the ``smart-locker`` service.

    Runs as a FastAPI background task — the HTTP response is already on the
    wire. The short delay lets the reply flush before the listener drops. A
    browser that is already gone (or cannot be signalled) does not keep the
    service running: the explicit ``systemctl stop`` is the point of the
    button, and ``Restart=always`` must not resurrect the process. An in-flight
    update unit is stopped first so its final ``systemctl start`` cannot undo
    the shutdown.
    """
    time.sleep(_STOP_RESPONSE_DELAY_SECONDS)
    try:
        exit_kiosk()
    except Exception as e:
        logger.warning(
            "Kiosk browser close failed (%s) — stopping the service anyway.", e
        )
    _stop_update_unit()
    stop_service()


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
