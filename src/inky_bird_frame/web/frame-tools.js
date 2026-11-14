// Shared viewing tools. No session renewal, source changes or panel telemetry.
const SVG = 'http://www.w3.org/2000/svg';
const GLYPHS = {
  previous: ['M14.5 5.5 8 12l6.5 6.5', false],
  next: ['M9.5 5.5 16 12l-6.5 6.5', false],
  play: ['M8.5 5.8v12.4L18.2 12z', true],
  pause: ['M7.8 5.5h3v13h-3zM13.2 5.5h3v13h-3z', true],
  more: ['M5.5 12h.01M12 12h.01M18.5 12h.01', false],
};
// Changes only the visible icon of a compact control; its text name stays.
export function glyph(node, name) {
  const [d, filled] = GLYPHS[name];
  const path = node.children[0].children[0];
  path.setAttribute('d', d);
  path.setAttribute('fill', filled ? 'currentColor' : 'none');
}
export function compact(node, name) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'icon frame-icon');
  svg.append(document.createElementNS(SVG, 'path'));
  const label = document.createElement('span');
  label.className = 'frame-label';
  label.textContent = node.textContent;
  node.replaceChildren(svg, label);
  node.classList.add('frame-compact');
  glyph(node, name);
  return node;
}
export function stepIndex(position, change, length) {
  return length ? (((position + change) % length) + length) % length : -1;
}
// A two-step control: the first press only asks, and focus lands on the safe choice.
export function confirmAction({ button, label, question, confirmLabel, cancelLabel, action }) {
  const node = document.createElement('div'),
    ask = document.createElement('div'),
    text = document.createElement('span');
  node.className = 'frame-confirm';
  ask.className = 'frame-confirm-ask';
  ask.setAttribute('role', 'group');
  ask.setAttribute('aria-label', question);
  text.textContent = question;
  const reset = () => {
    ask.hidden = true;
    start.hidden = false;
  };
  const start = button(label, () => {
    start.hidden = true;
    ask.hidden = false;
    keep.focus();
  });
  const yes = button(confirmLabel, async () => {
    yes.disabled = true;
    try {
      await action();
    } catch {
      // The action reports its own failure; the control returns to its first step.
    } finally {
      yes.disabled = false;
    }
    reset();
  });
  const keep = button(cancelLabel, () => {
    reset();
    start.focus();
  });
  ask.append(text, yes, keep);
  ask.hidden = true;
  node.append(start, ask);
  return { node, reset };
}
function keyboardFocus(node) {
  try {
    return node.matches(':focus-visible');
  } catch {
    return false;
  }
}
export function frameTools({
  viewport,
  controls,
  status,
  button,
  primary,
  secondary,
  previous,
  next,
  togglePlay,
  onQuiet = () => {},
}) {
  let lock = null,
    keepAwake = false,
    quietTimer = null,
    startX = null,
    disposed = false,
    acquiring = false,
    swallow = false,
    generation = 0;
  const panel = document.createElement('div');
  panel.className = 'frame-more';
  panel.id = 'frame-more';
  const more = compact(
    button('More', () => expand(!controls.classList.contains('expanded'))),
    'more',
  );
  more.classList.add('frame-more-toggle');
  more.setAttribute('aria-controls', panel.id);
  more.setAttribute('aria-expanded', 'false');
  function expand(open) {
    controls.classList.toggle('expanded', open);
    more.setAttribute('aria-expanded', String(open));
  }
  controls.append(...primary, more, panel);
  panel.append(...secondary);
  const zoom = button('Zoom', () => {
    viewport.classList.toggle('zoomed');
    zoom.textContent = viewport.classList.contains('zoomed') ? 'Fit whole plate' : 'Zoom';
  });
  panel.append(zoom);
  if (document.documentElement.requestFullscreen)
    panel.append(
      button('Fullscreen', async () => {
        try {
          if (document.fullscreenElement) await document.exitFullscreen();
          else await document.documentElement.requestFullscreen();
        } catch {
          status.textContent = 'Fullscreen is not available here. Viewing still works.';
        }
      }),
    );
  let awake;
  function releaseLock() {
    const held = lock;
    lock = null;
    return held?.release().catch(() => {
      status.textContent =
        'The browser could not confirm screen-sleep release. Close this view to stop keeping it awake.';
    });
  }
  async function acquire() {
    if (lock || disposed || acquiring) return;
    acquiring = true;
    const requested = generation;
    try {
      const acquired = await navigator.wakeLock.request('screen');
      if (!keepAwake || disposed || document.hidden || requested !== generation) {
        await acquired.release();
        return;
      }
      lock = acquired;
      acquired.addEventListener('release', () => {
        if (lock === acquired) lock = null;
      });
    } catch {
      if (requested === generation) {
        keepAwake = false;
        awake?.setAttribute('aria-pressed', 'false');
        status.textContent =
          'This browser could not keep the screen awake. Its normal sleep settings apply.';
      }
    } finally {
      acquiring = false;
      if (keepAwake && !lock && !disposed && !document.hidden) acquire();
    }
  }
  if (navigator.wakeLock && window.isSecureContext) {
    awake = button('Keep screen awake', async () => {
      keepAwake = !keepAwake;
      awake.setAttribute('aria-pressed', String(keepAwake));
      if (keepAwake) await acquire();
      else {
        generation += 1;
        await releaseLock();
      }
      awake.setAttribute('aria-pressed', String(keepAwake));
    });
    awake.setAttribute('aria-pressed', 'false');
    panel.append(awake);
  }
  const visible = () => {
    if (document.hidden) {
      generation += 1;
      releaseLock();
    } else if (keepAwake) acquire();
  };
  const key = (event) => {
    if (event.key === 'Escape' && controls.classList.contains('expanded')) {
      expand(false);
      more.focus();
      return;
    }
    if (event.target.closest('input,textarea,select,button,a,[contenteditable]')) return;
    if (event.key === 'ArrowRight') next();
    if (event.key === 'ArrowLeft') previous();
    if (event.key === ' ') {
      event.preventDefault();
      togglePlay();
    }
  };
  const down = (event) => {
    startX = event.isPrimary && !viewport.classList.contains('zoomed') ? event.clientX : null;
  };
  const cancel = () => {
    startX = null;
  };
  const up = (event) => {
    if (startX !== null && Math.abs(event.clientX - startX) > 60) {
      if (event.clientX < startX) next();
      else previous();
    }
    startX = null;
  };
  const drag = (event) => event.preventDefault();
  function quiet() {
    const active = document.activeElement;
    if (controls.contains(active)) {
      // Keyboard focus keeps the controls in view; a tapped button lets go,
      // so a later Space cannot press a control nobody can see.
      if (keyboardFocus(active)) return;
      active.blur();
    }
    expand(false);
    controls.classList.add('quiet');
    onQuiet();
  }
  const reveal = () => {
    controls.classList.remove('quiet');
    clearTimeout(quietTimer);
    quietTimer = setTimeout(quiet, 5000);
  };
  // Faded controls take no pointer events, so a waking press lands beside
  // them. An engine that retargets its click to the revealed control is
  // stopped here before the control sees it.
  const wake = () => {
    swallow = controls.classList.contains('quiet');
    reveal();
  };
  const typed = () => {
    swallow = false;
    reveal();
  };
  const moved = (event) => {
    if (event.pointerType === 'mouse') reveal();
  };
  const guard = (event) => {
    if (!swallow) return;
    swallow = false;
    if (controls.contains(event.target)) {
      event.preventDefault();
      event.stopPropagation();
    }
  };
  document.addEventListener('keydown', key);
  document.addEventListener('visibilitychange', visible);
  document.addEventListener('pointerdown', wake);
  document.addEventListener('pointermove', moved);
  document.addEventListener('click', guard, true);
  document.addEventListener('keydown', typed);
  controls.addEventListener('focusin', reveal);
  viewport.addEventListener('pointerdown', down);
  viewport.addEventListener('pointerup', up);
  viewport.addEventListener('pointercancel', cancel);
  viewport.addEventListener('dragstart', drag);
  reveal();
  return () => {
    disposed = true;
    generation += 1;
    keepAwake = false;
    releaseLock();
    clearTimeout(quietTimer);
    document.removeEventListener('keydown', key);
    document.removeEventListener('visibilitychange', visible);
    document.removeEventListener('pointerdown', wake);
    document.removeEventListener('pointermove', moved);
    document.removeEventListener('click', guard, true);
    document.removeEventListener('keydown', typed);
    controls.removeEventListener('focusin', reveal);
    viewport.removeEventListener('pointerdown', down);
    viewport.removeEventListener('pointerup', up);
    viewport.removeEventListener('pointercancel', cancel);
    viewport.removeEventListener('dragstart', drag);
  };
}
