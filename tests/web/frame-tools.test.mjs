import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton as find } from './fake-dom.mjs';

const source = await readFile(
  new URL('../../src/inky_bird_frame/web/frame-tools.js', import.meta.url),
  'utf8',
);
const tools = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const { frameTools } = tools;

const button = (text, action) => {
  const node = new Element('button');
  node.textContent = text;
  node.addEventListener('click', action);
  return node;
};
const actionOf = (node) => [...node.listeners.get('click')][0];

// A pointer press on `target`, then the click the browser dispatches: the
// document capture listeners run first and may stop it reaching the target.
function press(target) {
  globalThis.document.fire('pointerdown', { target, pointerType: 'touch' });
  target.fire('pointerdown', { target, pointerType: 'touch' });
  if (target.tagName === 'BUTTON') {
    target.focusVisible = false;
    target.focus();
  }
  const click = globalThis.document.fire('click', { target });
  if (!click.stopped) target.fire('click', click);
  return click;
}

function setup({ secure = false, wakeLock } = {}) {
  const old = {
    document: globalThis.document,
    window: globalThis.window,
    navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
  };
  globalThis.document = fakeDocument();
  globalThis.window = { isSecureContext: secure };
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: wakeLock ? { wakeLock } : {},
  });
  const calls = { previous: 0, next: 0, toggle: 0, quiet: 0, played: 0 };
  const controls = new Element(),
    viewport = new Element(),
    status = new Element('p');
  const play = button('Play', () => (calls.played += 1));
  const shuffle = button('Shuffle', () => {});
  let cleanup = () => {};
  return {
    calls,
    controls,
    viewport,
    status,
    play,
    shuffle,
    start() {
      cleanup = frameTools({
        viewport,
        controls,
        status,
        button,
        primary: [play],
        secondary: [shuffle],
        previous: () => (calls.previous += 1),
        next: () => (calls.next += 1),
        togglePlay: () => (calls.toggle += 1),
        onQuiet: () => (calls.quiet += 1),
      });
      return cleanup;
    },
    restore() {
      cleanup();
      globalThis.document = old.document;
      globalThis.window = old.window;
      if (old.navigator) Object.defineProperty(globalThis, 'navigator', old.navigator);
      else delete globalThis.navigator;
    },
  };
}


test('a tap that wakes faded controls does not also press the control under it', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const frame = setup();
  try {
    frame.start();
    t.mock.timers.tick(5000);
    assert(frame.controls.classList.contains('quiet'), 'idle controls fade');
    const wake = press(frame.play);
    assert.equal(frame.calls.played, 0, 'the waking tap is swallowed');
    assert(wake.defaultPrevented, 'the waking tap cannot follow a link either');
    assert(!frame.controls.classList.contains('quiet'), 'the tap reveals the controls');
    press(frame.play);
    assert.equal(frame.calls.played, 1, 'the next tap on visible controls works');
  } finally {
    frame.restore();
  }
});

test('a wake tap outside the controls does not swallow a later keyboard click', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const frame = setup();
  try {
    frame.start();
    t.mock.timers.tick(5000);
    press(frame.viewport);
    frame.play.focusVisible = true;
    frame.play.focus();
    globalThis.document.fire('keydown', { target: frame.play, key: 'Enter' });
    const click = globalThis.document.fire('click', { target: frame.play });
    if (!click.stopped) frame.play.fire('click', click);
    assert.equal(frame.calls.played, 1);
  } finally {
    frame.restore();
  }
});

test('controls fade again after a tap leaves a button focused', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const frame = setup();
  try {
    frame.start();
    press(frame.play);
    assert.equal(frame.calls.played, 1);
    assert.equal(globalThis.document.activeElement, frame.play, 'the browser focused the button');
    frame.controls.fire('focusin', { target: frame.play });
    t.mock.timers.tick(5000);
    assert(frame.controls.classList.contains('quiet'), 'pointer focus does not pin the bar');
    assert.notEqual(
      globalThis.document.activeElement,
      frame.play,
      'focus leaves the faded button so Space cannot press it unseen',
    );
  } finally {
    frame.restore();
  }
});

test('keyboard focus inside the controls keeps them visible', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const frame = setup();
  try {
    frame.start();
    frame.play.focusVisible = true;
    frame.play.focus();
    frame.controls.fire('focusin', { target: frame.play });
    t.mock.timers.tick(5000);
    assert(!frame.controls.classList.contains('quiet'));
    assert.equal(globalThis.document.activeElement, frame.play);
  } finally {
    frame.restore();
  }
});

test('mouse movement reveals faded controls', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const frame = setup();
  try {
    frame.start();
    t.mock.timers.tick(5000);
    globalThis.document.fire('pointermove', { target: frame.viewport, pointerType: 'touch' });
    assert(frame.controls.classList.contains('quiet'), 'touch movement is a gesture, not a wake');
    globalThis.document.fire('pointermove', { target: frame.viewport, pointerType: 'mouse' });
    assert(!frame.controls.classList.contains('quiet'));
  } finally {
    frame.restore();
  }
});

test('secondary controls sit behind one More disclosure that closes when the bar fades', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const frame = setup();
  try {
    frame.start();
    const [play, more, panel] = frame.controls.children;
    assert.equal(play, frame.play, 'primary controls come first');
    assert.equal(more.textContent, 'More');
    assert(more.classList.contains('frame-more-toggle'));
    assert(panel.contains(frame.shuffle), 'caller secondary controls are in the panel');
    assert(find(panel, 'Zoom'), 'viewing tools are in the panel');
    assert.equal(more.getAttribute('aria-controls'), panel.id);
    assert.equal(more.getAttribute('aria-expanded'), 'false');
    press(more);
    assert.equal(more.getAttribute('aria-expanded'), 'true');
    assert(frame.controls.classList.contains('expanded'));
    globalThis.document.fire('keydown', { target: frame.shuffle, key: 'Escape' });
    assert.equal(more.getAttribute('aria-expanded'), 'false', 'Escape closes the panel');
    assert.equal(globalThis.document.activeElement, more, 'focus returns to More');
    press(more);
    t.mock.timers.tick(5000);
    assert.equal(more.getAttribute('aria-expanded'), 'false', 'fading closes the panel');
    assert(!frame.controls.classList.contains('expanded'));
    assert.equal(frame.calls.quiet, 1, 'callers learn the bar faded');
  } finally {
    frame.restore();
  }
});

test('swipe steps plates and the frame image is not draggable', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const frame = setup();
  try {
    frame.start();
    frame.viewport.fire('pointerdown', { clientX: 300 });
    frame.viewport.fire('pointerup', { clientX: 200 });
    assert.equal(frame.calls.next, 1);
    frame.viewport.fire('pointerdown', { clientX: 200 });
    frame.viewport.fire('pointerup', { clientX: 300 });
    assert.equal(frame.calls.previous, 1);
    frame.viewport.fire('pointerdown', { clientX: 200 });
    frame.viewport.fire('pointercancel', {});
    frame.viewport.fire('pointerup', { clientX: 400 });
    assert.equal(frame.calls.previous, 1, 'a cancelled gesture does not step');
    const image = new Element('img');
    frame.viewport.append(image);
    const drag = frame.viewport.fire('dragstart', { target: image });
    assert(drag.defaultPrevented, 'a mouse drag cannot start a native image drag');
  } finally {
    frame.restore();
  }
});

test('stepIndex wraps both ways and reports an empty view', () => {
  assert.equal(tools.stepIndex(0, -1, 3), 2);
  assert.equal(tools.stepIndex(2, 1, 3), 0);
  assert.equal(tools.stepIndex(1, 2, 3), 0);
  assert.equal(tools.stepIndex(0, 1, 0), -1, 'nothing to step through');
  assert.equal(tools.stepIndex(5, 1, 0), -1);
});

test('compact controls keep a text name beside an icon', () => {
  const old = globalThis.document;
  globalThis.document = fakeDocument();
  try {
    const node = button('Play', () => {});
    tools.compact(node, 'play');
    assert(node.classList.contains('frame-compact'));
    assert.equal(node.textContent, 'Play', 'the accessible name is unchanged');
    const [icon, label] = node.children;
    assert.equal(icon.tagName, 'SVG');
    assert.equal(icon.getAttribute('aria-hidden'), 'true');
    assert.equal(label.className, 'frame-label');
    const before = icon.children[0].getAttribute('d');
    tools.glyph(node, 'pause');
    assert.notEqual(icon.children[0].getAttribute('d'), before, 'the icon follows the state');
    assert.equal(node.textContent, 'Play', 'changing the icon keeps the name');
  } finally {
    globalThis.document = old;
  }
});

test('a confirmed action needs two presses and resets on cancel or reset', async () => {
  const old = globalThis.document;
  globalThis.document = fakeDocument();
  try {
    let ran = 0,
      fail = false;
    const confirm = tools.confirmAction({
      button,
      label: 'Forget this screen',
      question: 'Forget this screen? It will need pairing again.',
      confirmLabel: 'Forget',
      cancelLabel: 'Keep',
      action: async () => {
        ran += 1;
        if (fail) throw new Error('failed');
      },
    });
    const start = find(confirm.node, 'Forget this screen'),
      yes = find(confirm.node, 'Forget'),
      keep = find(confirm.node, 'Keep');
    const ask = yes.parent;
    assert(ask.hidden, 'the confirmation starts hidden');
    start.fire('click');
    assert.equal(ran, 0, 'the first press only asks');
    assert(start.hidden);
    assert(!ask.hidden);
    assert.equal(globalThis.document.activeElement, keep, 'focus lands on the safe choice');
    keep.fire('click');
    assert(ask.hidden);
    assert(!start.hidden);
    assert.equal(globalThis.document.activeElement, start);
    assert.equal(ran, 0);
    start.fire('click');
    confirm.reset();
    assert(ask.hidden, 'reset returns to the first step');
    start.fire('click');
    fail = true;
    yes.fire('click');
    assert(yes.disabled, 'a second press cannot repeat the request');
    await setImmediate();
    assert.equal(ran, 1);
    assert(!yes.disabled, 'a failed action can be tried again');
    assert(ask.hidden, 'a failed action returns to the first step');
  } finally {
    globalThis.document = old;
  }
});

function wakeLockStub({ deferRequest = false, deferRelease = false } = {}) {
  const requests = [],
    sentinels = [],
    releases = [];
  const make = () => {
    const sentinel = Object.assign(new EventTarget(), {
      released: false,
      release() {
        const finish = () => {
          this.released = true;
          this.dispatchEvent(new Event('release'));
        };
        if (!deferRelease) return Promise.resolve(finish());
        return new Promise((resolve) =>
          releases.push(() => {
            finish();
            resolve();
          }),
        );
      },
    });
    sentinels.push(sentinel);
    return sentinel;
  };
  return {
    requests,
    sentinels,
    releases,
    wakeLock: {
      request: () =>
        deferRequest
          ? new Promise((resolve) => requests.push(() => resolve(make())))
          : Promise.resolve(make()),
    },
  };
}

test('rapid wake-lock toggles and disposal release every deferred sentinel', async () => {
  const stub = wakeLockStub({ deferRequest: true });
  const frame = setup({ secure: true, wakeLock: stub.wakeLock });
  try {
    const cleanup = frame.start();
    const action = actionOf(find(frame.controls, 'Keep screen awake'));
    const pending = action();
    await action();
    await action();
    assert.equal(stub.requests.length, 1, 'acquisition is serialized while the first request waits');
    stub.requests[0]();
    await pending;
    await setImmediate();
    assert.equal(stub.sentinels[0].released, true, 'superseded first sentinel releases itself');
    assert.equal(stub.requests.length, 2, 'the deliberate new on state can request a fresh lock');
    await action();
    cleanup();
    stub.requests[1]();
    await setImmediate();
    assert(
      stub.sentinels.every((sentinel) => sentinel.released),
      'off and disposal release every acquired sentinel',
    );
    assert.equal(stub.requests.length, 2, 'disposal never starts another request');
  } finally {
    frame.restore();
  }
});

test('a new on state acquires while an old sentinel release is pending', async () => {
  const stub = wakeLockStub({ deferRelease: true });
  const frame = setup({ secure: true, wakeLock: stub.wakeLock });
  try {
    const cleanup = frame.start();
    const awake = find(frame.controls, 'Keep screen awake');
    const action = actionOf(awake);
    await action();
    assert.equal(awake.getAttribute('aria-pressed'), 'true');
    const off = action();
    await action();
    assert.equal(stub.sentinels.length, 2);
    stub.releases[0]();
    await off;
    assert(!stub.sentinels[1].released);
    assert.equal(awake.getAttribute('aria-pressed'), 'true', 'the toggle reports its state');
    assert.equal(awake.textContent, 'Keep screen awake', 'the toggle keeps its name');
    cleanup();
    stub.releases[1]();
    await setImmediate();
    assert(stub.sentinels.every((sentinel) => sentinel.released));
  } finally {
    frame.restore();
  }
});

test('the keep-awake toggle reports off when the browser refuses', async () => {
  const frame = setup({
    secure: true,
    wakeLock: { request: () => Promise.reject(new Error('denied')) },
  });
  try {
    frame.start();
    const awake = find(frame.controls, 'Keep screen awake');
    assert.equal(awake.getAttribute('aria-pressed'), 'false');
    await actionOf(awake)();
    assert.equal(awake.getAttribute('aria-pressed'), 'false');
    assert.match(frame.status.textContent, /could not keep the screen awake/);
  } finally {
    frame.restore();
  }
});
