import { randomHex } from '/app/keys.js';
import { WORDS, notedClause } from '/app/words.js';

// The saved check, as sentences the owner can read at a glance.
function checks(review) {
  const lines = [];
  if (typeof review.passed === 'boolean')
    lines.push(review.passed ? 'Checks passed' : 'Checks did not pass');
  if (typeof review.location_free === 'boolean')
    lines.push(review.location_free ? 'No place details in the image' : 'The image may show place details');
  const scores = [
    ['Species', review.species_accuracy],
    ['Anatomy', review.anatomy_accuracy],
    ['Lettering', review.text_accuracy],
    ['Composition', review.composition_quality],
  ]
    .filter(([, score]) => Number.isInteger(score))
    .map(([name, score]) => name + ' ' + score + ' of 5');
  if (scores.length) lines.push(scores.join(' · '));
  return lines;
}

export async function renderCandidate(context) {
  const { ui, el, button, api, main, query, href, link, note, base, byId } = context;
  base('Review candidate');
  main.append(link('Back to ' + WORDS.inTheWorks, 'work'));
  const candidate = await api(
    '/v1/owner/candidates/' + encodeURIComponent(query.get('bird') || ''),
  );
  const noted = notedClause(byId?.get(candidate.taxon_id)?.retained_evidence);
  const intro = main.querySelector('.intro');
  if (intro)
    intro.textContent = candidate.common_name + (noted ? ' — ' + noted : '') + '.';
  const content = el('section', '', 'journal-card');
  const image = el('img', '', 'detail-art');
  image.src = candidate.portrait_url;
  image.alt = 'New plate, not approved yet: ' + candidate.common_name;
  content.append(image, el('h2', candidate.common_name), note(candidate.scientific_name));
  // A plate that cannot be kept carries the reason from the controller.
  if (!candidate.can_approve && candidate.notice) content.append(note(candidate.notice));
  const lines = checks(candidate.quality_review || {});
  if (lines.length) {
    const list = el('ul', '', 'plate-checks');
    for (const line of lines) list.append(el('li', line));
    content.append(el('h2', 'Checks'), list);
  }
  if (candidate.profile) {
    content.append(el('h2', 'Field notes'), note(candidate.profile.family));
    const facts = el('dl', '', 'measurements');
    for (const [name, value] of Object.entries(candidate.profile.measurements))
      facts.append(el('dt', name[0].toUpperCase() + name.slice(1)), el('dd', value));
    const marks = el('ul');
    for (const mark of candidate.profile.field_marks) marks.append(el('li', mark));
    content.append(
      facts,
      marks,
      el('p', candidate.profile.habitat),
      el('p', candidate.profile.behavior),
    );
  }
  if (candidate.verification_sources.length) {
    const references = el('ul', '', 'candidate-references');
    for (const source of candidate.verification_sources) {
      const item = el('li'),
        anchor = el('a', source.title);
      anchor.href = source.url;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      item.append(anchor);
      references.append(item);
    }
    content.append(el('h2', 'References'), references);
  }
  let loaded = false;
  image.addEventListener('load', () => {
    loaded = true;
  });
  if (image.complete && image.naturalWidth) loaded = true;
  const problem = el('p', '', 'error');
  problem.setAttribute('role', 'alert');
  // Answers that mean the controller kept nothing from this request.
  const refusals = new Set([
    'access_error',
    'journal_changed',
    'journal_error',
    'journal_unavailable',
    'operation_busy',
    'operation_error',
  ]);
  function decision(approve, reason = '') {
    let key = randomHex(16),
      request = null;
    return button(approve ? 'Confirm local approval' : 'Confirm rejection', async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      try {
        if (approve && !loaded) throw new Error('Wait for the whole plate to load before approving it.');
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
        else if (!request || refusals.has(error.code)) {
          // A refusal saved nothing, so the next try uses current settings and text.
          request = null;
          key = randomHex(16);
          problem.textContent = error.message;
        } else
          // The decision may have been saved: send exactly the same one again.
          problem.textContent = error.message + ' Trying again is safe. Check recent operations if the response was lost.';
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
      note('Approved plates join your journal. If you publish plates, a published copy cannot be taken back.'),
    );
  }
  const form = el('form'),
    label = el('label', 'Why reject this candidate?');
  label.htmlFor = 'candidate-reason';
  const reason = el('textarea');
  reason.id = label.htmlFor;
  reason.maxLength = 2000;
  reason.required = true;
  const hint = note('This note is sent to the image service to guide the next attempt.');
  hint.id = 'candidate-reason-hint';
  reason.setAttribute('aria-describedby', hint.id);
  const reject = decision(false, () => {
    if (!reason.value.trim()) throw new Error('Give a rejection reason.');
    return reason.value;
  });
  form.append(label, reason, hint, reject);
  content.append(
    form,
    note('Rejecting keeps a private copy. No new plate starts until you ask for one.'),
    problem,
  );
  main.append(content);
}
