import { icon } from '/app/ui.js';
import { sourceState, nextCheck, requestState } from '/app/work-status.js';
import { plural, plainDate, windowName, eventName, when } from '/app/words.js';

export { plural };

// Results in plain words: no identifiers, hashes or raw status values.
const statusWords = {
  pending: 'Waiting to start.',
  running: 'Under way.',
  succeeded: 'Done.',
  failed: 'This did not finish.',
  interrupted: 'This stopped before it finished.',
};
export function statusText(status) {
  return statusWords[status] || 'Inky could not tell how this went.';
}
const shortStatus = {
  pending: 'Waiting',
  running: 'Under way',
  succeeded: 'Done',
  failed: 'Did not finish',
  interrupted: 'Stopped',
};

// What each request is called, and what happened once it was done.
const outcomes = {
  'saved-collection': ['Change to your frame', 'Your frame was changed. It updates at its next refresh.'],
  refresh: ['Source check', 'Your sources were checked.'],
  retry: ['Trying plates again', 'These birds are back in the works.'],
  'retry-now': ['Trying plates again', 'These birds will be tried next.'],
  'notification-test': ['Test notification', 'Inky tried each destination.'],
  'notification-retry': ['Sending again', 'Inky tried the undelivered messages again.'],
  'import-preview': ['History file check', 'The file was checked. Nothing has changed yet.'],
  'import-commit': ['History update', 'Your history was updated.'],
  'trip-preview': ['Trip check', 'The trip was checked. Nothing is saved yet.'],
  'trip-save': ['Saving a trip', 'The trip was saved.'],
  'queue-plates': ['Asking for plates', 'The missing plates are in the works.'],
  'approve-candidate': ['Keeping a new plate', 'The plate is in your journal.'],
  'reject-candidate': ['Sending a plate back', 'The plate was sent back.'],
  'community-preview': ['Community search', 'The search finished. Nothing is saved yet.'],
  'community-save': ['Saving a collection', 'The collection was saved.'],
};
export function outcomeTitle(kind) {
  return outcomes[kind]?.[0] || 'Your request';
}
export function outcomeSentence(receipt) {
  if (receipt.status !== 'succeeded') return statusText(receipt.status);
  // A history file with nothing new returns no counts.
  if (receipt.kind === 'import-commit' && !('rows' in (receipt.result || {})))
    return 'Your history had no changes.';
  return outcomes[receipt.kind]?.[1] || 'Done.';
}

const countWords = {
  added_count: ['bird added', 'birds added'],
  removed_count: ['bird removed', 'birds removed'],
  migrated_legacy_queue_count: ['bird kept from your earlier setup', 'birds kept from your earlier setup'],
  species_count: ['species', 'species'],
  skipped_count: ['species left out', 'species left out'],
  delivered_count: ['destination reached', 'destinations reached'],
  failed_count: ['destination not reached', 'destinations not reached'],
  requeued: ['message to send again', 'messages to send again'],
  attempted: ['message tried', 'messages tried'],
  delivered: ['message delivered', 'messages delivered'],
  failed: ['message not delivered', 'messages not delivered'],
  pending: ['message waiting', 'messages waiting'],
  dead_letters: ['message that could not be delivered', 'messages that could not be delivered'],
  queued: ['plate asked for', 'plates asked for'],
  already_queued: ['bird already waiting for a plate', 'birds already waiting for a plate'],
  held: ['bird left as it is', 'birds left as they are'],
  ready: ['bird already with a plate', 'birds already with a plate'],
};
// History import totals, named as the import review names them.
const totalWords = {
  rows: 'Rows in the file',
  checklists: 'Checklists',
  species: 'Bird names',
  duplicate_rows: 'Repeated rows',
  added_checklists: 'New checklists',
  updated_checklists: 'Changed checklists',
  removed_checklists: 'Removed checklists',
  unchanged_checklists: 'Unchanged checklists',
  imported: 'New records',
  updated: 'Changed records',
  removed: 'Removed records',
  duplicates: 'Unchanged records',
  dated: 'Dated records',
  undated: 'Undated records',
  earliest_observed_on: 'Earliest checklist',
  latest_observed_on: 'Latest checklist',
};
const flagWords = {
  generation_enabled: ['Plate making is on', 'Plate making is off'],
  dispatcher_busy: ['Messages were already being sent', ''],
  unchanged: ['Your history had no changes', ''],
  dry_run: ['Nothing was changed: this was a trial run', ''],
};
// Shown elsewhere on the page, or internal: identifiers, hashes and revisions.
const shownElsewhere = new Set(['message', 'taxon_ids', 'providers', 'error_type']);
const internal = /(^|_)(id|ids|hash|sha256|digest|revision|key)$/;

// A field from a newer controller, named readably rather than by its raw key.
function fieldLabel(key) {
  const words = key.replaceAll('_', ' ');
  return words[0].toUpperCase() + words.slice(1);
}
// A value as the owner reads it: a date as a date, a window as words.
function fieldValue(key, value) {
  if (key === 'window') return windowName(value);
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) return plainDate(value) || value;
  return String(value);
}

// One plain line per result field. A field this page has no words for still
// shows, under a readable label.
export function resultLines(result) {
  const lines = [];
  for (const [key, value] of Object.entries(result || {})) {
    if (shownElsewhere.has(key) || internal.test(key)) continue;
    if (typeof value === 'number' && countWords[key])
      lines.push(plural(value, ...countWords[key]));
    else if (typeof value === 'boolean' && flagWords[key]) {
      const text = flagWords[key][value ? 0 : 1];
      if (text) lines.push(text);
    } else if (['number', 'string'].includes(typeof value) && totalWords[key])
      lines.push(totalWords[key] + ': ' + fieldValue(key, value));
    else if (typeof value === 'number' || (typeof value === 'string' && value))
      lines.push(fieldLabel(key) + ': ' + fieldValue(key, value));
    else if (typeof value === 'boolean') lines.push(fieldLabel(key) + ': ' + (value ? 'yes' : 'no'));
  }
  return lines;
}

const finished = new Set(['succeeded', 'failed', 'interrupted']);
// The receipt once it finishes, or null when `active()` turns false first.
export async function waitForOperation(api, receipt, active) {
  while (!finished.has(receipt.status)) {
    await new Promise((resolve) => setTimeout(resolve, document.hidden ? 5000 : 1000));
    if (!active()) return null;
    receipt = await api('/v1/owner/operations/' + encodeURIComponent(receipt.operation_id));
  }
  return receipt;
}

const providerWords = {
  ok: 'checked',
  error: 'could not be checked',
  degraded: 'checked, but not completely',
};
// Where to look next after a finished request.
const nextPlaces = {
  refresh: [['Sources', 'sources']],
  retry: [['In the works', 'work']],
  'retry-now': [['In the works', 'work']],
  'queue-plates': [['In the works', 'work']],
  'approve-candidate': [['In the works', 'work']],
  'reject-candidate': [['In the works', 'work']],
  'notification-test': [['Notifications', 'notifications']],
  'notification-retry': [['Notifications', 'notifications']],
};

export async function renderOperation(context) {
  const { ui, el, button, api, main, query, byId, sourceNames, link, note, base, setStopScreen } =
    context;
  base('Your request');
  main.append(link('Back to Manage', 'manage'));
  const content = el('section', '', 'journal-card');
  main.append(content);
  let timer,
    stopped = false;
  setStopScreen(() => {
    stopped = true;
    clearTimeout(timer);
  });
  async function update() {
    try {
      const receipt = await api(
        '/v1/owner/operations/' + encodeURIComponent(query.get('operation') || ''),
      );
      if (stopped || !content.isConnected) return;
      const title = outcomeTitle(receipt.kind);
      const heading = main.querySelector('h1');
      if (heading) heading.textContent = title;
      document.title = title + ' | Inky';
      const sentence = el('p', outcomeSentence(receipt));
      sentence.setAttribute('role', 'status');
      content.replaceChildren(sentence);
      const result = receipt.result || {};
      // A finished request this page can name says it in its own sentence; the
      // controller's words stay for anything else, and for why one did not finish.
      if (
        typeof result.message === 'string' &&
        (receipt.status !== 'succeeded' || !outcomes[receipt.kind])
      )
        content.append(el('p', result.message));
      for (const id of Array.isArray(result.taxon_ids) ? result.taxon_ids : [])
        content.append(link(byId.get(id)?.common_name || 'Bird ' + id, 'work', { bird: id }));
      for (const provider of Array.isArray(result.providers) ? result.providers : [])
        content.append(
          el(
            'p',
            (sourceNames[provider.name]?.[0] || provider.name) +
              ': ' +
              (providerWords[provider.status] || 'not checked') +
              ' · ' +
              plural(provider.species_count, 'species', 'species'),
          ),
        );
      for (const line of resultLines(result)) content.append(note(line));
      if (['pending', 'running'].includes(receipt.status))
        timer = setTimeout(update, document.hidden ? 5000 : 1000);
      else {
        if (receipt.kind === 'community-preview')
          content.append(link('See what was found', 'community', {}, 'primary-link'));
        if (receipt.result?.collection_id)
          content.append(
            link(
              'Open the collection',
              'collection',
              { view: receipt.result.collection_id },
              'primary-link',
            ),
          );
        for (const [text, page] of nextPlaces[receipt.kind] || []) content.append(link(text, page));
      }
    } catch (error) {
      if (error.code === 'sign_in_required') ui.showProblem(error);
      else if (!stopped) {
        content.replaceChildren(note(error.message), button('Check again', update));
      }
    }
  }
  await update();
}

export async function renderGuides(context) {
  const { el, api, main, area, link, note, base, empty } = context;
  base(
    area === 'health'
      ? 'Health and help'
      : area === 'backups'
        ? 'Backups and upgrades'
        : 'Advanced tools',
  );
  main.append(link('Back to Manage', 'manage'));
  function guide(title, path, text) {
    const section = el('section', '', 'journal-card'),
      anchor = el('a', 'Open ' + title, 'text-link');
    anchor.href = 'https://github.com/veteranbv/inky-bird/blob/main/docs/' + path;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    section.append(el('h2', title), note(text), anchor);
    main.append(section);
  }
  if (area === 'health') {
    const health = await api('/v1/owner/health');
    main.append(note('Installed version: ' + health.version), note(health.notice));
    for (const check of health.checks)
      main.append(empty(check.name + ' · ' + check.status, check.detail));
    guide(
      'Operations',
      'operations.md',
      'For a closer look, run the controller and display doctor on the computers they are installed on. This page cannot run them or show raw logs.',
    );
    main.append(
      link('Sources', 'sources'),
      link('In the works', 'work'),
      link('Notifications', 'notifications'),
    );
  } else if (area === 'backups') {
    guide(
      'Backup and restore',
      'backup.md',
      'Stop Inky first, then back up its settings, its private state (which holds your keys), your plates and its working files together. A collection file is not a backup.',
    );
    guide(
      'Upgrade and migration',
      'migration-v1.md',
      'An upgrade keeps your sources, plate-making settings, the birds on your frame and your wall frame’s address. Make a backup first, and keep it until the new version is working.',
    );
    main.append(link('Settings', 'settings'));
  } else {
    guide(
      'Plate repair',
      'operations.md',
      'Replacing a kept plate, starting a bird’s research again or choosing a correction source is done on the controller, with a fresh check of the plate. This page cannot skip those checks.',
    );
    guide(
      'Installation and services',
      'installation.md',
      'Addresses, services, certificates and package updates are set by the installer, not from this page.',
    );
    guide(
      'HTTPS and Authentik',
      'browser-access.md',
      'Single sign-on links one named owner account; it never trusts an email address or a group on its own. Never copy your identity service’s password into Inky.',
    );
  }
}

// What In the works lists under "Needs a hand": unfinished new plates, birds
// waiting for the owner to try again, and stopped ones. A new plate waiting for
// a look is good news, not one of them.
function handGroups(work) {
  const terminal = (work?.terminal_details || []).filter((detail) => detail.state !== 'pending');
  const unfinished = (work?.in_review?.candidates || []).filter(
    (candidate) =>
      !candidate.inspect_available &&
      !terminal.some((detail) => detail.taxon_id === candidate.taxon_id),
  );
  const waiting = (work?.retry_details || []).filter((detail) =>
    (work.needs_attention || []).includes(detail.taxon_id),
  );
  return { terminal, unfinished, waiting };
}
export function needsHand(work) {
  const { terminal, unfinished, waiting } = handGroups(work);
  return new Set([...unfinished, ...waiting, ...terminal].map((bird) => bird.taxon_id));
}

// What needs the owner, counted for Manage. Good news first.
export function manageNotes(management) {
  if (!management?.available) return [];
  const candidates = management.work?.in_review?.candidates || [];
  const ready = new Set(
    candidates.filter((candidate) => candidate.inspect_available).map((bird) => bird.taxon_id),
  ).size;
  const hand = needsHand(management.work).size;
  const failing = (management.sources || []).filter(
    (source) => source.enabled && source.status === 'error',
  ).length;
  const notes = [];
  if (ready)
    notes.push([
      ready === 1 ? '1 new plate is ready to look at.' : ready + ' new plates are ready to look at.',
      'work',
    ]);
  if (hand) notes.push([hand === 1 ? '1 plate needs a hand' : hand + ' plates need a hand', 'work']);
  if (failing)
    notes.push([
      failing === 1 ? '1 source could not be checked' : failing + ' sources could not be checked',
      'sources',
    ]);
  if (management.notifications?.dead_letters)
    notes.push(['Some notifications did not get through', 'notifications']);
  return notes;
}

const manageGroups = [
  [
    'Birds and sources',
    [['sources', 'Sources', 'Where your birds come from, and when each was last checked.']],
  ],
  ['Plates', [['work', 'In the works', 'See which plates are being made, and what is waiting.']]],
  [
    'Frame and displays',
    [
      [
        'displays',
        'Wall frame',
        'When your wall frame last changed, and the screens that show your birds.',
      ],
    ],
  ],
  [
    'Integrations',
    [
      [
        'integrations',
        'Home Assistant and Homepage',
        'Show your plates and how Inky is doing on a home dashboard.',
      ],
      ['notifications', 'Notifications', 'Where Inky sends news, and whether it arrived.'],
    ],
  ],
  [
    'System',
    [
      ['settings', 'Settings and backups', 'Sources, plate making and where messages go.'],
      ['health', 'Health and help', 'Check that everything is running, and find help.'],
      ['backups', 'Backups and upgrades', 'Keep a copy of your journal before you upgrade.'],
      ['advanced', 'Advanced tools', 'Host setup and plate repair, for whoever installed Inky.'],
    ],
  ],
];
const glyphs = {
  sources: 'birds',
  work: 'feather',
  displays: 'frame',
  integrations: 'pulse',
  notifications: 'bell',
  settings: 'manage',
  health: 'pulse',
  backups: 'archive',
  advanced: 'wrench',
};

export function renderManage(context) {
  const { ui, el, button, main, link, note, base, management } = context;
  base('Manage');
  const notes = manageNotes(management);
  if (notes.length) {
    const list = el('ul', '', 'manage-notes');
    for (const [text, page] of notes) {
      const item = el('li');
      item.append(link(text, page));
      list.append(item);
    }
    main.append(list);
  }
  for (const [title, cards] of manageGroups) {
    const group = el('section', '', 'manage-group');
    const grid = el('div', '', 'manage-grid');
    for (const [key, name, text] of cards) {
      const node = link('', key, {}, 'journal-card');
      node.append(icon(glyphs[key]), el('h3', name), el('p', text));
      grid.append(node);
    }
    group.append(el('h2', title), grid);
    main.append(group);
  }
  const account = button('Account and access', ui.account);
  account.prepend(icon('account'));
  main.append(account, button('Sign out', ui.signOut));
  const about = el('section', '', 'journal-card about-card');
  about.append(el('h2', 'About Inky Bird'));
  about.append(
    note('Inky Bird is free, open-source software made by Henry Sowell.'),
  );
  const places = el('p');
  for (const [text, url] of [
    ['henrysowell.com', 'https://henrysowell.com'],
    ['GitHub', 'https://github.com/veteranbv'],
    ['X', 'https://x.com/realhenry'],
  ]) {
    const anchor = el('a', text, 'text-link');
    anchor.href = url;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    places.append(anchor);
  }
  about.append(places);
  const feedback = el('p', '', 'hint');
  const issues = el('a', 'GitHub issues');
  issues.href = 'https://github.com/veteranbv/inky-bird/issues';
  issues.target = '_blank';
  issues.rel = 'noopener noreferrer';
  feedback.append(document.createTextNode('Ideas and feedback are welcome on '), issues, document.createTextNode('.'));
  about.append(feedback);
  main.append(about);
}

// Plate-making stages as the owner would say them.
const stageWords = {
  starting: 'Getting ready',
  references: 'Finding reference photos',
  research: 'Reading about the bird',
  generating: 'Drawing',
  reviewing: 'Checking the drawing',
  preparing_retries: 'Getting retries ready',
  approving: 'Adding it to your journal',
};
const holdWords = {
  pending: 'A new plate is waiting for you to look at.',
  incomplete_pending: 'Its new plate is unfinished and needs a hand.',
  rejected: 'You sent its last plate back.',
  failed: 'Its last try did not work.',
};
const axisWords = {
  species_accuracy: 'Right bird',
  anatomy_accuracy: 'Anatomy',
  text_accuracy: 'Lettering',
  composition_quality: 'Composition',
};

export async function renderStatus(context) {
  const {
    ui, el, api, main, query, area, data, byId, management, sourceNames, link, note, base,
    unreadable, empty, operationAction, currentRun, setStopScreen,
  } = context;
  base(
    {
      sources: 'Sources',
      work: 'In the works',
      notifications: 'Notifications',
    }[area],
    area === 'work' ? 'Plates are made one at a time.' : '',
  );
  main.append(link('Back to Manage', 'manage'));
  if (!management.available) {
    main.append(
      management.reason === 'unreadable'
        ? unreadable([
            (data.notices || []).find((notice) => notice.part === 'plate-work')?.message ||
              'Plate work could not be read.',
          ])
        : empty(
            'Full configuration unavailable',
            'Start Inky through its documented serve command to include controller settings.',
          ),
    );
    return;
  }
  function stamp(label, value) {
    return note(label + ': ' + (when(value) || 'not yet'));
  }
  function pollManagement(draw) {
    let timer, stopped = false, latest = management;
    const notice = el('p', '', 'hint');
    main.append(notice);
    setStopScreen(() => { stopped = true; clearTimeout(timer); });
    function show(current, unavailable) {
      const focused = document.activeElement, region = focused?.closest('section');
      const target = focused?.tagName === 'A' ? focused.getAttribute('href') : null;
      draw(current, unavailable);
      if (target && region?.isConnected && !focused.isConnected) {
        const replacement = [...region.querySelectorAll('a')].find((node) => node.getAttribute('href') === target);
        if (replacement) replacement.focus({ preventScroll: true });
        else { region.tabIndex = -1; region.focus({ preventScroll: true }); }
      }
    }
    async function update() {
      try {
        const result = await api('/v1/owner/management');
        if (stopped || currentRun() !== pendingRun) return;
        latest = result;
        show(latest, false);
        notice.textContent = 'Updated ' + new Date().toLocaleTimeString() + '. This page keeps itself up to date.';
      } catch (error) {
        if (stopped) return;
        if (error.code === 'sign_in_required') { ui.showProblem(error); return; }
        show(latest, true);
        notice.textContent = 'Inky is not answering right now, so this may be out of date.';
      }
      if (!stopped) timer = setTimeout(update, document.hidden ? 15000 : 5000);
    }
    const pendingRun = currentRun();
    show(latest, false);
    timer = setTimeout(update, 5000);
  }
  if (area === 'sources') {
    const span = management.window === 'all-time' ? 'at any time' : 'in the ' + windowName(management.window).toLowerCase();
    main.append(note('Birds noted ' + span + '. Inky checks each source on a schedule; it does not listen all the time.'));
    main.append(operationAction('Check sources now', 'refresh',
      'Inky checks each source that is on. One that fails does not stop the others.'));
    const schedule = el('section', '', 'journal-card');
    main.append(schedule);
    const rows = new Map();
    for (const source of management.sources) {
      // A newer controller can report a source this page does not know yet.
      const known = Boolean(sourceNames[source.name]);
      const [name] = sourceNames[source.name] || [source.name];
      const node = el('section', '', 'journal-card source-row');
      const top = el('div', '', 'source-top');
      const chip = el('span', '', 'chip');
      top.append(el('h2', name), chip);
      const summary = note('');
      const details = el('details');
      details.append(el('summary', 'Details'));
      const body = el('div');
      details.append(body);
      node.append(top, summary, details);
      if (['ebird-archive', 'birdnet-analyzer'].includes(source.name))
        node.append(link('Import history', 'imports', { kind: source.name }));
      if (known) {
        const setup = link('Set up', 'settings', { source: source.name }, 'primary-link');
        setup.setAttribute('aria-label', 'Set up ' + name);
        node.append(setup);
      }
      rows.set(source.name, { chip, summary, details, body, kind: sourceNames[source.name]?.[1] });
      main.append(node);
    }
    const detections = el('section', '', 'journal-card');
    main.append(detections);
    pollManagement((current, unavailable) => {
      const refresh = current.activity?.entries?.['job-refresh'] || {};
      schedule.replaceChildren(el('h2', 'When sources are checked'),
        note('Every ' + plural(current.refresh_minutes, 'minute', 'minutes') + ', counted from the end of the last check.'),
        note(unavailable ? 'Next check: not known right now.' : nextCheck(refresh)));
      for (const source of current.sources) {
        const row = rows.get(source.name), activity = source.activity || {};
        if (!row) continue;
        row.chip.textContent = unavailable && source.enabled ? 'Not known right now' : sourceState(source, current.work.complete);
        const species = activity.species_count ?? source.species_count;
        const unmatched = activity.unresolved_count ?? source.unresolved_count;
        const checked = when(activity.last_success_at || source.last_checked_at);
        row.summary.textContent = !source.enabled
          ? ''
          : [
              typeof species === 'number' ? plural(species, 'species', 'species') : '',
              checked ? 'last checked ' + checked : 'not checked yet',
            ].filter(Boolean).join(' · ');
        row.summary.hidden = !source.enabled;
        row.details.hidden = !source.enabled;
        row.body.replaceChildren(
          ...(row.kind ? [note(row.kind)] : []),
          stamp('First check recorded', activity.first_started_at),
          stamp('Last check started', activity.started_at),
          stamp('Last check finished', activity.finished_at),
          stamp('Last good check', activity.last_success_at),
          stamp('Results saved', source.last_checked_at),
          stamp('Last noted', source.latest_detection_at),
          ...(typeof unmatched === 'number' && unmatched
            ? [note(plural(unmatched, 'name', 'names') + ' Inky could not match')]
            : []),
        );
        if (source.enabled && (source.status === 'error' || activity.status === 'failed'))
          row.body.append(note('Check this source’s settings. The controller log has the details.'));
      }
      detections.replaceChildren(el('h2', 'Recently noted'),
        note('The latest record of each bird, up to 100.'),
        stamp('Updated', current.discovery_refreshed_at));
      if (!current.work.complete && current.discovery_refreshed_at) detections.append(note('The last check did not finish, so these may be out of date.'));
      for (const bird of current.recent_detections || [])
        detections.append(link(bird.common_name, 'bird', { bird: bird.taxon_id }),
          note('Noted ' + (when(bird.latest_detection_at) || 'at an unknown time') + ' · ' +
            (bird.timestamp_source
              ? sourceNames[bird.timestamp_source]?.[0] || bird.timestamp_source
              : 'from ' + ((bird.sources || []).map((name) => sourceNames[name]?.[0] || name).join(', ') || 'your sources'))));
      if (!current.recent_detections?.length) detections.append(note('No dated records yet.'));
    });
  } else if (area === 'work') {
    main.append(link('Plate-making settings', 'settings'));
    const runtime = el('section', '', 'journal-card'), queue = el('section', '', 'journal-card');
    main.append(runtime, queue);
    const selected = new Set(), controls = el('section', '', 'journal-card'), choices = el('div'), inputs = new Map();
    const charges = 'Plates are drawn with your own image-generation account, and its charges apply.';
    const retryNow = operationAction('Try as soon as possible', 'retry-now',
      'Inky checks sources and draws these next, after anything already being drawn. ' + charges, () => [...selected]);
    const noWake = note('“Try as soon as possible” needs plate making on and Inky’s own scheduler running.');
    retryNow.hidden = true;
    const queueAction = operationAction('Try at the next check', 'retry',
      'Your earlier notes are kept, and every new plate is checked again. ' + charges, () => [...selected]);
    // Checked here so a bad selection is a note beside the choices, not a page error.
    const selectionHint = el('p', '', 'hint');
    selectionHint.setAttribute('role', 'status');
    function checkSelection() {
      const valid = selected.size >= 1 && selected.size <= 32;
      for (const action of [queueAction, retryNow]) action.querySelector('button').disabled = !valid;
      selectionHint.textContent = valid || !inputs.size ? '' : 'Select between 1 and 32 birds.';
    }
    controls.append(el('h2', 'Choose birds to try again'), note('Choose up to 32. Plates you kept are never redrawn.'), choices,
      selectionHint, queueAction, retryNow, noWake);
    checkSelection();
    main.append(controls);
    const review = el('section', '', 'journal-card'), holds = el('section', '', 'journal-card'),
      retrying = el('section', '', 'journal-card'), requests = el('section', '', 'journal-card');
    main.append(review, holds, retrying, requests);
    pollManagement((current, unavailable) => {
      const work = current.work, activity = current.activity?.entries || {}, generation = activity.generation || {};
      retryNow.hidden = unavailable || current.retry_wake_available !== true;
      noWake.hidden = !retryNow.hidden;
      runtime.replaceChildren(el('h2', 'Now'));
      if (!current.generation_enabled) runtime.append(note('Plate making is off. Requests are kept until you turn it on.'));
      if (!unavailable && generation.status === 'running')
        runtime.append(el('p', (generation.common_name || 'A plate') + ' · ' + (stageWords[generation.stage] || 'Working') +
          (generation.attempt ? ' · try ' + generation.attempt : '')), stamp('Started', generation.started_at));
      else runtime.append(note(unavailable ? 'Not known right now.' : generation.status === 'unknown' ? 'Inky has not reported lately, so it may or may not be drawing.' : 'Nothing is being drawn right now.'),
        stamp('Last plate work finished', generation.finished_at));
      if (!work.complete) runtime.append(note('Sources need a good check before the next plate is made.'));
      if (!unavailable) for (const [key, label] of [['job-refresh', 'Checking sources now'], ['job-catalog-publish', 'Publishing your plates now'], ['job-notifications', 'Sending notifications now']])
        if (activity[key]?.status === 'running') runtime.append(note(label));
      if (current.generation_enabled) runtime.append(note(unavailable ? 'Next check: not known right now.' : nextCheck(activity['job-generate'])));
      const waiting = work.queue || [];
      queue.replaceChildren(el('h2', 'Next up'),
        note(waiting.length ? 'As of the last check. The order can change.' : 'Nothing is waiting.'));
      const list = el('ol');
      for (const bird of waiting)
        list.append(el('li', bird.common_name + (!unavailable && generation.status === 'running' && generation.taxon_id === bird.taxon_id ? ' · being drawn now' : '')));
      if (waiting.length) queue.append(list);
      const ids = new Set([...(work.can_run || []), ...(work.waiting || []), ...(work.needs_attention || [])]);
      const explicit = Number(query.get('bird'));
      if (explicit && byId.has(explicit) && !byId.get(explicit).plate_ready && !work.approved_taxon_ids?.includes(explicit)) ids.add(explicit);
      for (const [id, row] of inputs) if (!ids.has(id)) { row.remove(); inputs.delete(id); selected.delete(id); }
      for (const id of ids) if (!inputs.has(id)) {
        const detail = [...(work.retry_details || []), ...(work.terminal_details || []), ...(work.queue || [])].find((bird) => bird.taxon_id === id);
        const label = el('label', '', 'check-label'), input = el('input');
        input.type = 'checkbox';
        input.addEventListener('change', () => {
          if (input.checked) selected.add(id);
          else selected.delete(id);
          checkSelection();
        });
        label.append(input, document.createTextNode(byId.get(id)?.common_name || detail?.common_name || 'Bird ' + id));
        choices.append(label); inputs.set(id, label);
      }
      checkSelection();
      if (!ids.size && !choices.querySelector('.empty-selection')) choices.append(el('p', 'No birds need trying again.', 'empty-selection'));
      if (ids.size) choices.querySelector('.empty-selection')?.remove();
      const candidates = work.in_review.candidates;
      const ready = candidates.filter((candidate) => candidate.inspect_available);
      review.replaceChildren(el('h2', 'New plates'));
      if (work.in_review.status !== 'ok') review.append(note('Inky could not read every new plate. Whoever runs Inky should take a look.'));
      review.append(note(!ready.length ? 'No new plates right now.' : ready.length === 1 ? '1 new plate is ready to look at.' : ready.length + ' new plates are ready to look at.'));
      for (const candidate of ready)
        review.append(link('Look at ' + candidate.common_name, 'candidate', { bird: candidate.taxon_id }));
      holds.replaceChildren(el('h2', 'Needs a hand'));
      retrying.replaceChildren(el('h2', 'Trying again soon'));
      let soon = 0;
      const { terminal, unfinished } = handGroups(work);
      for (const candidate of unfinished)
        holds.append(note(candidate.common_name + ': its new plate is unfinished. Whoever runs Inky can recover it before it is tried again.'));
      for (const detail of work.retry_details || []) {
        const waitsForOwner = work.needs_attention.includes(detail.taxon_id);
        if (!waitsForOwner) soon += 1;
        (waitsForOwner ? holds : retrying).append(
          link(detail.common_name || 'Bird ' + detail.taxon_id, 'bird', { bird: detail.taxon_id }),
          note(detail.failure_message), stamp('Last try', detail.last_failed_at),
          note(plural(detail.attempts, 'try', 'tries') + ' so far'),
          note(waitsForOwner ? 'Waiting for you to choose it above.'
            : 'Inky tries again on its own after ' + (when(detail.next_attempt_at) || 'a short wait') + '.'));
      }
      for (const detail of terminal) {
        holds.append(link(detail.common_name, 'bird', { bird: detail.taxon_id }),
          note(holdWords[detail.state] || 'It is waiting for you.'), note(detail.failure_message),
          stamp('Last try', detail.last_failed_at), stamp('Last started', detail.last_attempt_at),
          note((typeof detail.recorded_attempts === 'number' ? plural(detail.recorded_attempts, 'try', 'tries') + ' · ' : '') + 'each check needs 4 out of 5 or better'),
          ...Object.entries(detail.review_scores || {}).map(([axis, score]) => note((axisWords[axis] || axis.replaceAll('_', ' ')) + ': ' + score + ' out of 5')),
          note('Choose it above to try again.'));
      }
      if (!needsHand(work).size) holds.append(note('Nothing needs a hand.'));
      retrying.hidden = !soon;
      requests.replaceChildren(el('h2', 'Recent requests to try again'));
      for (const request of (work.retry_requests || []).slice(-32).reverse())
        requests.append(link(byId.get(request.taxon_id)?.common_name || 'Bird ' + request.taxon_id, 'bird', { bird: request.taxon_id }),
          note(requestState(request, work)), stamp('Asked', request.created_at), stamp('Updated', request.updated_at));
      if (!work.retry_requests?.length) requests.append(note('None yet.'));
    });
  } else {
    const state = management.notifications;
    main.append(
      el('p', state.enabled ? 'Notifications are on.' : 'Notifications are off.'),
      el(
        'p',
        [
          state.pending ? plural(state.pending, 'message waiting', 'messages waiting') : '',
          state.dead_letters
            ? plural(state.dead_letters, 'message could not be delivered', 'messages could not be delivered')
            : '',
        ].filter(Boolean).join(' · ') || 'Nothing is waiting to be sent.',
      ),
    );
    for (const destination of state.destinations)
      main.append(el('p', destination.name + ' · ' + destination.events.map(eventName).join(', ')));
    main.append(
      note('Each destination’s address stays on the controller.'),
    );
    main.append(link('Notification settings', 'settings'));
    if (state.enabled)
      main.append(
        operationAction(
          'Send a test notification',
          'notification-test',
          'Inky sends one test, clearly labeled, to each destination. It is a real message.',
        ),
      );
    if (state.enabled && state.dead_letters)
      main.append(
        operationAction(
          'Send undelivered messages again',
          'notification-retry',
          'Inky tries each undelivered message again. A destination that already has one does not get it twice.',
        ),
      );
  }
  const recent = await api('/v1/owner/operations');
  if (main.isConnected) main.append(...recentOperations(context, recent.operations));
}

function recentOperations(context, operations) {
  const { el, link } = context;
  if (!operations.length) return [];
  return [
    el('h2', 'Recent requests'),
    ...operations
      .slice()
      .reverse()
      .map((receipt) =>
        link(
          outcomeTitle(receipt.kind) + ' · ' + (shortStatus[receipt.status] || 'Not known'),
          'operation',
          { operation: receipt.operation_id },
        ),
      ),
  ];
}
