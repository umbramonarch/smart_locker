"""
File: setup_service.py
Description: First-boot Setup. Decides whether the locker has no active admin
             (Setup is open) and stores the dashboard admin password typed at
             Setup into .env as SMART_LOCKER_DASHBOARD_ADMIN_SECRET.
Project: smart_locker/services
Notes: The .env write updates the running process too (os.environ), so the
       secret works without a restart. The secret value is never logged.
"""

import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import BASE_DIR, DASHBOARD_ADMIN_SECRET_ENV_VAR
from smart_locker.database.repositories import UserRepository

logger = logging.getLogger(__name__)


def env_file_path() -> Path:
    """The .env file Setup writes to.

    ``SMART_LOCKER_ENV_PATH`` overrides the default project-root ``.env``
    (tests point it at a temp file). Read on each call.
    """
    override = (os.getenv("SMART_LOCKER_ENV_PATH") or "").strip()
    return Path(override) if override else BASE_DIR / ".env"


def setup_needed(db_session: Session) -> bool:
    """Whether Setup is open: True while the database has no active admin.

    Setup closes the moment one active admin exists — the card tap is what
    finishes it, not the arming POST.
    """
    return UserRepository.first_active_admin(db_session) is None


def write_dashboard_secret(secret: str) -> None:
    """Persist the dashboard admin password to .env and this process.

    Writes ``SMART_LOCKER_DASHBOARD_ADMIN_SECRET="<secret>"`` to the env file
    (replacing an existing line for that key), applies mode 640 so only the
    owner and group can read it, and sets ``os.environ`` so the running
    process accepts the password immediately. Callers must check
    ``dashboard_admin_secret()`` first — this function always writes.

    Args:
        secret: The password to store. Must not contain CR/LF.

    Raises:
        ValueError: If the secret contains a newline.
        OSError: If the env file cannot be written.
    """
    if "\n" in secret or "\r" in secret:
        raise ValueError("Secret must not contain newlines.")

    path = env_file_path()
    key = DASHBOARD_ADMIN_SECRET_ENV_VAR
    escaped = secret.replace("\\", "\\\\").replace('"', '\\"')
    line = f'{key}="{escaped}"'

    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing = []

    out: list[str] = []
    written = False
    for current in existing:
        stripped = current.strip()
        is_key = stripped.startswith(f"{key}=") or stripped.startswith(f"{key} =")
        if is_key:
            if not written:
                out.append(line)
                written = True
            # drop duplicate key lines
        else:
            out.append(current)
    if not written:
        out.append(line)

    # Write to a sibling temp file then rename — a torn .env would lose keys.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o640)
    except OSError:
        # Non-POSIX host (dev box) has no real mode bits — harmless.
        logger.debug("Could not chmod %s to 640.", path)

    os.environ[key] = secret
    logger.info("Dashboard admin secret written to %s.", path)
