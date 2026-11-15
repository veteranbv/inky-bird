import { icon } from '/app/ui.js';
import { sourceState, nextCheck, requestState } from '/app/work-status.js';

export async function renderOperation(context) {
  const {
    ui, el, button, api, main, query, byId, sourceNames, link, note, base, operationNames,
    setStopScreen,
  } = context;
  base(
    'Operation outcome',
    'Inky saves this result on the controller. Reloading this page does not repeat the action.',
  );
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
      content.replaceChildren(
        el('h2', operationNames[receipt.kind] || 'Owner operation'),
        el('p', receipt.status),
      );
      if (receipt.result) {
        if (receipt.result.message) content.append(el('p', receipt.result.message));
        for (const [key, value] of Object.entries(receipt.result)) {
          if (key === 'message') continue;
          if (key === 'taxon_ids') {
            for (const id of value) content.append(link(byId.get(id)?.common_name || 'Taxon ' + id, 'work', { bird: id }));
            continue;
          }
          if (key === 'providers') {
            for (const provider of value)
              content.append(
                el(
                  'p',
                  (sourceNames[provider.name]?.[0] || provider.name) +
                    ': ' +
                    provider.status +
                    ' · ' +
                    provider.species_count +
                    ' species',
                ),
              );
          } else if (['string', 'number', 'boolean'].includes(typeof value))
            content.append(note(key.replaceAll('_', ' ') + ': ' + value));
        }
      }
      if (['pending', 'running'].includes(receipt.status))
        timer = setTimeout(update, document.hidden ? 5000 : 1000);
      else {
        if (receipt.kind === 'community-preview')
          content.append(link('Review community selection', 'community', {}, 'primary-link'));
        if (receipt.result?.collection_id)
          content.append(
            link(
              'Open saved collection',
              'collection',
              { view: receipt.result.collection_id },
              'primary-link',
            ),
          );
        content.append(
          link('Check sources', 'sources'),
          link('Check plate work', 'work'),
          link('Check notifications', 'notifications'),
        );
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
      'Run the controller and display doctor from their installed hosts. Check actual scheduler/service health and successful panel telemetry. This page does not run privileged commands or download raw logs.',
    );
    main.append(
      link('Check Sources', 'sources'),
      link('Check plate work', 'work'),
      link('Check notification delivery', 'notifications'),
    );
  } else if (area === 'backups') {
    guide(
      'Backup and restore',
      'backup.md',
      'Stop browser serving and scheduled writers first. Back up the authoritative configuration, complete private state, approved catalog and working data together. Private state includes credentials. A collection export is not a controller backup.',
    );
    guide(
      'Upgrade and migration',
      'migration-v1.md',
      'Existing deployments keep their source choices, generation policy, Saved birds and default wall route. Make a matching runtime/state backup before updating. Do not let a newer runtime overwrite your only rollback copy.',
    );
    main.append(link('Configuration ownership', 'settings'));
  } else {
    guide(
      'Plate repair',
      'operations.md',
      'Approved replacements, research resets and correction-source selection require the existing explicit operator migration and human review. Browser approval cannot replace approved art or bypass required factual/visual checks.',
    );
    guide(
      'Installation and services',
      'installation.md',
      'Host binding, native services, executable paths, certificates and package updates remain installer-owned. Browser settings cannot safely pretend to reconfigure a host.',
    );
    guide(
      'HTTPS and Authentik',
      'browser-access.md',
      'Optional single sign-on uses an explicit owner identity link, not automatic promotion by email or group. Never copy the identity-service password into Inky.',
    );
  }
}

export function renderManage(context) {
  const { ui, el, button, main, link, note, base } = context;
  base('Manage', 'One controller. The same sources, reviewed plates and work rules.');
  const sections = [
    ['sources', 'Sources', 'Check all seven supported inputs and the shared observation window.'],
    ['work', 'Plate work', 'Keep stopped, waiting and runnable work distinct.'],
    ['displays', 'Displays', 'Check the last confirmed physical panel update.'],
    [
      'integrations',
      'Home Assistant and Homepage',
      'Choose scoped artwork and separate read-only status credentials.',
    ],
    ['notifications', 'Notifications', 'Check delivery separately from bird discovery.'],
    [
      'settings',
      'Settings and backups',
      'Source choices, generation policy and configuration ownership.',
    ],
  ];
  sections.push(
    ['health', 'Health and help', 'Read-only application checks and host doctor guidance.'],
    [
      'backups',
      'Backups and upgrades',
      'Protect private state and follow a deliberate migration.',
    ],
    [
      'advanced',
      'Advanced tools',
      'Explicit operator workflows for host setup and approved plate repair.',
    ],
  );
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
  const grid = el('div', '', 'manage-grid');
  for (const [key, title, text] of sections) {
    const node = link('', key, {}, 'journal-card');
    node.append(icon(glyphs[key]), el('h2', title), el('p', text));
    grid.append(node);
  }
  main.append(grid);
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

export async function renderStatus(context) {
  const {
    ui, el, api, main, query, area, data, byId, management, sourceNames, link, note, base,
    unreadable, empty, operationNames, operationAction, currentRun, setStopScreen,
  } = context;
  base(
    {
      sources: 'Sources',
      work: 'Plate work',
      notifications: 'Notifications',
    }[area],
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
    return note(label + ': ' + (value ? new Date(value).toLocaleString() : 'Not recorded'));
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
        notice.textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · refreshes automatically';
      } catch (error) {
        if (stopped) return;
        if (error.code === 'sign_in_required') { ui.showProblem(error); return; }
        show(latest, true);
        notice.textContent = 'Status updates unavailable. Current activity is unknown; saved history remains below.';
      }
      if (!stopped) timer = setTimeout(update, document.hidden ? 15000 : 5000);
    }
    const pendingRun = currentRun();
    show(latest, false);
    timer = setTimeout(update, 5000);
  }
  if (area === 'sources') {
    main.append(note('Selected observation window: ' + management.window + '. A source check is not continuous microphone or feeder monitoring. First recorded run means tracking since this version; older first-run history is unknown.'));
    main.append(operationAction('Refresh observations', 'refresh',
      'Read enabled sources now. Partial failures stay isolated. This action does not start a model.'));
    const schedule = el('section', '', 'journal-card');
    main.append(schedule);
    const cards = new Map();
    for (const source of management.sources) {
      const [name, kind] = sourceNames[source.name], node = el('section', '', 'journal-card');
      const details = el('div');
      node.append(el('h2', name), note(kind), details);
      if (['ebird-archive', 'birdnet-analyzer'].includes(source.name))
        node.append(note('Imported history · not a live connection.'), link('Import or review history', 'imports', { kind: source.name }));
      node.append(link('Set up ' + name, 'settings', { source: source.name }, 'primary-link'));
      cards.set(source.name, details);
      main.append(node);
    }
    const detections = el('section', '', 'journal-card');
    main.append(detections);
    pollManagement((current, unavailable) => {
      const refresh = current.activity?.entries?.['job-refresh'] || {};
      schedule.replaceChildren(el('h2', 'Source refresh schedule'),
        note('Configured interval: ' + current.refresh_minutes + ' minutes after a cycle finishes.'),
        note(unavailable ? 'Next check unknown while updates are unavailable.' : nextCheck(refresh)));
      for (const source of current.sources) {
        const node = cards.get(source.name), activity = source.activity || {};
        node.replaceChildren(el('p', unavailable && source.enabled ? 'Current activity unknown' : sourceState(source, current.work.complete), 'connection'),
          stamp('First recorded run', activity.first_started_at),
          stamp('Last started', activity.started_at),
          stamp('Last finished', activity.finished_at),
          stamp('Last successful check', activity.last_success_at),
          stamp('Last saved snapshot', source.last_checked_at),
          stamp('Latest dated detection or report in this window', source.latest_detection_at),
          note((activity.species_count ?? source.species_count ?? 'Unknown') + ' matched species · ' +
            (activity.unresolved_count ?? source.unresolved_count ?? 'Unknown') + ' unresolved'));
        if (source.enabled && (source.status === 'error' || activity.status === 'failed'))
          node.append(note('Check source configuration and retained operator evidence. Raw upstream responses are not exposed here.'));
      }
      detections.replaceChildren(el('h2', 'Recent detections & reports'),
        note('Latest dated record per species in the selected window; not an event stream or proof a device is recording. A merged timestamp does not identify which contributing source supplied it and can include imported history. Showing up to 100 species.'),
        stamp('Snapshot refreshed', current.discovery_refreshed_at));
      if (!current.work.complete) detections.append(note('Saved discovery is ' + current.work.discovery_status + '; these are historical snapshot results, not confirmed current detections.'));
      for (const bird of current.recent_detections || [])
        detections.append(link(bird.common_name, 'bird', { bird: bird.taxon_id }),
          stamp(bird.timestamp_source ? (sourceNames[bird.timestamp_source]?.[0] || bird.timestamp_source) : 'Latest merged record · provider unknown', bird.latest_detection_at),
          ...(!bird.timestamp_source ? [note('Contributing sources: ' + (bird.sources || []).map((name) => sourceNames[name]?.[0] || name).join(' · '))] : []));
      if (!current.recent_detections?.length) detections.append(note('No dated records available in this snapshot. That does not prove no birds were detected.'));
    });
  } else if (area === 'work') {
    main.append(link('Generation settings', 'settings'));
    const runtime = el('section', '', 'journal-card'), queue = el('section', '', 'journal-card');
    main.append(runtime, queue);
    const selected = new Set(), controls = el('section', '', 'journal-card'), choices = el('div'), inputs = new Map();
    const retryNow = operationAction('Retry selected as soon as possible', 'retry-now',
      'Ask the existing scheduler to refresh sources and run next. This waits behind the active cycle, can use your AI allowance, and does not bypass generation settings or sourced review.', () => [...selected]);
    const noWake = note('An immediate check requires generation to be enabled and the Inky scheduler to be running. Use the scheduled queue with external timers.');
    retryNow.hidden = true;
    controls.append(el('h2', 'Choose birds for retry'), note('Select up to 32. Requests are durable and deduplicated; generation stays serial. Approved plates are never regenerated by this action.'), choices,
      operationAction('Queue selected for the next scheduled check', 'retry',
        'Preserve correction guidance and mandatory sourced review. Preparation waits behind active work. Generation can use your AI allowance when enabled.', () => [...selected]), retryNow, noWake);
    main.append(controls);
    const holds = el('section', '', 'journal-card'), requests = el('section', '', 'journal-card');
    main.append(holds, requests);
    pollManagement((current, unavailable) => {
      const work = current.work, activity = current.activity?.entries || {}, generation = activity.generation || {};
      retryNow.hidden = unavailable || current.retry_wake_available !== true;
      noWake.hidden = !retryNow.hidden;
      runtime.replaceChildren(el('h2', 'Current activity'),
        note(current.generation_enabled ? 'Generation enabled · one cycle at a time' : 'Generation off · requests and history are retained'),
        note(unavailable ? 'Current activity unknown' : nextCheck(activity['job-generate'], current.generation_enabled)));
      if (!unavailable && generation.status === 'running')
        runtime.append(el('p', (generation.common_name || 'Generation cycle') + ' · ' + (generation.stage || 'working').replaceAll('_', ' ') +
          (generation.attempt ? ' · review round ' + generation.attempt : '')), stamp('Cycle started', generation.started_at));
      else runtime.append(note(generation.status === 'unknown' ? 'Last generation heartbeat is stale; running is not confirmed.' : 'No plate generation is currently confirmed active.'),
        stamp('Last cycle finished', generation.finished_at));
      if (!work.complete) runtime.append(note('Discovery is ' + work.discovery_status + '. A healthy refresh is required before generation.'));
      if (!unavailable) for (const [key, label] of [['job-refresh', 'Refreshing sources'], ['job-catalog-publish', 'Publishing approved catalog'], ['job-notifications', 'Dispatching notifications']])
        if (activity[key]?.status === 'running') runtime.append(note(label + ' · scheduler job active'));
      queue.replaceChildren(el('h2', 'Runnable queue · ' + (work.queue || []).length),
        note('Order at the last check. Source changes, active work, backoff and per-cycle limits can change the next start. No finish time is promised.'));
      const list = el('ol');
      for (const bird of work.queue || [])
        list.append(el('li', bird.common_name + (!unavailable && generation.status === 'running' && generation.taxon_id === bird.taxon_id ? ' · active cycle' : '')));
      queue.append(list);
      if (!work.queue?.length) queue.append(note('No runnable plates in this snapshot.'));
      const ids = new Set([...(work.can_run || []), ...(work.waiting || []), ...(work.needs_attention || [])]);
      const explicit = Number(query.get('bird'));
      if (explicit && byId.has(explicit) && !byId.get(explicit).plate_ready && !work.approved_taxon_ids?.includes(explicit)) ids.add(explicit);
      for (const [id, row] of inputs) if (!ids.has(id)) { row.remove(); inputs.delete(id); selected.delete(id); }
      for (const id of ids) if (!inputs.has(id)) {
        const detail = [...(work.retry_details || []), ...(work.terminal_details || []), ...(work.queue || [])].find((bird) => bird.taxon_id === id);
        const label = el('label', '', 'check-label'), input = el('input');
        input.type = 'checkbox';
        input.addEventListener('change', () => input.checked ? selected.add(id) : selected.delete(id));
        label.append(input, document.createTextNode(byId.get(id)?.common_name || detail?.common_name || 'Taxon ' + id));
        choices.append(label); inputs.set(id, label);
      }
      if (!ids.size && !choices.querySelector('.empty-selection')) choices.append(el('p', 'No birds currently need retry selection.', 'empty-selection'));
      if (ids.size) choices.querySelector('.empty-selection')?.remove();
      holds.replaceChildren(el('h2', 'Waiting or needs attention'));
      for (const detail of work.retry_details || [])
        holds.append(link(detail.common_name || 'Taxon ' + detail.taxon_id, 'bird', { bird: detail.taxon_id }),
          note(detail.failure_message), stamp('Last failed cycle', detail.last_failed_at),
          note(detail.attempts + ' recorded failed cycles'),
          note(work.needs_attention.includes(detail.taxon_id) ? 'Held until an explicit retry request; no automatic attempt scheduled.' : 'Automatic retry waits until eligibility, then a scheduler check.'),
          stamp('Retry eligibility', detail.next_attempt_at));
      for (const detail of work.terminal_details || [])
        holds.append(link(detail.common_name, 'bird', { bird: detail.taxon_id }), note(detail.failure_message),
          stamp('Last failure', detail.last_failed_at), stamp('Last attempt started', detail.last_attempt_at),
          note((detail.recorded_attempts ?? 'Unknown') + ' recorded attempts · scores must be at least 4/5, plus all other review checks'),
          ...Object.entries(detail.review_scores || {}).map(([axis, score]) => note(axis.replaceAll('_', ' ') + ': ' + score + '/5')),
          note('Held · ' + detail.state.replaceAll('_', ' ') + ' · no automatic retry scheduled.'));
      const review = work.in_review;
      holds.append(el('h3', 'Pending review · ' + review.candidates.length));
      if (review.status !== 'ok') holds.append(note('Pending inventory is incomplete; operator inspection is required.'));
      for (const candidate of review.candidates)
        holds.append(candidate.inspect_available ? link('Inspect ' + candidate.common_name, 'candidate', { bird: candidate.taxon_id }) : note(candidate.common_name + ': incomplete or unsafe pending state. Use operator recovery before retrying.'));
      requests.replaceChildren(el('h2', 'Recent retry requests'));
      for (const request of (work.retry_requests || []).slice(-32).reverse())
        requests.append(link(byId.get(request.taxon_id)?.common_name || 'Taxon ' + request.taxon_id, 'bird', { bird: request.taxon_id }),
          note(requestState(request, work)), stamp('Requested', request.created_at), stamp('Updated', request.updated_at));
      if (!work.retry_requests?.length) requests.append(note('No recorded retry requests.'));
    });
  } else {
    const state = management.notifications;
    main.append(
      el('p', state.enabled ? 'Notifications enabled' : 'Notifications off'),
      el('p', state.pending + ' pending · ' + state.dead_letters + ' dead letters'),
    );
    for (const destination of state.destinations)
      main.append(el('p', destination.name + ' · ' + destination.events.join(', ')));
    main.append(
      note(
        'Destination secrets stay on the controller. App-managed settings can change destinations; managed settings stay with the installer.',
      ),
    );
    main.append(link('Notification settings', 'settings'));
    if (state.enabled)
      main.append(
        operationAction(
          'Send a test notification',
          'notification-test',
          'Send one clearly labeled test to each configured destination. This is a real external notification. The result reports failures separately.',
        ),
      );
    if (state.enabled && state.dead_letters)
      main.append(
        operationAction(
          'Retry dead letters',
          'notification-retry',
          'Return failed messages to the existing outbox and request delivery. Destinations that already received a message are not sent it again.',
        ),
      );
  }
  const recent = await api('/v1/owner/operations');
  if (main.isConnected && recent.operations.length) {
    main.append(el('h2', 'Recent owner operations'));
    for (const receipt of recent.operations.slice().reverse())
      main.append(
        link(
          (operationNames[receipt.kind] || 'Operation') + ' · ' + receipt.status,
          'operation',
          { operation: receipt.operation_id },
        ),
      );
  }
}
