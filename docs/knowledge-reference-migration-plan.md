<!-- Coordinator-authored plan, reviewed and copied into source in Milestone E (2026-11-05). Not applied: no profile, reference, learned skill or knowledge store was changed. -->

# Knowledge-reference migration plan — not applied

Scope: safe preparation only. No profile, raw source, learned skill, runtime configuration or knowledge state has been edited. Source-only capability workflows do not depend on completing this migration.

## Finding and authority

The November 4 research status snapshot reports 12 partial profiles and five ready profiles. These are historical observations, not a new live readiness attestation. Its old canonical-root references point to `/opt/repos/claude-code-autopilot/data/knowledge-canonical`; the configured store is `/opt/repos/agent-ops-kit/data/knowledge-canonical`.

`scripts/knowledge_plan.py:canonical_root` chooses the configured store (`KNOWLEDGE_CANONICAL`, else `<repo>/data/knowledge-canonical`). `resolve_ref` deliberately rejects a reference that names a different root (review correction: the plan draft called it `read_canonical_ref`). Keep this fail-closed behavior. The specialist router's skill-existence check is weaker than learned/current status; neither establishes tools, authorization or runtime acceptance.

## Plan before any migration

1. Obtain exact source scope and exclusive ownership of each affected profile's knowledge/reference/state files. Confirm no active learner or curator owns them. Approval covers reference metadata only; copying or relearning books is a separate operation with approved model/egress scope.
2. Run existing read-only status and dry-run tools. Produce a metadata-only proposed mapping for each affected reference: profile, reference identity, old root, canonical relative path, expected source_id and SHA-256, configured destination, destination kind and availability, approved knowledge assignment, and current learned record identity.
3. Do not import private source text into a shared review or cloud inference context. Metadata can still be sensitive; keep the report within its approved company/knowledge scope. Validate source bytes locally only after that access is approved.
4. Check the canonical destination is inside the configured root, regular and not symlinked, and its source_id/content digest match the existing approved reference and canonical manifest. Match identity, not filename/title. Confirm the profile is still authorized for that source. Different hashes, duplicate identities, missing files, ambiguous assignments and changed scope block that item.
5. Classify each item: verified relocation with unchanged content; changed content requiring re-learning; source missing; ambiguous identity; scope no longer approved. A prefix replacement cannot distinguish these and is forbidden.

## Proposed approved apply

- Preview an exact allowlist of reference files with before-hash/mode, proposed after-hash and original absence state; bind the approval to that manifest and current configured root.
- Preserve original bytes/modes in an approved non-public backup. Apply only verified relocation records under a one-writer lease; refuse on changed input, changed source hash, active learner, path escape or changed scope. No deletion/move of original books.
- Use the existing canonical-reference writer/validator rather than inventing a new knowledge-store implementation. Do not mark learned based on relocation. Unchanged source content may retain historical learned evidence only when its provenance/skill identity is verified; changed or unprovable provenance remains partial.
- Read back every changed reference and rerun strict validation and `make knowledge-learn-status`. Keep missing/blocked sources visible. Do not change the specialist router to label them ready.
- Relearning, if needed, is a distinct approved action using the exact profile and approved inference route. Private sources require local-only inference with no cloud fallback. Confirm actual run, skill changes, reference coverage and unchanged original source digests.

## Rollback

Before apply, record each prior file digest/mode and absent state. Restore only manifest-listed files when their installed hashes equal the approved after-hashes. Refuse a changed post-install file; never overwrite a learner's newer work. Re-run status after restore. Do not reset or delete learned state to make a check pass.

## Required synthetic tests before apply tooling is authorized

- Same source identity/hash at relocated root: proposed reference validates.
- Same filename, different bytes or source_id: blocked.
- Missing source, changed approved profile, unknown scope: blocked with reason.
- Symlink/traversal, duplicate identity, concurrent learner or changed preview: refused.
- Existing learned skill with unresolved source: learned/current remains unknown or partial.
- Interrupted apply and rollback preserve source bytes, file modes and unrelated state.
- Repeated verified dry run is idempotent and writes nothing.
- Real source text never appears in migration logs, external review or cloud requests.

## Readiness evidence to report separately

For each capability/profile: defined; installed; learned/current; tools available; authorized; runtime accepted; executed; outcome verified. An installed SKILL.md proves none of the later dimensions. Fixture tests prove source behavior only. Historical research counts, current filesystem checks, protected runtime authority, actual workflow execution and verified outcomes must have separate timestamps and evidence handles.
