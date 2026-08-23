/**
 * OuroborosHub card verdict — the ONE client-side authority joining a hub
 * catalog row with the global /api/extensions listing row for the same
 * canonical name (plan §7.5, frozen contract).
 *
 * Pure data-in/data-out: no fetches, no DOM, no version parsing. The only
 * comparisons are string inequality on versions and strict equality between
 * the local content hash and the publish-receipt hash. The local content hash
 * is NEVER compared with the catalog (the payload sidecar is part of the
 * hash, so listing-vs-catalog byte equality is structurally false; that
 * equality lives server-side in the official_hub review profile and reaches
 * this function only as the `official_hub_verified` fact).
 *
 * @typedef {Object} HubListingRow  One /api/extensions skill row projection.
 * @property {string} name               canonical skill name
 * @property {string} source            classification tag (self_authored, …)
 * @property {string} location          physical bucket: external|clawhub|ouroboroshub|native|user_repo|''
 * @property {string} version            local manifest version
 * @property {string} content_hash       loader content hash of the local tree
 * @property {boolean} official_hub_verified byte-exact match with the live catalog (server fact)
 * @property {Object|null} published     publish receipt section (slug, version, content_hash, pr_number, pr_url, …)
 * @property {boolean} published_malformed  receipt exists on disk but is unreadable (server projects published=null)
 * @property {boolean} review_stale
 *
 * @typedef {Object} HubCatalogRow  One /api/marketplace/ouroboroshub/catalog row projection.
 * @property {string} slug
 * @property {string} sanitized_name    server-computed canonical name (JS never sanitizes)
 * @property {string} latest_version
 * @property {boolean} identity_conflict catalog holds >1 slug with this canonical name
 *
 * @typedef {Object} HubSyncVerdict
 * @property {'install'|'installed'|'update'|'adopt'|'wait_pr'|'none'} action
 * @property {Array<'submitted_pr'|'published'|'update_available'|'catalog_unavailable'|'listing_unavailable'|'conflict'>} badges
 * @property {{local_version: string, catalog_version: string, receipt_pr: number|null,
 *            edited_since_submission: boolean, occupying_bucket: string|null,
 *            no_receipt: boolean, receipt_unreadable: boolean}} copy_facts
 *   `receipt_unreadable` is the §7.5 "publish record unreadable" copy fact —
 *   additive beside the frozen keys so malformed-receipt copy stays distinct
 *   from the no-receipt warning (`no_receipt` is false when the receipt is
 *   merely unreadable).
 */

/**
 * Project one /api/extensions skill row into the §7.5 listing-row shape.
 * Prefers a server-provided `location`; otherwise derives the physical bucket
 * from `payload_root` (skills/<bucket>/…) and falls back to the source tag
 * only for the repo-plane buckets that have no data-plane payload_root.
 * @returns {HubListingRow|null}
 */
export function hubListingRowFor(skill) {
    if (!skill || typeof skill !== 'object') return null;
    return {
        name: String(skill.name || ''),
        source: String(skill.source || ''),
        location: listingLocation(skill),
        version: String(skill.version || ''),
        content_hash: String(skill.content_hash || ''),
        official_hub_verified: skill.official_hub_verified === true,
        published: skill.published && typeof skill.published === 'object' ? skill.published : null,
        published_malformed: skill.published_malformed === true,
        review_stale: skill.review_stale === true,
        identity_collision: skill.identity_collision === true,
    };
}

function listingLocation(skill) {
    const explicit = String(skill.location || '');
    if (explicit) return explicit;
    const bucket = /^skills\/(external|clawhub|ouroboroshub)\//.exec(String(skill.payload_root || ''));
    if (bucket) return bucket[1];
    const source = String(skill.source || '').toLowerCase();
    if (source === 'native' || source === 'user_repo') return source;
    return '';
}

/**
 * Compute the card verdict for one (listing row, catalog row) pair.
 * Rules verbatim from plan §7.5; §7 wins over every earlier draft.
 *
 * @param {HubListingRow|null} listingRow local occupant of the canonical name, or null
 * @param {HubCatalogRow|null} catalogRow catalog entry for the slug, or null (slug absent)
 * @param {{catalogUnavailable?: boolean, listingUnavailable?: boolean}} [flags]
 * @returns {HubSyncVerdict}
 */
export function hubSyncVerdict(listingRow, catalogRow, flags = {}) {
    const catalogUnavailable = flags.catalogUnavailable === true;
    const listingUnavailable = flags.listingUnavailable === true;
    // A failed listing fetch means no local fact may be claimed at all.
    const listing = listingUnavailable ? null : (listingRow || null);
    const catalog = catalogUnavailable ? null : (catalogRow || null);
    // Conflict is fail-closed from EITHER plane: a catalog whose slugs collide
    // on one canonical name, or a local listing row the loader marked as an
    // identity collision (several same-name occupants — no affordance may act
    // on an ambiguous identity).
    const conflict = Boolean(
        (catalog && catalog.identity_conflict === true)
        || (listing && listing.identity_collision === true),
    );

    const published = listing && listing.published && typeof listing.published === 'object'
        ? listing.published
        : null;
    const location = listing ? String(listing.location || '') : '';
    const localVersion = listing ? String(listing.version || '') : '';
    const catalogVersion = catalog ? String(catalog.latest_version || '') : '';
    const publishedVersion = published ? String(published.version || '') : '';
    const receiptHashMatches = Boolean(published
        && String(listing.content_hash || '') === String(published.content_hash || ''));

    const copy_facts = {
        local_version: localVersion,
        catalog_version: catalogVersion,
        receipt_pr: published && typeof published.pr_number === 'number' ? published.pr_number : null,
        edited_since_submission: Boolean(published && !receiptHashMatches),
        occupying_bucket: listing && location && location !== 'ouroboroshub' ? location : null,
        no_receipt: Boolean(listing && !published && listing.published_malformed !== true),
        receipt_unreadable: Boolean(listing && listing.published_malformed === true),
    };

    let action = 'none';
    if (!listingUnavailable && !conflict) {
        if (!listing) {
            // No local occupant → Install (only from a live catalog row).
            if (catalog) action = 'install';
        } else if (location === 'ouroboroshub') {
            // Hub bucket: Installed, or Update when the live catalog version
            // differs (string inequality only — no ordering semantics).
            action = catalog && catalogVersion !== localVersion ? 'update' : 'installed';
        } else if (location === 'external') {
            // wait_pr preempts Adopt: the local bytes ARE the submitted bytes
            // and the catalog does not serve that submitted version yet —
            // never offer adopting the older catalog back over the submission.
            if (!catalogUnavailable && published && receiptHashMatches
                && catalogVersion !== publishedVersion) {
                action = 'wait_pr';
            } else if (catalog) {
                action = 'adopt';
            }
        }
        // clawhub (v1 unsupported), native, user_repo, unknown → 'none'.
    }

    const badges = [];
    if (!listingUnavailable && !conflict && !catalogUnavailable && published
        && (!catalog || catalogVersion !== publishedVersion)) {
        // Receipt exists and the catalog does not confirm the published version
        // (slug absent, or a different served version) → "Submitted PR #N".
        badges.push('submitted_pr');
    }
    if (listing && location === 'ouroboroshub' && listing.official_hub_verified === true) {
        // "Published vX" rides ONLY on the server's byte-exact verification.
        badges.push('published');
    }
    if (!conflict && listing && location === 'ouroboroshub' && catalog
        && catalogVersion !== localVersion) {
        badges.push('update_available');
    }
    if (catalogUnavailable) badges.push('catalog_unavailable');
    if (listingUnavailable) badges.push('listing_unavailable');
    if (conflict) badges.push('conflict');

    return { action, badges, copy_facts };
}
