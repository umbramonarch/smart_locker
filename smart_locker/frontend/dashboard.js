/**
 * @fileoverview Public dashboard: Inventory (SQLite catalog), Locker
 *               (cabinet units), and Display (kiosk snapshot). Sort, search,
 *               status filter, and polling. Owner change on a non-cabinet
 *               row is public. The Admin button opens the catalog editor,
 *               sheet-change review, users, logs, and NFC unbind / arm-bind
 *               behind the admin secret. No login. No remote control of the
 *               kiosk.
 * @project smart_locker/frontend
 * @description Tabs switch locally. The workbook is a hidden Pi-written
 *              mirror — a banner reports pending writes and hand edits.
 *              Asset-label text comes from /api/config. Admin mutations
 *              send X-Smart-Locker-Admin from a prompted secret.
 */

/* ── State ────────────────────────────────────────────────────────────────── */

/** Cached Inventory rows from the SQLite catalog. */
let inventoryData = [];
/** Cached Locker rows from SQLite. */
let devicesData = [];
/** Error text when the Inventory fetch last failed. */
let inventoryError = '';
/** Last mirror status payload (configured, state, pending_writes…). */
let mirrorStatus = null;

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

/** Cached registered users for the admin overlay. */
let usersData = [];
/** Cached transaction rows for the admin overlay. */
let txData = [];
/** Cached sheet-diff rows for the admin overlay. */
let sheetDiffs = [];

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


/** Promise resolve/reject callbacks for the active admin-secret modal. */
let adminSecretResolve = null;


/** Message shown when the secret cannot be checked against the API. */
const ADMIN_SECRET_UNAVAILABLE_MSG =
  'Could not verify the admin secret right now. Try again.';


/**
 * Show the styled admin-secret modal.
 *
 * @param {string} [message] - Optional error text to display on open.
 */
function openAdminSecretDialog(message) {
  const dialog = document.getElementById('admin-secret-dialog');
  const input = document.getElementById('admin-secret-input');
  const err = document.getElementById('admin-secret-error');
  if (err) {
    err.textContent = message || '';
    err.style.display = message ? '' : 'none';
  }
  if (input) input.value = '';
  if (dialog) dialog.hidden = false;
  if (input) input.focus();
}


/**
 * Hide the admin-secret modal.
 */
function closeAdminSecretDialog() {
  const dialog = document.getElementById('admin-secret-dialog');
  if (dialog) dialog.hidden = true;
}


/**
 * Resolve the pending admin-secret promise with the given value.
 * @param {string} value - Secret string, or empty if cancelled.
 */
function resolveAdminSecret(value) {
  if (adminSecretResolve) {
    adminSecretResolve(value);
    adminSecretResolve = null;
  }
  closeAdminSecretDialog();
}


/**
 * Check a secret against the admin API.
 *
 * @param {string} secret - Secret to send in the admin header.
 * @returns {Promise<string>} 'ok', 'invalid' on 401, or 'unavailable'.
 */
async function validateAdminSecret(secret) {
  const headers = { 'Content-Type': 'application/json' };
  headers[ADMIN_HEADER] = secret;
  let res = null;
  try {
    res = await fetch('/api/dashboard/users', { headers });
  } catch (_) {
    return 'unavailable';
  }
  if (!res) return 'unavailable';
  if (res.ok) return 'ok';
  if (res.status === 401 || res.status === 403) return 'invalid';
  return 'unavailable';
}


/**
 * Validate the typed admin secret before entering admin mode.
 * On success store it and resolve; otherwise show an error and stay open.
 */
async function submitAdminSecret() {
  const input = document.getElementById('admin-secret-input');
  const err = document.getElementById('admin-secret-error');
  const secret = input ? input.value.trim() : '';
  if (!secret) {
    if (err) {
      err.textContent = 'Enter the admin secret.';
      err.style.display = '';
    }
    if (input) input.focus();
    return;
  }

  const result = await validateAdminSecret(secret);
  if (result === 'ok') {
    sessionStorage.setItem(ADMIN_SECRET_KEY, secret);
    resolveAdminSecret(secret);
    return;
  }
  if (err) {
    err.textContent = result === 'invalid'
      ? 'Incorrect admin secret.'
      : ADMIN_SECRET_UNAVAILABLE_MSG;
    err.style.display = '';
  }
  if (input) input.focus();
}


/**
 * Prompt for the dashboard admin secret using the styled modal.
 * A stored secret is re-validated, and cleared if the API rejects it.
 *
 * @returns {Promise<string>} Secret string, or empty string if cancelled.
 */
async function ensureDashboardAdminSecret() {
  const stored = sessionStorage.getItem(ADMIN_SECRET_KEY) || '';
  let message = '';
  if (stored) {
    const result = await validateAdminSecret(stored);
    if (result === 'ok') return stored;
    if (result === 'invalid') sessionStorage.removeItem(ADMIN_SECRET_KEY);
    else message = ADMIN_SECRET_UNAVAILABLE_MSG;
  }
  openAdminSecretDialog(message);
  return new Promise((resolve) => {
    adminSecretResolve = resolve;
  });
}


/* ── First-boot Setup (no admin enrolled yet) ─────────────────────────────── */

/** Interval handle for polling /api/setup while the kiosk ceremony runs. */
let setupPollTimer = null;

/**
 * Whether first-boot Setup is open on the locker (no active admin).
 * Fails closed: errors are treated as "setup not needed".
 *
 * @returns {Promise<boolean>}
 */
async function dashboardSetupNeeded() {
  try {
    const res = await fetch('/api/setup');
    if (!res.ok) return false;
    const data = await res.json();
    return !!data.needed;
  } catch (_) {
    return false;
  }
}

/**
 * Show the Setup dialog. Arming the first-admin card window is kiosk-only
 * (POST /api/setup is loopback — a LAN caller must not plant the dashboard
 * password or squat the enrollment window), so the dialog guides the
 * operator to the locker touchscreen and polls until the first admin
 * exists. When Setup closes, the admin-secret dialog collects the password
 * the operator chose at the kiosk.
 */
function openSetupDialog() {
  const dialog = document.getElementById('setup-dialog');
  const err = document.getElementById('setup-dialog-error');
  const waiting = document.getElementById('setup-dialog-waiting');
  if (err) err.style.display = 'none';
  if (waiting) waiting.style.display = '';
  if (dialog) dialog.hidden = false;
  pollSetupUntilEnrolled(0);
}


/**
 * Hide the Setup dialog and stop the enrollment poll. An armed card window
 * on the locker expires by itself after 60s — the dashboard cannot cancel
 * it remotely (cancel is loopback-only on purpose).
 */
function closeSetupDialog() {
  const dialog = document.getElementById('setup-dialog');
  if (dialog) dialog.hidden = true;
  if (setupPollTimer) {
    clearInterval(setupPollTimer);
    setupPollTimer = null;
  }
}


/**
 * Re-check whether the kiosk-side Setup has completed (the admin card has
 * been tapped). Bound to the dialog's "Check now" button — the dashboard
 * cannot arm Setup itself (loopback-only), so this is a manual refresh.
 *
 * @returns {Promise<void>}
 */
async function submitSetupDialog() {
  const err = document.getElementById('setup-dialog-error');
  const btn = document.getElementById('setup-dialog-confirm');
  if (btn) btn.disabled = true;
  try {
    const needed = await dashboardSetupNeeded();
    if (!needed) {
      closeSetupDialog();
      openAdminOverlay();
      return;
    }
    if (err) {
      err.textContent = 'Not enrolled yet — finish setup on the locker touchscreen.';
      err.style.display = '';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}


/**
 * Poll /api/setup until the kiosk-side Setup ceremony completes (an admin
 * card was tapped). ``deadline`` of 0 means no timeout — the operator can
 * take as long as they need at the locker, and the poll stops when the
 * dialog closes.
 *
 * @param {number} deadline - Epoch ms when the poll gives up; 0 = never.
 */
function pollSetupUntilEnrolled(deadline) {
  const waiting = document.getElementById('setup-dialog-waiting');
  const err = document.getElementById('setup-dialog-error');
  if (setupPollTimer) clearInterval(setupPollTimer);
  setupPollTimer = setInterval(async () => {
    const dialog = document.getElementById('setup-dialog');
    if (!dialog || dialog.hidden || (deadline && Date.now() > deadline)) {
      clearInterval(setupPollTimer);
      setupPollTimer = null;
      if (dialog && !dialog.hidden && deadline) {
        if (waiting) waiting.style.display = 'none';
        if (err) {
          err.textContent = 'Card tap timed out. Start setup again.';
          err.style.display = '';
        }
      }
      return;
    }
    try {
      const res = await fetch('/api/setup');
      if (!res.ok) return;
      const data = await res.json();
      if (!data.needed) {
        clearInterval(setupPollTimer);
        setupPollTimer = null;
        closeSetupDialog();
        openAdminOverlay();
      }
    } catch (_) { /* keep polling */ }
  }, 2000);
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
 * Fetch Locker, Inventory, and mirror status independently. Skips
 * overlapping polls and hidden tabs.
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
 * Parallel Locker + optional Inventory + mirror-status GETs. An open
 * admin overlay counts as wanting Inventory — its catalog buttons gate
 * on those rows — and re-renders after the poll so they never go stale.
 *
 * @returns {Promise<void>}
 */
async function _fetchTablesWork() {
  const tab = activeTabName();
  const overlay = document.getElementById('admin-overlay');
  const adminOpen = !!(overlay && !overlay.hidden);
  const wantInventory = tab === 'inventory' || adminOpen;
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
          let detail = 'Catalog is not available.';
          try {
            const body = await res.json();
            if (body && body.detail) detail = String(body.detail);
          } catch (_) { /* keep default */ }
          inventoryError = detail;
        }
      })
      .catch(() => {
        inventoryError = 'Catalog is not available.';
      })
    : Promise.resolve();
  const mirrorP = fetch('/api/dashboard/mirror')
    .then(async (res) => {
      if (res.ok) mirrorStatus = await res.json();
    })
    .catch(() => { /* keep last mirror status */ });
  await Promise.all([devicesP, inventoryP, mirrorP]);
  renderMirrorBanner();
  renderDevices();
  if (wantInventory) renderInventory();
  if (adminOpen) renderAdminOverlay();
  updateTimestamp();
}


/**
 * Show the mirror warning line: pending write, unavailable file, or hand
 * edits waiting for an admin decision. Hidden when everything is fine.
 */
function renderMirrorBanner() {
  const banner = document.getElementById('mirror-banner');
  const text = document.getElementById('mirror-banner-text');
  if (!banner || !text) return;
  let message = '';
  if (mirrorStatus && mirrorStatus.configured !== false) {
    if (mirrorStatus.external_changes) {
      message = 'The spreadsheet was edited by hand. Review the changes in Admin.';
    } else if (mirrorStatus.pending_writes) {
      message = 'The spreadsheet will catch up — it cannot be written right now.';
    } else if (mirrorStatus.state === 'error') {
      message = `The spreadsheet could not be written (${mirrorStatus.last_error || 'error'}). It keeps retrying.`;
    }
  }
  text.textContent = message;
  banner.hidden = !message;
}


/**
 * Load dropdown names (registered users + registrants + in-locker token).
 * Public — owner edit on a non-cabinet device has no password.
 */
async function fetchOwners() {
  try {
    const res = await fetch('/api/dashboard/owners');
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
 * Render Inventory from cached catalog rows, applying search and sort.
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
      <td>${calCell(d)}</td>
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
      <td>${calCell(d)}</td>
    </tr>
  `).join('');
}


/**
 * Calibration cell: the ISO date, wrapped in a badge while the state is
 * due_soon/due/overdue ('ok' and dateless rows stay plain).
 *
 * @param {Object} d - Inventory or Locker row (calibration_* fields).
 * @returns {string} HTML for the cell.
 */
function calCell(d) {
  const state = d.calibration_state;
  const date = esc(d.calibration_due ?? '');
  if (!date || !state || state === 'ok') return date;
  return `<span class="cal-badge ${esc(state)}">${date}</span>`;
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
 * Error detail from an API body — a string, or pydantic's list of issues.
 *
 * @param {Object} body - Parsed response JSON (may be null/non-object).
 * @param {string} fallback - Text to use when the body has no detail.
 * @returns {string} Human-readable error text.
 */
function detailText(body, fallback) {
  if (body && Array.isArray(body.detail)) {
    const msg = body.detail.map(e => e && e.msg ? String(e.msg) : String(e)).join(' ');
    return msg || fallback;
  }
  if (body && body.detail) return String(body.detail);
  return fallback;
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
      headers: { 'Content-Type': 'application/json' },
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
 * Admin button: open the admin overlay, or first-boot Setup while no admin
 * is enrolled (there may be no secret to check yet).
 */
async function openAdmin() {
  const overlay = document.getElementById('admin-overlay');
  if (overlay && !overlay.hidden) {
    closeAdminOverlay();
    return;
  }
  if (await dashboardSetupNeeded()) openSetupDialog();
  else await openAdminOverlay();
}


/**
 * Show the admin overlay and load users, logs, catalog, and locker tags.
 * The admin secret is validated first; the overlay only opens on success.
 */
async function openAdminOverlay() {
  const secret = await ensureDashboardAdminSecret();
  if (!secret) return;
  const overlay = document.getElementById('admin-overlay');
  if (overlay) overlay.hidden = false;
  const status = document.getElementById('admin-tag-status');
  if (status) status.textContent = '';
  fetchAdminTables();
}


/**
 * Hide the admin overlay.
 */
function closeAdminOverlay() {
  const overlay = document.getElementById('admin-overlay');
  if (overlay) overlay.hidden = true;
}


/**
 * Load users, transactions, locker rows, the full catalog, and any pending
 * sheet edits for the overlay.
 */
async function fetchAdminTables() {
  const headers = dashboardAdminHeaders();
  const wantDiffs = !!(mirrorStatus && mirrorStatus.external_changes);
  const [usersRes, txRes, devicesRes, inventoryRes, diffsRes] = await Promise.all([
    fetch('/api/dashboard/users', { headers }).catch(() => null),
    fetch('/api/dashboard/transactions', { headers }).catch(() => null),
    fetch('/api/dashboard/devices').catch(() => null),
    fetch('/api/dashboard/inventory').catch(() => null),
    wantDiffs
      ? fetch('/api/dashboard/mirror/diffs', { headers }).catch(() => null)
      : Promise.resolve(null),
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
  try {
    if (inventoryRes && inventoryRes.ok) inventoryData = await inventoryRes.json();
  } catch (_) { /* keep cached catalog rows */ }
  sheetDiffs = [];
  if (diffsRes && diffsRes.ok) {
    try {
      const body = await diffsRes.json();
      sheetDiffs = Array.isArray(body.diffs) ? body.diffs : [];
    } catch (_) { /* no diffs */ }
  }
  renderAdminOverlay();
}


/**
 * Render users, transactions, catalog, sheet diffs, and NFC actions in the
 * admin overlay.
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

  const sheetSection = document.getElementById('admin-sheet-section');
  if (sheetSection) {
    sheetSection.hidden = sheetDiffs.length === 0;
    const diffBody = document.getElementById('sheet-diff-tbody');
    if (diffBody) {
      diffBody.innerHTML = sheetDiffs.map(d => {
        const sheetVal = Array.isArray(d.sheet) ? d.sheet.join(' · ') : (d.sheet ?? '');
        const dbVal = Array.isArray(d.database) ? d.database.join(' · ') : (d.database ?? '');
        return `
          <tr>
            <td>${esc(d.pm_number)}</td>
            <td>${esc(d.kind)}</td>
            <td>${esc(d.field || '')}</td>
            <td>${esc(sheetVal)}</td>
            <td>${esc(dbVal)}</td>
          </tr>
        `;
      }).join('');
    }
  }

  const catalogBody = document.getElementById('admin-catalog-tbody');
  if (catalogBody) {
    catalogBody.innerHTML = inventoryData.map(d => {
      // Maintenance lifecycle applies to cabinet units only — a borrowed
      // unit is refused by the API, so the row offers no dead button.
      let serviceBtn = '';
      if (d.in_locker && d.status === 'available') {
        serviceBtn = `<button type="button" class="admin-tag-btn" data-maint-pm="${esc(d.pm_number)}">To maintenance</button>`;
      } else if (d.in_locker && d.status === 'maintenance') {
        serviceBtn = `<button type="button" class="admin-tag-btn admin-tag-bind" data-service-pm="${esc(d.pm_number)}">Back in service</button>`;
      }
      return `
      <tr>
        <td>${esc(d.pm_number)}</td>
        <td>${esc(d.name)}</td>
        <td>${esc(d.device_type ?? '')}</td>
        <td>${esc(d.serial_number ?? '')}</td>
        <td>${esc(d.location ?? '')}</td>
        <td>${d.in_locker ? 'Yes' : 'No'}</td>
        <td>
          <button type="button" class="admin-tag-btn" data-edit-pm="${esc(d.pm_number)}">Edit</button>
          ${serviceBtn}
          <button type="button" class="admin-tag-btn" data-remove-pm="${esc(d.pm_number)}">Remove</button>
        </td>
      </tr>
    `;
    }).join('');
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


/** PM of the catalog row open in the device dialog ('' = add mode). */
let deviceDialogPm = '';


/**
 * Open the device editor. ``pm`` empty → add mode; otherwise prefill from
 * the cached catalog row and lock the id field.
 *
 * @param {string} pm - Catalog PM to edit, or '' to add.
 */
function openDeviceDialog(pm) {
  deviceDialogPm = pm || '';
  const row = pm ? inventoryData.find(d => d.pm_number === pm) : null;
  const title = document.getElementById('device-dialog-title');
  const err = document.getElementById('device-dialog-error');
  const pmInput = document.getElementById('device-pm');
  const locLabel = document.getElementById('device-location-label');
  const locInput = document.getElementById('device-location');
  const fields = {
    'device-pm': row ? row.pm_number : '',
    'device-name': row ? (row.name || '') : '',
    'device-type': row ? (row.device_type || '') : '',
    'device-serial': row ? (row.serial_number || '') : '',
    'device-manufacturer': row ? (row.manufacturer || '') : '',
    'device-model': row ? (row.model || '') : '',
    'device-calibration': row ? (row.calibration_due || '') : '',
    'device-location': row ? (row.location || '') : '',
  };
  Object.entries(fields).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.value = value;
  });
  if (title) title.textContent = row ? `Edit ${row.pm_number}` : 'Add device';
  if (pmInput) pmInput.disabled = !!row;
  // A cabinet unit's place is owned by borrow/return — hide the field.
  const inLocker = !!(row && row.in_locker);
  if (locInput) locInput.disabled = inLocker;
  if (locLabel) locLabel.style.display = inLocker ? 'none' : '';
  if (locInput) locInput.style.display = inLocker ? 'none' : '';
  if (err) {
    err.textContent = '';
    err.style.display = 'none';
  }
  const dialog = document.getElementById('device-dialog');
  if (dialog) dialog.hidden = false;
  const first = row ? document.getElementById('device-name') : pmInput;
  if (first) first.focus();
}


/**
 * Hide the device editor without saving.
 */
function closeDeviceDialog() {
  deviceDialogPm = '';
  const dialog = document.getElementById('device-dialog');
  if (dialog) dialog.hidden = true;
}


/**
 * Save the device dialog: POST for add, PATCH for edit.
 */
async function submitDeviceDialog() {
  const err = document.getElementById('device-dialog-error');
  const btn = document.getElementById('device-confirm');
  const value = (id) => {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  };
  const fields = {
    name: value('device-name'),
    device_type: value('device-type'),
    serial_number: value('device-serial'),
    manufacturer: value('device-manufacturer'),
    model: value('device-model'),
    calibration_due: value('device-calibration'),
    location: value('device-location'),
  };
  // A cabinet unit's place is owned by borrow/return — editing it must not
  // send the hidden (prefilled) location field or the API 409s the PATCH.
  if (deviceDialogPm) {
    const row = inventoryData.find(d => d.pm_number === deviceDialogPm);
    if (row && row.in_locker) delete fields.location;
  }
  if (btn) btn.disabled = true;
  if (err) {
    err.textContent = '';
    err.style.display = 'none';
  }
  try {
    let res;
    if (deviceDialogPm) {
      res = await fetch(`/api/dashboard/devices/${encodeURIComponent(deviceDialogPm)}`, {
        method: 'PATCH',
        headers: dashboardAdminHeaders(),
        body: JSON.stringify(fields),
      });
    } else {
      res = await fetch('/api/dashboard/devices', {
        method: 'POST',
        headers: dashboardAdminHeaders(),
        body: JSON.stringify({ pm_number: value('device-pm'), ...fields }),
      });
    }
    if (!res.ok) {
      let detail = 'Could not save the device.';
      if (res.status === 401) {
        sessionStorage.removeItem(ADMIN_SECRET_KEY);
        detail = 'Admin authorization failed. Open Admin again and re-enter the secret.';
      } else {
        try {
          const body = await res.json();
          if (body && body.detail) detail = String(body.detail);
        } catch (_) { /* keep default */ }
      }
      if (err) {
        err.textContent = detail;
        err.style.display = '';
      }
      return;
    }
    closeDeviceDialog();
    await fetchTables();
    fetchAdminTables();
  } catch (_) {
    if (err) {
      err.textContent = 'Could not save the device.';
      err.style.display = '';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}


/**
 * Remove a catalog device after confirming in-place on the row's button.
 * A borrowed unit is refused by the API with 409.
 *
 * @param {string} pm - Catalog PM to remove.
 * @param {HTMLElement} btn - The Remove button clicked.
 */
async function removeCatalogDevice(pm, btn) {
  const status = document.getElementById('admin-catalog-status');
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    const orig = btn.textContent;
    btn.textContent = 'Sure?';
    setTimeout(() => { btn.dataset.armed = ''; btn.textContent = orig; }, 3000);
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch(`/api/dashboard/devices/${encodeURIComponent(pm)}`, {
      method: 'DELETE',
      headers: dashboardAdminHeaders(),
    });
    if (!res.ok) {
      let detail = 'Could not remove the device.';
      if (res.status === 401) {
        sessionStorage.removeItem(ADMIN_SECRET_KEY);
        detail = 'Admin authorization failed. Open Admin again and re-enter the secret.';
      } else {
        try {
          const body = await res.json();
          if (body && body.detail) detail = String(body.detail);
        } catch (_) { /* keep default */ }
      }
      if (status) status.textContent = detail;
      return;
    }
    if (status) status.textContent = `Removed ${pm}.`;
    await fetchTables();
    fetchAdminTables();
  } catch (_) {
    if (status) status.textContent = 'Could not remove the device.';
  } finally {
    btn.disabled = false;
  }
}


/**
 * Take one cabinet unit out of service ("To maintenance" row action).
 * Confirms in-place like Remove — returning needs a new calibration
 * date, so this is not a toggle: first click arms 'Sure?', the second
 * POSTs.
 *
 * @param {string} pm - Locker PM number.
 * @param {HTMLElement} btn - The button clicked (disabled while in flight).
 */
async function toMaintenance(pm, btn) {
  const status = document.getElementById('admin-catalog-status');
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    const orig = btn.textContent;
    btn.textContent = 'Sure?';
    setTimeout(() => { btn.dataset.armed = ''; btn.textContent = orig; }, 3000);
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch(
      `/api/dashboard/devices/${encodeURIComponent(pm)}/maintenance`,
      { method: 'POST', headers: dashboardAdminHeaders() }
    );
    if (!res.ok) {
      let detail = 'Could not mark maintenance.';
      if (res.status === 401) {
        sessionStorage.removeItem(ADMIN_SECRET_KEY);
        detail = 'Admin authorization failed. Open Admin again and re-enter the secret.';
      } else {
        try {
          const body = await res.json();
          detail = detailText(body, detail);
        } catch (_) { /* keep default */ }
      }
      if (status) status.textContent = detail;
      return;
    }
    if (status) status.textContent = `${pm} is in maintenance.`;
    await fetchTables();
    fetchAdminTables();
  } catch (_) {
    if (status) status.textContent = 'Could not mark maintenance.';
  } finally {
    btn.disabled = false;
  }
}


/** PM of the cabinet unit open in the back-in-service dialog. */
let serviceDialogPm = '';


/**
 * Open the back-in-service dialog: the new calibration date is required
 * and never prefilled — the stale date may be why the unit went down, so
 * the admin must pick a new one rather than confirm it unchanged.
 *
 * @param {string} pm - Locker PM number of a unit in maintenance.
 */
function openServiceDialog(pm) {
  serviceDialogPm = pm || '';
  const row = inventoryData.find(d => d.pm_number === pm);
  const pmEl = document.getElementById('service-dialog-pm');
  const input = document.getElementById('service-calibration');
  const err = document.getElementById('service-dialog-error');
  if (pmEl) pmEl.textContent = row ? `${row.pm_number} — ${row.name}` : pm;
  if (input) input.value = '';
  if (err) {
    err.textContent = '';
    err.style.display = 'none';
  }
  const dialog = document.getElementById('service-dialog');
  if (dialog) dialog.hidden = false;
  if (input) input.focus();
}


/**
 * Hide the back-in-service dialog without saving.
 */
function closeServiceDialog() {
  serviceDialogPm = '';
  const dialog = document.getElementById('service-dialog');
  if (dialog) dialog.hidden = true;
}


/**
 * Confirm back-in-service: POST the new calibration date.
 */
async function submitServiceDialog() {
  const pm = serviceDialogPm;
  const input = document.getElementById('service-calibration');
  const err = document.getElementById('service-dialog-error');
  const btn = document.getElementById('service-confirm');
  const status = document.getElementById('admin-catalog-status');
  if (!pm || !input) return;
  // Back in service without a new calibration date is refused — say so
  // here instead of surfacing the API's 422 payload.
  if (!input.value.trim()) {
    if (err) {
      err.textContent = 'Pick the new calibration date.';
      err.style.display = '';
    }
    input.focus();
    return;
  }
  if (btn) btn.disabled = true;
  if (err) {
    err.textContent = '';
    err.style.display = 'none';
  }
  try {
    const res = await fetch(
      `/api/dashboard/devices/${encodeURIComponent(pm)}/back-in-service`,
      {
        method: 'POST',
        headers: dashboardAdminHeaders(),
        body: JSON.stringify({ calibration_due: input.value.trim() }),
      }
    );
    if (!res.ok) {
      let detail = 'Could not return the unit to service.';
      if (res.status === 401) {
        sessionStorage.removeItem(ADMIN_SECRET_KEY);
        detail = 'Admin authorization failed. Open Admin again and re-enter the secret.';
      } else {
        try {
          const body = await res.json();
          detail = detailText(body, detail);
        } catch (_) { /* keep default */ }
      }
      if (err) {
        err.textContent = detail;
        err.style.display = '';
      }
      return;
    }
    // {ok, changed, device}: a still-due/overdue calibration_state means
    // the borrow gate stays closed — say so instead of claiming full service.
    let body = null;
    try {
      body = await res.json();
    } catch (_) { /* no JSON — report plainly */ }
    closeServiceDialog();
    if (status) {
      const state = body && body.device ? body.device.calibration_state : null;
      status.textContent = (state === 'due' || state === 'overdue')
        ? `${pm} is back in service — still unborrowable (calibration due ${body.device.calibration_due}).`
        : `${pm} is back in service.`;
    }
    await fetchTables();
    fetchAdminTables();
  } catch (_) {
    if (err) {
      err.textContent = 'Could not return the unit to service.';
      err.style.display = '';
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}


/**
 * Apply the sheet's hand edits to the database.
 */
async function applySheetEdits() {
  const status = document.getElementById('sheet-diff-status');
  const btn = document.getElementById('sheet-apply');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/dashboard/mirror/apply', {
      method: 'POST',
      headers: dashboardAdminHeaders(),
    });
    if (!res.ok) {
      let detail = 'Could not apply the sheet edits.';
      if (res.status === 401) {
        sessionStorage.removeItem(ADMIN_SECRET_KEY);
        detail = 'Admin authorization failed. Open Admin again and re-enter the secret.';
      } else {
        try {
          const body = await res.json();
          if (body && body.detail) detail = String(body.detail);
        } catch (_) { /* keep default */ }
      }
      if (status) status.textContent = detail;
      return;
    }
    const body = await res.json();
    if (status) status.textContent =
      `Applied ${body.applied} change(s)${body.skipped ? `, skipped ${body.skipped}` : ''}.`;
    sheetDiffs = [];
    await fetchTables();
    fetchAdminTables();
  } catch (_) {
    if (status) status.textContent = 'Could not apply the sheet edits.';
  } finally {
    if (btn) btn.disabled = false;
  }
}


/**
 * Keep the database and overwrite the hand edits on the next mirror write.
 */
async function dismissSheetEdits() {
  const status = document.getElementById('sheet-diff-status');
  const btn = document.getElementById('sheet-keep');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/dashboard/mirror/dismiss', {
      method: 'POST',
      headers: dashboardAdminHeaders(),
    });
    if (!res.ok) {
      let detail = 'Could not dismiss the sheet edits.';
      if (res.status === 401) {
        sessionStorage.removeItem(ADMIN_SECRET_KEY);
        detail = 'Admin authorization failed. Open Admin again and re-enter the secret.';
      }
      if (status) status.textContent = detail;
      return;
    }
    if (status) status.textContent = 'Kept the database — the sheet will be overwritten.';
    sheetDiffs = [];
    await fetchTables();
    fetchAdminTables();
  } catch (_) {
    if (status) status.textContent = 'Could not dismiss the sheet edits.';
  } finally {
    if (btn) btn.disabled = false;
  }
}


/**
 * Arm a 60s bind window on the Pi; the sticker must be tapped at the kiosk.
 *
 * @param {string} pm - Locker PM number.
 * @param {HTMLElement} [btn] - The Bind button clicked (disabled while in flight).
 */
async function armBind(pm, btn) {
  const status = document.getElementById('admin-tag-status');
  if (status) status.textContent = '';
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/dashboard/bind-tag', {
      method: 'POST',
      headers: dashboardAdminHeaders(),
      body: JSON.stringify({ pm_number: pm }),
    });
    if (res.status === 401) {
      sessionStorage.removeItem(ADMIN_SECRET_KEY);
      if (status) status.textContent = 'Admin authorization failed. Open Admin to enter the secret.';
      return;
    }
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
  } finally {
    if (btn) btn.disabled = false;
  }
}


/**
 * Clear the sticker HMAC for one locker PM.
 *
 * @param {string} pm - Locker PM number.
 * @param {HTMLElement} [btn] - The Unbind button clicked (disabled while in flight).
 */
async function unbindTag(pm, btn) {
  const status = document.getElementById('admin-tag-status');
  if (status) status.textContent = '';
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/dashboard/unbind-tag', {
      method: 'POST',
      headers: dashboardAdminHeaders(),
      body: JSON.stringify({ pm_number: pm }),
    });
    if (!res.ok) {
      let detail = 'Could not unbind.';
      if (res.status === 401) {
        sessionStorage.removeItem(ADMIN_SECRET_KEY);
        detail = 'Admin authorization failed. Open Admin to enter the secret.';
      } else {
        try {
          const body = await res.json();
          if (body && body.detail) detail = String(body.detail);
        } catch (_) { /* keep default */ }
      }
      if (status) status.textContent = detail;
      return;
    }
    if (status) status.textContent = `Unbound ${pm}.`;
    await fetchAdminTables();
    renderDevices();
  } catch (_) {
    if (status) status.textContent = 'Could not unbind.';
  } finally {
    if (btn) btn.disabled = false;
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
    if (bindBtn) armBind(bindBtn.dataset.bindPm || '', bindBtn);
    const unbindBtn = e.target.closest('[data-unbind-pm]');
    if (unbindBtn) unbindTag(unbindBtn.dataset.unbindPm || '', unbindBtn);
    const editBtn = e.target.closest('[data-edit-pm]');
    if (editBtn) openDeviceDialog(editBtn.dataset.editPm || '');
    const maintBtn = e.target.closest('[data-maint-pm]');
    if (maintBtn) toMaintenance(maintBtn.dataset.maintPm || '', maintBtn);
    const serviceBtn = e.target.closest('[data-service-pm]');
    if (serviceBtn) openServiceDialog(serviceBtn.dataset.servicePm || '');
    const removeBtn = e.target.closest('[data-remove-pm]');
    if (removeBtn) removeCatalogDevice(removeBtn.dataset.removePm || '', removeBtn);
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

  const adminOpen = document.getElementById('admin-open');
  if (adminOpen) adminOpen.addEventListener('click', () => { openAdmin(); });

  const catalogAdd = document.getElementById('catalog-add-open');
  if (catalogAdd) catalogAdd.addEventListener('click', () => openDeviceDialog(''));

  const deviceCancel = document.getElementById('device-cancel');
  if (deviceCancel) deviceCancel.addEventListener('click', closeDeviceDialog);

  const deviceConfirm = document.getElementById('device-confirm');
  if (deviceConfirm) deviceConfirm.addEventListener('click', () => submitDeviceDialog());

  const deviceDialog = document.getElementById('device-dialog');
  if (deviceDialog) {
    deviceDialog.addEventListener('click', (e) => {
      if (e.target === deviceDialog) closeDeviceDialog();
    });
  }

  const serviceCancel = document.getElementById('service-cancel');
  if (serviceCancel) serviceCancel.addEventListener('click', closeServiceDialog);

  const serviceConfirm = document.getElementById('service-confirm');
  if (serviceConfirm) serviceConfirm.addEventListener('click', () => submitServiceDialog());

  const serviceDialog = document.getElementById('service-dialog');
  if (serviceDialog) {
    serviceDialog.addEventListener('click', (e) => {
      if (e.target === serviceDialog) closeServiceDialog();
    });
  }

  const sheetApply = document.getElementById('sheet-apply');
  if (sheetApply) sheetApply.addEventListener('click', () => applySheetEdits());

  const sheetKeep = document.getElementById('sheet-keep');
  if (sheetKeep) sheetKeep.addEventListener('click', () => dismissSheetEdits());

  const adminClose = document.getElementById('admin-overlay-close');
  if (adminClose) adminClose.addEventListener('click', closeAdminOverlay);

  const adminOverlay = document.getElementById('admin-overlay');
  if (adminOverlay) {
    adminOverlay.addEventListener('click', (e) => {
      if (e.target === adminOverlay) closeAdminOverlay();
    });
  }

  const secretCancel = document.getElementById('admin-secret-cancel');
  if (secretCancel) secretCancel.addEventListener('click', () => resolveAdminSecret(''));

  const secretConfirm = document.getElementById('admin-secret-confirm');
  if (secretConfirm) secretConfirm.addEventListener('click', submitAdminSecret);

  const secretInput = document.getElementById('admin-secret-input');
  if (secretInput) {
    secretInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitAdminSecret();
    });
  }

  const secretDialog = document.getElementById('admin-secret-dialog');
  if (secretDialog) {
    secretDialog.addEventListener('click', (e) => {
      if (e.target === secretDialog) resolveAdminSecret('');
    });
  }

  const setupCancel = document.getElementById('setup-dialog-cancel');
  if (setupCancel) setupCancel.addEventListener('click', closeSetupDialog);

  const setupConfirm = document.getElementById('setup-dialog-confirm');
  if (setupConfirm) setupConfirm.addEventListener('click', submitSetupDialog);

  const setupDialog = document.getElementById('setup-dialog');
  if (setupDialog) {
    setupDialog.addEventListener('click', (e) => {
      if (e.target === setupDialog) closeSetupDialog();
    });
  }
}


/* ── Initialisation ───────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initEvents();
  loadSharedSiteConfig();
  fetchTables();
  fetchDisplay();
  setInterval(fetchTables, REFRESH_MS);
  setInterval(fetchDisplay, DISPLAY_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      fetchTables();
      fetchDisplay();
    }
  });
});
