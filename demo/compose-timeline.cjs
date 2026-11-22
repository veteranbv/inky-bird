// Build demo/out/timeline.json: the frame-exact plan the Remotion composition
// and the exporter share. Scene holds come from the storyboard minimums,
// stretched so no narration sentence is ever cut, and capped by each captured
// clip's usable length.
//
//   node demo/compose-timeline.cjs <out-dir> [--no-voice]
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { readFileSync, writeFileSync, existsSync } = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const out = path.resolve(process.argv[2] || '');
assert(out, 'Usage: node demo/compose-timeline.cjs <out-dir> [--no-voice]');
const voiced = !process.argv.includes('--no-voice');
const storyboard = JSON.parse(readFileSync(path.join(root, 'demo/storyboard.json')));
const { fps, width, height } = storyboard;

// The film must look like the app: colors and type come from its own tokens.
const css = readFileSync(path.join(root, 'src/inky_bird_frame/web/app.css'), 'utf8');
const rootBlock = css.slice(css.indexOf(':root {'), css.indexOf('\n}', css.indexOf(':root {')));
const token = (name) => {
  const match = rootBlock.match(new RegExp(`--${name}:\\s*([^;]+);`));
  assert(match, `app.css is missing --${name}`);
  return match[1].trim();
};
const tokens = Object.fromEntries([
  'paper', 'paper-raised', 'paper-sunken', 'ink', 'ink-soft', 'line', 'line-strong',
  'accent', 'accent-strong', 'accent-soft', 'accent-ink', 'serif', 'radius', 'shadow',
].map((name) => [name, token(name)]));

const durations = voiced
  ? JSON.parse(readFileSync(path.join(out, 'voice/narration-durations.json')))
  : {};

const probe = (file) => Number(execFileSync(process.env.INKY_QA_FFPROBE || 'ffprobe', [
  '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file,
], { encoding: 'utf8' }).trim());

const CROSSFADE_FRAMES = 12;
const NARRATION_PAD_SECONDS = 1.1;
let cursor = 0;
const scenes = storyboard.scenes.map((scene) => {
  let seconds = scene.minSeconds;
  if (voiced) {
    const narration = durations[scene.id];
    assert(typeof narration === 'number', `No narration duration for ${scene.id}`);
    seconds = Math.max(seconds, narration + NARRATION_PAD_SECONDS);
  }
  const entry = {
    id: scene.id, kind: scene.kind, caption: scene.caption,
    samplePill: Boolean(scene.samplePill), chips: scene.chips,
    fadeIn: !scene.hardCut && cursor > 0,
    narrationSeconds: voiced ? durations[scene.id] : 0,
    audio: voiced ? `demo/audio/${scene.id}.wav` : null,
  };
  if (scene.kind === 'clip') {
    const events = JSON.parse(readFileSync(path.join(out, 'capture', `${scene.id}.events.json`)));
    assert.deepEqual(events.blockedExternalRequests, [], `${scene.id} left loopback`);
    assert.deepEqual(events.pageErrors, [], `${scene.id} raised page errors`);
    const clipSeconds = probe(path.join(out, 'capture', `${scene.id}.webm`));
    const usable = clipSeconds - events.trimStart - 0.2;
    assert(usable >= seconds,
      `${scene.id}: clip holds ${usable.toFixed(1)}s but the scene needs ${seconds.toFixed(1)}s; record a longer tail`);
    entry.clip = `demo/${scene.id}.webm`;
    entry.trimStart = events.trimStart;
    entry.events = events.events;
  }
  if (scene.asset) entry.asset = `demo/${path.basename(scene.asset)}`;
  entry.contentFrames = Math.round(seconds * fps);
  entry.startFrame = Math.max(0, cursor - (entry.fadeIn ? CROSSFADE_FRAMES : 0));
  cursor = entry.startFrame + entry.contentFrames;
  return entry;
});

const totalFrames = cursor;
const totalSeconds = totalFrames / fps;
assert(totalSeconds >= 45 && totalSeconds <= 75,
  `Film is ${totalSeconds.toFixed(1)}s; the brief wants 45-75s`);

const timeline = {
  fps, width, height, voiced, tokens, totalFrames,
  crossfadeFrames: CROSSFADE_FRAMES, scenes,
};
writeFileSync(path.join(out, 'timeline.json'), JSON.stringify(timeline, null, 2) + '\n');
console.log(`Timeline: ${totalSeconds.toFixed(1)}s over ${scenes.length} scenes`);
for (const scene of scenes)
  console.log(`  ${scene.id.padEnd(12)} ${(scene.startFrame / fps).toFixed(1).padStart(5)}s +${(scene.contentFrames / fps).toFixed(1)}s${scene.fadeIn ? '' : ' (cut)'}`);
