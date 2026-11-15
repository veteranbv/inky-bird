import { randomHex } from '/app/keys.js';
import { plural, waitForOperation } from '/app/manage.js';

export function renderCollections(context) {
  const { el, main, data, link, note, art, members, viewLabel, base, playLink } = context;
  base(
    'Collections',
    'Keep a selection together. Saving a group does not change your wall or request new art.',
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
        ready.length + (ready.length === 1 ? ' plate ready · ' : ' plates ready · ') +
        (birds.length - ready.length) + ' not ready',
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
      'Current and saved keeps the existing controller selection. Named collections do not join it automatically. Available artwork is not personal evidence.',
    ),
  );
}

export async function renderCommunity(context) {
  const { ui, el, button, api, main, href, link, note, base } = context;
  base(
    'Birds reported near a place',
    'Search iNaturalist community reports. These are not your personal sightings. The query sends this point, radius and dates to iNaturalist.',
  );
  const preview = await api('/v1/owner/community');
  const key = () => randomHex(24);
  if (preview.status === 'ready') {
    const group = preview.collection;
    main.append(
      el('h2', group.label),
      note(group.start_date + ' through ' + group.end_date + ' · inclusive dates'),
      note(group.members.length + ' species · complete search result'),
      note(preview.notice),
    );
    const list = el('ul');
    for (const member of group.members)
      list.append(el('li', member.common_name + ' · ' + member.scientific_name));
    main.append(list);
    const requestKey = key();
    main.append(
      button('Save community collection', async () => {
        try {
          const receipt = await api('/v1/owner/community/save', {
            key: requestKey,
            preview_hash: preview.preview_hash,
            confirm: true,
          });
          location.assign(href('operation', { operation: receipt.operation_id }));
        } catch (error) {
          ui.showProblem(error);
        }
      }),
    );
  }
  main.append(
    button(
      preview.status === 'ready' ? 'Discard preview and search again' : 'New community search',
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
            'Searches must contain no more than 500 species. Narrow a larger result; Inky never saves a truncated selection.',
          ),
          note(
            'A successful new search replaces only the temporary preview, not saved collections.',
          ),
        );
        const submit = el('button', 'Check community reports');
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
    'Choose species for a private saved selection. This does not claim that you saw them.',
  );
  if (group?.kind === 'personal-trip')
    main.append(
      note(
        'Existing checklist links stay with retained members. Any bird you add here is a manual selection, not a new personal sighting. Unresolved source names stay until you remove or resolve them below.',
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
      document.createTextNode(bird.common_name + (bird.plate_ready ? '' : ' · plate not ready')),
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
      ? shown + (shown === 1 ? ' species shown' : ' species shown') + ' · chosen birds stay chosen'
      : '';
  }
  finder.addEventListener('input', filterRows);
  // Unresolved names stay unless the owner unticks them. Choosing the bird
  // with the same scientific name and unticking its name resolves it.
  const unresolved = group?.unresolved || [];
  const kept = new Set(unresolved);
  const pending = el('fieldset');
  if (unresolved.length) {
    pending.append(
      el('legend', 'Unresolved species'),
      note('Untick a name to remove it. To resolve one, choose its bird above, then untick its name.'),
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
        input.checked ? kept.add(member) : kept.delete(member),
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
      // Sent only when something was unticked, so plain edits keep their 1.0 shape.
      if (kept.size !== unresolved.length)
        body.unresolved = unresolved
          .filter((member) => kept.has(member))
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
    'Bring a saved selection to this controller. Imported labels are not proof that you saw a bird.',
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
        throw new Error('Choose a collection file no larger than 1 MiB.');
      const document = JSON.parse(await file.text());
      const preview = await api('/v1/owner/journal/collections/import-preview', { document });
      if (selection !== pending || !content.isConnected) return;
      content.append(
        el('h2', preview.label),
        note(
          preview.resolved +
            ' matched species · ' +
            preview.ready +
            ' approved plates · ' +
            preview.unresolved.length +
            ' unresolved',
        ),
        note(preview.notice),
      );
      if (preview.start_date)
        content.append(
          note(preview.start_date + ' through ' + preview.end_date + ' · inclusive dates'),
        );
      for (const member of preview.unresolved)
        content.append(note('Unresolved: ' + (member.scientific_name || member.common_name)));
      const collection_id = randomHex(16);
      const problem = el('p', '', 'error');
      problem.setAttribute('role', 'alert');
      content.append(
        button('Import this selection', async (event) => {
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
                ' You can retry this same save safely, or check Collections for its result.';
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
      ? 'Artwork, not a record of personal sightings.'
      : 'A saved view. Play does not change the physical wall.',
  );
  main.append(playLink(view));
  const tools = el('section', '', 'collection-tools');
  if (view === 'saved')
    tools.append(savedReview(context, 'Review adding all available approved art', 'import-approved'));
  const group = data.collections.find((item) => item.collection_id === view);
  if (group) {
    main.append(
      note(
        group.kind === 'personal-trip'
          ? 'Personal trip · linked eBird checklists'
          : group.kind === 'community-history'
            ? 'Community reports · not your personal sightings'
            : group.kind === 'imported-selection'
              ? 'Imported selection · personal evidence not verified here'
              : group.kind === 'favorites'
                ? 'Bookmarks · not sightings'
                : 'Saved selection · not a sighting record',
      ),
    );
    if (group.start_date)
      main.append(note(group.start_date + ' through ' + group.end_date + ' · inclusive dates'));
    for (const member of group.unresolved)
      main.append(
        note(
          'Unresolved: ' +
            (member.scientific_name || member.common_name) +
            ' · no generation requested',
        ),
      );
    if (group.unresolved.length && group.kind !== 'favorites')
      main.append(note('Edit this collection to remove or resolve unresolved species.'));
    for (const member of group.evidence || [])
      if (member.status !== 'linked')
        main.append(
          el(
            'p',
            (byId.get(member.taxon_id)?.common_name || member.scientific_name) +
              ': ' +
              (member.status === 'source-changed'
                ? 'Source record changed. Your curated selection stays, but its original personal evidence no longer matches.'
                : 'Source history is unavailable. Inky cannot confirm this member’s evidence.'),
            'notice',
          ),
        );
    tools.append(
      button('Export collection', async () => {
        if (
          !confirm(
            'This file includes this collection’s name, dates and species. It does not include credentials or private source identifiers. Check those details before sharing it.',
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
          ui.showProblem(error);
        }
      }),
    );
    const request = el('section', '', 'journal-card'),
      problem = el('p', '', 'error');
    problem.setAttribute('role', 'alert');
    request.append(
      button('Review missing-plate requests', async (event) => {
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
            el('h2', 'Queue missing plates?'),
            note(
              plan.new_requests.length +
                ' new requests · ' +
                plan.already_queued.length +
                ' already queued · ' +
                plan.held.length +
                ' held · ' +
                plan.ready.length +
                ' ready · ' +
                preview.unresolved +
                ' unresolved',
            ),
          );
          for (const id of plan.held) {
            const pending = management.work?.in_review.candidates.some(
              (candidate) => candidate.taxon_id === id && candidate.inspect_available,
            );
            summary.append(
              note(
                (byId.get(id)?.common_name || 'Taxon ' + id) +
                  ': existing review or recovery hold. This action will not clear it.',
              ),
              pending
                ? link('Inspect the pending candidate', 'candidate', { bird: id })
                : link('Inspect this bird’s recovery', 'work', { bird: id }),
            );
          }
          for (const item of plan.waiting)
            summary.append(
              note(
                (byId.get(item.taxon_id)?.common_name || 'Taxon ' + item.taxon_id) +
                  ': retry not before ' +
                  new Date(item.next_attempt_at).toLocaleString(),
              ),
            );
          summary.append(
            note(
              settings.generation_enabled
                ? 'Automatic generation is enabled. Eligible requests can use your AI allowance or incur costs at a scheduled cycle.'
                : 'Generation is off. Requests stay queued until you deliberately enable it.',
            ),
            note(
              'This does not start a model, add Saved birds, clear a hold, reset retry timing or change the wall. Unresolved identities cannot request a plate.',
            ),
          );
          if (plan.new_requests.length) {
            const key = randomHex(16);
            summary.append(
              button('Confirm these plate requests', async (event) => {
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
                      error.message + ' Check recent operations if the response was lost.';
                }
              }),
            );
          }
          summary.append(
            button('Close preview', () => {
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
            'Remove this collection? Source history, Saved birds and approved plates stay unchanged.',
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
          ui.showProblem(error);
        }
      }),
    );
  const grid = el('div', '', 'bird-grid');
  for (const bird of birds) {
    const node = card(bird),
      origin = group?.member_origins.find((member) => member.taxon_id === bird.taxon_id)?.origin;
    if (group?.kind === 'personal-trip') {
      const linked = group.evidence?.find((member) => member.taxon_id === bird.taxon_id)?.status;
      node.append(
        note(
          origin === 'ebird-checklist' && linked === 'linked'
            ? 'Your eBird checklist · linked within this trip’s dates'
            : origin === 'ebird-checklist'
              ? 'Saved trip member · personal evidence needs attention'
              : 'Manual selection · not a personal sighting record',
        ),
      );
    } else if (group?.kind === 'imported-selection')
      node.append(note('Imported selection · personal evidence not verified here'));
    else if (group?.kind === 'community-history')
      node.append(
        note(
          origin === 'community-report'
            ? 'Community report · not your personal sighting'
            : 'Manual selection · no community report linked',
        ),
      );
    grid.append(node);
  }
  if (!birds.length)
    grid.append(
      empty(
        'An empty collection is fine',
        'Add birds when you are ready. Nothing outside this view will play.',
      ),
    );
  main.append(grid);
  if (tools.childElementCount) main.append(el('h2', 'Collection tools'), tools);
}

function savedSummary(preview) {
  return (
    plural(preview.added_count, 'addition', 'additions') +
    ' · ' +
    plural(preview.removed_count, 'removal', 'removals') +
    ' · ' +
    plural(
      preview.migrated_legacy_queue_count,
      'legacy seed member migrated',
      'legacy seed members migrated',
    )
  );
}

// The confirm step for changes beyond one bird; its outcome opens on its own page.
function confirmSaved(context, section, problem, body, preview) {
  const { el, button, api, note, href } = context;
  const key = randomHex(24);
  const confirm = button('Confirm Saved birds change', async (event) => {
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
      problem.textContent = error.message + ' Check Saved birds if the connection was lost.';
      control.disabled = false;
    }
  });
  section.append(
    el('h2', 'Change Saved birds?'),
    note(savedSummary(preview)),
    note(
      'Saved birds join the compatible current selection and can affect the existing wall immediately at its next refresh. This is curation, not a sighting. Other named collections and source history stay unchanged. No model starts.',
    ),
    confirm,
    button('Cancel', () => location.reload()),
  );
  confirm.focus();
}

async function savedPreview(api, action, taxonId) {
  const settings = await api('/v1/owner/settings');
  const body = { action, configuration_revision: settings.revision };
  if (taxonId) body.taxon_id = taxonId;
  return [body, await api('/v1/owner/saved/preview', body)];
}

// Bulk Saved birds changes keep their review, confirmation and outcome page.
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
// other birds (an old queue migration) still asks first.
export function savedToggle(context, bird) {
  const { ui, el, button, api } = context;
  const section = el('section', '', 'journal-card'),
    status = el('p', '', 'hint'),
    problem = el('p', '', 'error');
  status.setAttribute('role', 'status');
  problem.setAttribute('role', 'alert');
  let saved = Boolean(bird.saved),
    undo = null;
  const control = button('', () => change(saved ? 'remove' : 'add', true));
  const label = () => {
    control.textContent = saved ? 'Remove from Saved birds' : 'Add to Saved birds';
  };
  label();
  async function change(action, offerUndo) {
    control.disabled = true;
    undo?.remove();
    undo = null;
    problem.textContent = '';
    status.textContent = 'Updating Saved birds…';
    try {
      const [body, preview] = await savedPreview(api, action, bird.taxon_id);
      const own = action === 'add' ? preview.added_count : preview.removed_count;
      const other = action === 'add' ? preview.removed_count : preview.added_count;
      if (own > 1 || other || preview.migrated_legacy_queue_count) {
        status.textContent = '';
        control.remove();
        confirmSaved(context, section, problem, body, preview);
        return;
      }
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
        throw new Error(receipt.result?.message || 'The operation did not finish.');
      saved = action === 'add';
      label();
      status.textContent = saved ? 'Added to Saved birds.' : 'Removed from Saved birds.';
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
      problem.textContent = error.message + ' Check Saved birds if the connection was lost.';
    } finally {
      control.disabled = false;
    }
    if (section.isConnected && !section.contains(document.activeElement)) control.focus();
  }
  section.append(control, status, problem);
  return section;
}

// The journal's Saved birds action: one bird toggles, everything else is reviewed.
export function savedChange(context, label, action, taxonId) {
  return taxonId
    ? savedToggle(context, { taxon_id: taxonId, saved: action === 'remove' })
    : savedReview(context, label, action, taxonId);
}
