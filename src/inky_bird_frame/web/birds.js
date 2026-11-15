export function renderBirds(context) {
  const { el, main, query, data, sourceNames, link, note, card, base, problems, empty } = context;
  const artScope = query.get('scope') === 'art';
  base(
    artScope ? 'Available artwork' : 'Your birds',
    artScope
      ? 'Approved local plates. Browsing does not add a sighting or change your wall.'
      : 'Birds in retained history, current observations or saved collections. A missing plate does not erase a bird.',
  );
  const scopes = el('div', '', 'segment');
  scopes.setAttribute('role', 'navigation');
  scopes.setAttribute('aria-label', 'Bird scope');
  for (const [label, active, extra] of [
    ['Your birds', !artScope, {}],
    ['Available artwork', artScope, { scope: 'art' }],
  ]) {
    const option = link(label, 'birds', extra, '');
    if (active) option.setAttribute('aria-current', 'page');
    scopes.append(option);
  }
  main.append(scopes);
  problems();
  const label = el('label', 'Find a bird');
  label.htmlFor = 'bird-search';
  const search = el('input');
  search.type = 'search';
  search.id = label.htmlFor;
  search.autocomplete = 'off';
  const count = el('p', '', 'hint');
  count.setAttribute('role', 'status');
  const grid = el('div', '', 'bird-grid');
  main.append(label, search, count, grid);
  // Cards are built once; searching only hides them, so images are never refetched.
  const cards = data.birds
    .filter((bird) => (artScope ? bird.plate_ready : bird.known))
    .map((bird) => {
      const node = card(bird);
      grid.append(node);
      return [(bird.common_name + ' ' + bird.scientific_name).toLocaleLowerCase(), node];
    });
  let none = null;
  function filter() {
    const term = search.value.trim().toLocaleLowerCase();
    let shown = 0;
    for (const [name, node] of cards) {
      node.hidden = !name.includes(term);
      if (!node.hidden) shown += 1;
    }
    count.textContent = shown + ' species';
    none?.remove();
    none = shown
      ? null
      : empty(
          'No birds here yet',
          term ? 'Try another name.' : 'Connect a source or choose artwork for a collection.',
        );
    if (none) grid.append(none);
  }
  search.addEventListener('input', filter);
  filter();
  if (!artScope) {
    const history = el('details');
    history.append(el('summary', 'History coverage and unmatched species'));
    for (const source of data.history_coverage || [])
      history.append(
        note((sourceNames[source.source]?.[0] || source.source) + ': ' + source.coverage),
      );
    for (const member of data.unresolved_history || [])
      history.append(
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
    history.append(
      note(
        'Unmatched names stay private. They cannot request artwork until an exact taxon match exists. Viewing history does not contact a source or change generation eligibility. Live sources without retained local history describe only their last discovery window.',
      ),
    );
    main.append(history);
  }
}

export async function renderBird(context) {
  const {
    ui, el, button, api, main, query, data, byId, sourceNames, link, note, art, evidence, base,
    empty, savedAction,
  } = context;
  const bird = byId.get(Number(query.get('bird')));
  if (!bird) {
    base('Bird unavailable');
    main.append(link('Back to birds', 'birds'));
    return;
  }
  base(bird.common_name, bird.scientific_name);
  main.querySelector('.intro')?.classList.add('scientific');
  main.append(note(evidence(bird)));
  for (const item of bird.retained_evidence || [])
    main.append(
      note(
        (sourceNames[item.source]?.[0] || item.source) +
          ': ' +
          item.count +
          ' ' +
          item.unit +
          ' in retained history. Counts from different sources are not added together.',
      ),
    );
  if (bird.plate_ready) {
    const image = art(bird, 'detail-art');
    main.append(image);
    const result = await api('/v1/owner/journal/birds/' + bird.taxon_id + '/profile');
    if (!image.isConnected) return;
    if (result.profile) {
      const profile = result.profile;
      main.append(el('h2', 'Field notes'), note(profile.family));
      const facts = el('dl', '', 'measurements');
      for (const [key, value] of Object.entries(profile.measurements))
        facts.append(el('dt', key[0].toUpperCase() + key.slice(1)), el('dd', value));
      const marks = el('ul');
      for (const text of profile.field_marks) marks.append(el('li', text));
      main.append(
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
      main.append(sources);
    } else main.append(note(result.notice));
  } else
    main.append(
      empty(
        'Plate not ready',
        'This bird stays in your journal. Open Plate work to inspect its current state.',
      ),
    );
  const groups = data.collections.filter((group) => group.taxon_ids.includes(bird.taxon_id));
  main.append(el('h2', 'In your collections'));
  for (const group of groups)
    main.append(link(group.label, 'collection', { view: group.collection_id }));
  const favorite = groups.some((group) => group.kind === 'favorites');
  main.append(
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
  );
  main.append(
    link(
      'Make a collection with this bird',
      'edit-collection',
      { bird: bird.taxon_id },
      'primary-link',
    ),
    link('Back to birds', 'birds'),
  );
  if (bird.saved || bird.plate_ready)
    main.append(
      savedAction(
        bird.saved ? 'Review removal from Saved birds' : 'Review adding to Saved birds',
        bird.saved ? 'remove' : 'add',
        bird.taxon_id,
      ),
    );
}
