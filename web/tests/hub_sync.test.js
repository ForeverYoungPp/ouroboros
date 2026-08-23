// Rule table for the frozen §7.5 hub card verdict (hubflow sprint 2026-08-23).
// Every branch of hubSyncVerdict is pinned: action enum, badge set, copy facts.

import assert from 'node:assert/strict';
import test from 'node:test';

import { hubListingRowFor, hubSyncVerdict } from '../modules/hub_sync.js';

const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

function catalogRow(overrides = {}) {
    return {
        slug: 'claudexor_quotas',
        sanitized_name: 'claudexor_quotas',
        latest_version: '0.3.0',
        identity_conflict: false,
        ...overrides,
    };
}

function listingRow(overrides = {}) {
    return {
        name: 'claudexor_quotas',
        source: 'ouroboroshub',
        location: 'ouroboroshub',
        version: '0.3.0',
        content_hash: HASH_A,
        official_hub_verified: false,
        published: null,
        published_malformed: false,
        review_stale: false,
        ...overrides,
    };
}

function receipt(overrides = {}) {
    return {
        slug: 'claudexor_quotas',
        version: '0.2.0',
        content_hash: HASH_A,
        repository: 'razzant/ouroboroshub',
        pr_number: 38,
        pr_url: 'https://github.com/razzant/ouroboroshub/pull/38',
        published_at: '2026-08-20T00:00:00Z',
        ...overrides,
    };
}

// ---------------------------------------------------------------------------
// Install / Installed / Update (no local row; hub bucket).
// ---------------------------------------------------------------------------

test('no local occupant with a live catalog row -> install', () => {
    const verdict = hubSyncVerdict(null, catalogRow(), {});
    assert.equal(verdict.action, 'install');
    assert.deepEqual(verdict.badges, []);
    assert.deepEqual(verdict.copy_facts, {
        local_version: '',
        catalog_version: '0.3.0',
        receipt_pr: null,
        edited_since_submission: false,
        occupying_bucket: null,
        no_receipt: false,
        receipt_unreadable: false,
    });
});

test('no local occupant and no catalog row -> none (nothing to install from)', () => {
    const verdict = hubSyncVerdict(null, null, {});
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, []);
});

test('hub bucket at the catalog version -> installed', () => {
    const verdict = hubSyncVerdict(listingRow(), catalogRow(), {});
    assert.equal(verdict.action, 'installed');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.local_version, '0.3.0');
    assert.equal(verdict.copy_facts.occupying_bucket, null);
});

test('hub bucket behind the catalog -> update + update_available badge', () => {
    const verdict = hubSyncVerdict(
        listingRow({ version: '0.2.0' }),
        catalogRow({ latest_version: '0.3.0' }),
        {},
    );
    assert.equal(verdict.action, 'update');
    assert.deepEqual(verdict.badges, ['update_available']);
    assert.equal(verdict.copy_facts.local_version, '0.2.0');
    assert.equal(verdict.copy_facts.catalog_version, '0.3.0');
});

test('version comparison is string inequality only (no semver ordering)', () => {
    // '0.10.0' vs '0.9.0' — inequality is all that is claimed; a numerically
    // "older" catalog version still reads as a difference, never as ordering.
    const verdict = hubSyncVerdict(
        listingRow({ version: '0.10.0' }),
        catalogRow({ latest_version: '0.9.0' }),
        {},
    );
    assert.equal(verdict.action, 'update');
});

test('verified hub bucket -> published badge rides ONLY official_hub_verified===true', () => {
    const verified = hubSyncVerdict(
        listingRow({ official_hub_verified: true }),
        catalogRow(),
        {},
    );
    assert.equal(verified.action, 'installed');
    assert.deepEqual(verified.badges, ['published']);

    // Truthy-but-not-true never counts.
    const truthy = hubSyncVerdict(
        listingRow({ official_hub_verified: 1 }),
        catalogRow(),
        {},
    );
    assert.deepEqual(truthy.badges, []);

    // Verified fact outside the hub bucket never earns the badge.
    const external = hubSyncVerdict(
        listingRow({ location: 'external', official_hub_verified: true }),
        catalogRow(),
        {},
    );
    assert.equal(external.badges.includes('published'), false);
});

// ---------------------------------------------------------------------------
// Adopt (external occupant) — receipt shapes the copy, never the eligibility.
// ---------------------------------------------------------------------------

test('external occupant with a catalog slug and NO receipt -> adopt with no_receipt warning fact', () => {
    const verdict = hubSyncVerdict(
        listingRow({ location: 'external', source: 'self_authored', version: '0.1.0' }),
        catalogRow(),
        {},
    );
    assert.equal(verdict.action, 'adopt');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.no_receipt, true);
    assert.equal(verdict.copy_facts.receipt_unreadable, false);
    assert.equal(verdict.copy_facts.receipt_pr, null);
    assert.equal(verdict.copy_facts.occupying_bucket, 'external');
});

test('external occupant with receipt, hash match, catalog serves the published version -> adopt', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '0.2.0',
            content_hash: HASH_A,
            published: receipt({ version: '0.2.0', content_hash: HASH_A }),
        }),
        catalogRow({ latest_version: '0.2.0' }),
        {},
    );
    // Merged and served: adopt moves the bucket even at hash match.
    assert.equal(verdict.action, 'adopt');
    assert.deepEqual(verdict.badges, []);
    assert.equal(verdict.copy_facts.edited_since_submission, false);
    assert.equal(verdict.copy_facts.no_receipt, false);
    assert.equal(verdict.copy_facts.receipt_pr, 38);
});

test('external occupant edited since submission -> adopt with edited fact (+ submitted_pr when catalog differs)', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '0.2.0',
            content_hash: HASH_B,
            published: receipt({ version: '0.2.0', content_hash: HASH_A }),
        }),
        catalogRow({ latest_version: '0.3.0' }),
        {},
    );
    // Local bytes differ from the submission → this is NOT wait_pr.
    assert.equal(verdict.action, 'adopt');
    assert.deepEqual(verdict.badges, ['submitted_pr']);
    assert.equal(verdict.copy_facts.edited_since_submission, true);
});

// ---------------------------------------------------------------------------
// wait_pr — local bytes ARE the submission, catalog does not serve it yet.
// ---------------------------------------------------------------------------

test('unedited submission with a different catalog version -> wait_pr, never adopt', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '0.4.0',
            content_hash: HASH_A,
            published: receipt({ version: '0.4.0', content_hash: HASH_A, pr_number: 42 }),
        }),
        catalogRow({ latest_version: '0.3.0' }),
        {},
    );
    assert.equal(verdict.action, 'wait_pr');
    assert.deepEqual(verdict.badges, ['submitted_pr']);
    assert.equal(verdict.copy_facts.receipt_pr, 42);
    assert.equal(verdict.copy_facts.edited_since_submission, false);
});

test('receipt with slug absent from the catalog -> submitted_pr badge (pending first merge)', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            version: '0.1.0',
            content_hash: HASH_A,
            published: receipt({ version: '0.1.0', content_hash: HASH_A, pr_number: 55 }),
        }),
        null,
        {},
    );
    // No catalog row: nothing to adopt/install; the submission is the story.
    assert.equal(verdict.action, 'wait_pr');
    assert.deepEqual(verdict.badges, ['submitted_pr']);
    assert.equal(verdict.copy_facts.receipt_pr, 55);
});

test('edited local copy with slug absent from the catalog -> none, badge still says submitted', () => {
    const verdict = hubSyncVerdict(
        listingRow({
            location: 'external',
            content_hash: HASH_B,
            published: receipt({ content_hash: HASH_A }),
        }),
        null,
        {},
    );
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['submitted_pr']);
    assert.equal(verdict.copy_facts.edited_since_submission, true);
});

// ---------------------------------------------------------------------------
// Occupied by non-adoptable buckets -> honest none cards.
// ---------------------------------------------------------------------------

for (const bucket of ['native', 'user_repo', 'clawhub']) {
    test(`${bucket} occupant -> none with occupying_bucket fact`, () => {
        const verdict = hubSyncVerdict(
            listingRow({ location: bucket, source: bucket === 'clawhub' ? 'clawhub' : bucket }),
            catalogRow(),
            {},
        );
        assert.equal(verdict.action, 'none');
        assert.deepEqual(verdict.badges, []);
        assert.equal(verdict.copy_facts.occupying_bucket, bucket);
    });
}

test('unknown/empty location -> none (fail closed, no invented action)', () => {
    const verdict = hubSyncVerdict(listingRow({ location: '' }), catalogRow(), {});
    assert.equal(verdict.action, 'none');
    assert.equal(verdict.copy_facts.occupying_bucket, null);
});

// ---------------------------------------------------------------------------
// Catalog identity conflict -> contract-error card, no actions.
// ---------------------------------------------------------------------------

test('identity_conflict -> conflict badge and action none, even for an installable pair', () => {
    const notInstalled = hubSyncVerdict(null, catalogRow({ identity_conflict: true }), {});
    assert.equal(notInstalled.action, 'none');
    assert.deepEqual(notInstalled.badges, ['conflict']);

    const hubBucket = hubSyncVerdict(
        listingRow({ version: '0.1.0', official_hub_verified: true }),
        catalogRow({ identity_conflict: true }),
        {},
    );
    assert.equal(hubBucket.action, 'none');
    // No update claims off a conflicted catalog row; the listing-plane
    // published fact stays (it is server-verified, not a catalog comparison).
    assert.deepEqual(hubBucket.badges, ['published', 'conflict']);
});

// ---------------------------------------------------------------------------
// Availability flags — fetch failures never impersonate facts.
// ---------------------------------------------------------------------------

test('catalogUnavailable -> never install, catalog_unavailable badge', () => {
    const verdict = hubSyncVerdict(null, null, { catalogUnavailable: true });
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['catalog_unavailable']);
});

test('catalogUnavailable with a hub-bucket row -> installed stays a local fact, no update claim', () => {
    const verdict = hubSyncVerdict(
        listingRow({ version: '0.2.0', published: receipt() }),
        null,
        { catalogUnavailable: true },
    );
    assert.equal(verdict.action, 'installed');
    // No submitted_pr either: without the catalog we cannot say it is
    // unconfirmed. Only the honest unavailability badge remains.
    assert.deepEqual(verdict.badges, ['catalog_unavailable']);
    assert.equal(verdict.copy_facts.catalog_version, '');
});

test('catalogUnavailable with an external submission -> no wait_pr/adopt guess', () => {
    const verdict = hubSyncVerdict(
        listingRow({ location: 'external', content_hash: HASH_A, published: receipt() }),
        null,
        { catalogUnavailable: true },
    );
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['catalog_unavailable']);
});

test('listingUnavailable -> never Install, listing_unavailable badge, local claims dropped', () => {
    const verdict = hubSyncVerdict(null, catalogRow(), { listingUnavailable: true });
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['listing_unavailable']);
    assert.equal(verdict.copy_facts.no_receipt, false);

    // Even a stale listing row passed by mistake is ignored: no local facts.
    const withRow = hubSyncVerdict(listingRow(), catalogRow(), { listingUnavailable: true });
    assert.equal(withRow.action, 'none');
    assert.equal(withRow.copy_facts.local_version, '');
    assert.deepEqual(withRow.badges, ['listing_unavailable']);
});

test('both fetches down -> both badges in the frozen order', () => {
    const verdict = hubSyncVerdict(null, null, { catalogUnavailable: true, listingUnavailable: true });
    assert.equal(verdict.action, 'none');
    assert.deepEqual(verdict.badges, ['catalog_unavailable', 'listing_unavailable']);
});

// ---------------------------------------------------------------------------
// Malformed receipt -> distinct copy fact, not the "someone else's" warning.
// ---------------------------------------------------------------------------

test('published_malformed -> receipt_unreadable fact, no_receipt stays false, adopt still offered', () => {
    const verdict = hubSyncVerdict(
        listingRow({ location: 'external', published: null, published_malformed: true }),
        catalogRow(),
        {},
    );
    assert.equal(verdict.action, 'adopt');
    assert.equal(verdict.copy_facts.receipt_unreadable, true);
    assert.equal(verdict.copy_facts.no_receipt, false);
    assert.deepEqual(verdict.badges, []);
});

// ---------------------------------------------------------------------------
// hubListingRowFor — /api/extensions row -> §7.5 listing-row projection.
// ---------------------------------------------------------------------------

test('hubListingRowFor prefers the server location and coerces types', () => {
    const row = hubListingRowFor({
        name: 'quotas',
        source: 'ouroboroshub',
        location: 'ouroboroshub',
        payload_root: 'skills/external/quotas',
        version: '0.3.0',
        content_hash: HASH_A,
        official_hub_verified: true,
        published: receipt(),
        published_malformed: false,
        review_stale: false,
    });
    assert.equal(row.location, 'ouroboroshub');
    assert.equal(row.official_hub_verified, true);
    assert.equal(row.published.pr_number, 38);
});

test('hubListingRowFor derives the bucket from payload_root when location is absent', () => {
    for (const bucket of ['external', 'clawhub', 'ouroboroshub']) {
        const row = hubListingRowFor({
            name: 'quotas',
            source: 'self_authored',
            payload_root: `skills/${bucket}/quotas`,
            version: '0.1.0',
        });
        assert.equal(row.location, bucket);
    }
});

test('hubListingRowFor falls back to the source tag only for repo-plane buckets', () => {
    assert.equal(hubListingRowFor({ name: 'x', source: 'native' }).location, 'native');
    assert.equal(hubListingRowFor({ name: 'x', source: 'user_repo' }).location, 'user_repo');
    // A data-plane tag without a payload_root claims nothing.
    assert.equal(hubListingRowFor({ name: 'x', source: 'self_authored' }).location, '');
    assert.equal(hubListingRowFor(null), null);
});

test('hubListingRowFor normalizes junk published/malformed fields', () => {
    const row = hubListingRowFor({
        name: 'x',
        source: 'external',
        payload_root: 'skills/external/x',
        published: 'not-an-object',
        published_malformed: 'yes',
        review_stale: 1,
    });
    assert.equal(row.published, null);
    assert.equal(row.published_malformed, false);
    assert.equal(row.review_stale, false);
});
