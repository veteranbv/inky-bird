import { icon } from '/app/ui.js';

export function renderHome(context) {
  const {
    el, main, data, byId, management, link, note, art, evidence, base, problems, empty, playLink,
  } = context;
  base('Your field journal', 'Birds worth keeping close.');
  problems();
  const featured = byId.get(data.featured_taxon_id);
  if (featured) {
    const hero = link('', 'bird', { bird: featured.taxon_id }, 'featured-art');
    hero.append(art(featured));
    const caption = el('div', '', 'featured-caption');
    caption.append(
      el('h2', featured.common_name),
      el('p', featured.scientific_name, 'scientific'),
      note(evidence(featured)),
    );
    const actions = el('div', '', 'home-actions');
    actions.append(playLink('current'), link('Browse your birds', 'birds'));
    main.append(hero, caption, actions);
  } else {
    const fresh =
      !data.birds.some((bird) => bird.known) &&
      (!management.available || !management.sources.some((source) => source.enabled));
    if (fresh) {
      main.append(el('h2', 'Welcome. Start wherever you like.'));
      const start = el('div', '', 'manage-grid start-paths');
      for (const [title, text, glyph, page, extra] of [
        [
          'Browse the artwork',
          'Reviewed field-journal plates are ready to enjoy now. Browsing never records a sighting.',
          'birds',
          'birds',
          { scope: 'art' },
        ],
        [
          'Keep birds close',
          'Save Favorites and build collections for the wall and the Frame view.',
          'collections',
          'collections',
          {},
        ],
        [
          'Connect your birds',
          'Add eBird, a feeder or a sound station when you are ready. Artwork works without them.',
          'manage',
          'sources',
          {},
        ],
      ]) {
        const card = link('', page, extra, 'journal-card');
        card.append(icon(glyph), el('h2', title), el('p', text));
        start.append(card);
      }
      main.append(start);
    } else
      main.append(
        empty(
          'Make room for a bird',
          'No approved plate is in your current selection yet. Browse available artwork or open a saved collection.',
        ),
        link('Browse available artwork', 'birds', { scope: 'art' }, 'primary-link'),
      );
  }
  if (management.available) {
    if (!management.sources.some((source) => source.enabled))
      main.append(
        note('No sources selected for refresh. Artwork and saved collections still work.'),
      );
    else if (!management.work.complete)
      main.append(
        note(
          'Discovery is ' +
            management.work.discovery_status +
            '. The last saved selection may be out of date.',
        ),
      );
    if (management.last_confirmed_panel_update)
      main.append(
        note(
          'Last confirmed panel update: ' +
            new Date(management.last_confirmed_panel_update).toLocaleString(),
        ),
      );
  }
}
