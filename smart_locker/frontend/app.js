/* ============================================================
   STATE — single source of truth for the UI
============================================================ */
const S = {
  screen:     'idle',   // current screen id
  user:       null,     // { id, name, role }
  devices:    [],
  selected:   null,     // device object open in detail overlay
  mode:       null,     // 'borrow' | 'return'
  prevScreen: null,     // screen that was active before an overlay opened
  idleTimer:  null,
  cdTimer:    null,
  cdSeconds:  120,      // matches SESSION_TIMEOUT_SECONDS in settings.py
  cdWarnAt:   10,       // show warning when N seconds remain
};

/* ============================================================
   DEMO DATA — remove when real API is connected
============================================================ */
const DEMO_USERS = [
  { id: 1, name: 'Alex Johnson', role: 'admin' },
  { id: 2, name: 'Jamie Lee',    role: 'user'  },
  { id: 3, name: 'Morgan Chen',  role: 'user'  },
];
let demoUserIdx = 0;

const DEMO_DEVICES = [
  { id:1, name:'Canon EOS R5',      device_type:'Camera',         serial_number:'CNR5-001',        locker_slot:1, description:'Full-frame mirrorless camera. Handle with care. Return with battery charged.',                    image_path:null, status:'available',   borrower_name:null      },
  { id:2, name:'DJI Mavic 3',       device_type:'Drone',          serial_number:'DJI-M3-042',      locker_slot:2, description:'Professional drone with 4/3 CMOS sensor. Requires signed certification form to borrow.',         image_path:null, status:'borrowed',    borrower_name:'Sarah K.' },
  { id:3, name:'Rode VideoMic Pro', device_type:'Microphone',     serial_number:'RVM-P-007',       locker_slot:3, description:'Directional condenser microphone. Include dead-cat windshield when filming outdoors.',             image_path:null, status:'available',   borrower_name:null      },
  { id:4, name:'MacBook Pro 16"',   device_type:'Laptop',         serial_number:'MBP16-2024-03',   locker_slot:4, description:'M3 Max · 64 GB RAM. Adobe CC, DaVinci Resolve, and Final Cut Pro installed.',                     image_path:null, status:'available',   borrower_name:null      },
  { id:5, name:'Godox SL-60W',      device_type:'Studio Light',   serial_number:'GOD-SL60-011',    locker_slot:5, description:'LED video light · 60 W daylight. Includes softbox and C-stand adapter.',                         image_path:null, status:'borrowed',    borrower_name:'You'     },
  { id:6, name:'Zoom H6 Recorder',  device_type:'Audio Recorder', serial_number:'ZMH6-099',        locker_slot:6, description:'Portable 6-track recorder. 32 GB SD card included. Batteries not provided.',                       image_path:null, status:'available',   borrower_name:null      },
  { id:7, name:'iPad Pro 12.9"',    device_type:'Tablet',         serial_number:'IPD-PRO-2024-08', locker_slot:7, description:'Includes Apple Pencil 2 and Magic Keyboard. Configured for field monitoring.',                    image_path:null, status:'available',   borrower_name:null      },
  { id:8, name:'Manfrotto 504X',    device_type:'Tripod',         serial_number:'MFT-504X-002',    locker_slot:8, description:'Professional fluid-head video tripod. 12 kg payload. Inspect head lock before each use.',          image_path:null, status:'maintenance', borrower_name:null      },
];

/* ============================================================
   API STUBS
   Each function is a 1-for-1 replacement when the FastAPI
   backend API layer is ready. To connect to the real API:
     1. Uncomment the fetch() block inside the function.
     2. Delete the demo code below it.
============================================================ */

/**
 * POST /api/auth/tap
 * Body:    { uid_hmac: string }
 * Returns: { success: bool, user: { id, name, role } | null }
 *
 * Note: in production, uid_hmac arrives from the NFC reader via
 * a backend-pushed event (SSE or WebSocket), not from the UI.
 */
async function apiAuthTap(uid_hmac) {
  // const res = await fetch('/api/auth/tap', {
  //   method: 'POST',
  //   headers: { 'Content-Type': 'application/json' },
  //   body: JSON.stringify({ uid_hmac }),
  // });
  // return res.json();

  await sleep(380);
  const user = DEMO_USERS[demoUserIdx++ % DEMO_USERS.length];
  return { success: true, user };
}

/**
 * GET /api/devices
 * Returns: Array of {
 *   id, name, device_type, serial_number, locker_slot,
 *   description, image_path, status, borrower_name
 * }
 */
async function apiGetDevices() {
  // const res = await fetch('/api/devices');
  // return res.json();

  await sleep(280);
  return DEMO_DEVICES;
}

/**
 * POST /api/borrow
 * Body:    { device_id: int }
 * Returns: { success: bool, message: string }
 */
async function apiBorrow(device_id) {
  // const res = await fetch('/api/borrow', {
  //   method: 'POST',
  //   headers: { 'Content-Type': 'application/json' },
  //   body: JSON.stringify({ device_id }),
  // });
  // return res.json();

  await sleep(480);
  const d = S.devices.find(x => x.id === device_id);
  if (d) { d.status = 'borrowed'; d.borrower_name = 'You'; }
  return { success: true, message: `${d?.name ?? 'Device'} borrowed.` };
}

/**
 * POST /api/return
 * Body:    { device_id: int }
 * Returns: { success: bool, message: string }
 */
async function apiReturn(device_id) {
  // const res = await fetch('/api/return', {
  //   method: 'POST',
  //   headers: { 'Content-Type': 'application/json' },
  //   body: JSON.stringify({ device_id }),
  // });
  // return res.json();

  await sleep(480);
  const d = S.devices.find(x => x.id === device_id);
  if (d) { d.status = 'available'; d.borrower_name = null; }
  return { success: true, message: `${d?.name ?? 'Device'} returned.` };
}

/**
 * POST /api/session/end
 * Returns: { success: bool }
 */
async function apiEndSession() {
  // await fetch('/api/session/end', { method: 'POST' });
  await sleep(200);
}

/* ============================================================
   NAVIGATION — clip-path wipe transitions
   Screens and overlays are separate: opening an overlay does
   not exit the underlying screen; it layers on top.
============================================================ */
function getEl(id) {
  return document.getElementById('screen-' + id)
      || document.getElementById('overlay-' + id);
}

function navigate(toId) {
  if (S.screen === toId) return;

  const fromEl = getEl(S.screen);
  const toEl   = getEl(toId);
  if (!toEl) return;

  const toIsOverlay   = toEl.classList.contains('overlay');
  const fromIsOverlay = fromEl && fromEl.classList.contains('overlay');

  // Reveal the target (double rAF ensures the CSS transition fires)
  toEl.style.display = '';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    toIsOverlay ? toEl.classList.add('visible') : toEl.classList.add('active');
  }));

  // Exit the current screen (skip when opening an overlay on top of it)
  if (!toIsOverlay && fromEl && !fromIsOverlay) {
    fromEl.classList.add('exit');
    setTimeout(() => fromEl.classList.remove('active', 'exit'), 460);
  }

  // Close an overlay when navigating back to a regular screen
  if (fromIsOverlay && !toIsOverlay) {
    fromEl.classList.add('hidden-up');
    setTimeout(() => {
      fromEl.classList.remove('visible', 'hidden-up');
      fromEl.style.display = 'none';
    }, 460);
  }

  S.screen = toId;
}

/* ============================================================
   CLOCK
============================================================ */
function tickClock() {
  const now = new Date();
  document.getElementById('clock-time').textContent =
    now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
  document.getElementById('clock-date').textContent =
    now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }).toUpperCase();
}
tickClock();
setInterval(tickClock, 1000);

/* ============================================================
   INACTIVITY TIMER
   Arms after every user interaction. When it fires, shows a
   10-second countdown overlay before auto-ending the session.
============================================================ */
function armIdle() {
  clearTimeout(S.idleTimer);
  if (S.screen === 'idle') return;
  S.idleTimer = setTimeout(showInactivity, (S.cdSeconds - S.cdWarnAt) * 1000);
}

function showInactivity() {
  S.prevScreen = S.screen;
  const overlay = document.getElementById('overlay-inactivity');
  overlay.style.display = '';
  let secs = S.cdWarnAt;
  document.getElementById('inactivity-num').textContent = secs;
  requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add('visible')));
  S.cdTimer = setInterval(() => {
    secs--;
    document.getElementById('inactivity-num').textContent = secs;
    if (secs <= 0) { clearInterval(S.cdTimer); endSession(true); }
  }, 1000);
}

function dismissInactivity() {
  clearInterval(S.cdTimer);
  const overlay = document.getElementById('overlay-inactivity');
  if (!overlay.classList.contains('visible')) return;
  overlay.classList.add('hidden-up');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-up');
    overlay.style.display = 'none';
  }, 460);
  if (S.prevScreen && S.screen !== S.prevScreen) S.screen = S.prevScreen;
  armIdle();
}

// Re-arm the inactivity timer on any user activity
['click', 'touchstart', 'keydown'].forEach(evt =>
  document.addEventListener(evt, () => { if (S.screen !== 'idle') armIdle(); })
);

/* ============================================================
   AUTH
============================================================ */
async function handleTap() {
  if (S.screen !== 'idle') return;
  const result = await apiAuthTap('DEMO_UID_HMAC');
  if (!result.success || !result.user) {
    showAuthFailed();
    return;
  }
  S.user = result.user;
  fillMainMenu(result.user);
  navigate('main-menu');
  armIdle();
}

function showAuthFailed() {
  navigate('auth-failed');
  // Animate the depletion progress bar
  const bar = document.getElementById('auth-progress');
  bar.style.transition = 'none';
  bar.style.width = '100%';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    bar.style.transition = 'width 3s linear';
    bar.style.width = '0%';
  }));
  setTimeout(() => navigate('idle'), 3100);
}

async function endSession(fromTimeout = false) {
  clearTimeout(S.idleTimer);
  clearInterval(S.cdTimer);
  dismissInactivity();
  await apiEndSession();
  S.user     = null;
  S.devices  = [];
  S.selected = null;
  navigate('idle');
}

/* ============================================================
   MAIN MENU
============================================================ */
function fillMainMenu(user) {
  const first = user.name.split(' ')[0].toUpperCase();
  document.getElementById('menu-name').textContent    = first;
  document.getElementById('user-avatar').textContent  =
    user.name.split(' ').map(n => n[0]).join('').toUpperCase();
  document.getElementById('badge-name').textContent   = user.name;
  const pill = document.getElementById('badge-role');
  pill.textContent = user.role === 'admin' ? 'Admin' : 'User';
  pill.className   = 'role-pill' + (user.role === 'admin' ? ' admin' : '');
}

/* ============================================================
   DEVICE GRID — borrow and return screens
============================================================ */
async function openBorrow() {
  navigate('borrow');
  const devices = await apiGetDevices();
  S.devices = devices;
  const myCount = devices.filter(d => d.borrower_name === 'You').length;
  document.getElementById('borrow-badge').textContent = `${myCount} / 5 borrowed`;
  buildGrid('borrow-grid', devices, 'borrow');
}

async function openReturn() {
  navigate('return');
  const devices = await apiGetDevices();
  S.devices = devices;
  const mine = devices.filter(d => d.borrower_name === 'You').length;
  document.getElementById('return-badge').textContent =
    `${mine} item${mine !== 1 ? 's' : ''} to return`;
  buildGrid('return-grid', devices, 'return');
}

function buildGrid(gridId, devices, mode) {
  const grid   = document.getElementById(gridId);
  grid.innerHTML = '';

  const sorted = [...devices].sort((a, b) => (a.locker_slot || 99) - (b.locker_slot || 99));

  sorted.forEach((dev, i) => {
    const avail = dev.status === 'available';
    const mine  = dev.borrower_name === 'You';
    const maint = dev.status === 'maintenance';

    // Card modifier class drives hover styles and pointer-events
    let cls = 'device-card';
    if (mode === 'borrow') cls += avail ? ' available' : ' unavailable';
    else                   cls += mine  ? ' mine'      : ' unavailable';

    // Status badge text and colour class
    let statusCls, statusTxt;
    if      (mine)  { statusCls = 'mine-tag';  statusTxt = 'YOURS';     }
    else if (avail) { statusCls = 'available'; statusTxt = 'AVAILABLE'; }
    else if (maint) { statusCls = 'maint';     statusTxt = 'MAINT';     }
    else            { statusCls = 'in-use';    statusTxt = 'IN USE';    }

    const slotLabel = `S${String(dev.locker_slot ?? 0).padStart(2, '0')}`;

    const card = document.createElement('div');
    card.className = cls;
    card.innerHTML = `
      <div class="card-image">
        ${dev.image_path
          ? `<img src="${dev.image_path}" alt="${dev.name}" loading="lazy">`
          : `<div class="card-img-placeholder">
               <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
               <span>${slotLabel}</span>
             </div>`}
        <div class="card-slot">${slotLabel}</div>
        <div class="card-status ${statusCls}">${statusTxt}</div>
      </div>
      <div class="card-body">
        <div class="card-name">${dev.name}</div>
        <div class="card-type">${dev.device_type}</div>
      </div>
    `;

    // Staggered entrance: each card fades/slides in with a small delay
    setTimeout(() => card.classList.add('in'), i * 55);

    // All cards are tappable — unavailable ones still show info in detail view
    card.addEventListener('click', () => { clickSound(); openDetail(dev, mode); });
    grid.appendChild(card);
  });
}

/* ============================================================
   DEVICE DETAIL OVERLAY
============================================================ */
function openDetail(dev, mode) {
  S.selected   = dev;
  S.mode       = mode;
  S.prevScreen = S.screen;

  const avail = dev.status === 'available';
  const mine  = dev.borrower_name === 'You';
  const maint = dev.status === 'maintenance';
  const slot  = `S${String(dev.locker_slot ?? 0).padStart(2, '0')}`;

  // Populate text fields
  document.getElementById('detail-slot-tag').textContent =
    `SLOT ${String(dev.locker_slot ?? 0).padStart(2, '0')}`;
  document.getElementById('detail-name').textContent    = dev.name;
  document.getElementById('detail-type').textContent    = dev.device_type;
  document.getElementById('detail-serial').textContent  = dev.serial_number;
  document.getElementById('detail-img-slot').textContent = slot;
  document.getElementById('detail-status').textContent  =
    maint ? 'Under Maintenance' : avail ? 'Available' : mine ? 'Borrowed by You' : 'In Use';
  document.getElementById('detail-desc').textContent    =
    dev.description || 'No description available.';

  // Image — swap in a real img tag if a path exists
  const imgPane     = document.getElementById('detail-img-pane');
  const placeholder = document.getElementById('detail-img-placeholder');
  const existingImg = imgPane.querySelector('img');
  if (existingImg) existingImg.remove();
  if (dev.image_path) {
    placeholder.classList.add('hidden');
    const img = document.createElement('img');
    img.src = dev.image_path;
    img.alt = dev.name;
    imgPane.appendChild(img);
  } else {
    placeholder.classList.remove('hidden');
  }

  // Borrower info row (only shown when borrowed by someone else)
  const borrowerRow = document.getElementById('detail-borrower-row');
  if (dev.borrower_name && !mine) {
    document.getElementById('detail-borrower-name').textContent = dev.borrower_name;
    borrowerRow.classList.remove('hidden');
  } else {
    borrowerRow.classList.add('hidden');
  }

  // Confirm button — state/label depends on mode and device status
  const btn = document.getElementById('confirm-btn');
  btn.className = 'confirm-btn';
  if (mode === 'borrow') {
    if (avail)      { btn.textContent = 'Confirm Borrow';    btn.classList.add('do-borrow'); }
    else if (maint) { btn.textContent = 'Under Maintenance'; btn.classList.add('disabled'); }
    else            { btn.textContent = 'Already Borrowed';  btn.classList.add('disabled'); }
  } else {
    if (mine)       { btn.textContent = 'Confirm Return';    btn.classList.add('do-return'); }
    else if (avail) { btn.textContent = 'Not Borrowed';      btn.classList.add('disabled'); }
    else            { btn.textContent = 'Not Your Device';   btn.classList.add('disabled'); }
  }

  navigate('device-detail');
}

function closeDetail() {
  const overlay = document.getElementById('overlay-device-detail');
  overlay.classList.add('hidden-up');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-up');
    overlay.style.display = 'none';
  }, 460);
  // Restore the underlying grid screen as the current screen
  S.screen = S.prevScreen || (S.mode === 'return' ? 'return' : 'borrow');
}

async function confirmAction() {
  const btn = document.getElementById('confirm-btn');
  if (btn.classList.contains('disabled')) return;

  btn.textContent = '…';
  btn.classList.add('disabled');

  const dev = S.selected;
  const result = S.mode === 'borrow'
    ? await apiBorrow(dev.id)
    : await apiReturn(dev.id);

  closeDetail();
  showToast(result.message, result.success ? 'success' : 'error');

  // Refresh the grid to reflect the updated device status
  if (S.mode === 'borrow') openBorrow();
  else                     openReturn();
}

/* ============================================================
   TOAST NOTIFICATION
============================================================ */
let toastTimer;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  clearTimeout(toastTimer);
  el.textContent = msg;
  el.className = `show${type ? ' toast-' + type : ''}`;
  toastTimer = setTimeout(() => { el.className = ''; }, 3200);
}

/* ============================================================
   CUSTOM CURSOR — dot tracks instantly, ring lags behind
============================================================ */
const cursorDot  = document.getElementById('cursor');
const cursorRing = document.getElementById('cursor-ring');
let mx = 0, my = 0, rx = 0, ry = 0;

document.addEventListener('mousemove', e => {
  mx = e.clientX; my = e.clientY;
  cursorDot.style.left = mx + 'px';
  cursorDot.style.top  = my + 'px';
});

(function animRing() {
  rx += (mx - rx) * 0.13;
  ry += (my - ry) * 0.13;
  cursorRing.style.left = rx + 'px';
  cursorRing.style.top  = ry + 'px';
  requestAnimationFrame(animRing);
})();

/* ============================================================
   AUDIO CLICK FEEDBACK — synthesised via Web Audio API
   No audio files needed; produces a short descending tone.
============================================================ */
function clickSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const g   = ctx.createGain();
    osc.connect(g);
    g.connect(ctx.destination);
    osc.frequency.setValueAtTime(900, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(420, ctx.currentTime + 0.07);
    g.gain.setValueAtTime(0.07, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.09);
    osc.start();
    osc.stop(ctx.currentTime + 0.09);
  } catch (_) { /* audio not available — silently ignore */ }
}

/* ============================================================
   EVENT LISTENERS
============================================================ */
document.getElementById('btn-borrow').addEventListener('click', () => { clickSound(); openBorrow();  });
document.getElementById('btn-return').addEventListener('click', () => { clickSound(); openReturn();  });
document.getElementById('btn-end').addEventListener('click',    () => { clickSound(); endSession();  });

document.querySelectorAll('.back-btn').forEach(btn =>
  btn.addEventListener('click', () => { clickSound(); navigate(btn.dataset.target); })
);

document.getElementById('detail-close').addEventListener('click',  () => { clickSound(); closeDetail();      });
document.getElementById('confirm-btn').addEventListener('click',   () => { clickSound(); confirmAction();    });
document.getElementById('stay-btn').addEventListener('click',      () => { clickSound(); dismissInactivity(); });

// Debug button: tap on idle → simulate auth; tap elsewhere → end session
document.getElementById('debug-btn').addEventListener('click', () => {
  if (S.screen === 'idle') handleTap();
  else                     endSession();
});

/* ============================================================
   INIT
============================================================ */
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Overlays start hidden so they don't briefly flash on page load
document.getElementById('overlay-inactivity').style.display    = 'none';
document.getElementById('overlay-device-detail').style.display = 'none';
