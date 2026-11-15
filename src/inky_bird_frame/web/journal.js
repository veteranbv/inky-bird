// The controller owns birds and collections. No private journal enters browser storage.
import { renderSettings } from '/app/settings.js';
import { renderImports } from '/app/imports.js';
import { renderPresentation } from '/app/presentation.js';
import { icon, inlineProblem, clearProblem } from '/app/ui.js';
import { randomHex } from '/app/keys.js';
import { renderHome } from '/app/home.js';
import { renderBirds, renderBird } from '/app/birds.js';
import {
  renderCollections,
  renderCommunity,
  renderEditCollection,
  renderImportCollection,
  renderCollection,
  savedChange,
} from '/app/collections.js';
import { renderFrame } from '/app/frame.js';
import { renderCandidate } from '/app/candidate.js';
import { renderOperation, renderGuides, renderManage, renderStatus } from '/app/manage.js';
let run = 0;
let stopScreen = () => {};
let frameSignOut = null;
export function retainSignedOutFrame() {
  if (!frameSignOut) return false;
  frameSignOut();
  return true;
}
export function closeJournal() {
  run += 1;
  stopScreen();
  stopScreen = () => {};
  frameSignOut = null;
  document.querySelector('#journal-navigation')?.remove();
  document.body.classList.remove('journal', 'presenting');
}

// Plates are 3:4. Grids use the small copy; Frame, Home and bird pages keep the full plate.
const plateSizes = { full: [1200, 1600], thumbnail: [480, 640] };
export function plateURL(bird, size = 'full') {
  return (
    '/v1/owner/journal/birds/' +
    bird.taxon_id +
    (size === 'thumbnail' ? '/thumbnail' : '/portrait') +
    '?revision=' +
    encodeURIComponent(bird.portrait_sha256)
  );
}

const sourceNames = {
  inaturalist: ['iNaturalist', 'Community reports'],
  ebird: ['eBird nearby', 'Community reports'],
  'ebird-archive': ['eBird Archive', 'Your eBird checklists'],
  birdweather: ['BirdWeather', 'Station detections'],
  'birdnet-go': ['BirdNET-Go', 'Station detections'],
  'birdnet-analyzer': ['BirdNET Analyzer', 'Imported detections'],
  birdbuddy: ['Bird Buddy', 'Feeder records'],
};

export async function openJournal(ui, message = '') {
  closeJournal();
  const pending = run;
  const data = await ui.api('/v1/owner/journal');
  if (run !== pending) return;
  const { element: el, button, api, screen } = ui;
  const main = document.querySelector('#main');
  const query = new URL(location.href).searchParams;
  const area = query.get('page') || 'home';
  const view = query.get('view') || 'current';
  const byId = new Map(data.birds.map((bird) => [bird.taxon_id, bird]));
  function href(page, extra = {}) {
    return '/app?' + new URLSearchParams({ page, ...extra });
  }
  function link(text, page, extra = {}, style = 'text-link') {
    const node = el('a', text, style);
    node.href = href(page, extra);
    return node;
  }
  function note(text) {
    return el('p', text, 'hint');
  }
  function art(bird, className = '', size = 'full') {
    const image = el('img', '', className);
    image.alt = 'Approved field-journal plate: ' + bird.common_name;
    // Set before src so the browser can defer the request and reserve the space.
    image.loading = 'lazy';
    image.decoding = 'async';
    const [width, height] = plateSizes[size];
    image.width = width;
    image.height = height;
    image.src = plateURL(bird, size);
    return image;
  }
  function evidence(bird) {
    return (
      [
        ...new Set(
          [...bird.sources, ...(bird.retained_evidence || []).map((item) => item.source)]
            .map((name) => sourceNames[name]?.[1])
            .filter(Boolean),
        ),
      ].join(' · ') ||
      (bird.sources.includes('owner-selection')
        ? 'Plate request or review · not a sighting'
        : bird.saved
          ? 'Saved selection'
          : bird.known
            ? 'Collection selection'
            : 'Available artwork · not a sighting')
    );
  }
  function card(bird) {
    const node = link('', 'bird', { bird: bird.taxon_id }, 'bird-card');
    if (bird.plate_ready) node.append(art(bird, '', 'thumbnail'));
    else node.append(el('div', 'Plate not ready', 'missing-art'));
    node.append(
      el('h2', bird.common_name),
      el('p', bird.scientific_name, 'scientific'),
      note(evidence(bird)),
    );
    return node;
  }
  function members(key) {
    if (key === 'all') return data.birds;
    const ids =
      key === 'favorites'
        ? data.collections.find((group) => group.kind === 'favorites')?.taxon_ids || []
        : key === 'saved'
          ? data.saved_taxon_ids
          : key === 'current'
            ? data.active_taxon_ids
            : data.collections.find((group) => group.collection_id === key)?.taxon_ids;
    if (!ids)
      throw new Error(
        'This collection is no longer available. Open Collections and choose another.',
      );
    return ids.map((id) => byId.get(id)).filter(Boolean);
  }
  function viewLabel(key) {
    return key === 'favorites'
      ? 'Favorites'
      : key === 'all'
        ? 'All available artwork'
        : key === 'saved'
          ? 'Saved birds'
          : key === 'current'
            ? 'Current and saved'
            : data.collections.find((group) => group.collection_id === key)?.label || 'Collection';
  }
  function base(title, description = '') {
    screen(title, description);
    document.body.classList.add('journal');
    const nav = el('nav', '', 'journal-navigation');
    nav.id = 'journal-navigation';
    nav.setAttribute('aria-label', 'Journal');
    for (const [key, name, glyph, extra] of [
      ['home', 'Home', 'home'],
      ['birds', 'Birds', 'birds'],
      ['collections', 'Collections', 'collections'],
      ['frame', 'Frame', 'frame', { view: 'current' }],
      ['manage', 'Manage', 'manage'],
    ]) {
      const item = link('', key, extra || {}, '');
      item.append(icon(glyph), el('span', name));
      if (key === area) item.setAttribute('aria-current', 'page');
      nav.append(item);
    }
    document.querySelector('header').after(nav);
    if (message) main.append(el('p', message, 'connection'));
    if (
      management.available &&
      !['manage', 'sources', 'work', 'notifications', 'frame', 'operation', 'candidate'].includes(
        area,
      )
    ) {
      // Each issue links to the screen that names the affected birds or
      // destinations and offers the action, not to a general directory.
      const issues = [];
      const stopped = management.work.needs_attention.length;
      if (stopped)
        issues.push([stopped === 1 ? '1 plate stopped' : stopped + ' plates stopped', 'work']);
      const candidates = management.work.in_review.candidates;
      const review = candidates.filter((candidate) => candidate.inspect_available).length;
      if (review)
        issues.push([
          review === 1 ? '1 plate ready for your review' : review + ' plates ready for your review',
          'work',
        ]);
      const held = candidates.length - review;
      if (held)
        issues.push([
          held === 1 ? '1 plate held for recovery' : held + ' plates held for recovery',
          'work',
        ]);
      if (management.notifications.dead_letters)
        issues.push(['notification delivery failing', 'notifications']);
      const failing = management.sources.filter(
        (source) => source.enabled && source.status === 'error',
      ).length;
      if (failing)
        issues.push([
          failing === 1 ? 'a source check failed' : failing + ' source checks failed',
          'sources',
        ]);
      if (issues.length) {
        const banner = el('div', '', 'attention-banner');
        banner.setAttribute('role', 'status');
        banner.append(icon('alert'));
        for (const [text, page] of issues) banner.append(link(text, page, {}, 'attention-item'));
        main.append(banner);
      }
    }
  }
  // Parts the controller could not read; the rest of the journal still works.
  function unreadable(messages) {
    const box = el('div', '', 'notice');
    box.setAttribute('role', 'status');
    for (const text of messages) box.append(el('p', text));
    return box;
  }
  function problems() {
    const messages = [
      ...(data.notices || []).map((notice) => notice.message),
      ...(data.history_coverage || [])
        .filter((source) => source.status === 'unavailable')
        .map((source) => (sourceNames[source.source]?.[0] || source.source) + ': ' + source.coverage),
    ];
    if (messages.length) main.append(unreadable(messages));
  }
  function empty(title, text) {
    const section = el('section', '', 'journal-card');
    section.append(el('h2', title), el('p', text));
    return section;
  }
  function playLink(key) {
    const node = link('Play', 'frame', { view: key }, 'primary-link');
    node.prepend(icon('play'));
    return node;
  }
  const management = data.management;
  const operationNames = {
    refresh: 'Refresh observations',
    retry: 'Queue plates for retry',
    'retry-now': 'Retry as soon as the current cycle allows',
    'notification-test': 'Test notification delivery',
    'notification-retry': 'Retry notification dead letters',
    'import-preview': 'Check history file',
    'import-commit': 'Confirm history update',
    'trip-preview': 'Review trip selection',
    'trip-save': 'Save trip',
    'queue-plates': 'Queue missing plates',
    'approve-candidate': 'Approve candidate locally',
    'reject-candidate': 'Reject candidate',
    'community-preview': 'Check community history',
    'community-save': 'Save community collection',
  };
  function operationAction(label, kind, explanation, taxonId) {
    const section = el('section', '', 'journal-card');
    let preview = null;
    const start = button(label, async () => {
      if (preview) {
        preview.querySelector('button')?.focus();
        return;
      }
      preview = el('div', '', 'notice');
      const problem = el('p', '', 'error');
      problem.setAttribute('role', 'alert');
      let revision, key;
      const targets = typeof taxonId === 'function' ? taxonId() : taxonId;
      clearProblem(section);
      try {
        if (Array.isArray(targets) && (!targets.length || targets.length > 32))
          throw new Error('Select between 1 and 32 birds.');
        const settings = await api('/v1/owner/settings');
        revision = settings.revision;
        if (!revision) throw new Error('This service has no authoritative configuration file.');
        key = randomHex(24);
      } catch (error) {
        preview = null;
        inlineProblem(ui, section, error);
        return;
      }
      preview.append(
        el('h2', operationNames[kind]),
        note(explanation),
        ...(Array.isArray(targets) ? [note('Selected: ' + targets.map((id) => byId.get(id)?.common_name || 'Taxon ' + id).join(', '))] : []),
        button('Confirm action', async (event) => {
          const control = event.currentTarget;
          control.disabled = true;
          try {
            const body = { kind, key, revision, confirm: true };
            if (Array.isArray(targets)) body.taxon_ids = targets;
            else if (targets) body.taxon_id = targets;
            const receipt = await api('/v1/owner/operations', body);
            location.assign(href('operation', { operation: receipt.operation_id }));
          } catch (error) {
            if (error.code === 'sign_in_required') ui.showProblem(error);
            else {
              problem.textContent =
                error.message + ' Check recent operations if the connection was lost.';
              control.disabled = false;
            }
          }
        }),
        button('Cancel', () => {
          preview.remove();
          preview = null;
          start.focus();
        }),
        problem,
      );
      section.append(preview);
      preview.querySelector('button').focus();
    });
    section.append(start);
    return section;
  }
  function savedAction(label, action, taxonId, plateReady) {
    return savedChange(context, label, action, taxonId, plateReady);
  }
  // Each area module receives the page's shared state through this one object.
  const context = {
    ui,
    el,
    button,
    api,
    main,
    query,
    area,
    view,
    data,
    byId,
    management,
    sourceNames,
    plateURL,
    href,
    link,
    note,
    art,
    evidence,
    card,
    members,
    viewLabel,
    base,
    unreadable,
    problems,
    empty,
    playLink,
    operationNames,
    operationAction,
    savedAction,
    closeJournal,
    currentRun: () => run,
    setStopScreen(stop) {
      stopScreen = stop;
    },
    setFrameSignOut(signOut) {
      frameSignOut = signOut;
    },
  };
  if (area === 'home') renderHome(context);
  else if (area === 'birds') renderBirds(context);
  else if (area === 'bird') await renderBird(context);
  else if (area === 'collections') renderCollections(context);
  else if (area === 'community') await renderCommunity(context);
  else if (area === 'edit-collection') renderEditCollection(context);
  else if (area === 'import-collection') renderImportCollection(context);
  else if (area === 'collection') renderCollection(context);
  else if (area === 'frame') renderFrame(context);
  else if (area === 'candidate') await renderCandidate(context);
  else if (area === 'operation') await renderOperation(context);
  else if (area === 'imports') {
    stopScreen = await renderImports(ui, {
      base,
      link,
      note,
      kind: query.get('kind') || 'ebird-archive',
      journalRevision: data.revision,
    });
  } else if (area === 'settings') {
    const source = query.get('source');
    if (source && !sourceNames[source]) throw new Error('Choose a supported source.');
    await renderSettings(ui, { base, link, note, sourceNames, source });
  } else if (['displays', 'integrations'].includes(area)) {
    await renderPresentation(ui, { base, link, note, data, area });
  } else if (['health', 'backups', 'advanced'].includes(area)) await renderGuides(context);
  else if (area === 'manage') renderManage(context);
  else if (['sources', 'work', 'notifications'].includes(area)) await renderStatus(context);
  else base('Page not found', 'Choose a place in your journal.');
}
