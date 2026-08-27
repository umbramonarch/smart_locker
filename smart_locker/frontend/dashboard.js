/**
 * @fileoverview Public dashboard: Inventory (Excel), Locker (SQLite), and
 *               Display (kiosk snapshot). Sort, search, status filter, and
 *               polling. Anyone can change owner on Inventory and Locker
 *               after confirm. No login. No remote control of the kiosk.
 * @project smart_locker/frontend
 * @description Tabs switch locally. Inventory errors (share down) leave
 *              the Locker tab usable. Asset-label text comes from /api/config.
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

/** Names for the owner datalist (users + registrants + in-locker token). */
let ownerNames = [];
/** In-locker Location token from GET /api/dashboard/owners. */
let inLockerToken = 'Locker';
/** PM currently open in the owner dialog, or ''. */
let ownerEditPm = '';


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
}


/* ── Data fetching ────────────────────────────────────────────────────────── */

/**
 * Fetch Locker (SQLite) and Inventory (Excel) independently so a down share
 * only fails Inventory.
 */
async function fetchTables() {
  try {
    const res = await fetch('/api/dashboard/devices');
    if (res.ok) {
      devicesData = await res.json();
      renderDevices();
    }
  } catch (_) { /* keep stale locker rows */ }

  try {
    const res = await fetch('/api/dashboard/inventory');
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
    renderInventory();
  } catch (_) {
    inventoryError = 'Catalog Excel is not available.';
    renderInventory();
  }

  updateTimestamp();
  fetchOwners();
}


/**
 * Load dropdown names (registered users + registrants + in-locker token).
 */
async function fetchOwners() {
  try {
    const res = await fetch('/api/dashboard/owners');
    if (!res.ok) return;
    const data = await res.json();
    ownerNames = Array.isArray(data.names) ? data.names : [];
    if (typeof data.in_locker_token === 'string' && data.in_locker_token.trim()) {
      inLockerToken = data.in_locker_token.trim();
    }
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
  try {
    const res = await fetch('/api/dashboard/display');
    if (!res.ok) return;
    const data = await res.json();
    const screenEl = document.getElementById('display-screen');
    const userEl = document.getElementById('display-user');
    if (screenEl) screenEl.textContent = data.label || data.screen || '—';
    if (userEl) userEl.textContent = data.user_name || 'None';
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
      <td>${ownerCell(d.pm_number, d.location)}</td>
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
      <td>${ownerCell(d.pm_number, d.borrower_name || (d.status === 'available' ? inLockerToken : ''))}</td>
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
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
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


/* ── Event wiring ─────────────────────────────────────────────────────────── */

/**
 * Wire tabs, sort headers, Locker filters, Inventory search, and owner edit.
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
      renderInventory();
    });
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.owner-btn');
    if (btn) {
      openOwnerDialog(btn.dataset.pm || '', btn.dataset.owner || '');
    }
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
}


/* ── Initialisation ───────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initEvents();
  loadSiteConfig();
  fetchOwners();
  fetchTables();
  fetchDisplay();
  setInterval(fetchTables, REFRESH_MS);
  setInterval(fetchDisplay, DISPLAY_MS);
});
