/**
 * @fileoverview Client-side state machine for the kiosk touch UI. Manages screen
 *               transitions, API communication, SSE event handling, and user
 *               interaction flow across idle, auth, menu, locker availability,
 *               return, detail, registration (incl. Register as admin), admin,
 *               Register Device (PM + slot + NFC), Users (deactivate / replace
 *               card), return-slot, software-update, and appliance shutdown overlays.
 * @project smart_locker/frontend
 * @description Demo mode (?demo), circle-reveal transitions, split text,
 *              inactivity countdown, and self-registration.
 */

/* ============================================================
   STATE — single source of truth for the UI
============================================================ */
/** Whether to use demo mode with mock data. Enabled by adding ?demo to the URL. */
const USE_DEMO = new URLSearchParams(window.location.search).has('demo');

/* ============================================================
   PERFORMANCE MODE
   window.__LITE__ is set by the detection script in index.html
   (low-power host / prefers-reduced-motion / ?lite). PERF.lite is
   the LIVE flag every effect consults, so a runtime FPS downgrade
   (see enableLite) instantly stops the heavy work. Pointer-driven
   effects (custom cursor, magnetic hover, mouse parallax) are also
   gated on canHover, so they never run on the touch panel.
============================================================ */
const PERF = { lite: !!window.__LITE__ };
const canHover = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

/** Site noun for the locker join key. API field stays pm_number. */
let ASSET_LABEL = 'PM number';

/**
 * Apply the site join-key label to kiosk copy (Register Device, detail pane).
 * @param {string} label - Display noun from GET /api/config.
 */
function applyAssetLabels(label) {
  const text = (label || '').trim() || 'PM number';
  ASSET_LABEL = text;
  const detail = document.getElementById('detail-pm-label');
  if (detail) detail.textContent = text;
  const adminDesc = document.getElementById('admin-register-device-desc');
  if (adminDesc) adminDesc.textContent = `${text}, slot, then tap NFC`;
  const listHint = document.getElementById('bind-list-hint');
  if (listHint) {
    listHint.textContent =
      `Add a unit from the Excel list (${text} + free slot), then tap its sticker. Existing rows can bind, unbind, or change slot.`;
  }
  const addHint = document.getElementById('bind-add-hint');
  if (addHint) {
    addHint.textContent =
      `Enter the ${text} from the catalog spreadsheet, pick a free slot, then continue to tap the sticker.`;
  }
  const search = document.getElementById('bind-search');
  if (search) search.placeholder = `Search name or ${text}…`;
  const pmInput = document.getElementById('bind-pm-input');
  if (pmInput) pmInput.placeholder = text;
}

/**
 * Load SMART_LOCKER_ASSET_LABEL from the backend. Demo mode keeps the default.
 * @returns {Promise<void>}
 */
async function loadSiteConfig() {
  if (USE_DEMO) return;
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const data = await res.json();
    if (data && typeof data.asset_label === 'string') applyAssetLabels(data.asset_label);
  } catch (_) { /* keep built-in label */ }
}

/**
 * Switch to lite mode at runtime (called by the FPS probe on a janky host).
 * Adds the html.lite class so the CSS strips the heavy effects, flips the
 * live PERF.lite flag (the cursor loop and parallax/magnetic handlers all
 * bail on it), and persists the choice for this machine.
 */
function enableLite() {
  if (PERF.lite) return;
  PERF.lite = true;
  document.documentElement.classList.add('lite');
  try { localStorage.setItem('sl_lite', '1'); } catch (_) { /* storage unavailable */ }
}

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
  lastClickX: null,     // track click origin for circle reveal
  lastClickY: null,
  adminRegistration: false, // true when admin-initiated manual registration is in progress
  usersReplacePending: false, // a replace-card window is armed on the Users overlay
  updating:   false,    // software-update overlay is up; SSE must not navigate
  handoverDeviceId: null, // device awaiting handover confirmation
  handoverFromScreen: null, // screen/overlay active when the handover opened
};

/** @type {string|null} Currently selected registrant name from the name list */
let selectedRegistrantName = null;

/* ============================================================
   DEMO DATA — remove when real API is connected
============================================================ */
const DEMO_USERS = [
  { id: 1, name: 'Alex Johnson', role: 'admin' },
  { id: 2, name: 'Jamie Lee',    role: 'user'  },
  { id: 3, name: 'Morgan Chen',  role: 'user'  },
];
/** @type {number} Index into DEMO_USERS, cycles on each simulated card tap */
let demoUserIdx = 0;

const DEMO_DEVICES = [
  { id:1, pm_number:'PM-001', name:'Keysight DSOX3054T',  device_type:'Oscilloscope',   serial_number:'MY12345678',  manufacturer:'Keysight',       model:'DSOX3054T',   barcode:'490001', locker_slot:1,  description:null, image_path:null, calibration_due:'2026-09-15', status:'available',   borrower_name:null, has_tag:false },
  { id:2, pm_number:'PM-002', name:'Rohde & Schwarz HMC8043', device_type:'Power Supply', serial_number:'RS-HMC-042', manufacturer:'Rohde & Schwarz', model:'HMC8043',    barcode:'490002', locker_slot:2,  description:null, image_path:null, calibration_due:'2026-11-01', status:'borrowed',    borrower_name:'Sarah K.' },
  { id:3, pm_number:'PM-003', name:'Fluke 87V',           device_type:'Multimeter',     serial_number:'FL-87V-007',  manufacturer:'Fluke',          model:'87V',         barcode:'490003', locker_slot:3,  description:null, image_path:null, calibration_due:'2026-06-30', status:'available',   borrower_name:null       },
  { id:4, pm_number:'PM-004', name:'Keysight 34465A',     device_type:'Multimeter',     serial_number:'MY98765432',  manufacturer:'Keysight',       model:'34465A',      barcode:'490004', locker_slot:4,  description:null, image_path:null, calibration_due:null,         status:'available',   borrower_name:null       },
  { id:5, pm_number:'PM-005', name:'Fluke i400s',         device_type:'Current Probe',  serial_number:null,          manufacturer:'Fluke',          model:'i400s',       barcode:'490005', locker_slot:5,  description:null, image_path:null, calibration_due:'2027-01-15', status:'borrowed',    borrower_name:'You'      },
  { id:6, pm_number:'PM-006', name:'Tektronix TBS2104X',  device_type:'Oscilloscope',   serial_number:'TEK-TBS-099', manufacturer:'Tektronix',      model:'TBS2104X',    barcode:'490006', locker_slot:6,  description:null, image_path:null, calibration_due:'2026-08-20', status:'available',   borrower_name:null       },
  { id:7, pm_number:'PM-007', name:'Hioki DT4282',        device_type:'Multimeter',     serial_number:null,          manufacturer:'Hioki',          model:'DT4282',      barcode:'490007', locker_slot:7,  description:null, image_path:null, calibration_due:null,         status:'available',   borrower_name:null       },
  { id:8, pm_number:'PM-008', name:'Megger MIT485/2',     device_type:'Insulation Tester', serial_number:'MEG-485-002', manufacturer:'Megger',      model:'MIT485/2',    barcode:'490008', locker_slot:8,  description:null, image_path:null, calibration_due:'2026-12-01', status:'maintenance', borrower_name:null       },
];

/** @type {string[]} Demo registrant names for testing the name list without backend */
const DEMO_REGISTRANTS = [
  'Alice Bauer', 'Bob Fischer', 'Clara Hoffmann', 'David Klein',
  'Eva Meier', 'Felix Schneider', 'Greta Weber', 'Hans Richter',
  'Irene Schwarz', 'Jan Lehmann', 'Katrin Braun', 'Lars Werner',
];

/* ============================================================
   API — real fetch() calls with demo fallback
============================================================ */
/**
 * Authenticate a user by NFC card tap. In demo mode, cycles through demo users.
 * In live mode, auth is handled via SSE, so this only applies to demo.
 * @param {string} uid_hmac - The HMAC hash of the card UID.
 * @returns {Promise<Object>} Result with success boolean and optional user object.
 */
async function apiAuthTap(uid_hmac) {
  if (USE_DEMO) {
    await sleep(380); // simulate network latency
    const user = DEMO_USERS[demoUserIdx++ % DEMO_USERS.length];
    return { success: true, user };
  }
  // In live mode, auth comes via SSE — this is only called in demo mode
  return { success: false };
}

/**
 * Fetch all devices from the API. Returns demo data when in demo mode.
 * @returns {Promise<Array<Object>>} Array of device objects, or empty array on error.
 */
async function apiGetDevices() {
  if (USE_DEMO) { await sleep(280); return DEMO_DEVICES; } // simulate fetch latency
  const res = await fetch('/api/devices');
  if (!res.ok) return [];
  return await res.json();
}

/**
 * Borrow a device by ID. In demo mode, updates the local device status directly.
 * @param {number} device_id - The database ID of the device to borrow.
 * @returns {Promise<Object>} Result with success boolean and message string.
 */
async function apiBorrow(device_id) {
  if (USE_DEMO) {
    await sleep(480); // simulate borrow API round-trip
    const d = S.devices.find(x => x.id === device_id);
    if (d) { d.status = 'borrowed'; d.borrower_name = 'You'; }
    return { success: true, message: `${d?.name ?? 'Device'} borrowed.` };
  }
  const res = await fetch(`/api/devices/${device_id}/borrow`, { method: 'POST' });
  return await res.json();
}

/**
 * Return a borrowed device by ID. In demo mode, resets the local device status.
 * @param {number} device_id - The database ID of the device to return.
 * @returns {Promise<Object>} Result with success boolean and message string.
 */
async function apiReturn(device_id) {
  if (USE_DEMO) {
    await sleep(480); // simulate return API round-trip
    const d = S.devices.find(x => x.id === device_id);
    if (d) { d.status = 'available'; d.borrower_name = null; }
    return { success: true, message: `${d?.name ?? 'Device'} returned.` };
  }
  const res = await fetch(`/api/devices/${device_id}/return`, { method: 'POST' });
  return await res.json();
}

/**
 * Accept a handover of a borrowed device from another user. In demo mode, the
 * local device row is updated so the current user appears as the new borrower.
 * @param {number} device_id - The database ID of the device to transfer.
 * @returns {Promise<Object>} Result with success boolean and message string.
 */
async function apiTransfer(device_id) {
  if (USE_DEMO) {
    await sleep(480);
    const d = S.devices.find(x => x.id === device_id);
    if (d) { d.status = 'borrowed'; d.borrower_name = 'You'; }
    return { success: true, message: `${d?.name ?? 'Device'} transferred to you.` };
  }
  const res = await fetch(`/api/devices/${device_id}/transfer`, { method: 'POST' });
  return await res.json();
}

/**
 * End the current user session. Calls the backend session-end endpoint in live mode.
 * @returns {Promise<void>}
 */
async function apiEndSession() {
  if (USE_DEMO) { await sleep(200); return; } // simulate session end
  await fetch('/api/session/end', { method: 'POST' }).catch(() => {});
}

/**
 * Start the user self-registration flow by sending the user's name to the backend.
 * The backend then waits for the next NFC tap to associate a card with that name.
 * @param {string} name - The display name the new user entered.
 * @returns {Promise<Object>} Result with success boolean and optional error detail.
 */
async function apiStartRegistration(name) {
  if (USE_DEMO) {
    await sleep(400); // simulate registration API round-trip
    return { success: true };
  }
  const res = await fetch('/api/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  return await res.json();
}

/**
 * Cancel an in-progress registration request on the backend.
 * @returns {Promise<void>}
 */
async function apiCancelRegistration() {
  if (USE_DEMO) return;
  await fetch('/api/register/cancel', { method: 'POST' }).catch(() => {});
}

/**
 * Fetch the list of approved registrant names from the backend. These names
 * come from the Location column in the source Excel and are
 * stored in the registrants table. Already-registered users are excluded.
 * @returns {Promise<string[]>} Alphabetically sorted array of available names.
 */
async function apiGetRegistrants() {
  if (USE_DEMO) {
    await sleep(300); // simulate API latency
    return DEMO_REGISTRANTS;
  }
  try {
    const res = await fetch('/api/registrants');
    if (!res.ok) return [];
    const data = await res.json();
    return data.names || [];
  } catch (_) { return []; }
}

/**
 * Start an admin-initiated manual registration. Unlike the self-service
 * endpoint, this bypasses the registrant name validation and works while
 * an admin session is active.
 * @param {string} name - The display name for the new user.
 * @param {string} role - 'user' or 'admin' (from the Register as admin switch).
 * @returns {Promise<Object>} Result with success boolean and optional detail.
 */
async function apiStartAdminRegistration(name, role) {
  if (USE_DEMO) {
    await sleep(400); // simulate API round-trip
    return { success: true };
  }
  try {
    const res = await fetch('/api/admin/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, role }),
    });
    return await res.json();
  } catch (_) { return { success: false, detail: 'Request failed' }; }
}

/* ============================================================
   Enhancement C: CIRCLE REVEAL NAVIGATION
   Track last click position; set CSS custom properties on the
   target screen so the circle expands from the click origin.
============================================================ */
// Capture click origin so the circle reveal expands from where you tap.
// pointerdown fires earliest and works on touch + mouse.
['pointerdown', 'click'].forEach(evt =>
  document.addEventListener(evt, e => {
    S.lastClickX = e.clientX;
    S.lastClickY = e.clientY;
  })
);

/**
 * Get a screen or overlay element by its logical ID. Checks for both
 * 'screen-{id}' and 'overlay-{id}' element IDs.
 * @param {string} id - The logical screen/overlay identifier (e.g., 'idle', 'device-detail').
 * @returns {HTMLElement|null} The matching DOM element, or null if not found.
 */
function getEl(id) {
  return document.getElementById('screen-' + id)
      || document.getElementById('overlay-' + id);
}

/**
 * Set CSS custom properties --reveal-x and --reveal-y on an element so the
 * circle-reveal clip-path animation expands from the last click position.
 * @param {HTMLElement} el - The screen element to set reveal origin on.
 */
function setRevealOrigin(el) {
  if (S.lastClickX != null) {
    const xPct = ((S.lastClickX / window.innerWidth) * 100).toFixed(1) + '%';
    const yPct = ((S.lastClickY / window.innerHeight) * 100).toFixed(1) + '%';
    el.style.setProperty('--reveal-x', xPct);
    el.style.setProperty('--reveal-y', yPct);
  }
}

/** @type {number|undefined} Timeout handle for hiding the device-detail overlay */
let detailHideTimer;

/**
 * Navigate to a different screen or overlay using circle-reveal (screens) or
 * polygon-wipe (overlays) transitions. Handles exit animations on the outgoing
 * screen and entrance animations on the incoming one.
 * @param {string} toId - The logical ID of the target screen or overlay.
 */
function navigate(toId) {
  if (S.screen === toId) return;

  const fromEl = getEl(S.screen);
  const toEl   = getEl(toId);
  if (!toEl) return;

  const toIsOverlay   = toEl.classList.contains('overlay');
  const fromIsOverlay = fromEl && fromEl.classList.contains('overlay');

  if (toIsOverlay) {
    // Overlays use the polygon wipe — open immediately
    toEl.classList.remove('hidden-left', 'hidden-right');
    toEl.style.display = '';
    if (toEl.id === 'overlay-device-detail') clearTimeout(detailHideTimer);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      toEl.classList.add('visible');
      triggerSplitText(toEl);
    }));
  } else {
    // Set the circle origin on the entering screen, then reveal it
    setRevealOrigin(toEl);
    toEl.style.display = '';
    requestAnimationFrame(() => requestAnimationFrame(() => {
      toEl.classList.add('active');
      triggerSplitText(toEl);
    }));

    // Collapse the exiting screen toward the click point.
    // The reflow between setRevealOrigin and adding .exit is critical:
    // it forces the browser to commit the new origin position at 150%
    // (no visual change) BEFORE the radius transition to 0% begins.
    // Without it, both the position and radius animate simultaneously.
    if (fromEl && !fromIsOverlay) {
      setRevealOrigin(fromEl);
      const exitMs = PERF.lite ? 250 : 1200;
      if (!PERF.lite) {
        void fromEl.offsetHeight;          // force reflow — lock in new origin
      }
      fromEl.classList.add('exit');
      setTimeout(() => fromEl.classList.remove('active', 'exit'), exitMs);
    }
  }

  // Close an overlay when navigating back to a regular screen
  if (fromIsOverlay && !toIsOverlay) {
    const isDetail = fromEl.id === 'overlay-device-detail';
    const hideCls  = isDetail ? 'hidden-right' : 'hidden-left';
    fromEl.classList.add(hideCls);
    setTimeout(() => {
      fromEl.classList.remove('visible', hideCls);
      fromEl.style.display = 'none';
    }, 710);
  }

  S.screen = toId;
  reportKioskDisplay(toId);
}

/**
 * Tell the Pi which screen the Riverdi is showing so /dashboard Display can poll.
 * Demo mode does not POST. Failures are ignored so navigation never waits.
 * @param {string} [screenId] - Logical screen id; defaults to S.screen.
 */
function reportKioskDisplay(screenId) {
  if (USE_DEMO) return;
  const screen = screenId || S.screen || 'idle';
  fetch('/api/kiosk/display', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ screen }),
  }).catch(() => {});
}

/* ============================================================
   Enhancement E: SPLIT TEXT ANIMATIONS
   Splits text content into individual characters, each wrapped
   in a span with a stagger delay. Re-splits on content change.
============================================================ */
/**
 * Split an element's text content into individual character spans for staggered
 * entrance animations. Each span gets a CSS custom property --i for delay calculation.
 * @param {HTMLElement} el - The element whose text content will be split into characters.
 */
function splitTextIntoChars(el) {
  const text = el.textContent;
  if (!text.trim()) return;
  el.innerHTML = '';
  el.classList.add('split-text');
  [...text].forEach((ch, i) => {
    const span = document.createElement('span');
    span.className = ch === ' ' ? 'split-char space' : 'split-char';
    span.textContent = ch === ' ' ? '\u00A0' : ch;
    span.style.setProperty('--i', i);
    el.appendChild(span);
  });
}

/**
 * Trigger split-text entrance animations on a screen element. The .active class
 * on the parent screen activates the CSS animation via .screen.active .split-char.
 * @param {HTMLElement} screenEl - The screen element that was just navigated to.
 */
function triggerSplitText(screenEl) {
  // The .active class on the parent screen triggers the CSS animation
  // via .screen.active .split-char selector
}

/**
 * Initialize character-split text on the idle screen elements at page load.
 * Splits the headline ("TAP YOUR CARD") and subtitle into individual character spans.
 */
function initSplitText() {
  // Idle headline: "TAP YOUR CARD"
  const idleHeadline = document.querySelector('.idle-headline .reveal-inner');
  if (idleHeadline) splitTextIntoChars(idleHeadline);

  // Idle sub: "to access the equipment locker"
  const idleSub = document.querySelector('.idle-sub .reveal-inner');
  if (idleSub) splitTextIntoChars(idleSub);
}

/**
 * Split the main menu greeting and username text into individual character spans
 * for entrance animations. Called after dynamic content is set in fillMainMenu().
 */
function splitMenuText() {
  const greeting = document.querySelector('.menu-greeting .reveal-inner');
  if (greeting) splitTextIntoChars(greeting);
  const username = document.getElementById('menu-name');
  if (username) splitTextIntoChars(username);
}

/* ============================================================
   Enhancement F: SLOT-MACHINE NUMBER TRANSITIONS
   Wraps each digit in a container that slides out/in when changed.
============================================================ */
/**
 * Update a numeric display with slot-machine style digit transitions. Each digit
 * slides up when its value changes. Initializes the digit DOM structure on first call.
 * @param {HTMLElement} el - The container element for the slot-machine number display.
 * @param {number} newValue - The new numeric value to display.
 */
function updateSlotNumber(el, newValue) {
  const newStr = String(newValue);
  const oldStr = el.dataset.slotValue || '';
  el.dataset.slotValue = newStr;

  // Initialize if empty
  if (!el.querySelector('.slot-digit')) {
    el.innerHTML = '';
    [...newStr].forEach(ch => {
      const digit = document.createElement('span');
      digit.className = 'slot-digit';
      const inner = document.createElement('span');
      inner.className = 'slot-digit-inner';
      inner.textContent = ch;
      digit.appendChild(inner);
      el.appendChild(digit);
    });
    return;
  }

  // Update each digit with animation
  const digits = el.querySelectorAll('.slot-digit');
  [...newStr].forEach((ch, i) => {
    if (i < digits.length) {
      const inner = digits[i].querySelector('.slot-digit-inner');
      if (inner.textContent !== ch) {
        inner.classList.remove('slide-up');
        void inner.offsetWidth; // force reflow
        inner.classList.add('slide-up');
        setTimeout(() => { inner.textContent = ch; }, 175); // halfway through the slide-up CSS transition
      }
    }
  });
}

/* ============================================================
   CLOCK
============================================================ */
/**
 * Update the clock display with the current time (HH:MM) and date (e.g., "SAT, MAR 28").
 * Called once per second via setInterval.
 */
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
   INACTIVITY TIMER — with slot-machine countdown
============================================================ */
/**
 * Arm (or re-arm) the inactivity timer. After (cdSeconds - cdWarnAt) seconds of
 * no user interaction, the inactivity countdown overlay will appear. Resets on
 * every click, touch, or keydown event.
 */
function armIdle() {
  clearTimeout(S.idleTimer);
  if (S.screen === 'idle') return;
  if (S.updating) return;
  S.idleTimer = setTimeout(showInactivity, (S.cdSeconds - S.cdWarnAt) * 1000);
}

/**
 * Display the inactivity countdown overlay with a slot-machine countdown timer.
 * When the countdown reaches zero, the session ends automatically.
 */
function showInactivity() {
  if (S.updating) return;
  S.prevScreen = S.screen;
  const overlay = document.getElementById('overlay-inactivity');
  overlay.style.display = '';
  let secs = S.cdWarnAt;
  const numEl = document.getElementById('inactivity-num');
  updateSlotNumber(numEl, secs);
  requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add('visible')));
  S.cdTimer = setInterval(() => {
    secs--;
    updateSlotNumber(numEl, secs);
    if (secs <= 0) { clearInterval(S.cdTimer); endSession(true); }
  }, 1000);
}

/**
 * Dismiss the inactivity countdown overlay and re-arm the idle timer.
 * Called when the user clicks "Stay" or interacts during the countdown.
 */
function dismissInactivity() {
  clearInterval(S.cdTimer);
  const overlay = document.getElementById('overlay-inactivity');
  if (!overlay.classList.contains('visible')) return;
  overlay.classList.add('hidden-left');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-left');
    overlay.style.display = 'none';
  }, 710);
  if (S.prevScreen && S.screen !== S.prevScreen) S.screen = S.prevScreen;
  armIdle();
}

/** True while a session-touch POST is in flight. */
let sessionTouchInFlight = false;
/** Debounce timer for session-touch (coalesces click+touchstart). */
let sessionTouchTimer = 0;

/**
 * Arm idle locally; ping the server at most once per second.
 */
function onUserActivity() {
  if (S.screen === 'idle') return;
  armIdle();
  if (USE_DEMO || !S.user || sessionTouchInFlight) return;
  if (sessionTouchTimer) return;
  sessionTouchTimer = setTimeout(() => {
    sessionTouchTimer = 0;
    if (!S.user || S.screen === 'idle' || sessionTouchInFlight) return;
    sessionTouchInFlight = true;
    fetch('/api/session/touch', { method: 'POST' })
      .catch(() => {})
      .finally(() => { sessionTouchInFlight = false; });
  }, 1000);
}

document.addEventListener('pointerdown', onUserActivity);
document.addEventListener('keydown', onUserActivity);

/* ============================================================
   AUTH
============================================================ */
/**
 * Handle an NFC card tap event in demo mode. Authenticates the user and navigates
 * to the main menu on success, or shows the auth-failed screen on failure.
 * @returns {Promise<void>}
 */
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

/**
 * Show the authentication failed screen with an animated progress bar that
 * counts down over 3 seconds before automatically returning to the idle screen.
 */
function showAuthFailed() {
  navigate('auth-failed');
  const bar = document.getElementById('auth-progress');
  bar.style.transition = 'none';
  bar.style.width = '100%';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    bar.style.transition = 'width 3s linear';
    bar.style.width = '0%';
  }));
  setTimeout(() => navigate('idle'), 3100);
}

/**
 * End the current user session, clear all state, dismiss overlays, and return
 * to the idle screen. Notifies the backend unless the end was triggered by SSE.
 * @param {boolean} [fromTimeout=false] - True if the session ended due to inactivity timeout.
 * @param {boolean} [fromSSE=false] - True if the session end was triggered by an SSE event
 *   (skip backend call to avoid circular notification).
 * @returns {Promise<void>}
 */
async function endSession(fromTimeout = false, fromSSE = false) {
  clearTimeout(S.idleTimer);
  clearTimeout(sessionEndedFallbackTimer);
  sessionEndedFallbackTimer = null;
  clearInterval(S.cdTimer);
  dismissInactivity();
  dismissSlotOverlay();
  if (!fromSSE) await apiEndSession();
  S.user     = null;
  S.devices  = [];
  S.selected = null;
  S.adminRegistration = false;
  adminSessionActive = false;
  closeAdminPanel();
  hideRegisterDeviceOverlay();
  hideUsersOverlay();
  navigate('idle');
}

/* ============================================================
   MAIN MENU
============================================================ */
/**
 * Populate the main menu screen with the authenticated user's information,
 * including greeting, avatar initials, name badge, and role pill.
 * @param {Object} user - The authenticated user object.
 * @param {number} user.id - User database ID.
 * @param {string} user.name - User's full display name.
 * @param {string} user.role - User role ('admin' or 'user').
 */
function fillMainMenu(user) {
  const first = user.name.split(' ')[0].toUpperCase();
  document.getElementById('menu-name').textContent    = first;
  document.getElementById('user-avatar').textContent  =
    user.name.split(' ').map(n => n[0]).join('').toUpperCase();
  document.getElementById('badge-name').textContent   = user.name;
  const pill = document.getElementById('badge-role');
  pill.textContent = user.role === 'admin' ? 'Admin' : 'User';
  pill.className   = 'role-pill' + (user.role === 'admin' ? ' admin' : '');
  // Enhancement E: re-split the dynamic text
  splitMenuText();
  updateMenuBorrowCount();
}

/**
 * Refresh the optional N/5 borrowed line on the scan-first main menu.
 * @returns {Promise<void>}
 */
async function updateMenuBorrowCount() {
  const el = document.getElementById('menu-borrow-count');
  if (!el) return;
  try {
    const devices = await apiGetDevices();
    S.devices = devices;
    setMenuBorrowCount(devices);
  } catch (_) { /* leave the last count */ }
}

/**
 * Write the main-menu "N / 5 borrowed" line from an already-fetched list.
 * @param {Array<Object>} devices - Device list from the API.
 */
function setMenuBorrowCount(devices) {
  const el = document.getElementById('menu-borrow-count');
  if (!el) return;
  const n = devices.filter(d => d.borrower_name === 'You').length;
  el.textContent = `${n} / 5 borrowed`;
}

/**
 * After a sticker auto-intent, refresh an open grid or detail overlay so the
 * UI matches locker state. Successful returns show the slot overlay instead
 * of a toast.
 * @param {Object} data - SSE device_action payload.
 * @returns {Promise<void>}
 */
async function refreshAfterDeviceAction(data) {
  await updateMenuBorrowCount();
  const devices = S.devices || [];
  const detailOpen = S.screen === 'device-detail';
  const gridScreen = detailOpen ? S.prevScreen : S.screen;
  if (gridScreen === 'borrow') {
    setLockerBadge(devices);
    buildGrid('borrow-grid', devices, 'borrow');
  } else if (gridScreen === 'return') {
    const mine = devices.filter(d => d.borrower_name === 'You').length;
    document.getElementById('return-badge').textContent =
      `${mine} item${mine !== 1 ? 's' : ''} to return`;
    buildGrid('return-grid', devices, 'return');
  }
  if (detailOpen && S.selected) {
    const updated = devices.find(d => d.id === S.selected.id);
    if (updated) openDetail(updated, S.mode);
  }
}

/**
 * Re-arm the kiosk inactivity UI after a tag tap (backend touch() is not enough).
 */
function keepSessionAliveFromTag() {
  dismissInactivity();
  armIdle();
  if (!USE_DEMO && S.user) {
    fetch('/api/session/touch', { method: 'POST' }).catch(() => {});
  }
}

/* ============================================================
   DEVICE GRID — locker availability overlay and return screen
============================================================ */
/**
 * Count locker devices that are in the cabinet vs borrowed out.
 * Maintenance rows are neither.
 * @param {Array<Object>} devices - Device list from the API.
 * @returns {{inCount: number, outCount: number}}
 */
function lockerInOutCounts(devices) {
  let inCount = 0;
  let outCount = 0;
  for (const d of devices) {
    if (d.status === 'available') inCount += 1;
    else if (d.status === 'borrowed') outCount += 1;
  }
  return { inCount, outCount };
}

/**
 * Set the locker overlay badge to "N in · M out".
 * @param {Array<Object>} devices - Device list from the API.
 */
function setLockerBadge(devices) {
  const { inCount, outCount } = lockerInOutCounts(devices);
  document.getElementById('borrow-badge').textContent =
    `${inCount} in · ${outCount} out`;
}

/**
 * Open the locker availability overlay, fetch devices, show in/out counts,
 * and build the card grid. Screen-pick borrow remains available on a card.
 * @returns {Promise<void>}
 */
async function openBorrow() {
  navigate('borrow');
  const devices = await apiGetDevices();
  S.devices = devices;
  setLockerBadge(devices);
  setMenuBorrowCount(devices);
  buildGrid('borrow-grid', devices, 'borrow');
}

/**
 * Open the return screen, fetch the current device list from the API, update
 * the return count badge, and build the device card grid.
 * @returns {Promise<void>}
 */
async function openReturn() {
  navigate('return');
  const devices = await apiGetDevices();
  S.devices = devices;
  const mine = devices.filter(d => d.borrower_name === 'You').length;
  document.getElementById('return-badge').textContent =
    `${mine} item${mine !== 1 ? 's' : ''} to return`;
  setMenuBorrowCount(devices);
  buildGrid('return-grid', devices, 'return');
}

/**
 * Allow only local photo paths under images/. Catalog names never become
 * javascript: URLs or HTML.
 * @param {string|null|undefined} raw
 * @returns {string}
 */
function safeKioskImagePath(raw) {
  if (!raw || typeof raw !== 'string') return '';
  const path = raw.trim().replace(/\\/g, '/');
  if (!path || path.includes('..') || path.includes(':') || path.startsWith('//')) {
    return '';
  }
  const bare = path.replace(/^\/+/, '');
  if (!bare.startsWith('images/')) return '';
  return path;
}

/**
 * Build one locker card with textContent / setAttribute (no catalog HTML).
 * @param {Object} dev
 * @param {string} cls
 * @param {string} statusCls
 * @param {string} statusTxt
 * @param {string} slotLabel
 * @returns {HTMLElement}
 */
function buildDeviceCardEl(dev, cls, statusCls, statusTxt, slotLabel) {
  const card = document.createElement('div');
  card.className = cls;

  const cardImage = document.createElement('div');
  cardImage.className = 'card-image';
  const imgPath = safeKioskImagePath(dev.image_path);

  if (imgPath) {
    const img = document.createElement('img');
    img.src = imgPath;
    img.alt = dev.name || '';
    img.loading = 'lazy';
    cardImage.appendChild(img);

    const reveal = document.createElement('div');
    reveal.className = 'card-hover-reveal';
    const revealImg = document.createElement('div');
    revealImg.className = 'card-hover-reveal-img';
    revealImg.style.backgroundImage = `url("${imgPath.replace(/"/g, '\\"')}")`;
    const revealIcon = document.createElement('div');
    revealIcon.className = 'card-hover-icon';
    revealIcon.innerHTML =
      '<svg viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>';
    reveal.appendChild(revealImg);
    reveal.appendChild(revealIcon);
    cardImage.appendChild(reveal);
  } else {
    const ph = document.createElement('div');
    ph.className = 'card-img-placeholder';
    ph.innerHTML =
      '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>';
    const slotSpan = document.createElement('span');
    slotSpan.textContent = slotLabel;
    ph.appendChild(slotSpan);
    cardImage.appendChild(ph);
  }

  const slotEl = document.createElement('div');
  slotEl.className = 'card-slot';
  slotEl.textContent = slotLabel;
  const statusEl = document.createElement('div');
  statusEl.className = `card-status ${statusCls}`;
  statusEl.textContent = statusTxt;
  cardImage.appendChild(slotEl);
  cardImage.appendChild(statusEl);

  const body = document.createElement('div');
  body.className = 'card-body';
  const nameEl = document.createElement('div');
  nameEl.className = 'card-name';
  nameEl.textContent = dev.name || '';
  body.appendChild(nameEl);
  if (dev.pm_number) {
    const pmEl = document.createElement('div');
    pmEl.className = 'card-pm';
    pmEl.textContent = dev.pm_number;
    body.appendChild(pmEl);
  }
  const typeEl = document.createElement('div');
  typeEl.className = 'card-type';
  typeEl.textContent = dev.device_type || '';
  body.appendChild(typeEl);

  card.appendChild(cardImage);
  card.appendChild(body);
  return card;
}

/**
 * Build the device card grid for either borrow or return mode. Creates card DOM
 * elements sorted by locker slot, sets up IntersectionObserver for scroll-triggered
 * entrance animations, scroll parallax on card images, and mouse hover parallax.
 * @param {string} gridId - The DOM ID of the grid container element.
 * @param {Array<Object>} devices - Array of device objects from the API.
 * @param {string} mode - Either 'borrow' or 'return', controls card styling and behavior.
 */
function buildGrid(gridId, devices, mode) {
  const grid   = document.getElementById(gridId);
  grid.innerHTML = '';

  // Disconnect previous observer if any
  if (grid._scrollObs) { grid._scrollObs.disconnect(); grid._scrollObs = null; }

  // Sort by locker slot number; 99 is a fallback for devices without a slot assignment
  const sorted = [...devices].sort((a, b) => (a.locker_slot || 99) - (b.locker_slot || 99));

  const scrollRoot = grid.closest('.grid-wrapper');
  let cardObserver = null;
  if (!PERF.lite) {
    cardObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          entry.target.classList.remove('out-up');
        } else {
          // Card scrolled out of view — determine direction
          if (entry.boundingClientRect.top < entry.rootBounds.top) {
            entry.target.classList.add('out-up');
            entry.target.classList.remove('in');
          } else {
            entry.target.classList.remove('in', 'out-up');
          }
        }
      });
    }, { root: scrollRoot, threshold: 0.15, rootMargin: '40px 0px' });
    grid._scrollObs = cardObserver;
  }

  // Scroll/mouse parallax on card images is pointer-driven eye-candy that
  // forces a getBoundingClientRect() per card per frame. Skip it entirely on
  // the touch kiosk (no pointer) and in lite mode (too costly) — the cards
  // still get click handling below. Lite skips the observer too.
  const fancy = canHover && !PERF.lite;

  // Scroll parallax: shift card images based on scroll position
  const parallaxCards = [];
  /**
   * Recalculate scroll-based parallax offsets for all card images in the grid.
   * Shifts each image vertically based on its distance from the viewport center.
   */
  function onGridScroll() {
    const wrapRect = scrollRoot.getBoundingClientRect();
    const centerY = wrapRect.top + wrapRect.height / 2;
    for (const { card, img } of parallaxCards) {
      const cardRect = card.getBoundingClientRect();
      const cardCenterY = cardRect.top + cardRect.height / 2;
      // Normalized offset: -1 (top) to +1 (bottom) relative to viewport center
      const offset = (cardCenterY - centerY) / (wrapRect.height / 2);
      const yShift = offset * -14; // max ±14px vertical shift
      img.style.transform = `scale(1.12) translateY(${yShift}px)`;
    }
    grid._rafId = null;
  }
  if (fancy) {
    scrollRoot.addEventListener('scroll', () => {
      if (!grid._rafId) grid._rafId = requestAnimationFrame(onGridScroll);
    }, { passive: true });
  }

  sorted.forEach((dev, i) => {
    const avail = dev.status === 'available';
    const mine  = dev.borrower_name === 'You';
    const maint = dev.status === 'maintenance';

    let cls = 'device-card';
    if (mode === 'borrow') cls += avail ? ' available' : ' unavailable';
    else                   cls += mine  ? ' mine'      : ' unavailable';

    let statusCls, statusTxt;
    if      (mine)  { statusCls = 'mine-tag';  statusTxt = 'YOURS'; }
    else if (avail) { statusCls = 'available'; statusTxt = 'IN';    }
    else if (maint) { statusCls = 'maint';     statusTxt = 'MAINT'; }
    else            { statusCls = 'in-use';    statusTxt = 'OUT';   }

    const slotLabel = `S${String(dev.locker_slot ?? 0).padStart(2, '0')}`;
    const card = buildDeviceCardEl(dev, cls, statusCls, statusTxt, slotLabel);

    if (cardObserver) cardObserver.observe(card);
    else card.classList.add('in');

    // Track for scroll parallax + mouse parallax on hover (pointer-only eye-candy)
    const cardImg = card.querySelector('.card-image img');
    if (fancy && cardImg) {
      parallaxCards.push({ card, img: cardImg });
      card.addEventListener('mousemove', e => {
        if (PERF.lite) return;           // runtime downgrade — stop applying transforms
        const rect = card.getBoundingClientRect();
        const x = (e.clientX - rect.left) / rect.width - 0.5;
        const y = (e.clientY - rect.top) / rect.height - 0.5;
        cardImg.style.transform = `scale(1.15) translate(${x * -14}px, ${y * -14}px)`; // ±14px parallax shift opposite to cursor
      });
      card.addEventListener('mouseleave', () => {
        // Restore to scroll-parallax transform
        cardImg.style.transform = '';
        if (!grid._rafId) grid._rafId = requestAnimationFrame(onGridScroll);
      });
    }

    card.addEventListener('click', () => { clickSound(); openDetail(dev, mode); });
    grid.appendChild(card);
  });

  // Trigger initial parallax positioning (full mode only)
  if (fancy) requestAnimationFrame(onGridScroll);
}

/* ============================================================
   DEVICE DETAIL OVERLAY
============================================================ */
/**
 * Open the device detail overlay with full information about a device. Populates
 * all detail fields (name, PM, type, serial, image, status, description) and configures
 * the confirm button based on device availability and current mode.
 * @param {Object} dev - The device object to display details for.
 * @param {string} mode - Either 'borrow' or 'return', determines confirm button behavior.
 */
function openDetail(dev, mode) {
  S.selected   = dev;
  S.mode       = mode;
  // Re-opening while detail is already up must keep the grid id. Otherwise a
  // sticker refresh sets prevScreen to device-detail, later taps skip the grid,
  // and closeDetail restores a hidden overlay as the current screen.
  if (S.screen !== 'device-detail') {
    S.prevScreen = S.screen;
  }

  const avail = dev.status === 'available';
  const mine  = dev.borrower_name === 'You';
  const maint = dev.status === 'maintenance';
  const slot  = `S${String(dev.locker_slot ?? 0).padStart(2, '0')}`;

  document.getElementById('detail-slot-tag').textContent =
    `SLOT ${String(dev.locker_slot ?? 0).padStart(2, '0')}`;
  document.getElementById('detail-name').textContent    = dev.name;
  document.getElementById('detail-pm').textContent      = dev.pm_number || '—';
  document.getElementById('detail-type').textContent    = dev.device_type;
  document.getElementById('detail-serial').textContent  = dev.serial_number;
  document.getElementById('detail-img-slot').textContent = slot;
  const statusEl = document.getElementById('detail-status');
  statusEl.textContent =
    maint ? 'Under Maintenance' : avail ? 'Available' : mine ? 'Borrowed by You' : 'In Use';
  // Color-code the status: cyan for yours, green for available, amber for maintenance
  statusEl.style.color =
    mine ? 'var(--info)' : avail ? 'var(--success)' : maint ? 'var(--warning)' : 'var(--text-muted)';
  document.getElementById('detail-desc').textContent    =
    dev.description || 'No description available.';

  const imgPath     = safeKioskImagePath(dev.image_path);
  const imgPane     = document.getElementById('detail-img-pane');
  const placeholder = document.getElementById('detail-img-placeholder');
  const existingImg = imgPane.querySelector('img');
  if (existingImg) existingImg.remove();
  if (imgPath) {
    placeholder.classList.add('hidden');
    const img = document.createElement('img');
    img.src = imgPath;
    img.alt = dev.name || '';
    imgPane.appendChild(img);
  } else {
    placeholder.classList.remove('hidden');
  }

  const borrowerRow = document.getElementById('detail-borrower-row');
  if (dev.borrower_name && !mine) {
    document.getElementById('detail-borrower-name').textContent = dev.borrower_name;
    borrowerRow.classList.remove('hidden');
  } else {
    borrowerRow.classList.add('hidden');
  }

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

/**
 * Close the device detail overlay with a slide-right exit animation and restore
 * the previous screen (borrow or return grid). Lite fades with opacity only, so
 * hidden-right (and a shorter hide) must drop hit-testing before display:none.
 */
function closeDetail() {
  const overlay = document.getElementById('overlay-device-detail');
  clearTimeout(detailHideTimer);
  overlay.classList.add('hidden-right');
  const hideMs = PERF.lite ? 220 : 710;
  detailHideTimer = setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-right');
    overlay.style.display = 'none';
    detailHideTimer = undefined;
  }, hideMs);
  S.screen = S.prevScreen || (S.mode === 'return' ? 'return' : 'borrow');
  reportKioskDisplay(S.screen);
}

/**
 * Execute the borrow or return action for the currently selected device. Disables
 * the button during the request. Successful returns show the slot overlay;
 * other outcomes show a toast. Then refreshes the grid.
 * @returns {Promise<void>}
 */
async function confirmAction() {
  const btn = document.getElementById('confirm-btn');
  if (btn.classList.contains('disabled')) return;

  btn.textContent = '\u2026';
  btn.classList.add('disabled');

  const dev = S.selected;
  const result = S.mode === 'borrow'
    ? await apiBorrow(dev.id)
    : await apiReturn(dev.id);

  const wasReturn = S.mode === 'return';
  closeDetail();
  if (wasReturn && result.success) {
    showSlotOverlay(dev.name, dev.locker_slot);
  } else {
    showToast(result.message, result.success ? 'success' : 'error');
  }

  if (S.mode === 'borrow') openBorrow();
  else                     openReturn();
}

/* ============================================================
   TOAST NOTIFICATION
============================================================ */
/** @type {number|undefined} Timeout handle for auto-hiding the active toast */
let toastTimer;
/**
 * Display a temporary toast notification message at the bottom of the screen.
 * Automatically hides after 3.2 seconds. Consecutive calls reset the timer.
 * @param {string} msg - The message text to display.
 * @param {string} [type=''] - Optional CSS modifier class ('success' or 'error').
 */
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  clearTimeout(toastTimer);
  el.textContent = msg;
  el.className = `show${type ? ' toast-' + type : ''}`;
  toastTimer = setTimeout(() => { el.className = ''; }, 3200); // 3.2s display duration
}

/* ============================================================
   RETURN SLOT OVERLAY — put the device in its locker slot
============================================================ */
/** @type {number|undefined} Timeout handle for auto-hiding the slot overlay */
let slotOverlayTimer;
/** @type {number|undefined} Timeout handle for the hide animation */
let slotHideTimer;

/**
 * Show a full-screen overlay with the device name and locker slot after a
 * successful return (idle tap, main-menu sticker, or Return screen confirm).
 * @param {string} name - Device display name.
 * @param {number|null|undefined} slot - Physical locker slot, if assigned.
 */
function showSlotOverlay(name, slot) {
  const overlay = document.getElementById('overlay-slot');
  if (!overlay) return;
  document.getElementById('slot-return-name').textContent = name || 'Device';
  const putEl = document.getElementById('slot-return-put');
  const numEl = document.getElementById('slot-return-num');
  if (slot != null) {
    putEl.textContent = 'Put in slot';
    putEl.style.display = '';
    numEl.textContent = String(slot);
    numEl.style.display = '';
  } else {
    putEl.style.display = 'none';
    numEl.style.display = 'none';
  }
  clearTimeout(slotOverlayTimer);
  clearTimeout(slotHideTimer);
  overlay.classList.remove('hidden-left');
  overlay.style.display = '';
  requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add('visible')));
  slotOverlayTimer = setTimeout(dismissSlotOverlay, 8000);
}

/**
 * Hide the return-slot overlay. Safe to call when it is already hidden.
 */
function dismissSlotOverlay() {
  clearTimeout(slotOverlayTimer);
  clearTimeout(slotHideTimer);
  const overlay = document.getElementById('overlay-slot');
  if (!overlay || overlay.style.display === 'none') return;
  if (!overlay.classList.contains('visible')) {
    overlay.style.display = 'none';
    return;
  }
  overlay.classList.add('hidden-left');
  slotHideTimer = setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-left');
    overlay.style.display = 'none';
  }, 710);
}

/* ============================================================
   HANDOVER OVERLAY — take over a device held by another user
============================================================ */
let handoverData = null;
/** @type {number|undefined} Timeout handle for the handover overlay hide animation */
let handoverHideTimer;

/**
 * Open the handover confirmation overlay with details from the SSE event.
 * @param {Object} data - SSE handover_requested payload.
 */
function openHandover(data) {
  if (S.updating) return;
  handoverData = data;
  S.handoverDeviceId = data.device_id;
  const msgEl = document.getElementById('handover-msg');
  if (msgEl) {
    msgEl.textContent = data.device_name + ' is currently assigned to ' + data.current_holder_name + '. Transfer responsibility to ' + data.user_name + '?';
  }
  // Remember where the handover was opened from, but do not overwrite
  // S.prevScreen — that already tracks the grid behind device-detail.
  if (S.screen !== 'handover') {
    S.handoverFromScreen = S.screen;
  }
  clearTimeout(handoverHideTimer);
  handoverHideTimer = undefined;
  if (S.screen === 'handover') {
    const overlay = document.getElementById('overlay-handover');
    if (overlay) {
      overlay.classList.remove('hidden-left', 'hidden-right');
      overlay.style.display = '';
      overlay.classList.add('visible');
    }
  }
  navigate('handover');
}

/**
 * Close the handover overlay and return to the previous screen.
 */
function closeHandover() {
  clearTimeout(handoverHideTimer);
  handoverHideTimer = undefined;
  const overlay = document.getElementById('overlay-handover');
  if (overlay && overlay.style.display !== 'none') {
    if (overlay.classList.contains('visible')) {
      overlay.classList.add('hidden-left');
      handoverHideTimer = setTimeout(() => {
        overlay.classList.remove('visible', 'hidden-left');
        overlay.style.display = 'none';
        handoverHideTimer = undefined;
      }, 710);
    } else {
      overlay.style.display = 'none';
    }
  }
  handoverData = null;
  S.handoverDeviceId = null;
  S.screen = S.handoverFromScreen || 'main-menu';
  S.handoverFromScreen = null;
  reportKioskDisplay(S.screen);
}

/**
 * Cancel a pending handover and dismiss the overlay.
 */
function cancelHandover() {
  closeHandover();
}

/**
 * Accept the handover by POSTing to the transfer endpoint.
 */
async function acceptHandover() {
  const deviceId = S.handoverDeviceId;
  const from = S.handoverFromScreen;
  if (deviceId == null || !from) return;
  const result = await apiTransfer(deviceId);
  if (result && result.success) {
    showToast(result.message, 'success');
    closeHandover();
    if (from === 'borrow') {
      await openBorrow();
    } else if (from === 'return') {
      await openReturn();
    } else if (from === 'device-detail') {
      const devices = await apiGetDevices();
      if (!devices.length) {
        // Failed or empty refresh: keep the last known list and drop the stale detail.
        closeDetail();
        showToast('Could not refresh the device list.', 'error');
      } else {
        S.devices = devices;
        setMenuBorrowCount(devices);
        const gridMode = S.prevScreen === 'return' ? 'return' : 'borrow';
        buildGrid(gridMode + '-grid', devices, gridMode);
        const updated = S.selected ? devices.find(d => d.id === S.selected.id) : null;
        if (updated) openDetail(updated, S.mode || 'borrow');
        else closeDetail();
      }
    } else {
      await updateMenuBorrowCount();
    }
  } else {
    showToast((result && result.message) || 'Could not transfer device.', 'error');
  }
}

/* ============================================================
   CUSTOM CURSOR — Enhancement D: glow on interactive elements
============================================================ */
const cursorDot  = document.getElementById('cursor');
const cursorRing = document.getElementById('cursor-ring');
let mx = 0, my = 0, rx = 0, ry = 0;
let ringRunning = false;

/**
 * Animate the cursor ring to follow the cursor dot with an eased lag.
 * Self-suspending: the rAF loop stops once the ring has caught up to the dot
 * (instead of running at 60fps forever) and is restarted on the next
 * mousemove. Bails permanently if lite mode is enabled at runtime.
 */
function animRing() {
  if (PERF.lite) { ringRunning = false; return; } // runtime downgrade — stop
  rx += (mx - rx) * 0.13; // 0.13 = easing factor — lower values increase lag
  ry += (my - ry) * 0.13;
  cursorRing.style.left = rx + 'px';
  cursorRing.style.top  = ry + 'px';
  // Keep going only until the ring has visually settled on the dot.
  if (Math.abs(mx - rx) > 0.5 || Math.abs(my - ry) > 0.5) {
    requestAnimationFrame(animRing);
  } else {
    ringRunning = false;
  }
}

/**
 * Wire up the custom cursor (dot + trailing ring + hover glow). Only invoked
 * on hover-capable, non-lite devices — the touch kiosk and lite mode use the
 * native/no cursor, so none of this work runs there.
 */
function initCursor() {
  document.addEventListener('mousemove', e => {
    mx = e.clientX; my = e.clientY;
    cursorDot.style.left = mx + 'px';
    cursorDot.style.top  = my + 'px';

    // Detect hovering over interactive elements
    const target = e.target.closest('button, .action-btn, .device-card, .confirm-btn, .stay-btn, a');
    if (target) {
      cursorDot.classList.add('hovering');
      cursorRing.classList.add('hovering');
    } else {
      cursorDot.classList.remove('hovering');
      cursorRing.classList.remove('hovering');
    }

    // Resume the eased ring follow if it had settled/suspended.
    if (!ringRunning && !PERF.lite) { ringRunning = true; requestAnimationFrame(animRing); }
  });
}

if (canHover && !PERF.lite) initCursor();

/* ============================================================
   Enhancement D: MAGNETIC HOVER (back / close / stay / confirm)
   Main-menu Locker / Return / End Session tiles are CSS-only hover.
============================================================ */
/**
 * Initialize magnetic hover on back, close, stay, and confirm buttons.
 * Main-menu `.action-btn` tiles (Locker / Return / End Session) stay CSS-only:
 * per-mousemove getBoundingClientRect plus a 0.4s transform transition made
 * hover feel late on a mouse.
 */
function initMagneticHover() {
  document.querySelectorAll('.back-btn, .detail-close, .stay-btn, .confirm-btn').forEach(btn => {
    btn.addEventListener('mousemove', e => {
      if (PERF.lite) return;             // runtime downgrade — stop applying transforms
      const rect = btn.getBoundingClientRect();
      const x = e.clientX - rect.left - rect.width / 2;
      const y = e.clientY - rect.top - rect.height / 2;
      btn.style.transform = `translate(${x * 0.18}px, ${y * 0.18}px)`; // 0.18 = stronger magnetic effect for standalone buttons
    });
    btn.addEventListener('mouseleave', () => {
      btn.style.transform = '';
    });
  });
}

/* ============================================================
   SEAMLESS MARQUEE — clone track to fill any viewport width
============================================================ */
/**
 * Initialize the seamless infinite marquee by cloning the track element enough
 * times to fill the viewport width plus one extra copy for seamless looping.
 * Re-populates on window resize.
 */
function initMarquee() {
  const bar = document.querySelector('.marquee-bar');
  if (!bar) return;
  const original = bar.querySelector('.marquee-track');
  if (!original) return;

  /**
   * Calculate and create the necessary number of track clones to ensure seamless
   * scrolling across the current viewport width. Removes previous clones first.
   */
  function populate() {
    // Remove previous clones
    bar.querySelectorAll('.marquee-track[aria-hidden]').forEach(c => c.remove());
    // Measure one copy vs the container
    const trackW = original.offsetWidth;
    const barW   = bar.offsetWidth;
    if (!trackW) return;
    // Need enough copies so content >= barW + trackW (one scrolling out + rest filling)
    const copies = Math.ceil(barW / trackW) + 1;
    for (let i = 0; i < copies; i++) {
      const clone = original.cloneNode(true);
      clone.setAttribute('aria-hidden', 'true');
      bar.appendChild(clone);
    }
  }

  populate();
  window.addEventListener('resize', populate);
}

/* ============================================================
   REGISTRATION FLOW
============================================================ */
/** @type {number|null} Interval handle for the registration countdown timer (60s) */
let registerCountdownTimer = null;
/** @type {number|null} Timeout that returns to idle after register success/fail. */
let afterRegisterTimer = null;
/** @type {number|null} Timeout that simulates the NFC tap in ?demo registration. */
let demoRegisterTapTimer = null;

/**
 * Cancel the delayed return-to-idle after registration.
 */
function clearAfterRegisterTimer() {
  if (afterRegisterTimer !== null) {
    clearTimeout(afterRegisterTimer);
    afterRegisterTimer = null;
  }
}

/**
 * Schedule return-to-idle after a registration outcome. Replaces any prior wait.
 * @param {number} ms - Delay in milliseconds.
 */
function scheduleAfterRegistration(ms) {
  clearAfterRegisterTimer();
  afterRegisterTimer = setTimeout(() => {
    afterRegisterTimer = null;
    navigateAfterRegistration();
  }, ms);
}

/**
 * Show a specific step in the registration flow, hiding all other steps.
 * Re-triggers the CSS entrance animation on the revealed step.
 * @param {string} stepId - The DOM ID of the registration step element to show.
 */
function showRegisterStep(stepId) {
  document.querySelectorAll('.register-step').forEach(el => el.classList.add('hidden'));
  const step = document.getElementById(stepId);
  if (step) {
    step.classList.remove('hidden');
    // Re-trigger entrance animation
    step.style.animation = 'none';
    void step.offsetHeight;
    step.style.animation = '';
  }
}

/**
 * Open the registration screen. In self-service mode (default), fetches the
 * approved name list from the backend and renders it as a searchable,
 * scrollable list. In admin mode (S.adminRegistration === true), shows a
 * free-text name input for manual registration of any name.
 * @returns {Promise<void>}
 */
async function openRegister() {
  selectedRegistrantName = null;
  clearInterval(registerCountdownTimer);
  clearTimeout(demoRegisterTapTimer);
  demoRegisterTapTimer = null;
  clearAfterRegisterTimer();

  if (S.adminRegistration) {
    // Admin manual registration — show free-text input
    const input = document.getElementById('register-name-admin');
    input.value = '';
    document.getElementById('register-admin-role').checked = false;
    document.getElementById('register-next-btn-admin').disabled = true;
    showRegisterStep('register-step-name-admin');
    navigate('register');
    setTimeout(() => input.focus(), 800);
  } else {
    // Self-service — fetch approved names and show searchable list
    document.getElementById('register-search').value = '';
    document.getElementById('register-next-btn').disabled = true;
    showRegisterStep('register-step-name');
    navigate('register');
    const names = await apiGetRegistrants();
    populateNameList(names);
    setTimeout(() => document.getElementById('register-search').focus(), 800);
  }
}

/**
 * Populate the registrant name list with selectable name items. Each name
 * becomes a button that the user can click to select it for registration.
 * Entrance stagger uses CSS --i / name-item-in (not transitionDelay), so
 * hover is not lagged after the list appears.
 * @param {string[]} names - Array of approved registrant names to display.
 */
function populateNameList(names) {
  const list = document.getElementById('register-name-list');
  const noResults = document.getElementById('register-no-results');
  list.innerHTML = '';

  if (names.length === 0) {
    noResults.style.display = '';
    noResults.textContent = 'No names available. Contact an admin for registration.';
    return;
  }
  noResults.style.display = 'none';

  names.forEach((name, i) => {
    const btn = document.createElement('button');
    btn.className = 'name-item';
    btn.type = 'button';
    btn.dataset.name = name;
    btn.textContent = name;
    // Cap stagger at 0.6s (20 × 0.03s), matching the previous entrance cap
    btn.style.setProperty('--i', String(Math.min(i, 20)));
    btn.addEventListener('click', () => { clickSound(); selectRegistrantName(name, btn); });
    list.appendChild(btn);
  });
}

/**
 * Mark a registrant name as selected. Highlights the clicked item, deselects
 * any previously selected item, and enables the Continue button.
 * @param {string} name - The selected person's name.
 * @param {HTMLElement} btn - The clicked name-item button element.
 */
function selectRegistrantName(name, btn) {
  selectedRegistrantName = name;
  // Remove selection from all items and highlight the clicked one
  document.querySelectorAll('.name-item.selected').forEach(el => el.classList.remove('selected'));
  btn.classList.add('selected');
  // Enable the continue button now that a name is selected
  document.getElementById('register-next-btn').disabled = false;
}

/**
 * Submit the registration name and advance to the "tap your card" step. In
 * self-service mode, uses the selected name from the list and calls the
 * standard registration endpoint. In admin mode, reads the free-text input
 * and calls the admin registration endpoint. Starts a 60-second countdown.
 * On timeout or backend error, shows the error step and navigates back.
 * @returns {Promise<void>}
 */
async function submitRegistrationName() {
  let name;
  let endpoint;
  let role;

  if (S.adminRegistration) {
    // Admin manual registration — get name from text input
    name = document.getElementById('register-name-admin').value.trim();
    if (!name) return;
    role = document.getElementById('register-admin-role').checked ? 'admin' : 'user';
    endpoint = apiStartAdminRegistration;
  } else {
    // Self-service — get the name selected from the list
    name = selectedRegistrantName;
    if (!name) return;
    endpoint = apiStartRegistration;
  }

  // Disable the appropriate continue button to prevent double-submit
  const btnId = S.adminRegistration ? 'register-next-btn-admin' : 'register-next-btn';
  document.getElementById(btnId).disabled = true;

  document.getElementById('register-confirm-name').textContent = name;
  showRegisterStep('register-step-tap');

  // Start 60-second countdown timer (must match REGISTRATION_TIMEOUT_SECONDS)
  let secs = 60;
  const cdEl = document.getElementById('register-countdown');
  cdEl.textContent = secs + 's';
  registerCountdownTimer = setInterval(() => {
    secs--;
    cdEl.textContent = secs + 's';
    if (secs <= 0) {
      clearInterval(registerCountdownTimer);
      showRegisterStep('register-step-error');
      document.getElementById('register-error-msg').textContent =
        'Registration timed out. Please try again.';
      scheduleAfterRegistration(3500);
    }
  }, 1000);

  // Tell backend to await the next NFC tap for registration
  const result = S.adminRegistration ? await endpoint(name, role) : await endpoint(name);
  if (!result.success) {
    clearInterval(registerCountdownTimer);
    showRegisterStep('register-step-error');
    document.getElementById('register-error-msg').textContent =
      result.detail || result.message || 'Could not start registration.';
    scheduleAfterRegistration(3500);
  } else if (USE_DEMO) {
    // Demo mode has no NFC hardware — simulate the card tap after 1.5 s.
    clearTimeout(demoRegisterTapTimer);
    demoRegisterTapTimer = setTimeout(() => {
      demoRegisterTapTimer = null;
      handleRegistrationSuccess({
        user: { id: 0, name, role: role || 'user' },
        replaced: false,
      });
    }, 1500);
  }
}

/**
 * After registration succeeds or fails, return to idle so the next work-card
 * tap is a login. Do not restart an admin session — that leftover session
 * would treat the next tap as logout. If a work-card tap already started a
 * new session (SSE may still be in flight), leave that session alone.
 */
async function navigateAfterRegistration() {
  S.adminRegistration = false;
  apiCancelRegistration();
  if (S.screen !== 'register') return;

  if (!USE_DEMO) {
    try {
      const res = await fetch('/api/session');
      if (S.screen !== 'register') return;
      const data = await res.json();
      if (S.screen !== 'register') return;
      // overlay: leftover admin Register User session. A non-overlay
      // session is a work-card login (SSE may still be in flight).
      if (data.active && !data.overlay) return;
    } catch (_) { /* go idle */ }
  }

  if (S.screen !== 'register') return;
  await endSession();
}

/**
 * Cancel the in-progress registration flow, stop the countdown timer,
 * notify the backend, and return to the idle screen. If the cancel was
 * triggered during an admin-initiated registration, the admin session is
 * ended and the admin state is cleared so the system returns to a clean
 * idle state.
 */
function cancelRegistration() {
  clearInterval(registerCountdownTimer);
  clearTimeout(demoRegisterTapTimer);
  demoRegisterTapTimer = null;
  clearAfterRegisterTimer();
  apiCancelRegistration();
  if (S.adminRegistration) {
    S.adminRegistration = false;
    adminSessionActive = false;
    endSession();
  } else {
    navigate('idle');
  }
}

/**
 * Handle a successful registration SSE event. Shows the success step with a
 * welcome message and automatically navigates back after 4 seconds.
 * @param {Object} data - The SSE event data containing the new user info.
 * @param {Object} data.user - The newly registered user object.
 * @param {string} data.user.name - The registered user's display name.
 */
function handleRegistrationSuccess(data) {
  clearInterval(registerCountdownTimer);
  if (S.screen !== 'register') return;
  showRegisterStep('register-step-success');
  document.getElementById('register-success-msg').textContent =
    data.user.role === 'admin'
      ? `Registered ${data.user.name} as admin. They can now tap their card to log in.`
      : `Welcome, ${data.user.name}! You can now tap your card to log in.`;
  scheduleAfterRegistration(4000);
}

/**
 * Handle a failed registration SSE event. Shows the error step with the failure
 * reason and automatically navigates back after 4 seconds.
 * @param {Object} data - The SSE event data containing the failure reason.
 * @param {string} [data.reason] - Human-readable failure reason string.
 */
function handleRegistrationFailed(data) {
  clearInterval(registerCountdownTimer);
  if (S.screen !== 'register') return;
  showRegisterStep('register-step-error');
  document.getElementById('register-error-msg').textContent =
    data.reason || 'Registration failed. Please try again.';
  scheduleAfterRegistration(4000);
}

/* ============================================================
   HIDDEN ADMIN PANEL — 5× tap on clock area within 3 seconds
============================================================ */
const adminTaps = [];
const ADMIN_TAP_COUNT = 5;
const ADMIN_TAP_WINDOW = 3000; // ms
let adminSessionActive = false;

/**
 * Record a tap on the clock area and check if the admin tap sequence (5 taps
 * within 3 seconds) has been completed. Opens the admin panel on success.
 */
function checkAdminTapSequence() {
  const now = Date.now();
  adminTaps.push(now);
  // Keep only taps within the time window
  while (adminTaps.length > 0 && (now - adminTaps[0]) > ADMIN_TAP_WINDOW) {
    adminTaps.shift();
  }
  if (adminTaps.length >= ADMIN_TAP_COUNT) {
    adminTaps.length = 0;
    toggleAdminPanel();
  }
}

/**
 * Toggle the admin panel overlay between open and closed states.
 */
function toggleAdminPanel() {
  const overlay = document.getElementById('overlay-admin');
  if (overlay.classList.contains('visible')) {
    dismissAdminToIdle();
  } else {
    openAdminPanel();
  }
}

/**
 * Open the admin panel overlay with a polygon-wipe entrance animation.
 * Plays a click sound on activation.
 */
async function openAdminPanel() {
  clickSound();
  if (S.screen === 'idle') S.screen = 'admin';
  armIdle();
  const overlay = document.getElementById('overlay-admin');
  overlay.style.display = '';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    overlay.classList.add('visible');
  }));
  // Establish the backend admin session up front so EVERY panel action (sync
  // status/preview/commit, export, software update) is authorized the moment
  // the panel opens — not only after the admin happens to use the Borrow/Return
  // shortcuts (which were previously the only callers of adminStartSession).
  await adminStartSession();
  refreshSyncStatus();
  reportKioskDisplay('admin');
}

/**
 * Close the admin panel overlay with a slide-left exit animation.
 * Does not end the session (Borrow/Return/Register keep it).
 */
function closeAdminPanel() {
  const overlay = document.getElementById('overlay-admin');
  if (!overlay || overlay.style.display === 'none') return;
  overlay.classList.add('hidden-left');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-left');
    overlay.style.display = 'none';
    reportKioskDisplay(S.screen);
  }, 710);
}

/**
 * X / 5× toggle off: hide admin UI and end the overlay session so idle is idle.
 */
function dismissAdminToIdle() {
  closeAdminPanel();
  hideRegisterDeviceOverlay();
  hideUsersOverlay();
  apiCancelRegistration();
  adminSessionActive = false;
  endSession();
}

/**
 * Start a real backend admin session via POST /api/admin/session. The backend
 * finds the first active admin user in the database and creates a server-side
 * session so that subsequent API calls (borrow, return, sync) pass the
 * require_session check. Falls back to a demo-mode synthetic user when
 * USE_DEMO is true.
 * @returns {Promise<boolean>} True if the admin session was created, false on failure.
 */
async function adminStartSession(overlay = true) {
  if (USE_DEMO) {
    // Demo mode — no backend, use synthetic admin user
    adminSessionActive = true;
    S.user = { id: 0, name: 'Admin', role: 'admin' };
    fillMainMenu(S.user);
    return true;
  }

  try {
    const url = overlay ? '/api/admin/session' : '/api/admin/session?overlay=false';
    const res = await fetch(url, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      // Backend rejected — show error and stay on current screen
      showToast(data.detail || 'Admin session failed', 'error');
      return false;
    }
    // Backend created a real session — store user and update UI
    adminSessionActive = true;
    S.user = data.user;
    fillMainMenu(S.user);
    return true;
  } catch (_) {
    showToast('Could not reach server', 'error');
    return false;
  }
}

/**
 * Admin shortcut: close the admin panel, start a real backend admin session,
 * navigate to the main menu, and then open the locker overlay. If the backend
 * session creation fails (e.g. no admin users enrolled), the panel closes but
 * navigation is aborted so the user stays on the current screen.
 * @returns {Promise<void>}
 */
async function adminGotoBorrow() {
  closeAdminPanel();
  const ok = await adminStartSession(false);
  if (!ok) return; // session creation failed — stay on current screen
  await sleep(300); // wait for panel close animation
  navigate('main-menu');
  await sleep(200); // wait for circle-reveal transition
  openBorrow();
  armIdle();
}

/**
 * Admin shortcut: close the admin panel, start a real backend admin session,
 * navigate to the main menu, and then open the return screen. If the backend
 * session creation fails (e.g. no admin users enrolled), the panel closes but
 * navigation is aborted so the user stays on the current screen.
 * @returns {Promise<void>}
 */
async function adminGotoReturn() {
  closeAdminPanel();
  const ok = await adminStartSession(false);
  if (!ok) return; // session creation failed — stay on current screen
  await sleep(300); // wait for panel close animation
  navigate('main-menu');
  await sleep(200); // wait for circle-reveal transition
  openReturn();
  armIdle();
}

/** @type {{timer:number, origDesc:string}|null} Pending sync confirmation, if any. */
let _syncPreview = null;

/**
 * Render the "Last sync: …" line in the admin footer from /api/admin/sync-status.
 * @returns {Promise<void>}
 */
async function refreshSyncStatus() {
  const el = document.getElementById('admin-sync-status');
  if (!el) return;
  try {
    const res = await fetch('/api/admin/sync-status');
    if (!res.ok) return;
    const s = await res.json();
    if (!s.at) { el.textContent = 'Last sync: never'; return; }
    const when = s.at_local
      ? `${s.at_local}${s.ago ? ' (' + s.ago + ')' : ''}`
      : new Date(s.at).toLocaleString();
    const verdict = s.ok ? `${s.imported} new, ${s.updated} updated` : `failed${s.message ? ': ' + s.message : ''}`;
    el.textContent = `Last sync: ${when} (${s.trigger}) — ${verdict}`;
  } catch (_) { /* status unavailable — leave the line as-is */ }
}

/**
 * Admin source sync with a preview-then-confirm flow. The first tap runs a
 * dry-run preview (/api/admin/sync-preview) and shows the add/update/skip diff
 * on the button; a second tap within the confirm window commits the import
 * (/api/admin/sync-source). Avoids native dialogs so it works in kiosk Chromium.
 * @returns {Promise<void>}
 */
async function adminSyncSource() {
  const btn = document.getElementById('admin-sync-source');
  const label = btn.querySelector('.admin-btn-label');
  const desc = btn.querySelector('.admin-btn-desc');

  const reset = (origDesc) => {
    label.textContent = 'Sync Source';
    if (origDesc !== undefined) desc.textContent = origDesc;
    btn.classList.remove('confirm');
    btn.style.pointerEvents = '';
  };

  // Second tap within the confirm window -> commit the import.
  if (_syncPreview) {
    const p = _syncPreview;
    _syncPreview = null;
    clearTimeout(p.timer);
    label.textContent = 'Syncing…';
    btn.style.pointerEvents = 'none';
    try {
      const res = await fetch('/api/admin/sync-source', { method: 'POST' });
      const data = await res.json();
      if (res.ok) showToast(`Synced: ${data.imported} new, ${data.updated} updated`, 'success');
      else showToast(data.detail || 'Sync failed', 'error');
    } catch (_) {
      showToast('Sync request failed', 'error');
    }
    reset(p.origDesc);
    refreshSyncStatus();
    return;
  }

  // First tap -> dry-run preview.
  const origDesc = desc.textContent;
  label.textContent = 'Checking…';
  btn.style.pointerEvents = 'none';
  try {
    const res = await fetch('/api/admin/sync-preview', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Preview failed', 'error');
      reset();
      return;
    }
    const changes = data.imported + data.updated;
    if (changes === 0) {
      showToast(`Up to date — ${data.unchanged} unchanged, ${data.skipped} skipped`, 'success');
      reset();
      refreshSyncStatus();
      return;
    }
    label.textContent = `Apply ${changes} change${changes === 1 ? '' : 's'}?`;
    desc.textContent = `${data.imported} new · ${data.updated} upd · ${data.unchanged} same · ${data.skipped} skip — tap to apply`;
    btn.classList.add('confirm');
    btn.style.pointerEvents = '';
    const timer = setTimeout(() => { _syncPreview = null; reset(origDesc); }, 8000);
    _syncPreview = { timer, origDesc };
  } catch (_) {
    showToast('Preview request failed', 'error');
    reset();
  }
}

/**
 * Download the full database as an Excel workbook from the admin panel.
 * Fetches the binary .xlsx from the admin export endpoint, creates a
 * temporary blob URL, and triggers a browser download. Shows progress
 * feedback on the button and a toast on completion or failure.
 * @returns {Promise<void>}
 */
async function adminExportExcel() {
  const btn = document.getElementById('admin-export-excel');
  const label = btn.querySelector('.admin-btn-label');
  const origText = label.textContent;
  label.textContent = 'Downloading\u2026';
  btn.style.pointerEvents = 'none';

  try {
    const res = await fetch('/api/admin/export-excel');
    if (!res.ok) {
      const data = await res.json();
      showToast(data.detail || 'Export failed', 'error');
      return;
    }
    /* Convert the response body to a blob, create a temporary download
       link, click it to trigger the browser's save dialog, then clean up. */
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'smart_locker_data.xlsx';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast('Excel exported', 'success');
  } catch (_) {
    showToast('Export request failed', 'error');
  } finally {
    label.textContent = origText;
    btn.style.pointerEvents = '';
  }
}

/** @type {number} Bumped to cancel in-flight update / health polling. */
let updatePollGen = 0;

/** Terminal states that never stop the service (refuse / nothing to apply). */
const UPDATE_DISMISS_STATES = ['up_to_date', 'idle', 'failed'];
/** Terminal states after a swap that failed and rolled back — show, don't reload. */
const UPDATE_ROLLBACK_STATES = ['rolled_back', 'rollback_unhealthy'];

/**
 * Parse the `at` timestamp from `/api/admin/update-status` (ISO-8601, often
 * with a colon-less timezone offset such as `+0200`).
 * @param {string|null|undefined} at - Timestamp from update-status.json.
 * @returns {number|null} Epoch milliseconds, or null if missing/unparseable.
 */
function parseUpdateAt(at) {
  if (!at || typeof at !== 'string') return null;
  const normalized = at.replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
  const t = Date.parse(normalized);
  return Number.isFinite(t) ? t : null;
}

/**
 * Whether a status payload was written for this launch (not a leftover file).
 * @param {Object} data - JSON from `/api/admin/update-status`.
 * @param {number} launchedAt - `Date.now()` just before POST `/api/admin/update`.
 * @returns {boolean}
 */
function isFreshUpdateStatus(data, launchedAt) {
  const t = parseUpdateAt(data && data.at);
  if (t == null) return false;
  return t >= launchedAt - 15000; // 15s slack for systemd-run + truncated seconds
}

/**
 * Show the full-screen software-update overlay over the admin panel and
 * reset copy to the in-progress "Updating / Do not power off" state.
 */
function showUpdateOverlay() {
  updatePollGen += 1;
  S.updating = true;
  clearTimeout(S.idleTimer);
  clearInterval(S.cdTimer);
  const overlay = document.getElementById('overlay-update');
  const title = document.getElementById('update-title');
  const sub = document.getElementById('update-sub');
  const spinner = document.getElementById('update-spinner');
  const dismiss = document.getElementById('update-dismiss');
  if (title) title.textContent = 'Updating';
  if (sub) sub.classList.remove('hidden');
  if (spinner) spinner.classList.remove('hidden');
  if (dismiss) dismiss.classList.add('hidden');
  setUpdateStatusLine('Starting…', '');
  overlay.style.display = '';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    overlay.classList.add('visible');
  }));
}

/**
 * Hide the software-update overlay with the same left wipe as inactivity.
 */
function hideUpdateOverlay() {
  const overlay = document.getElementById('overlay-update');
  if (!overlay.classList.contains('visible')) {
    overlay.style.display = 'none';
    return;
  }
  overlay.classList.add('hidden-left');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-left');
    overlay.style.display = 'none';
  }, 710);
}

/**
 * Set the overlay status line and optional version caption.
 * @param {string} message - Text for the status line.
 * @param {string} [version] - Version string to show, or `''` to hide.
 */
function setUpdateStatusLine(message, version) {
  const statusEl = document.getElementById('update-status');
  if (statusEl && message != null) statusEl.textContent = message;
  if (arguments.length > 1) {
    const verEl = document.getElementById('update-version');
    if (!verEl) return;
    const text = version ? String(version) : '';
    verEl.textContent = text ? ('Version ' + text) : '';
    verEl.style.display = text ? '' : 'none';
  }
}

/**
 * Switch the overlay to a dismissible terminal result. Stops polling. The
 * overlay stays up until the user taps OK (no cancel of an in-flight update).
 * @param {string} message - Status line to show.
 * @param {{failed?: boolean, title?: string}} [opts] - Failure styling / title.
 */
function enterUpdateTerminal(message, opts) {
  opts = opts || {};
  updatePollGen += 1;
  S.updating = false;
  const spinner = document.getElementById('update-spinner');
  const sub = document.getElementById('update-sub');
  const dismiss = document.getElementById('update-dismiss');
  const title = document.getElementById('update-title');
  if (opts.title && title) title.textContent = opts.title;
  else if (opts.failed && title) title.textContent = 'Update failed';
  if (spinner) spinner.classList.add('hidden');
  if (sub) sub.classList.add('hidden');
  setUpdateStatusLine(message);
  if (dismiss) dismiss.classList.remove('hidden');
}

/**
 * Dismiss a terminal update result, clear `S.updating`, and restore the
 * admin panel (or idle underneath it). Does not reload.
 */
function dismissUpdateOverlay() {
  S.updating = false;
  updatePollGen += 1;
  hideUpdateOverlay();
}

/**
 * Trigger a software update from the admin panel. Confirms first, then shows
 * the full-screen overlay, then POSTs `/api/admin/update`. Demo mode never
 * POSTs — it shows a dismissible "Pi only" message after a brief overlay.
 * @returns {Promise<void>}
 */
async function adminUpdate() {
  if (S.updating) return;
  if (!confirm('Apply a software update now? Plug in the USB stick with locker-updates/ first. The kiosk will show an update screen and restart briefly; a failed update rolls back automatically.')) {
    return;
  }

  showUpdateOverlay();

  if (USE_DEMO) {
    setUpdateStatusLine('Demo preview — on the Pi this screen stays until the update finishes.');
    const dismiss = document.getElementById('update-dismiss');
    if (dismiss) dismiss.classList.remove('hidden');
    return;
  }

  const launchedAt = Date.now();
  const gen = updatePollGen;
  try {
    const res = await fetch('/api/admin/update', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (gen !== updatePollGen) return;
    if (!res.ok) {
      const msg = data.detail || 'Update could not start';
      enterUpdateTerminal(msg, { failed: true, title: 'Update failed' });
      showToast(msg, 'error');
      return;
    }
    setUpdateStatusLine(data.message || 'Starting…');
    pollUpdateStatus(launchedAt, gen);
  } catch (_) {
    if (gen !== updatePollGen) return;
    const msg = 'Update request failed';
    enterUpdateTerminal(msg, { failed: true, title: 'Update failed' });
    showToast(msg, 'error');
  }
}

/**
 * Poll `/api/admin/update-status` while the API is up. Terminal-before-swap
 * (`up_to_date` / `idle` / `failed`) and rollback results stay on the overlay
 * with a dismiss button. `success` reloads. A failed fetch means the service
 * stopped for the swap — switch to `/api/health` polling.
 * @param {number} launchedAt - Epoch ms when the POST was sent.
 * @param {number} gen - `updatePollGen` snapshot; mismatch cancels this loop.
 * @returns {Promise<void>}
 */
async function pollUpdateStatus(launchedAt, gen) {
  while (S.updating && gen === updatePollGen) {
    await sleep(1500);
    if (!S.updating || gen !== updatePollGen) return;

    let data;
    try {
      const res = await fetch('/api/admin/update-status');
      if (!res.ok) {
        // 401/403 after a restart: session died with the process, not "still
        // swapping". Connection errors also land here. Both → health poll.
        setUpdateStatusLine('Installing…');
        await pollHealthAfterUpdate(gen);
        return;
      }
      data = await res.json();
    } catch (_) {
      setUpdateStatusLine('Installing…');
      await pollHealthAfterUpdate(gen);
      return;
    }

    const state = data.state || '';
    const message = data.message || state;
    const version = data.version || data.current_version || '';
    if (message) setUpdateStatusLine(message, version);

    const fresh = isFreshUpdateStatus(data, launchedAt);
    if (!fresh && (UPDATE_DISMISS_STATES.includes(state)
        || UPDATE_ROLLBACK_STATES.includes(state)
        || state === 'success')) {
      continue; // leftover status file from a previous run
    }

    if (state === 'success') {
      setUpdateStatusLine('Restarting…', version);
      await sleep(400);
      if (gen !== updatePollGen) return;
      location.reload();
      return;
    }
    if (UPDATE_DISMISS_STATES.includes(state)) {
      enterUpdateTerminal(message, { failed: state === 'failed' });
      return;
    }
    if (UPDATE_ROLLBACK_STATES.includes(state)) {
      enterUpdateTerminal(message, { failed: true, title: 'Update failed' });
      return;
    }
  }
}

/**
 * Poll `/api/health` every ~2s after the update service has gone down.
 * Verdict comes from the public ``update`` field (no admin session). Reload
 * only on ``success``; rollback/failed stay on this overlay. Bound so a dead
 * API does not spin "Installing…" forever.
 * @param {number} gen - `updatePollGen` snapshot; mismatch cancels this loop.
 * @returns {Promise<void>}
 */
async function pollHealthAfterUpdate(gen) {
  const deadline = Date.now() + 180000;
  while (S.updating && gen === updatePollGen) {
    await sleep(2000);
    if (!S.updating || gen !== updatePollGen) return;
    if (Date.now() > deadline) {
      enterUpdateTerminal('Update is taking too long. Check logs/update.log on the Pi.', { failed: true });
      return;
    }
    try {
      const res = await fetch('/api/health');
      if (!res.ok) continue;
      const data = await res.json();
      const verdict = data && data.update;
      const state = (verdict && verdict.state) || '';
      const message = (verdict && verdict.message) || state;
      const version = (verdict && (verdict.version || verdict.current_version)) || '';

      if (UPDATE_ROLLBACK_STATES.includes(state) || state === 'failed') {
        enterUpdateTerminal(message, { failed: true, title: 'Update failed' });
        return;
      }
      if (state === 'success' && data.status === 'ok') {
        setUpdateStatusLine('Restarting…', version);
        await sleep(400);
        if (gen !== updatePollGen) return;
        location.reload();
        return;
      }
      // Health can be ok while status is still "updating" (written after the
      // health-gate). Keep waiting for success or rollback.
      if (data.status === 'ok') {
        setUpdateStatusLine(message || 'Installing…', version);
      }
    } catch (_) { /* service still down */ }
  }
}

/**
 * Admin shortcut: close the admin panel and open the registration screen in
 * admin mode (free-text name entry, bypasses registrant list validation).
 * The admin session stays active until the card is tapped so the backend
 * accepts POST /api/admin/register; after enroll the session is ended and
 * the kiosk returns to idle.
 * @returns {Promise<void>}
 */
async function adminRegisterUser() {
  closeAdminPanel();
  S.adminRegistration = true;
  await sleep(300); // wait for panel close animation
  openRegister();
}

/** @type {number|null} Bind-window countdown interval. */
let bindCountdownTimer = null;

/**
 * Show a Register Device overlay step and hide the others.
 * @param {string} stepId - Element id of the step to show.
 */
function showBindStep(stepId) {
  document.querySelectorAll('#overlay-register-device .bind-step').forEach(el => {
    el.classList.toggle('hidden', el.id !== stepId);
  });
}

/**
 * Hide the Register Device overlay without ending the admin session.
 */
function hideRegisterDeviceOverlay() {
  clearInterval(bindCountdownTimer);
  const overlay = document.getElementById('overlay-register-device');
  if (!overlay || overlay.style.display === 'none') return;
  overlay.classList.add('hidden-left');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-left');
    overlay.style.display = 'none';
  }, 710);
}

/**
 * Close Register Device, cancel a pending bind, return toward admin/idle.
 */
function closeRegisterDevice() {
  clearInterval(bindCountdownTimer);
  apiCancelRegistration();
  hideRegisterDeviceOverlay();
  openAdminPanel();
}

/**
 * Open the admin Register Device overlay (add from Excel, or bind an existing row).
 * @returns {Promise<void>}
 */
async function adminRegisterDevice() {
  closeAdminPanel();
  await sleep(300);
  const overlay = document.getElementById('overlay-register-device');
  overlay.style.display = '';
  showBindStep('bind-step-list');
  document.getElementById('bind-search').value = '';
  S.screen = 'admin';
  armIdle();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    overlay.classList.add('visible');
  }));
  await populateBindList();
}

/** Debounce timer for Register Device bind-search (filters cached rows). */
let bindSearchTimer = 0;

/**
 * Fetch devices and render the bind list: name + PM (+ slot), unbound first.
 * Duplicate names stay as distinct rows (PM identifies the unit).
 * Search filters the cached list; pass refresh=true after a bind/register.
 *
 * @param {boolean} [refresh=true] - When false, filter ``S.devices`` without a GET.
 * @returns {Promise<void>}
 */
async function populateBindList(refresh) {
  const list = document.getElementById('bind-device-list');
  if (!list) return;
  if (refresh !== false || !Array.isArray(S.devices)) {
    const devices = await apiGetDevices();
    S.devices = devices;
  }
  const devices = S.devices || [];
  const query = (document.getElementById('bind-search').value || '').toLowerCase().trim();
  const filtered = devices.filter(d => {
    if (!query) return true;
    const name = (d.name || '').toLowerCase();
    const pm = (d.pm_number || '').toLowerCase();
    return name.includes(query) || pm.includes(query);
  });
  filtered.sort((a, b) => {
    const at = a.has_tag ? 1 : 0;
    const bt = b.has_tag ? 1 : 0;
    if (at !== bt) return at - bt;
    const n = (a.name || '').localeCompare(b.name || '');
    if (n !== 0) return n;
    return (a.pm_number || '').localeCompare(b.pm_number || '');
  });
  list.innerHTML = '';
  filtered.forEach(dev => {
    const row = document.createElement('div');
    row.className = 'bind-row';
    const tagged = !!dev.has_tag;

    const info = document.createElement('div');
    info.className = 'bind-row-info';
    const nameEl = document.createElement('div');
    nameEl.className = 'bind-row-name';
    nameEl.textContent = dev.name || '';
    const metaEl = document.createElement('div');
    metaEl.className = 'bind-row-meta';
    const slot = dev.locker_slot != null ? ` · Slot ${dev.locker_slot}` : '';
    metaEl.textContent = `${dev.pm_number || '—'}${slot}`;
    info.appendChild(nameEl);
    info.appendChild(metaEl);

    const pill = document.createElement('span');
    pill.className = 'bind-tag-pill' + (tagged ? '' : ' unbound');
    pill.textContent = tagged ? 'Tagged' : 'No tag';

    const actions = document.createElement('div');
    actions.className = 'bind-row-actions';
    const bindBtn = document.createElement('button');
    bindBtn.type = 'button';
    bindBtn.className = 'bind-go';
    bindBtn.textContent = tagged ? 'Replace tag' : 'Bind';
    bindBtn.addEventListener('click', () => { clickSound(); startDeviceTagBind(dev); });
    actions.appendChild(bindBtn);
    const slotBtn = document.createElement('button');
    slotBtn.type = 'button';
    slotBtn.className = 'bind-unbind';
    slotBtn.textContent = 'Slot';
    slotBtn.addEventListener('click', () => { clickSound(); openChangeSlot(dev); });
    actions.appendChild(slotBtn);
    if (tagged) {
      const unbindBtn = document.createElement('button');
      unbindBtn.type = 'button';
      unbindBtn.className = 'bind-unbind';
      unbindBtn.textContent = 'Unbind';
      unbindBtn.addEventListener('click', () => { clickSound(); unbindDeviceTag(dev); });
      actions.appendChild(unbindBtn);
    }

    row.appendChild(info);
    row.appendChild(pill);
    row.appendChild(actions);
    list.appendChild(row);
  });
}

/**
 * Start the 60s tap-the-sticker window for one locker device.
 * @param {Object} dev - Device row (id, name, pm_number).
 * @returns {Promise<void>}
 */
async function startDeviceTagBind(dev) {
  document.getElementById('bind-confirm-name').textContent =
    `${dev.name} (${dev.pm_number})`;
  showBindStep('bind-step-tap');
  try {
    const res = await fetch(`/api/admin/devices/${dev.id}/bind-tag`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      apiCancelRegistration();
      showBindStep('bind-step-error');
      document.getElementById('bind-error-msg').textContent =
        data.detail || 'Could not start bind.';
      setTimeout(() => {
        showBindStep('bind-step-list');
        populateBindList();
      }, 2500);
      return;
    }
  } catch (_) {
    apiCancelRegistration();
    showBindStep('bind-step-error');
    document.getElementById('bind-error-msg').textContent = 'Could not start bind.';
    setTimeout(() => {
      showBindStep('bind-step-list');
      populateBindList();
    }, 2500);
    return;
  }
  startBindCountdown();
}

/** Minimum slot buttons shown in the picker (grows with occupied max + 1). */
const SLOT_GRID_MIN = 12;
/** Same cap as the Register Device / change-slot API (MAX_LOCKER_SLOT). */
const SLOT_GRID_MAX = 48;

/** @type {number|null} Slot chosen on the Add from Excel step. */
let selectedAddSlot = null;

/** @type {Object|null} Device being moved in the change-slot step. */
let slotChangeDevice = null;

/** @type {number|null} Slot chosen on the change-slot step. */
let selectedChangeSlot = null;

/**
 * Occupied locker slot numbers, optionally ignoring one device (the one being moved).
 * @param {number|null} [exceptId]
 * @returns {Set<number>}
 */
function occupiedSlots(exceptId) {
  const used = new Set();
  (S.devices || []).forEach(d => {
    if (d.locker_slot == null) return;
    if (exceptId != null && d.id === exceptId) return;
    used.add(d.locker_slot);
  });
  return used;
}

/**
 * Render a 1..N slot picker. Occupied slots are disabled.
 * @param {string} containerId
 * @param {Set<number>} occupied
 * @param {number|null} selected
 * @param {function(number): void} onPick
 */
function renderSlotGrid(containerId, occupied, selected, onPick) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '';
  let maxUsed = 0;
  occupied.forEach(n => { if (n > maxUsed) maxUsed = n; });
  const max = Math.min(SLOT_GRID_MAX, Math.max(SLOT_GRID_MIN, maxUsed + 1));
  for (let n = 1; n <= max; n++) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'bind-slot-btn';
    btn.textContent = String(n);
    if (occupied.has(n)) {
      btn.classList.add('taken');
      btn.disabled = true;
    }
    if (selected === n) btn.classList.add('selected');
    btn.addEventListener('click', () => { clickSound(); onPick(n); });
    el.appendChild(btn);
  }
}

/**
 * 60s countdown on the tap-sticker step. Shared by Bind and Add from Excel.
 */
function startBindCountdown() {
  clearInterval(bindCountdownTimer);
  let secs = 60;
  const cdEl = document.getElementById('bind-countdown');
  cdEl.textContent = secs + 's';
  bindCountdownTimer = setInterval(() => {
    secs--;
    cdEl.textContent = secs + 's';
    if (secs <= 0) {
      clearInterval(bindCountdownTimer);
      apiCancelRegistration();
      showBindStep('bind-step-error');
      document.getElementById('bind-error-msg').textContent =
        'Bind timed out. Please try again.';
      setTimeout(() => {
        showBindStep('bind-step-list');
        populateBindList();
      }, 2500);
    }
  }, 1000);
}

/**
 * Open the Add from Excel step (PM + free slot).
 */
function openAddFromExcel() {
  if (USE_DEMO) {
    showToast('Add from Excel is Pi only', 'error');
    return;
  }
  document.getElementById('bind-pm-input').value = '';
  document.getElementById('bind-add-error').textContent = '';
  selectedAddSlot = null;
  const paint = () => {
    renderSlotGrid('bind-slot-grid', occupiedSlots(), selectedAddSlot, n => {
      selectedAddSlot = n;
      paint();
    });
  };
  paint();
  showBindStep('bind-step-add');
}

/**
 * POST PM + slot, then wait for the sticker tap (bind window already armed).
 * @returns {Promise<void>}
 */
async function submitRegisterDevice() {
  if (USE_DEMO) {
    showToast('Add from Excel is Pi only', 'error');
    return;
  }
  const pm = (document.getElementById('bind-pm-input').value || '').trim();
  const err = document.getElementById('bind-add-error');
  err.textContent = '';
  if (!pm) {
    err.textContent = `Enter the ${ASSET_LABEL}.`;
    return;
  }
  if (!selectedAddSlot) {
    err.textContent = 'Pick a free slot.';
    return;
  }
  try {
    const res = await fetch('/api/admin/devices/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pm_number: pm, locker_slot: selectedAddSlot }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail;
      err.textContent = typeof detail === 'string' ? detail : 'Could not register.';
      return;
    }
    document.getElementById('bind-confirm-name').textContent =
      `${data.name} (${data.pm_number})`;
    showBindStep('bind-step-tap');
    startBindCountdown();
  } catch (_) {
    err.textContent = 'Could not register.';
  }
}

/**
 * Open the change-slot step for an existing locker row.
 * @param {Object} dev
 */
function openChangeSlot(dev) {
  slotChangeDevice = dev;
  selectedChangeSlot = dev.locker_slot != null ? dev.locker_slot : null;
  document.getElementById('bind-slot-name').textContent =
    `${dev.name} (${dev.pm_number})`;
  document.getElementById('bind-slot-error').textContent = '';
  const paint = () => {
    renderSlotGrid(
      'bind-change-slot-grid',
      occupiedSlots(dev.id),
      selectedChangeSlot,
      n => { selectedChangeSlot = n; paint(); },
    );
  };
  paint();
  showBindStep('bind-step-slot');
}

/**
 * POST a new slot for the device opened in openChangeSlot.
 * @returns {Promise<void>}
 */
async function submitChangeSlot() {
  const err = document.getElementById('bind-slot-error');
  err.textContent = '';
  if (!slotChangeDevice) return;
  if (!selectedChangeSlot) {
    err.textContent = 'Pick a slot.';
    return;
  }
  if (USE_DEMO) {
    slotChangeDevice.locker_slot = selectedChangeSlot;
    showBindStep('bind-step-list');
    populateBindList();
    return;
  }
  try {
    const res = await fetch(`/api/admin/devices/${slotChangeDevice.id}/slot`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ locker_slot: selectedChangeSlot }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail;
      err.textContent = typeof detail === 'string' ? detail : 'Could not change slot.';
      return;
    }
    showBindStep('bind-step-list');
    await populateBindList();
  } catch (_) {
    err.textContent = 'Could not change slot.';
  }
}

/**
 * Clear tag_hmac on a device and refresh the bind list.
 * @param {Object} dev - Device row.
 * @returns {Promise<void>}
 */
async function unbindDeviceTag(dev) {
  try {
    const res = await fetch(`/api/admin/devices/${dev.id}/unbind-tag`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      showToast(`Unbound ${dev.name} (${dev.pm_number})`, 'success');
      await populateBindList();
    } else {
      showToast(data.detail || 'Unbind failed', 'error');
    }
  } catch (_) {
    showToast('Unbind failed', 'error');
  }
}

/**
 * Handle tag_bind_success SSE: show success, then return to the list.
 * @param {Object} data - SSE payload with device_name / pm_number.
 */
function handleTagBindSuccess(data) {
  const overlay = document.getElementById('overlay-register-device');
  if (!overlay || overlay.style.display === 'none') return;
  clearInterval(bindCountdownTimer);
  showBindStep('bind-step-success');
  const name = data.device_name || 'Device';
  const pm = data.pm_number ? ` (${data.pm_number})` : '';
  document.getElementById('bind-success-msg').textContent = `Bound ${name}${pm}.`;
  setTimeout(() => {
    showBindStep('bind-step-list');
    populateBindList();
  }, 2500);
}

/**
 * Handle tag_bind_failed SSE: show the reason, then return to the list.
 * @param {Object} data - SSE payload with reason.
 */
function handleTagBindFailed(data) {
  const overlay = document.getElementById('overlay-register-device');
  if (!overlay || overlay.style.display === 'none') return;
  clearInterval(bindCountdownTimer);
  showBindStep('bind-step-error');
  document.getElementById('bind-error-msg').textContent =
    data.reason || 'Bind failed. Please try again.';
  setTimeout(() => {
    showBindStep('bind-step-list');
    populateBindList();
  }, 2500);
}

/* ============================================================
   ADMIN — USERS OVERLAY (list, deactivate, replace card)
============================================================ */
/** @type {number|null} Replace-card countdown interval. */
let usersCountdownTimer = null;
/** @type {Array<Object>} Cached rows from GET /api/admin/users. */
let usersList = [];
/** @type {number|null} User id pending deactivate confirm or replace tap. */
let usersTargetId = null;
/** @type {boolean} Replace-card POST in flight; extra clicks are ignored. */
let usersReplaceArming = false;
/** @type {number|null} Users overlay auto-return timer (success/error steps). */
let usersStepTimer = null;
/** @type {number|null} Fallback idle timer after self-deactivate (SSE loss). */
let sessionEndedFallbackTimer = null;

/**
 * Show a Users overlay step and hide the others.
 * @param {string} stepId - Element id of the step to show.
 */
function showUsersStep(stepId) {
  document.querySelectorAll('#overlay-users .users-step').forEach(el => {
    el.classList.toggle('hidden', el.id !== stepId);
  });
}

/**
 * Fetch active users for the Users overlay. Demo mode builds a static list
 * from DEMO_USERS with borrowed counts derived from DEMO_DEVICES.
 * @returns {Promise<Array<Object>|null>} User rows, or null on failure.
 */
async function apiAdminListUsers() {
  if (USE_DEMO) {
    await sleep(300); // simulate API latency
    const counts = {};
    DEMO_DEVICES.forEach(d => {
      if (d.status === 'borrowed' && d.borrower_name) {
        counts[d.borrower_name] = (counts[d.borrower_name] || 0) + 1;
      }
    });
    return DEMO_USERS.map(u => ({
      id: u.id, name: u.name, role: u.role,
      borrowed_count: counts[u.name] || 0,
    }));
  }
  try {
    const res = await fetch('/api/admin/users');
    if (!res.ok) return null;
    const data = await res.json();
    return data.users || [];
  } catch (_) { return null; }
}

/**
 * Render the cached usersList into #users-list rows (name, role badge,
 * borrowed count, Replace card / Deactivate buttons).
 */
function renderUsersList() {
  const list = document.getElementById('users-list');
  list.innerHTML = '';
  if (!usersList.length) {
    const empty = document.createElement('p');
    empty.className = 'bind-hint';
    empty.textContent = 'No active users.';
    list.appendChild(empty);
    return;
  }
  usersList.forEach(u => {
    const row = document.createElement('div');
    row.className = 'bind-row';

    const info = document.createElement('div');
    info.className = 'bind-row-info';
    const nameEl = document.createElement('div');
    nameEl.className = 'bind-row-name';
    nameEl.textContent = u.name || '';
    const metaEl = document.createElement('div');
    metaEl.className = 'bind-row-meta';
    metaEl.textContent = `${u.borrowed_count || 0} borrowed`;
    info.appendChild(nameEl);
    info.appendChild(metaEl);

    const pill = document.createElement('span');
    pill.className = 'bind-tag-pill' + (u.role === 'admin' ? '' : ' unbound');
    pill.textContent = u.role === 'admin' ? 'admin' : 'user';

    const actions = document.createElement('div');
    actions.className = 'bind-row-actions';
    const replaceBtn = document.createElement('button');
    replaceBtn.type = 'button';
    replaceBtn.className = 'bind-go';
    replaceBtn.textContent = 'Replace card';
    replaceBtn.dataset.replaceId = u.id;
    const deactBtn = document.createElement('button');
    deactBtn.type = 'button';
    deactBtn.className = 'bind-unbind';
    deactBtn.textContent = 'Deactivate';
    deactBtn.dataset.deactivateId = u.id;
    actions.appendChild(replaceBtn);
    actions.appendChild(deactBtn);

    row.appendChild(info);
    row.appendChild(pill);
    row.appendChild(actions);
    list.appendChild(row);
  });
}

/**
 * Reload GET /api/admin/users and re-render the list step.
 * @returns {Promise<void>}
 */
async function loadUsersList() {
  const users = await apiAdminListUsers();
  if (users === null) {
    document.getElementById('users-error').textContent = 'Could not load users.';
    if (!usersList.length) renderUsersList();
  } else {
    usersList = users;
    document.getElementById('users-error').textContent = '';
    renderUsersList();
  }
}

/**
 * Admin shortcut: close the admin panel and open the Users overlay.
 * @returns {Promise<void>}
 */
async function adminOpenUsers() {
  closeAdminPanel();
  await sleep(300); // wait for panel close animation
  const overlay = document.getElementById('overlay-users');
  overlay.style.display = '';
  showUsersStep('users-step-list');
  document.getElementById('users-error').textContent = '';
  S.screen = 'admin';
  armIdle();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    overlay.classList.add('visible');
  }));
  await loadUsersList();
}

/**
 * Hide the Users overlay without reopening the admin panel (used when the
 * session ends underneath — e.g. the admin deactivated themselves).
 */
function hideUsersOverlay() {
  clearInterval(usersCountdownTimer);
  clearTimeout(usersStepTimer);
  usersStepTimer = null;
  S.usersReplacePending = false;
  const overlay = document.getElementById('overlay-users');
  if (!overlay || overlay.style.display === 'none') return;
  overlay.classList.remove('visible');
  overlay.style.display = 'none';
}

/**
 * Whether the Users overlay tap-the-new-card step is currently visible.
 * @returns {boolean} True if the tap step is showing.
 */
function isUsersTapStepShowing() {
  const step = document.getElementById('users-step-tap');
  return !!step && !step.classList.contains('hidden');
}

/**
 * Enable or disable the Users list Replace buttons (arming feedback).
 * @param {boolean} disabled - True to disable while the arm POST is in flight.
 */
function setUsersReplaceButtonsDisabled(disabled) {
  document.querySelectorAll('#users-list [data-replace-id]').forEach(b => {
    b.disabled = disabled;
  });
}

/**
 * Close the Users overlay, cancel an armed replace window, reopen the panel.
 */
function closeUsersOverlay() {
  clearInterval(usersCountdownTimer);
  // Closing from the tap step must always release the server window, even if
  // a late response already cleared the pending flag (double-click race).
  if (S.usersReplacePending || isUsersTapStepShowing()) {
    S.usersReplacePending = false;
    apiCancelRegistration();
  }
  hideUsersOverlay();
  openAdminPanel();
}

/**
 * Open the deactivate confirm step for one user row.
 * @param {number} id - User primary key.
 */
function usersAskDeactivate(id) {
  const u = usersList.find(x => x.id === id);
  if (!u) return;
  usersTargetId = id;
  document.getElementById('users-confirm-name').textContent = u.name;
  document.getElementById('users-confirm-error').textContent = '';
  showUsersStep('users-step-confirm');
}

/**
 * POST the deactivate for the confirmed user. 409 details (last admin,
 * devices still held) show inline. When the response ends the panel's own
 * session, the overlay hides and the session_ended SSE drives idle.
 * @returns {Promise<void>}
 */
async function usersDeactivate() {
  const id = usersTargetId;
  const errEl = document.getElementById('users-confirm-error');
  errEl.textContent = '';
  if (USE_DEMO) {
    await sleep(300);
    usersList = usersList.filter(u => u.id !== id);
    showUsersStep('users-step-list');
    renderUsersList();
    return;
  }
  try {
    const res = await fetch(`/api/admin/users/${id}/deactivate`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      errEl.textContent = data.detail || 'Could not deactivate.';
      return;
    }
    if (data.session_ended) {
      hideUsersOverlay();
      // The backend session is authoritatively dead; drop the local user now
      // so the fallback can tell a re-login apart from SSE loss.
      S.user = null;
      scheduleSessionEndedFallback();
      return; // session_ended SSE navigates to idle
    }
    showUsersStep('users-step-list');
    await loadUsersList();
  } catch (_) {
    errEl.textContent = 'Could not deactivate.';
  }
}

/**
 * Idle locally if the session_ended SSE never arrives after self-deactivate.
 * Conservative: only fires when no (re-)login happened since — a live
 * session always has S.user set and leaves the admin screen.
 */
function scheduleSessionEndedFallback() {
  clearTimeout(sessionEndedFallbackTimer);
  sessionEndedFallbackTimer = setTimeout(() => {
    sessionEndedFallbackTimer = null;
    if (S.user === null && S.screen === 'admin') endSession(false, true);
  }, 2500);
}

/**
 * Arm the 60s replace-card window for one user, then show the tap step.
 * @param {number} id - User primary key.
 * @returns {Promise<void>}
 */
async function usersReplaceCard(id) {
  const u = usersList.find(x => x.id === id);
  if (!u) return;
  usersTargetId = id;
  document.getElementById('users-tap-name').textContent = u.name;
  const errEl = document.getElementById('users-error');
  errEl.textContent = '';
  if (USE_DEMO) {
    await sleep(300);
    showUsersStep('users-step-success');
    document.getElementById('users-success-msg').textContent =
      `Card replaced for ${u.name}.`;
    clearTimeout(usersStepTimer);
    usersStepTimer = setTimeout(() => { showUsersStep('users-step-list'); }, 2500);
    return;
  }
  // In-flight guard: a double-click's second POST would 409 against the
  // window the first POST just armed, then clear the pending flag and hide
  // its error on the now-hidden list step. Separate from
  // S.usersReplacePending, which tracks the armed window for close/cancel.
  if (usersReplaceArming) return;
  usersReplaceArming = true;
  setUsersReplaceButtonsDisabled(true);
  // Mark pending BEFORE the POST: the backend arms its window the moment the
  // request arrives, so a fast tap's SSE result can land before this fetch
  // resolves. Cleared on every HTTP failure below.
  S.usersReplacePending = true;
  try {
    const res = await fetch(`/api/admin/users/${id}/replace-card`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    usersReplaceArming = false;
    setUsersReplaceButtonsDisabled(false);
    if (!res.ok) {
      // Late loser of a double-arm race: the tap step is already up from the
      // winning POST, so leave the armed window alone and stay silent.
      if (res.status === 409 && isUsersTapStepShowing()) return;
      S.usersReplacePending = false;
      errEl.textContent = data.detail || 'Could not replace card.';
      return;
    }
  } catch (_) {
    usersReplaceArming = false;
    setUsersReplaceButtonsDisabled(false);
    S.usersReplacePending = false;
    errEl.textContent = 'Could not replace card.';
    return;
  }
  showUsersStep('users-step-tap');
  startUsersCountdown();
}

/**
 * 60s countdown on the tap-the-new-card step. On timeout the window is
 * cancelled via POST /api/register/cancel and the error step shows.
 */
function startUsersCountdown() {
  clearInterval(usersCountdownTimer);
  let secs = 60;
  const cdEl = document.getElementById('users-countdown');
  cdEl.textContent = secs + 's';
  usersCountdownTimer = setInterval(() => {
    secs--;
    cdEl.textContent = secs + 's';
    if (secs <= 0) {
      clearInterval(usersCountdownTimer);
      S.usersReplacePending = false;
      apiCancelRegistration();
      showUsersStep('users-step-error');
      document.getElementById('users-error-msg').textContent =
        'Card replace timed out. Please try again.';
      clearTimeout(usersStepTimer);
      usersStepTimer = setTimeout(() => {
        showUsersStep('users-step-list');
        loadUsersList();
      }, 2500);
    }
  }, 1000);
}

/**
 * Route registration_success / registration_failed SSE to the Users overlay
 * while a replace-card window is armed.
 * @param {Object} data - SSE payload (user or reason).
 * @param {boolean} ok - True on registration_success.
 */
function handleUsersReplaceResult(data, ok) {
  S.usersReplacePending = false;
  const overlay = document.getElementById('overlay-users');
  if (!overlay || overlay.style.display === 'none') return;
  clearInterval(usersCountdownTimer);
  if (ok) {
    showUsersStep('users-step-success');
    const name = data.user && data.user.name ? data.user.name : 'user';
    document.getElementById('users-success-msg').textContent =
      `Card replaced for ${name}.`;
  } else {
    showUsersStep('users-step-error');
    document.getElementById('users-error-msg').textContent =
      data.reason || 'Card replace failed. Please try again.';
  }
  clearTimeout(usersStepTimer);
  usersStepTimer = setTimeout(() => {
    showUsersStep('users-step-list');
    loadUsersList();
  }, 2500);
}

/**
 * End the admin session from the admin panel. Closes the panel, clears the
 * admin session flag, and calls the standard session end flow.
 */
function adminEndSession() {
  closeAdminPanel();
  adminSessionActive = false;
  endSession();
}

/**
 * Close Chromium kiosk (backend stays up). Confirm first. Demo never POSTs.
 * @returns {Promise<void>}
 */
async function adminExitKiosk() {
  if (S.updating) return;
  if (!confirm('Close the kiosk browser? The locker service stays running. Chromium will not come back until the next login or reboot.')) {
    return;
  }
  if (USE_DEMO) {
    showToast('Demo preview — Exit kiosk is Pi only', 'success');
    return;
  }
  try {
    const res = await fetch('/api/admin/exit-kiosk', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || 'Could not exit kiosk', 'error');
      return;
    }
    showToast(data.message || 'Kiosk closing…', 'success');
  } catch (_) {
    showToast('Exit kiosk request failed', 'error');
  }
}

/**
 * Show the full-screen shutting-down overlay over the admin panel.
 * Sets ``S.updating`` so SSE handlers ignore card taps until poweroff
 * finishes or the overlay is dismissed (failed start / demo).
 */
function showPowerOverlay() {
  S.updating = true;
  clearTimeout(S.idleTimer);
  clearInterval(S.cdTimer);
  const overlay = document.getElementById('overlay-power');
  if (!overlay) return;
  overlay.style.display = '';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    overlay.classList.add('visible');
  }));
}

/**
 * Hide the shutting-down overlay (used when poweroff could not start).
 */
function hidePowerOverlay() {
  S.updating = false;
  const overlay = document.getElementById('overlay-power');
  if (!overlay) return;
  overlay.classList.remove('visible');
  overlay.style.display = 'none';
  const dismiss = document.getElementById('power-dismiss');
  if (dismiss) dismiss.classList.add('hidden');
}

/**
 * Power off the Pi. Confirm first. Demo never POSTs.
 * @returns {Promise<void>}
 */
async function adminShutdown() {
  if (S.updating) return;
  if (!confirm('Shut down the Raspberry Pi now? The locker will power off.')) {
    return;
  }
  showPowerOverlay();
  if (USE_DEMO) {
    showToast('Demo preview — Shut down is Pi only', 'success');
    const dismiss = document.getElementById('power-dismiss');
    if (dismiss) dismiss.classList.remove('hidden');
    return;
  }
  try {
    const res = await fetch('/api/admin/shutdown', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      hidePowerOverlay();
      showToast(data.detail || 'Could not shut down', 'error');
      return;
    }
  } catch (_) {
    hidePowerOverlay();
    showToast('Shut down request failed', 'error');
  }
}

/* ============================================================
   AUDIO CLICK FEEDBACK
============================================================ */
/**
 * Play a short click/tap audio feedback sound using the Web Audio API.
 * Creates a brief oscillator sweep from 900Hz to 420Hz over 70ms.
 * Silently ignored if audio is not available.
 */
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
document.getElementById('overlay-slot').addEventListener('click',  () => { clickSound(); dismissSlotOverlay(); });
document.getElementById('handover-accept').addEventListener('click', () => { clickSound(); acceptHandover(); });
document.getElementById('handover-cancel').addEventListener('click', () => { clickSound(); cancelHandover(); });

// Registration — self-service (name list selection)
document.getElementById('idle-register-link').addEventListener('click', () => { clickSound(); openRegister(); });
document.getElementById('register-cancel-btn').addEventListener('click', () => { clickSound(); cancelRegistration(); });
document.getElementById('register-next-btn').addEventListener('click', () => { clickSound(); submitRegistrationName(); });

// Registration — search/filter: filter the name list as the user types
document.getElementById('register-search').addEventListener('input', (e) => {
  const query = e.target.value.toLowerCase().trim();
  const items = document.querySelectorAll('.name-item');
  let visibleCount = 0;
  items.forEach(item => {
    const match = item.dataset.name.toLowerCase().includes(query);
    item.style.display = match ? '' : 'none';
    if (match) visibleCount++;
  });
  // Show "no results" hint when all items are filtered out
  const noResults = document.getElementById('register-no-results');
  noResults.style.display = visibleCount === 0 ? '' : 'none';
  noResults.textContent = 'No matches found. Contact an admin for manual registration.';
  // Deselect if the selected name is now hidden
  if (selectedRegistrantName) {
    const selectedEl = document.querySelector('.name-item.selected');
    if (selectedEl && selectedEl.style.display === 'none') {
      selectedEl.classList.remove('selected');
      selectedRegistrantName = null;
      document.getElementById('register-next-btn').disabled = true;
    }
  }
});

// Registration — admin manual (free-text input)
const regAdminInput = document.getElementById('register-name-admin');
regAdminInput.addEventListener('input', () => {
  document.getElementById('register-next-btn-admin').disabled = !regAdminInput.value.trim();
});
regAdminInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && regAdminInput.value.trim()) { clickSound(); submitRegistrationName(); }
});
document.getElementById('register-next-btn-admin').addEventListener('click', () => {
  clickSound(); submitRegistrationName();
});

// Admin panel — secret clock tap zone (5× tap within 3s)
document.querySelector('.clock').addEventListener('click', e => {
  e.stopPropagation();
  checkAdminTapSequence();
});

document.getElementById('admin-close').addEventListener('click', () => { clickSound(); dismissAdminToIdle(); });
document.getElementById('admin-goto-borrow').addEventListener('click', () => { clickSound(); adminGotoBorrow(); });
document.getElementById('admin-goto-return').addEventListener('click', () => { clickSound(); adminGotoReturn(); });
document.getElementById('admin-sync-source').addEventListener('click', () => { clickSound(); adminSyncSource(); });
document.getElementById('admin-register-user').addEventListener('click', () => { clickSound(); adminRegisterUser(); });
document.getElementById('admin-register-device').addEventListener('click', () => { clickSound(); adminRegisterDevice(); });
document.getElementById('admin-users').addEventListener('click', () => { clickSound(); adminOpenUsers(); });
document.getElementById('users-close').addEventListener('click', () => { clickSound(); closeUsersOverlay(); });
document.getElementById('users-confirm-back').addEventListener('click', () => { clickSound(); showUsersStep('users-step-list'); });
document.getElementById('users-confirm-submit').addEventListener('click', () => { clickSound(); usersDeactivate(); });
document.getElementById('users-list').addEventListener('click', e => {
  const replaceBtn = e.target.closest('[data-replace-id]');
  if (replaceBtn) { clickSound(); usersReplaceCard(Number(replaceBtn.dataset.replaceId)); return; }
  const deactBtn = e.target.closest('[data-deactivate-id]');
  if (deactBtn) { clickSound(); usersAskDeactivate(Number(deactBtn.dataset.deactivateId)); }
});
document.getElementById('bind-device-close').addEventListener('click', () => { clickSound(); closeRegisterDevice(); });
document.getElementById('bind-search').addEventListener('input', () => {
  clearTimeout(bindSearchTimer);
  bindSearchTimer = setTimeout(() => { populateBindList(false); }, 200);
});
document.getElementById('bind-add-open').addEventListener('click', () => { clickSound(); openAddFromExcel(); });
document.getElementById('bind-add-back').addEventListener('click', () => { clickSound(); showBindStep('bind-step-list'); populateBindList(); });
document.getElementById('bind-add-submit').addEventListener('click', () => { clickSound(); submitRegisterDevice(); });
document.getElementById('bind-slot-back').addEventListener('click', () => { clickSound(); showBindStep('bind-step-list'); });
document.getElementById('bind-slot-submit').addEventListener('click', () => { clickSound(); submitChangeSlot(); });
document.getElementById('admin-export-excel').addEventListener('click', () => { clickSound(); adminExportExcel(); });
document.getElementById('admin-update').addEventListener('click', () => { clickSound(); adminUpdate(); });
document.getElementById('admin-exit-kiosk').addEventListener('click', () => { clickSound(); adminExitKiosk(); });
document.getElementById('admin-shutdown').addEventListener('click', () => { clickSound(); adminShutdown(); });
document.getElementById('admin-end-session').addEventListener('click', () => { clickSound(); adminEndSession(); });
document.getElementById('update-dismiss').addEventListener('click', () => { clickSound(); dismissUpdateOverlay(); });
document.getElementById('power-dismiss').addEventListener('click', () => { clickSound(); hidePowerOverlay(); });

/* ============================================================
   INIT
============================================================ */
/**
 * Return a Promise that resolves after the specified delay. Utility for
 * simulating async delays in demo mode and sequencing UI transitions.
 * @param {number} ms - Delay in milliseconds.
 * @returns {Promise<void>} Resolves after the delay.
 */
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Overlays start hidden so they don't briefly flash on page load
document.getElementById('overlay-inactivity').style.display    = 'none';
document.getElementById('overlay-device-detail').style.display = 'none';
document.getElementById('overlay-admin').style.display         = 'none';
document.getElementById('overlay-register-device').style.display = 'none';
document.getElementById('overlay-users').style.display        = 'none';
document.getElementById('overlay-update').style.display        = 'none';
document.getElementById('overlay-power').style.display         = 'none';
document.getElementById('overlay-slot').style.display          = 'none';

// Enhancement E: split text on initial page load
initSplitText();

// Enhancement D: magnetic hover on action buttons — pointer-driven, so skip
// on the touch kiosk and in lite mode.
if (canHover && !PERF.lite) initMagneticHover();

// Seamless marquee: clone tracks to fill any viewport width. Kept in lite —
// the bar is a cheap translateX, and hiding it looked like a frozen ticker.
initMarquee();

/* ============================================================
   RUNTIME FPS PROBE — auto-downgrade to lite on a janky host
   Samples frame cadence in ~1.5s windows on the animated idle screen.
   Hardened against false downgrades (see MEM-20260614-1310): a WARM-UP
   delay skips the initial load/animation burst, and TWO CONSECUTIVE bad
   windows are required before switching to lite — so a single transient
   stall no longer strips the full UI. Skipped when already lite or when
   the user explicitly forced the full experience (?full).
   (On the Pi appliance the kiosk launches with ?lite, so this never runs
   there; it only guards mid-tier non-kiosk hosts.)
============================================================ */
function probePerformance() {
  if (PERF.lite || window.__FORCE_FULL__) return;

  const WINDOW_MS = 1500;       // length of one sample window
  const JANK_FRAME_MS = 22;     // a frame slower than this is ~below 45fps
  const JANK_SHARE = 0.35;      // window is "bad" if this share of frames are janky
  const WARMUP_MS = 1200;       // ignore the initial load/animation burst

  function sampleWindow(onDone) {
    let start = null, last = null, frames = 0, slow = 0;
    function tick(t) {
      if (PERF.lite) return;                     // already downgraded elsewhere
      if (start === null) { start = last = t; requestAnimationFrame(tick); return; }
      const dt = t - last; last = t; frames++;
      if (dt > JANK_FRAME_MS) slow++;
      if (t - start < WINDOW_MS) { requestAnimationFrame(tick); return; }
      onDone(frames >= 10 && slow / frames > JANK_SHARE);
    }
    requestAnimationFrame(tick);
  }

  // Warm up, then require two consecutive bad windows (hysteresis) to downgrade.
  setTimeout(() => sampleWindow((bad1) => {
    if (!bad1) return;                           // host holds up — stay full
    sampleWindow((bad2) => { if (bad2) enableLite(); });
  }), WARMUP_MS);
}
probePerformance();

/* ============================================================
   SSE — real-time events from backend (live mode only)
============================================================ */
/**
 * Establish a Server-Sent Events connection to the backend for real-time
 * push notifications. Handles auth success/failure, session end/timeout,
 * reader connect/disconnect, and registration success/failure events.
 * Automatically reconnects after 3 seconds on connection error.
 */
function connectSSE() {
  const source = new EventSource('/api/events');

  source.addEventListener('auth_success', e => {
    if (S.updating) return;
    clearAfterRegisterTimer();
    dismissSlotOverlay();
    const data = JSON.parse(e.data);
    S.user = data.user;
    fillMainMenu(data.user);
    navigate('main-menu');
    armIdle();
  });

  source.addEventListener('auth_failed', () => {
    if (S.updating) return;
    dismissSlotOverlay();
    showAuthFailed();
  });

  source.addEventListener('session_ended', () => {
    if (S.updating) return;
    endSession(false, true);
  });

  source.addEventListener('session_timeout', () => {
    if (S.updating) return;
    // After admin enroll the backend drops the overlay session immediately so
    // the next work-card tap is login. The NFC bridge then sees "session gone"
    // and would emit timeout; ignore it while the welcome/error step is up.
    if (S.screen === 'register') return;
    endSession(true, true);
  });

  source.addEventListener('reader_disconnected', () => {
    if (S.updating) return;
    showToast('NFC reader disconnected', 'error');
  });

  source.addEventListener('reader_connected', () => {
    if (S.updating) return;
    showToast('NFC reader reconnected', 'success');
  });

  source.addEventListener('registration_success', e => {
    if (S.updating) return;
    const data = JSON.parse(e.data);
    // Payload-first routing: every replace-path result carries replaced:true
    // (app_context.py), so no pending-flag fallback is needed — and it would
    // misroute an enrol result landing mid-flight to the Users overlay.
    if (data.replaced === true) { handleUsersReplaceResult(data, true); return; }
    handleRegistrationSuccess(data);
  });

  source.addEventListener('registration_failed', e => {
    if (S.updating) return;
    const data = JSON.parse(e.data);
    // Payload-first routing: see registration_success above.
    if (data.replaced === true) { handleUsersReplaceResult(data, false); return; }
    handleRegistrationFailed(data);
  });

  source.addEventListener('device_action', e => {
    if (S.updating) return;
    const data = JSON.parse(e.data);
    keepSessionAliveFromTag();
    if (data.success && data.action === 'return') {
      showSlotOverlay(data.device_name, data.locker_slot);
    } else {
      showToast(data.message || '', data.success ? 'success' : 'error');
    }
    if (S.user) refreshAfterDeviceAction(data);
  });

  source.addEventListener('handover_requested', e => {
    if (S.updating) return;
    const data = JSON.parse(e.data);
    keepSessionAliveFromTag();
    openHandover(data);
  });

  source.addEventListener('device_tag_idle', e => {
    if (S.updating) return;
    const data = JSON.parse(e.data);
    keepSessionAliveFromTag();
    showToast(data.message || 'Tap your work card first.');
  });

  source.addEventListener('unknown_tag', e => {
    if (S.updating) return;
    const data = JSON.parse(e.data);
    keepSessionAliveFromTag();
    showToast(data.message || 'Unknown tag.');
  });

  source.addEventListener('tag_bind_success', e => {
    if (S.updating) return;
    handleTagBindSuccess(JSON.parse(e.data));
  });

  source.addEventListener('tag_bind_failed', e => {
    if (S.updating) return;
    handleTagBindFailed(JSON.parse(e.data));
  });

  source.onerror = () => {
    source.close();
    setTimeout(connectSSE, 3000); // 3s delay before reconnecting after SSE error
  };
}

/**
 * Check if a user session is already active on the backend (handles browser refresh).
 * Restores the main menu only for a real work-card (or admin shortcut) session.
 * A leftover 5-tap overlay is not a login: stay on idle and do not POST overlay=false.
 * @returns {Promise<void>}
 */
async function checkExistingSession() {
  try {
    const res = await fetch('/api/session');
    const data = await res.json();
    // Leftover 5-tap overlay is not a work-card login. Do not open the
    // Locker menu or POST overlay=false (device tags would borrow/return
    // as that admin with no card).
    if (data.active && data.user && !data.overlay) {
      S.user = data.user;
      fillMainMenu(data.user);
      navigate('main-menu');
      armIdle();
      await fetch('/api/admin/session?overlay=false', { method: 'POST' }).catch(() => {});
    }
  } catch (_) { /* server not reachable — stay on idle */ }
}

if (USE_DEMO) {
  // In demo mode, clicking anywhere on the idle screen simulates a card tap
  document.getElementById('screen-idle').addEventListener('click', (e) => {
    // Don't intercept the register link
    if (e.target.closest('.idle-register-link')) return;
    handleTap();
  });
} else {
  loadSiteConfig();
  connectSSE();
  checkExistingSession();
  reportKioskDisplay('idle');
}

/* ============================================================
   DEV / SIMULATION — no-hardware tap injection
   Activates ONLY when the backend reports the fake NFC reader is
   running (SMART_LOCKER_FAKE_READER). In production /api/dev/status
   returns fake_reader:false, so nothing below is wired up and there
   is zero visible footprint. Provides a floating "Simulate tap"
   button plus the F2 keyboard shortcut; both POST /api/dev/tap, which
   flows through the real NFC bridge exactly like a physical card tap
   (work-card login/logout, device-tag auto-intent, or pending bind).
============================================================ */
(function initDevTap() {
  fetch('/api/dev/status')
    .then(r => (r.ok ? r.json() : null))
    .then(status => {
      if (!status || !status.fake_reader) return;   // not in simulation mode

      async function simulateTap(uid) {
        try {
          const res = await fetch('/api/dev/tap', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(uid ? { uid } : {}),
          });
          if (res.status === 400) {
            // No default UID configured on the server — ask once and retry.
            const entered = prompt('Simulated card UID (hex):');
            if (entered) return simulateTap(entered.trim());
            return;
          }
          if (!res.ok) showToast('Simulated tap failed', 'error');
          // On success the auth_success / auth_failed SSE event drives the UI.
        } catch (_) {
          showToast('Simulated tap request failed', 'error');
        }
      }

      const btn = document.createElement('button');
      btn.id = 'dev-tap-btn';
      btn.type = 'button';
      btn.textContent = '⊙ Simulate tap (F2)';
      btn.setAttribute('aria-label', 'Simulate an NFC card tap (developer tool)');
      Object.assign(btn.style, {
        position: 'fixed', right: '12px', bottom: '12px', zIndex: '9999',
        padding: '8px 12px', font: '600 13px Inter, system-ui, sans-serif',
        color: '#fff', background: 'rgba(150,20,20,.85)',
        border: '1px solid rgba(255,255,255,.35)', borderRadius: '8px',
        cursor: 'pointer', letterSpacing: '.02em',
      });
      btn.addEventListener('click', () => simulateTap());
      document.body.appendChild(btn);

      document.addEventListener('keydown', (e) => {
        if (e.key === 'F2') { e.preventDefault(); simulateTap(); }
      });

      console.info('[sim] Fake NFC reader active — press F2 or the corner button to inject a tap.');
    })
    .catch(() => { /* dev status unavailable — ignore */ });
})();
