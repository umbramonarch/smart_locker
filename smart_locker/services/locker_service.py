"""
File: locker_service.py
Description: Borrow/return business logic for the Smart Locker system. Enforces
             per-user borrow limits, device availability checks, ownership rules,
             admin return-on-behalf, and unattended idle return with full
             transaction logging. After a successful location change, the
             catalog mirror is marked dirty (best-effort write-back).
Project: smart_locker/services
Notes: The borrow limit is configured via MAX_BORROWS in config/settings.py
       (default 5). Admins can return any device on behalf of the original
       borrower. Idle sticker taps use return_unattended (no work card).
       The mirror write never raises; SQLite remains the source of truth.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from config.settings import MAX_BORROWS
from smart_locker.auth.session_manager import UserSession
from smart_locker.database.models import Device, DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, TransactionRepository, UserRepository
from smart_locker.services.calibration import borrow_block_reason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoanOutcome:
    """Outcome of a borrow/transfer attempt.

    ``reason`` is a short kiosk-safe refusal clause (no device name, no
    digests) that the API layer appends to its ``Could not … {name}`` line;
    empty means the generic message — e.g. the id did not resolve. Truthy
    when the loan succeeded so bool-style callers keep working.
    """

    success: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.success


def _write_location(db_session: Session) -> None:
    """Mark a changed loan for a mirror write after its caller commits SQLite."""
    db_session.info["mirror_dirty_pending"] = True


@event.listens_for(Session, "after_commit")
def _schedule_committed_location(session: Session) -> None:
    """Only committed loan changes can trigger a workbook write."""
    if session.info.pop("mirror_dirty_pending", False):
        from smart_locker.sync import mirror

        mirror.mark_dirty()
        mirror.schedule_flush()


@event.listens_for(Session, "after_rollback")
def _discard_uncommitted_location(session: Session) -> None:
    session.info.pop("mirror_dirty_pending", None)


class LockerService:
    """Business logic for device borrow and return operations.

    Enforces per-user borrow limits (MAX_BORROWS from config), device
    availability checks, ownership rules for returns, admin
    return-on-behalf, and unattended return from the idle kiosk. All
    operations are logged to the transaction audit trail.
    """

    @staticmethod
    def borrow_device(
        db_session: Session,
        user_session: UserSession,
        device_id: int,
        notes: str | None = None,
    ) -> LoanOutcome:
        """Borrow a device for the current user.

        Args:
            db_session: Active database session.
            user_session: Current authenticated user session.
            device_id: ID of device to borrow.
            notes: Optional notes for the transaction.

        Returns:
            LoanOutcome — truthy on success; ``reason`` says why a refusal
            happened (no sticker, calibration due/overdue, maintenance,
            limit, …).
        """
        if user_session.is_expired:
            logger.warning("Borrow attempted with expired session.")
            return LoanOutcome(False, "session expired — tap your card again")

        user = user_session.user

        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None or device.locker_slot is None:
            # Catalog-only rows are not in the cabinet — they cannot be borrowed.
            logger.warning("Borrow failed: device %d not found.", device_id)
            return LoanOutcome(False)

        if device.tag_hmac is None:
            # No sticker bound — the unit waits on the admin manage list for
            # one. A borrowed-untagged row stays returnable via return_device.
            logger.warning(
                "Borrow failed: device %d (%s) has no sticker bound.",
                device_id,
                device.name,
            )
            return LoanOutcome(False, "no sticker bound")

        if device.status != DeviceStatus.AVAILABLE:
            reason = (
                "in maintenance"
                if device.status == DeviceStatus.MAINTENANCE
                else "already borrowed"
            )
            logger.warning(
                "Borrow failed: device %d (%s) is %s.",
                device_id,
                device.name,
                device.status.value,
            )
            return LoanOutcome(False, reason)

        block = borrow_block_reason(device.calibration_due)
        if block is not None:
            logger.warning(
                "Borrow refused: %s (device=%d) — %s.",
                device.name,
                device_id,
                block,
            )
            return LoanOutcome(False, block)

        borrowed_count = DeviceRepository.count_borrowed_by_user(db_session, user.id)
        if borrowed_count >= MAX_BORROWS:
            logger.warning(
                "Borrow failed: %s has reached the borrow limit (%d/%d).",
                user.display_name,
                borrowed_count,
                MAX_BORROWS,
            )
            return LoanOutcome(
                False, f"borrow limit reached ({borrowed_count}/{MAX_BORROWS})"
            )

        if not DeviceRepository.borrow(db_session, device, user.id):
            # The status checked above was a stale snapshot — another
            # session committed a different state first (e.g. dashboard
            # maintenance). Read the winning state and refuse with the
            # same reasons as the early check.
            try:
                db_session.refresh(device)
            except InvalidRequestError:
                # The row was deleted mid-race — a plain refusal.
                return LoanOutcome(False)
            if device.tag_hmac is None:
                logger.warning(
                    "Borrow failed: device %d (%s) lost its sticker during the check.",
                    device_id,
                    device.name,
                )
                return LoanOutcome(False, "no sticker bound")
            reason = (
                "in maintenance"
                if device.status == DeviceStatus.MAINTENANCE
                else "already borrowed"
            )
            logger.warning(
                "Borrow failed: device %d (%s) became %s during the check.",
                device_id,
                device.name,
                device.status.value,
            )
            return LoanOutcome(False, reason)

        TransactionRepository.log_borrow(db_session, user.id, device_id, notes)
        user_session.touch()
        _write_location(db_session)

        logger.info(
            "%s borrowed %s (device=%d)",
            user.display_name,
            device.name,
            device_id,
        )
        return LoanOutcome(True)

    @staticmethod
    def return_device(
        db_session: Session,
        user_session: UserSession,
        device_id: int,
        notes: str | None = None,
    ) -> bool:
        """Return a borrowed device.

        Args:
            db_session: Active database session.
            user_session: Current authenticated user session.
            device_id: ID of device to return.
            notes: Optional notes for the transaction.

        Returns:
            True if return succeeded, False otherwise.
        """
        if user_session.is_expired:
            logger.warning("Return attempted with expired session.")
            return False

        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None:
            logger.warning("Return failed: device %d not found.", device_id)
            return False

        if device.status != DeviceStatus.BORROWED:
            logger.warning(
                "Return failed: device %d (%s) is not borrowed.",
                device_id,
                device.name,
            )
            return False

        user = user_session.user
        if device.current_borrower_id != user.id:
            if user.role != UserRole.ADMIN:
                logger.warning(
                    "Return failed: device %d is borrowed by user %d, not %d.",
                    device_id,
                    device.current_borrower_id,
                    user.id,
                )
                return False

            # Admin returning on behalf of the original borrower
            original_borrower_id = device.current_borrower_id
            DeviceRepository.return_device(db_session, device)
            TransactionRepository.log_return(
                db_session,
                user_id=original_borrower_id,
                device_id=device_id,
                notes=notes,
                performed_by_id=user.id,
            )
            user_session.touch()
            _write_location(db_session)
            logger.info(
                "Admin %s returned %s (device=%d) on behalf of user %d",
                user.display_name,
                device.name,
                device_id,
                original_borrower_id,
            )
            return True

        DeviceRepository.return_device(db_session, device)
        TransactionRepository.log_return(db_session, user.id, device_id, notes)
        user_session.touch()
        _write_location(db_session)

        logger.info(
            "%s returned %s (device=%d)",
            user.display_name,
            device.name,
            device_id,
        )
        return True

    @staticmethod
    def check_transfer(
        db_session: Session,
        user_session: UserSession,
        device: Device | None,
    ) -> LoanOutcome:
        """Preflight a handover: truthy when a transfer to this session may proceed.

        Runs the same refusal ladder as ``transfer_device`` in the same order
        — expired session, missing device, not borrowed, already held, no
        recorded borrower, calibration block, borrow limit — so a caller can
        refuse before offering the handover prompt.

        Args:
            db_session: Active database session.
            user_session: Current authenticated user session.
            device: Already-loaded device row, or None when the id missed.

        Returns:
            LoanOutcome — truthy when the transfer may proceed; ``reason``
            says why a refusal happened.
        """
        if user_session.is_expired:
            logger.warning("Transfer attempted with expired session.")
            return LoanOutcome(False, "session expired — tap your card again")

        if device is None:
            logger.warning("Transfer failed: device not found.")
            return LoanOutcome(False)

        if device.status != DeviceStatus.BORROWED:
            logger.warning(
                "Transfer failed: device %d (%s) is not borrowed.",
                device.id,
                device.name,
            )
            return LoanOutcome(False, "not borrowed")

        user = user_session.user
        if device.current_borrower_id == user.id:
            logger.warning(
                "Transfer failed: device %d is already held by %s.",
                device.id,
                user.display_name,
            )
            return LoanOutcome(False, "already held by you")

        if device.current_borrower_id is None:
            logger.warning(
                "Transfer failed: device %d has no recorded borrower.",
                device.id,
            )
            return LoanOutcome(False)

        block = borrow_block_reason(device.calibration_due)
        if block is not None:
            logger.warning(
                "Transfer refused: %s (device=%d) — %s.",
                device.name,
                device.id,
                block,
            )
            return LoanOutcome(False, block)

        new_borrowed_count = DeviceRepository.count_borrowed_by_user(
            db_session, user.id
        )
        if new_borrowed_count >= MAX_BORROWS:
            logger.warning(
                "Transfer failed: %s has reached the borrow limit (%d/%d).",
                user.display_name,
                new_borrowed_count,
                MAX_BORROWS,
            )
            return LoanOutcome(
                False, f"borrow limit reached ({new_borrowed_count}/{MAX_BORROWS})"
            )

        return LoanOutcome(True)

    @staticmethod
    def transfer_device(
        db_session: Session,
        user_session: UserSession,
        device_id: int,
        notes: str | None = None,
    ) -> LoanOutcome:
        """Transfer responsibility for a borrowed device to the current user.

        Records a return for the original borrower and a borrow for the new
        user, so the audit trail is preserved. The device stays borrowed;
        only current_borrower_id changes. Enforces the new user's borrow
        limit and the same calibration block as a fresh borrow — a handover
        must not keep an overdue unit in circulation.

        Args:
            db_session: Active database session.
            user_session: Current authenticated user session.
            device_id: ID of the device to transfer.
            notes: Optional notes for the transaction.

        Returns:
            LoanOutcome — truthy on success; ``reason`` says why a refusal
            happened.
        """
        device = DeviceRepository.find_by_id(db_session, device_id)
        outcome = LockerService.check_transfer(db_session, user_session, device)
        if not outcome:
            return outcome

        user = user_session.user
        original_borrower_id = device.current_borrower_id
        original_borrower = UserRepository.find_by_id(db_session, original_borrower_id)
        original_name = original_borrower.display_name if original_borrower else "unknown"

        DeviceRepository.return_device(db_session, device)
        TransactionRepository.log_return(
            db_session,
            user_id=original_borrower_id,
            device_id=device_id,
            notes=f"transferred to {user.display_name}",
        )
        # Defensive: our own return flush made the row AVAILABLE inside this
        # transaction, so a refused borrow here can only be a raced write —
        # refuse rather than overwrite the winning state.
        if not DeviceRepository.borrow(db_session, device, user.id):
            return LoanOutcome(False)
        TransactionRepository.log_borrow(
            db_session,
            user.id,
            device_id,
            notes=f"transferred from {original_name}",
        )
        user_session.touch()
        _write_location(db_session)

        logger.info(
            "%s transferred %s (device=%d) from user %d",
            user.display_name,
            device.name,
            device_id,
            original_borrower_id,
        )
        return LoanOutcome(True)

    @staticmethod
    def return_unattended(db_session: Session, device_id: int) -> bool:
        """Return a borrowed device without a work-card session.

        Used when a borrowed device sticker is tapped on the idle kiosk.
        The original borrower stays on the transaction log; notes record
        that no card was presented.

        Args:
            db_session: Active database session.
            device_id: ID of the device to return.

        Returns:
            True if return succeeded, False otherwise.
        """
        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None:
            logger.warning("Unattended return failed: device %d not found.", device_id)
            return False

        if device.status != DeviceStatus.BORROWED:
            logger.warning(
                "Unattended return failed: device %d (%s) is not borrowed.",
                device_id,
                device.name,
            )
            return False

        original_borrower_id = device.current_borrower_id
        if original_borrower_id is None:
            logger.warning(
                "Unattended return failed: device %d (%s) has no borrower.",
                device_id,
                device.name,
            )
            return False

        DeviceRepository.return_device(db_session, device)
        TransactionRepository.log_return(
            db_session,
            user_id=original_borrower_id,
            device_id=device_id,
            notes="returned at kiosk without card",
        )
        _write_location(db_session)
        logger.info(
            "Unattended return of %s (device=%d) for user %d",
            device.name,
            device_id,
            original_borrower_id,
        )
        return True
