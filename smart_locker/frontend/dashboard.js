/**
 * @fileoverview Client-side logic for the Smart Locker network dashboard.
 *               Fetches device, transaction, and user data from the public
 *               dashboard API, renders sortable/filterable tables, and
 *               auto-refreshes every 30 seconds.
 * @project smart_locker/frontend
 * @description Manages three data tables (Devices, Transactions, Users) with
 *              column sorting, status filtering for devices, section collapse
 *              toggling, and a polling-based auto-refresh cycle. All data
 *              comes from the /api/dashboard/* endpoints which require no
 *              authentication.
 */

/* ── State ────────────────────────────────────────────────────────────────── */

/** Cached data arrays from the most recent fetch — used for re-rendering
 *  when sort/filter changes without hitting the server again. */
let devicesData = [];
let transactionsData = [];
let usersData = [];

/** Current sort configuration per table. Key is a column name, direction is
 *  'asc' or 'desc'. Null key means no active sort (use server order). */
const sortState = {
  devices:      { key: null, dir: 'asc' },
  transactions: { key: null, dir: 'asc' },
  users:        { key: null, dir: 'asc' },
};

/** Active status filter for the devices table ('all', 'available',
 *  'borrowed', or 'maintenance'). */
let activeFilter = 'all';

/** Auto-refresh interval ID — stored so it can be cleared if needed. */
let refreshInterval = null;

/** How often to poll the server for fresh data (milliseconds). */
const REFRESH_MS = 30_000;


/* ── Data fetching ────────────────────────────────────────────────────────── */

/**
 * Fetch all three dashboard endpoints in parallel and re-render tables.
 *
 * Catches network errors silently — the dashboard simply shows stale data
 * until the next successful refresh. Updates the "last updated" timestamp
 * on success.
 */
async function fetchAll() {
  try {
    const [devRes, txnRes, usrRes] = await Promise.all([
      fetch('/api/dashboard/devices'),
      fetch('/api/dashboard/transactions'),
      fetch('/api/dashboard/users'),
    ]);

    if (devRes.ok) {
      devicesData = await devRes.json();
      renderDevices();
    }
    if (txnRes.ok) {
      transactionsData = await txnRes.json();
      renderTransactions();
    }
    if (usrRes.ok) {
      usersData = await usrRes.json();
      renderUsers();
    }

    updateTimestamp();
  } catch (_) {
    /* Network error — keep showing stale data, retry on next cycle */
  }
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
 * Handles nulls (sorted to the end), numbers (numeric comparison), and
 * strings (locale-aware, case-insensitive). The original array is not
 * mutated — a sorted copy is returned.
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

    /* Nulls and empty strings sort to the end regardless of direction */
    if (va == null || va === '') return 1;
    if (vb == null || vb === '') return -1;

    /* Numeric comparison when both values look like numbers */
    if (typeof va === 'number' && typeof vb === 'number') {
      return dir === 'asc' ? va - vb : vb - va;
    }

    /* String comparison — locale-aware, case-insensitive */
    va = String(va).toLowerCase();
    vb = String(vb).toLowerCase();
    const cmp = va.localeCompare(vb);
    return dir === 'asc' ? cmp : -cmp;
  });
}


/**
 * Handle a click on a sortable column header.
 *
 * Toggles the sort direction if the column is already active, otherwise
 * sets it as the new sort column in ascending order. Updates the visual
 * sort indicator and re-renders the affected table.
 *
 * @param {string} table - Table identifier ('devices', 'transactions', 'users').
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

  /* Update visual indicators on all <th> in this table */
  const tableEl = document.getElementById(`${table}-table`);
  tableEl.querySelectorAll('th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === key) {
      th.classList.add(state.dir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  });

  /* Re-render the table with the new sort applied */
  if (table === 'devices') renderDevices();
  else if (table === 'transactions') renderTransactions();
  else if (table === 'users') renderUsers();
}


/* ── Rendering ────────────────────────────────────────────────────────────── */

/**
 * Render the devices table from cached data, applying the active status
 * filter and sort order. Updates the device count badge.
 */
function renderDevices() {
  let data = devicesData;

  /* Apply status filter */
  if (activeFilter !== 'all') {
    data = data.filter(d => d.status === activeFilter);
  }

  /* Apply sort */
  const s = sortState.devices;
  if (s.key) data = sortData(data, s.key, s.dir);

  const tbody = document.getElementById('devices-tbody');
  const empty = document.getElementById('devices-empty');
  const count = document.getElementById('device-count');

  /* Summary counts for the badge (always computed from full data) */
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
      <td>${esc(d.borrower_name ?? '')}</td>
      <td>${esc(d.calibration_due ?? '')}</td>
    </tr>
  `).join('');
}


/**
 * Render the transactions table from cached data with the current sort.
 * Updates the transaction count badge.
 */
function renderTransactions() {
  let data = transactionsData;

  const s = sortState.transactions;
  if (s.key) data = sortData(data, s.key, s.dir);

  const tbody = document.getElementById('transactions-tbody');
  const empty = document.getElementById('txn-empty');
  const count = document.getElementById('txn-count');

  count.textContent = `${transactionsData.length} records`;

  if (data.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = data.map(t => {
    const typeCls = t.transaction_type === 'borrow' ? 'borrow' : 'return';
    return `
    <tr>
      <td>${esc(t.timestamp ?? '')}</td>
      <td>${esc(t.user_name)}</td>
      <td>${esc(t.device_name)}</td>
      <td><span class="type-badge ${typeCls}">${t.transaction_type}</span></td>
      <td>${esc(t.performed_by)}</td>
      <td>${esc(t.notes ?? '')}</td>
    </tr>
  `;
  }).join('');
}


/**
 * Render the users table from cached data with the current sort.
 * Updates the user count badge.
 */
function renderUsers() {
  let data = usersData;

  const s = sortState.users;
  if (s.key) data = sortData(data, s.key, s.dir);

  const tbody = document.getElementById('users-tbody');
  const empty = document.getElementById('users-empty');
  const count = document.getElementById('user-count');

  count.textContent = `${usersData.length} users`;

  if (data.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = data.map(u => {
    const roleCls = u.role === 'admin' ? 'admin' : 'user';
    const activeCls = u.is_active ? 'yes' : 'no';
    const activeLabel = u.is_active ? 'Yes' : 'No';
    return `
    <tr>
      <td>${esc(u.display_name)}</td>
      <td><span class="role-badge ${roleCls}">${u.role}</span></td>
      <td><span class="active-dot ${activeCls}"></span>${activeLabel}</td>
      <td>${esc(u.registered_at ?? '')}</td>
    </tr>
  `;
  }).join('');
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


/* ── Event wiring ─────────────────────────────────────────────────────────── */

/**
 * Wire up section collapse toggles, sort headers, and filter buttons.
 * Called once on DOMContentLoaded.
 */
function initEvents() {
  /* Section collapse toggles — click header to expand/collapse */
  document.querySelectorAll('.section-header[data-toggle]').forEach(header => {
    header.addEventListener('click', (e) => {
      /* Don't toggle when clicking filter buttons inside the header */
      if (e.target.closest('.filter-btn')) return;
      header.closest('.section').classList.toggle('collapsed');
    });
  });

  /* Column sort — click any <th> with a data-sort attribute */
  document.querySelectorAll('#devices-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => handleSort('devices', th.dataset.sort));
  });
  document.querySelectorAll('#transactions-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => handleSort('transactions', th.dataset.sort));
  });
  document.querySelectorAll('#users-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => handleSort('users', th.dataset.sort));
  });

  /* Status filter buttons for the devices table */
  document.querySelectorAll('#status-filters .filter-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      document.querySelectorAll('#status-filters .filter-btn').forEach(b =>
        b.classList.remove('active')
      );
      btn.classList.add('active');
      activeFilter = btn.dataset.filter;
      renderDevices();
    });
  });
}


/* ── Initialisation ───────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initEvents();
  fetchAll();
  refreshInterval = setInterval(fetchAll, REFRESH_MS);
});
