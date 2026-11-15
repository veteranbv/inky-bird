import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import { Element, fakeDocument } from './fake-dom.mjs';

const source = await readFile(
  new URL('../../src/inky_bird_frame/web/boot.js', import.meta.url),
  'utf8',
);
const stuck = 'Inky could not start in this browser. Update the browser or open Inky in another one.';

// Runs boot.js the way a browser runs a classic script: no module features.
function boot({ modules = true } = {}) {
  const doc = fakeDocument();
  const main = new Element('main');
  main.id = 'main';
  main.setAttribute('aria-busy', 'true');
  const status = new Element('p');
  status.id = 'status';
  status.textContent = 'Checking your connection…';
  main.append(status);
  doc.body.append(main);
  doc.getElementById = (id) => [main, status].find((node) => node.id === id && node.isConnected) || null;
  const script = new Element('script');
  if (modules) script.noModule = false;
  doc.createElement = () => script;
  const timers = [];
  const listeners = [];
  const window = {
    setTimeout(callback, delay) {
      timers.push({ callback, delay });
    },
    addEventListener(type, listener, capture) {
      listeners.push({ type, listener, capture });
    },
  };
  window.window = window;
  window.document = doc;
  vm.runInNewContext(source, window);
  return { main, status, timers, listeners };
}

test('a start that never finishes says what to do', () => {
  const { main, status, timers } = boot();
  assert.equal(status.textContent, 'Checking your connection…');
  assert.equal(timers.length, 1);
  assert(timers[0].delay >= 10000);
  timers[0].callback();
  assert.equal(status.textContent, stuck);
  assert.equal(main.getAttribute('aria-busy'), 'false');
});

test('a page the app already drew is left alone', () => {
  const { main, status, timers } = boot();
  main.replaceChildren(new Element('h1'));
  timers[0].callback();
  assert.equal(status.textContent, 'Checking your connection…');
  assert.equal(main.getAttribute('aria-busy'), 'true');
});

test('a browser without modules is told at once', () => {
  const { status } = boot({ modules: false });
  assert.equal(status.textContent, stuck);
});

test('a script that fails to load or run is reported at once', () => {
  for (const event of [{ target: new Element('script') }, { target: {}, message: 'SyntaxError' }]) {
    const { status, listeners } = boot();
    const error = listeners.find(({ type, capture }) => type === 'error' && capture === true);
    assert(error, 'errors are watched in the capture phase');
    error.listener(event);
    assert.equal(status.textContent, stuck);
  }
});

test('an image that fails to load is not a failed start', () => {
  const { status, listeners } = boot();
  listeners.find(({ type }) => type === 'error').listener({ target: new Element('img') });
  assert.equal(status.textContent, 'Checking your connection…');
});
