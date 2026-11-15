// Settings edit the controller's authoritative file. Secrets are write-only.
import { inlineProblem } from '/app/ui.js';
import { windowName, eventName } from '/app/words.js';

const secretNames = {
  ebird_api_key: 'eBird API key',
  birdweather_token: 'BirdWeather station token',
  geoapify_api_key: 'Geoapify API key',
};

export async function renderSettings(ui, { base, link, note, sourceNames, source }) {
  const { element: el, button, api } = ui;
  const main = document.querySelector('#main');
  base(
    source ? sourceNames[source][0] : 'Settings and backups',
    source
      ? sourceNames[source][1]
      : 'Choose how Inky runs. Installation and network settings stay with the installer.',
  );
  if (source) main.append(link('Back to Sources', 'sources'));
  main.append(link('Back to Manage', 'manage'));
  const problem = el('p', '', 'error');
  problem.setAttribute('role', 'alert');
  const privateNotice = note(
    'HTTP does not encrypt settings or credentials in transit. Use a unique Inky password on a trusted home network. Use HTTPS for untrusted networks.',
  );
  if (location.protocol === 'http:') main.append(privateNotice);
  if (source === 'birdbuddy') await birdBuddyAccount(ui, main, note);
  let settings;
  try {
    settings = await api('/v1/owner/settings');
  } catch (error) {
    inlineProblem(ui, main, error, { label: 'Try again', action: () => location.reload() });
    return;
  }
  if (settings.restart_required) {
    main.append(
      el('h2', 'Finish switching configuration ownership'),
      note(
        'Your file is now app-managed. Re-run the controller installer with this same file, then restart Inky. This replaces fixed timers with the application scheduler. Until then, browser edits stay locked. No source history or plates were removed.',
      ),
    );
    return;
  }
  if (!settings.editable) {
    main.append(
      el('h2', 'Managed by your installer'),
      note(
        'Your TOML file and environment remain authoritative. Change the file, validate it, then re-run native setup or restart Docker. This page never edits a temporary copy that startup will replace.',
      ),
    );
    if (settings.revision)
      main.append(
        button('Preview switch to app-managed settings', () => {
          const preview = el('section', '', 'notice');
          preview.append(
            el('h2', 'Keep one configuration file'),
            note(
              'Inky will back up this private file, then mark it app-managed. Your source choices, generation policy, credentials and history stay unchanged. Future browser saves update this same file. Environment credentials stay locked.',
            ),
            ...(settings.adoption_notice ? [note(settings.adoption_notice)] : []),
            note(
              'You must re-run native setup and restart the service after this change. Docker will preserve this app-managed file on restart. Host, HTTPS and identity settings remain installer-owned. To undo adoption, stop Inky and restore the matching previous file, then run setup again.',
            ),
            button('Confirm switch', async (event) => {
              const control = event.currentTarget;
              control.disabled = true;
              try {
                await api('/v1/owner/settings', {
                  revision: settings.revision,
                  changes: {},
                  adopt: true,
                });
                location.reload();
              } catch (error) {
                problem.textContent = error.message;
                control.disabled = false;
              }
            }),
            button('Keep managed settings', () => preview.remove()),
          );
          main.append(preview, problem);
          preview.querySelector('button').focus();
        }),
      );
    return;
  }
  const form = el('form'),
    fields = settings.discovery;
  const changes = { discovery: {}, controller: {}, notifications: {} };
  const read = [];
  function section(title, text = '') {
    const fieldset = el('fieldset');
    fieldset.append(el('legend', title));
    if (text) fieldset.append(note(text));
    form.append(fieldset);
    return fieldset;
  }
  function input(
    parent,
    key,
    label,
    value,
    { type = 'text', hint = '', secret = false, external = false, target = 'discovery' } = {},
  ) {
    const field = el('input'),
      caption = el('label', label);
    field.id = 'setting-' + key;
    caption.htmlFor = field.id;
    field.type = secret ? 'password' : type;
    field.value = value ?? '';
    field.autocomplete = secret ? 'new-password' : 'off';
    field.spellcheck = false;
    field.disabled = external;
    if (type === 'number') {
      field.step = ['latitude', 'longitude'].includes(key) ? 'any' : '1';
    }
    if (external) field.placeholder = 'Managed by the installer environment';
    else if (secret)
      field.placeholder = settings.credentials[key]?.configured
        ? 'Already set. Leave empty to keep it.'
        : 'Not set';
    parent.append(caption, field);
    // A placeholder disappears once typing starts, so say it in text too.
    if (secret && !external && settings.credentials[key]?.configured)
      parent.append(note('Saved. Leave this empty to keep it.'));
    if (hint) parent.append(note(hint));
    read.push(() => {
      if (external || (secret && !field.value)) return;
      changes[target][key] =
        field.value.trim() === '' ? null : type === 'number' ? Number(field.value) : field.value;
    });
    return field;
  }
  function checkbox(parent, label, checked, readValue) {
    const row = el('label', '', 'check-label'),
      field = el('input');
    field.type = 'checkbox';
    field.checked = checked;
    row.append(field, document.createTextNode(label));
    parent.append(row);
    read.push(() => readValue(field.checked));
    return field;
  }
  if (source) {
    const group = section(
      'Use this source',
      'Birds this source notes can go on your frame. Plates for them are made only if plate making is on.',
    );
    checkbox(
      group,
      'Check ' + sourceNames[source][0] + ' for new birds',
      fields.sources.includes(source),
      (checked) => {
        // Sources this page does not know, such as one from a newer release, stay.
        const others = fields.sources.filter((key) => key !== source);
        changes.discovery.sources = checked
          ? fields.sources.includes(source)
            ? [...fields.sources]
            : [...others, source]
          : others;
      },
    );
    if (['inaturalist', 'ebird'].includes(source))
      main.append(note('Location and the live window are shared. Open Settings to change them.'));
    const secrets = {
      ebird: ['ebird_api_key', 'eBird API key'],
      birdweather: ['birdweather_token', 'BirdWeather station token'],
    };
    if (secrets[source]) {
      const [key, name] = secrets[source];
      input(section('Connection'), key, name, '', {
        secret: true,
        external: settings.credentials[key]?.external,
      });
    }
    if (source === 'birdnet-go')
      input(
        section(
          'Connection',
          'Use a server address that this controller can reach. Do not use an HTTPS login page in place of the BirdNET-Go API.',
        ),
        'birdnet_go_url',
        'BirdNET-Go base URL',
        fields.birdnet_go_url,
        { type: 'url' },
      );
    if (source === 'birdbuddy') {
      const group = section(
        'Birds added by hand',
        'Birds added by hand in Bird Buddy can come from your whole account, not just this feeder. They are separate from the postcards your feeder sends.',
      );
      checkbox(
        group,
        'Include birds added by hand',
        fields.birdbuddy_include_manual_sightings,
        (checked) => {
          changes.discovery.birdbuddy_include_manual_sightings = checked;
        },
      );
    }
    if (['ebird-archive', 'birdnet-analyzer'].includes(source))
      form.append(
        note(
          'This is history you imported, not a live connection. Turning it on uses its records within your window; it never fetches a new file.',
        ),
      );
  } else {
    const locationGroup = section(
      'Location for community reports',
      'Choose one form. This location applies to iNaturalist and nearby eBird only, not your stations or imported trips.',
    );
    const mode = el('select');
    mode.setAttribute('aria-label', 'Location format');
    for (const [value, name] of [
      ['none', 'No community location'],
      ['coordinates', 'Latitude and longitude'],
      ['zip', 'US ZIP code'],
      ['postal', 'International postal code'],
    ]) {
      const option = el('option', name);
      option.value = value;
      mode.append(option);
    }
    mode.value =
      fields.latitude !== undefined
        ? 'coordinates'
        : fields.zip_code
          ? 'zip'
          : fields.postal_code
            ? 'postal'
            : 'none';
    locationGroup.append(mode);
    const rows = new Map();
    for (const [key, title, type, group] of [
      ['latitude', 'Latitude', 'number', 'coordinates'],
      ['longitude', 'Longitude', 'number', 'coordinates'],
      ['zip_code', 'US ZIP code', 'text', 'zip'],
      ['postal_code', 'Postal code', 'text', 'postal'],
      ['country_code', 'Two-letter country code', 'text', 'postal'],
    ]) {
      const row = el('div');
      locationGroup.append(row);
      input(row, key, title, fields[key], { type });
      rows.set(key, [row, group]);
    }
    const geocoder = el('div');
    locationGroup.append(geocoder);
    input(geocoder, 'geoapify_api_key', 'Geoapify API key', '', {
      secret: true,
      external: settings.credentials.geoapify_api_key.external,
    });
    function updateLocation() {
      for (const [row, group] of rows.values()) row.hidden = group !== mode.value;
      geocoder.hidden = mode.value !== 'postal';
    }
    mode.addEventListener('change', updateLocation);
    updateLocation();
    read.push(() => {
      for (const [key, [, group]] of rows) if (group !== mode.value) changes.discovery[key] = null;
      if (mode.value !== 'postal' && !settings.credentials.geoapify_api_key.external)
        changes.discovery.geoapify_api_key = null;
    });
    const selection = section(
      'Shared live selection',
      'These choices set what your regular checks look for. Collections keep their own birds. eBird nearby looks back at most 30 days and 50 km.',
    );
    const window = el('select');
    window.setAttribute('aria-label', 'Observation window');
    for (const key of ['last-day', 'last-week', 'last-30-days', 'last-year', 'all-time']) {
      const option = el('option', windowName(key));
      option.value = key;
      window.append(option);
    }
    window.value = fields.window;
    selection.append(window);
    read.push(() => {
      changes.discovery.window = window.value;
    });
    input(selection, 'radius_km', 'Radius in kilometers', fields.radius_km, { type: 'number' });
    input(selection, 'species_limit', 'Live species limit', fields.species_limit, {
      type: 'number',
      hint: 'This limits live discovery, not the membership of a saved trip. BirdWeather supports at most 100.',
    });
    const generation = section(
      'Plate making',
      'Browsing your plates is free. New plates are drawn with your own image-generation account, and that provider’s charges apply. Set the account up on the controller first, then run the controller doctor. Every new plate is checked before you keep it. Turning this off keeps your history and waiting requests; a plate already being drawn can finish.',
    );
    checkbox(
      generation,
      'Make new plates on a schedule',
      settings.generation_enabled,
      (checked) => {
        changes.controller.generation_enabled = checked;
      },
    );
    const consent = checkbox(
      generation,
      'I understand this can start using my image-generation account at the next check.',
      false,
      () => {},
    );
    read.push(() => {
      if (!settings.generation_enabled && changes.controller.generation_enabled && !consent.checked)
        throw new Error('Tick the box to confirm before turning plate making on.');
    });
    const notifications = section(
      'Notifications',
      'Secrets are never returned. Leave an existing destination URL empty to keep it. Changing its name requires its URL again. Removing a row removes that destination after confirmation.',
    );
    checkbox(notifications, 'Enable notifications', settings.notifications.enabled, (checked) => {
      changes.notifications.enabled = checked;
    });
    const destinationRows = el('div');
    notifications.append(destinationRows);
    const external = settings.notifications.destinations.some((item) => item.external);
    const destinationReaders = [];
    let destinationCount = 0;
    function destination(saved = {}) {
      const row = el('fieldset');
      row.append(el('legend', 'Destination'));
      const name = el('input');
      name.value = saved.name || '';
      name.id = 'destination-name-' + ++destinationCount;
      const nameLabel = el('label', 'Destination name');
      nameLabel.htmlFor = name.id;
      const url = el('input');
      url.type = 'password';
      url.autocomplete = 'new-password';
      url.id = 'destination-url-' + destinationCount;
      const urlLabel = el('label', 'Private notification URL');
      urlLabel.htmlFor = url.id;
      url.placeholder = saved.name
        ? 'Already set. Leave empty to keep.'
        : 'Apprise notification URL';
      row.append(nameLabel, name, urlLabel, url);
      if (saved.name) row.append(note('A URL is saved. Leave this empty to keep it.'));
      const known = [
        'discovery',
        'generation_approved',
        'terminal_error',
        'degraded',
        'recovered',
        'publication_error',
        'publication_recovered',
        'display_stale',
        'display_recovered',
      ];
      // An event this page does not list stays with its destination.
      const events = [...known, ...(saved.events || []).filter((event) => !known.includes(event))];
      const checked = [];
      for (const event of events) {
        const label = el('label', '', 'check-label'),
          field = el('input');
        field.type = 'checkbox';
        field.checked = !saved.events || saved.events.includes(event);
        label.append(field, document.createTextNode(eventName(event)));
        row.append(label);
        checked.push([event, field]);
      }
      row.append(
        button('Remove destination', () => {
          row.remove();
          withdrawReview();
        }),
      );
      destinationRows.append(row);
      destinationReaders.push(() =>
        row.isConnected
          ? {
              name: name.value.trim(),
              ...(url.value ? { url: url.value } : {}),
              events: checked.filter(([, field]) => field.checked).map(([key]) => key),
            }
          : null,
      );
    }
    if (external)
      notifications.append(
        note(
          'Destination credentials are managed by the environment. Change them through the installer.',
        ),
      );
    else {
      for (const saved of settings.notifications.destinations) destination(saved);
      notifications.append(
        button('Add destination', () => {
          destination();
          withdrawReview();
        }),
      );
      read.push(() => {
        changes.notifications.destinations = destinationReaders.map((get) => get()).filter(Boolean);
      });
    }
    read.push(() => {
      changes.confirm_generation = consent.checked;
    });
  }
  const review = el('section', '', 'notice');
  review.hidden = true;
  const submit = el('button', 'Review changes');
  submit.type = 'submit';
  form.append(submit, problem);
  // Outside the form, so typing or pressing Enter in its sign-in confirmation
  // neither withdraws the review nor submits the form again.
  main.append(form, review);
  // A review describes one set of choices; any later change withdraws it.
  function withdrawReview() {
    review.hidden = true;
    review.replaceChildren();
  }
  form.addEventListener('input', withdrawReview);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    problem.textContent = '';
    review.replaceChildren();
    try {
      changes.discovery = {};
      changes.controller = {};
      changes.notifications = {};
      for (const get of read) get();
      const consent = changes.confirm_generation || false;
      delete changes.confirm_generation;
      const summary = [];
      if (source)
        summary.push(
          (changes.discovery.sources.includes(source) ? 'Check ' : 'Do not check ') +
            sourceNames[source][0] +
            ' for new birds.',
        );
      else
        summary.push(
          'Window: ' + windowName(changes.discovery.window) + '.',
          'Plate making: ' + (changes.controller.generation_enabled ? 'on' : 'off') + '.',
          'Notifications: ' + (changes.notifications.enabled ? 'on' : 'off') + '.',
        );
      summary.push(...differences(settings, changes));
      review.append(
        el('h2', 'Save these choices?'),
        ...summary.map(note),
        note(
          'Inky checks the whole file before saving and keeps a private copy of the previous one. Changes apply from the next check. Saving a source does not test its connection.',
        ),
        button('Save settings', async (event) => {
          const control = event.currentTarget;
          control.disabled = true;
          const save = async () => {
            await api('/v1/owner/settings', {
              revision: settings.revision,
              changes,
              confirm_generation: consent,
            });
            for (const field of form.querySelectorAll('input[type="password"]')) field.value = '';
            location.reload();
          };
          try {
            await save();
          } catch (error) {
            control.disabled = false;
            if (error.code === 'sign_in_required') ui.showProblem(error);
            else if (error.code === 'confirmation_required') confirmIdentity(ui, review, save);
            else problem.textContent = error.message;
          }
        }),
        button('Keep editing', () => {
          review.hidden = true;
          submit.focus();
        }),
      );
      review.hidden = false;
      review.querySelector('button').focus();
    } catch (error) {
      problem.textContent = error.message;
    }
  });
}

// Keys and delivery addresses need a recent sign-in. The choices stay on the
// page while the owner confirms, then the same save runs again.
function confirmIdentity(ui, place, retry) {
  const { element: el, button, api } = ui;
  place.querySelector('.confirm-identity')?.remove();
  const notice = el('section', '', 'notice confirm-identity');
  notice.append(
    el('p', 'Confirm it is you before saving keys or delivery addresses. Your choices stay on this page.'),
  );
  if (ui.localPasswordAllowed) {
    const label = el('label', 'Your Inky password'),
      password = el('input');
    password.type = 'password';
    password.id = 'settings-confirm-password';
    password.autocomplete = 'current-password';
    label.htmlFor = password.id;
    notice.append(
      label,
      password,
      button('Confirm and save', async () => {
        try {
          await api('/v1/owner/confirm-password', { passphrase: password.value });
        } catch (failure) {
          inlineProblem(ui, notice, failure);
          return;
        } finally {
          password.value = '';
        }
        notice.remove();
        try {
          await retry();
        } catch (failure) {
          inlineProblem(ui, place, failure);
        }
      }),
    );
    password.focus();
  }
  if (ui.singleSignOnReady)
    notice.append(
      button('Confirm with single sign-on', async () => {
        try {
          await ui.confirmWithSSO();
        } catch (failure) {
          inlineProblem(ui, notice, failure);
        }
      }),
      el(
        'p',
        'Sign-in returns to Account and access. Come back here and save again; unsaved entries will clear.',
      ),
    );
  if (!ui.localPasswordAllowed && !ui.singleSignOnReady)
    notice.append(el('p', 'Confirming a sign-in is not available right now. Try again later.'));
  notice.append(button('Cancel confirmation', () => notice.remove()));
  place.append(notice);
}

// What a save changes beyond the fixed summary: location, limits, secrets and
// destinations, so nothing is removed or replaced without being named.
function differences(settings, changes) {
  const fields = settings.discovery,
    next = changes.discovery,
    lines = [];
  const place = (values) =>
    values.latitude != null
      ? 'latitude ' + values.latitude + ', longitude ' + values.longitude
      : values.zip_code
        ? 'US ZIP code ' + values.zip_code
        : values.postal_code
          ? 'postal code ' + values.postal_code + (values.country_code ? ', ' + values.country_code : '')
          : 'no community location';
  const keys = ['latitude', 'longitude', 'zip_code', 'postal_code', 'country_code'];
  if (keys.some((key) => key in next)) {
    const after = Object.fromEntries(keys.map((key) => [key, key in next ? next[key] : fields[key]]));
    if (place(after) !== place(fields)) lines.push('Community location: ' + place(after) + '.');
  }
  if ('radius_km' in next && next.radius_km !== (fields.radius_km ?? null))
    lines.push('Radius: ' + (next.radius_km ?? 'not set') + (next.radius_km == null ? '.' : ' km.'));
  if ('species_limit' in next && next.species_limit !== (fields.species_limit ?? null))
    lines.push('Live species limit: ' + (next.species_limit ?? 'not set') + '.');
  if ('birdnet_go_url' in next && next.birdnet_go_url !== (fields.birdnet_go_url ?? null))
    lines.push('BirdNET-Go base URL: ' + (next.birdnet_go_url ?? 'not set') + '.');
  for (const [key, name] of Object.entries(secretNames)) {
    if (!(key in next)) continue;
    const saved = settings.credentials[key]?.configured;
    if (next[key] === null) {
      if (saved) lines.push('Clear the saved ' + name + '.');
    } else lines.push((saved ? 'Replace the saved ' : 'Save a new ') + name + '.');
  }
  const destinations = changes.notifications.destinations;
  if (destinations) {
    const before = settings.notifications.destinations.map((item) => item.name);
    const after = destinations.map((item) => item.name);
    for (const name of before)
      if (!after.includes(name)) lines.push('Remove notification destination: ' + name + '.');
    for (const item of destinations)
      if (!before.includes(item.name)) lines.push('Add notification destination: ' + item.name + '.');
      else if (item.url) lines.push('Replace the private URL for ' + item.name + '.');
  }
  return lines;
}

async function birdBuddyAccount(ui, main, note) {
  const { api, element: el, button } = ui;
  const section = el('section', '', 'journal-card');
  let response;
  try {
    response = await api('/v1/owner/birdbuddy');
  } catch (error) {
    // The rest of the page, including the switch for this source, still works.
    section.append(el('h2', 'Bird Buddy account'));
    main.append(section);
    inlineProblem(ui, section, error, { label: 'Try again', action: () => location.reload() });
    return;
  }
  const state = response.state;
  section.append(
    el('h2', 'Bird Buddy account'),
    note(
      'Use a dedicated email/password guest account. Bird Buddy’s private API is unsupported and can change without notice. You must obtain and maintain Bird Buddy’s permission. Inky cannot grant or verify it.',
    ),
  );
  const terms = el('a', 'Read Bird Buddy’s private-API restrictions', 'text-link');
  terms.href = 'https://mybirdbuddy.com/app-eula/';
  terms.target = '_blank';
  terms.rel = 'noopener noreferrer';
  const guests = el('a', 'Set up a guest feeder member', 'text-link');
  guests.href =
    'https://support.mybirdbuddy.com/hc/en-us/articles/4404431992337-Adding-guest-Feeder-Members-to-your-Birdbuddy';
  guests.target = '_blank';
  guests.rel = 'noopener noreferrer';
  section.append(terms, guests);
  const problem = el('p', '', 'error');
  problem.setAttribute('role', 'alert');
  if (state.authenticated) {
    section.append(
      el('p', 'Saved login for ' + state.feeder.name),
      note(
        'Role: ' +
          state.feeder.role +
          '. Its sign-in is saved; that alone does not show it still works. Use Check sources now on Sources to try it.',
      ),
    );
    if (!state.least_privilege_guest)
      section.append(
        el(
          'p',
          'This is an owner account. A dedicated guest account is the supported least-privilege setup. Reauthorize with a guest account.',
          'notice',
        ),
      );
    if (state.history) {
      section.append(
        note(
          'Last check that worked: ' +
            new Date(state.history.last_successful_sync_at).toLocaleString(),
        ),
        note(
          'Postcards in your journal: ' +
            state.history.retained_postcards +
            '. Birds added by hand: ' +
            (state.history.manual_sightings_included ? 'included' : 'left out') +
            '.',
        ),
      );
    }
    section.append(
      button('Remove local Bird Buddy login', async (event) => {
        if (
          !confirm(
            'Remove this Inky’s Bird Buddy login and your permission note? Birds already noted and your plates stay. This does not sign Inky out at Bird Buddy itself.',
          )
        )
          return;
        const control = event.currentTarget;
        control.disabled = true;
        try {
          await api('/v1/owner/birdbuddy/logout', { confirm: true });
          location.reload();
        } catch (error) {
          control.disabled = false;
          if (error.code === 'sign_in_required') ui.showProblem(error);
          else problem.textContent = error.message;
        }
      }),
    );
  } else
    section.append(
      note(
        state.history_present
          ? 'No saved login. Birds noted earlier stay in your journal.'
          : 'Not signed in. Nothing from Bird Buddy is in your journal yet.',
      ),
    );
  if (!response.secure_bootstrap) {
    section.append(
      el(
        'p',
        'Bird Buddy sign-in needs HTTPS, or a browser on the controller using its loopback address. Ordinary home-network HTTP cannot protect this guest password. The documented CLI login remains available.',
        'notice',
      ),
      problem,
    );
    main.append(section);
    return;
  }
  const form = el('form'),
    attestation = el('label', '', 'check-label'),
    confirmed = el('input');
  confirmed.type = 'checkbox';
  attestation.append(
    confirmed,
    document.createTextNode(
      'I confirm that Bird Buddy has authorized automated API access for this account.',
    ),
  );
  form.append(attestation);
  function credential(title, type, id) {
    const label = el('label', title),
      input = el('input');
    input.id = id;
    input.type = type;
    input.autocomplete = 'off';
    input.required = true;
    input.disabled = true;
    input.maxLength = type === 'email' ? 320 : 4096;
    label.htmlFor = id;
    form.append(label, input);
    return input;
  }
  const email = credential('Dedicated guest email', 'email', 'birdbuddy-email'),
    password = credential(
      'Guest password (used only for this sign-in)',
      'password',
      'birdbuddy-password',
    );
  const feeders = el('div'),
    submit = el(
      'button',
      state.authenticated ? 'Replace saved guest login' : 'Sign in to Bird Buddy',
    );
  submit.type = 'submit';
  submit.disabled = true;
  let choice = null;
  confirmed.addEventListener('change', () => {
    email.disabled = password.disabled = submit.disabled = !confirmed.checked;
    if (!confirmed.checked) {
      email.value = password.value = '';
      feeders.replaceChildren();
      choice = null;
    }
  });
  for (const input of [email, password])
    input.addEventListener('input', () => {
      feeders.replaceChildren();
      choice = null;
      submit.textContent = state.authenticated
        ? 'Replace saved guest login'
        : 'Sign in to Bird Buddy';
    });
  form.append(
    feeders,
    note(
      'Inky keeps the rotating refresh token, feeder choice and attestation date. It does not save this email or password. Choose the feeder explicitly if the guest can access more than one.',
    ),
    submit,
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!confirmed.checked) return;
    submit.disabled = true;
    problem.textContent = '';
    try {
      const body = { confirm: true, email: email.value, password: password.value };
      if (choice) {
        if (!choice.value) throw new Error('Choose the feeder for this installation.');
        body.feeder_id = choice.value;
      }
      const result = await api('/v1/owner/birdbuddy/login', body);
      if (!form.isConnected) return;
      if (result.choose_feeder) {
        const label = el('label', 'Choose this installation’s feeder');
        label.htmlFor = 'birdbuddy-feeder';
        choice = el('select');
        choice.id = label.htmlFor;
        choice.required = true;
        const empty = el('option', 'Choose a feeder');
        empty.value = '';
        choice.append(empty);
        for (const feeder of result.feeders) {
          const option = el('option', feeder.name + ' · ' + feeder.role);
          option.value = feeder.feeder_id;
          choice.append(option);
        }
        feeders.replaceChildren(label, choice);
        submit.textContent = 'Use the selected feeder';
        choice.focus();
      } else {
        email.value = password.value = '';
        location.reload();
      }
    } catch (error) {
      if (error.code === 'sign_in_required') {
        email.value = password.value = '';
        ui.showProblem(error);
      } else problem.textContent = error.message;
    } finally {
      submit.disabled = !confirmed.checked;
    }
  });
  section.append(form, problem);
  main.append(section);
}
