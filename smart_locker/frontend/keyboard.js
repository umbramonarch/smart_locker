/**
 * File: keyboard.js
 * Description: Shared on-screen touch keyboard for the kiosk page. Opens when
 *              any text/password input gains focus — by tap or programmatic
 *              .focus() — writes into that field at the caret, and closes on
 *              Done or when the field blurs or its screen/step leaves view.
 *              The dashboard uses the PC keyboard, so only index.html loads
 *              this file.
 * Project: smart_locker/frontend
 * Notes: Key taps are handled on pointerdown with preventDefault so the
 *        focused input never loses focus. Every mutation dispatches a
 *        bubbling 'input' event so existing search filters and Continue
 *        validators fire unchanged. Done dispatches a synthetic Enter
 *        keydown first — flagged __kbdEnter so handlers skip their own
 *        click sound — so flows that treat Enter as submit still work.
 *        Styles live in style.css under ON-SCREEN KEYBOARD.
 */
(function () {
  'use strict';

  /* ---------- what counts as a typeable field -------------------------
     'number' is deliberately absent: commit() writes via .value= and a
     number input coerces non-numeric text to "" — silent corruption. */
  const TEXT_TYPES = new Set(['text', 'password', 'search', 'email', 'url', 'tel']);
  function isField(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.tagName === 'TEXTAREA') return true;
    return el.tagName === 'INPUT' && TEXT_TYPES.has((el.type || 'text').toLowerCase());
  }

  /* ---------- layout ---------------------------------------------------
     Rows are arrays of key ids — a single character, or a special key id
     from SPECIAL_LABEL. SPECIAL_FLEX weights each row to ~10 units; the
     middle letter rows run narrower for the home-row stagger. */
  const SPECIAL_FLEX = { shift: 1.5, back: 1.5, space: 3, done: 2, sym: 1.5, abc: 2, acc: 1, tab: 1.5 };
  const PAGES = {
    letters: [
      [...'1234567890'],
      [...'qwertyuiop'],
      [...'asdfghjkl'],
      ['shift', ...'zxcvbnm', 'back'],
      ['sym', '-', '.', 'tab', 'space', 'done'],
    ],
    symbols: [
      [...'!@#$%&*()_^'],
      [...'+-=/?;:\'",'],
      [...'.<>[]{}\\|~`'],
      ['abc', 'acc', 'tab', 'space', 'back', 'done'],
    ],
    accents: [
      [...'áàâäãåæā'],
      [...'éèêëíìîï'],
      [...'óòôöõøúùûü'],
      [...'ñçýÿšž'],
      ['abc', 'sym', 'tab', 'space', 'back', 'done'],
    ],
  };
  const SPECIAL_LABEL = {
    shift: '⇧', back: '⌫', space: 'Space', done: 'Done', sym: '!@#', abc: 'ABC', acc: 'áé', tab: 'Tab',
  };
  /* Pages whose character keys respond to Shift (symbols never shift). */
  const SHIFTABLE = new Set(['letters', 'accents']);

  /* ---------- state ---------------------------------------------------- */
  let bound = null;          // input currently receiving keystrokes
  let shiftMode = 'none';    // 'none' | 'shift' (one char) | 'caps'
  let page = 'letters';
  let shifted = null;        // screen/overlay pushed up so the field clears the keys
  let repeatDelay = 0;       // hold-to-repeat warmup on Backspace
  let repeatTimer = 0;       // hold-to-repeat interval on Backspace

  /* ---------- DOM ------------------------------------------------------ */
  const kbd = document.createElement('div');
  kbd.id = 'kbd';
  kbd.className = 'kbd';
  kbd.setAttribute('aria-hidden', 'true');

  function render() {
    kbd.textContent = '';
    const shifting = SHIFTABLE.has(page) && shiftMode !== 'none';
    for (const rowSpec of PAGES[page]) {
      const row = document.createElement('div');
      row.className = 'kbd-row';
      for (const spec of rowSpec) {
        const key = document.createElement('button');
        key.type = 'button';
        key.tabIndex = -1;
        key.dataset.key = spec;
        const isChar = spec.length === 1;
        key.textContent = isChar && shifting ? spec.toUpperCase() : (SPECIAL_LABEL[spec] || spec);
        key.className = 'kbd-key' + (isChar ? '' : ` kbd-key-${spec}`);
        if (spec === 'shift' && shiftMode !== 'none') {
          key.classList.add(shiftMode === 'caps' ? 'kbd-shift-caps' : 'kbd-shift-on');
        }
        const flex = SPECIAL_FLEX[spec] || 1;
        if (flex !== 1) key.style.flex = `${flex} 1 0%`;
        row.appendChild(key);
      }
      kbd.appendChild(row);
    }
  }
  render();
  document.body.appendChild(kbd);

  /* ---------- editing -------------------------------------------------- */
  function caret(el) {
    const start = el.selectionStart == null ? el.value.length : el.selectionStart;
    const end = el.selectionEnd == null ? el.value.length : el.selectionEnd;
    return { start, end };
  }

  function commit(el, value, pos) {
    el.value = value;
    try { el.setSelectionRange(pos, pos); } catch (_) { /* some types lack caret */ }
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function insertText(el, text) {
    const { start, end } = caret(el);
    const room = el.maxLength > 0 ? el.maxLength - (el.value.length - (end - start)) : text.length;
    const insert = text.slice(0, Math.max(0, room));
    if (!insert) return;
    commit(el, el.value.slice(0, start) + insert + el.value.slice(end), start + insert.length);
  }

  function backspace(el) {
    let { start, end } = caret(el);
    if (start === end) {
      if (start === 0) return;
      start -= 1;
    }
    commit(el, el.value.slice(0, start) + el.value.slice(end), start);
  }

  /* ---------- show / hide ----------------------------------------------
     Screens are position:fixed and cannot scroll, so a field near the
     bottom would sit under the keyboard. Instead of scrolling we push the
     field's whole screen/overlay up by the overlap — the mobile "content
     shift" pattern. Controls the keyboard still covers (Continue, Cancel)
     are reached after Done or a blur, same as a phone. The shift transform
     animates because transform lives in the hosts' own transition lists —
     .kbd-shift adds only the transform, so screen/overlay exits keep their
     clip-path/opacity timing while shifted. */
  function clearShift() {
    if (shifted) {
      shifted.classList.remove('kbd-shift');
      shifted.style.removeProperty('--kbd-shift');
      shifted = null;
    }
  }

  function revealField(el) {
    const host = el.closest('.screen, .overlay');
    if (!host) { clearShift(); return; }
    /* Measure the field inside its host: both rects carry the same live
       shift, so the difference is the field's unshifted position even
       mid-transition. Hosts are fixed inset:0, so unshifted top is 0. */
    const relBottom = el.getBoundingClientRect().bottom - host.getBoundingClientRect().top;
    const kbdTop = window.innerHeight - kbd.offsetHeight;
    const need = Math.round(relBottom + 20 - kbdTop);
    if (need <= 0) { if (shifted === host) clearShift(); return; }
    if (shifted !== host) clearShift();
    host.style.setProperty('--kbd-shift', `${need}px`);
    host.classList.add('kbd-shift');
    shifted = host;
  }

  function stopRepeat() {
    clearTimeout(repeatDelay);
    clearInterval(repeatTimer);
    repeatDelay = repeatTimer = 0;
  }

  function open(el) {
    stopRepeat();
    bound = el;
    /* Fresh keyboard per field — a previous user's symbols/accents page or
       caps-lock must not leak into the next session's field. */
    if (page !== 'letters' || shiftMode !== 'none') {
      page = 'letters';
      shiftMode = 'none';
      render();
    }
    if (!kbd.classList.contains('kbd-open')) {
      kbd.classList.add('kbd-open');
      kbd.setAttribute('aria-hidden', 'false');
    }
    revealField(el);
  }

  function hide() {
    stopRepeat();
    bound = null;
    clearShift();
    kbd.classList.remove('kbd-open');
    kbd.setAttribute('aria-hidden', 'true');
  }

  /* Focused inputs can be stranded when their step (.hidden), screen
     (.active / .exit), or overlay (.visible / .hidden-left|right / inline
     display) leaves view — none of those reliably fire focusout. The same
     walk guards open(): a stale .focus() timer can land on a host the user
     already navigated away from. Departure classes count as gone the moment
     they appear — .exit/.hidden-* keep .active/.visible through the close
     animation, which is exactly the window stale focus timers land in. */
  function fieldGone(el) {
    if (!el || !el.isConnected) return true;
    for (let cur = el; cur && cur !== document.body; cur = cur.parentElement) {
      const c = cur.classList;
      if (c.contains('hidden')) return true;
      if (c.contains('exit') || c.contains('hidden-left') || c.contains('hidden-right')) return true;
      if (c.contains('screen') && !c.contains('active')) return true;
      if (c.contains('overlay') && !c.contains('visible')) return true;
      if (cur.style.display === 'none') return true;
    }
    return false;
  }

  /* Two-way guard: (1) a bound field leaving view — hidden step, departing
     or deactivated host, detach, disable — closes the keyboard; (2) a field
     that still holds DOM focus while its host becomes visible — a .focus()
     that landed in the double-rAF open gap, or a screen regaining .active
     with the field already focused — opens the keyboard on the next DOM
     mutation, so a stranded focus never leaves a dead field. Covering
     overlays (handover, inactivity, slot, update) can't be detected here —
     the host stays .visible — so app.js blurs the field when it opens one. */
  new MutationObserver(() => {
    if (bound) {
      if (fieldGone(bound) || !isField(bound)) { bound.blur(); hide(); }
    } else {
      const ae = document.activeElement;
      if (isField(ae) && !fieldGone(ae)) open(ae);
    }
  }).observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['class', 'style', 'disabled', 'readonly'],
  });

  document.addEventListener('focusin', e => {
    if (!isField(e.target) || fieldGone(e.target)) return;
    /* Inputs added after load don't carry the inputmode="none" markup —
       keep any native OSK suppressed on touch-capable browsers. */
    if (e.target.inputMode !== 'none') e.target.inputMode = 'none';
    open(e.target);
  });
  document.addEventListener('focusout', e => {
    if (e.target !== bound) return;
    stopRepeat();
    bound = null;
    /* focusout fires after focus has already moved: hop straight to a newly
       focused field; when focus left the document entirely (body), close at
       once — a visible keyboard with no bound field is a dead window for
       taps. */
    const ae = document.activeElement;
    if (isField(ae) && !fieldGone(ae)) open(ae);
    else hide();
  });

  /* ---------- key handling ----------------------------------------------
     preventDefault on pointerdown/mousedown keeps focus on the input —
     without it the first key tap blurs the field and the keyboard would
     close itself. */
  kbd.addEventListener('pointerdown', e => e.preventDefault());
  kbd.addEventListener('mousedown', e => e.preventDefault());

  function handleKey(id) {
    let el = bound;
    if (!el) {
      /* Bound was cleared by a blur but focus already landed on another
         field (hop raced focusin, or the observer hasn't run yet): write
         to the field that actually holds focus instead of eating the tap. */
      const ae = document.activeElement;
      if (isField(ae) && !fieldGone(ae)) bound = el = ae;
    }
    if (!el) return;
    if (typeof clickSound === 'function') clickSound();
    switch (id) {
      case 'shift':
        shiftMode = shiftMode === 'none' ? 'shift' : shiftMode === 'shift' ? 'caps' : 'none';
        render();
        return;
      case 'back':  backspace(el); return;
      case 'space': insertText(el, ' '); return;
      case 'sym':   page = 'symbols'; render(); return;
      case 'acc':   page = 'accents'; render(); return;
      case 'abc':   page = 'letters'; render(); return;
      case 'tab': {
        /* Fields under the keyboard can't be tapped — Tab hops focus to the
           next visible input in the same screen/overlay instead. */
        const host = el.closest('.screen, .overlay') || document;
        const fields = [...host.querySelectorAll('input, textarea')]
          .filter(f => isField(f) && !fieldGone(f) && f.getBoundingClientRect().width > 0);
        const next = fields[(fields.indexOf(el) + 1) % fields.length];
        if (next && next !== el) next.focus();
        return;
      }
      case 'done': {
        /* Let flows that accept Enter (Continue-equivalents) see the key —
           __kbdEnter tells those handlers the keyboard already played the
           click — then close and release the field. */
        const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true });
        ev.__kbdEnter = true;
        el.dispatchEvent(ev);
        hide();
        el.blur();
        return;
      }
      default: {
        const ch = SHIFTABLE.has(page) && shiftMode !== 'none' ? id.toUpperCase() : id;
        insertText(el, ch);
        if (shiftMode === 'shift') { shiftMode = 'none'; render(); }
      }
    }
  }

  kbd.addEventListener('pointerdown', e => {
    const key = e.target.closest('.kbd-key');
    if (!key) return;
    stopRepeat(); // a second touch cancels a running repeat
    handleKey(key.dataset.key);
    /* Held Backspace repeats like a real keyboard — field clearing. */
    if (key.dataset.key === 'back' && bound) {
      repeatDelay = setTimeout(() => {
        repeatTimer = setInterval(() => {
          if (bound) handleKey('back');
          else stopRepeat();
        }, 55);
      }, 420);
    }
  });
  document.addEventListener('pointerup', stopRepeat);
  document.addEventListener('pointercancel', stopRepeat);
})();
