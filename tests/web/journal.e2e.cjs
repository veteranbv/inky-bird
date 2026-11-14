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
const origin = 'http://127.0.0.1:18796',
  configPath = path.join(fixture, 'config.toml');
const publicCatalog = JSON.parse(fs.readFileSync(root + '/catalog/index.json')).species;
const cardinal = publicCatalog.find((bird) => bird.common_name === 'Northern Cardinal');
const robin = publicCatalog.find((bird) => bird.common_name === 'American Robin');
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
    `import csv,json,tomllib
from pathlib import Path
from inky_bird_frame.settings import initialize_config,_toml
from inky_bird_frame.ebird_archive import REQUIRED_COLUMNS
root=Path(${JSON.stringify(fixture)})
p=root/'config.toml'
raw=tomllib.loads(p.read_text());raw['controller'].update(catalog_dir=str(root/'catalog'),port=18796,bind_host='127.0.0.1');p.write_bytes(_toml(raw))
birds=json.loads(${JSON.stringify(JSON.stringify([cardinal, robin]))})
with (root/'archive.csv').open('w',newline='') as handle:
 writer=csv.DictWriter(handle,fieldnames=REQUIRED_COLUMNS);writer.writeheader()
 for n,bird in enumerate(birds):
  row=dict.fromkeys(REQUIRED_COLUMNS,'');row.update({'Submission ID':f'S-SYNTHETIC-{n}','Date':['2001-03-01','2001-03-02'][n],'Location':f'Fixture garden {n}','Scientific Name':bird['scientific_name'],'Common Name':bird['common_name']});writer.writerow(row)
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
    `from pathlib import Path
import uvicorn
from inky_bird_frame.config import load_config
from inky_bird_frame.web import create_app
import inky_bird_frame.community as community
# Only this synthetic QA server replaces the remote community query.
community.get_json=lambda *args,**kwargs: {'total_results':1,'results':[{'count':4,'taxon':{'id':${cardinal.taxon_id},'rank':'species','preferred_common_name':'Northern Cardinal','name':'Cardinalis cardinalis'}}]}
config=load_config(Path(${JSON.stringify(configPath)}),load_secrets=False)
uvicorn.run(create_app(config.controller,app_config=config),host='127.0.0.1',port=18796,access_log=False)
`,
  ],
  {
    cwd: root,
    stdio: ['ignore', log, log],
    env: { PATH: process.env.PATH, PYTHONPATH: root + '/src' },
  },
);
let browser,
  page,
  phase = 'start';
(async () => {
  for (let n = 0; n < 50; n++) {
    try {
      if ((await fetch(origin + '/v1/access')).ok) break;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
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
  const errors = [];
  page.on('pageerror', (error) => errors.push({ phase, message: error.message }));
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
  phase = 'reserved-setup-rendering';
  const reservedStatus = async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    assert.equal(payload.data.setup_required, true);
    payload.data.setup_code = { state: 'ready', expires_in_seconds: 60 };
    await route.fulfill({ response, json: payload });
  };
  await context.route(origin + '/v1/access', reservedStatus);
  await page.goto(origin + '/app');
  await heading('Finish setup on the controller');
  assert.equal(await page.locator('input').count(), 0);
  await page.getByText('If you use Docker', {exact: true}).click();
  await page.getByText('docker compose exec controller inky-bird-frame access setup --config /data/config.toml', {exact: true}).waitFor();
  const guide = await context.request.get(origin + '/app/access-guide');
  assert.equal(guide.status(), 200);
  assert((await guide.text()).includes('An unattended upgrade does not wait for a prompt.'));
  await page.setViewportSize({width: 320, height: 844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({width: 390, height: 844});
  await context.unroute(origin + '/v1/access', reservedStatus);
  phase = 'registration';
  await page.reload();
  await heading('Make Inky yours');
  const cookieTrustNotice =
    'Use this address only with web apps you trust. A different port does not keep sign-in cookies separate.';
  await page.getByText(cookieTrustNotice, { exact: true }).waitFor();
  // Check the HTTPS rendering branch only; real TLS is qualified separately.
  const encryptedStatus = async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.data.encrypted = true;
    await route.fulfill({ response, json: payload });
  };
  await context.route(origin + '/v1/access', encryptedStatus);
  await page.reload();
  await page.getByText(/Encrypted connection/).waitFor();
  await page.getByText(cookieTrustNotice, { exact: true }).waitFor();
  await context.unroute(origin + '/v1/access', encryptedStatus);
  await page.reload();
  await heading('Make Inky yours');
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  fs.writeFileSync(fixture + '/sign-in-phone.png', await page.screenshot({ fullPage: true }), {
    mode: 0o600,
  });
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Choose your Inky password', { exact: true }).fill(password);
  await page.getByLabel('Confirm your password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await heading('Your field journal');
  await page
    .getByText('No sources selected for refresh. Artwork and saved collections still work.', {
      exact: true,
    })
    .waitFor();
  assert.equal(await page.getByText(/The last saved selection may be out of date/).count(), 0);
  await page.getByRole('link', { name: '2 plates ready for your review', exact: true }).waitFor();
  await page.getByRole('link', { name: '1 plate held for recovery', exact: true }).waitFor();
  assert.equal(await page.getByText(/3 plates ready for your review/).count(), 0);
  phase = 'birdbuddy-bootstrap-ux';
  await page.goto(origin + '/app?page=settings&source=birdbuddy');
  await heading('Bird Buddy');
  assert(await page.getByLabel('Dedicated guest email', { exact: true }).isDisabled());
  const permission = page.getByRole('checkbox', {
    name: 'I confirm that Bird Buddy has authorized automated API access for this account.',
    exact: true,
  });
  await permission.check();
  await page
    .getByLabel('Dedicated guest email', { exact: true })
    .fill('synthetic-guest@example.test');
  await page
    .getByLabel('Guest password (used only for this sign-in)', { exact: true })
    .fill('SYNTHETIC_GUEST_NOT_SENT');
  await permission.uncheck();
  assert.equal(await page.getByLabel('Dedicated guest email', { exact: true }).inputValue(), '');
  assert.equal(
    await page
      .getByLabel('Guest password (used only for this sign-in)', { exact: true })
      .inputValue(),
    '',
  );
  phase = 'favorites';
  await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
  await heading('Northern Cardinal');
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Add to Favorites', exact: true }).click(),
  ]);
  await heading('Northern Cardinal');
  assert.equal(state().collections[0].kind, 'favorites');
  phase = 'upload';
  await page.goto(origin + '/app?page=imports&kind=ebird-archive');
  await heading('Your eBird history');
  await page
    .getByLabel('History file', { exact: true })
    .setInputFiles(path.join(fixture, 'archive.csv'));
  await page.getByRole('button', { name: 'Upload for checking', exact: true }).click();
  await step('2. Check the file');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/ebird-archive-observations.json')));
  phase = 'preview';
  await page.getByRole('button', { name: 'Check this file', exact: true }).click();
  await step('3. Review history changes');
  await page.reload();
  await heading('Your eBird history');
  await heading('3. Review history changes');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/ebird-archive-observations.json')));
  phase = 'history-confirmation';
  await page.getByRole('button', { name: 'Confirm history update', exact: true }).click();
  await step('4. Choose your checklists');
  assert.equal(state().collections.length, 1);
  await page.getByLabel('Trip start date', { exact: true }).fill('2001-03-01');
  await page.getByLabel('Trip end date', { exact: true }).fill('2001-03-02');
  await page.getByLabel('Trip name', { exact: true }).fill('A synthetic spring trip');
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('checkbox', { name: /2001-03-01/ }).check();
  assert.equal(await page.getByRole('checkbox').count(), 2);
  phase = 'trip-preview';
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  await page.reload();
  await heading('Your eBird history');
  await heading('5. Save your trip');
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: fixture + '/trip-review-phone.png', fullPage: true });
  phase = 'stale-trip-and-recovery';
  const session = (await (await context.request.get(origin + '/v1/owner/session')).json()).data;
  const journal = (await (await context.request.get(origin + '/v1/owner/journal')).json()).data;
  const edited = await context.request.post(origin + '/v1/owner/journal/collections/favorite', {
    headers: { Origin: origin, 'X-Inky-CSRF': session.csrf },
    data: { revision: journal.revision, taxon_id: cardinal.taxon_id, favorite: false },
  });
  assert.equal(edited.status(), 200);
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('This step did not finish');
  await page.getByRole('button', { name: 'Check saved state', exact: true }).click();
  await step('5. Save your trip');
  await page.getByRole('button', { name: 'Edit trip selection', exact: true }).click();
  await step('4. Choose your checklists');
  assert.equal(
    await page.getByLabel('Trip name', { exact: true }).inputValue(),
    'A synthetic spring trip',
  );
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('button', { name: 'Review this trip', exact: true }).waitFor();
  assert(await page.getByRole('checkbox', { name: /2001-03-01/ }).isChecked());
  assert(!(await page.getByRole('checkbox', { name: /2001-03-02/ }).isChecked()));
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  phase = 'trip-save';
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('A synthetic spring trip');
  const trip = state().collections.find((group) => group.kind === 'personal-trip');
  assert.equal(trip.members.length, 1);
  assert.equal(trip.members[0].taxon_id, cardinal.taxon_id);
  assert(!fs.existsSync(path.join(fixture, 'var/controller/owner-import/upload.bin')));
  phase = 'portable-export';
  page.once('dialog', (dialog) => dialog.accept());
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export collection', exact: true }).click();
  const download = await downloadPromise;
  const exported = fs.readFileSync(await download.path(), 'utf8');
  assert(!exported.includes('checklist_hash'));
  assert(!exported.includes('Fixture garden'));
  const portablePath = path.join(fixture, 'portable.json');
  fs.writeFileSync(portablePath, exported, { mode: 0o600 });
  phase = 'portable-import-response-loss';
  await page.goto(origin + '/app?page=import-collection');
  await heading('Import a collection');
  await page.getByLabel('Inky collection file', { exact: true }).setInputFiles(portablePath);
  await page.getByRole('button', { name: 'Import this selection', exact: true }).waitFor();
  let interrupted = false;
  await context.route('**/v1/owner/journal/collections/import', async (route) => {
    if (!interrupted) {
      interrupted = true;
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page.getByRole('button', { name: 'Import this selection', exact: true }).click();
  await page.getByText(/You can retry this same save safely/).waitFor();
  const after = state().collections.length;
  await page.getByRole('button', { name: 'Import this selection', exact: true }).click();
  await step('A synthetic spring trip');
  assert.equal(state().collections.length, after);
  assert.equal(
    state().collections.filter((group) => group.kind === 'imported-selection').length,
    1,
  );
  const imported = state().collections.find((group) => group.kind === 'imported-selection');
  await page.waitForURL((url) => url.searchParams.get('view') === imported.collection_id);
  phase = 'retained-history';
  await page.goto(origin + '/app?page=imports&kind=ebird-archive');
  await heading('Your eBird history');
  await page
    .getByRole('button', { name: 'Use retained history without an upload', exact: true })
    .click();
  await step('4. Choose your checklists');
  await page.getByLabel('Trip name', { exact: true }).fill('Missing Robin fixture');
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('checkbox', { name: /2001-03-02/ }).check();
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('Missing Robin fixture');
  phase = 'explicit-missing-plate-queue';
  await page.getByRole('button', { name: 'Review missing-plate requests', exact: true }).click();
  await heading('Queue missing plates?');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/generation-queue.json')));
  await page.getByRole('button', { name: 'Confirm these plate requests', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
  const queue = JSON.parse(
    fs.readFileSync(path.join(fixture, 'var/controller/generation-queue.json')),
  ).species;
  assert.equal(queue.length, 1);
  assert.equal(queue[0].taxon_id, robin.taxon_id);
  assert.deepEqual(queue[0].sources, ['owner-selection']);
  phase = 'candidate-inspection-and-approval';
  await page.goto(origin + '/app?page=work');
  await heading('Plate work');
  await page
    .getByRole('link', { name: 'Inspect Synthetic QA candidate 9999991', exact: true })
    .click();
  await heading('Review candidate');
  await page.locator('.detail-art').evaluate((image) => image.decode());
  await page
    .getByRole('checkbox', {
      name: 'I inspected the full plate, its text, anatomy and cited review. Make this artwork approved locally and eligible for existing publication.',
      exact: true,
    })
    .check();
  await page.getByRole('button', { name: 'Confirm local approval', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
  assert(
    fs.existsSync(
      path.join(fixture, 'catalog/species/9999991-synthetic-qa-candidate-9999991/manifest.json'),
    ),
  );
  phase = 'candidate-rejection';
  await page.goto(origin + '/app?page=candidate&bird=9999992');
  await heading('Review candidate');
  await page
    .getByLabel('Why reject this candidate?', { exact: true })
    .fill('Synthetic QA rejection, retain the fixture privately.');
  await page.getByRole('button', { name: 'Confirm rejection', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
  assert(
    fs.existsSync(
      path.join(
        fixture,
        'var/controller/rejected/9999992-synthetic-qa-candidate-9999992/manifest.json',
      ),
    ),
  );
  phase = 'recovery-hold-banner';
  await page.goto(origin + '/app');
  await heading('Your field journal');
  await page.getByRole('link', { name: '1 plate held for recovery', exact: true }).waitFor();
  assert.equal(await page.getByText(/ready for your review/).count(), 0);
  await page.getByRole('link', { name: '1 plate held for recovery', exact: true }).click();
  await heading('Plate work');
  await page
    .getByText('Taxon 9999993: incomplete or unsafe pending state. Use operator recovery before retrying.', { exact: true })
    .waitFor();
  assert.equal(await page.getByRole('link', { name: /Inspect .*9999993/ }).count(), 0);
  phase = 'collection-filter';
  await page.goto(origin + '/app?page=edit-collection');
  await heading('New collection');
  await page.getByLabel('Collection name', { exact: true }).fill('Filter journey');
  await page.getByRole('checkbox', { name: /Northern Cardinal/ }).check();
  // Filtering hides non-matching rows (role queries exclude hidden nodes)
  // while chosen birds stay chosen. Scientific names match too.
  await page.getByLabel('Find a bird to add', { exact: true }).fill('Turdus');
  await page.getByRole('checkbox', { name: /American Robin/ }).waitFor();
  assert.equal(await page.getByRole('checkbox', { name: /Northern Cardinal/ }).count(), 0);
  await page.getByRole('checkbox', { name: /American Robin/ }).check();
  await page.getByLabel('Find a bird to add', { exact: true }).fill('zz-no-such-bird');
  await page.getByText('0 species shown · chosen birds stay chosen', { exact: true }).waitFor();
  assert.equal(await page.getByRole('checkbox', { name: /American Robin/ }).count(), 0);
  await page.getByLabel('Find a bird to add', { exact: true }).fill('');
  assert(await page.getByRole('checkbox', { name: /Northern Cardinal/ }).isChecked());
  assert(await page.getByRole('checkbox', { name: /American Robin/ }).isChecked());
  // Leaving without saving commits nothing.
  await page.goto(origin + '/app?page=collections');
  await heading('Collections');
  assert.equal(await page.getByRole('link', { name: 'Filter journey', exact: true }).count(), 0);
  await page.getByText('1 plate ready · 0 not ready', { exact: true }).first().waitFor();
  assert.equal(await page.getByText(/1 plates ready/).count(), 0);
  await page.goto(origin + '/app?page=edit-collection');
  await heading('New collection');
  await page.getByLabel('Collection name', { exact: true }).fill('Filter journey');
  await page.getByRole('checkbox', { name: /Northern Cardinal/ }).check();
  await page.getByLabel('Find a bird to add', { exact: true }).fill('American Robin');
  await page.getByRole('checkbox', { name: /American Robin/ }).check();
  await page.getByRole('button', { name: 'Save collection', exact: true }).click();
  await heading('Collections');
  await page.getByRole('link', { name: 'Filter journey', exact: true }).click();
  await heading('Filter journey');
  await page.getByRole('link', { name: /Northern Cardinal/ }).first().waitFor();
  await page.getByRole('link', { name: /American Robin/ }).first().waitFor();
  // Remove the journey collection so later phases see the expected state.
  // The Remove action lives in the collection page's tools.
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Remove collection', exact: true }).click();
  await heading('Collections');
  assert.equal(await page.getByRole('link', { name: 'Filter journey', exact: true }).count(), 0);
  phase = 'pair-readonly-screen';
  // Frame controls fade after five idle seconds and take no taps while faded,
  // so every Frame step reveals them first. Narrow screens keep secondary
  // controls behind More; wide screens have no More button.
  const openMore = async (target) => {
    await target.keyboard.press('Shift');
    await target.locator('.frame-controls:not(.quiet)').waitFor();
    const more = target.getByRole('button', { name: 'More', exact: true });
    if ((await more.isVisible()) && (await more.getAttribute('aria-expanded')) !== 'true')
      await more.click();
  };
  // The browser enters fullscreen some time after the click, so wait for the
  // request to settle before deciding; a late entry would otherwise leave the
  // window fullscreen for every later phase.
  const fullscreenSettles = async (target, entered) => {
    for (let n = 0; n < 40; n++) {
      if ((await target.evaluate(() => Boolean(document.fullscreenElement))) === entered)
        return true;
      await target.waitForTimeout(50);
    }
    return false;
  };
  // Zoom must enlarge the drawn plate, not just widen its box; Fit restores
  // the whole plate.
  const plateScale = (target) =>
    target.locator('.frame-art').evaluate((image) => {
      const box = image.getBoundingClientRect();
      return Math.min(box.width / image.naturalWidth, box.height / image.naturalHeight);
    });
  const zoomEnlarges = async (target) => {
    await openMore(target);
    const fit = await plateScale(target);
    await target.getByRole('button', { name: 'Zoom', exact: true }).click();
    const zoomed = await plateScale(target);
    assert(zoomed > fit * 1.5, 'Zoom enlarges the plate');
    await target.getByRole('button', { name: 'Fit whole plate', exact: true }).click();
    assert(Math.abs((await plateScale(target)) - fit) < 0.001);
  };
  // With More closed, the control bar sits fully below the bird name.
  const captionClear = async (target) => {
    await target.keyboard.press('Shift');
    await target.locator('.frame-controls:not(.quiet)').waitFor();
    assert.equal(await target.locator('.frame-controls.expanded').count(), 0);
    const caption = await target.locator('.frame-caption').boundingBox();
    const bar = await target.locator('.frame-controls').boundingBox();
    // Half-pixel tolerance for subpixel layout.
    assert(caption && bar && caption.y + caption.height <= bar.y + 0.5, 'the bar covers the bird name');
  };
  await page.goto(origin + '/app?page=displays');
  await heading('Displays');
  const screenContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: engine !== 'firefox',
    hasTouch: engine !== 'firefox',
    reducedMotion: 'reduce',
  });
  await screenContext.route('**/*', (route) =>
    new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
  );
  const screen = await screenContext.newPage();
  await screen.clock.install();
  screen.on('pageerror', (error) => errors.push({ phase, message: error.message }));
  await screen.goto(origin + '/screen');
  await screen.getByRole('button', { name: 'Pair this screen', exact: true }).click();
  await screen.getByRole('heading', { name: 'Pair this screen', exact: true }).waitFor();
  const pairCode = await screen.locator('.pairing-code').innerText();
  // A failed Cancel keeps the code, and its message survives the next
  // approval check instead of being replaced within seconds.
  await screenContext.route('**/v1/screen/forget', (route) => route.abort());
  await screen.getByRole('button', { name: 'Cancel pairing', exact: true }).click();
  const cancelFailure = screen.getByText(/Pairing was not cancelled\./);
  await cancelFailure.waitFor();
  const approvalCheck = screen.waitForResponse((response) =>
    response.url().endsWith('/v1/screen/view'),
  );
  await screen.clock.runFor(15100);
  await approvalCheck;
  await screen.getByText(/Waiting for owner approval\./).waitFor();
  assert(await cancelFailure.isVisible(), 'the Cancel failure stays visible');
  assert.equal(await screen.locator('.pairing-code').innerText(), pairCode);
  await screenContext.unroute('**/v1/screen/forget');
  await page.reload();
  await page.getByRole('heading', { name: 'Pairing request · ' + pairCode, exact: true }).waitFor();
  await page.getByLabel('Screen name', { exact: true }).fill('Synthetic tablet');
  await page.getByLabel('Permitted view', { exact: true }).first().selectOption(trip.collection_id);
  await page
    .getByLabel('I matched this code on the screen I want to pair', { exact: true })
    .check();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Pair this screen', exact: true }).click(),
  ]);
  await screen.clock.runFor(15100);
  await screen.locator('.frame-art').waitFor();
  await screen.locator('.frame-art').evaluate((image) => image.decode());
  await captionClear(screen);
  await zoomEnlarges(screen);
  await screen.keyboard.press('Escape');
  await screen.setViewportSize({ width: 844, height: 390 });
  await captionClear(screen);
  await screen.setViewportSize({ width: 390, height: 844 });
  assert.equal((await screenContext.request.get(origin + '/v1/owner/journal')).status(), 401);
  const screenView = (await (await screenContext.request.get(origin + '/v1/screen/view')).json())
    .data;
  assert.equal(screenView.species.length, 1);
  assert.equal(screenView.species[0].taxon_id, cardinal.taxon_id);
  phase = 'authoritative-screen-scope-removal';
  const replacement = publicCatalog.find(
    (bird) => bird.taxon_id !== cardinal.taxon_id && bird.taxon_id !== robin.taxon_id,
  );
  fs.cpSync(
    path.join(root, 'catalog', path.dirname(replacement.portrait_path)),
    path.join(fixture, 'catalog', path.dirname(replacement.portrait_path)),
    { recursive: true },
  );
  const catalogFile = path.join(fixture, 'catalog/index.json'),
    expanded = JSON.parse(fs.readFileSync(catalogFile));
  expanded.species.push(replacement);
  fs.writeFileSync(catalogFile, JSON.stringify(expanded));
  const ownerSession = (await (await context.request.get(origin + '/v1/owner/session')).json())
    .data;
  let currentJournal = (await (await context.request.get(origin + '/v1/owner/journal')).json())
    .data;
  // Where the engine honors a trusted fullscreen request, the paired screen
  // runs the following state changes as an ambient display, and each notice
  // must land inside the live window rather than below the full-height plate.
  let screenAmbient = false;
  await openMore(screen);
  const screenFullscreen = screen.getByRole('button', { name: 'Fullscreen', exact: true });
  if ((await screenFullscreen.count()) === 1) {
    await screenFullscreen.click();
    await fullscreenSettles(screen, true);
    screenAmbient = (await screen.evaluate(() => document.fullscreenElement?.tagName)) === 'HTML';
  }
  const screenInside = async (locator) => {
    const size = await screen.evaluate(() => ({ width: innerWidth, height: innerHeight }));
    const box = await locator.boundingBox();
    return (
      box && box.y >= 0 && box.x >= 0 && box.y + box.height <= size.height && box.x + box.width <= size.width
    );
  };
  await screenContext.route('**/v1/screen/plates/' + replacement.taxon_id + '?*', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: '{"ok":false}' }),
  );
  assert.equal(
    (
      await context.request.post(origin + '/v1/owner/journal/collections/save', {
        headers: { Origin: origin, 'X-Inky-CSRF': ownerSession.csrf },
        data: {
          revision: currentJournal.revision,
          collection_id: trip.collection_id,
          label: trip.label,
          taxon_ids: [replacement.taxon_id],
        },
      })
    ).status(),
    200,
  );
  await screen.clock.runFor(15100);
  await screen.getByText(/temporarily unavailable/).waitFor();
  if (screenAmbient) assert(await screenInside(screen.getByText(/temporarily unavailable/)));
  assert.equal(await screen.locator('img[src]').count(), 0);
  currentJournal = (await (await context.request.get(origin + '/v1/owner/journal')).json()).data;
  assert.equal(
    (
      await context.request.post(origin + '/v1/owner/journal/collections/remove', {
        headers: { Origin: origin, 'X-Inky-CSRF': ownerSession.csrf },
        data: { revision: currentJournal.revision, collection_id: trip.collection_id },
      })
    ).status(),
    200,
  );
  await screen.clock.runFor(15100);
  const emptyNotice = screen.getByText(
    'No approved plates in the permitted view. Inky will not switch views.',
    { exact: true },
  );
  await emptyNotice.waitFor();
  if (screenAmbient) assert(await screenInside(emptyNotice));
  assert.equal(await screen.locator('img[src]').count(), 0);
  // An emptied view leaves nothing to step through.
  await screen.keyboard.press('ArrowRight');
  assert.equal(await screen.locator('img[src]').count(), 0);
  phase = 'screen-forget-failure';
  // Forget asks first, and a failed request keeps the screen paired and
  // presenting instead of clearing it before the answer.
  await screenContext.route('**/v1/screen/forget', (route) => route.abort());
  await openMore(screen);
  await screen.getByRole('button', { name: 'Forget this screen', exact: true }).click();
  await screen.getByRole('button', { name: 'Forget', exact: true }).click();
  await screen.getByText(/This screen was not forgotten\./).waitFor();
  assert(await screen.evaluate(() => document.body.classList.contains('presenting')));
  await screen.getByRole('button', { name: 'Forget this screen', exact: true }).waitFor();
  await screenContext.unroute('**/v1/screen/forget');
  await page.getByRole('button', { name: 'Revoke screen', exact: true }).click();
  await screen.clock.runFor(15100);
  await screen.getByRole('heading', { name: 'Screen access ended', exact: true }).waitFor();
  assert.equal(await screen.locator('img').count(), 0);
  phase = 'scoped-integrations';
  await page.goto(origin + '/app?page=integrations');
  await heading('Integrations');
  await page
    .getByLabel('Permitted view', { exact: true })
    .first()
    .selectOption(imported.collection_id);
  await page
    .getByLabel(
      'I approve exposing this selected species membership on my trusted network or VPN',
      { exact: true },
    )
    .check();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Enable this art view', exact: true }).click(),
  ]);
  const artResponse = await context.request.get(origin + '/v1/dashboard/art');
  assert.equal(artResponse.status(), 200);
  const artText = await artResponse.text();
  assert(!artText.includes(trip.label));
  assert(!artText.includes('2001-03-01'));
  await page.getByRole('button', { name: 'Create status credential', exact: true }).click();
  await page.getByRole('heading', { name: 'Copy this credential now', exact: true }).waitFor();
  const serviceSecret = await page.locator('.notice pre').first().innerText();
  const aggregate = await context.request.get(origin + '/v1/integrations/status', {
    headers: { Authorization: 'Bearer ' + serviceSecret },
  });
  assert.equal(aggregate.status(), 200);
  assert(!JSON.stringify(await aggregate.json()).includes(trip.label));
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'I saved it · clear this copy', exact: true }).click(),
  ]);
  await heading('Integrations');
  assert.equal(await page.getByText(serviceSecret, { exact: true }).count(), 0);
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Revoke Dashboard status', exact: true }).click(),
  ]);
  await heading('Integrations');
  assert.equal(
    (
      await context.request.get(origin + '/v1/integrations/status', {
        headers: { Authorization: 'Bearer ' + serviceSecret },
      })
    ).status(),
    401,
  );
  await screenContext.close();
  for (const filename of ['collection.json', 'display-last-success.json'])
    assert(!fs.existsSync(path.join(fixture, 'var/controller', filename)));
  phase = 'community-wizard';
  await page.goto(origin + '/app?page=community');
  await heading('Birds reported near a place');
  await page.getByRole('button', { name: 'New community search', exact: true }).click();
  for (const [label, value] of [
    ['Collection name', 'Synthetic community selection'],
    ['Start date', '2001-03-01'],
    ['End date', '2001-03-02'],
    ['Latitude', '0.125'],
    ['Longitude', '0.25'],
    ['Radius in km', '10'],
  ])
    await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: 'Check community reports', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByRole('link', { name: 'Review community selection', exact: true }).click();
  await heading('Synthetic community selection');
  assert(!state().collections.some((group) => group.kind === 'community-history'));
  await page.getByRole('button', { name: 'Save community collection', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  const communityGroup = state().collections.find((group) => group.kind === 'community-history');
  assert.equal(communityGroup.members[0].origin, 'community-report');
  assert.deepEqual(communityGroup.members[0].evidence, []);
  phase = 'manual-community-member-label';
  const communitySession = (
    await (await context.request.get(origin + '/v1/owner/session')).json()
  ).data;
  const manualAddition = await context.request.post(
    origin + '/v1/owner/journal/collections/save',
    {
      headers: { Origin: origin, 'X-Inky-CSRF': communitySession.csrf },
      data: {
        revision: state().revision,
        collection_id: communityGroup.collection_id,
        label: communityGroup.label,
        taxon_ids: [cardinal.taxon_id, replacement.taxon_id],
      },
    },
  );
  assert.equal(manualAddition.status(), 200);
  await page.goto(origin + '/app?page=collection&view=' + communityGroup.collection_id);
  await heading('Synthetic community selection');
  assert.equal(
    await page.getByText('Community report · not your personal sighting', { exact: true }).count(),
    1,
  );
  assert.equal(
    await page.getByText('Manual selection · no community report linked', { exact: true }).count(),
    1,
  );
  phase = 'health-and-guides';
  await page.goto(origin + '/app?page=health');
  await heading('Health and help');
  await page.getByRole('heading', { name: 'Approved catalog index · ok', exact: true }).waitFor();
  for (const [area, title] of [
    ['backups', 'Backups and upgrades'],
    ['advanced', 'Advanced tools'],
  ]) {
    await page.goto(origin + '/app?page=' + area);
    await heading(title);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  phase = 'saved-birds-explicit-confirmation';
  await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
  await heading('Northern Cardinal');
  await page.getByRole('button', { name: 'Review adding to Saved birds', exact: true }).click();
  await heading('Change Saved birds?');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/collection.json')));
  await page.getByRole('button', { name: 'Confirm Saved birds change', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(fixture, 'var/controller/collection.json'))).taxa[0]
      .taxon_id,
    cardinal.taxon_id,
  );
  phase = 'owner-frame';
  await page.goto(origin + '/app?page=frame&view=' + imported.collection_id);
  await page.locator('.frame-art').waitFor();
  await page.locator('.frame-art').evaluate((image) => image.decode());
  await zoomEnlarges(page);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  phase = 'frame-ambient-labels';
  // Toggles keep their names and report state through aria-pressed.
  const labels = page.getByRole('button', { name: 'Bird name', exact: true });
  assert.equal(await labels.getAttribute('aria-pressed'), 'true');
  await page.locator('.frame-caption').waitFor();
  await labels.click();
  assert.equal(await labels.getAttribute('aria-pressed'), 'false');
  assert(!(await page.locator('.frame-caption').isVisible()));
  await labels.click();
  assert.equal(await labels.getAttribute('aria-pressed'), 'true');
  await page.locator('.frame-caption').waitFor();
  for (const name of ['Play', 'Shuffle'])
    assert.equal(
      await page.getByRole('button', { name, exact: true }).getAttribute('aria-pressed'),
      'false',
    );
  phase = 'frame-ambient-fade';
  // Idle fades the controls even though the clicked toggle may still hold
  // focus; keyboard activity brings them back.
  await page.clock.runFor(5100);
  await page.locator('.frame-controls.quiet').waitFor();
  assert.equal(
    await page.getByRole('button', { name: 'More', exact: true }).getAttribute('aria-expanded'),
    'false',
  );
  assert(await page.locator('.frame-controls').evaluate((bar) => getComputedStyle(bar).pointerEvents === 'none'));
  if (engine !== 'firefox') {
    // A tap on faded controls only wakes them: it must not also press Play.
    const playBox = await page.getByRole('button', { name: 'Play', exact: true }).boundingBox();
    await page.touchscreen.tap(playBox.x + playBox.width / 2, playBox.y + playBox.height / 2);
    await page.locator('.frame-controls:not(.quiet)').waitFor();
    assert.equal(
      await page.getByRole('button', { name: 'Play', exact: true }).getAttribute('aria-pressed'),
      'false',
    );
    await page.clock.runFor(5100);
    await page.locator('.frame-controls.quiet').waitFor();
  }
  await page.keyboard.press('Shift');
  await page.locator('.frame-controls:not(.quiet)').waitFor();
  // Ambient fullscreen, where the engine honors a trusted request: the plate
  // owns the viewport, the placard stays inside it, a cleared caption leaves
  // no empty box, and failure notices stay on screen rather than below it.
  phase = 'frame-ambient-fullscreen';
  // The all-artwork view always holds at least two ready plates here, so
  // Next must fetch a new portrait and the aborted request must surface its
  // notice. The Fullscreen button renders only where the API exists, and an
  // engine may still refuse the request; both gates skip without weakening
  // the journeys that do enter real fullscreen.
  await page.goto(origin + '/app?page=frame&view=all');
  await page.locator('.frame-art').waitFor();
  await page.locator('.frame-art').evaluate((image) => image.decode());
  await openMore(page);
  const fullscreenButton = page.getByRole('button', { name: 'Fullscreen', exact: true });
  if ((await fullscreenButton.count()) === 1) {
    await fullscreenButton.click();
    await fullscreenSettles(page, true);
  }
  if ((await page.evaluate(() => document.fullscreenElement?.tagName)) === 'HTML') {
    // Fullscreen can resize the window to the real screen (Firefox), so the
    // live window metrics are the frame of reference, not the viewport.
    const size = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
    const inside = (box) =>
      box && box.y >= 0 && box.x >= 0 && box.y + box.height <= size.height && box.x + box.width <= size.width;
    assert(inside(await page.locator('.frame-caption').boundingBox()));
    assert(
      await page.evaluate(() => {
        const caption = document.querySelector('.frame-caption');
        const kept = caption.textContent;
        caption.textContent = '';
        const hidden = getComputedStyle(caption).display === 'none';
        caption.textContent = kept;
        return hidden;
      }),
    );
    await page.route('**/v1/owner/journal/birds/*/portrait', (route) => route.abort());
    await page.keyboard.press('Shift');
    await page.getByRole('button', { name: 'Next', exact: true }).click();
    const notice = page.getByText(/Plate unavailable\. Keeping the last approved plate\./);
    await notice.waitFor();
    assert(inside(await notice.boundingBox()));
    await page.unroute('**/v1/owner/journal/birds/*/portrait');
    await page.evaluate(() => document.exitFullscreen());
    assert(await fullscreenSettles(page, false));
  }
  phase = 'frame-ambient-notice-clearance';
  // On every engine, at phone width with the controls revealed, a populated
  // notice must sit fully above the one-row control bar, never behind it.
  await page.keyboard.press('Shift');
  await page.locator('.frame-controls:not(.quiet)').waitFor();
  await page.route('**/v1/owner/journal/birds/*/portrait', (route) => route.abort());
  // At phone width the bar never scrolls sideways: previous, play, next and
  // More are always on screen, and every control behind More fits the
  // window when it opens. Previous then steps to the other plate, whose
  // aborted fetch surfaces the notice for the clearance geometry.
  const windowWidth = await page.evaluate(() => innerWidth);
  const fits = async (locator) => {
    const box = await locator.boundingBox();
    // One-pixel tolerance for subpixel layout.
    assert(box && box.x >= -1 && box.x + box.width <= windowWidth + 1);
  };
  const barFits = () =>
    page.locator('.frame-controls').evaluate((bar) => bar.scrollWidth <= bar.clientWidth + 1);
  assert(await barFits());
  for (const name of ['Previous', 'Play', 'Next', 'More'])
    await fits(page.getByRole('button', { name, exact: true }));
  await openMore(page);
  assert(await barFits());
  for (const control of await page.locator('#frame-more').locator('button:visible, a:visible').all())
    await fits(control);
  await page.keyboard.press('Escape');
  assert.equal(
    await page.getByRole('button', { name: 'More', exact: true }).getAttribute('aria-expanded'),
    'false',
  );
  await captionClear(page);
  const previousControl = page.getByRole('button', { name: 'Previous', exact: true });
  await previousControl.click();
  const clearanceNotice = page.getByText(/Plate unavailable\. Keeping the last approved plate\./);
  await clearanceNotice.waitFor();
  const noticeBox = await clearanceNotice.boundingBox();
  const barBox = await page.locator('.frame-controls').boundingBox();
  assert(noticeBox && barBox && noticeBox.y >= 0 && noticeBox.y + noticeBox.height <= barBox.y);
  await page.unroute('**/v1/owner/journal/birds/*/portrait');
  phase = 'frame-swipe';
  // A horizontal mouse drag on the plate steps to the next plate instead of
  // starting a native image drag.
  const swipeCaption = await page.locator('.frame-caption').innerText();
  const plateBox = await page.locator('.frame-viewport').boundingBox();
  await page.mouse.move(plateBox.x + plateBox.width * 0.75, plateBox.y + plateBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(plateBox.x + plateBox.width * 0.25, plateBox.y + plateBox.height / 2, {
    steps: 5,
  });
  await page.mouse.up();
  await page.waitForFunction(
    (before) => document.querySelector('.frame-caption')?.innerText !== before,
    swipeCaption,
  );
  if (engine === 'chromium') {
    // A finger swipe on the plate while the controls are faded steps too:
    // the touch that wakes the bar must not swallow the step.
    await page.clock.runFor(5100);
    await page.locator('.frame-controls.quiet').waitFor();
    const touchCaption = await page.locator('.frame-caption').innerText();
    const y = plateBox.y + plateBox.height / 2;
    const touch = await page.context().newCDPSession(page);
    const finger = (type, x) =>
      touch.send('Input.dispatchTouchEvent', {
        type,
        touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }],
      });
    await finger('touchStart', plateBox.x + plateBox.width * 0.8);
    for (const share of [0.65, 0.5, 0.35, 0.2])
      await finger('touchMove', plateBox.x + plateBox.width * share);
    await finger('touchEnd');
    await touch.detach();
    await page.waitForFunction(
      (before) => document.querySelector('.frame-caption')?.innerText !== before,
      touchCaption,
    );
  }
  phase = 'frame-resilience';
  // A network outage fails both the periodic check and the next plate, yet
  // playback continues; an emptied view recovers without a reload once its
  // plates return.
  await page.keyboard.press('Shift');
  const playControl = page.getByRole('button', { name: 'Play', exact: true });
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  await page.route('**/v1/owner/journal', (route) => route.abort());
  await page.route('**/v1/owner/journal/birds/*/portrait', (route) => route.abort());
  await page.clock.runFor(60100);
  await page.getByText(/Keeping the last approved plate\./).waitFor();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  await page.unroute('**/v1/owner/journal');
  await page.route('**/v1/owner/journal', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    for (const bird of body.data.birds) bird.plate_ready = false;
    await route.fulfill({ response, json: body });
  });
  await page.clock.runFor(60100);
  await page.getByText(/This view has no approved plates or was removed\./).waitFor();
  assert.equal(await page.locator('.frame-art[src]').count(), 0);
  // Playback stays on, but an emptied view has nothing left to step to.
  const emptiedSteps = [];
  const countStep = (request) => {
    if (/\/v1\/owner\/journal\/birds\/\d+\/portrait$/.test(request.url()))
      emptiedSteps.push(request);
  };
  page.on('request', countStep);
  await page.keyboard.press('ArrowRight');
  await page.clock.runFor(120100);
  await page.waitForTimeout(100);
  page.off('request', countStep);
  assert.equal(emptiedSteps.length, 0, 'an emptied view stops stepping');
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('.frame-art[src]').count(), 0);
  await page.unroute('**/v1/owner/journal');
  await page.unroute('**/v1/owner/journal/birds/*/portrait');
  await page.clock.runFor(60100);
  await page.locator('.frame-art[src]').waitFor();
  await page.keyboard.press('Shift');
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'false');
  phase = 'frame-broken-plate';
  // One plate that keeps failing must not hold playback on one bird. A
  // synthetic copy of a ready plate under another id makes three plates, so
  // the step past the broken one shows a different bird. The plate after the
  // shown one fails; the one after that must appear.
  const shownId = Number(
    new URL(
      await page.locator('.frame-more a[href*="page=bird"]').getAttribute('href'),
      origin,
    ).searchParams.get('bird'),
  );
  const copyId = 990001;
  const plates = { broken: null, source: null, expected: null };
  await page.route('**/v1/owner/journal', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const ready = body.data.birds.filter((bird) => bird.plate_ready);
    plates.source = ready[0].taxon_id;
    ready.push({
      ...ready[0],
      taxon_id: copyId,
      common_name: 'Synthetic copy plate',
      portrait_url: '/v1/owner/journal/birds/' + copyId + '/portrait',
    });
    body.data.birds.push(ready.at(-1));
    const at = ready.findIndex((bird) => bird.taxon_id === shownId);
    plates.broken = ready[(at + 1) % ready.length].taxon_id;
    plates.expected = ready[(at + 2) % ready.length].common_name;
    await route.fulfill({ response, json: body });
  });
  await page.route('**/v1/owner/journal/birds/*/portrait', async (route) => {
    const id = Number(route.request().url().split('/birds/')[1].split('/')[0]);
    if (id === plates.broken) return route.abort();
    if (id !== copyId) return route.continue();
    const original = route.request().url().replace('/birds/' + copyId + '/', '/birds/' + plates.source + '/');
    return route.fulfill({ response: await route.fetch({ url: original }) });
  });
  // While paused, one check picks up the third plate.
  const thirdPlate = page.waitForResponse((response) =>
    response.url().endsWith('/v1/owner/journal'),
  );
  await page.clock.runFor(60100);
  await thirdPlate;
  assert(plates.broken !== null && plates.expected !== null);
  await page.keyboard.press('Shift');
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'true');
  const shownName = () => page.locator('.frame-caption span').first().innerText();
  // Small steps let each plate request settle before the next timer fires.
  for (let waited = 0; waited < 240000 && (await shownName()) !== plates.expected; waited += 500) {
    await page.clock.runFor(500);
    await page.waitForTimeout(20);
  }
  assert.equal(await shownName(), plates.expected, 'playback moves past a plate that keeps failing');
  await page.keyboard.press('Shift');
  await playControl.click();
  assert.equal(await playControl.getAttribute('aria-pressed'), 'false');
  await page.unroute('**/v1/owner/journal/birds/*/portrait');
  await page.unroute('**/v1/owner/journal');
  phase = 'frame-wide';
  // Wide screens show every control in one bar and no More button.
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.keyboard.press('Shift');
  await page.locator('.frame-controls:not(.quiet)').waitFor();
  assert.equal(await page.getByRole('button', { name: 'More', exact: true }).count(), 0);
  for (const name of ['Previous', 'Play', 'Next', 'Shuffle', 'Bird name', 'Zoom'])
    assert(await page.getByRole('button', { name, exact: true }).isVisible());
  assert(await page.locator('.frame-controls').evaluate((bar) => bar.scrollWidth <= bar.clientWidth + 1));
  const wideBar = await page.locator('.frame-controls').boundingBox();
  assert(wideBar.y + wideBar.height <= 800);
  await captionClear(page);
  await zoomEnlarges(page);
  await page.setViewportSize({ width: 844, height: 390 });
  await captionClear(page);
  await page.setViewportSize({ width: 390, height: 844 });
  phase = 'sso-error-visibility';
  // Frontend contract only: the provider state and start failure are stubbed,
  // like the community query. Real OIDC exchange stays backend-tested.
  const ssoContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
  });
  const ssoPage = await ssoContext.newPage();
  await ssoPage.route('**/v1/access', (route) =>
    route.fulfill({
      json: {
        ok: true,
        schema_version: 1,
        data: {
          secure: true,
          setup_required: false,
          registration_allowed: false,
          setup_code: null,
          password_min_length: 15,
          password_max_length: 1024,
          encrypted: true,
          access_mode: 'https-only',
          legacy_account: false,
          session_policy: { regular_hours: 24, remembered_days: 30 },
          provider: { label: 'Authentik', ready: true, linked: true },
          local_login: true,
        },
      },
    }),
  );
  await ssoPage.route('**/v1/owner/oidc/start', (route) =>
    route.fulfill({
      status: 502,
      json: {
        ok: false,
        schema_version: 1,
        error: { code: 'provider_error', message: 'Synthetic provider start failure.' },
      },
    }),
  );
  await ssoPage.goto(origin + '/app');
  await ssoPage.getByRole('heading', { name: 'Welcome back', exact: true }).waitFor();
  const passwordDisclosure = ssoPage.locator('details.auth-alt');
  assert.equal(await passwordDisclosure.count(), 1);
  assert(!(await passwordDisclosure.evaluate((node) => node.open)));
  // One shared stay-signed-in control, one hint, used by the leading action.
  assert.equal(
    await ssoPage.getByRole('checkbox', { name: 'Stay signed in on this device' }).count(),
    1,
  );
  assert.equal(await ssoPage.locator('#remember-hint').count(), 1);
  const ssoStart = ssoPage.getByRole('button', { name: 'Continue with Authentik', exact: true });
  await ssoStart.click();
  await ssoPage
    .getByText('Synthetic provider start failure.', { exact: true })
    .waitFor({ state: 'visible' });
  assert(await ssoStart.isEnabled());
  assert(!(await passwordDisclosure.evaluate((node) => node.open)));
  await passwordDisclosure.locator('summary').click();
  assert(await passwordDisclosure.evaluate((node) => node.open));
  assert.equal(
    await ssoPage.getByRole('checkbox', { name: 'Stay signed in on this device' }).count(),
    1,
  );
  assert.equal(await ssoPage.locator('#remember-hint').count(), 1);
  await ssoContext.close();
  if (process.env.INKY_QA_UPLOAD_FILE) {
    phase = 'large-mobile-upload';
    await page.goto(origin + '/app?page=imports&kind=ebird-archive');
    await heading('Your eBird history');
    await page
      .getByLabel('History file', { exact: true })
      .setInputFiles(process.env.INKY_QA_UPLOAD_FILE);
    await page.getByRole('button', { name: 'Upload for checking', exact: true }).click();
    await step('2. Check the file');
    assert.equal(
      fs.statSync(path.join(fixture, 'var/controller/owner-import/upload.bin')).size,
      fs.statSync(process.env.INKY_QA_UPLOAD_FILE).size,
    );
    await page.getByRole('button', { name: 'Cancel preview', exact: true }).click();
    await heading('1. Choose the complete source file');
    assert(!fs.existsSync(path.join(fixture, 'var/controller/owner-import/upload.bin')));
  }
  if (process.env.INKY_QA_AXE) {
    const { default: AxeBuilder } = require(process.env.INKY_QA_AXE),
      audits = [];
    for (const [area, title] of [
      ['home', 'Your field journal'],
      ['birds', 'Your birds'],
      ['collections', 'Collections'],
      ['manage', 'Manage'],
      ['health', 'Health and help'],
      ['integrations', 'Integrations'],
      ['displays', 'Displays'],
      ['settings', 'Settings and backups'],
    ]) {
      phase = 'accessibility-' + area;
      await page.goto(origin + '/app?page=' + area);
      await heading(title);
      const result = await new AxeBuilder({ page }).analyze();
      audits.push({
        page: area,
        violations: result.violations.map(({ id, impact, nodes }) => ({
          id,
          impact,
          targets: nodes.map((node) => node.target),
        })),
      });
    }
    fs.writeFileSync(fixture + '/accessibility-result.json', JSON.stringify(audits, null, 2), {
      mode: 0o600,
    });
    assert(
      audits.every((result) => result.violations.length === 0),
      'automated accessibility violations need review',
    );
  }
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    fixture + '/history-browser-result.json',
    JSON.stringify(
      {
        passed: true,
        synthetic: true,
        communityQuery: 'mocked',
        reservedSetupRendering: 'synthetic access status; rejection tested through real backend',
        engine,
        sourceCommit: execFileSync('git', ['rev-parse', 'HEAD'], {
          cwd: root,
          encoding: 'utf8',
        }).trim(),
        sourceDirty:
          execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim()
            .length > 0,
        largeUpload: !!process.env.INKY_QA_UPLOAD_FILE,
        accessibility: !!process.env.INKY_QA_AXE,
        checks: [
          'owner setup',
          'HTTP and HTTPS cookie-host trust guidance (HTTPS rendering synthetic)',
          'Favorites separate from sightings',
          'complete archive upload',
          'separate history confirmation',
          'explicit checklist selection',
          'stale edit and response-loss recovery',
          'portable collection export/import',
          'retained history',
          'explicit missing-plate requests',
          'strict candidate approval/rejection',
          'paired-screen scope withdrawal and revocation',
          'paired-screen two-step Forget with failure kept paired',
          'scoped dashboard art',
          'separate status credential and revocation',
          'community evidence not personal',
          'Health and operator guidance',
          'explicit Saved birds preview',
          'Frame viewing tools',
          'Frame wake tap, fade after tap, swipe, outage and emptied-view recovery',
          'Frame phone More bar and wide single bar',
          'no browser storage credentials',
          'no model process or physical panel write',
          'no script errors',
        ],
      },
      null,
      2,
    ),
    { mode: 0o600 },
  );
  console.log('Journal browser acceptance passed (' + engine + '). Synthetic evidence: ' + fixture);
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
          phase,
          type: error.name,
          actual: Array.isArray(error.actual) ? error.actual : undefined,
        },
        null,
        2,
      ),
      { mode: 0o600 },
    );
    console.error('History browser failed at ' + phase + ': ' + error.name);
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
