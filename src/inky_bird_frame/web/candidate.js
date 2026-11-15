import { randomHex } from '/app/keys.js';

export async function renderCandidate(context) {
  const { ui, el, button, api, main, query, href, link, note, base } = context;
  base(
    'Review candidate',
    'Generated art is not approved artwork. Inspect the complete plate before deciding.',
  );
  main.append(link('Back to plate work', 'work'));
  const candidate = await api(
    '/v1/owner/candidates/' + encodeURIComponent(query.get('bird') || ''),
  );
  const content = el('section', '', 'journal-card');
  content.append(
    el('h2', candidate.common_name),
    note(candidate.scientific_name),
    note(candidate.notice),
  );
  const image = el('img', '', 'detail-art');
  image.src = candidate.portrait_url;
  image.alt = 'Unapproved candidate: ' + candidate.common_name;
  content.append(image, el('h2', 'Saved quality review'));
  const scores = el('dl', '', 'measurements');
  for (const [name, score] of Object.entries(candidate.quality_review))
    scores.append(el('dt', name.replaceAll('_', ' ')), el('dd', String(score)));
  content.append(scores);
  if (candidate.profile) {
    content.append(el('h2', 'Candidate field notes'), note(candidate.profile.family));
    for (const [name, value] of Object.entries(candidate.profile.measurements))
      content.append(el('p', name + ': ' + value));
    const marks = el('ul');
    for (const mark of candidate.profile.field_marks) marks.append(el('li', mark));
    content.append(
      marks,
      el('p', candidate.profile.habitat),
      el('p', candidate.profile.behavior),
    );
  }
  content.append(el('h2', 'Review sources'));
  for (const source of candidate.verification_sources) {
    const link = el('a', source.title, 'text-link');
    link.href = source.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    content.append(link);
  }
  let loaded = false;
  image.addEventListener('load', () => {
    loaded = true;
  });
  if (image.complete && image.naturalWidth) loaded = true;
  const problem = el('p', '', 'error');
  problem.setAttribute('role', 'alert');
  function decision(approve, reason = '') {
    let key = randomHex(16),
      request = null;
    return button(approve ? 'Confirm local approval' : 'Confirm rejection', async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      try {
        if (approve && !loaded)
          throw new Error('The full candidate image must load before you can approve it.');
        if (!request) {
          const settings = await api('/v1/owner/settings');
          request = {
            key,
            configuration_revision: settings.revision,
            candidate_revision: candidate.candidate_revision,
            confirm: true,
          };
          if (!approve) request.reason = reason();
        }
        const receipt = await api(
          '/v1/owner/candidates/' + candidate.taxon_id + '/' + (approve ? 'approve' : 'reject'),
          request,
        );
        location.assign(href('operation', { operation: receipt.operation_id }));
      } catch (error) {
        control.disabled = false;
        if (error.code === 'sign_in_required') ui.showProblem(error);
        else if (request && ['offline', 'invalid_response'].includes(error.code))
          // The decision may have been saved: send exactly the same one again.
          problem.textContent =
            error.message + ' Check recent operations if the response was lost.';
        else {
          // A refusal saved nothing, so the next try uses current settings and text.
          request = null;
          key = randomHex(16);
          problem.textContent = error.message;
        }
      }
    });
  }
  if (candidate.can_approve) {
    const label = el('label', '', 'check-label'),
      consent = el('input');
    consent.type = 'checkbox';
    label.append(
      consent,
      document.createTextNode(
        'I inspected the full plate, its text, anatomy and cited review. Make this artwork approved locally and eligible for existing publication.',
      ),
    );
    const approve = decision(true);
    approve.disabled = true;
    consent.addEventListener('change', () => {
      approve.disabled = !consent.checked;
    });
    content.append(
      label,
      approve,
      note(
        'Approval copies this reviewed candidate into the local catalog. Published copies cannot be recalled. Replacing approved art later needs the deliberate operator migration workflow.',
      ),
    );
  }
  const form = el('form'),
    label = el('label', 'Why reject this candidate?');
  label.htmlFor = 'candidate-reason';
  const reason = el('textarea');
  reason.id = label.htmlFor;
  reason.maxLength = 2000;
  reason.required = true;
  const reject = decision(false, () => {
    if (!reason.value.trim()) throw new Error('Give a rejection reason.');
    return reason.value;
  });
  form.append(label, reason, reject);
  content.append(
    form,
    note(
      'Rejection preserves the artifact privately and keeps a hold. It does not start another model attempt.',
    ),
    problem,
  );
  main.append(content);
}
