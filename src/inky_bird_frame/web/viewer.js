// The read-only screen has no owner cookie handling, private history or storage.
import { frameTools, compact, glyph, stepIndex, confirmAction } from '/app/frame-tools.js';
const main = document.querySelector('#main');
// A hung request must not stop a wall screen: every request ends by this deadline.
const REQUEST_TIMEOUT = 20000;
const el = (tag, text = '') => {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
};
const button = (text, action) => {
  const node = el('button', text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
};
let stopped = false,
  timer = null,
  objectURL = null,
  loading = null,
  sequence = 0,
  presentation = 0,
  stopTools = () => {};
function notResponding() {
  return Object.assign(new Error('Inky is not responding. Keep this page open; it will try again.'), {
    code: 'offline',
  });
}
async function api(path, body) {
  const request = new AbortController();
  const deadline = setTimeout(() => request.abort(), REQUEST_TIMEOUT);
  try {
    let response;
    try {
      response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: request.signal,
      });
    } catch {
      if (request.signal.aborted) throw notResponding();
      throw Object.assign(new Error('Offline. Keep this page open and check your connection.'), {
        code: 'offline',
      });
    }
    let result;
    try {
      result = await response.json();
    } catch {
      if (request.signal.aborted) throw notResponding();
      throw new Error('Inky is not responding. Try again shortly.');
    }
    if (!response.ok || !result.ok)
      throw Object.assign(new Error(result.error?.message || 'Screen access is unavailable.'), {
        code: result.error?.code,
      });
    return result.data;
  } finally {
    clearTimeout(deadline);
  }
}
function clearArt() {
  stopTools();
  stopTools = () => {};
  sequence += 1;
  loading?.abort();
  if (objectURL) URL.revokeObjectURL(objectURL);
  objectURL = null;
  main.querySelector('img')?.remove();
  document.body.classList.remove('presenting');
  document.title = 'Inky screen';
}
// The request comes first: until it succeeds, the screen keeps showing what it showed.
async function forget(status, failure) {
  try {
    await api('/v1/screen/forget', {});
  } catch (error) {
    status.textContent = failure + ' ' + error.message;
    throw error;
  }
  stopped = true;
  clearTimeout(timer);
  clearArt();
  location.reload();
}
function unavailable(message) {
  stopped = true;
  clearTimeout(timer);
  clearArt();
  main.replaceChildren(
    el('h1', 'Screen access ended'),
    el('p', message),
    button('Pair this screen again', pair),
  );
}
async function pair() {
  stopped = false;
  clearArt();
  clearTimeout(timer);
  try {
    const pending = await api('/v1/screen/pair', {});
    if (pending.status === 'viewer') {
      await start();
      return;
    }
    const code = el('p', pending.pairing_code);
    code.className = 'pairing-code';
    const status = el('p', 'Waiting for the owner. This request lasts ten minutes.');
    status.setAttribute('role', 'status');
    const cancel = button('Cancel pairing', async () => {
      cancel.disabled = true;
      try {
        await forget(status, 'Pairing was not cancelled.');
      } catch {
        cancel.disabled = false;
      }
    });
    main.replaceChildren(
      el('h1', 'Pair this screen'),
      code,
      el(
        'p',
        'On your signed-in device, open Manage → Displays. Match this code, name this screen and choose the view it may show.',
      ),
      el(
        'p',
        'Pairing permits approved artwork only. It does not sign this screen in as the owner.',
      ),
      status,
      cancel,
    );
    let attempts = 0;
    const check = async () => {
      if (stopped || ++attempts > 40) {
        unavailable('Pairing expired. Start a new request.');
        return;
      }
      try {
        const view = await api('/v1/screen/view');
        if (!stopped) present(view);
      } catch (error) {
        if (stopped) return;
        status.textContent =
          error.code === 'offline' ? error.message : 'Waiting for owner approval.';
        timer = setTimeout(check, 15000);
      }
    };
    timer = setTimeout(check, 15000);
  } catch (error) {
    unavailable(error.message);
  }
}
function present(initial) {
  clearTimeout(timer);
  stopped = false;
  const current = ++presentation;
  const live = () => !stopped && current === presentation;
  document.body.classList.add('presenting');
  let birds = initial.species,
    position = 0,
    paused = false,
    shuffle = false,
    rendered = '';
  const image = el('img'),
    caption = el('p'),
    status = el('p');
  image.className = 'frame-art';
  image.draggable = false;
  caption.className = 'frame-caption';
  image.hidden = true;
  // The shared status contract: presenting-mode notices float inside the
  // viewport, so a fullscreen screen never hides its reason below the fold.
  status.className = 'hint';
  status.setAttribute('role', 'status');
  const controls = el('div');
  controls.className = 'frame-controls';
  const togglePlay = () => {
    paused = !paused;
    play.setAttribute('aria-pressed', String(!paused));
    glyph(play, paused ? 'play' : 'pause');
  };
  const play = compact(button('Play', togglePlay), 'pause');
  play.setAttribute('aria-pressed', 'true');
  const step = (change) => show(stepIndex(position, change, birds.length));
  const previous = compact(
    button('Previous', () => step(-1)),
    'previous',
  );
  const next = compact(
    button('Next', () => step(1)),
    'next',
  );
  const random = button('Shuffle', () => {
    shuffle = !shuffle;
    random.setAttribute('aria-pressed', String(shuffle));
  });
  random.setAttribute('aria-pressed', 'false');
  const forgetting = confirmAction({
    button,
    label: 'Forget this screen',
    question: 'Forget this screen? It will need pairing again.',
    confirmLabel: 'Forget',
    cancelLabel: 'Keep showing birds',
    action: () => forget(status, 'This screen was not forgotten.'),
  });
  const viewport = el('div');
  viewport.className = 'frame-viewport';
  viewport.append(image);
  stopTools = frameTools({
    viewport,
    controls,
    status,
    button,
    primary: [previous, play, next],
    secondary: [random, forgetting.node],
    previous: () => step(-1),
    next: () => step(1),
    togglePlay,
    onQuiet: forgetting.reset,
  });
  const dock = el('div');
  dock.className = 'frame-dock';
  dock.append(status, controls);
  main.replaceChildren(viewport, caption, dock);
  function withdraw() {
    sequence += 1;
    loading?.abort();
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null;
    rendered = '';
    image.removeAttribute('src');
    image.hidden = true;
    caption.textContent = '';
  }
  async function show(index = position) {
    const bird = birds[index];
    if (!bird) {
      withdraw();
      status.textContent = 'No approved plates in the permitted view. Inky will not switch views.';
      return;
    }
    position = index;
    const key = bird.taxon_id + ':' + bird.portrait_sha256;
    if (rendered === key) {
      status.textContent = '';
      return;
    }
    loading?.abort();
    const controller = new AbortController();
    loading = controller;
    const pending = ++sequence;
    let timedOut = false;
    const deadline = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT);
    try {
      const response = await fetch(bird.portrait_url, {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      if ([401, 403].includes(response.status)) {
        unavailable('Access expired or was revoked.');
        return;
      }
      if (!response.ok) throw new Error('The selected plate is temporarily unavailable.');
      const url = URL.createObjectURL(await response.blob());
      const decoded = new Image();
      decoded.src = url;
      try {
        await decoded.decode();
      } catch {
        URL.revokeObjectURL(url);
        throw new Error('This image could not be displayed.');
      }
      if (stopped || pending !== sequence) {
        URL.revokeObjectURL(url);
        return;
      }
      if (objectURL) URL.revokeObjectURL(objectURL);
      objectURL = url;
      image.src = url;
      image.hidden = false;
      image.alt = 'Approved field-journal plate: ' + bird.common_name;
      caption.textContent = bird.common_name;
      rendered = key;
      status.textContent = '';
    } catch (error) {
      if ((timedOut || !controller.signal.aborted) && !stopped)
        status.textContent =
          (timedOut ? 'The selected plate is temporarily unavailable.' : error.message) +
          ' Keeping the last approved image.';
    } finally {
      clearTimeout(deadline);
    }
  }
  let turn = Date.now();
  async function poll() {
    if (!live()) return;
    try {
      if (document.hidden) return;
      const view = await api('/v1/screen/view');
      if (!live()) return;
      const id = birds[position]?.taxon_id;
      birds = view.species;
      if (
        rendered &&
        !birds.some((bird) => bird.taxon_id + ':' + bird.portrait_sha256 === rendered)
      )
        withdraw();
      position = Math.max(
        0,
        birds.findIndex((bird) => bird.taxon_id === id),
      );
      if (!paused && Date.now() - turn >= 60000) {
        position = shuffle
          ? Math.floor(Math.random() * birds.length)
          : stepIndex(position, 1, birds.length);
        turn = Date.now();
      }
      await show(position);
    } catch (error) {
      if (!live()) return;
      if (error.code === 'sign_in_required' || error.code === 'secure_access_required') {
        unavailable(error.message);
        return;
      }
      status.textContent = error.message + ' Keeping the last approved image.';
    } finally {
      if (live()) timer = setTimeout(poll, 15000);
    }
  }
  show();
  timer = setTimeout(poll, 15000);
}
async function start() {
  try {
    present(await api('/v1/screen/view'));
  } catch (error) {
    main.replaceChildren(
      el('h1', 'Show birds here'),
      el(
        'p',
        error.code === 'offline'
          ? error.message
          : 'Pair this browser to show one approved view without owner access.',
      ),
      button('Pair this screen', pair),
    );
  }
}
addEventListener('pagehide', () => {
  stopped = true;
  clearTimeout(timer);
  loading?.abort();
  stopTools();
  if (objectURL) URL.revokeObjectURL(objectURL);
});
await start();
