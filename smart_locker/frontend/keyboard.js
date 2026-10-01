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
 *        keydown first, so flows that treat Enter as submit still work.
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
     Rows are arrays of keys. A key is a character string or [id, flex].
     Special ids: shift, back, space, done, sym (to symbols), abc (to
     letters). Flex values make each row total 10 units. */
  const SPECIAL_FLEX = { shift: 1.5, back: 1.5, space: 3, done: 2, sym: 1.5, abc: 2, tab: 1.5 };
  const PAGES = {
    letters: [
      [...'1234567890'],
      [...'qwertyuiop'],
      [...'asdfghjkl'],
      ['shift', ...'zxcvbnm', 'back'],
      ['sym', '-', '.', 'tab', 'space', 'done'],
    ],
    symbols: [
      [...'!@#$%&*()_'],
      [...'+-=/?;:\'",'],
      [...'.<>[]{}\\|~'],
      ['abc', 'tab', 'space', 'back', 'done'],
    ],
  };
  const SPECIAL_LABEL = {
    shift: '⇧', back: '⌫', space: 'Space', done: 'Done', sym: '!@#', abc: 'ABC', tab: 'Tab',
  };

  /* ---------- state ---------------------------------------------------- */
  let bound = null;          // input currently receiving keystrokes
  let hideTimer = 0;         // deferred blur-hide so focus hops don't flicker
  let shiftMode = 'none';    // 'none' | 'shift' (one char) | 'caps'
  let page = 'letters';
  let shifted = null;        // screen/overlay pushed up so the field clears the keys
  let shiftedPx = 0;         // shift applied to `shifted`

  /* ---------- DOM ------------------------------------------------------ */
  const kbd = document.createElement('div');
  kbd.id = 'kbd';
  kbd.className = 'kbd';
  kbd.setAttribute('aria-hidden', 'true');

  function normKey(spec) {
    if (Array.isArray(spec)) return { id: spec[0], flex: spec[1] };
    return { id: spec, flex: SPECIAL_FLEX[spec] || 1 };
  }

  function render() {
    kbd.textContent = '';
    for (const rowSpec of PAGES[page]) {
      const row = document.createElement('div');
      row.className = 'kbd-row';
      for (const spec of rowSpec) {
        const { id, flex } = normKey(spec);
        const key = document.createElement('button');
        key.type = 'button';
        key.tabIndex = -1;
        key.dataset.key = id;
        const isChar = id.length === 1;
        const label = isChar && page === 'letters' && shiftMode !== 'none'
          ? id.toUpperCase()
          : (SPECIAL_LABEL[id] || id);
        key.textContent = label;
        key.className = 'kbd-key' + (isChar ? '' : ` kbd-key-${id}`);
        if (id === 'shift' && shiftMode !== 'none') {
          key.classList.add(shiftMode === 'caps' ? 'kbd-shift-caps' : 'kbd-shift-on');
        }
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
     are reached after Done or a blur, same as a phone. */
  function clearShift() {
    if (shifted) {
      shifted.classList.remove('kbd-shift');
      shifted.style.removeProperty('--kbd-shift');
      shifted = null;
      shiftedPx = 0;
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
    shiftedPx = need;
  }

  function open(el) {
    clearTimeout(hideTimer);
    bound = el;
    if (!kbd.classList.contains('kbd-open')) {
      kbd.classList.add('kbd-open');
      kbd.setAttribute('aria-hidden', 'false');
    }
    revealField(el);
  }

  function hide() {
    clearTimeout(hideTimer);
    bound = null;
    clearShift();
    kbd.classList.remove('kbd-open');
    kbd.setAttribute('aria-hidden', 'true');
  }

  /* Focused inputs can be stranded when their step (.hidden), screen
     (.active), or overlay (.visible / inline display) leaves view — none of
     those reliably fire focusout. The same walk guards open(): a stale
     .focus() timer can land on a screen the user already navigated away
     from (screens clip away but keep their layout). */
  function fieldGone(el) {
    if (!el.isConnected || el.hidden) return true;
    // Computed visibility: covers the HTML hidden attribute, class or
    // display:none rules, and visibility:hidden — anywhere up the chain.
    if (typeof el.checkVisibility === 'function'
        && !el.checkVisibility({ checkVisibilityCSS: true })) return true;
    for (let cur = el; cur && cur !== document.body; cur = cur.parentElement) {
      const c = cur.classList;
      if (c.contains('hidden')) return true;
      if (c.contains('screen') && !c.contains('active')) return true;
      if (c.contains('overlay') && !c.contains('visible')) return true;
      if (cur.style.display === 'none') return true;
    }
    // A covering fixed overlay (detail card, error dialog) owns the taps —
    // a field left focused behind it is gone for our purposes.
    const host = el.closest('.screen, .overlay');
    for (const ov of document.querySelectorAll('.overlay')) {
      if (ov === host) continue;
      const shown = ov.classList.contains('visible')
        || (ov.style.display && ov.style.display !== 'none');
      if (shown) return true;
    }
    return false;
  }
  new MutationObserver(() => {
    if (bound && fieldGone(bound)) { bound.blur(); hide(); }
  }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class', 'style'] });

  document.addEventListener('focusin', e => {
    if (isField(e.target) && !fieldGone(e.target)) open(e.target);
  });
  document.addEventListener('focusout', e => {
    if (e.target !== bound) return;
    /* Bound input blurred — drop it now so a key tap inside the hide grace
       window cannot write into a field that already lost focus. A focus
       hop to the next field cancels the hide via focusin. */
    bound = null;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 120);
  });

  /* ---------- key handling ----------------------------------------------
     preventDefault on pointerdown/mousedown keeps focus on the input —
     without it the first key tap blurs the field and the keyboard would
     close itself. */
  kbd.addEventListener('pointerdown', e => e.preventDefault());
  kbd.addEventListener('mousedown', e => e.preventDefault());

  function handleKey(id) {
    const el = bound;
    if (!el) return;
    // The field may have been covered or hidden since it bound — never
    // mutate an ineligible target.
    if (fieldGone(el)) { bound = null; hide(); return; }
    switch (id) {
      case 'shift':
        shiftMode = shiftMode === 'none' ? 'shift' : shiftMode === 'shift' ? 'caps' : 'none';
        render();
        return;
      case 'back':  backspace(el); return;
      case 'space': insertText(el, ' '); return;
      case 'sym':   page = 'symbols'; render(); return;
      case 'abc':   page = 'letters'; render(); return;
      case 'tab': {
        /* Fields under the keyboard can't be tapped — Tab hops focus to the
           next visible input in the same screen/overlay instead. */
        const host = el.closest('.screen, .overlay') || document;
        const fields = [...host.querySelectorAll('input, textarea')]
          .filter(f => isField(f) && f.getBoundingClientRect().width > 0);
        const next = fields[(fields.indexOf(el) + 1) % fields.length];
        if (next && next !== el) next.focus();
        return;
      }
      case 'done': {
        /* Let flows that accept Enter (Continue-equivalents) see the key,
           then close and release the field. */
        el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        hide();
        el.blur();
        return;
      }
      default: {
        const ch = page === 'letters' && shiftMode !== 'none' ? id.toUpperCase() : id;
        insertText(el, ch);
        if (shiftMode === 'shift') { shiftMode = 'none'; render(); }
      }
    }
  }

  kbd.addEventListener('pointerdown', e => {
    const key = e.target.closest('.kbd-key');
    if (key) handleKey(key.dataset.key);
  });
})();
