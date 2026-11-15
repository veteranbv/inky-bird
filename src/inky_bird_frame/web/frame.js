import { frameTools, compact, glyph, stepIndex } from '/app/frame-tools.js';

// Longer than one playing turn, so a slow plate is replaced by the next turn first.
const PLATE_TIMEOUT = 90000;

export function renderFrame(context) {
  const {
    ui, el, button, api, main, view, data, byId, plateURL, href, link, note, members, viewLabel,
    base, empty, closeJournal, setStopScreen, setFrameSignOut,
  } = context;
  let birds = members(view).filter((bird) => bird.plate_ready);
  base(viewLabel(view));
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
    const card = empty('No approved plates in this view', 'Inky will not switch to another collection.');
    main.append(card, link('Exit Frame', 'collections'));
    let left = false;
    // A view that is empty for now starts playing once its plates arrive.
    const waiting = setInterval(async () => {
      if (document.hidden) return;
      try {
        const current = await api('/v1/owner/journal');
        if (left || !card.isConnected) return;
        if (ready(current, viewIds(current))) location.reload();
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
    status = el('p', '', 'hint');
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
  }
  function signedOut() {
    if (expired) return;
    ui.sessionExpired();
    expired = true;
    pause();
    closeJournal();
    image.alt = 'Last visible approved bird plate';
    document.title = 'Inky';
    status.textContent =
      'Signed out. The last approved image stays visible. Sign in, or pair this screen for continuous read-only viewing.';
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
      nextImage.alt = 'Approved plate: ' + bird.common_name;
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
      details.href = href('bird', { bird: bird.taxon_id });
      details.hidden = false;
      status.textContent = '';
    } catch (error) {
      // One failed plate never stops playback: the next turn moves past it.
      // While paused, Previous and Next stay relative to the plate shown.
      if ((timedOut || !request.signal.aborted) && !expired && image.isConnected) {
        if (!paused) position = index;
        status.textContent = paused
          ? 'Plate unavailable. Keeping the last approved plate. Try Next when the connection returns.'
          : 'Plate unavailable. Keeping the last approved plate. Playback continues.';
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
    secondary: [details, link('Exit Frame', 'collections'), random, labels],
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
          'This view has no approved plates or was removed. Inky will not switch views.';
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
        status.textContent = 'Offline. Keeping the last approved plate. Inky will keep checking.';
      }
    }
  }, 60000);
  let noticeRoom = null;
  setStopScreen(() => {
    clearInterval(timer);
    clearInterval(check);
    loading?.abort();
    if (objectURL) URL.revokeObjectURL(objectURL);
    stopTools();
    if (noticeRoom) {
      noticeRoom.disconnect();
      main.style.removeProperty('--frame-notice-room');
    }
  });
  setFrameSignOut(signedOut);
  const dock = el('div', '', 'frame-dock');
  dock.append(status, controls);
  // A notice can take several lines above the bar; keep the bird name and
  // note clear of it. The More menu still opens over the page.
  if (typeof ResizeObserver === 'function') {
    noticeRoom = new ResizeObserver(() =>
      main.style.setProperty(
        '--frame-notice-room',
        (status.offsetHeight ? status.offsetHeight + 8 : 0) + 'px',
      ),
    );
    noticeRoom.observe(status);
  }
  main.append(
    viewport,
    caption,
    dock,
    note('Plates change once a minute while playing. This does not refresh the wall panel.'),
  );
  if (!window.isSecureContext)
    main.append(note('This screen may sleep: keeping it awake needs an HTTPS address.'));
  show();
}
