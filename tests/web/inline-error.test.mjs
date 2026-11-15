import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

const source = await readFile(new URL('../../src/inky_bird_frame/web/ui.js', import.meta.url), 'utf8');
const { inlineProblem, clearProblem } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
);

function setup() {
  globalThis.document = fakeDocument();
  const place = new Element('section');
  document.body.append(place);
  const problems = [];
  const ui = {
    showProblem: (error) => problems.push(error),
    button(text, action) {
      const node = new Element('button');
      node.textContent = text;
      node.addEventListener('click', action);
      return node;
    },
  };
  return { place, problems, ui };
}

const alerts = (root, found = []) => {
  for (const child of root.children) {
    if (child.getAttribute('role') === 'alert') found.push(child);
    alerts(child, found);
  }
  return found;
};

test('a failure is shown beside its place and the page stays', () => {
  const { place, problems, ui } = setup();
  const kept = new Element('p');
  kept.textContent = 'Already drawn';
  place.append(kept);
  inlineProblem(ui, place, Object.assign(new Error('Settings could not be read.'), { code: 'x' }));
  assert.equal(problems.length, 0);
  assert.ok(kept.isConnected);
  assert.deepEqual(
    alerts(place).map((node) => node.textContent),
    ['Settings could not be read.'],
  );
});

test('a repeated failure replaces the message instead of stacking', () => {
  const { place, ui } = setup();
  inlineProblem(ui, place, new Error('First'));
  inlineProblem(ui, place, new Error('Second'));
  assert.deepEqual(
    alerts(place).map((node) => node.textContent),
    ['Second'],
  );
});

test('an ended session goes to sign-in and draws nothing inline', () => {
  const { place, problems, ui } = setup();
  const ended = Object.assign(new Error('Sign in again.'), { code: 'sign_in_required' });
  assert.equal(inlineProblem(ui, place, ended), null);
  assert.deepEqual(problems, [ended]);
  assert.equal(place.children.length, 0);
});

test('a retry control runs its action, and clearing removes the message', () => {
  const { place, ui } = setup();
  let retried = 0;
  inlineProblem(ui, place, new Error('Offline'), { label: 'Try again', action: () => retried++ });
  findButton(place, 'Try again').fire('click');
  assert.equal(retried, 1);
  clearProblem(place);
  assert.equal(alerts(place).length, 0);
  inlineProblem(ui, place, new Error('Again'));
  assert.equal(findButton(place, 'Try again'), null, 'a later message without retry has no button');
});
