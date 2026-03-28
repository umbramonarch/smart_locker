/* ============================================================
   STATE — single source of truth for the UI
============================================================ */
const USE_DEMO = false; // true = demo data, false = real API + SSE

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
  { id:1, pm_number:'PM-001', name:'PM-001 Keysight DSOX3054T',  device_type:'Oscilloscope',   serial_number:'MY12345678',  manufacturer:'Keysight',       model:'DSOX3054T',   barcode:'490001', locker_slot:1,  description:null, image_path:null, calibration_due:'2026-09-15', status:'available',   borrower_name:null       },
  { id:2, pm_number:'PM-002', name:'PM-002 Rohde & Schwarz HMC8043', device_type:'Power Supply', serial_number:'RS-HMC-042', manufacturer:'Rohde & Schwarz', model:'HMC8043',    barcode:'490002', locker_slot:2,  description:null, image_path:null, calibration_due:'2026-11-01', status:'borrowed',    borrower_name:'Sarah K.' },
  { id:3, pm_number:'PM-003', name:'PM-003 Fluke 87V',           device_type:'Multimeter',     serial_number:'FL-87V-007',  manufacturer:'Fluke',          model:'87V',         barcode:'490003', locker_slot:3,  description:null, image_path:null, calibration_due:'2026-06-30', status:'available',   borrower_name:null       },
  { id:4, pm_number:'PM-004', name:'PM-004 Keysight 34465A',     device_type:'Multimeter',     serial_number:'MY98765432',  manufacturer:'Keysight',       model:'34465A',      barcode:'490004', locker_slot:4,  description:null, image_path:null, calibration_due:null,         status:'available',   borrower_name:null       },
  { id:5, pm_number:'PM-005', name:'PM-005 Fluke i400s',         device_type:'Current Probe',  serial_number:null,          manufacturer:'Fluke',          model:'i400s',       barcode:'490005', locker_slot:5,  description:null, image_path:null, calibration_due:'2027-01-15', status:'borrowed',    borrower_name:'You'      },
  { id:6, pm_number:'PM-006', name:'PM-006 Tektronix TBS2104X',  device_type:'Oscilloscope',   serial_number:'TEK-TBS-099', manufacturer:'Tektronix',      model:'TBS2104X',    barcode:'490006', locker_slot:6,  description:null, image_path:null, calibration_due:'2026-08-20', status:'available',   borrower_name:null       },
  { id:7, pm_number:'PM-007', name:'PM-007 Hioki DT4282',        device_type:'Multimeter',     serial_number:null,          manufacturer:'Hioki',          model:'DT4282',      barcode:'490007', locker_slot:7,  description:null, image_path:null, calibration_due:null,         status:'available',   borrower_name:null       },
  { id:8, pm_number:'PM-008', name:'PM-008 Megger MIT485/2',     device_type:'Insulation Tester', serial_number:'MEG-485-002', manufacturer:'Megger',      model:'MIT485/2',    barcode:'490008', locker_slot:8,  description:null, image_path:null, calibration_due:'2026-12-01', status:'maintenance', borrower_name:null       },
];

/* ============================================================
   API — real fetch() calls with demo fallback
============================================================ */
async function apiAuthTap(uid_hmac) {
  if (USE_DEMO) {
    await sleep(380);
    const user = DEMO_USERS[demoUserIdx++ % DEMO_USERS.length];
    return { success: true, user };
  }
  // In live mode, auth comes via SSE — this is only called in demo mode
  return { success: false };
}

async function apiGetDevices() {
  if (USE_DEMO) { await sleep(280); return DEMO_DEVICES; }
  const res = await fetch('/api/devices');
  if (!res.ok) return [];
  return await res.json();
}

async function apiBorrow(device_id) {
  if (USE_DEMO) {
    await sleep(480);
    const d = S.devices.find(x => x.id === device_id);
    if (d) { d.status = 'borrowed'; d.borrower_name = 'You'; }
    return { success: true, message: `${d?.name ?? 'Device'} borrowed.` };
  }
  const res = await fetch(`/api/devices/${device_id}/borrow`, { method: 'POST' });
  return await res.json();
}

async function apiReturn(device_id) {
  if (USE_DEMO) {
    await sleep(480);
    const d = S.devices.find(x => x.id === device_id);
    if (d) { d.status = 'available'; d.borrower_name = null; }
    return { success: true, message: `${d?.name ?? 'Device'} returned.` };
  }
  const res = await fetch(`/api/devices/${device_id}/return`, { method: 'POST' });
  return await res.json();
}

async function apiEndSession() {
  if (USE_DEMO) { await sleep(200); return; }
  await fetch('/api/session/end', { method: 'POST' }).catch(() => {});
}

async function apiStartRegistration(name) {
  if (USE_DEMO) {
    await sleep(400);
    return { success: true };
  }
  const res = await fetch('/api/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  return await res.json();
}

async function apiCancelRegistration() {
  if (USE_DEMO) return;
  await fetch('/api/register/cancel', { method: 'POST' }).catch(() => {});
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

function getEl(id) {
  return document.getElementById('screen-' + id)
      || document.getElementById('overlay-' + id);
}

function setRevealOrigin(el) {
  if (S.lastClickX != null) {
    const xPct = ((S.lastClickX / window.innerWidth) * 100).toFixed(1) + '%';
    const yPct = ((S.lastClickY / window.innerHeight) * 100).toFixed(1) + '%';
    el.style.setProperty('--reveal-x', xPct);
    el.style.setProperty('--reveal-y', yPct);
  }
}

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
      setTimeout(() => fromEl.classList.remove('active', 'exit'), 1300);
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

function triggerSplitText(screenEl) {
  // The .active class on the parent screen triggers the CSS animation
  // via .screen.active .split-char selector
}

// Initialize split text on page load for idle screen elements
function initSplitText() {
  // Idle headline: "TAP YOUR CARD"
  const idleHeadline = document.querySelector('.idle-headline .reveal-inner');
  if (idleHeadline) splitTextIntoChars(idleHeadline);

  // Idle sub: "to access the equipment locker"
  const idleSub = document.querySelector('.idle-sub .reveal-inner');
  if (idleSub) splitTextIntoChars(idleSub);
}

// Split menu text when navigating to main-menu (called after content is set)
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
        setTimeout(() => { inner.textContent = ch; }, 175);
      }
    }
  });
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
   INACTIVITY TIMER — with slot-machine countdown
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
  const numEl = document.getElementById('inactivity-num');
  updateSlotNumber(numEl, secs);
  requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add('visible')));
  S.cdTimer = setInterval(() => {
    secs--;
    updateSlotNumber(numEl, secs);
    if (secs <= 0) { clearInterval(S.cdTimer); endSession(true); }
  }, 1000);
}

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
  const bar = document.getElementById('auth-progress');
  bar.style.transition = 'none';
  bar.style.width = '100%';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    bar.style.transition = 'width 3s linear';
    bar.style.width = '0%';
  }));
  setTimeout(() => navigate('idle'), 3100);
}

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

  // Disconnect previous observer if any
  if (grid._scrollObs) { grid._scrollObs.disconnect(); grid._scrollObs = null; }

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
  }, { root: scrollRoot, threshold: 0.15, rootMargin: '40px 0px' });

  // Scroll parallax: shift card images based on scroll position
  const parallaxCards = [];
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
        cardImg.style.transform = `scale(1.15) translate(${x * -14}px, ${y * -14}px)`;
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
  document.getElementById('detail-status').textContent  =
    maint ? 'Under Maintenance' : avail ? 'Available' : mine ? 'Borrowed by You' : 'In Use';
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

function closeDetail() {
  const overlay = document.getElementById('overlay-device-detail');
  overlay.classList.add('hidden-right');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-right');
    overlay.style.display = 'none';
  }, 710);
  S.screen = S.prevScreen || (S.mode === 'return' ? 'return' : 'borrow');
}

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
let toastTimer;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  clearTimeout(toastTimer);
  el.textContent = msg;
  el.className = `show${type ? ' toast-' + type : ''}`;
  toastTimer = setTimeout(() => { el.className = ''; }, 3200);
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

(function animRing() {
  rx += (mx - rx) * 0.13;
  ry += (my - ry) * 0.13;
  cursorRing.style.left = rx + 'px';
  cursorRing.style.top  = ry + 'px';
  requestAnimationFrame(animRing);
})();

/* ============================================================
   Enhancement D: MAGNETIC HOVER on action buttons
   Buttons subtly shift toward the cursor position on hover.
============================================================ */
function initMagneticHover() {
  document.querySelectorAll('.action-btn').forEach(btn => {
    btn.addEventListener('mousemove', e => {
      const rect = btn.getBoundingClientRect();
      const x = e.clientX - rect.left - rect.width / 2;
      const y = e.clientY - rect.top - rect.height / 2;
      // Clamp to ±4px so adjacent buttons never overlap
      const tx = Math.max(-4, Math.min(4, x * 0.04));
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
      btn.style.transform = `translate(${x * 0.18}px, ${y * 0.18}px)`;
    });
    btn.addEventListener('mouseleave', () => {
      btn.style.transform = '';
    });
  });
}

/* ============================================================
   REGISTRATION FLOW
============================================================ */
let registerCountdownTimer = null;

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

function openRegister() {
  // Reset state
  document.getElementById('register-name').value = '';
  document.getElementById('register-next-btn').disabled = true;
  showRegisterStep('register-step-name');
  clearInterval(registerCountdownTimer);
  navigate('register');
  // Focus the input after the transition
  setTimeout(() => document.getElementById('register-name').focus(), 800);
}

async function submitRegistrationName() {
  const nameInput = document.getElementById('register-name');
  const name = nameInput.value.trim();
  if (!name) return;

  const btn = document.getElementById('register-next-btn');
  btn.disabled = true;

  document.getElementById('register-confirm-name').textContent = name;
  showRegisterStep('register-step-tap');

  // Start countdown
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
      setTimeout(() => navigate('idle'), 3500);
    }
  }, 1000);

  // Tell backend to await next NFC tap for registration
  const result = await apiStartRegistration(name);
  if (!result.success) {
    clearInterval(registerCountdownTimer);
    showRegisterStep('register-step-error');
    document.getElementById('register-error-msg').textContent =
      result.detail || result.message || 'Could not start registration.';
    setTimeout(() => navigate('idle'), 3500);
  }
}

function cancelRegistration() {
  clearInterval(registerCountdownTimer);
  apiCancelRegistration();
  navigate('idle');
}

function handleRegistrationSuccess(data) {
  clearInterval(registerCountdownTimer);
  if (S.screen !== 'register') return;
  showRegisterStep('register-step-success');
  document.getElementById('register-success-msg').textContent =
    `Welcome, ${data.user.name}! You can now tap your card to log in.`;
  setTimeout(() => navigate('idle'), 4000);
}

function handleRegistrationFailed(data) {
  clearInterval(registerCountdownTimer);
  if (S.screen !== 'register') return;
  showRegisterStep('register-step-error');
  document.getElementById('register-error-msg').textContent =
    data.reason || 'Registration failed. Please try again.';
  setTimeout(() => navigate('idle'), 4000);
}

/* ============================================================
   AUDIO CLICK FEEDBACK
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

// Registration
document.getElementById('idle-register-link').addEventListener('click', () => { clickSound(); openRegister(); });
document.getElementById('register-cancel-btn').addEventListener('click', () => { clickSound(); cancelRegistration(); });
document.getElementById('register-next-btn').addEventListener('click', () => { clickSound(); submitRegistrationName(); });

const regInput = document.getElementById('register-name');
regInput.addEventListener('input', () => {
  document.getElementById('register-next-btn').disabled = !regInput.value.trim();
});
regInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && regInput.value.trim()) { clickSound(); submitRegistrationName(); }
});

/* ============================================================
   INIT
============================================================ */
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Overlays start hidden so they don't briefly flash on page load
document.getElementById('overlay-inactivity').style.display    = 'none';
document.getElementById('overlay-device-detail').style.display = 'none';

// Enhancement E: split text on initial page load
initSplitText();

// Enhancement D: magnetic hover on action buttons
initMagneticHover();

/* ============================================================
   SSE — real-time events from backend (live mode only)
============================================================ */
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
    setTimeout(connectSSE, 3000);
  };
}

// On page load: check if a session is already active (handles browser refresh)
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

if (!USE_DEMO) {
  connectSSE();
  checkExistingSession();
}
