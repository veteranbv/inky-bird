import { plateCounts, plural, shortDate } from '/app/words.js';

// With nothing on the frame, Home still opens on a plate: one from the
// catalog, the same all day.
function catalogPlate(birds) {
  const plates = birds.filter((bird) => bird.plate_ready);
  if (!plates.length) return null;
  const now = new Date();
  const day = Math.floor(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 86400000);
  return plates[day % plates.length];
}

export function renderHome(context) {
  const {
    el, main, data, byId, management, link, note, art, evidence, base, problems, playLink,
    whyThisBird,
  } = context;
  base('Your field journal', 'Birds worth keeping close.');
  problems();
  const onFrame = byId.get(data.featured_taxon_id);
  // When the frame's birds could not be read, it may not be empty.
  const frameUnknown = (data.notices || []).some((notice) => notice.part === 'current-birds');
  const featured = onFrame || (frameUnknown ? null : catalogPlate(data.birds));
  // Later notes join the plate's caption, which sits beside it on wide screens.
  let rail = main;
  if (featured) {
    const hero = link('', 'bird', { bird: featured.taxon_id }, 'featured-art');
    hero.append(art(featured));
    const caption = el('div', '', 'featured-caption');
    caption.append(el('h2', featured.common_name), el('p', featured.scientific_name, 'scientific'));
    if (onFrame) {
      caption.append(note(evidence(featured)));
      const why = whyThisBird(featured);
      if (why) caption.append(el('p', why, 'hint why-this-bird'));
    }
    const feature = el('div', '', 'home-feature');
    rail = el('div', '', 'home-rail');
    rail.append(caption);
    feature.append(hero, rail);
    main.append(feature);
  }
  const actions = el('div', '', 'home-actions');
  if (onFrame) actions.append(playLink('current'), link('Browse your birds', 'birds'));
  else {
    // A journal with parts it could not read is not a new one.
    const unreadable =
      management.reason === 'unreadable' ||
      (data.notices || []).length > 0 ||
      (data.history_coverage || []).some((source) => source.status === 'unavailable');
    const fresh =
      !unreadable &&
      !data.birds.some((bird) => bird.known) &&
      (!management.available || !management.sources.some((source) => source.enabled));
    if (!frameUnknown)
      rail.append(el('p', 'Pick a few birds you love to start your frame.', 'home-start'));
    actions.append(link('Choose birds', 'birds', { scope: 'art' }, 'primary-link'));
    if (fresh) actions.append(link('Connect a source (optional)', 'sources'));
  }
  rail.append(actions);
  const { newPlates } = plateCounts(management);
  if (newPlates) {
    // One plate opens directly; several are listed where they are made.
    const first = management.work.in_review.candidates.find((candidate) => candidate.inspect_available);
    const card =
      newPlates === 1
        ? link('', 'candidate', { bird: first.taxon_id }, 'journal-card new-plates')
        : link('', 'work', {}, 'journal-card new-plates');
    card.textContent = plural(newPlates, 'new plate is', 'new plates are') + ' ready to look at.';
    main.append(card);
  }
  lately(context);
  if (management.available) {
    if (!management.sources.some((source) => source.enabled))
      rail.append(note('No sources are checked for new birds. Plates and collections still work.'));
    else if (!management.work.complete)
      rail.append(note('The last check for new birds did not finish. Your frame may be out of date.'));
    // Facts the owner has to act on stay on Home; plates that need a hand stay in Manage.
    if (management.sources.some((source) => source.enabled && source.status === 'error'))
      rail.append(link('A source check failed.', 'sources', {}, 'text-link home-alert'));
    if (management.notifications?.dead_letters)
      rail.append(link('Some notifications did not get through.', 'notifications', {}, 'text-link home-alert'));
    if (management.last_confirmed_panel_update)
      rail.append(
        note(
          'Wall frame last updated ' +
            new Date(management.last_confirmed_panel_update).toLocaleString(),
        ),
      );
  }
}

// The latest birds in the journal, newest first, and how many it holds.
function lately({ el, main, data, byId, link }) {
  const recent = (data.recent_species || [])
    .map((entry) => ({ bird: byId.get(entry.taxon_id), when: shortDate(entry.last_noted) }))
    .filter(({ bird, when }) => bird && when)
    .slice(0, 3);
  let holder = main;
  if (recent.length) {
    holder = el('section', '', 'lately');
    holder.append(el('h2', 'Lately'));
    const list = el('ul', '', 'lately-list');
    for (const { bird, when } of recent) {
      const item = el('li');
      const entry = link('', 'bird', { bird: bird.taxon_id }, 'lately-bird');
      // The space keeps the name and date apart when read aloud.
      entry.append(
        el('span', bird.common_name, 'lately-name'),
        document.createTextNode(' '),
        el('span', when, 'lately-date'),
      );
      item.append(entry);
      list.append(item);
    }
    holder.append(list);
    main.append(holder);
  }
  const known = data.birds.filter((bird) => bird.known);
  if (!known.length) return;
  const plates = known.filter((bird) => bird.plate_ready).length;
  const parts = [plural(known.length, 'bird', 'birds') + ' in your journal'];
  if (plates) parts.push(plates === 1 ? '1 with a plate' : plates + ' with plates');
  holder.append(el('p', parts.join(' · '), 'hint journal-size'));
}
