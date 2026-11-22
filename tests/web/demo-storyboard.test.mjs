// Fast storyboard validity checks: structure, honest captions and a total
// length inside the demo brief's 45-75 second window.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const storyboard = JSON.parse(readFileSync(path.join(root, 'demo/storyboard.json')));

// Claims the film must never make: browser Play driving the physical wall,
// hand-painted artwork, or public nearby sightings presented as the owner's.
const FORBIDDEN = [
  /hand[- ]?painted/i,
  /painted by hand/i,
  /updates? (the|your) wall/i,
  /changes? (the|your) wall/i,
  /syncs? (the|your) wall/i,
  /drives? (the|your) wall/i,
  /your (own )?nearby sightings/i,
];

test('storyboard structure', () => {
  assert.equal(storyboard.fps, 30);
  assert.deepEqual([storyboard.width, storyboard.height], [1920, 1080]);
  const scenes = storyboard.scenes;
  assert(Array.isArray(scenes) && scenes.length >= 5);
  assert.equal(new Set(scenes.map((scene) => scene.id)).size, scenes.length);
  for (const scene of scenes) {
    assert.match(scene.id, /^[a-z][a-z-]*$/);
    assert(['title', 'plate', 'sources', 'clip', 'photo', 'end'].includes(scene.kind), scene.id);
    assert(typeof scene.minSeconds === 'number' && scene.minSeconds > 0, scene.id);
    assert(typeof scene.narration === 'string' && scene.narration.length > 0, scene.id);
    assert(typeof scene.caption === 'string', scene.id);
    if (!['title', 'end'].includes(scene.kind))
      assert(scene.caption.length > 0, `${scene.id} needs a caption`);
    if (scene.kind === 'clip') assert.match(scene.clip, /^[a-z-]+\.webm$/);
    if (scene.kind === 'photo') assert(scene.hardCut, 'the wall photo enters on a hard cut');
    if (scene.asset) assert(!path.isAbsolute(scene.asset) && !scene.asset.includes('..'));
  }
});

test('total length fits the 45-75s window', () => {
  const total = storyboard.scenes.reduce((sum, scene) => sum + scene.minSeconds, 0);
  assert(total >= 45 && total <= 75, `minimum holds sum to ${total}s`);
});

test('captions and narration make no forbidden claim', () => {
  for (const scene of storyboard.scenes)
    for (const pattern of FORBIDDEN) {
      assert(!pattern.test(scene.caption), `${scene.id} caption matches ${pattern}`);
      assert(!pattern.test(scene.narration), `${scene.id} narration matches ${pattern}`);
    }
});

test('UI scenes carry the Sample data pill', () => {
  for (const scene of storyboard.scenes)
    if (scene.kind === 'clip') assert(scene.samplePill, scene.id);
});
