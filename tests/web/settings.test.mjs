import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import { Element, fakeDocument, findButton } from './fake-dom.mjs';

const web = new URL('../../src/inky_bird_frame/web/', import.meta.url);
const moduleURL = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const uiURL = moduleURL(await readFile(new URL('ui.js', web), 'utf8'));
const wordsURL = moduleURL(await readFile(new URL('words.js', web), 'utf8'));
const { renderSettings } = await import(
  moduleURL(
    (await readFile(new URL('settings.js', web), 'utf8'))
      .replace("'/app/ui.js'", JSON.stringify(uiURL))
      .replace("'/app/words.js'", JSON.stringify(wordsURL)),
  )
);

const sourceNames = {
  inaturalist: ['iNaturalist', 'Community reports'],
  ebird: ['eBird nearby', 'Community reports'],
  birdweather: ['BirdWeather', 'Station detections'],
  birdbuddy: ['Bird Buddy', 'Feeder records'],
};

function settings(change = {}) {
  return {
    revision: 'r1',
    editable: true,
    restart_required: false,
    generation_enabled: false,
    discovery: {
      sources: ['inaturalist', 'future-source'],
      window: 'last-week',
      radius_km: 10,
      species_limit: 40,
      latitude: 1.0,
      longitude: 2.0,
      birdbuddy_include_manual_sightings: false,
    },
    credentials: {
      ebird_api_key: { configured: true, external: false },
      birdweather_token: { configured: false, external: false },
      geoapify_api_key: { configured: false, external: false },
    },
    notifications: {
      enabled: true,
      destinations: [
        { name: 'Phone', events: ['discovery', 'future_event'] },
        { name: 'Desk', events: ['terminal_error'] },
      ],
    },
    ...change,
  };
}

const walk = (root, found = []) => {
  for (const child of root.children) {
    found.push(child);
    walk(child, found);
  }
  return found;
};
const alerts = (root) =>
  walk(root)
    .filter((node) => node.getAttribute('role') === 'alert' && node.textContent)
    .map((node) => node.textContent);
async function flush() {
  for (let n = 0; n < 10; n++) await setImmediate();
}

async function open(t, routes, { source, localPasswordAllowed = true } = {}) {
  const names = ['document', 'location'];
  const saved = Object.fromEntries(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const doc = fakeDocument();
  doc.createTextNode = (text) => Object.assign(new Element('#text'), { text });
  const main = new Element('main');
  main.id = 'main';
  doc.body.append(main);
  doc.querySelector = (selector) => (selector === '#main' ? main : null);
  const reloads = [];
  Object.defineProperty(globalThis, 'document', { configurable: true, writable: true, value: doc });
  Object.defineProperty(globalThis, 'location', {
    configurable: true,
    writable: true,
    value: { protocol: 'https:', reload: () => reloads.push(true) },
  });
  t.after(() => {
    for (const [name, descriptor] of Object.entries(saved))
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
  });
  const calls = [],
    problems = [];
  const element = (tag, text, className) => {
    const node = new Element(tag);
    // Like a real form control, an input keeps its value as text.
    if (tag === 'input') {
      let value = '';
      Object.defineProperty(node, 'value', {
        get: () => value,
        set: (next) => {
          value = String(next);
        },
      });
    }
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const ui = {
    element,
    button(text, action) {
      const node = element('button', text);
      node.addEventListener('click', (event) => action({ currentTarget: node, ...event }));
      return node;
    },
    async api(path, body) {
      calls.push({ path, body });
      const answer = typeof routes[path] === 'function' ? routes[path](body) : routes[path];
      if (answer instanceof Error) throw answer;
      if (answer === undefined) throw new Error('unexpected ' + path);
      return structuredClone(answer);
    },
    showProblem: (error) => problems.push(error),
    localPasswordAllowed,
  };
  const helpers = {
    base(title, intro) {
      main.replaceChildren(element('h1', title), element('p', intro));
    },
    link: (text) => element('a', text),
    note: (text) => element('p', text, 'hint'),
    sourceNames,
    source,
  };
  await renderSettings(ui, helpers);
  return { main, calls, problems, reloads };
}
const field = (main, id) => walk(main).find((node) => node.id === 'setting-' + id);
const form = (main) => walk(main).find((node) => node.tagName === 'FORM');
const review = (main) => walk(main).find((node) => node.classes.has('notice') && !node.hidden);
const reviewText = (main) => review(main)?.children.map((node) => node.textContent) || [];
function submit(main) {
  form(main).fire('submit', {});
}

test('a settings page that cannot load keeps the page and offers a retry', async (t) => {
  const { main, problems, reloads } = await open(t, {
    '/v1/owner/settings': Object.assign(new Error('Settings could not be read.'), { code: 'journal_error' }),
  });
  assert.equal(main.children[0].textContent, 'Settings and backups');
  assert.deepEqual(alerts(main), ['Settings could not be read.']);
  assert.equal(problems.length, 0);
  findButton(main, 'Try again').fire('click');
  assert.equal(reloads.length, 1);
});

test('Bird Buddy state that cannot load still leaves the source switch', async (t) => {
  const { main } = await open(
    t,
    {
      '/v1/owner/settings': settings(),
      '/v1/owner/birdbuddy': Object.assign(new Error('Bird Buddy state could not be read.'), {
        code: 'journal_error',
      }),
    },
    { source: 'birdbuddy' },
  );
  assert.deepEqual(alerts(main), ['Bird Buddy state could not be read.']);
  assert.ok(
    walk(main).some((node) => node.textContent === 'Check Bird Buddy for new birds'),
    'the switch to turn Bird Buddy off is still there',
  );
});

test('the review names removed destinations, a new location and replaced secrets', async (t) => {
  const { main, calls } = await open(t, { '/v1/owner/settings': settings() });
  field(main, 'latitude').value = '3.5';
  field(main, 'radius_km').value = '25';
  field(main, 'species_limit').value = '60';
  field(main, 'geoapify_api_key').value = '';
  const desk = walk(main).filter((node) => node.tagName === 'FIELDSET' && node.children[0]?.textContent === 'Destination')[1];
  findButton(desk, 'Remove destination').fire('click');
  submit(main);
  const lines = reviewText(main).join('\n');
  assert.match(lines, /Remove notification destination: Desk\./);
  assert.doesNotMatch(lines, /Remove notification destination: Phone/);
  assert.match(lines, /Community location: latitude 3\.5, longitude 2\./);
  assert.match(lines, /Radius: 25 km\./);
  assert.match(lines, /Live species limit: 60\./);
  assert.doesNotMatch(lines, /Geoapify|eBird API key/, 'a secret that stays is not mentioned');
  findButton(main, 'Save settings').fire('click');
  await flush();
  const body = calls.at(-1).body;
  assert.deepEqual(
    body.changes.notifications.destinations.map((item) => item.name),
    ['Phone'],
  );
});

test('a config without a radius or species limit does not report them as changed', async (t) => {
  const saved = settings();
  delete saved.discovery.radius_km;
  delete saved.discovery.species_limit;
  const { main } = await open(t, { '/v1/owner/settings': saved });
  submit(main);
  assert.ok(findButton(main, 'Save settings'));
  assert.doesNotMatch(reviewText(main).join('\n'), /Radius|Live species limit/);
});

test('the review names a cleared Geoapify key', async (t) => {
  const saved = settings();
  saved.credentials.geoapify_api_key.configured = true;
  const { main } = await open(t, { '/v1/owner/settings': saved });
  const mode = walk(main).find((node) => node.tagName === 'SELECT' && node.getAttribute('aria-label') === 'Location format');
  mode.value = 'none';
  mode.fire('change');
  submit(main);
  const lines = reviewText(main).join('\n');
  assert.match(lines, /Clear the saved Geoapify API key\./);
  assert.match(lines, /Community location: no community location\./);
});

test('a source page names a replaced key', async (t) => {
  const { main } = await open(t, { '/v1/owner/settings': settings() }, { source: 'ebird' });
  field(main, 'ebird_api_key').value = 'replacement';
  submit(main);
  assert.match(reviewText(main).join('\n'), /Replace the saved eBird API key\./);
});

test('changing the form after a review withdraws that review', async (t) => {
  const { main, calls } = await open(t, { '/v1/owner/settings': settings() });
  submit(main);
  assert.ok(findButton(main, 'Save settings'));
  const desk = walk(main).filter((node) => node.tagName === 'FIELDSET' && node.children[0]?.textContent === 'Destination')[1];
  findButton(desk, 'Remove destination').fire('click');
  assert.equal(findButton(main, 'Save settings'), null, 'the old review cannot be saved');
  findButton(main, 'Add destination').fire('click');
  submit(main);
  assert.ok(findButton(main, 'Save settings'));
  findButton(main, 'Add destination').fire('click');
  assert.equal(findButton(main, 'Save settings'), null);
  submit(main);
  field(main, 'radius_km').value = '12';
  field(main, 'radius_km').fire('input');
  assert.equal(findButton(main, 'Save settings'), null, 'typing in a field withdraws the review');
  assert.equal(calls.filter(({ body }) => body).length, 0);
});

test('a source or event this page does not know survives a save', async (t) => {
  const page = await open(t, { '/v1/owner/settings': settings() }, { source: 'inaturalist' });
  submit(page.main);
  findButton(page.main, 'Save settings').fire('click');
  await flush();
  assert.deepEqual(page.calls.at(-1).body.changes.discovery.sources, ['inaturalist', 'future-source']);
  const full = await open(t, { '/v1/owner/settings': settings() });
  submit(full.main);
  findButton(full.main, 'Save settings').fire('click');
  await flush();
  const phone = full.calls.at(-1).body.changes.notifications.destinations.find((item) => item.name === 'Phone');
  assert.deepEqual(phone.events, ['discovery', 'future_event']);
});

test('a save that needs a recent sign-in asks for the password and saves the same choices', async (t) => {
  let confirmed = false;
  const { main, calls, problems } = await open(t, {
    '/v1/owner/settings': (body) =>
      body && !confirmed
        ? Object.assign(new Error('Confirm your sign-in before saving keys or delivery addresses'), {
            code: 'confirmation_required',
          })
        : body
          ? {}
          : settings(),
    '/v1/owner/confirm-password': (body) => {
      if (body.passphrase !== 'synthetic passphrase')
        return Object.assign(new Error('That password is not correct.'), {
          code: 'password_confirmation_failed',
        });
      confirmed = true;
      return {};
    },
  }, { source: 'ebird' });
  field(main, 'ebird_api_key').value = 'replacement';
  submit(main);
  findButton(main, 'Save settings').fire('click');
  await flush();
  assert.deepEqual(problems, []);
  const password = walk(main).find((node) => node.id === 'settings-confirm-password');
  assert.ok(password, 'the page asks for the password in place');
  // Typing in the confirmation is not a change to the reviewed choices.
  password.value = 'wrong';
  password.fire('input');
  findButton(main, 'Confirm and save').fire('click');
  await flush();
  assert.deepEqual(alerts(main), ['That password is not correct.']);
  password.value = 'synthetic passphrase';
  password.fire('input');
  findButton(main, 'Confirm and save').fire('click');
  await flush();
  const saves = calls.filter(({ path, body }) => path === '/v1/owner/settings' && body);
  assert.equal(saves.length, 2);
  assert.deepEqual(saves[1].body, saves[0].body);
  assert.equal(saves[1].body.changes.discovery.ebird_api_key, 'replacement');
  assert.equal(password.value, '');
});

test('a confirmation with no way to confirm says so instead of only offering Cancel', async (t) => {
  const { main } = await open(
    t,
    {
      '/v1/owner/settings': (body) =>
        body
          ? Object.assign(new Error('Confirm your sign-in before saving keys or delivery addresses'), {
              code: 'confirmation_required',
            })
          : settings(),
    },
    { source: 'ebird', localPasswordAllowed: false },
  );
  field(main, 'ebird_api_key').value = 'replacement';
  submit(main);
  findButton(main, 'Save settings').fire('click');
  await flush();
  const notice = walk(main).find((node) => node.classes.has('confirm-identity'));
  assert.ok(notice);
  assert.ok(
    walk(notice).some(
      (node) => node.textContent === 'Confirming a sign-in is not available right now. Try again later.',
    ),
  );
  assert.ok(findButton(notice, 'Cancel confirmation'));
});

test('destination fields have visible labels and say when a secret is saved', async (t) => {
  const { main } = await open(t, { '/v1/owner/settings': settings() });
  const labels = walk(main).filter((node) => node.tagName === 'LABEL');
  const named = (text) => labels.filter((node) => node.textContent === text);
  // Two saved destinations, each with both labels tied to their inputs.
  for (const text of ['Destination name', 'Private notification URL']) {
    assert.equal(named(text).length, 2);
    for (const label of named(text))
      assert.ok(walk(main).some((node) => node.tagName === 'INPUT' && node.id === label.htmlFor));
  }
  const shown = walk(main).map((node) => node.textContent);
  assert.equal(shown.filter((text) => text === 'A URL is saved. Leave this empty to keep it.').length, 2);
  // The Geoapify key on this page is not saved; the eBird key on its own page is.
  const saved = (root) =>
    walk(root).filter((node) => node.textContent === 'Saved. Leave this empty to keep it.').length;
  assert.equal(saved(main), 0);
  const ebird = await open(t, { '/v1/owner/settings': settings() }, { source: 'ebird' });
  assert.equal(saved(ebird.main), 1);
});

test('the plate making note promises only what Inky does with each new plate', async (t) => {
  const { main } = await open(t, { '/v1/owner/settings': settings() });
  const shown = walk(main).map((node) => node.textContent).join('\n');
  // A plate that passes its checks joins the journal without the owner's look.
  assert.doesNotMatch(shown, /before you keep it/);
  assert.match(shown, /Every new plate must pass its checks before it joins your journal\./);
});

test('the regular-check choices are named in plain words', async (t) => {
  const { main } = await open(t, { '/v1/owner/settings': settings() });
  const shown = walk(main).map((node) => node.textContent).join('\n');
  assert.doesNotMatch(shown, /Shared live selection|live discovery/);
  assert.match(shown, /What your regular checks look for/);
});

test('undoing adoption names the copy Inky keeps of the original file', async (t) => {
  const managed = settings({
    ownership: 'managed',
    editable: false,
    original_backup: 'config.toml.original',
    original_backup_available: false,
  });
  const { main } = await open(t, { '/v1/owner/settings': managed });
  findButton(main, 'Preview switch to app-managed settings').click();
  const shown = walk(main).map((node) => node.textContent).join('\n');
  assert.match(shown, /To undo adoption, stop Inky, put config\.toml\.original back in place of your configuration file, then run setup again\./);
  // An older server that does not name the copy keeps the general sentence.
  const older = await open(t, { '/v1/owner/settings': settings({ ownership: 'managed', editable: false }) });
  findButton(older.main, 'Preview switch to app-managed settings').click();
  assert.match(walk(older.main).map((node) => node.textContent).join('\n'), /restore the matching previous file/);
});
