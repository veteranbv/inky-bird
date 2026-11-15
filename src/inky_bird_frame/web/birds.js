// The list's search, sort and filter live in the address, so Back, the bird
// page and its previous and next links all see the same list.
function lastNoted(bird) {
  return (bird.retained_evidence || [])
    .map((item) => item.last_noted)
    .filter((noted) => typeof noted === 'string')
    .reduce((latest, noted) => (noted > latest ? noted : latest), '');
}
const byName = (a, b) => a.common_name.localeCompare(b.common_name) || a.taxon_id - b.taxon_id;
const sorts = {
  name: ['Name', byName],
  scientific: [
    'Scientific name',
    (a, b) => (a.scientific_name || '').localeCompare(b.scientific_name || '') || byName(a, b),
  ],
  recent: ['Last noted', (a, b) => lastNoted(b).localeCompare(lastNoted(a)) || byName(a, b)],
};
const shows = {
  all: ['All', () => true],
  favorites: ['Favorites', (bird, sets) => sets.favorites.has(bird.taxon_id)],
  current: ['Current and saved', (bird, sets) => sets.current.has(bird.taxon_id)],
  record: ['In your history', (bird) => (bird.retained_evidence || []).length > 0],
  missing: ['No plate yet', (bird) => !bird.plate_ready],
};
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
function listState(query) {
  const sort = query.get('sort'),
    show = query.get('show');
  return {
    scope: query.get('scope') === 'art' ? 'art' : '',
    q: query.get('q') || '',
    sort: own(sorts, sort) ? sort : 'name',
    show: own(shows, show) ? show : 'all',
  };
}
function listParams(state) {
  const params = {};
  if (state.scope) params.scope = state.scope;
  if (state.q) params.q = state.q;
  if (state.sort !== 'name') params.sort = state.sort;
  if (state.show !== 'all') params.show = state.show;
  return params;
}
function memberSets(data) {
  return {
    favorites: new Set(
      data.collections.find((group) => group.kind === 'favorites')?.taxon_ids || [],
    ),
    current: new Set(data.active_taxon_ids),
  };
}
const searchText = (bird) => (bird.common_name + ' ' + bird.scientific_name).toLocaleLowerCase();
const inScope = (bird, state) => (state.scope === 'art' ? bird.plate_ready : bird.known);
function shown(bird, state, sets) {
  return (
    shows[state.show][1](bird, sets) &&
    searchText(bird).includes(state.q.trim().toLocaleLowerCase())
  );
}
// Text fields keep their keys; modified keys stay with the browser.
function typing(event) {
  const target = event.target;
  return (
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    target?.isContentEditable ||
    ['INPUT', 'SELECT', 'TEXTAREA'].includes(target?.tagName)
  );
}
function savedState() {
  return history.state && typeof history.state === 'object' ? history.state : {};
}
// Browsers may refuse frequent address updates; the list itself still works.
function replaceAddress(state, url) {
  try {
    history.replaceState(state, '', url);
  } catch {}
}

export function renderBirds(context) {
  const {
    el, main, query, data, sourceNames, href, link, note, card, base, problems, empty,
    setStopScreen,
  } = context;
  const state = listState(query);
  const artScope = state.scope === 'art';
  base(
    artScope ? 'Available artwork' : 'Your birds',
    artScope
      ? 'Approved local plates. Browsing does not add a sighting or change your wall.'
      : 'Birds in retained history, current observations or saved collections. A missing plate does not erase a bird.',
  );
  const scopes = el('div', '', 'segment');
  scopes.setAttribute('role', 'navigation');
  scopes.setAttribute('aria-label', 'Bird scope');
  const scopeLinks = [];
  for (const [label, active, scope] of [
    ['Your birds', !artScope, ''],
    ['Available artwork', artScope, 'art'],
  ]) {
    const option = link(label, 'birds', {}, '');
    if (active) option.setAttribute('aria-current', 'page');
    scopes.append(option);
    scopeLinks.push([option, scope]);
  }
  main.append(scopes);
  problems();
  const tools = el('div', '', 'list-tools');
  function field(text, id, control) {
    const wrap = el('div', '', 'list-field'),
      label = el('label', text);
    label.htmlFor = control.id = id;
    wrap.append(label, control);
    tools.append(wrap);
    return control;
  }
  const search = el('input');
  search.type = 'search';
  search.autocomplete = 'off';
  search.value = state.q;
  field('Find a bird', 'bird-search', search);
  function choice(options, value) {
    const select = el('select');
    for (const [key, [label]] of Object.entries(options)) {
      const option = el('option', label);
      option.value = key;
      select.append(option);
    }
    select.value = value;
    return select;
  }
  const sort = field('Sort by', 'bird-sort', choice(sorts, state.sort));
  const show = field('Show', 'bird-show', choice(shows, state.show));
  const count = el('p', '', 'hint');
  count.id = 'bird-count';
  count.setAttribute('role', 'status');
  const grid = el('div', '', 'bird-grid');
  main.append(tools, count, grid);
  const sets = memberSets(data);
  // Cards are built once; searching only hides them and sorting only moves
  // them, so images are never refetched.
  const cards = data.birds
    .filter((bird) => inScope(bird, state))
    .map((bird) => {
      const node = card(bird);
      // Back to this list brings the opened card into view again.
      node.addEventListener('click', () =>
        replaceAddress({ ...savedState(), birdsAt: bird.taxon_id }, href('birds', listParams(state))),
      );
      return { bird, node };
    });
  let sorted = null;
  let none = null;
  function update() {
    if (sorted !== state.sort) {
      sorted = state.sort;
      cards.sort((a, b) => sorts[state.sort][1](a.bird, b.bird));
      for (const { node } of cards) grid.append(node);
    }
    let visible = 0;
    for (const { bird, node } of cards) {
      node.hidden = !shown(bird, state, sets);
      if (!node.hidden) visible += 1;
      // Card links carry the list too, so the bird page can step through it.
      node.href = href('bird', { bird: bird.taxon_id, ...listParams(state) });
    }
    for (const [option, scope] of scopeLinks)
      option.href = href('birds', listParams({ ...state, scope }));
    count.textContent = visible + ' species';
    none?.remove();
    none = visible
      ? null
      : empty(
          'No birds here yet',
          state.q.trim()
            ? 'Try another name.'
            : state.show !== 'all'
              ? 'Try another filter.'
              : 'Connect a source or choose artwork for a collection.',
        );
    if (none) grid.append(none);
  }
  function changed() {
    state.q = search.value;
    state.sort = sort.value;
    state.show = show.value;
    update();
    replaceAddress(savedState(), href('birds', listParams(state)));
  }
  search.addEventListener('input', changed);
  sort.addEventListener('change', changed);
  show.addEventListener('change', changed);
  update();
  // The list draws after the page loads, so the browser cannot restore it.
  history.scrollRestoration = 'manual';
  const { birdsAt, ...rest } = savedState();
  const target = cards.find(
    ({ bird }) => bird.taxon_id === Number(birdsAt ?? query.get('at')),
  )?.node;
  if (birdsAt !== undefined || query.has('at'))
    replaceAddress(rest, href('birds', listParams(state)));
  if (target && !target.hidden) {
    target.scrollIntoView({ block: 'center' });
    target.focus({ preventScroll: true });
  }
  const onKey = (event) => {
    if (event.key !== '/' || typing(event)) return;
    event.preventDefault();
    search.focus();
  };
  document.addEventListener('keydown', onKey);
  setStopScreen(() => document.removeEventListener('keydown', onKey));
  if (!artScope) {
    const coverage = el('details');
    coverage.append(el('summary', 'History coverage and unmatched species'));
    for (const source of data.history_coverage || [])
      coverage.append(
        note((sourceNames[source.source]?.[0] || source.source) + ': ' + source.coverage),
      );
    for (const member of data.unresolved_history || [])
      coverage.append(
        note(
          'Unmatched: ' +
            (member.scientific_name || member.common_name) +
            ' · ' +
            (sourceNames[member.source]?.[0] || member.source) +
            ' · ' +
            member.count +
            ' ' +
            member.unit,
        ),
      );
    coverage.append(
      note(
        'Unmatched names stay private. They cannot request artwork until an exact taxon match exists. Viewing history does not contact a source or change generation eligibility. Live sources without retained local history describe only their last discovery window.',
      ),
    );
    main.append(coverage);
  }
}

export async function renderBird(context) {
  const {
    ui, el, button, api, main, query, data, byId, sourceNames, plateURL, link, note, art,
    evidence, base, empty, savedAction, setStopScreen,
  } = context;
  const state = listState(query);
  const bird = byId.get(Number(query.get('bird')));
  const back = link('Back to birds', 'birds', {
    ...listParams(state),
    ...(bird ? { at: bird.taxon_id } : {}),
  });
  if (!bird) {
    base('Bird unavailable');
    main.append(back);
    return;
  }
  base(bird.common_name, bird.scientific_name);
  main.querySelector('.intro')?.classList.add('scientific');
  const nav = el('nav', '', 'bird-nav');
  nav.setAttribute('aria-label', 'Bird list');
  nav.append(back);
  const sets = memberSets(data);
  const list = data.birds
    .filter((other) => inScope(other, state) && shown(other, state, sets))
    .sort(sorts[state.sort][1]);
  const at = list.indexOf(bird);
  const steps = {};
  if (at >= 0)
    for (const [label, other, key] of [
      ['Previous', list[at - 1], 'ArrowLeft'],
      ['Next', list[at + 1], 'ArrowRight'],
    ])
      if (other) {
        const step = link(label, 'bird', { bird: other.taxon_id, ...listParams(state) });
        step.setAttribute('aria-label', label + ': ' + other.common_name);
        steps[key] = step.href;
        nav.append(step);
      }
  main.prepend(nav);
  const onKey = (event) => {
    if (!steps[event.key] || typing(event) || event.shiftKey) return;
    event.preventDefault();
    location.assign(steps[event.key]);
  };
  document.addEventListener('keydown', onKey);
  setStopScreen(() => document.removeEventListener('keydown', onKey));
  const layout = el('div', '', 'bird-page'),
    plate = el('div', '', 'bird-plate'),
    notes = el('div', '', 'bird-notes');
  layout.append(plate, notes);
  main.append(layout);
  notes.append(note(evidence(bird)));
  for (const item of bird.retained_evidence || [])
    notes.append(
      note(
        (sourceNames[item.source]?.[0] || item.source) +
          ': ' +
          item.count +
          ' ' +
          item.unit +
          ' in retained history. Counts from different sources are not added together.',
      ),
    );
  const fieldNotes = el('div', '', 'field-notes');
  if (bird.plate_ready) {
    // The full plate opens on its own, where the browser can zoom it.
    const zoom = el('a', '', 'plate-link');
    zoom.href = plateURL(bird);
    zoom.append(art(bird, 'detail-art'));
    plate.append(zoom);
    notes.append(fieldNotes);
  } else
    plate.append(
      empty(
        'Plate not ready',
        'This bird stays in your journal. Open Plate work to inspect its current state.',
      ),
    );
  const groups = data.collections.filter((group) => group.taxon_ids.includes(bird.taxon_id));
  notes.append(el('h2', 'In your collections'));
  for (const group of groups)
    notes.append(link(group.label, 'collection', { view: group.collection_id }));
  const favorite = groups.some((group) => group.kind === 'favorites');
  notes.append(
    button(favorite ? 'Remove from Favorites' : 'Add to Favorites', async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      try {
        await api('/v1/owner/journal/collections/favorite', {
          revision: data.revision,
          taxon_id: bird.taxon_id,
          favorite: !favorite,
        });
        location.reload();
      } catch (error) {
        control.disabled = false;
        ui.showProblem(error);
      }
    }),
    note(
      'Favorites are bookmarks. They do not record sightings, request art or change your wall.',
    ),
    link(
      'Make a collection with this bird',
      'edit-collection',
      { bird: bird.taxon_id },
      'primary-link',
    ),
  );
  if (bird.saved || bird.plate_ready)
    notes.append(
      savedAction(
        bird.saved ? 'Review removal from Saved birds' : 'Review adding to Saved birds',
        bird.saved ? 'remove' : 'add',
        bird.taxon_id,
      ),
    );
  if (!bird.plate_ready) return;
  // Field notes load last: if they fail, the rest of the page still works.
  let result;
  try {
    result = await api('/v1/owner/journal/birds/' + bird.taxon_id + '/profile');
  } catch (error) {
    if (!fieldNotes.isConnected) return;
    if (error.code === 'sign_in_required') throw error;
    fieldNotes.append(note(error.message));
    return;
  }
  if (!fieldNotes.isConnected) return;
  if (!result.profile) {
    fieldNotes.append(note(result.notice));
    return;
  }
  const profile = result.profile;
  fieldNotes.append(el('h2', 'Field notes'), note(profile.family));
  const facts = el('dl', '', 'measurements');
  for (const [key, value] of Object.entries(profile.measurements))
    facts.append(el('dt', key[0].toUpperCase() + key.slice(1)), el('dd', value));
  const marks = el('ul');
  for (const text of profile.field_marks) marks.append(el('li', text));
  fieldNotes.append(
    facts,
    el('h2', 'Look for'),
    marks,
    el('h2', 'Habitat'),
    el('p', profile.habitat),
    el('h2', 'Behavior'),
    el('p', profile.behavior),
    el('h2', 'Sources'),
  );
  const sources = el('ul');
  for (const source of profile.sources) {
    const item = el('li'),
      anchor = el('a', source.title);
    anchor.href = source.url;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    item.append(anchor);
    sources.append(item);
  }
  fieldNotes.append(sources);
}
