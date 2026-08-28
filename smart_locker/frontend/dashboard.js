/**
 * @fileoverview Public dashboard: Inventory (Excel), Locker (SQLite), and
 *               Display (kiosk snapshot). Sort, search, status filter, and
 *               polling. Owner change is Inventory only (not locker PMs).
 *               5-tap the header clock for users, logs, and NFC unbind /
 *               arm-bind. No login. No remote control of the kiosk.
 * @project smart_locker/frontend
 * @description Tabs switch locally. Inventory errors (share down) leave
 *              the Locker tab usable. Asset-label text comes from /api/config.
 *              Dashboard 5-tap is not authorization; bind/unbind send
 *              X-Smart-Locker-Admin from a prompted secret.
 */

/* ── State ────────────────────────────────────────────────────────────────── */

/** Cached Inventory rows from the last successful Excel fetch. */
let inventoryData = [];
/** Cached Locker rows from SQLite. */
let devicesData = [];
/** True when Inventory last failed (share down / unreadable Excel). */
let inventoryError = '';

/** Current sort configuration per table. */
const sortState = {
  inventory: { key: null, dir: 'asc' },
  devices:   { key: null, dir: 'asc' },
};

/** Active status filter for the Locker table. */
let activeFilter = 'all';

/** Inventory search string (case-insensitive substring). */
let inventoryQuery = '';

/** How often to poll Inventory and Locker (milliseconds). */
const REFRESH_MS = 30_000;
/** How often to poll the kiosk Display snapshot. */
const DISPLAY_MS = 2_000;
/** Debounce for Inventory search so each keystroke is not a full tbody rebuild. */
const SEARCH_DEBOUNCE_MS = 200;

/** In-flight ``fetchTables`` promise, or null. */
let tablesInFlight = null;
/** Inventory search debounce timer. */
let inventorySearchTimer = 0;
/** Last Inventory render fingerprint (skip identical tbody rebuilds). */
let lastInventoryStamp = '';
/** Last Locker render fingerprint. */
let lastDevicesStamp = '';

/** Names for the owner datalist (users + registrants + in-locker token). */
let ownerNames = [];
/** PM currently open in the owner dialog, or ''. */
let ownerEditPm = '';

/** Cached registered users for the 5-tap overlay. */
let usersData = [];
/** Cached transaction rows for the 5-tap overlay. */
let txData = [];

const adminTaps = [];
const ADMIN_TAP_COUNT = 5;
const ADMIN_TAP_WINDOW = 3000;

/** sessionStorage key for the dashboard admin secret (header value). */
const ADMIN_SECRET_KEY = 'smartLockerAdminSecret';
/** Header name matching config.settings.DASHBOARD_ADMIN_HEADER. */
const ADMIN_HEADER = 'X-Smart-Locker-Admin';


/**
 * Headers for dashboard admin POSTs. Secret comes from sessionStorage.
 *
 * @returns {Object<string, string>} Fetch headers including Content-Type.
 */
function dashboardAdminHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  const secret = sessionStorage.getItem(ADMIN_SECRET_KEY) || '';
  if (secret) headers[ADMIN_HEADER] = secret;
  return headers;
}


/**
 * Prompt once per tab for the dashboard admin secret if it is not stored.
 *
 * @returns {string} Secret string, possibly empty if the prompt was cancelled.
 */
function ensureDashboardAdminSecret() {
  let secret = sessionStorage.getItem(ADMIN_SECRET_KEY) || '';
  if (!secret) {
    secret = window.prompt('Dashboard admin secret') || '';
    if (secret) sessionStorage.setItem(ADMIN_SECRET_KEY, secret);
  }
  return secret;
}


/**
 * Apply the site join-key label to Inventory and Locker column headers.
 * @param {string} label - Display noun from GET /api/config.
 */
function applyAssetLabel(label) {
  const text = (label || '').trim();
  if (!text) return;
  ['col-asset-label', 'col-inventory-asset-label'].forEach(id => {
    const th = document.getElementById(id);
    if (th) th.textContent = text;
  });
}

/**
 * Load SMART_LOCKER_ASSET_LABEL from the public config endpoint.
 * @returns {Promise<void>}
 */
async function loadSiteConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const data = await res.json();
    if (data && typeof data.asset_label === 'string') applyAssetLabel(data.asset_label);
  } catch (_) { /* keep built-in column title */ }
}


/* ── Tabs ─────────────────────────────────────────────────────────────────── */

/**
 * Show one tab panel and mark its button selected.
 * @param {string} name - 'inventory' | 'locker' | 'display'.
 */
function showTab(name) {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    const on = btn.dataset.tab === name;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.hidden = panel.id !== `panel-${name}`;
  });
  if (name === 'display') fetchDisplay();
  else fetchTables();
}


/**
 * Currently selected public tab id.
 *
 * @returns {string} 'inventory' | 'locker' | 'display'.
 */
function activeTabName() {
  const on = document.querySelector('.tab-btn.active');
  return (on && on.dataset.tab) || 'inventory';
}


/* ── Data fetching ────────────────────────────────────────────────────────── */

/**
 * Fetch Locker (SQLite) and Inventory (Excel) independently so a down share
 * only fails Inventory. Skips overlapping polls and hidden-tab Excel copies.
 *
 * @returns {Promise<void>}
 */
async function fetchTables() {
  if (document.hidden) return;
  if (tablesInFlight) return tablesInFlight;
  tablesInFlight = _fetchTablesWork().finally(() => { tablesInFlight = null; });
  return tablesInFlight;
}


/**
 * Parallel Locker + optional Inventory GETs for the visible tab.
 *
 * @returns {Promise<void>}
 */
async function _fetchTablesWork() {
  const tab = activeTabName();
  const wantInventory = tab === 'inventory';
  const devicesP = fetch('/api/dashboard/devices')
    .then(async (res) => {
      if (res.ok) devicesData = await res.json();
    })
    .catch(() => { /* keep stale locker rows */ });
  const inventoryP = wantInventory
    ? fetch('/api/dashboard/inventory')
      .then(async (res) => {
        if (res.ok) {
          inventoryData = await res.json();
          inventoryError = '';
        } else {
          inventoryData = [];
          let detail = 'Catalog Excel is not available.';
          try {
            const body = await res.json();
            if (body && body.detail) detail = String(body.detail);
          } catch (_) { /* keep default */ }
          inventoryError = detail;
        }
      })
      .catch(() => {
        inventoryError = 'Catalog Excel is not available.';
      })
    : Promise.resolve();
  await Promise.all([devicesP, inventoryP]);
  renderDevices();
  if (wantInventory) renderInventory();
  updateTimestamp();
}


/**
 * Load dropdown names (registered users + registrants + in-locker token).
 * Requires the dashboard admin secret header.
 */
async function fetchOwners() {
  try {
    const res = await fetch('/api/dashboard/owners', {
      headers: dashboardAdminHeaders(),
    });
    if (!res.ok) return;
    const data = await res.json();
    ownerNames = Array.isArray(data.names) ? data.names : [];
    fillOwnerDatalist();
  } catch (_) { /* keep last names */ }
}


/**
 * Rebuild the owner <datalist> from cached names.
 */
function fillOwnerDatalist() {
  const list = document.getElementById('owner-names');
  if (!list) return;
  list.innerHTML = ownerNames.map(n => `<option value="${esc(n)}"></option>`).join('');
}


/**
 * Poll what the Riverdi is showing. View only — never POSTs to the kiosk.
 */
async function fetchDisplay() {
  if (document.hidden) return;
  if (activeTabName() !== 'display') return;
  try {
    const res = await fetch('/api/dashboard/display');
    if (!res.ok) return;
    const data = await res.json();
    const screenEl = document.getElementById('display-screen');
    const userEl = document.getElementById('display-user');
    if (screenEl) screenEl.textContent = data.label || data.screen || '—';
    if (userEl) userEl.textContent = data.occupied ? 'In use' : 'Idle';
  } catch (_) { /* keep last snapshot */ }
}


/**
 * Update the "Last updated" label in the header with the current time.
 */
function updateTimestamp() {
  const el = document.getElementById('last-updated');
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  el.textContent = `Updated ${hh}:${mm}:${ss}`;
}


/* ── Sorting ──────────────────────────────────────────────────────────────── */

/**
 * Sort an array of objects by a given key in the specified direction.
 *
 * @param {Object[]} data  - Array of row objects to sort.
 * @param {string}   key   - Object property name to sort by.
 * @param {string}   dir   - 'asc' or 'desc'.
 * @returns {Object[]} Sorted copy of the input array.
 */
function sortData(data, key, dir) {
  return [...data].sort((a, b) => {
    let va = a[key];
    let vb = b[key];

    if (va == null || va === '') return 1;
    if (vb == null || vb === '') return -1;

    if (typeof va === 'number' && typeof vb === 'number') {
      return dir === 'asc' ? va - vb : vb - va;
    }

    va = String(va).toLowerCase();
    vb = String(vb).toLowerCase();
    const cmp = va.localeCompare(vb);
    return dir === 'asc' ? cmp : -cmp;
  });
}


/**
 * Handle a click on a sortable column header.
 *
 * @param {string} table - Table identifier ('inventory' or 'devices').
 * @param {string} key   - Column key that was clicked.
 */
function handleSort(table, key) {
  const state = sortState[table];
  if (state.key === key) {
    state.dir = state.dir === 'asc' ? 'desc' : 'asc';
  } else {
    state.key = key;
    state.dir = 'asc';
  }

  const tableEl = document.getElementById(`${table}-table`);
  tableEl.querySelectorAll('th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === key) {
      th.classList.add(state.dir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  });

  if (table === 'inventory') renderInventory();
  else renderDevices();
}


/* ── Rendering ────────────────────────────────────────────────────────────── */

/**
 * Render Inventory from cached Excel rows, applying search and sort.
 */
function renderInventory() {
  const errorEl = document.getElementById('inventory-error');
  const tbody = document.getElementById('inventory-tbody');
  const empty = document.getElementById('inventory-empty');
  const count = document.getElementById('inventory-count');

  if (inventoryError) {
    errorEl.textContent = inventoryError;
    errorEl.style.display = '';
    tbody.innerHTML = '';
    empty.style.display = 'none';
    count.textContent = '';
    lastInventoryStamp = '';
    return;
  }
  errorEl.style.display = 'none';

  let data = inventoryData;
  const q = inventoryQuery.trim().toLowerCase();
  if (q) {
    data = data.filter(d =>
      Object.values(d).some(v => v != null && String(v).toLowerCase().includes(q))
    );
  }

  const s = sortState.inventory;
  if (s.key) data = sortData(data, s.key, s.dir);

  count.textContent = `${data.length} / ${inventoryData.length}`;

  const stamp = `${inventoryError}|${q}|${s.key}|${s.dir}|${data.length}|${JSON.stringify(data)}`;
  if (stamp === lastInventoryStamp) return;
  lastInventoryStamp = stamp;

  if (data.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = data.map(d => `
    <tr>
      <td>${esc(d.pm_number)}</td>
      <td>${esc(d.name)}</td>
      <td>${esc(d.manufacturer)}</td>
      <td>${esc(d.model)}</td>
      <td>${esc(d.serial_number)}</td>
      <td>${d.in_locker ? esc(d.location) : ownerCell(d.pm_number, d.location)}</td>
      <td>${esc(d.calibration_due)}</td>
    </tr>
  `).join('');
}


/**
 * Render the Locker table from cached SQLite data.
 */
function renderDevices() {
  let data = devicesData;

  if (activeFilter !== 'all') {
    data = data.filter(d => d.status === activeFilter);
  }

  const s = sortState.devices;
  if (s.key) data = sortData(data, s.key, s.dir);

  const tbody = document.getElementById('devices-tbody');
  const empty = document.getElementById('devices-empty');
  const count = document.getElementById('device-count');

  const avail = devicesData.filter(d => d.status === 'available').length;
  count.textContent = `${avail} available / ${devicesData.length} total`;

  const stamp = `${activeFilter}|${s.key}|${s.dir}|${data.length}|${JSON.stringify(data)}`;
  if (stamp === lastDevicesStamp) return;
  lastDevicesStamp = stamp;

  if (data.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = data.map(d => `
    <tr>
      <td>${d.locker_slot ?? '—'}</td>
      <td>${esc(d.pm_number)}</td>
      <td>${esc(d.name)}</td>
      <td>${esc(d.device_type ?? '')}</td>
      <td><span class="status-badge ${d.status}">${d.status}</span></td>
      <td>${esc(d.borrower_name ?? '')}</td>
      <td>${d.has_tag ? 'Tagged' : 'No tag'}</td>
      <td>${esc(d.calibration_due ?? '')}</td>
    </tr>
  `).join('');
}


/* ── Utility ──────────────────────────────────────────────────────────────── */

/**
 * Escape a string for safe HTML insertion (prevents XSS).
 *
 * @param {string} str - Raw string to escape.
 * @returns {string} HTML-safe string with &, <, >, ", ' escaped.
 */
function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}


/**
 * Clickable owner / location cell for Inventory and Locker.
 *
 * @param {string} pm - Equipment number.
 * @param {string} owner - Current owner text (may be empty).
 * @returns {string} Button HTML.
 */
function ownerCell(pm, owner) {
  const text = owner || '—';
  return `<button type="button" class="owner-btn" data-pm="${esc(pm)}" data-owner="${esc(owner || '')}">${esc(text)}</button>`;
}


/**
 * Open the owner dialog for one PM.
 *
 * @param {string} pm - Equipment number.
 * @param {string} current - Current owner text to prefill.
 */
function openOwnerDialog(pm, current) {
  ownerEditPm = pm;
  const dialog = document.getElementById('owner-dialog');
  const pmEl = document.getElementById('owner-dialog-pm');
  const input = document.getElementById('owner-input');
  const err = document.getElementById('owner-dialog-error');
  if (pmEl) pmEl.textContent = pm;
  if (input) {
    input.value = current || '';
  }
  if (err) {
    err.textContent = '';
    err.style.display = 'none';
  }
  ensureDashboardAdminSecret();
  fetchOwners();
  if (dialog) dialog.hidden = false;
  if (input) input.focus();
}


/**
 * Hide the owner dialog without writing.
 */
function closeOwnerDialog() {
  ownerEditPm = '';
  const dialog = document.getElementById('owner-dialog');
  if (dialog) dialog.hidden = true;
}


/**
 * Confirm the owner change and POST it.
 */
async function confirmOwnerEdit() {
  const pm = ownerEditPm;
  const input = document.getElementById('owner-input');
  const err = document.getElementById('owner-dialog-error');
  const btn = document.getElementById('owner-confirm');
  if (!pm || !input) return;
  const owner = input.value.trim();
  if (btn) btn.disabled = true;
  if (err) {
    err.textContent = '';
    err.style.display = 'none';
  }
  try {
    const res = await fetch('/api/dashboard/owner', {
      method: 'POST',
      headers: dashboardAdminHeaders(),
      body: JSON.stringify({ pm_number: pm, owner }),
    });
    if (!res.ok) {
      let detail = 'Could not change owner.';
      try {
        const body = await res.json();
        if (body && body.detail) detail = String(body.detail);
      } catch (_) { /* keep default */ }
      if (err) {
        err.textContent = detail;
        err.style.display = '';
      }
      return;
    }
    closeOwnerDialog();
    await fetchTables();
  } catch (_) {
    if (err) {
      err.textContent = 'Could not change owner.';
      err.style.display = '';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}


/**
 * Format the header clock as local HH:MM:SS.
 */
function tickClock() {
  const el = document.getElementById('dash-clock');
  if (!el) return;
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  el.textContent = `${hh}:${mm}:${ss}`;
}


/**
 * Record a tap on the header clock. Five taps within 3s opens admin.
 */
function checkAdminTapSequence() {
  const now = Date.now();
  adminTaps.push(now);
  while (adminTaps.length > 0 && (now - adminTaps[0]) > ADMIN_TAP_WINDOW) {
    adminTaps.shift();
  }
  if (adminTaps.length >= ADMIN_TAP_COUNT) {
    adminTaps.length = 0;
    const overlay = document.getElementById('admin-overlay');
    if (overlay && !overlay.hidden) closeAdminOverlay();
    else openAdminOverlay();
  }
}


/**
 * Show the 5-tap overlay and load users, logs, and locker tags.
 */
function openAdminOverlay() {
  const overlay = document.getElementById('admin-overlay');
  if (overlay) overlay.hidden = false;
  const status = document.getElementById('admin-tag-status');
  if (status) status.textContent = '';
  ensureDashboardAdminSecret();
  fetchAdminTables();
}


/**
 * Hide the 5-tap overlay.
 */
function closeAdminOverlay() {
  const overlay = document.getElementById('admin-overlay');
  if (overlay) overlay.hidden = true;
}


/**
 * Load users, transactions, and locker rows for the overlay.
 * Does not re-copy Inventory Excel (the overlay does not show that table).
 */
async function fetchAdminTables() {
  const headers = dashboardAdminHeaders();
  const [usersRes, txRes, devicesRes] = await Promise.all([
    fetch('/api/dashboard/users', { headers }).catch(() => null),
    fetch('/api/dashboard/transactions', { headers }).catch(() => null),
    fetch('/api/dashboard/devices').catch(() => null),
  ]);
  try {
    usersData = usersRes && usersRes.ok ? await usersRes.json() : [];
  } catch (_) {
    usersData = [];
  }
  try {
    txData = txRes && txRes.ok ? await txRes.json() : [];
  } catch (_) {
    txData = [];
  }
  if ((usersRes && usersRes.status === 401) || (txRes && txRes.status === 401)) {
    sessionStorage.removeItem(ADMIN_SECRET_KEY);
    const status = document.getElementById('admin-tag-status');
    if (status) status.textContent =
      'Dashboard admin authorization failed. Check the secret.';
    usersData = [];
    txData = [];
  }
  try {
    if (devicesRes && devicesRes.ok) devicesData = await devicesRes.json();
  } catch (_) { /* keep cached locker rows */ }
  renderAdminOverlay();
}


/**
 * Render users, transactions, and NFC actions in the 5-tap overlay.
 */
function renderAdminOverlay() {
  const usersBody = document.getElementById('admin-users-tbody');
  if (usersBody) {
    usersBody.innerHTML = usersData.map(u => `
      <tr>
        <td>${esc(u.display_name)}</td>
        <td>${esc(u.role)}</td>
        <td>${u.is_active ? 'Yes' : 'No'}</td>
        <td>${esc(u.registered_at)}</td>
      </tr>
    `).join('');
  }

  const txBody = document.getElementById('admin-tx-tbody');
  if (txBody) {
    txBody.innerHTML = txData.map(t => `
      <tr>
        <td>${esc(t.timestamp)}</td>
        <td>${esc(t.user_name)}</td>
        <td>${esc(t.device_name)}</td>
        <td>${esc(t.transaction_type)}</td>
        <td>${esc(t.performed_by)}</td>
        <td>${esc(t.notes)}</td>
      </tr>
    `).join('');
  }

  const tagsBody = document.getElementById('admin-tags-tbody');
  if (!tagsBody) return;
  tagsBody.innerHTML = devicesData.map(d => {
    const tagged = !!d.has_tag;
    const bindLabel = tagged ? 'Replace tag' : 'Bind';
    const unbind = tagged
      ? `<button type="button" class="admin-tag-btn" data-unbind-pm="${esc(d.pm_number)}">Unbind</button>`
      : '';
    return `
      <tr>
        <td>${d.locker_slot ?? '—'}</td>
        <td>${esc(d.pm_number)}</td>
        <td>${esc(d.name)}</td>
        <td>${tagged ? 'Tagged' : 'No tag'}</td>
        <td>
          <button type="button" class="admin-tag-btn admin-tag-bind" data-bind-pm="${esc(d.pm_number)}">${bindLabel}</button>
          ${unbind}
        </td>
      </tr>
    `;
  }).join('');
}


/**
 * Arm a 60s bind window on the Pi; the sticker must be tapped at the kiosk.
 *
 * @param {string} pm - Locker PM number.
 */
async function armBind(pm) {
  const status = document.getElementById('admin-tag-status');
  if (status) status.textContent = '';
  try {
    const res = await fetch('/api/dashboard/bind-tag', {
      method: 'POST',
      headers: dashboardAdminHeaders(),
      body: JSON.stringify({ pm_number: pm }),
    });
    let detail = 'Could not arm bind.';
    try {
      const body = await res.json();
      if (body && body.message) detail = String(body.message);
      else if (body && body.detail) detail = String(body.detail);
    } catch (_) { /* keep default */ }
    if (status) status.textContent = res.ok
      ? `${detail} (${pm})`
      : detail;
  } catch (_) {
    if (status) status.textContent = 'Could not arm bind.';
  }
}


/**
 * Clear the sticker HMAC for one locker PM.
 *
 * @param {string} pm - Locker PM number.
 */
async function unbindTag(pm) {
  const status = document.getElementById('admin-tag-status');
  if (status) status.textContent = '';
  try {
    const res = await fetch('/api/dashboard/unbind-tag', {
      method: 'POST',
      headers: dashboardAdminHeaders(),
      body: JSON.stringify({ pm_number: pm }),
    });
    if (!res.ok) {
      let detail = 'Could not unbind.';
      try {
        const body = await res.json();
        if (body && body.detail) detail = String(body.detail);
      } catch (_) { /* keep default */ }
      if (status) status.textContent = detail;
      return;
    }
    if (status) status.textContent = `Unbound ${pm}.`;
    await fetchAdminTables();
    renderDevices();
  } catch (_) {
    if (status) status.textContent = 'Could not unbind.';
  }
}


/* ── Event wiring ─────────────────────────────────────────────────────────── */

/**
 * Wire tabs, sort headers, Locker filters, Inventory search, and Inventory owner edit.
 */
function initEvents() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => showTab(btn.dataset.tab));
  });

  document.querySelectorAll('#inventory-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => handleSort('inventory', th.dataset.sort));
  });
  document.querySelectorAll('#devices-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => handleSort('devices', th.dataset.sort));
  });

  document.querySelectorAll('#status-filters .filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#status-filters .filter-btn').forEach(b =>
        b.classList.remove('active')
      );
      btn.classList.add('active');
      activeFilter = btn.dataset.filter;
      renderDevices();
    });
  });

  const search = document.getElementById('inventory-search');
  if (search) {
    search.addEventListener('input', () => {
      inventoryQuery = search.value;
      clearTimeout(inventorySearchTimer);
      inventorySearchTimer = setTimeout(renderInventory, SEARCH_DEBOUNCE_MS);
    });
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.owner-btn');
    if (btn) {
      openOwnerDialog(btn.dataset.pm || '', btn.dataset.owner || '');
    }
    const bindBtn = e.target.closest('[data-bind-pm]');
    if (bindBtn) armBind(bindBtn.dataset.bindPm || '');
    const unbindBtn = e.target.closest('[data-unbind-pm]');
    if (unbindBtn) unbindTag(unbindBtn.dataset.unbindPm || '');
  });

  const cancel = document.getElementById('owner-cancel');
  if (cancel) cancel.addEventListener('click', closeOwnerDialog);

  const confirmBtn = document.getElementById('owner-confirm');
  if (confirmBtn) confirmBtn.addEventListener('click', () => confirmOwnerEdit());

  const dialog = document.getElementById('owner-dialog');
  if (dialog) {
    dialog.addEventListener('click', (e) => {
      if (e.target === dialog) closeOwnerDialog();
    });
  }

  const clock = document.getElementById('dash-clock');
  if (clock) {
    clock.addEventListener('click', (e) => {
      e.stopPropagation();
      checkAdminTapSequence();
    });
  }

  const adminClose = document.getElementById('admin-overlay-close');
  if (adminClose) adminClose.addEventListener('click', closeAdminOverlay);

  const adminOverlay = document.getElementById('admin-overlay');
  if (adminOverlay) {
    adminOverlay.addEventListener('click', (e) => {
      if (e.target === adminOverlay) closeAdminOverlay();
    });
  }
}


/* ── Initialisation ───────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initEvents();
  loadSiteConfig();
  fetchTables();
  fetchDisplay();
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(fetchTables, REFRESH_MS);
  setInterval(fetchDisplay, DISPLAY_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      fetchTables();
      fetchDisplay();
    }
  });
});
