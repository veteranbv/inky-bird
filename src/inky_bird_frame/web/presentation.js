// Owner-only controls. Screen cookies and status credentials are not owner logins.
import { inlineProblem, clearProblem } from '/app/ui.js';
import { confirmAction } from '/app/frame-tools.js';
import { when } from '/app/words.js';

export async function renderPresentation(ui, { base, link, note, data, area }) {
  const { api, element: el, button } = ui,
    main = document.querySelector('#main');
  base(
    area === 'integrations' ? 'Integrations' : 'Wall frame',
    area === 'integrations'
      ? 'Choose what your home dashboard may see.'
      : 'Choose what your wall frame and paired screens show.',
  );
  let state;
  try {
    state = await api('/v1/owner/presentation');
  } catch (error) {
    main.append(link('Back to Manage', 'manage'));
    inlineProblem(ui, main, error, { label: 'Try again', action: () => location.reload() });
    return;
  }
  const label = (key) =>
    key === 'current'
      ? 'On my frame'
      : key === 'saved'
        ? 'On my frame · added by you'
        : key === 'all'
          ? 'All plates'
          : key === 'favorites'
            ? 'Favorites'
            : data.collections.find((group) => group.collection_id === key)?.label ||
              'Removed collection';
  const views = [
    'current',
    'saved',
    'favorites',
    'all',
    ...data.collections
      .filter((group) => group.kind !== 'favorites')
      .map((group) => group.collection_id),
  ];
  let serial = 0,
    busy = false;
  // At most one open confirmation per section.
  const confirmations = new WeakMap();
  const refuse = (section, text) => inlineProblem(ui, section, new Error(text));
  function field(section, title, type = 'text', value = '') {
    const label = el('label', title),
      input = el('input');
    input.type = type;
    input.id = 'presentation-' + ++serial;
    label.htmlFor = input.id;
    input.value = value;
    if (type === 'checkbox') {
      label.textContent = '';
      label.className = 'check-label';
      label.append(input, document.createTextNode(title));
      section.append(label);
    } else section.append(label, input);
    return input;
  }
  function choice(section, selected = 'current') {
    const name = el('label', 'Birds to show'),
      input = el('select');
    input.id = 'view-' + ++serial;
    name.htmlFor = input.id;
    for (const key of views) {
      const option = el('option', label(key));
      option.value = key;
      option.selected = key === selected;
      input.append(option);
    }
    section.append(name, input);
    return input;
  }
  // Resolves true once the change was made, false when it was not.
  async function update(body, section, done = () => location.reload()) {
    if (busy) {
      inlineProblem(ui, section, {
        message: 'Another change is still being saved. Try again when it finishes.',
      });
      return false;
    }
    busy = true;
    try {
      const result = await api('/v1/owner/presentation', {
        revision: state.revision,
        confirm: true,
        ...body,
      });
      clearProblem(section);
      done(result);
      return true;
    } catch (error) {
      if (error.code === 'confirmation_required') {
        clearProblem(section);
        confirmations.get(section)?.remove();
        const notice = el('section', '', 'notice');
        confirmations.set(section, notice);
        notice.append(
          note(
            'Confirm it is you before granting or changing access. Your choices stay on this page.',
          ),
        );
        if (ui.localPasswordAllowed) {
          const password = field(notice, 'Your Inky password', 'password');
          password.autocomplete = 'current-password';
          notice.append(
            button('Confirm and retry', async () => {
              try {
                await api('/v1/owner/confirm-password', { passphrase: password.value });
                password.value = '';
                notice.remove();
                await update(body, section, done);
              } catch (failure) {
                password.value = '';
                inlineProblem(ui, notice, failure);
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
            note(
              'Sign-in returns to Account and access. Return here and repeat this change; unsaved entries will clear.',
            ),
          );
        notice.append(button('Cancel confirmation', () => notice.remove()));
        section.append(notice);
      } else inlineProblem(ui, section, error);
      return false;
    } finally {
      busy = false;
    }
  }
  // Revoking takes two presses: the first only asks.
  function revoke(section, id, label, question, keep) {
    return confirmAction({
      button,
      label,
      question,
      confirmLabel: label.startsWith('Remove') ? 'Remove' : 'Revoke',
      cancelLabel: keep,
      action: () => update({ action: 'revoke', id }, section),
    }).node;
  }
  main.append(link('Back to Manage', 'manage'));
  if (area === 'displays') {
    main.append(
      el('h2', 'Paired screens'),
      note(
        'On the tablet or computer you want to use, open /screen at your HTTPS address, then match its code here. A paired screen shows plates only and cannot sign in. On a plain home-network address, use Play instead.',
      ),
      el('a', 'Open the screen page'),
    );
    main.lastChild.href = '/screen';
    main.lastChild.className = 'text-link';
    for (const grant of state.grants.filter((grant) => grant.kind !== 'status')) {
      const section = el('section', '', 'journal-card');
      section.append(
        el('h2', grant.kind === 'pending' ? 'Screen waiting to pair · ' + grant.pairing_code : grant.name),
      );
      if (grant.kind === 'pending') {
        section.append(note('Check that the screen shows this code before you pair it.'));
        const name = field(section, 'Screen name', 'text', ''),
          view = choice(section),
          days = field(section, 'Days to keep showing birds', 'number', '90');
        name.maxLength = 128;
        days.min = '1';
        days.max = '365';
        const consent = field(
          section,
          'I matched this code on the screen I want to pair',
          'checkbox',
        );
        section.append(
          button('Pair this screen', () => {
            if (!consent.checked) {
              refuse(section, 'Match the code and confirm first.');
              return;
            }
            update(
              {
                action: 'pair',
                id: grant.id,
                name: name.value,
                view: view.value,
                days: Number(days.value),
              },
              section,
            );
          }),
        );
      } else
        section.append(
          note(label(grant.view)),
          ...(when(grant.expires_at * 1000)
            ? [note('Shows birds until ' + when(grant.expires_at * 1000))]
            : []),
          note(
            grant.last_seen_at
              ? 'Last seen ' + when(grant.last_seen_at * 1000)
              : 'Not connected yet',
          ),
        );
      section.append(
        grant.kind === 'pending'
          ? revoke(section, grant.id, 'Remove request', 'Remove this pairing request?', 'Keep request')
          : revoke(
              section,
              grant.id,
              'Revoke screen',
              'Revoke this screen? It stops showing birds at its next check.',
              'Keep access',
            ),
        note('A screen that is on clears at its next check; one that is off clears when it reconnects.'),
      );
      main.append(section);
    }
    const wall = el('section', '', 'journal-card');
    wall.append(
      el('h2', 'What your wall frame shows'),
      note('Now: ' + label(state.wall_view)),
      note('A change shows from its next refresh, if your wall frame uses the /wall address below.'),
      note('If the view has no plates, the wall frame keeps its last plate until it does.'),
    );
    const view = choice(wall, state.wall_view),
      consent = field(wall, 'Show this view on my wall frame', 'checkbox');
    wall.append(
      button('Show on my wall frame', () => {
        if (consent.checked) update({ action: 'wall', view: view.value }, wall);
        else refuse(wall, 'Tick the box first.');
      }),
      note(
        'One-time setup on the wall frame: set display_node.controller_url to ' +
          location.origin +
          '/wall, then restart its display service. This page cannot change that for you.',
      ),
      note(
        data.management.last_confirmed_panel_update
          ? 'Your wall frame last changed ' + when(data.management.last_confirmed_panel_update) + '.'
          : 'Your wall frame has not reported a change yet.',
      ),
    );
    main.append(wall);
    return;
  }
  const art = el('section', '', 'journal-card');
  art.append(
    el('h2', 'Plates on a dashboard'),
    note(state.dashboard_view ? 'On · ' + label(state.dashboard_view) : 'Off'),
    note(
      'A plain page of plates: no dates, sources or private names. Anyone who can reach it on your network can see which birds it shows.',
    ),
  );
  const view = choice(art, state.dashboard_view || 'current'),
    consent = field(
      art,
      'I understand anyone on my network or VPN can see which birds this shows',
      'checkbox',
    );
  art.append(
    button('Turn on dashboard plates', () => {
      if (consent.checked) update({ action: 'dashboard', view: view.value }, art);
      else refuse(art, 'Tick the box first.');
    }),
  );
  if (state.dashboard_view)
    art.append(button('Turn dashboard plates off', () => update({ action: 'dashboard-off' }, art)));
  art.append(
    note(
      state.embed_allowed_origins.length
        ? 'Dashboards that may show it: ' + state.embed_allowed_origins.join(', ')
        : 'Only this Inky address may show it. For another dashboard, set controller.embed_allowed_origins and restart Inky.',
    ),
    note(
      'An HTTPS dashboard needs an HTTPS Inky address, and a phone away from home needs your VPN. Never turn off certificate checks to make it work.',
    ),
  );
  const mixedContent =
    location.protocol === 'http:' &&
    state.embed_allowed_origins.some((origin) => origin.startsWith('https:'));
  if (mixedContent)
    art.append(
      el(
        'p',
        'Your dashboard uses HTTPS, but this Inky address uses HTTP, so the browser will block the plates. Open Inky at its HTTPS address before copying this. Status sensors still work.',
        'notice',
      ),
    );
  const recipe = el('pre');
  recipe.textContent =
    'Home Assistant webpage card:\ntype: iframe\nurl: ' +
    location.origin +
    '/v1/dashboard/art\naspect_ratio: 133%\n\nHomepage iframe widget:\nwidget:\n  type: iframe\n  src: ' +
    location.origin +
    '/v1/dashboard/art';
  if (!mixedContent) art.append(recipe);
  main.append(art);
  const status = el('section', '', 'journal-card');
  status.append(
    el('h2', 'Status sensors'),
    note(
      'A separate read-only credential shares only the version, how many plates you have and how many are showing in its view, how the last check went, and the wall frame’s health. It cannot see collections, history, keys or images. It expires, and you can revoke it here.',
    ),
  );
  const name = field(status, 'Credential name', 'text', 'Dashboard status'),
    permitted = choice(status),
    days = field(status, 'Credential expires after days', 'number', '90');
  days.min = '1';
  days.max = '365';
  name.maxLength = 128;
  // One press makes one credential: the button stays off once it is made.
  const create = button('Create status credential', async () => {
    create.disabled = true;
    const made = await update(
      { action: 'status', name: name.value, view: permitted.value, days: Number(days.value) },
      status,
      (result) => {
        create.disabled = true;
        state.revision += 1;
        const once = el('section', '', 'notice'),
          secret = el('pre');
        secret.textContent = result.secret;
        once.append(
          el('h2', 'Copy this credential now'),
          note(
            'It is shown only once. Keep it in your dashboard’s secret store. If you lose it, revoke it and make another.',
          ),
          secret,
        );
        const config = el('pre');
        config.textContent =
          'GET ' +
          location.origin +
          '/v1/integrations/status\nAuthorization: Bearer <saved status credential>\n\nHome Assistant:\nrest:\n  - resource: ' +
          location.origin +
          '/v1/integrations/status\n    headers:\n      Authorization: !secret inky_status_authorization\n    scan_interval: 60\n    sensor:\n      - name: Inky plates in rotation\n        value_template: "{{ value_json.data.view_ready_count }}"\n      - name: Inky approved plates\n        value_template: "{{ value_json.data.approved_count }}"\n\nHomepage customapi:\nwidget:\n  type: customapi\n  url: ' +
          location.origin +
          '/v1/integrations/status\n  headers:\n    Authorization: "Bearer {{HOMEPAGE_VAR_INKY_STATUS}}"\n  mappings:\n    - field: data.view_ready_count\n      label: In rotation\n      format: number\n    - field: data.approved_count\n      label: Approved\n      format: number';
        once.append(
          config,
          button('I saved it · clear this copy', () => location.reload()),
        );
        status.append(once);
      },
    );
    if (!made) create.disabled = false;
  });
  status.append(create);
  for (const grant of state.grants.filter((grant) => grant.kind === 'status'))
    status.append(
      el('h3', grant.name),
      note(label(grant.view) + (when(grant.expires_at * 1000) ? ' · until ' + when(grant.expires_at * 1000) : '')),
      revoke(
        status,
        grant.id,
        'Revoke ' + grant.name,
        'Revoke this status credential? Dashboards that use it stop updating.',
        'Keep credential',
      ),
    );
  main.append(status, link('Notifications', 'notifications'));
}
