"""
File: user_admin_lock.py
Description: Process-level lock serializing user deactivation against
             borrow/transfer commits.
Project: smart_locker/services
Notes: Kept in its own leaf module (stdlib only) so both the HTTP routes
       and LockerService share one object without an import cycle.
       uvicorn runs a single worker, so one process-level lock suffices.
       Never hold this lock while acquiring pending_state_lock.
"""

import threading

# Serializes admin deactivate guard-plus-commit with LockerService
# borrow/transfer guard-plus-commit, preserving the no-held-devices
# invariant: a deactivated user holds no devices, and a deactivated user
# cannot gain holdings. Returns are intentionally NOT serialized — they
# can only shrink holdings and must stay open for deactivated holders.
user_admin_lock = threading.Lock()
