/**
 * File: shared.js
 * Description: Shared clock and public site configuration for kiosk and dashboard.
 * Project: smart_locker/frontend
 * Notes: Pages provide applyAssetLabel(label) for page-specific label updates.
 */

function tickSharedClock() {
  const now = new Date();
  const kioskTime = document.getElementById('clock-time');
  const kioskDate = document.getElementById('clock-date');
  const dashboardClock = document.getElementById('dash-clock');
  if (kioskTime) {
    kioskTime.textContent =
      now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  if (kioskDate) {
    kioskDate.textContent =
      now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }).toUpperCase();
  }
  if (dashboardClock) {
    dashboardClock.textContent =
      `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
  }
}

async function loadSharedSiteConfig(options = {}) {
  if (options.demo) return;
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const data = await res.json();
    if (data && typeof data.asset_label === 'string' && typeof applyAssetLabel === 'function') {
      applyAssetLabel(data.asset_label);
    }
    if (data && Number.isInteger(data.max_borrows)) {
      window.SMART_LOCKER_MAX_BORROWS = data.max_borrows;
      if (typeof setMenuBorrowCount === 'function') setMenuBorrowCount(window.__menuDevices || []);
    }
  } catch (_) { /* keep built-in labels */ }
}

tickSharedClock();
setInterval(tickSharedClock, 1000);
