import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { Element } from './fake-dom.mjs';

const source = await readFile(new URL('../../src/inky_bird_frame/web/ui.js', import.meta.url), 'utf8');
const ui = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('an opened notice stays open while the same text is written again', (t) => {
  const observers = [];
  globalThis.MutationObserver = class {
    constructor(callback) {
      this.callback = callback;
      observers.push(this);
    }
    observe() {}
    disconnect() {}
  };
  t.after(() => delete globalThis.MutationObserver);
  const button = (text, action) => {
    const node = new Element('button');
    node.textContent = text;
    node.addEventListener('click', action);
    return node;
  };
  const status = new Element('p');
  const notice = ui.clampedNotice(status, button);
  const write = (text) => {
    status.textContent = text;
    for (const observer of observers) observer.callback([]);
  };
  write('Offline. Keeping the last plate; Inky keeps checking.');
  notice.control.click();
  assert.equal(status.classList.contains('open'), true);
  // A poll that fails again writes the same words.
  write('Offline. Keeping the last plate; Inky keeps checking.');
  assert.equal(status.classList.contains('open'), true);
  assert.equal(notice.control.textContent, 'Less');
  // New words start folded.
  write('This plate did not load. Keeping the last one; Play goes on.');
  assert.equal(status.classList.contains('open'), false);
  assert.equal(notice.control.textContent, 'More');
  notice.stop();
});
