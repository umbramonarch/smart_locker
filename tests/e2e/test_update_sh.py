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

from tests.e2e.update_sandbox import UpdateSandbox, find_bash

BASH = find_bash()

pytestmark = pytest.mark.skipif(
    BASH is None, reason="update.sh e2e tests need GNU bash on PATH"
)

OLD_ROW = ("PM-1", "Scope", 3, "available")


@pytest.fixture()
def sandbox(tmp_path):
    return UpdateSandbox(tmp_path, BASH).build()


@pytest.fixture()
def sandbox_no_cal(tmp_path):
    """Old appliance whose DB predates the calibration_due column."""
    return UpdateSandbox(tmp_path, BASH).build(include_calibration_column=False)


def _assert_rolled_back(sb: UpdateSandbox, env_before: bytes) -> None:
    """Previous code + DB restored, service back up on the old version."""
    status = sb.status_json()
    assert status["state"] == "rolled_back", (
        f"status={status!r}\n--- update.log ---\n{sb.update_log()}"
    )
    assert sb.version() == "1.0.0"
    assert 'APP_MARK = "old-1.0.0"' in sb.path("smart_locker/app.py").read_text(
        encoding="utf-8"
    )
    # Files that only exist in the new tree must be gone again.
    assert not sb.path("smart_locker/new_feature.py").exists()
    assert not sb.path("BOOT_FAIL").exists()
    assert not sb.path("PIP_FAIL").exists()
    assert sb.env_file.read_bytes() == env_before
    assert sb.device_rows() == [OLD_ROW]
    # The service the health gate sees afterwards is the old version.
    assert sb.service_state() == "running"


# ---------------------------------------------------------------------------
# A good payload is healthy; .env and the database are the pre-update files.
# ---------------------------------------------------------------------------


def test_good_payload_is_healthy_and_preserves_runtime_files(sandbox):
    env_before = sandbox.env_file.read_bytes()
    sandbox.write_payload("1.1.0")

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

    # Runtime files survived the code swap untouched.
    assert sandbox.env_file.read_bytes() == env_before
    assert sandbox.device_rows() == [OLD_ROW]

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
    sandbox.write_payload("1.1.0", boot_fail=True)

    r = sandbox.run_update()

    assert r.returncode == 1
    _assert_rolled_back(sandbox, env_before)
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
    sandbox_no_cal.write_payload("1.1.0", boot_fail=True)

    r = sandbox_no_cal.run_update()

    assert r.returncode == 1
    # The migration really ran before the boot failure ...
    assert "ADD   devices.calibration_due" in sandbox_no_cal.update_log()
    # ... and the restored database is the pre-update file, so the column the
    # failed version added is gone again.
    assert "calibration_due" not in sandbox_no_cal.device_columns()
    assert sandbox_no_cal.device_rows() == [OLD_ROW]
    _assert_rolled_back(sandbox_no_cal, sandbox_no_cal.env_file.read_bytes())
