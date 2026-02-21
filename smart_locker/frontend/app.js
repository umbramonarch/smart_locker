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
  lastClickX: null,     // Enhancement C: track click origin for circle reveal
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
  { id:1, name:'Canon EOS R5',      device_type:'Camera',         serial_number:'CNR5-001',        locker_slot:1, description:'Full-frame mirrorless camera. Handle with care. Return with battery charged.',                    image_path:'images/camera.jpg',         status:'available',   borrower_name:null      },
  { id:2, name:'DJI Mavic 3',       device_type:'Drone',          serial_number:'DJI-M3-042',      locker_slot:2, description:'Professional drone with 4/3 CMOS sensor. Requires signed certification form to borrow.',         image_path:'images/drone.jpg',          status:'borrowed',    borrower_name:'Sarah K.' },
  { id:3, name:'Rode VideoMic Pro', device_type:'Microphone',     serial_number:'RVM-P-007',       locker_slot:3, description:'Directional condenser microphone. Include dead-cat windshield when filming outdoors.',             image_path:'images/microphone.jpg',     status:'available',   borrower_name:null      },
  { id:4, name:'MacBook Pro 16"',   device_type:'Laptop',         serial_number:'MBP16-2024-03',   locker_slot:4, description:'M3 Max · 64 GB RAM. Adobe CC, DaVinci Resolve, and Final Cut Pro installed.',                     image_path:'images/laptop.jpg',         status:'available',   borrower_name:null      },
  { id:5, name:'Godox SL-60W',      device_type:'Studio Light',   serial_number:'GOD-SL60-011',    locker_slot:5, description:'LED video light · 60 W daylight. Includes softbox and C-stand adapter.',                         image_path:'images/studio_light.jpg',   status:'borrowed',    borrower_name:'You'     },
  { id:6, name:'Zoom H6 Recorder',  device_type:'Audio Recorder', serial_number:'ZMH6-099',        locker_slot:6, description:'Portable 6-track recorder. 32 GB SD card included. Batteries not provided.',                       image_path:'images/audio_recorder.jpg', status:'available',   borrower_name:null      },
  { id:7, name:'iPad Pro 12.9"',    device_type:'Tablet',         serial_number:'IPD-PRO-2024-08', locker_slot:7, description:'Includes Apple Pencil 2 and Magic Keyboard. Configured for field monitoring.',                    image_path:'images/tablet.jpg',         status:'available',   borrower_name:null      },
  { id:8, name:'Manfrotto 504X',    device_type:'Tripod',         serial_number:'MFT-504X-002',    locker_slot:8, description:'Professional fluid-head video tripod. 12 kg payload. Inspect head lock before each use.',          image_path:'images/tripod.jpg',         status:'maintenance', borrower_name:null      },
];

/* ============================================================
   API STUBS
============================================================ */
async function apiAuthTap(uid_hmac) {
  await sleep(380);
  const user = DEMO_USERS[demoUserIdx++ % DEMO_USERS.length];
  return { success: true, user };
}

async function apiGetDevices() {
  await sleep(280);
  return DEMO_DEVICES;
}

async function apiBorrow(device_id) {
  await sleep(480);
  const d = S.devices.find(x => x.id === device_id);
  if (d) { d.status = 'borrowed'; d.borrower_name = 'You'; }
  return { success: true, message: `${d?.name ?? 'Device'} borrowed.` };
}

async function apiReturn(device_id) {
  await sleep(480);
  const d = S.devices.find(x => x.id === device_id);
  if (d) { d.status = 'available'; d.borrower_name = null; }
  return { success: true, message: `${d?.name ?? 'Device'} returned.` };
}

async function apiEndSession() {
  await sleep(200);
}

/* ============================================================
   Enhancement C: CIRCLE REVEAL NAVIGATION
   Track last click position; set CSS custom properties on the
   target screen so the circle expands from the click origin.
============================================================ */
document.addEventListener('click', e => {
  S.lastClickX = e.clientX;
  S.lastClickY = e.clientY;
});

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

  // Set circle reveal origin on screen transitions
  if (!toIsOverlay) setRevealOrigin(toEl);

  // Reveal the target (double rAF ensures the CSS transition fires)
  toEl.style.display = '';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    toIsOverlay ? toEl.classList.add('visible') : toEl.classList.add('active');
    // Enhancement E: trigger split-text animations on the new screen
    triggerSplitText(toEl);
  }));

  // Exit the current screen (skip when opening an overlay on top of it)
  if (!toIsOverlay && fromEl && !fromIsOverlay) {
    if (!toIsOverlay) setRevealOrigin(fromEl);
    fromEl.classList.add('exit');
    setTimeout(() => fromEl.classList.remove('active', 'exit'), 560);
  }

  // Close an overlay when navigating back to a regular screen
  if (fromIsOverlay && !toIsOverlay) {
    fromEl.classList.add('hidden-up');
    setTimeout(() => {
      fromEl.classList.remove('visible', 'hidden-up');
      fromEl.style.display = 'none';
    }, 510);
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
  overlay.classList.add('hidden-up');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-up');
    overlay.style.display = 'none';
  }, 510);
  if (S.prevScreen && S.screen !== S.prevScreen) S.screen = S.prevScreen;
  armIdle();
}

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

  const sorted = [...devices].sort((a, b) => (a.locker_slot || 99) - (b.locker_slot || 99));

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
        <div class="card-slot">${slotLabel}</div>
        <div class="card-status ${statusCls}">${statusTxt}</div>
      </div>
      <div class="card-body">
        <div class="card-name">${dev.name}</div>
        <div class="card-type">${dev.device_type}</div>
      </div>
    `;

    // Enhancement B: staggered entrance (base delay ensures first card transitions)
    setTimeout(() => card.classList.add('in'), 80 + i * 75);

    // Enhancement D: image parallax on hover
    const cardImg = card.querySelector('.card-image img');
    if (cardImg) {
      card.addEventListener('mousemove', e => {
        const rect = card.getBoundingClientRect();
        const x = (e.clientX - rect.left) / rect.width - 0.5;
        const y = (e.clientY - rect.top) / rect.height - 0.5;
        cardImg.style.transform = `scale(1.08) translate(${x * -12}px, ${y * -12}px)`;
      });
      card.addEventListener('mouseleave', () => {
        cardImg.style.transform = '';
      });
    }

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
  overlay.classList.add('hidden-up');
  setTimeout(() => {
    overlay.classList.remove('visible', 'hidden-up');
    overlay.style.display = 'none';
  }, 510);
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

// Enhancement E: split text on initial page load
initSplitText();

// Enhancement D: magnetic hover on action buttons
initMagneticHover();
