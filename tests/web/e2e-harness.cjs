// Shared synthetic installation, browser and helpers for the journal browser
// journeys. Requiring this file prepares the fixture and starts the server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const playwright = require(process.env.INKY_QA_PLAYWRIGHT || 'playwright');
const engine = process.env.INKY_QA_BROWSER || 'chromium';
assert(['chromium', 'firefox', 'webkit'].includes(engine));
const root = path.resolve(__dirname, '../..');
const python = process.env.INKY_QA_PYTHON || path.join(root, '.venv/bin/python');
const username = 'synthetic-owner';
const password = require('node:crypto').randomBytes(32).toString('base64url');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'inky-history-browser-'));
const configPath = path.join(fixture, 'config.toml');
// The server binds its own free port and reports it here, so parallel runs
// never collide and readiness can only ever reach this run's server.
const portFile = path.join(fixture, 'server.port');
// The server appends one line per 5xx answer, missing app asset (404 under
// /app) or unhandled exception, written before the answer is sent, so every
// request path is covered however it was routed or made (page, fetch or API
// request).
const serverErrorFile = path.join(fixture, 'server-errors.jsonl');
const readinessDeadlineMs = 120000;
const publicCatalog = JSON.parse(fs.readFileSync(root + '/catalog/index.json')).species;
const cardinal = publicCatalog.find((bird) => bird.common_name === 'Northern Cardinal');
const robin = publicCatalog.find((bird) => bird.common_name === 'American Robin');
// Owner plate URLs carry their revision, so a route must match the query too.
const plateRoute = /\/v1\/owner\/journal\/birds\/\d+\/portrait(\?|$)/;
// Explicit new-install initialization must precede any synthetic catalog/history.
execFileSync(python, ['-c', `from pathlib import Path\nfrom inky_bird_frame.settings import initialize_config\ninitialize_config(Path(${JSON.stringify(configPath)}))`], {cwd:root});
fs.mkdirSync(path.join(fixture, 'catalog/species'), { recursive: true });
fs.cpSync(
  path.join(root, 'catalog', path.dirname(cardinal.portrait_path)),
  path.join(fixture, 'catalog', path.dirname(cardinal.portrait_path)),
  { recursive: true },
);
fs.writeFileSync(
  path.join(fixture, 'catalog/index.json'),
  JSON.stringify({ schema_version: 1, generated_at: cardinal.approved_at, species: [cardinal] }),
);
execFileSync(
  python,
  [
    '-c',
    `import csv,json,sys,tomllib
from pathlib import Path
from inky_bird_frame.settings import initialize_config,_toml
sys.path.insert(0,'tests')
from ebird_fixtures import EXPORT_COLUMNS
root=Path(${JSON.stringify(fixture)})
p=root/'config.toml'
raw=tomllib.loads(p.read_text());raw['controller'].update(catalog_dir=str(root/'catalog'),bind_host='127.0.0.1');p.write_bytes(_toml(raw))
birds=json.loads(${JSON.stringify(JSON.stringify([cardinal, robin]))})
with (root/'archive.csv').open('w',newline='') as handle:
 writer=csv.DictWriter(handle,fieldnames=EXPORT_COLUMNS,restval='');writer.writeheader()
 for n,bird in enumerate(birds):
  writer.writerow({'Submission ID':f'S-SYNTHETIC-{n}','Date':['2001-03-01','2001-03-02'][n],'Location':f'Fixture garden {n}','Scientific Name':bird['scientific_name'],'Common Name':bird['common_name']})
state=root/'var/controller';state.mkdir(parents=True,exist_ok=True)
cache={'schema_version':1,'entries':{bird['scientific_name']:{'taxon_id':bird['taxon_id'],'common_name':bird['common_name'],'scientific_name':bird['scientific_name']} for bird in birds}}
(state/'ebird-archive-taxonomy-crosswalk.json').write_text(json.dumps(cache))
from PIL import Image,ImageDraw
from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import write_candidate_manifest
from inky_bird_frame.models import SpeciesProfileData,SourceLink,QualityReview
links=[SourceLink(title='Fixture source one',url='https://first.example/bird'),SourceLink(title='Fixture source two',url='https://second.example/bird')]
for identifier in [9999991,9999992]:
 bird=BirdSpecies(identifier,'Synthetic QA candidate '+str(identifier),'Fixture candidate '+str(identifier),1,'fixture')
 folder=state/'pending'/f'{identifier}-synthetic-qa-candidate-{identifier}';folder.mkdir(parents=True)
 for name,size in [('portrait.png',(1200,1600)),('display.png',(1600,1200))]:
  image=Image.new('RGB',size,'white');ImageDraw.Draw(image).text((80,100),'SYNTHETIC QA CANDIDATE',fill='black',font_size=50);image.save(folder/name)
 profile=SpeciesProfileData(taxon_id=identifier,common_name=bird.common_name,scientific_name=bird.scientific_name,family='Fixture family',measurements={'length':'10 cm','wingspan':'20 cm','weight':'30 g'},field_marks=['One','Two','Three','Four'],habitat='Fixture habitat',behavior='Fixture behavior',palette=['white','gray','black'],sources=links)
 write_candidate_manifest(folder,bird,profile,[],QualityReview(True,5,5,5,5,True,(),tuple(links)),generator='fixture',prompt_version='fixture')
held=state/'pending'/'9999993-synthetic-recovery-hold';held.mkdir(parents=True)
image=Image.new('RGB',(1200,1600),'white');ImageDraw.Draw(image).text((80,100),'SYNTHETIC RECOVERY HOLD',fill='black',font_size=50);image.save(held/'portrait.png')
`,
  ],
  { cwd: root },
);
const log = fs.openSync(path.join(fixture, 'server.log'), 'a', 0o600);
const server = spawn(
  python,
  [
    '-c',
    `import json,os,socket,tomllib
from pathlib import Path
import uvicorn
from inky_bird_frame.config import load_config
from inky_bird_frame.settings import _toml
listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
listener.bind(('127.0.0.1',0));listener.listen(64)
port=listener.getsockname()[1]
p=Path(${JSON.stringify(configPath)})
raw=tomllib.loads(p.read_text());raw['controller']['port']=port;p.write_bytes(_toml(raw))
from inky_bird_frame.web import create_app
import inky_bird_frame.community as community
# Only this synthetic QA server replaces the remote community query.
community.get_json=lambda *args,**kwargs: {'total_results':1,'results':[{'count':4,'taxon':{'id':${cardinal.taxon_id},'rank':'species','preferred_common_name':'Northern Cardinal','name':'Cardinalis cardinalis'}}]}
config=load_config(p,load_secrets=False)
served=create_app(config.controller,app_config=config)
problems=Path(${JSON.stringify(serverErrorFile)})
def record(entry):
 with problems.open('a') as handle: handle.write(json.dumps(entry)+'\\n')
async def app(scope,receive,send):
 async def checked(message):
  status=message.get('status',0) if message['type']=='http.response.start' else 0
  if status>=500 or (status==404 and (scope.get('path','')+'/').startswith('/app/')):
   record({'response':status,'path':scope.get('path')})
  await send(message)
 try: await served(scope,receive,checked)
 except Exception as error:
  record({'exception':type(error).__name__,'path':scope.get('path')});raise
ready=Path(${JSON.stringify(portFile)});pending=ready.with_suffix('.pending')
pending.write_text(str(port));os.replace(pending,ready)
uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False)).run(sockets=[listener])
`,
  ],
  {
    cwd: root,
    stdio: ['ignore', log, log],
    env: { PATH: process.env.PATH, PYTHONPATH: root + '/src' },
  },
);

let serverExit = null;
server.on('exit', (code, signal) => {
  serverExit = { code, signal };
});
// A crash inside the browser driver skips the cleanup below; never leave the server running.
process.on('exit', () => {
  if (!serverExit) server.kill('SIGTERM');
});

// Fails unless this run's own server answers before the deadline.
async function serverOrigin() {
  const deadline = Date.now() + readinessDeadlineMs;
  let origin = null;
  for (;;) {
    if (serverExit) throw new Error('Synthetic server exited before it was ready');
    if (Date.now() > deadline) throw new Error('Synthetic server was not ready in time');
    if (!origin && fs.existsSync(portFile)) {
      const port = Number(fs.readFileSync(portFile, 'utf8'));
      assert(Number.isInteger(port) && port > 0 && port < 65536);
      origin = 'http://127.0.0.1:' + port;
    }
    if (origin) {
      try {
        const response = await fetch(origin + '/v1/access', { signal: AbortSignal.timeout(5000) });
        if (response.ok) return origin;
      } catch {}
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

let serverErrorsRead = 0;
let serverExitReported = false;
// Moves new server-side problems into errors under the current phase.
function collectServerErrors(t) {
  if (fs.existsSync(serverErrorFile)) {
    const lines = fs.readFileSync(serverErrorFile, 'utf8').split('\n').filter(Boolean);
    for (const line of lines.slice(serverErrorsRead))
      t.errors.push({ phase: t.phase, server: JSON.parse(line) });
    serverErrorsRead = lines.length;
  }
  if (serverExit && !serverExitReported) {
    serverExitReported = true;
    t.errors.push({ phase: t.phase, serverExit });
  }
}

function run(body) {
  const t = { phase: 'start', engine, root, fixture, publicCatalog, cardinal, robin, plateRoute, username, password };
  let browser, page;
  (async () => {
    t.phase = 'server-readiness';
    const origin = await serverOrigin();
    t.origin = origin;
    t.phase = 'browser-launch';
    browser = await playwright[engine].launch({
      headless: true,
      ...(process.env.INKY_QA_BROWSER_EXECUTABLE
        ? { executablePath: process.env.INKY_QA_BROWSER_EXECUTABLE }
        : {}),
      ...(engine === 'chromium'
        ? { args: ['--disable-background-networking', '--use-mock-keychain'] }
        : {}),
    });
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: engine !== 'firefox',
      hasTouch: engine !== 'firefox',
      reducedMotion: 'reduce',
    });
    await context.route('**/*', (route) =>
      new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
    );
    page = await context.newPage();
    await page.clock.install();
    const errors = (t.errors = []);
    page.on('pageerror', (error) => errors.push({ phase: t.phase, message: error.message }));
    // The server records the resource failures that matter (5xx and missing
    // app assets); other 4xx answers are expected by the journeys.
    context.on('console', (message) => {
      if (message.type() === 'error' && !message.text().startsWith('Failed to load resource'))
        errors.push({ phase: t.phase, console: message.text() });
    });
    // Fulfilled routes never reach the server, so they are not reported.
    context.on('response', (response) => {
      if (response.status() >= 500 || response.status() === 404) collectServerErrors(t);
    });
    async function heading(name) {
      await page.getByRole('heading', { name, exact: true }).waitFor();
    }
    async function step(name) {
      for (let n = 0; n < 30; n++) {
        if (await page.getByRole('heading', { name, exact: true }).isVisible()) return;
        await page.clock.runFor(1100);
        await page.waitForTimeout(20);
      }
      await heading(name);
    }
    const state = () =>
      JSON.parse(fs.readFileSync(path.join(fixture, 'var/controller/journal.json')));
    Object.assign(t, { browser, context, page, errors, heading, step, state });
    await body(t);
    // Catches what no browser response showed, such as an API request's 5xx.
    t.phase = 'server-errors';
    collectServerErrors(t);
    assert.deepEqual(errors, [], 'the real server answered every request without an error');
  })()
    .catch(async (error) => {
      if (page)
        await page
          .screenshot({ path: fixture + '/history-browser-failure.png', fullPage: true })
          .catch(() => {});
      fs.writeFileSync(
        fixture + '/failure.json',
        JSON.stringify(
          {
            engine,
            phase: t.phase,
            type: error.name,
            actual: Array.isArray(error.actual) ? error.actual : undefined,
          },
          null,
          2,
        ),
        { mode: 0o600 },
      );
      console.error('History browser failed at ' + t.phase + ': ' + error.name);
      // Recorded problems are synthetic and often happened in an earlier phase.
      for (const entry of Array.isArray(error.actual) ? error.actual : [])
        if (entry && typeof entry === 'object' && 'phase' in entry)
          console.error(
            '  recorded in ' + entry.phase + ': ' + JSON.stringify({ ...entry, phase: undefined }),
          );
      console.error(
        error.stack
          ?.split('\n')
          .filter((line) => line.trimStart().startsWith('at '))
          .join('\n'),
      );
      console.error('Synthetic failure evidence: ' + fixture);
      process.exitCode = 1;
    })
    .finally(async () => {
      if (browser) await browser.close();
      server.kill('SIGTERM');
      fs.closeSync(log);
    });
}

module.exports = { run };
