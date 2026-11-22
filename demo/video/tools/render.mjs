// Bundle once, render a still of every scene, then the full film.
//
//   node tools/render.mjs --timeline <timeline.json> --out <dir> [--stills-only] [--scene <id>]...
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { bundle } from '@remotion/bundler';
import { renderMedia, renderStill, selectComposition } from '@remotion/renderer';

const here = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const value = (flag) => {
  const index = argv.indexOf(flag);
  return index === -1 ? null : argv[index + 1];
};
const timelinePath = value('--timeline');
const out = value('--out');
if (!timelinePath || !out) throw new Error('Usage: node tools/render.mjs --timeline <file> --out <dir>');
const stillsOnly = argv.includes('--stills-only');
const only = [];
for (let index = 0; index < argv.length; index += 1)
  if (argv[index] === '--scene') only.push(argv[index + 1]);

const timeline = JSON.parse(readFileSync(timelinePath, 'utf8'));
const inputProps = { timeline };

const serveUrl = await bundle({
  entryPoint: path.join(here, '../src/index.ts'),
  publicDir: path.join(here, '../public'),
});
const composition = await selectComposition({ serveUrl, id: 'InkyDemo', inputProps });

const stills = path.join(out, 'stills');
mkdirSync(stills, { recursive: true });
for (const scene of timeline.scenes) {
  if (only.length && !only.includes(scene.id)) continue;
  // The title still is the poster: it must match the first decoded frame.
  const frame = scene.kind === 'title'
    ? scene.startFrame
    : scene.startFrame + Math.floor(scene.contentFrames / 2);
  const output = path.join(stills, `${scene.id}.png`);
  await renderStill({ serveUrl, composition, inputProps, frame, output });
  console.log(`still ${scene.id}: frame ${frame}`);
}

if (!stillsOnly && !only.length) {
  await renderMedia({
    serveUrl,
    composition,
    inputProps,
    codec: 'h264',
    crf: 18,
    pixelFormat: 'yuv420p',
    outputLocation: path.join(out, 'render.mp4'),
    onProgress: ({ progress }) => {
      if (Math.round(progress * 100) % 20 === 0)
        process.stdout.write(`\rrender ${(progress * 100).toFixed(0)}%   `);
    },
  });
  process.stdout.write('\n');
  console.log('Rendered', path.join(out, 'render.mp4'));
}
