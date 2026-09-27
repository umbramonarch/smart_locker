"""
File: setup_service.py
Description: First-boot Setup. Decides whether the locker has no active admin
             (Setup is open) and stores the dashboard admin password typed at
             Setup into the service-owned dashboard.secret file.
Project: smart_locker/services
Notes: The secret cannot go in .env: on the installed appliance .env is
       root-owned and the app directory is sticky, so the service cannot
       replace it — and must not, since update.sh parses .env as root.
       dashboard.secret is created and owned by the service account, survives
       rsync --delete via update.sh PRESERVE, and is re-owned by
       apply_runtime_permissions after every update. The write also updates
       os.environ so the running process accepts the password immediately.
       The secret value is never logged.
"""

import logging
import os

from sqlalchemy.orm import Session

from config.settings import (
    DASHBOARD_ADMIN_SECRET_ENV_VAR,
    dashboard_secret_path,
)
from smart_locker.database.repositories import UserRepository

logger = logging.getLogger(__name__)


def setup_needed(db_session: Session) -> bool:
    """Whether Setup is open: True while the database has no active admin.

    Setup closes the moment one active admin exists — the card tap is what
    finishes it, not the arming POST.
    """
    return UserRepository.first_active_admin(db_session) is None


def write_dashboard_secret(secret: str) -> None:
    """Persist the dashboard admin password and apply it to this process.

    Atomically writes ``secret`` to ``dashboard_secret_path()`` (temp file +
    rename — the service owns the target, so this works inside the sticky
    root-owned app directory), applies mode 640, and sets ``os.environ`` so
    ``dashboard_admin_secret()`` returns it without a restart. Callers must
    check ``dashboard_admin_secret()`` first — this function always writes.

    Args:
        secret: The password to store. Must not contain CR/LF.

    Raises:
        ValueError: If the secret contains a newline.
        OSError: If the file cannot be written.
    """
    if "\n" in secret or "\r" in secret:
        raise ValueError("Secret must not contain newlines.")

    path = dashboard_secret_path()

    # Write to a sibling temp file then rename — a torn file would lose the
    # only copy of the dashboard password.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(secret + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o640)
    except OSError:
        # Non-POSIX host (dev box) has no real mode bits — harmless.
        logger.debug("Could not chmod %s to 640.", path)

    os.environ[DASHBOARD_ADMIN_SECRET_ENV_VAR] = secret
    logger.info("Dashboard admin secret written to %s.", path)
