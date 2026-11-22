import { frameTools, compact, glyph, stepIndex } from '/app/frame-tools.js';
import { clampedNotice } from '/app/ui.js';

// Longer than one playing turn, so a slow plate is replaced by the next turn first.
const PLATE_TIMEOUT = 90000;

export function renderFrame(context) {
  const {
    ui, el, button, api, main, view, data, byId, plateURL, href, link, note, members, viewLabel,
    base, empty, reopen, closeJournal, setStopScreen, setFrameSignOut, whyThisBird,
  } = context;
  let birds = members(view).filter((bird) => bird.plate_ready);
  base('Play', viewLabel(view));
  // The members of this view in the journal `current`; undefined when removed.
  const viewIds = (current) =>
    view === 'favorites'
      ? current.collections.find((group) => group.kind === 'favorites')?.taxon_ids || []
      : view === 'all'
        ? current.birds.map((bird) => bird.taxon_id)
        : view === 'current'
          ? current.active_taxon_ids
          : view === 'saved'
            ? current.saved_taxon_ids
            : current.collections.find((group) => group.collection_id === view)?.taxon_ids;
  const ready = (current, ids) =>
    Boolean(ids) &&
    ids.some((id) => current.birds.some((bird) => bird.taxon_id === id && bird.plate_ready));
  if (!birds.length) {
    const card = empty('No plates here yet', 'Play starts by itself once this view has plates.');
    main.append(card, link('Exit Play', 'collections'));
    let left = false;
    // A view that is empty for now starts playing once its plates arrive.
    const waiting = setInterval(async () => {
      if (document.hidden) return;
      try {
        const current = await api('/v1/owner/journal');
        if (left || !card.isConnected) return;
        if (ready(current, viewIds(current))) {
          clearInterval(waiting);
          await reopen();
        }
      } catch (error) {
        if (left || !card.isConnected) return;
        if (['sign_in_required', 'secure_access_required'].includes(error.code)) {
          clearInterval(waiting);
          ui.showProblem(error);
        }
      }
    }, 60000);
    setStopScreen(() => {
      left = true;
      clearInterval(waiting);
    });
    return;
  }
  document.body.classList.add('presenting');
  document.querySelector('#journal-navigation')?.remove();
  let position = 0,
    paused = true,
    timer = null,
    expired = false,
    shuffle = false;
  let shownRevision = data.revision,
    shownSignature = birds.map((bird) => bird.taxon_id + ':' + bird.portrait_sha256).join(' ');
  let image = el('img', '', 'frame-art'),
    objectURL = null,
    renderedKey = '',
    loading = null;
  image.hidden = true;
  image.draggable = false;
  const caption = el('p', '', 'frame-caption'),
    // "Noted at your feeder this week": only when the page's helper has a line for this bird.
    why = el('p', '', 'hint frame-why'),
    status = el('p', '', 'hint');
  why.hidden = true;
  status.setAttribute('role', 'status');
  const controls = el('div', '', 'frame-controls');
  const viewport = el('div', '', 'frame-viewport');
  viewport.append(image);
  function withdraw() {
    loading?.abort();
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null;
    renderedKey = '';
    image.removeAttribute('src');
    image.hidden = true;
    caption.textContent = '';
    why.textContent = '';
    why.hidden = true;
  }
  function signedOut() {
    if (expired) return;
    ui.sessionExpired();
    expired = true;
    pause();
    closeJournal();
    image.alt = 'The last plate shown';
    document.title = 'Inky';
    status.textContent =
      'You are signed out. The last plate stays here. Sign in, or pair this screen to keep showing birds.';
    main.replaceChildren(image, status, link('Sign in', 'home'));
    byId.clear();
    for (const key of Object.keys(data)) delete data[key];
    birds = [];
  }
  async function show(index = position) {
    const bird = birds[index];
    if (!bird || expired) return;
    const key = bird.taxon_id + ':' + bird.portrait_sha256;
    loading?.abort();
    if (key === renderedKey) {
      position = index;
      status.textContent = '';
      return;
    }
    const request = new AbortController();
    loading = request;
    let nextURL,
      timedOut = false;
    // A hung plate request must not hold Frame: it ends by this deadline.
    const deadline = setTimeout(() => {
      timedOut = true;
      request.abort();
    }, PLATE_TIMEOUT);
    try {
      const response = await fetch(plateURL(bird), {
        credentials: 'same-origin',
        signal: request.signal,
      });
      if ((request.signal.aborted && !timedOut) || !image.isConnected) return;
      if ([401, 403].includes(response.status)) {
        signedOut();
        return;
      }
      if (!response.ok || response.headers.get('content-type')?.split(';')[0] !== 'image/png')
        throw new Error('Plate unavailable');
      const body = await response.blob();
      clearTimeout(deadline);
      nextURL = URL.createObjectURL(body);
      const nextImage = el('img', '', 'frame-art');
      nextImage.draggable = false;
      nextImage.src = nextURL;
      nextImage.alt = 'Plate: ' + bird.common_name;
      await nextImage.decode();
      if (request.signal.aborted || expired || !image.isConnected) return;
      image.replaceWith(nextImage);
      image = nextImage;
      if (objectURL) URL.revokeObjectURL(objectURL);
      objectURL = nextURL;
      nextURL = null;
      renderedKey = key;
      position = index;
      caption.replaceChildren(
        el('span', bird.common_name),
        el('span', bird.scientific_name, 'scientific'),
      );
      why.textContent = whyThisBird?.(bird) || '';
      why.hidden = !why.textContent;
      details.href = href('bird', { bird: bird.taxon_id });
      details.hidden = false;
      status.textContent = '';
    } catch (error) {
      // One failed plate never stops playback: the next turn moves past it.
      // While paused, Previous and Next stay relative to the plate shown.
      if ((timedOut || !request.signal.aborted) && !expired && image.isConnected) {
        if (!paused) position = index;
        status.textContent = paused
          ? 'This plate did not load. Keeping the last one. Try Next when the connection is back.'
          : 'This plate did not load. Keeping the last one; Play goes on.';
      }
    } finally {
      clearTimeout(deadline);
      if (nextURL) URL.revokeObjectURL(nextURL);
    }
  }
  // An emptied or removed view leaves no list to step through.
  function step(change) {
    const index = stepIndex(position, change, birds.length);
    if (!document.hidden && index >= 0) show(index);
  }
  function pause() {
    paused = true;
    clearInterval(timer);
    play.setAttribute('aria-pressed', 'false');
    glyph(play, 'play');
  }
  function togglePlay() {
    if (!paused) {
      pause();
      return;
    }
    paused = false;
    play.setAttribute('aria-pressed', 'true');
    glyph(play, 'pause');
    timer = setInterval(() => {
      if (!document.hidden && !expired)
        step(
          shuffle && birds.length > 1 ? 1 + Math.floor(Math.random() * (birds.length - 1)) : 1,
        );
    }, 60000);
  }
  const play = compact(button('Play', togglePlay), 'play');
  play.setAttribute('aria-pressed', 'false');
  const details = link('Details', 'bird', { bird: birds[0].taxon_id });
  details.addEventListener('click', pause);
  const random = button('Shuffle', () => {
    shuffle = !shuffle;
    random.setAttribute('aria-pressed', String(shuffle));
  });
  random.setAttribute('aria-pressed', 'false');
  const labels = button('Bird name', () => {
    const hidden = document.body.classList.toggle('frame-labels-off');
    labels.setAttribute('aria-pressed', String(!hidden));
  });
  labels.setAttribute('aria-pressed', 'true');
  const stopTools = frameTools({
    viewport,
    controls,
    status,
    button,
    primary: [
      compact(
        button('Previous', () => step(-1)),
        'previous',
      ),
      play,
      compact(
        button('Next', () => step(1)),
        'next',
      ),
    ],
    secondary: [details, link('Exit Play', 'collections'), random, labels],
    previous: () => step(-1),
    next: () => step(1),
    togglePlay,
  });
  // Check authority, not activity. A display view must not renew an owner session.
  const check = setInterval(async () => {
    if (document.hidden) return;
    try {
      const current = await api('/v1/owner/journal');
      if (!image.isConnected || expired) return;
      const ids = viewIds(current);
      // An empty or removed view clears the plate but keeps checking, so
      // plates that return play again without a reload.
      if (!ready(current, ids)) {
        birds = [];
        withdraw();
        details.hidden = true;
        status.textContent =
          'This view has no plates now. Play waits here and starts again when it has some.';
        return;
      }
      const next = current.birds.filter(
        (bird) => ids.includes(bird.taxon_id) && bird.plate_ready,
      );
      const signature = next.map((bird) => bird.taxon_id + ':' + bird.portrait_sha256).join(' ');
      // Nothing changed and the current plate is showing: leave Frame as it is.
      if (
        current.revision === shownRevision &&
        signature === shownSignature &&
        renderedKey &&
        birds[position] &&
        renderedKey === birds[position].taxon_id + ':' + birds[position].portrait_sha256
      ) {
        status.textContent = '';
        return;
      }
      shownRevision = current.revision;
      shownSignature = signature;
      const previous = birds[position]?.taxon_id;
      birds = next;
      if (
        renderedKey &&
        !birds.some((bird) => bird.taxon_id + ':' + bird.portrait_sha256 === renderedKey)
      )
        withdraw();
      await show(
        Math.max(
          0,
          birds.findIndex((bird) => bird.taxon_id === previous),
        ),
      );
    } catch (error) {
      if (['sign_in_required', 'secure_access_required'].includes(error.code)) {
        signedOut();
      } else {
        loading?.abort();
        status.textContent = 'Offline. Keeping the last plate; Inky keeps checking.';
      }
    }
  }, 60000);
  const notice = clampedNotice(status, button);
  setStopScreen(() => {
    clearInterval(timer);
    clearInterval(check);
    loading?.abort();
    if (objectURL) URL.revokeObjectURL(objectURL);
    stopTools();
    notice.stop();
  });
  setFrameSignOut(signedOut);
  const dock = el('div', '', 'frame-dock');
  dock.append(status, notice.control, controls);
  // The bird name and notes sit beside the plate on a short landscape screen.
  const side = el('div', '', 'frame-side');
  side.append(
    caption,
    why,
    note('Plates change once a minute while playing. Your wall frame is not affected.'),
  );
  if (!window.isSecureContext)
    side.append(note('This screen may sleep: keeping it awake needs an HTTPS address.'));
  // Last in the column, so a notice of any length takes its own room above
  // the bar instead of covering the bird name or the notes.
  main.append(viewport, side, dock);
  show();
}
