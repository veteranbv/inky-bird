// Raw files live only in controller staging and the browser's file input.
// History confirmation and trip saving are separate, recoverable steps.
import { statusText } from '/app/manage.js';
import { plural, plainDate, when } from '/app/words.js';

export async function renderImports(ui, { base, link, note, kind }) {
  const { element: el, button, api, upload } = ui;
  if (!['ebird-archive', 'birdnet-analyzer'].includes(kind))
    throw new Error('Choose a supported history source.');
  base(
    kind === 'ebird-archive' ? 'Your eBird history' : 'Analyzer history',
    'Check the file first. You choose when history changes.',
  );
  const main = document.querySelector('#main'),
    content = el('section', '', 'journal-card');
  const steps = el(
    'p',
    kind === 'ebird-archive'
      ? 'File → Check → History → Checklists → Save'
      : 'File → Check → History',
    'hint',
  );
  main.append(steps, content, link('Back to sources', 'sources'));
  let stopped = false,
    timer,
    transfer = null;
  const randomId = () =>
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (item) =>
      item.toString(16).padStart(2, '0'),
    ).join('');
  const problem = () => {
    const node = el('p', '', 'error');
    node.setAttribute('role', 'alert');
    return node;
  };
  function field(form, title, type, value = '') {
    const label = el('label', title),
      input = el('input');
    input.id = 'import-' + type + '-' + form.children.length;
    input.type = type;
    input.value = value;
    label.htmlFor = input.id;
    form.append(label, input);
    return input;
  }
  function fail(node, error) {
    if (error.code === 'sign_in_required') ui.showProblem(error);
    else {
      node.textContent = error.message;
      // The step that owned this message may have been replaced meanwhile.
      if (!node.isConnected && content.isConnected) content.append(node);
    }
  }
  // A recovery button shows its own failure beside it instead of rejecting.
  const recoveryAlerts = new WeakMap();
  function recovery(label, action) {
    const alert = problem();
    const control = button(label, async () => {
      control.disabled = true;
      alert.textContent = '';
      try {
        await action();
      } catch (error) {
        if (stopped) return;
        control.disabled = false;
        recoveryFailed(control, error);
      }
    });
    recoveryAlerts.set(control, alert);
    return control;
  }
  function recoveryFailed(control, error) {
    const alert = recoveryAlerts.get(control);
    fail(alert, error);
    if (!alert.isConnected) {
      if (control.isConnected) control.after(alert);
      else content.append(alert);
    }
  }
  const unknownOutcome = ['offline', 'timeout', 'invalid_response'];
  function operation(label, action, uploadId, extra, onSuccess = load) {
    const section = el('div'),
      error = problem();
    let key = randomId(),
      request = null;
    const start = button(label, async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      error.textContent = '';
      let accepted = false;
      try {
        if (!request) {
          const settings = await api('/v1/owner/settings');
          request = {
            upload_id: uploadId,
            key,
            revision: settings.revision,
            confirm: true,
            ...(await extra()),
          };
        }
        const receipt = await api('/v1/owner/imports/' + action, request);
        accepted = true;
        if (!stopped) await watch(receipt.operation_id, onSuccess);
      } catch (failure) {
        // An accepted step, a possibly lost reply or a server failure (which may
        // follow the saved receipt) is sent again unchanged, so the controller can
        // recognise it. A refusal is rebuilt from current choices.
        if (!accepted && !unknownOutcome.includes(failure.code) && !(failure.status >= 500)) {
          request = null;
          key = randomId();
        }
        control.disabled = false;
        fail(error, failure);
      }
    });
    section.append(start, error);
    return section;
  }
  async function watch(id, done = load) {
    clearTimeout(timer);
    const receipt = await api('/v1/owner/operations/' + encodeURIComponent(id));
    if (stopped || !content.isConnected) return;
    content.replaceChildren(
      el('h2', 'Working on this step'),
      note(statusText(receipt.status)),
      link('See how it went', 'operation', { operation: id }),
    );
    if (['pending', 'running'].includes(receipt.status)) {
      timer = setTimeout(
        () =>
          watch(id, done).catch((error) => {
            if (!stopped) {
              const retry = recovery('Check again', () => watch(id, done));
              content.append(retry);
              recoveryFailed(retry, error);
            }
          }),
        document.hidden ? 5000 : 1000,
      );
      return;
    }
    if (receipt.status === 'succeeded') {
      if (receipt.result?.collection_id) {
        location.assign(
          '/app?page=collection&view=' + encodeURIComponent(receipt.result.collection_id),
        );
        return;
      }
      await done();
      return;
    }
    content.replaceChildren(
      el(
        'h2',
        receipt.status === 'interrupted' ? 'This step was interrupted' : 'This step did not finish',
      ),
      note(receipt.result?.message || 'Check what was saved before trying again.'),
      recovery('Check saved state', load),
      link('See how it went', 'operation', { operation: id }),
    );
  }
  async function load() {
    const state = (await api('/v1/owner/imports')).preview;
    if (stopped || !content.isConnected) return;
    content.replaceChildren();
    if (!state) {
      chooseFile();
      return;
    }
    if (state.kind !== kind) {
      content.append(
        el('h2', 'Another history preview is open'),
        link('Go back to it', 'imports', { kind: state.kind }),
        cancel(state),
      );
      return;
    }
    content.append(
      note('Inky keeps this file privately until ' + (when(state.expires_at) || 'the preview ends') + '.'),
    );
    // The controller removes an expired preview and refuses its steps, so the
    // browser clock never decides expiry.
    if (state.status === 'receiving') {
      content.append(
        el('h2', 'Upload did not finish'),
        note(
          'Your history and trips are unchanged. Cancel this file and choose it again.',
        ),
        cancel(state),
      );
      return;
    }
    if (state.status === 'uploaded') {
      content.append(
        el('h2', '2. Check the file'),
        note(
          'Inky reads the whole file and compares it with the history it already has. Nothing changes yet.',
        ),
        operation('Check this file', 'import-preview', state.upload_id, () => ({})),
        cancel(state),
      );
      return;
    }
    if (state.status === 'committed') {
      content.append(
        el('h2', 'Your history is updated'),
        note('A trip is saved only once you see it in Collections.'),
      );
      if (kind === 'birdnet-analyzer') {
        content.append(
          note(
            'This does not turn the source on, ask for plates or change your frame.',
          ),
          cancel(state, 'Finish'),
          link('Analyzer settings', 'settings', { source: kind }),
        );
        return;
      }
      if (state.trip) {
        reviewTrip(state);
        return;
      }
      chooseChecklists(state);
      return;
    }
    const preview = state.preview;
    content.append(el('h2', '3. Review history changes'));
    const labels = {
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
    };
    const counts = el('dl', '', 'measurements');
    for (const [key, label] of Object.entries(labels))
      if (key in preview.stats)
        counts.append(el('dt', label), el('dd', String(preview.stats[key])));
    content.append(
      counts,
      note(preview.notice),
      note(
        preview.impact.live_source_enabled
          ? 'This source is on, so these birds join your regular checks.'
          : 'This source is off. Importing will not turn it on.',
      ),
      note(
        preview.generation_enabled
          ? 'Plate making is on. New birds can be drawn after the next check, with your own image-generation account; its charges apply.'
          : 'Plate making is off. This will not turn it on.',
      ),
    );
    if (preview.impact.entering_window)
      content.append(
        note(
          plural(preview.impact.entering_window.length, 'bird name comes', 'bird names come') +
            ' into your current window, and ' +
            preview.impact.leaving_window.length +
            ' leave it.',
        ),
      );
    if (preview.impact.undated_notice) content.append(note(preview.impact.undated_notice));
    let acknowledge = null;
    if (
      preview.stats.removed_checklists ||
      preview.stats.removed ||
      preview.impact.affected_collections?.length
    ) {
      const label = el('label', '', 'check-label');
      acknowledge = el('input');
      acknowledge.type = 'checkbox';
      label.append(
        acknowledge,
        document.createTextNode(
          kind === 'birdnet-analyzer'
            ? 'I have checked the removed records. Remove them from my Analyzer history.'
            : 'I have checked the removed checklists. Keep my collections and mark the birds whose checklists changed.',
        ),
      );
      for (const name of preview.impact.affected_collections || [])
        content.append(note('Checklists change in: ' + name));
      content.append(label);
    }
    content.append(
      operation(
        preview.unchanged ? 'Nothing changed · continue' : 'Confirm history update',
        'import-commit',
        state.upload_id,
        () => {
          if (acknowledge && !acknowledge.checked)
            throw new Error('Tick the box to say you have checked what will be removed.');
          return {
            preview_hash: preview.preview_hash,
            allow_reduction: acknowledge?.checked || false,
          };
        },
      ),
      operation('Check this file again', 'import-preview', state.upload_id, () => ({})),
      cancel(state),
    );
  }
  function cancel(state, label = 'Cancel preview') {
    return button(label, async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      try {
        await api('/v1/owner/imports/cancel', { upload_id: state.upload_id });
        await load();
      } catch (error) {
        control.disabled = false;
        const alert = problem();
        fail(alert, error);
        content.append(alert);
      }
    });
  }
  function chooseFile() {
    if (kind === 'ebird-archive') {
      const error = problem(),
        here = el('div');
      here.hidden = true;
      content.append(here);
      // Offered only when there is eBird history to use.
      api('/v1/owner/journal')
        .then((journal) => {
          here.hidden = !(journal.history_coverage || []).some(
            (row) =>
              row.source === 'ebird-archive' && row.status === 'available' && row.species_count > 0,
          );
        })
        .catch(() => {});
      here.append(
        button('Use the history already here', async (event) => {
          const control = event.currentTarget;
          control.disabled = true;
          try {
            await api('/v1/owner/imports/retained', {});
            await load();
          } catch (failure) {
            control.disabled = false;
            fail(error, failure);
          }
        }),
        note(
          'Make a trip from the checklists Inky already has. Upload your archive again for place names or newer checklists.',
        ),
        error,
      );
    }
    content.append(
      el('h2', '1. Choose your file'),
      note(
        kind === 'ebird-archive'
          ? 'Use the ZIP or CSV from eBird’s Download My Data, as it comes. It replaces the history Inky has, so do not trim it to one trip.'
          : 'Use BirdNET Analyzer CSV output. A known recording date applies to this batch. Leave it blank if the date is unknown.',
      ),
    );
    const form = el('form'),
      file = field(form, 'History file', 'file');
    file.accept = kind === 'ebird-archive' ? '.zip,.csv' : '.csv';
    file.required = true;
    const observed =
      kind === 'birdnet-analyzer' ? field(form, 'Known recording date (optional)', 'date') : null;
    const error = problem(),
      send = el('button', 'Upload for checking');
    send.type = 'submit';
    const cancel = button('Stop upload', () => transfer?.abort());
    cancel.hidden = true;
    const progress = el('p', '', 'hint');
    progress.setAttribute('role', 'status');
    const recheck = recovery('Check saved upload', load);
    recheck.hidden = true;
    form.append(
      note(
        'Files up to 512 MB. A ZIP must also unpack to 512 MB or less. Inky needs 2 GB free to receive it, and keeps it privately for a limited time.',
      ),
      send,
      cancel,
      progress,
      error,
      recheck,
    );
    const MIB = 1024 * 1024;
    const unit = (total) => (total < MIB ? ' KB' : ' MB');
    const amount = (bytes, total) =>
      total < MIB ? String(Math.ceil(bytes / 1024)) : (bytes / MIB).toFixed(total < 10 * MIB ? 1 : 0);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const source = file.files[0];
      if (!source) return;
      if (!source.size || source.size > 512 * 1024 * 1024) {
        error.textContent = 'Choose a file up to 512 MB.';
        return;
      }
      send.disabled = true;
      file.disabled = true;
      if (observed) observed.disabled = true;
      cancel.hidden = false;
      transfer = new AbortController();
      error.textContent = '';
      recheck.hidden = true;
      recoveryAlerts.get(recheck).remove();
      progress.textContent =
        'Uploading ' +
        amount(source.size, source.size) +
        unit(source.size) +
        ' privately. No history has changed. Keep this page open.';
      let delivered = false;
      const sent = (loaded, total) => {
        if (stopped || !total) return;
        delivered = loaded >= total;
        progress.textContent =
          'Uploading privately: ' +
          Math.floor((100 * Math.min(loaded, total)) / total) +
          '% (' +
          amount(Math.min(loaded, total), total) +
          ' of ' +
          amount(total, total) +
          unit(total) +
          '). No history has changed. Keep this page open.';
      };
      try {
        const suffix = observed?.value ? '?observed_on=' + encodeURIComponent(observed.value) : '';
        await upload('/v1/owner/imports/upload/' + kind + suffix, source, transfer.signal, sent);
        if (!stopped) await load();
      } catch (failure) {
        if (!stopped) {
          // Once the whole file is sent, the controller may already hold it.
          if (failure.code === 'stopped')
            progress.textContent =
              failure.message + (delivered ? ' Check the saved upload before sending again.' : '');
          else {
            progress.textContent =
              'No history changed. If the response was lost, check the saved upload before sending again.';
            fail(error, failure);
          }
          recheck.hidden = false;
        }
      } finally {
        transfer = null;
        send.disabled = false;
        file.disabled = false;
        if (observed) observed.disabled = false;
        cancel.hidden = true;
      }
    });
    content.append(form);
  }
  function chooseChecklists(state) {
    content.append(
      el('h2', '4. Choose your checklists'),
      note(
        'Both dates are included. Place names come only from your file and are not kept in the saved trip.',
      ),
      note(
        'Checking a trip looks up species names Inky has not matched before on iNaturalist. Only the names are sent.',
      ),
    );
    const draft = state.draft || {};
    const form = el('form'),
      start = field(
        form,
        'Trip start date',
        'date',
        draft.start_date || state.preview.stats.earliest_observed_on,
      ),
      end = field(
        form,
        'Trip end date',
        'date',
        draft.end_date || state.preview.stats.latest_observed_on,
      ),
      name = field(form, 'Trip name', 'text', draft.label || '');
    start.required = end.required = name.required = true;
    name.maxLength = 128;
    const next = el('button', 'Find checklists');
    next.type = 'submit';
    const error = problem(),
      choices = el('div');
    form.append(next, error, choices);
    content.append(form, cancel(state, 'Finish without a trip'));
    let selection = 0;
    for (const input of [start, end, name])
      input.addEventListener('input', () => {
        selection++;
        choices.replaceChildren();
      });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const pending = ++selection;
      next.disabled = true;
      try {
        const result = await api('/v1/owner/imports/checklists', {
          upload_id: state.upload_id,
          start_date: start.value,
          end_date: end.value,
        });
        if (stopped || !form.isConnected || pending !== selection) return;
        const selected = new Set(),
          fields = el('fieldset');
        fields.append(el('legend', 'Checklists for this trip'));
        for (const record of result.checklists) {
          const label = el('label', '', 'check-label'),
            input = el('input');
          input.type = 'checkbox';
          if (draft.checklist_keys?.includes(record.key)) {
            input.checked = true;
            selected.add(record.key);
          }
          input.addEventListener('change', () =>
            input.checked ? selected.add(record.key) : selected.delete(record.key),
          );
          label.append(
            input,
            document.createTextNode(
              (plainDate(record.observed_on) || record.observed_on) +
                ' · ' +
                (record.location || 'No place name') +
                ' · ' +
                plural(record.species_count, 'species', 'species'),
            ),
          );
          fields.append(label);
        }
        const collection_id = draft.collection_id || randomId();
        choices.replaceChildren(
          fields,
          note(
            plural(result.checklists.length, 'checklist', 'checklists') +
              '. ' +
              (draft.checklist_keys
                ? 'Your earlier choices are back where they still exist. Check them before you go on.'
                : 'None are selected automatically.'),
          ),
          operation('Review this trip', 'trip-preview', state.upload_id, async () => {
            if (!selected.size) throw new Error('Choose at least one checklist.');
            const current = await api('/v1/owner/journal');
            return {
              choices: {
                history_revision: result.history_revision,
                checklist_keys: [...selected],
                start_date: result.start_date,
                end_date: result.end_date,
                label: name.value,
                collection_id,
                revision: current.revision,
              },
            };
          }),
        );
      } catch (failure) {
        fail(error, failure);
      } finally {
        next.disabled = false;
      }
    });
  }
  function reviewTrip(state) {
    const preview = state.trip.preview;
    content.append(
      el('h2', '5. Save your trip'),
      el('p', preview.label),
      note(
        plural(preview.matched, 'bird', 'birds') +
          ' · ' +
          plural(preview.ready, 'plate', 'plates') +
          (preview.unresolved.length ? ' · ' + preview.unresolved.length + ' not matched' : ''),
      ),
      note(preview.notice),
    );
    for (const member of preview.unresolved)
      content.append(
        note(
          'Not matched: ' +
            (member.scientific_name || member.common_name) +
            '. It will not be drawn.',
        ),
      );
    // The controller refuses to save while any name is still waiting.
    const waiting = preview.not_looked_up || [];
    for (const member of waiting)
      content.append(note('Not looked up yet: ' + (member.scientific_name || member.common_name)));
    if (!waiting.length && !preview.unresolved_for_now)
      content.append(
        operation('Save this trip', 'trip-save', state.upload_id, () => ({
          preview_hash: preview.preview_hash,
        })),
      );
    content.append(
      recovery('Edit trip selection', async () => {
        await api('/v1/owner/imports/trip-edit', { upload_id: state.upload_id });
        await load();
      }),
      cancel(state, 'Finish without saving this trip'),
      link('Collections', 'collections'),
    );
  }
  async function resume() {
    const recent = await api('/v1/owner/operations');
    const active = recent.operations.find(
      (receipt) =>
        ['import-preview', 'import-commit', 'trip-preview', 'trip-save'].includes(receipt.kind) &&
        ['pending', 'running'].includes(receipt.status),
    );
    if (active) await watch(active.operation_id);
    else await load();
  }
  try {
    await resume();
  } catch (error) {
    if (error.code === 'sign_in_required') throw error;
    const retry = recovery('Check saved state', resume);
    content.replaceChildren(retry);
    recoveryFailed(retry, error);
  }
  return () => {
    stopped = true;
    clearTimeout(timer);
    transfer?.abort();
  };
}
