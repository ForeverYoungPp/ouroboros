// Frozen §7.4 copy pins (plan hubflow-sprint-20260823): the exact user-facing
// strings the adopt confirm dialog and hub cards ship. A wording change is a
// deliberate plan edit, never a drive-by.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'modules', 'ouroboroshub.js'), 'utf8');

const FROZEN = [
    'No local publish record for this name - the hub skill may belong to someone else.',
    'Local files were edited since submission (PR #',
    'Replace the local copy (',
    ") with hub v",
    "confirmLabel: 'Adopt'",
    'Name taken by a local skill',
    'Catalog entry conflict',
    'Hub facts unavailable',
    'Submitted PR #',
    'Adopting a ClawHub-installed skill is not supported yet.',
];

for (const needle of FROZEN) {
    test(`frozen copy present: ${needle.slice(0, 40)}`, () => {
        assert.ok(src.includes(needle), `missing frozen copy: ${needle}`);
    });
}

test('stale-retry guard checks the fresh verdict for every action', () => {
    assert.ok(src.includes('verdict.action !== action'), 'runAction must revalidate ALL actions');
});

test('Update rides the update endpoint, never install?overwrite', () => {
    assert.ok(src.includes('/api/marketplace/ouroboroshub/update/'));
    assert.ok(!src.includes("{ slug, overwrite: true"), 'no overwrite-install update path');
});
