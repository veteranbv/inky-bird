import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

for (const area of ['birds', 'save', 'manage', 'frame']) {
  test(`${area}.e2e.cjs refuses to run on its own`, () => {
    const file = fileURLToPath(new URL(`./${area}.e2e.cjs`, import.meta.url));
    const result = spawnSync(process.execPath, [file], { encoding: 'utf8', env: {} });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /Run tests\/web\/journal\.e2e\.cjs/);
  });
}
