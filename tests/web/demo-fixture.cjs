// Shared synthetic fixture server for the README captures and the demo film.
// It boots the checked-out real app on a loopback ephemeral port with a fresh
// configuration and only the public approved plates a caller asks for. No
// personal state, no provider credentials, generation stays off.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { mkdtempSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { createInterface } = require('node:readline');

const root = path.resolve(__dirname, '../..');
const python = process.env.INKY_QA_PYTHON || path.join(root, '.venv/bin/python');

const SERVER = `
import json, shutil, socket, sys, tomllib
from pathlib import Path
import uvicorn
from inky_bird_frame.config import load_config
from inky_bird_frame.settings import _toml, initialize_config
from inky_bird_frame.web import create_app

fixture, repo = map(Path, sys.argv[1:3])
wanted = set(json.loads(sys.argv[3]))
config_path = fixture / 'config.toml'
initialize_config(config_path)
catalog = fixture / 'catalog'
rows = json.loads((repo / 'catalog/index.json').read_text())['species']
birds = [row for row in rows if row['taxon_id'] in wanted]
assert len(birds) == len(wanted)
for bird in birds:
    folder = Path(bird['portrait_path']).parent
    shutil.copytree(repo / 'catalog' / folder, catalog / folder)
(catalog / 'index.json').write_text(json.dumps({
    'schema_version': 1, 'generated_at': birds[0]['approved_at'], 'species': birds,
}))
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    raw = tomllib.loads(config_path.read_text())
    raw['controller'].update(catalog_dir=str(catalog), port=port, bind_host='127.0.0.1')
    config_path.write_bytes(_toml(raw))
    config = load_config(config_path, load_secrets=False)
    print(port, flush=True)
    uvicorn.Server(uvicorn.Config(
        create_app(config.controller, app_config=config), access_log=False,
    )).run(sockets=[listener])
`;

// Starts the fixture app with the given approved public plates. The returned
// origin() resolves once the server has printed its ephemeral loopback port.
function startFixture(taxonIds) {
  assert(Array.isArray(taxonIds) && taxonIds.length > 0);
  const fixture = mkdtempSync(path.join(tmpdir(), 'inky-demo-fixture-'));
  const server = spawn(python, ['-I', '-c', SERVER, fixture, root, JSON.stringify(taxonIds)], {
    cwd: fixture,
    env: { HOME: fixture, PATH: process.env.PATH },
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  async function origin() {
    const lines = createInterface({ input: server.stdout });
    const { value: port } = await lines[Symbol.asyncIterator]().next();
    assert.match(port || '', /^\d+$/);
    return `http://127.0.0.1:${port}`;
  }
  return { server, fixture, origin };
}

module.exports = { startFixture, root, python };
