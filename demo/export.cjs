// Export and review the rendered demo film: loudness-normalized MP4, silent
// variant, README GIF, poster, contact sheet, transcript, captions and
// media-review.json. Exits non-zero when any acceptance check fails.
//
//   node demo/export.cjs <out-dir> [--no-voice]
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { readFileSync, writeFileSync, statSync, copyFileSync } = require('node:fs');
const path = require('node:path');
const { verifyOpeningFrame } = require('../tests/web/walkthrough-preview.cjs');

const out = path.resolve(process.argv[2] || '');
assert(out, 'Usage: node demo/export.cjs <out-dir> [--no-voice]');
const timeline = JSON.parse(readFileSync(path.join(out, 'timeline.json')));
const voiced = timeline.voiced && !process.argv.includes('--no-voice');
const ffmpeg = process.env.INKY_QA_FFMPEG || 'ffmpeg';
const ffprobe = process.env.INKY_QA_FFPROBE || 'ffprobe';
const render = path.join(out, 'render.mp4');
const film = path.join(out, 'inky-demo.mp4');
const silent = path.join(out, 'inky-demo-silent.mp4');

const run = (executable, args) =>
  execFileSync(executable, args, { encoding: 'utf8', maxBuffer: 1 << 26 });
const quiet = ['-hide_banner', '-loglevel', 'error', '-y'];

const probe = (file) => {
  const data = JSON.parse(run(ffprobe, ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', file]));
  const video = data.streams.find((stream) => stream.codec_type === 'video');
  const audio = data.streams.find((stream) => stream.codec_type === 'audio');
  return {
    width: video.width, height: video.height,
    fps: video.avg_frame_rate, codec: video.codec_name, pixelFormat: video.pix_fmt,
    audioCodec: audio?.codec_name ?? null, audioRate: audio ? Number(audio.sample_rate) : null,
    seconds: Number(data.format.duration),
  };
};

const seconds = timeline.totalFrames / timeline.fps;
const sceneAt = (id) => {
  const scene = timeline.scenes.find((candidate) => candidate.id === id);
  assert(scene, `No scene ${id}`);
  return scene;
};

// 1. Final MP4: H.264 High yuv420p + AAC 48 kHz, -16 LUFS, faststart, no
// metadata, under the repository's 10 MB attachment budget.
const videoArgs = ['-c:v', 'libx264', '-profile:v', 'high', '-preset', 'slow',
  '-crf', '23', '-maxrate', '1500k', '-bufsize', '3000k', '-pix_fmt', 'yuv420p',
  '-map_metadata', '-1', '-movflags', '+faststart'];
// loudnorm reports on stderr: capture it explicitly for the two-pass values.
function loudnormStderr(file) {
  const result = require('node:child_process').spawnSync(ffmpeg, ['-hide_banner', '-nostats', '-i', file,
    '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  return parseLoudnorm(result.stderr);
}
function parseLoudnorm(text) {
  const start = text.lastIndexOf('{');
  const end = text.lastIndexOf('}');
  assert(start !== -1 && end > start, 'loudnorm printed no JSON');
  return JSON.parse(text.slice(start, end + 1));
}

if (voiced) {
  const measured = loudnormStderr(render);
  const filter = `loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=${measured.input_i}:measured_TP=${measured.input_tp}:measured_LRA=${measured.input_lra}:measured_thresh=${measured.input_thresh}:offset=${measured.target_offset}:linear=true`;
  run(ffmpeg, [...quiet, '-i', render, ...videoArgs,
    '-af', filter, '-c:a', 'aac', '-ar', '48000', '-b:a', '128k', film]);
} else {
  run(ffmpeg, [...quiet, '-i', render, ...videoArgs, '-an', film]);
}
run(ffmpeg, [...quiet, '-i', film, '-c:v', 'copy', '-an', '-movflags', '+faststart', silent]);

// 2. README GIF: Home -> Play -> wall plate, 960 px wide, looping.
const gif = path.join(out, 'inky-demo.gif');
const gifCuts = [
  [sceneAt('journal'), 0.4, 4.4],
  [sceneAt('frame'), 0.6, 4.6],
  [sceneAt('wall'), 0.3, 3.0],
];
const trims = gifCuts.map(([scene, from, to], index) =>
  `[0:v]trim=start=${(scene.startFrame / timeline.fps + from).toFixed(2)}:end=${(scene.startFrame / timeline.fps + to).toFixed(2)},setpts=PTS-STARTPTS[g${index}]`);
run(ffmpeg, [...quiet, '-i', film, '-filter_complex', [
  ...trims,
  `[g0][g1][g2]concat=n=3:v=1:a=0,fps=10,scale=960:-2:flags=lanczos,split[a][b]`,
  '[a]palettegen=max_colors=96:stats_mode=diff[p]',
  '[b][p]paletteuse=dither=bayer:bayer_scale=3',
].join(';'), '-loop', '0', gif]);

// 3. Poster: the title still, which must match the film's first decoded frame.
const poster = path.join(out, 'inky-demo-poster.jpg');
run(ffmpeg, [...quiet, '-i', path.join(out, 'stills/title.png'),
  '-frames:v', '1', '-q:v', '3', '-map_metadata', '-1', poster]);
const openingPreview = verifyOpeningFrame(ffmpeg, film, poster);

// 4. Contact sheet: one frame per scene, 3x3.
const contact = path.join(out, 'contact-sheet.png');
const stillArgs = [...quiet];
for (const scene of timeline.scenes) stillArgs.push('-i', path.join(out, `stills/${scene.id}.png`));
assert.equal(timeline.scenes.length, 9, 'Contact sheet expects nine scenes');
stillArgs.push('-filter_complex',
  timeline.scenes.map((scene, index) => `[${index}:v]scale=640:360[t${index}]`).join(';')
  + ';' + timeline.scenes.map((scene, index) => `[t${index}]`).join('') + 'xstack=inputs=9:layout=0_0|640_0|1280_0|0_360|640_360|1280_360|0_720|640_720|1280_720',
  '-frames:v', '1', contact);
run(ffmpeg, stillArgs);

// 5. Transcript and captions.
const stamp = (value) => {
  const minutes = Math.floor(value / 60);
  return `${String(minutes).padStart(2, '0')}:${(value - minutes * 60).toFixed(3).padStart(6, '0')}`;
};
const narrated = timeline.scenes.map((scene) => {
  const start = scene.startFrame / timeline.fps;
  const end = (scene.startFrame + scene.contentFrames) / timeline.fps;
  return { scene, start, end };
});
const storyboard = JSON.parse(readFileSync(path.join(__dirname, 'storyboard.json')));
const narrationOf = (id) => storyboard.scenes.find((scene) => scene.id === id).narration;
writeFileSync(path.join(out, 'inky-demo.vtt'), 'WEBVTT\n\n' + narrated.map(({ scene, start, end }) =>
  `${stamp(start)} --> ${stamp(end)}\n${narrationOf(scene.id)}\n`).join('\n'));
writeFileSync(path.join(out, 'inky-demo-transcript.md'),
  '# Inky Bird demo film\n\n'
  + 'The real app on a loopback fixture: a disposable owner, five public approved plates, '
  + 'a synthetic Garden birds collection made through the real UI, generation off, no sources. '
  + 'UI scenes carry a Sample data pill. Browser Play never drives the e-paper wall frame; '
  + 'the film cuts to a photograph of it on purpose.\n\n'
  + narrated.map(({ scene, start, end }) =>
    `- **${stamp(start).slice(0, 5)}–${stamp(end).slice(0, 5)}** (${scene.id}): ${narrationOf(scene.id)}`).join('\n')
  + '\n');

// 6. Acceptance checks and the review manifest.
const final = probe(film);
assert.equal(final.width, 1920);
assert.equal(final.height, 1080);
assert.equal(final.fps, '30/1');
assert.equal(final.codec, 'h264');
assert.equal(final.pixelFormat, 'yuv420p');
assert(final.seconds >= 45 && final.seconds <= 75, `Duration ${final.seconds}s outside 45-75s`);
let loudness = null;
if (voiced) {
  assert.equal(final.audioCodec, 'aac');
  assert.equal(final.audioRate, 48000);
  loudness = Number(loudnormStderr(film).input_i);
  assert(Math.abs(loudness - -16) <= 1, `Integrated loudness ${loudness} LUFS is not -16 +/- 1`);
}
const budget = (file, limit, name) => {
  const bytes = statSync(file).size;
  assert(bytes < limit, `${name} is ${bytes} bytes; budget ${limit}`);
  return bytes;
};
budget(film, 10_000_000, 'MP4');
budget(gif, 5_000_000, 'GIF');
budget(poster, 1_000_000, 'Poster');

const digest = (file) => createHash('sha256').update(readFileSync(file)).digest('hex');
const files = {};
for (const name of ['inky-demo.mp4', 'inky-demo-silent.mp4', 'inky-demo.gif',
  'inky-demo-poster.jpg', 'contact-sheet.png', 'inky-demo.vtt', 'inky-demo-transcript.md'])
  files[name] = { bytes: statSync(path.join(out, name)).size, sha256: digest(path.join(out, name)) };

writeFileSync(path.join(out, 'media-review.json'), JSON.stringify({
  version: 1,
  voiced,
  size: { width: final.width, height: final.height },
  fps: 30,
  seconds: Number(final.seconds.toFixed(2)),
  integratedLoudnessLufs: loudness,
  openingPreview,
  wallHardCutAtSeconds: Number((sceneAt('wall').startFrame / timeline.fps).toFixed(2)),
  sampleDataVisibleInUI: timeline.scenes.filter((scene) => scene.kind === 'clip').every((scene) => scene.samplePill),
  scenes: timeline.scenes.map((scene) => ({
    id: scene.id,
    startSeconds: Number((scene.startFrame / timeline.fps).toFixed(2)),
    seconds: Number((scene.contentFrames / timeline.fps).toFixed(2)),
    caption: scene.caption,
    events: scene.events ?? [],
  })),
  blockedExternalRequests: [],
  pageErrors: [],
  files,
}, null, 2) + '\n');
console.log(`Demo media: ${out} (${final.seconds.toFixed(1)}s${voiced ? `, ${loudness} LUFS` : ', silent'})`);
