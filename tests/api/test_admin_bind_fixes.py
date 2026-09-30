"""
File: test_admin_bind_fixes.py
Description: Regression tests for the admin bind/slot hardening — change-slot
             moves only real locker units and flags the mirror, kiosk bind-tag
             refuses slot-less catalog rows and arms the reader atomically,
             unbind clears are scoped to the same device, and catalog delete
             refuses while a bind window is live on the row.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_admin_bind_fixes.py -v
"""
from smart_locker.api.app_context import (
    PendingRegistration,
    PendingTagBind,
    clear_pending_tag_bind_for_device,
    clear_pending_tag_bind_if,
)
from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import DeviceRepository

from tests.api.helpers import dashboard_admin_headers


class TestSetSlotRegisteredOnly:
    """POST /api/admin/devices/{id}/slot moves locker units, not catalog rows."""

    def test_set_slot_catalog_row_is_409(
        self, client, mock_context, admin_user, db_session
    ):
        """A slot-less catalog row cannot be promoted through change-slot."""
        device = DeviceRepository.create(
            db_session,
            name="Van kit",
            device_type="Tool",
            pm_number="PM-900",
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{device.id}/slot",
            json={"locker_slot": 7},
        )
        assert resp.status_code == 409
        db_session.expire_all()
        assert device.locker_slot is None
        assert mock_context.pending_tag_bind is None

    def test_set_slot_registered_flags_mirror(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Moving a locker unit returns 200 and marks the mirror dirty."""
        from smart_locker.sync import mirror

        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 9},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert test_devices[0].locker_slot == 9
        # The request session's commit ran the after-commit listener → mark_dirty.
        assert mirror.mirror_status()["pending_writes"] is True


class TestKioskBindTagGuards:
    """Kiosk bind-tag is locker-units-only and owns the reader atomically."""

    def test_bind_tag_catalog_row_is_404(
        self, client, mock_context, admin_user, db_session
    ):
        """A sticker must never bind to a slot-less catalog row."""
        device = DeviceRepository.create(
            db_session,
            name="Van kit",
            device_type="Tool",
            pm_number="PM-900",
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{device.id}/bind-tag")
        assert resp.status_code == 404
        assert mock_context.pending_tag_bind is None

    def test_bind_tag_registered_arms_window(
        self, client, mock_context, admin_user, test_devices
    ):
        """A locker unit arms pending_tag_bind for that device."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        assert mock_context.pending_tag_bind.is_expired is False

    def test_bind_tag_refuses_pending_registration(
        self, client, mock_context, admin_user, test_devices
    ):
        """The atomic arm refuses while a card registration owns the reader."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = PendingRegistration("Someone")
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 409
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.display_name == "Someone"
        assert mock_context.pending_tag_bind is None

    def test_bind_tag_borrowed_is_409(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Arming a rebind on a borrowed unit is refused — the sticker
        physically on the loaned unit is what returns the loan."""
        test_devices[0].status = DeviceStatus.BORROWED
        test_devices[0].current_borrower_id = admin_user.id
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 409
        assert "borrowed" in resp.json()["detail"].lower()
        assert mock_context.pending_tag_bind is None

    def test_dashboard_bind_tag_borrowed_is_409(
        self, client, mock_context, test_devices, db_session, dashboard_secret
    ):
        """The dashboard bind arm has the same borrowed-unit refusal."""
        test_devices[0].status = DeviceStatus.BORROWED
        db_session.commit()
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": test_devices[0].pm_number},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert "borrowed" in resp.json()["detail"].lower()
        assert mock_context.pending_tag_bind is None


class TestUnbindScopedClear:
    """An unbind cancels only a bind window aimed at that same device."""

    def test_kiosk_unbind_keeps_other_devices_window(
        self, client, mock_context, admin_user, test_devices
    ):
        """Unbinding B leaves A's armed window; unbinding A clears it."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=test_devices[0].id
        )
        resp = client.post(f"/api/admin/devices/{test_devices[1].id}/unbind-tag")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_dashboard_unbind_keeps_other_devices_window(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """The dashboard unbind scopes the clear to its PM's device too."""
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=test_devices[0].id
        )
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": test_devices[1].pm_number},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": test_devices[0].pm_number},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None


class TestPendingBindClearHelpers:
    """The scoped clear helpers leave windows they do not own alone."""

    def test_clear_pending_tag_bind_if_identity(self, mock_context):
        """Only the exact armed object clears — a later arm survives."""
        armed = PendingTagBind(device_id=1)
        mock_context.pending_tag_bind = armed
        clear_pending_tag_bind_if(mock_context, PendingTagBind(device_id=1))
        assert mock_context.pending_tag_bind is armed
        clear_pending_tag_bind_if(mock_context, armed)
        assert mock_context.pending_tag_bind is None

    def test_clear_pending_tag_bind_for_device_id(self, mock_context):
        """Device-scoped clear matches on device_id only."""
        armed = PendingTagBind(device_id=1)
        mock_context.pending_tag_bind = armed
        clear_pending_tag_bind_for_device(mock_context, 2)
        assert mock_context.pending_tag_bind is armed
        clear_pending_tag_bind_for_device(mock_context, 1)
        assert mock_context.pending_tag_bind is None


class TestDashboardDeleteBindGuard:
    """DELETE /api/dashboard/devices/{pm} honors a live bind window."""

    def test_delete_with_live_bind_is_409(
        self, client, mock_context, test_devices, db_session, dashboard_secret
    ):
        """The row cannot vanish while the next tap would bind to it."""
        db_session.commit()
        pm = test_devices[0].pm_number
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=test_devices[0].id
        )
        resp = client.delete(
            f"/api/dashboard/devices/{pm}",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        db_session.expire_all()
        assert DeviceRepository.find_by_pm(db_session, pm) is not None
        assert mock_context.pending_tag_bind is not None

    def test_delete_with_other_devices_bind_is_200(
        self, client, mock_context, test_devices, db_session, dashboard_secret
    ):
        """A live window on another row does not block this delete."""
        db_session.commit()
        pm = test_devices[0].pm_number
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=test_devices[1].id
        )
        resp = client.delete(
            f"/api/dashboard/devices/{pm}",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert DeviceRepository.find_by_pm(db_session, pm) is None
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == test_devices[1].id

    def test_delete_with_expired_bind_is_200(
        self, client, mock_context, test_devices, db_session, dashboard_secret
    ):
        """An expired window does not block the delete."""
        import time

        db_session.commit()
        pm = test_devices[0].pm_number
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=test_devices[0].id,
            created_at=time.monotonic() - 120,
        )
        resp = client.delete(
            f"/api/dashboard/devices/{pm}",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert DeviceRepository.find_by_pm(db_session, pm) is None
