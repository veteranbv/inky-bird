import { randomHex } from '/app/keys.js';
import { waitForOperation } from '/app/manage.js';
import { WORDS, plural, plainDate, when } from '/app/words.js';
import { inlineProblem } from '/app/ui.js';

export function renderCollections(context) {
  const { el, main, data, link, note, art, members, viewLabel, base, playLink } = context;
  base(
    'Collections',
    'Groups of birds you keep together. Making one does not change your frame.',
  );
  const grid = el('div', '', 'bird-grid');
  for (const key of [
    'current',
    'saved',
    'favorites',
    ...data.collections
      .filter((group) => group.kind !== 'favorites')
      .map((group) => group.collection_id),
    'all',
  ]) {
    const birds = members(key);
    const ready = birds.filter((bird) => bird.plate_ready);
    const node = el('section', '', 'journal-card');
    if (ready[0]) {
      const cover = link('', 'collection', { view: key });
      cover.append(art(ready[0], '', 'thumbnail'));
      node.append(cover);
    }
    node.append(
      link(viewLabel(key), 'collection', { view: key }, 'collection-title'),
      note(
        plural(ready.length, 'plate', 'plates') +
          (birds.length > ready.length ? ' · ' + (birds.length - ready.length) + ' not drawn yet' : ''),
      ),
      playLink(key),
    );
    grid.append(node);
  }
  main.append(
    link('New collection', 'edit-collection', {}, 'primary-link'),
    link('Make a trip from your eBird checklists', 'imports', { kind: 'ebird-archive' }),
    link('Birds reported near a place', 'community'),
    link('Import a collection', 'import-collection'),
    grid,
    note(
      'A collection’s birds go on your frame only when you add them there.',
    ),
  );
}

export async function renderCommunity(context) {
  const { ui, el, button, api, main, href, link, note, base } = context;
  base(
    'Birds reported near a place',
    'Birds other people reported to iNaturalist. Searching sends the point, distance and dates you enter to iNaturalist.',
  );
  const preview = await api('/v1/owner/community');
  const key = () => randomHex(24);
  if (preview.status === 'ready') {
    const group = preview.collection;
    main.append(
      el('h2', group.label),
      note(plainDate(group.start_date) + ' to ' + plainDate(group.end_date)),
      note(plural(group.members.length, 'kind of bird', 'kinds of bird')),
      note(preview.notice),
    );
    const list = el('ul');
    for (const member of group.members)
      list.append(el('li', member.common_name + ' · ' + member.scientific_name));
    main.append(list);
    const requestKey = key();
    main.append(
      button('Save as a collection', async () => {
        try {
          const receipt = await api('/v1/owner/community/save', {
            key: requestKey,
            preview_hash: preview.preview_hash,
            confirm: true,
          });
          location.assign(href('operation', { operation: receipt.operation_id }));
        } catch (error) {
          inlineProblem(ui, main, error);
        }
      }),
    );
  }
  main.append(
    button(
      preview.status === 'ready' ? 'Search again' : 'Search for birds',
      () => {
        const form = el('form'),
          inputs = {};
        for (const [name, title, type] of [
          ['label', 'Collection name', 'text'],
          ['start_date', 'Start date', 'date'],
          ['end_date', 'End date', 'date'],
          ['latitude', 'Latitude', 'number'],
          ['longitude', 'Longitude', 'number'],
          ['radius_km', 'Radius in km', 'number'],
        ]) {
          const label = el('label', title),
            input = el('input');
          input.id = 'community-' + name;
          label.htmlFor = input.id;
          input.type = type;
          input.required = true;
          if (name === 'latitude' || name === 'longitude') {
            input.step = 'any';
            input.min = name === 'latitude' ? '-90' : '-180';
            input.max = name === 'latitude' ? '90' : '180';
          }
          if (name === 'radius_km') {
            input.min = '1';
            input.max = '500';
            input.step = '1';
          }
          if (name === 'label') input.maxLength = 128;
          inputs[name] = input;
          form.append(label, input);
        }
        form.append(
          note(
            'A search can hold up to 500 kinds of bird. Narrow the dates or distance if it finds more.',
          ),
          note('A new search replaces only this preview, not your collections.'),
        );
        const submit = el('button', 'Search iNaturalist');
        submit.type = 'submit';
        const problem = el('p', '', 'error');
        problem.setAttribute('role', 'alert');
        form.append(submit, problem);
        let requestKey = key();
        form.addEventListener('input', () => {
          requestKey = key();
        });
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          submit.disabled = true;
          try {
            const receipt = await api('/v1/owner/community/preview', {
              key: requestKey,
              choices: Object.fromEntries(
                Object.entries(inputs).map(([name, input]) => [name, input.value]),
              ),
              confirm: true,
            });
            location.assign(href('operation', { operation: receipt.operation_id }));
          } catch (error) {
            problem.textContent = error.message;
            submit.disabled = false;
          }
        });
        main.append(form);
        inputs.label.focus();
      },
    ),
    link('Back to Collections', 'collections'),
  );
}

export function renderEditCollection(context) {
  const { ui, el, api, main, query, data, href, link, note, base } = context;
  const group = data.collections.find((item) => item.collection_id === query.get('view'));
  if (group?.kind === 'favorites')
    throw new Error('Add or remove Favorites on each bird’s page.');
  const selected = new Set(group?.taxon_ids || [Number(query.get('bird'))].filter(Boolean));
  base(
    group ? 'Edit collection' : 'New collection',
    'Choose the birds for this collection.',
  );
  if (group?.kind === 'personal-trip')
    main.append(
      note(
        'Birds from your checklists keep their checklist links. Birds you add here are marked as added by you.',
      ),
    );
  const form = el('form');
  const label = el('label', 'Collection name');
  label.htmlFor = 'collection-name';
  const name = el('input');
  name.id = label.htmlFor;
  name.required = true;
  name.maxLength = 128;
  name.value = group?.label || '';
  const fields = el('fieldset');
  fields.append(el('legend', 'Choose birds'));
  // Filtering hides rows instead of rebuilding them, so chosen birds stay
  // chosen while the owner searches. Membership lives in `selected`.
  const findLabel = el('label', 'Find a bird to add');
  findLabel.htmlFor = 'collection-search';
  const finder = el('input');
  finder.type = 'search';
  finder.id = findLabel.htmlFor;
  finder.autocomplete = 'off';
  const matches = el('p', '', 'hint');
  matches.setAttribute('role', 'status');
  fields.append(findLabel, finder, matches);
  const rows = [];
  for (const bird of data.birds) {
    const row = el('label', '', 'check-label');
    const input = el('input');
    input.type = 'checkbox';
    input.checked = selected.has(bird.taxon_id);
    input.addEventListener('change', () =>
      input.checked ? selected.add(bird.taxon_id) : selected.delete(bird.taxon_id),
    );
    row.append(
      input,
      document.createTextNode(bird.common_name + (bird.plate_ready ? '' : ' · no plate yet')),
    );
    rows.push([bird, row]);
    fields.append(row);
  }
  function filterRows() {
    const term = finder.value.trim().toLocaleLowerCase();
    let shown = 0;
    for (const [bird, row] of rows) {
      const hidden =
        Boolean(term) &&
        !(bird.common_name + ' ' + bird.scientific_name).toLocaleLowerCase().includes(term);
      row.hidden = hidden;
      if (!hidden) shown += 1;
    }
    matches.textContent = term
      ? plural(shown, 'bird', 'birds') + ' shown · chosen birds stay chosen'
      : '';
  }
  finder.addEventListener('input', filterRows);
  // Enter in the search box would otherwise submit the form and save.
  finder.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') event.preventDefault();
  });
  // Unresolved names stay unless the owner unticks them. Choosing the bird
  // with the same scientific name and unticking its name resolves it.
  const unresolved = group?.unresolved || [];
  const removed = new Set();
  const pending = el('fieldset');
  if (unresolved.length) {
    pending.append(
      el('legend', 'Names Inky could not match'),
      note(
        'Untick a name to remove it. For a name marked "matches", also choose that bird above to keep its checklist links.',
      ),
    );
    for (const member of unresolved) {
      const row = el('label', '', 'check-label'),
        input = el('input'),
        match = data.birds.find(
          (bird) =>
            member.scientific_name &&
            bird.scientific_name.toLocaleLowerCase() === member.scientific_name.toLocaleLowerCase(),
        );
      input.type = 'checkbox';
      input.checked = true;
      input.addEventListener('change', () =>
        input.checked ? removed.delete(member) : removed.add(member),
      );
      row.append(
        input,
        document.createTextNode(
          (member.scientific_name || member.common_name) +
            (match ? ' · matches ' + match.common_name : ''),
        ),
      );
      pending.append(row);
    }
  }
  const problem = el('p', '', 'error');
  problem.setAttribute('role', 'alert');
  const save = el('button', 'Save collection');
  save.type = 'submit';
  form.append(label, name, fields, ...(unresolved.length ? [pending] : []), save, problem);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      const body = { revision: data.revision, label: name.value, taxon_ids: [...selected] };
      if (group) body.collection_id = group.collection_id;
      // Only the unticked names, and only when there are some, so plain edits
      // keep their 1.0 shape and a large trip stays within the request limit.
      if (removed.size)
        body.remove_unresolved = unresolved
          .filter((member) => removed.has(member))
          .map(({ common_name, scientific_name }) => ({ common_name, scientific_name }));
      await api('/v1/owner/journal/collections/save', body);
      if (form.isConnected) location.assign(href('collections'));
    } catch (error) {
      if (error.code === 'sign_in_required') ui.showProblem(error);
      else problem.textContent = error.message;
    } finally {
      save.disabled = false;
    }
  });
  main.append(form, link('Cancel', 'collections'));
}

export function renderImportCollection(context) {
  const { ui, el, button, api, main, data, href, link, note, base } = context;
  base(
    'Import a collection',
    'Add a collection from an Inky collection file.',
  );
  const label = el('label', 'Inky collection file');
  label.htmlFor = 'collection-file';
  const input = el('input');
  input.id = label.htmlFor;
  input.type = 'file';
  input.accept = '.json,application/json';
  const content = el('section', '', 'journal-card');
  main.append(label, input, content, link('Cancel', 'collections'));
  let selection = 0;
  input.addEventListener('change', async () => {
    const pending = ++selection;
    content.replaceChildren();
    try {
      const file = input.files[0];
      if (!file) return;
      if (file.size > 1024 * 1024)
        throw new Error('Choose a collection file of 1 MB or less.');
      const document = JSON.parse(await file.text());
      const preview = await api('/v1/owner/journal/collections/import-preview', { document });
      if (selection !== pending || !content.isConnected) return;
      content.append(
        el('h2', preview.label),
        note(
          plural(preview.resolved, 'bird', 'birds') +
            ' · ' +
            plural(preview.ready, 'plate', 'plates') +
            (preview.unresolved.length
              ? ' · ' + preview.unresolved.length + ' not matched'
              : ''),
        ),
        note(preview.notice),
      );
      if (preview.start_date)
        content.append(
          note(plainDate(preview.start_date) + ' to ' + plainDate(preview.end_date)),
        );
      for (const member of preview.unresolved)
        content.append(note('Not matched: ' + (member.scientific_name || member.common_name)));
      const collection_id = randomHex(16);
      const problem = el('p', '', 'error');
      problem.setAttribute('role', 'alert');
      content.append(
        button('Add this collection', async (event) => {
          const control = event.currentTarget;
          control.disabled = true;
          try {
            await api('/v1/owner/journal/collections/import', {
              document,
              revision: data.revision,
              preview_hash: preview.preview_hash,
              collection_id,
            });
            location.assign(href('collection', { view: collection_id }));
          } catch (error) {
            control.disabled = false;
            if (error.code === 'sign_in_required') ui.showProblem(error);
            else
              problem.textContent =
                error.message +
                ' You can try again safely, or check Collections to see whether it was added.';
          }
        }),
        problem,
        link('Check Collections', 'collections'),
      );
    } catch (error) {
      if (selection === pending)
        content.replaceChildren(
          el(
            'p',
            error instanceof SyntaxError ? 'This file is not valid JSON.' : error.message,
            'error',
          ),
        );
    }
  });
}

export function renderCollection(context) {
  const {
    ui, el, button, api, main, view, data, byId, management, href, link, note, card, members,
    viewLabel, base, empty, playLink,
  } = context;
  const birds = members(view);
  base(
    viewLabel(view),
    view === 'all'
      ? 'Every plate you could add.'
      : 'Play shows these birds here. Your wall frame keeps its own birds.',
  );
  main.append(playLink(view));
  const tools = el('section', '', 'collection-tools');
  if (view === 'saved')
    tools.append(savedReview(context, 'Add every bird with a plate', 'import-approved'));
  const group = data.collections.find((item) => item.collection_id === view);
  if (group) {
    main.append(
      note(
        group.kind === 'personal-trip'
          ? 'A trip from your eBird checklists'
          : group.kind === 'community-history'
            ? 'Birds other people reported nearby'
            : group.kind === 'imported-selection'
              ? 'Added from a collection file'
              : group.kind === 'favorites'
                ? 'Birds you’ve marked.'
                : 'A collection you made',
      ),
    );
    if (group.start_date)
      main.append(note(plainDate(group.start_date) + ' to ' + plainDate(group.end_date)));
    for (const member of group.unresolved)
      main.append(
        note('Not matched: ' + (member.scientific_name || member.common_name)),
      );
    if (group.unresolved.length && group.kind !== 'favorites')
      main.append(note('Edit this collection to remove names Inky could not match.'));
    for (const member of group.evidence || [])
      if (member.status !== 'linked')
        main.append(
          el(
            'p',
            (byId.get(member.taxon_id)?.common_name || member.scientific_name) +
              ': ' +
              (member.status === 'source-changed'
                ? 'its checklist changed after you made this collection. It stays in the collection.'
                : 'its checklist history cannot be read right now.'),
            'notice',
          ),
        );
    tools.append(
      button('Export collection', async () => {
        if (
          !confirm(
            'The file holds this collection’s name, dates and birds, and where each bird came from. Share it only if you are happy to share those.',
          )
        )
          return;
        try {
          const portable = await api(
            '/v1/owner/journal/collections/export?collection=' +
              encodeURIComponent(group.collection_id),
          );
          const url = URL.createObjectURL(
            new Blob([JSON.stringify(portable, null, 2) + '\n'], { type: 'application/json' }),
          );
          const anchor = el('a');
          anchor.href = url;
          anchor.download = 'inky-collection.json';
          anchor.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) {
          inlineProblem(ui, tools, error);
        }
      }),
    );
    const request = el('section', '', 'journal-card'),
      problem = el('p', '', 'error');
    problem.setAttribute('role', 'alert');
    request.append(
      button('Ask for the missing plates', async (event) => {
        const control = event.currentTarget;
        control.disabled = true;
        try {
          const settings = await api('/v1/owner/settings');
          const choices = {
            collection_id: group.collection_id,
            revision: data.revision,
            configuration_revision: settings.revision,
          };
          const preview = await api('/v1/owner/journal/collections/queue-preview', choices),
            plan = preview.plan;
          const summary = el('div', '', 'notice');
          summary.append(
            el('h2', 'Draw the missing plates?'),
            note(
              [
                plural(plan.new_requests.length, 'bird', 'birds') + ' to draw',
                plan.already_queued.length + ' already waiting',
                plan.held.length + ' left as they are',
                plural(plan.ready.length, 'bird', 'birds') + ' with a plate',
                ...(preview.unresolved ? [preview.unresolved + ' not matched'] : []),
              ].join(' · '),
            ),
          );
          for (const id of plan.held) {
            const pending = management.work?.in_review.candidates.some(
              (candidate) => candidate.taxon_id === id && candidate.inspect_available,
            );
            summary.append(
              note(
                (byId.get(id)?.common_name || 'Bird ' + id) +
                  ': has a plate waiting for you or needs a hand. This leaves it as it is.',
              ),
              pending
                ? link('Look at the new plate', 'candidate', { bird: id })
                : link('See what it needs', 'work', { bird: id }),
            );
          }
          for (const item of plan.waiting)
            summary.append(
              note(
                (byId.get(item.taxon_id)?.common_name || 'Bird ' + item.taxon_id) +
                  ': tries again ' +
                  (when(item.next_attempt_at) || 'after a short wait') +
                  '.',
              ),
            );
          summary.append(
            note(
              settings.generation_enabled
                ? 'Plates are drawn with your own image-generation account, and its charges apply.'
                : 'Plate making is off. These requests wait until you turn it on.',
            ),
            note('Your frame stays as it is. Names Inky could not match cannot be drawn.'),
          );
          if (plan.new_requests.length) {
            const key = randomHex(16);
            summary.append(
              button('Ask for these plates', async (event) => {
                const confirm = event.currentTarget;
                confirm.disabled = true;
                try {
                  const receipt = await api('/v1/owner/journal/collections/queue', {
                    ...choices,
                    preview_hash: preview.preview_hash,
                    key,
                    confirm: true,
                  });
                  location.assign(href('operation', { operation: receipt.operation_id }));
                } catch (error) {
                  confirm.disabled = false;
                  if (error.code === 'sign_in_required') ui.showProblem(error);
                  else
                    problem.textContent =
                      error.message +
                      ' If the connection dropped, check In the works before asking again.';
                }
              }),
            );
          }
          summary.append(
            button('Not now', () => {
              summary.remove();
              control.disabled = false;
              control.focus();
            }),
          );
          request.append(summary);
          summary.querySelector('button').focus();
        } catch (error) {
          control.disabled = false;
          if (error.code === 'sign_in_required') ui.showProblem(error);
          else problem.textContent = error.message;
        }
      }),
      problem,
    );
    if (group.unresolved.length || birds.some((bird) => !bird.plate_ready)) main.append(request);
  }
  if (group && group.kind !== 'favorites')
    tools.append(
      link('Edit collection', 'edit-collection', { view }),
      button('Remove collection', async () => {
        if (
          !confirm(
            'Remove this collection? Your journal, plates and frame stay as they are.',
          )
        )
          return;
        try {
          await api('/v1/owner/journal/collections/remove', {
            revision: data.revision,
            collection_id: view,
          });
          location.assign(href('collections'));
        } catch (error) {
          inlineProblem(ui, tools, error);
        }
      }),
    );
  const grid = el('div', '', 'bird-grid bird-list');
  for (const bird of birds) {
    const node = card(bird),
      origin = group?.member_origins.find((member) => member.taxon_id === bird.taxon_id)?.origin;
    if (group?.kind === 'personal-trip') {
      const linked = group.evidence?.find((member) => member.taxon_id === bird.taxon_id)?.status;
      node.append(
        note(
          origin === 'ebird-checklist' && linked === 'linked'
            ? 'On your checklists during this trip'
            : origin === 'ebird-checklist'
              ? 'Its checklist link needs a look'
              : 'Added by you',
        ),
      );
    } else if (group?.kind === 'imported-selection')
      node.append(note('From a collection file'));
    else if (group?.kind === 'community-history')
      node.append(
        note(
          origin === 'community-report'
            ? 'Reported by others'
            : 'Added by you',
        ),
      );
    grid.append(node);
  }
  if (!birds.length)
    grid.append(
      empty(
        'No birds here yet',
        'Add birds when you are ready.',
      ),
    );
  main.append(grid);
  if (tools.childElementCount) main.append(el('h2', 'Collection tools'), tools);
}

// "Adding 1 bird." Only the parts that change are named.
const changesNothing = (counts) =>
  !counts.added_count && !counts.removed_count && !counts.migrated_legacy_queue_count;

function savedSummary(preview) {
  const parts = [];
  if (preview.added_count) parts.push('Adding ' + plural(preview.added_count, 'bird', 'birds') + '.');
  if (preview.removed_count)
    parts.push('Removing ' + plural(preview.removed_count, 'bird', 'birds') + '.');
  if (preview.migrated_legacy_queue_count)
    parts.push(
      'Keeping ' +
        plural(preview.migrated_legacy_queue_count, 'bird', 'birds') +
        ' from your earlier setup.',
    );
  return parts.join(' ') || 'Nothing changes.';
}

// The server's own refusals are complete; only a change lost after it was sent
// leaves the outcome open.
function savedProblem(error, sent = true) {
  if (sent && error.code === 'interrupted')
    return 'Inky could not confirm whether your frame changed. Check On my frame before trying again.';
  return (
    error.message +
    (sent && ['offline', 'invalid_response'].includes(error.code)
      ? ' Check On my frame to see whether it changed.'
      : '')
  );
}

// The page cannot see which address the wall frame uses, so it speaks about
// the wall frame only while the wall view is one that follows On my frame on
// either address.
const wallFollows = (wall) => ['current', 'saved'].includes(wall);

// The default wall view also keeps birds that are still being noted, so a
// removed bird leaves it only when the controller's list no longer has it.
// Null when that list cannot be read.
async function stillShown(api, taxonId) {
  try {
    const journal = await api('/v1/owner/journal');
    return Array.isArray(journal?.active_taxon_ids)
      ? journal.active_taxon_ids.includes(taxonId)
      : null;
  } catch {
    return null;
  }
}

// What the inline result can say for certain after one bird was added or removed.
function toggleResult(name, saved, wall, stays) {
  const done = saved ? 'Added to On my frame.' : 'Removed from On my frame.';
  if (!wallFollows(wall)) return done;
  // The default view already shows a bird that is being noted.
  if (saved) return wall === 'saved' ? name + ' will show on your frame from the next refresh.' : done;
  if (stays === false) return name + ' will leave your frame at the next refresh.';
  if (wall === 'current' && stays === true)
    return done + ' ' + name + ' stays on your wall frame while it is still being noted.';
  return done;
}

// The confirm step for changes beyond one bird; its outcome opens on its own page.
function confirmSaved(context, section, problem, body, preview, reason = '') {
  const { el, button, api, note, href } = context;
  if (changesNothing(preview)) {
    const close = button('Close', () => location.reload());
    section.append(el('h2', 'Nothing to change'), note(savedSummary(preview)), close);
    close.focus();
    return;
  }
  const key = randomHex(24);
  const confirm = button('Change my frame', async (event) => {
    const control = event.currentTarget;
    control.disabled = true;
    try {
      const receipt = await api('/v1/owner/saved/change', {
        ...body,
        key,
        confirm: true,
        collection_revision: preview.collection_revision,
      });
      location.assign(href('operation', { operation: receipt.operation_id }));
    } catch (error) {
      problem.textContent = savedProblem(error);
      control.disabled = false;
    }
  });
  section.append(
    el('h2', 'Change what is on your frame?'),
    note(savedSummary(preview)),
    ...(reason ? [note(reason)] : []),
    ...(wallFollows(preview.wall)
      ? [note('Your wall frame uses the new list from its next refresh.')]
      : []),
    confirm,
    button('Cancel', () => location.reload()),
  );
  confirm.focus();
}

async function savedPreview(api, action, taxonId) {
  const [settings, wall] = await Promise.all([
    api('/v1/owner/settings'),
    api('/v1/owner/presentation').then(
      (state) => state?.wall_view,
      () => null,
    ),
  ]);
  const body = { action, configuration_revision: settings.revision };
  if (taxonId) body.taxon_id = taxonId;
  return [body, { ...(await api('/v1/owner/saved/preview', body)), wall }];
}

// Changes to many birds keep their review, confirmation and outcome page.
export function savedReview(context, label, action, taxonId) {
  const { el, button, api } = context;
  const section = el('section', '', 'journal-card'),
    problem = el('p', '', 'error');
  problem.setAttribute('role', 'alert');
  const start = button(label, async () => {
    start.disabled = true;
    try {
      const [body, preview] = await savedPreview(api, action, taxonId);
      confirmSaved(context, section, problem, body, preview);
    } catch (error) {
      problem.textContent = error.message;
      start.disabled = false;
    }
  });
  section.append(start, problem);
  return section;
}

// One bird: a toggle with an inline result and Undo. A change that would touch
// other birds (an old queue migration) still asks first, and so does removing a
// bird without approved art: adding it back would be refused, so Undo could not
// restore it.
export function savedToggle(context, bird) {
  const { ui, el, button, api } = context;
  const name = bird.common_name || context.byId?.get(bird.taxon_id)?.common_name || 'This bird';
  const section = el('section', '', 'journal-card'),
    status = el('p', '', 'hint'),
    problem = el('p', '', 'error');
  status.setAttribute('role', 'status');
  problem.setAttribute('role', 'alert');
  // The controller adds, and previews adding, only birds with approved art.
  let saved = Boolean(bird.saved),
    plateReady = Boolean(bird.plate_ready),
    undo = null,
    sent = false;
  const control = button('', () => change(saved ? 'remove' : 'add', true));
  const label = () => {
    control.textContent = saved ? 'Remove from my frame' : 'Add to my frame';
  };
  label();
  async function change(action, offerUndo) {
    control.disabled = true;
    undo?.remove();
    undo = null;
    problem.textContent = '';
    sent = false;
    status.textContent = 'Updating your frame…';
    try {
      const [body, preview] = await savedPreview(api, action, bird.taxon_id);
      const own = action === 'add' ? preview.added_count : preview.removed_count;
      const other = action === 'add' ? preview.removed_count : preview.added_count;
      if (!own && !other && !preview.migrated_legacy_queue_count) {
        // Another tab or device already made this change: show it, send nothing.
        saved = action === 'add';
        if (saved) plateReady = true;
        label();
        status.textContent = saved ? 'Already on your frame.' : 'Not on your frame.';
        return;
      }
      if (
        own > 1 ||
        other ||
        preview.migrated_legacy_queue_count ||
        (action === 'remove' && !plateReady)
      ) {
        status.textContent = '';
        control.remove();
        confirmSaved(
          context,
          section,
          problem,
          body,
          preview,
          action === 'remove' && !plateReady
            ? 'This bird has no plate yet, so you can add it back only once it has one.'
            : '',
        );
        return;
      }
      sent = true;
      const receipt = await waitForOperation(
        api,
        await api('/v1/owner/saved/change', {
          ...body,
          key: randomHex(24),
          confirm: true,
          collection_revision: preview.collection_revision,
        }),
        () => section.isConnected,
      );
      if (!receipt) return;
      if (receipt.status !== 'succeeded')
        throw Object.assign(
          new Error(receipt.result?.message || 'The operation did not finish.'),
          { code: receipt.status },
        );
      saved = action === 'add';
      if (saved) plateReady = true;
      label();
      const stays =
        !saved && ['current', 'saved'].includes(preview.wall)
          ? await stillShown(api, bird.taxon_id)
          : null;
      status.textContent = toggleResult(name, saved, preview.wall, stays);
      if (offerUndo) {
        undo = button('Undo', () => change(saved ? 'remove' : 'add', false));
        status.after(undo);
      }
    } catch (error) {
      status.textContent = '';
      if (error.code === 'sign_in_required') {
        ui.showProblem(error);
        return;
      }
      problem.textContent = savedProblem(error, sent);
    } finally {
      control.disabled = false;
    }
    if (section.isConnected && !section.contains(document.activeElement)) control.focus();
  }
  section.append(control, status, problem);
  return section;
}

// The journal's On my frame action: one bird toggles, everything else is reviewed.
export function savedChange(context, label, action, taxonId, plateReady = false) {
  return taxonId
    ? savedToggle(context, {
        taxon_id: taxonId,
        saved: action === 'remove',
        plate_ready: Boolean(plateReady),
      })
    : savedReview(context, label, action, taxonId);
}
