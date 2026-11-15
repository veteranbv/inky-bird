import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

// app.js starts the whole page when imported, so this loads only its request
// helpers, unchanged, from the shipped source.
const app = await readFile(new URL('../../src/inky_bird_frame/web/app.js', import.meta.url), 'utf8');
const start = app.indexOf('class RequestError');
const end = app.indexOf('function element(');
assert.ok(start > 0 && end > start, 'request helpers not found in app.js');
const helpers = await import(
  'data:text/javascript;base64,' +
    Buffer.from(
      "let csrf = 'csrf-token';\n" +
        app.slice(start, end) +
        '\nexport { upload, request, RequestError };\n',
    ).toString('base64')
);

const OFFLINE = 'Inky cannot be reached. Check your connection and try again.';

// Records what the helper sends; each test drives the reply.
class FakeXHR {
  static last = null;
  constructor() {
    this.headers = {};
    this.upload = {};
    this.status = 0;
    this.responseText = '';
    this.withCredentials = false;
    FakeXHR.last = this;
  }
  open(method, path) {
    this.method = method;
    this.path = path;
  }
  setRequestHeader(name, value) {
    this.headers[name] = value;
  }
  send(body) {
    this.body = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  reply(status, text) {
    this.status = status;
    this.responseText = text;
    this.onload?.();
  }
}

function withXHR(action) {
  return async () => {
    const original = globalThis.XMLHttpRequest;
    const originalFetch = globalThis.fetch;
    globalThis.XMLHttpRequest = FakeXHR;
    globalThis.fetch = () => {
      throw new Error('upload must not use fetch');
    };
    FakeXHR.last = null;
    try {
      await action();
    } finally {
      globalThis.XMLHttpRequest = original;
      globalThis.fetch = originalFetch;
    }
  };
}

async function rejection(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail('upload resolved');
}

test(
  'the upload helper sends the file with the owner headers and reports progress',
  withXHR(async () => {
    const file = { size: 300 };
    const seen = [];
    const result = helpers.upload('/v1/owner/imports/upload/ebird-archive', file, undefined, (
      loaded,
      total,
    ) => seen.push([loaded, total]));
    const xhr = FakeXHR.last;
    assert.equal(xhr.method, 'POST');
    assert.equal(xhr.path, '/v1/owner/imports/upload/ebird-archive');
    assert.deepEqual(xhr.headers, {
      'Content-Type': 'application/octet-stream',
      'X-Inky-CSRF': 'csrf-token',
    });
    assert.equal(xhr.withCredentials, false);
    assert.equal(xhr.body, file);
    xhr.upload.onprogress({ loaded: 100, total: 300, lengthComputable: true });
    xhr.upload.onprogress({ loaded: 5, total: 0, lengthComputable: false });
    xhr.reply(200, JSON.stringify({ ok: true, data: { upload_id: 'a' } }));
    assert.deepEqual(await result, { upload_id: 'a' });
    assert.deepEqual(seen, [
      [100, 300],
      [5, 0],
    ]);
  }),
);

test(
  'the progress callback stays optional',
  withXHR(async () => {
    const result = helpers.upload('/v1/upload', { size: 1 });
    FakeXHR.last.upload.onprogress?.({ loaded: 1, total: 1, lengthComputable: true });
    FakeXHR.last.reply(200, JSON.stringify({ ok: true, data: 'done' }));
    assert.equal(await result, 'done');
  }),
);

// A stop the owner asked for is its own outcome, not a network fault.
test(
  'stopping an upload aborts the request and reports it as stopped',
  withXHR(async () => {
    const controller = new AbortController();
    const result = helpers.upload('/v1/upload', { size: 1 }, controller.signal);
    controller.abort();
    assert.equal(FakeXHR.last.aborted, true);
    const error = await rejection(result);
    assert.ok(error instanceof helpers.RequestError);
    assert.equal(error.code, 'stopped');
    assert.notEqual(error.message, OFFLINE);
  }),
);

test(
  'an already stopped upload sends nothing',
  withXHR(async () => {
    const controller = new AbortController();
    controller.abort();
    const error = await rejection(helpers.upload('/v1/upload', { size: 1 }, controller.signal));
    assert.equal(error.code, 'stopped');
    assert.equal(FakeXHR.last?.body, undefined);
  }),
);

test('a request that never answers ends at its deadline', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const original = globalThis.fetch;
  let signal;
  globalThis.fetch = (_path, options) =>
    new Promise((_resolve, reject) => {
      signal = options.signal;
      signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
  t.after(() => {
    globalThis.fetch = original;
  });
  const result = helpers.request('/v1/owner/journal', { method: 'GET' });
  t.mock.timers.tick(59999);
  assert.equal(signal.aborted, false, 'a slow answer gets its full time');
  t.mock.timers.tick(1);
  const error = await rejection(result);
  assert.ok(error instanceof helpers.RequestError);
  assert.equal(error.code, 'timeout');
});

test('an answered request clears its deadline', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const original = globalThis.fetch;
  let signal;
  globalThis.fetch = async (_path, options) => {
    signal = options.signal;
    return new Response(JSON.stringify({ ok: true, data: 'done' }), { status: 200 });
  };
  t.after(() => {
    globalThis.fetch = original;
  });
  assert.equal(await helpers.request('/v1/owner/journal', { method: 'GET' }), 'done');
  t.mock.timers.tick(60000);
  assert.equal(signal.aborted, false);
});

test(
  'network failures and timeouts are reported as offline',
  withXHR(async () => {
    for (const event of ['onerror', 'ontimeout']) {
      const result = helpers.upload('/v1/upload', { size: 1 });
      FakeXHR.last[event]();
      const error = await rejection(result);
      assert.equal(error.code, 'offline');
      assert.equal(error.message, OFFLINE);
    }
  }),
);

test(
  'replies are interpreted exactly as for other requests',
  withXHR(async () => {
    const cases = [
      [
        409,
        JSON.stringify({ ok: false, error: { message: 'Already uploaded.', code: 'conflict' } }),
        'Already uploaded.',
        'conflict',
      ],
      [200, JSON.stringify({ ok: false }), 'Inky could not complete this request.', undefined],
      [500, JSON.stringify({ ok: true, data: {} }), 'Inky could not complete this request.', undefined],
      [
        200,
        '<html>',
        'This address did not return an Inky response. Check the address and try again.',
        'invalid_response',
      ],
      [
        502,
        '',
        'Inky is not responding right now (HTTP 502). Check that it is running.',
        'invalid_response',
      ],
    ];
    for (const [status, body, message, code] of cases) {
      const result = helpers.upload('/v1/upload', { size: 1 });
      FakeXHR.last.reply(status, body);
      const error = await rejection(result);
      assert.ok(error instanceof helpers.RequestError);
      assert.equal(error.message, message);
      assert.equal(error.code, code);
    }
  }),
);
