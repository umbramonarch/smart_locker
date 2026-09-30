"""
File: test_update_sh.py
Description: Phase-2 end-to-end coverage for deploy/install/update.sh. Per
             docs/planning/dashboard-and-database.md section 4, the only tests
             for the updater are real runs of the script against a temporary
             appliance directory — no unit tests of the shell helpers.
Project: smart_locker/tests/e2e
Notes: Each case boots a fabricated "old" appliance (VERSION, .env, real
       SQLite DB, stub venv) and drops a locker-updates/ payload next to it,
       then runs the repo's update.sh under bash. systemctl/sudo/curl/pip are
       fakes and rsync is shimmed only when absent; discovery, the version
       gate, the SQLite online backup, the code swap, the real
       scripts.migrate_db, the /api/health gate, and rollback run for real.
"""

import shutil

import pytest

from tests.e2e.update_sandbox import (
    UpdateSandbox,
    find_bash,
    gnu_tools_available,
)

BASH = find_bash()

pytestmark = pytest.mark.skipif(
    BASH is None or not gnu_tools_available(BASH),
    reason="update.sh e2e tests need bash and GNU coreutils "
    "(sort -V, xargs -r, GNU tar, stat -c) on PATH",
)

OLD_ROW = ("PM-1", "Scope", 3, "available")

MIRROR_STATE = '{"seeded": true, "pending_writes": true}\n'
CATALOG_XLSX = "mirror-workbook-bytes\n"


def _write_mirror_runtime_files(sb: UpdateSandbox) -> None:
    """The mirror runtime files that live next to the DB on a real appliance:
    ``mirror_state.json`` (seeded flag, pending writes, the hand-edit gate,
    write baseline) and the default-path ``smart_locker_catalog.xlsx``.
    ``rsync --delete`` must keep both — losing them silently re-adopts the
    sheet (wiping the registrant list and applying pending edits unreviewed).
    """
    sb.path("mirror_state.json").write_text(MIRROR_STATE, encoding="utf-8")
    sb.path("smart_locker_catalog.xlsx").write_text(CATALOG_XLSX, encoding="utf-8")


def _assert_mirror_files_intact(sb: UpdateSandbox) -> None:
    assert (
        sb.path("mirror_state.json").read_text(encoding="utf-8") == MIRROR_STATE
    )
    assert (
        sb.path("smart_locker_catalog.xlsx").read_text(encoding="utf-8")
        == CATALOG_XLSX
    )


@pytest.fixture()
def sandbox(tmp_path):
    return UpdateSandbox(tmp_path, BASH).build()


@pytest.fixture()
def sandbox_no_cal(tmp_path):
    """Old appliance whose DB predates the calibration_due column."""
    return UpdateSandbox(tmp_path, BASH).build(include_calibration_column=False)


def _assert_old_tree_restored(sb: UpdateSandbox) -> None:
    """Code, venv, and images exactly as before the update."""
    assert sb.version() == "1.0.0"
    assert 'APP_MARK = "old-1.0.0"' in sb.path("smart_locker/app.py").read_text(
        encoding="utf-8"
    )
    # Files that only exist in the new tree must be gone again.
    assert not sb.path("smart_locker/new_feature.py").exists()
    assert not sb.path("BOOT_FAIL").exists()
    assert not sb.path("PIP_FAIL").exists()
    assert sb.device_rows() == [OLD_ROW]
    # The failed update's pip changes are gone too — the fake pip's mutation
    # marker means a rollback that skipped the venv would leak it.
    assert not sb.path("venv/PIP_MUTATED").exists()
    # New-release UI assets are gone; the runtime device photo is kept.
    assert not sb.path("smart_locker/frontend/images/new_ui.png").exists()
    assert sb.path("smart_locker/frontend/images/device_photo.jpg").exists()


def _assert_runtime_fixups_reapplied(sb: UpdateSandbox) -> None:
    """The swap's ownership/permission fixups also ran during the rollback —
    the restore leaves the tree root:root, so .env, logs/, backups/, the
    images dir and the DB must be re-granted or the old service cannot boot
    (.env unreadable, RotatingFileHandler raising, SQLite read-only)."""
    calls = sb.perms_log()
    for needle in ("/.env", "/logs", "frontend/images", "smart_locker.db"):
        hits = [c for c in calls if needle in c]
        assert len(hits) >= 2, (
            f"expected swap+rollback fixups for {needle!r}, got:\n{calls!r}\n"
            f"--- update.log ---\n{sb.update_log()}"
        )
    assert sum(1 for c in calls if c.startswith("chmod 640 ")) >= 2
    assert sum(1 for c in calls if c.startswith("chmod 1775 ")) >= 2


def _assert_rolled_back(sb: UpdateSandbox, env_before: bytes) -> None:
    """Previous code + DB restored, service back up on the old version."""
    status = sb.status_json()
    assert status["state"] == "rolled_back", (
        f"status={status!r}\n--- update.log ---\n{sb.update_log()}"
    )
    # The status reports the version actually on the box, not the failed one.
    assert status["version"] == "1.0.0"
    _assert_old_tree_restored(sb)
    assert sb.env_file.read_bytes() == env_before
    # The service the health gate saw afterwards is the old version.
    assert sb.service_state() == "running"
    last_answer = sb.curl_answers()[-1]
    assert '"version":"1.0.0"' in last_answer and "api/health" in last_answer
    _assert_runtime_fixups_reapplied(sb)


# ---------------------------------------------------------------------------
# A good payload is healthy; .env and the database are the pre-update files.
# ---------------------------------------------------------------------------


def test_good_payload_is_healthy_and_preserves_runtime_files(sandbox):
    env_before = sandbox.env_file.read_bytes()
    _write_mirror_runtime_files(sandbox)
    sandbox.write_payload("1.1.0")
    # A stale mirror state/workbook riding inside the payload must not be
    # adopted either — INCOMING_SKIP keeps them out of the staged tree.
    (sandbox.updates_dir / "mirror_state.json").write_text(
        '{"poison": true}\n', encoding="utf-8"
    )
    (sandbox.updates_dir / "smart_locker_catalog.xlsx").write_text(
        "stale\n", encoding="utf-8"
    )
    # The sandbox DB carries the pre-shared-slots UNIQUE slot index — the
    # payload's real migrate_db.py must downgrade it during the update.
    assert sandbox.slot_index_unique() is True

    r = sandbox.run_update()

    assert r.returncode == 0, (
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}\n--- update.log ---\n"
        f"{sandbox.update_log()}"
    )
    status = sandbox.status_json()
    assert status["state"] == "success"
    assert status["version"] == "1.1.0"
    assert sandbox.version() == "1.1.0"
    assert 'APP_MARK = "new-1.1.0"' in sandbox.path(
        "smart_locker/app.py"
    ).read_text(encoding="utf-8")
    assert sandbox.path("smart_locker/new_feature.py").exists()
    # The release's UI asset was overlaid next to the runtime photo.
    assert sandbox.path("smart_locker/frontend/images/new_ui.png").exists()
    assert sandbox.path("smart_locker/frontend/images/device_photo.jpg").exists()

    # Runtime files survived the code swap untouched — including the mirror
    # state and the default-path mirror workbook (PRESERVE + INCOMING_SKIP).
    assert sandbox.env_file.read_bytes() == env_before
    assert sandbox.device_rows() == [OLD_ROW]
    _assert_mirror_files_intact(sandbox)
    # The migrated index exists but is no longer UNIQUE — shared-slot writes
    # keep working after the update.
    assert sandbox.slot_index_unique() is False
    # The health gate saw the NEW version answering.
    assert '"version":"1.1.0"' in sandbox.curl_answers()[-1]

    # The service was stopped for the swap and restarted on the new version.
    assert sandbox.systemctl_calls() == [
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
    ]
    assert sandbox.service_state() == "running"

    # The rollback point exists and the new tree's sudoers refresh ran.
    backups = list(sandbox.path("backups").glob("*"))
    assert any(b.name.startswith("code-") for b in backups)
    assert any(b.name.startswith("db-") for b in backups)
    sudoers_log = sandbox.path("logs/sudoers.log")
    assert sudoers_log.exists() and "apply-sudoers ran" in sudoers_log.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Health, migrate, or install failure restores previous code and database,
# and the service afterwards is the old one.
# ---------------------------------------------------------------------------


def test_health_failure_rolls_back_code_and_db(sandbox):
    env_before = sandbox.env_file.read_bytes()
    _write_mirror_runtime_files(sandbox)
    sandbox.write_payload("1.1.0", boot_fail=True)

    r = sandbox.run_update()

    assert r.returncode == 1
    _assert_rolled_back(sandbox, env_before)


def test_rollback_restores_backup_from_pre_keyboard_install(sandbox):
    # An appliance installed before the on-screen keyboard shipped has no
    # frontend/keyboard.js — its backup snapshot legitimately fails the
    # strict payload check, so the restore uses the looser code-tree check.
    # Otherwise a failed update would leave the new code in place.
    env_before = sandbox.env_file.read_bytes()
    _write_mirror_runtime_files(sandbox)
    sandbox.path("smart_locker/frontend/keyboard.js").unlink()
    sandbox.write_payload("1.1.0", boot_fail=True)

    r = sandbox.run_update()

    assert r.returncode == 1
    _assert_rolled_back(sandbox, env_before)
    # The restore mirrored the old install exactly — keyboard.js stays absent.
    assert not sandbox.path("smart_locker/frontend/keyboard.js").exists()
    # The restore rsync --delete (BACKUP_SKIP) keeps the mirror files too.
    _assert_mirror_files_intact(sandbox)
    # stop -> swap -> start new (never healthy) -> rollback stop -> start old
    assert sandbox.systemctl_calls() == [
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
    ]


def test_install_failure_rolls_back(sandbox):
    env_before = sandbox.env_file.read_bytes()
    sandbox.write_payload("1.1.0", pip_fail=True)

    r = sandbox.run_update()

    assert r.returncode == 1
    _assert_rolled_back(sandbox, env_before)
    pip_log = sandbox.root / "pip.log"
    assert pip_log.exists() and "install" in pip_log.read_text(encoding="utf-8")
    # Failure before the new-version start: stop, then rollback stop + start.
    assert sandbox.systemctl_calls() == [
        "systemctl stop e2e-locker",
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
    ]


def test_migrate_failure_rolls_back(sandbox):
    env_before = sandbox.env_file.read_bytes()
    sandbox.write_payload("1.1.0", migrate_fail=True)

    r = sandbox.run_update()

    assert r.returncode == 1
    _assert_rolled_back(sandbox, env_before)
    assert "forced failure" in sandbox.update_log()
    assert sandbox.systemctl_calls() == [
        "systemctl stop e2e-locker",
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
    ]


# ---------------------------------------------------------------------------
# A missing or bad payload does not stop the running app.
# ---------------------------------------------------------------------------


def test_missing_payload_leaves_running_app_alone(sandbox):
    r = sandbox.run_update()

    assert r.returncode == 0
    assert sandbox.status_json()["state"] == "idle"
    assert sandbox.systemctl_calls() == []
    assert sandbox.service_state() == "running"
    assert sandbox.version() == "1.0.0"
    assert 'APP_MARK = "old-1.0.0"' in sandbox.path(
        "smart_locker/app.py"
    ).read_text(encoding="utf-8")


def test_rollback_unhealthy_when_service_never_recovers(sandbox):
    """Even the restored tree cannot boot (e.g. the box itself is the
    problem) — the updater must say so instead of reporting success."""
    sandbox.write_payload("1.1.0", boot_fail=True)
    (sandbox.root / "FORCE_START_FAIL").write_text("1\n", encoding="utf-8")

    r = sandbox.run_update()

    assert r.returncode == 1
    status = sandbox.status_json()
    assert status["state"] == "rollback_unhealthy", (
        f"status={status!r}\n--- update.log ---\n{sandbox.update_log()}"
    )
    # Reports the version that should be running — not the failed payload.
    assert status["version"] == "1.0.0"
    assert sandbox.service_state() == "failed"
    _assert_old_tree_restored(sandbox)
    _assert_runtime_fixups_reapplied(sandbox)
    assert sandbox.systemctl_calls() == [
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
    ]


# ---------------------------------------------------------------------------
# A payload found on USB media is staged to local disk first; a nested
# locker-updates tree is applied in place — never deleted by its own copy.
# ---------------------------------------------------------------------------


def test_usb_payload_is_staged_to_local_disk_and_applied(sandbox):
    dest = sandbox.write_usb_payload("1.1.0")
    # Mirror runtime files on the stick are not payload — PAYLOAD_SKIP drops
    # them from the USB → locker-updates copy.
    (dest / "mirror_state.json").write_text('{"poison": true}\n', encoding="utf-8")
    (dest / "smart_locker_catalog.xlsx").write_text("stale\n", encoding="utf-8")

    r = sandbox.run_update()

    assert r.returncode == 0, (
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}\n--- update.log ---\n"
        f"{sandbox.update_log()}"
    )
    assert sandbox.status_json()["state"] == "success"
    assert sandbox.version() == "1.1.0"
    # The stick's tree was copied off the media root into locker-updates/.
    assert "Copying incoming tree" in sandbox.update_log()
    assert (
        sandbox.updates_dir / "VERSION"
    ).read_text(encoding="utf-8").strip() == "1.1.0"
    assert not (sandbox.updates_dir / "mirror_state.json").exists()
    assert not (sandbox.updates_dir / "smart_locker_catalog.xlsx").exists()


def test_nested_payload_is_not_deleted_by_its_own_staging_copy(sandbox):
    """locker-updates/<child>/ is itself the repo tree: the staging copy must
    not `rm -rf locker-updates` — that would delete the source mid-update."""
    nested = sandbox.write_nested_payload("1.1.0")

    r = sandbox.run_update()

    assert r.returncode == 0, (
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}\n--- update.log ---\n"
        f"{sandbox.update_log()}"
    )
    assert sandbox.status_json()["state"] == "success"
    assert sandbox.version() == "1.1.0"
    # The payload tree survived in place.
    assert (nested / "VERSION").exists()


# ---------------------------------------------------------------------------
# A backup failure aborts before any change — and the ERR trap fires once.
# ---------------------------------------------------------------------------


def test_backup_failure_aborts_before_stopping_the_service(sandbox):
    sandbox.write_payload("1.1.0")
    (sandbox.root / "TAR_CREATE_FAIL").write_text("1\n", encoding="utf-8")

    r = sandbox.run_update()

    assert r.returncode == 1
    assert sandbox.status_json()["state"] == "failed"
    assert sandbox.systemctl_calls() == []
    assert sandbox.service_state() == "running"
    assert sandbox.version() == "1.0.0"
    # on_err ran exactly once — the ERR trap did not double-fire from the
    # command-substitution/subshell boundary — and no rollback ran (nothing
    # was backed up to roll back to).
    assert sandbox.update_log().count("ERROR on line") == 1
    assert "ROLLBACK" not in sandbox.update_log()


def test_bad_or_older_payload_never_stops_the_service(sandbox):
    # Garbage in locker-updates/ is not a repo tree — nothing happens.
    sandbox.write_garbage_payload()
    r = sandbox.run_update()
    assert r.returncode == 0
    assert sandbox.status_json()["state"] == "idle"
    assert sandbox.systemctl_calls() == []

    # A valid tree that is older than the running version is refused before
    # the service is stopped — a downgrade must not cost uptime either.
    sandbox.write_payload("0.9.0")
    r = sandbox.run_update()
    assert r.returncode == 1
    assert sandbox.status_json()["state"] == "failed"
    assert sandbox.systemctl_calls() == []
    assert sandbox.service_state() == "running"
    assert sandbox.version() == "1.0.0"


def test_payload_missing_frontend_file_is_refused(sandbox):
    # A tree that has app.py/requirements.txt/update.sh but lost the kiosk
    # frontend (hand-assembled or partial copy) must not rsync in and
    # --delete the installed UI — it is not a repo tree.
    sandbox.write_payload("1.1.0")
    (sandbox.updates_dir / "smart_locker/frontend/keyboard.js").unlink()
    r = sandbox.run_update()
    assert r.returncode == 0
    assert sandbox.status_json()["state"] == "idle"
    assert sandbox.systemctl_calls() == []
    assert sandbox.service_state() == "running"
    assert sandbox.version() == "1.0.0"


# ---------------------------------------------------------------------------
# A second update after a good one follows the same rules.
# ---------------------------------------------------------------------------


def test_second_update_after_a_good_one(sandbox):
    sandbox.write_payload("1.1.0")
    r1 = sandbox.run_update()
    assert r1.returncode == 0
    assert sandbox.version() == "1.1.0"

    # The staged payload dir survives the swap (PRESERVE) — replace it with
    # the next stick's tree and run the updater again.
    sandbox.write_payload("1.2.0")
    r2 = sandbox.run_update()

    assert r2.returncode == 0, sandbox.update_log()
    assert sandbox.status_json()["state"] == "success"
    assert sandbox.version() == "1.2.0"
    assert 'APP_MARK = "new-1.2.0"' in sandbox.path(
        "smart_locker/app.py"
    ).read_text(encoding="utf-8")
    assert sandbox.device_rows() == [OLD_ROW]
    assert sandbox.systemctl_calls() == [
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
        "systemctl stop e2e-locker",
        "systemctl start e2e-locker",
    ]


# ---------------------------------------------------------------------------
# A migration that adds a column runs; a failed boot after that migration
# restores the previous database file, not a half-migrated one.
# ---------------------------------------------------------------------------


def test_migration_adding_a_column_runs(sandbox_no_cal):
    assert "calibration_due" not in sandbox_no_cal.device_columns()
    sandbox_no_cal.write_payload("1.1.0")

    r = sandbox_no_cal.run_update()

    assert r.returncode == 0, sandbox_no_cal.update_log()
    assert "ADD   devices.calibration_due" in sandbox_no_cal.update_log()
    assert "calibration_due" in sandbox_no_cal.device_columns()
    assert sandbox_no_cal.device_rows() == [OLD_ROW]


def test_failed_boot_after_migration_restores_pre_migration_db(sandbox_no_cal):
    assert "calibration_due" not in sandbox_no_cal.device_columns()
    env_before = sandbox_no_cal.env_file.read_bytes()
    sandbox_no_cal.write_payload("1.1.0", boot_fail=True)

    r = sandbox_no_cal.run_update()

    assert r.returncode == 1
    # The migration really ran before the boot failure ...
    assert "ADD   devices.calibration_due" in sandbox_no_cal.update_log()
    # ... and the restored database is the pre-update file, so the column the
    # failed version added is gone again.
    assert "calibration_due" not in sandbox_no_cal.device_columns()
    assert sandbox_no_cal.device_rows() == [OLD_ROW]
    _assert_rolled_back(sandbox_no_cal, env_before)
