/**
 * @fileoverview Client-side state machine for the kiosk touch UI. Manages screen
 *               transitions, API communication, SSE event handling, and user
 *               interaction flow across idle, auth, menu, borrow, return, detail,
 *               registration, and admin screens.
 * @project smart_locker/frontend
 * @description Includes demo mode with mock data, landonorris.com-inspired
 *              animations (circle reveals, character-split text, magnetic hover,
 *              image parallax), inactivity countdown, and self-registration flow.
 */

/* ============================================================
   STATE — single source of truth for the UI
============================================================ */
/** Whether to use demo mode with mock data. Enabled by adding ?demo to the URL. */
const USE_DEMO = new URLSearchParams(window.location.search).has('demo');

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
  { id:1, pm_number:'PM-001', name:'PM-001 Keysight DSOX3054T',  device_type:'Oscilloscope',   serial_number:'MY12345678',  manufacturer:'Keysight',       model:'DSOX3054T',   barcode:'490001', locker_slot:1,  description:null, image_path:null, calibration_due:'2026-09-15', status:'available',   borrower_name:null       },
  { id:2, pm_number:'PM-002', name:'PM-002 Rohde & Schwarz HMC8043', device_type:'Power Supply', serial_number:'RS-HMC-042', manufacturer:'Rohde & Schwarz', model:'HMC8043',    barcode:'490002', locker_slot:2,  description:null, image_path:null, calibration_due:'2026-11-01', status:'borrowed',    borrower_name:'Sarah K.' },
  { id:3, pm_number:'PM-003', name:'PM-003 Fluke 87V',           device_type:'Multimeter',     serial_number:'FL-87V-007',  manufacturer:'Fluke',          model:'87V',         barcode:'490003', locker_slot:3,  description:null, image_path:null, calibration_due:'2026-06-30', status:'available',   borrower_name:null       },
  { id:4, pm_number:'PM-004', name:'PM-004 Keysight 34465A',     device_type:'Multimeter',     serial_number:'MY98765432',  manufacturer:'Keysight',       model:'34465A',      barcode:'490004', locker_slot:4,  description:null, image_path:null, calibration_due:null,         status:'available',   borrower_name:null       },
  { id:5, pm_number:'PM-005', name:'PM-005 Fluke i400s',         device_type:'Current Probe',  serial_number:null,          manufacturer:'Fluke',          model:'i400s',       barcode:'490005', locker_slot:5,  description:null, image_path:null, calibration_due:'2027-01-15', status:'borrowed',    borrower_name:'You'      },
  { id:6, pm_number:'PM-006', name:'PM-006 Tektronix TBS2104X',  device_type:'Oscilloscope',   serial_number:'TEK-TBS-099', manufacturer:'Tektronix',      model:'TBS2104X',    barcode:'490006', locker_slot:6,  description:null, image_path:null, calibration_due:'2026-08-20', status:'available',   borrower_name:null       },
  { id:7, pm_number:'PM-007', name:'PM-007 Hioki DT4282',        device_type:'Multimeter',     serial_number:null,          manufacturer:'Hioki',          model:'DT4282',      barcode:'490007', locker_slot:7,  description:null, image_path:null, calibration_due:null,         status:'available',   borrower_name:null       },
  { id:8, pm_number:'PM-008', name:'PM-008 Megger MIT485/2',     device_type:'Insulation Tester', serial_number:'MEG-485-002', manufacturer:'Megger',      model:'MIT485/2',    barcode:'490008', locker_slot:8,  description:null, image_path:null, calibration_due:'2026-12-01', status:'maintenance', borrower_name:null       },
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
 * come from the "Aktueller Einsatzort" column in the source Excel and are
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
 * @returns {Promise<Object>} Result with success boolean and optional detail.
 */
async function apiStartAdminRegistration(name) {
  if (USE_DEMO) {
    await sleep(400); // simulate API round-trip
    return { success: true };
  }
  try {
    const res = await fetch('/api/admin/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
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
    toEl.style.display = '';
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
      void fromEl.offsetHeight;          // force reflow — lock in new origin
      fromEl.classList.add('exit');
      setTimeout(() => fromEl.classList.remove('active', 'exit'), 1300); // matches CSS --t-reveal (1.3s) transition
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
  S.idleTimer = setTimeout(showInactivity, (S.cdSeconds - S.cdWarnAt) * 1000);
}

/**
 * Display the inactivity countdown overlay with a slot-machine countdown timer.
 * When the countdown reaches zero, the session ends automatically.
 */
function showInactivity() {
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

['click', 'touchstart', 'keydown'].forEach(evt =>
  document.addEventListener(evt, () => {
    if (S.screen === 'idle') return;
    armIdle();
    // Ping backend to reset server-side inactivity timer
    if (!USE_DEMO && S.user) fetch('/api/session/touch', { method: 'POST' }).catch(() => {});
  })
);

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
  clearInterval(S.cdTimer);
  dismissInactivity();
  if (!fromSSE) await apiEndSession();
  S.user     = null;
  S.devices  = [];
  S.selected = null;
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
}

/* ============================================================
   DEVICE GRID — borrow and return screens
============================================================ */
/**
 * Open the borrow screen, fetch the current device list from the API, update
 * the borrow count badge, and build the device card grid.
 * @returns {Promise<void>}
 */
async function openBorrow() {
  navigate('borrow');
  const devices = await apiGetDevices();
  S.devices = devices;
  const myCount = devices.filter(d => d.borrower_name === 'You').length;
  // 5 = MAX_BORROWS default from config.settings (must match server setting)
  document.getElementById('borrow-badge').textContent = `${myCount} / 5 borrowed`;
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
  buildGrid('return-grid', devices, 'return');
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

  // IntersectionObserver: cards animate in/out as they scroll into view
  const scrollRoot = grid.closest('.grid-wrapper');
  const cardObserver = new IntersectionObserver((entries) => {
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
  }, { root: scrollRoot, threshold: 0.15, rootMargin: '40px 0px' }); // 15% visible triggers animation; 40px buffer for smooth entry

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
  scrollRoot.addEventListener('scroll', () => {
    if (!grid._rafId) grid._rafId = requestAnimationFrame(onGridScroll);
  }, { passive: true });
  grid._scrollObs = cardObserver;

  sorted.forEach((dev, i) => {
    const avail = dev.status === 'available';
    const mine  = dev.borrower_name === 'You';
    const maint = dev.status === 'maintenance';

    let cls = 'device-card';
    if (mode === 'borrow') cls += avail ? ' available' : ' unavailable';
    else                   cls += mine  ? ' mine'      : ' unavailable';

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
        ${dev.image_path ? `
        <div class="card-hover-reveal">
          <div class="card-hover-reveal-img" style="background-image:url('${dev.image_path}')"></div>
          <div class="card-hover-icon">
            <svg viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
          </div>
        </div>` : ''}
        <div class="card-slot">${slotLabel}</div>
        <div class="card-status ${statusCls}">${statusTxt}</div>
      </div>
      <div class="card-body">
        <div class="card-name">${dev.name}</div>
        <div class="card-type">${dev.device_type}</div>
      </div>
    `;

    // Observe card for scroll-triggered entrance
    cardObserver.observe(card);

    // Track for scroll parallax
    const cardImg = card.querySelector('.card-image img');
    if (cardImg) parallaxCards.push({ card, img: cardImg });

    // Mouse parallax on hover (layered on top of scroll parallax)
    if (cardImg) {
      card.addEventListener('mousemove', e => {
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

  // Trigger initial parallax positioning
  requestAnimationFrame(onGridScroll);
}

/* ============================================================
   DEVICE DETAIL OVERLAY
============================================================ */
/**
 * Open the device detail overlay with full information about a device. Populates
 * all detail fields (name, type, serial, image, status, description) and configures
 * the confirm button based on device availability and current mode.
 * @param {Object} dev - The device object to display details for.
 * @param {string} mode - Either 'borrow' or 'return', determines confirm button behavior.
 */
function openDetail(dev, mode) {
  S.selected   = dev;
  S.mode       = mode;
  S.prevScreen = S.screen;

  const avail = dev.status === 'available';
  const mine  = dev.borrower_name === 'You';
  const maint = dev.status === 'maintenance';
  const slot  = `S${String(dev.locker_slot ?? 0).padStart(2, '0')}`;

  document.getElementById('detail-slot-tag').textContent =
    `SLOT ${String(dev.locker_slot ?? 0).padStart(2, '0')}`;
  document.getElementById('detail-name').textContent    = dev.name;
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
 * the previous screen (borrow or return grid).
 */
function closeDetail() {
  const overlay = document.getElementById('overlay-device-detail');
  overlay.classList.add('hidden-right');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-right');
    overlay.style.display = 'none';
  }, 710);
  S.screen = S.prevScreen || (S.mode === 'return' ? 'return' : 'borrow');
}

/**
 * Execute the borrow or return action for the currently selected device. Disables
 * the button during the request, shows a toast with the result, then refreshes the grid.
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

  closeDetail();
  showToast(result.message, result.success ? 'success' : 'error');

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
   CUSTOM CURSOR — Enhancement D: glow on interactive elements
============================================================ */
const cursorDot  = document.getElementById('cursor');
const cursorRing = document.getElementById('cursor-ring');
let mx = 0, my = 0, rx = 0, ry = 0;

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
});

/**
 * Animate the cursor ring to follow the cursor dot with an eased lag.
 * Runs as a self-invoking requestAnimationFrame loop.
 */
(function animRing() {
  rx += (mx - rx) * 0.13; // 0.13 = easing factor — lower values increase lag
  ry += (my - ry) * 0.13;
  cursorRing.style.left = rx + 'px';
  cursorRing.style.top  = ry + 'px';
  requestAnimationFrame(animRing);
})();

/* ============================================================
   Enhancement D: MAGNETIC HOVER on action buttons
   Buttons subtly shift toward the cursor position on hover.
============================================================ */
/**
 * Initialize magnetic hover effect on action buttons, back buttons, close buttons,
 * stay button, and confirm button. Buttons subtly shift toward the cursor on hover,
 * clamped to +/-4px on action buttons to prevent overlap with neighbors.
 */
function initMagneticHover() {
  document.querySelectorAll('.action-btn').forEach(btn => {
    btn.addEventListener('mousemove', e => {
      const rect = btn.getBoundingClientRect();
      const x = e.clientX - rect.left - rect.width / 2;
      const y = e.clientY - rect.top - rect.height / 2;
      // Clamp to ±4px so adjacent buttons never overlap
      const tx = Math.max(-4, Math.min(4, x * 0.04)); // 0.04 sensitivity, ±4px max shift
      const ty = Math.max(-4, Math.min(4, y * 0.04));
      btn.style.transform = `translate(${tx}px, ${ty}px) scale(1.01)`;
    });
    btn.addEventListener('mouseleave', () => {
      btn.style.transform = '';
    });
  });

  // Also on back buttons, close button, stay button
  document.querySelectorAll('.back-btn, .detail-close, .stay-btn, .confirm-btn').forEach(btn => {
    btn.addEventListener('mousemove', e => {
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

  if (S.adminRegistration) {
    // Admin manual registration — show free-text input
    const input = document.getElementById('register-name-admin');
    input.value = '';
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
 * Items are stagger-animated on entrance.
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
    // Stagger entrance animation delay (capped at 0.6s for long lists)
    btn.style.transitionDelay = `${Math.min(i * 0.03, 0.6)}s`;
    btn.addEventListener('click', () => { clickSound(); selectRegistrantName(name, btn); });
    list.appendChild(btn);
  });

  // Trigger entrance animation after a frame so the initial state is captured
  requestAnimationFrame(() => {
    list.querySelectorAll('.name-item').forEach(el => el.classList.add('in'));
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

  if (S.adminRegistration) {
    // Admin manual registration — get name from text input
    name = document.getElementById('register-name-admin').value.trim();
    if (!name) return;
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
      setTimeout(() => navigateAfterRegistration(), 3500);
    }
  }, 1000);

  // Tell backend to await the next NFC tap for registration
  const result = await endpoint(name);
  if (!result.success) {
    clearInterval(registerCountdownTimer);
    showRegisterStep('register-step-error');
    document.getElementById('register-error-msg').textContent =
      result.detail || result.message || 'Could not start registration.';
    setTimeout(() => navigateAfterRegistration(), 3500);
  }
}

/**
 * Navigate to the appropriate screen after registration completes or fails.
 * In admin mode, returns to the main menu (admin session persists). In
 * self-service mode, returns to the idle screen.
 */
function navigateAfterRegistration() {
  if (S.adminRegistration) {
    S.adminRegistration = false;
    navigate('main-menu');
  } else {
    navigate('idle');
  }
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
    `Welcome, ${data.user.name}! You can now tap your card to log in.`;
  setTimeout(() => navigateAfterRegistration(), 4000);
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
  setTimeout(() => navigateAfterRegistration(), 4000);
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
    closeAdminPanel();
  } else {
    openAdminPanel();
  }
}

/**
 * Open the admin panel overlay with a polygon-wipe entrance animation.
 * Plays a click sound on activation.
 */
function openAdminPanel() {
  clickSound();
  const overlay = document.getElementById('overlay-admin');
  overlay.style.display = '';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    overlay.classList.add('visible');
  }));
}

/**
 * Close the admin panel overlay with a slide-left exit animation.
 */
function closeAdminPanel() {
  const overlay = document.getElementById('overlay-admin');
  overlay.classList.add('hidden-left');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-left');
    overlay.style.display = 'none';
  }, 710);
}

/**
 * Start a real backend admin session via POST /api/admin/session. The backend
 * finds the first active admin user in the database and creates a server-side
 * session so that subsequent API calls (borrow, return, sync) pass the
 * require_session check. Falls back to a demo-mode synthetic user when
 * USE_DEMO is true.
 * @returns {Promise<boolean>} True if the admin session was created, false on failure.
 */
async function adminStartSession() {
  if (USE_DEMO) {
    // Demo mode — no backend, use synthetic admin user
    adminSessionActive = true;
    S.user = { id: 0, name: 'Admin', role: 'admin' };
    fillMainMenu(S.user);
    return true;
  }

  try {
    const res = await fetch('/api/admin/session', { method: 'POST' });
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
 * navigate to the main menu, and then open the borrow screen. If the backend
 * session creation fails (e.g. no admin users enrolled), the panel closes but
 * navigation is aborted so the user stays on the current screen.
 * @returns {Promise<void>}
 */
async function adminGotoBorrow() {
  closeAdminPanel();
  const ok = await adminStartSession();
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
  const ok = await adminStartSession();
  if (!ok) return; // session creation failed — stay on current screen
  await sleep(300); // wait for panel close animation
  navigate('main-menu');
  await sleep(200); // wait for circle-reveal transition
  openReturn();
  armIdle();
}

/**
 * Trigger a manual source Excel sync from the admin panel. Updates the button
 * label to "Syncing..." during the request and shows a toast with the result.
 * @returns {Promise<void>}
 */
async function adminSyncSource() {
  const btn = document.getElementById('admin-sync-source');
  const label = btn.querySelector('.admin-btn-label');
  const origText = label.textContent;
  label.textContent = 'Syncing…';
  btn.style.pointerEvents = 'none';

  try {
    const res = await fetch('/api/admin/sync-source', { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      showToast(`Synced: ${data.imported} new, ${data.updated} updated`, 'success');
    } else {
      showToast(data.detail || 'Sync failed', 'error');
    }
  } catch (_) {
    showToast('Sync request failed', 'error');
  }

  label.textContent = origText;
  btn.style.pointerEvents = '';
}

/**
 * Admin shortcut: close the admin panel and open the registration screen in
 * admin mode (free-text name entry, bypasses registrant list validation).
 * The admin session remains active so the backend accepts the registration.
 * @returns {Promise<void>}
 */
async function adminRegisterUser() {
  closeAdminPanel();
  S.adminRegistration = true;
  await sleep(300); // wait for panel close animation
  openRegister();
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

document.getElementById('admin-close').addEventListener('click', () => { clickSound(); closeAdminPanel(); });
document.getElementById('admin-goto-borrow').addEventListener('click', () => { clickSound(); adminGotoBorrow(); });
document.getElementById('admin-goto-return').addEventListener('click', () => { clickSound(); adminGotoReturn(); });
document.getElementById('admin-sync-source').addEventListener('click', () => { clickSound(); adminSyncSource(); });
document.getElementById('admin-register-user').addEventListener('click', () => { clickSound(); adminRegisterUser(); });
document.getElementById('admin-end-session').addEventListener('click', () => { clickSound(); adminEndSession(); });

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

// Enhancement E: split text on initial page load
initSplitText();

// Enhancement D: magnetic hover on action buttons
initMagneticHover();

// Seamless marquee: clone tracks to fill any viewport width
initMarquee();

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
    const data = JSON.parse(e.data);
    S.user = data.user;
    fillMainMenu(data.user);
    navigate('main-menu');
    armIdle();
  });

  source.addEventListener('auth_failed', () => {
    showAuthFailed();
  });

  source.addEventListener('session_ended', () => {
    endSession(false, true);
  });

  source.addEventListener('session_timeout', () => {
    endSession(true, true);
  });

  source.addEventListener('reader_disconnected', () => {
    showToast('NFC reader disconnected', 'error');
  });

  source.addEventListener('reader_connected', () => {
    showToast('NFC reader reconnected', 'success');
  });

  source.addEventListener('registration_success', e => {
    const data = JSON.parse(e.data);
    handleRegistrationSuccess(data);
  });

  source.addEventListener('registration_failed', e => {
    const data = JSON.parse(e.data);
    handleRegistrationFailed(data);
  });

  source.onerror = () => {
    source.close();
    setTimeout(connectSSE, 3000); // 3s delay before reconnecting after SSE error
  };
}

/**
 * Check if a user session is already active on the backend (handles browser refresh).
 * If an active session exists, restores the UI to the main menu for that user.
 * @returns {Promise<void>}
 */
async function checkExistingSession() {
  try {
    const res = await fetch('/api/session');
    const data = await res.json();
    if (data.active && data.user) {
      S.user = data.user;
      fillMainMenu(data.user);
      navigate('main-menu');
      armIdle();
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
  connectSSE();
  checkExistingSession();
}
