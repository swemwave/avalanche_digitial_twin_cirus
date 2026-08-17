# Public-event independent human-review procedure

This procedure is the external handoff for the strict public-validation funnel.
It does not authorize a validation claim. The Digital Twin remains an
experimental research prototype, does not replace Avalanche Canada guidance or
field assessment, and reports relative indices rather than probabilities.

## Inputs and separation

Use `validation-data/candidates/blinded-observation-packets-v4.json` as the
custodian manifest. It identifies 26 immutable ZIPs by SHA-256, all released for
annotation. `regobs-448389` contains its two source photographs but no completed
Sentinel pair; that missing coverage remains explicit and may force reviewers to
return `not_supportable`. A released packet is an annotation input, not accepted
evidence.

For each packet, the custodian must:

1. Verify the ZIP SHA-256 against the manifest.
2. Give separate clean copies to reviewer A and reviewer B.
3. Give each reviewer only their blank form and the source material in the ZIP.
4. Withhold evaluated outputs, parameter results, AI overlays, the candidate-ID
   mapping, and the other reviewer's submission.
5. Confirm that both reviewers are real people, are distinct from one another,
   are independent of the evaluated model work, and have suitable avalanche and
   image-interpretation competence.

The optional AI artifact is
`validation-data/candidates/public-event-ai-annotation-proposals-v1.json`.
Every record is `ai_generated_only=true`; it is not included in a human packet
and cannot satisfy, corroborate, or replace either human review.

## Reviewer task

Each reviewer independently completes `forms/reviewer-a.json` or
`forms/reviewer-b.json` from the packet. Do not copy a provider mark without
declaring it. Do not infer an absent avalanche from an unreported or obscured
area.

For release, dense-flow deposit, and terminal dense-flow toe, record either:

- `observed`, with geometry, source scenes, method, effective resolution,
  horizontal uncertainty and confidence level, temporal uncertainty,
  confidence basis, attribution, CRS, coordinate order, transformation lineage,
  limitations, and an acceptance disposition; or
- `not_supportable`, with null geometry. Null means unknown, not observed zero.

For every observed component, explicitly complete all twelve mask records:
missing data, scene edge, detection exclusion, survey coverage, cloud, cloud
shadow, topographic/cast shadow, forest, water, layover, radar shadow, and prior
deposit. Use `mapped_present`, `checked_absent`, or `not_applicable`, always with
a basis. `survey_coverage` must be a mapped polygon; the image boundary is not
automatically a complete-search domain.

Both reviewers also independently propose path, mountain, and storm-cycle IDs
with a basis. They independently accept, reject, or decline to assess the frozen
200–250 kg/m³ transferred density prior. Acceptance requires a positive numeric
`transfer_uncertainty_kg_m3` and a written basis; unknown transfer uncertainty
must remain null and cannot pass the density gate.
Matching strings are not proof of independence; the custodian verification is
separately required.

## Identity verification record

After receiving both sealed submissions, a real project custodian creates one
JSON file named `<packet_id>.json` in the identity-verification directory. Never
invent identities, signatures, contacts, or decisions. The required shape is:

```json
{
  "schema": "avycore-independent-reviewer-verification-v1",
  "packet_id": "<packet_id>",
  "reviewer_identity_sha256s": ["<sha256 of trimmed reviewer A identity>", "<sha256 of trimmed reviewer B identity>"],
  "verifier_identity": "<real custodian identity>",
  "verified_at_utc": "<ISO-8601 UTC time>",
  "verification_method": "<how identity, competence, and independence were checked>",
  "reviewers_are_genuine_humans": true,
  "reviewers_independent_of_project_model": true,
  "reviewers_independent_of_each_other": true
}
```

This is an attested verification record, not a cryptographic signature. Do not
claim stronger identity assurance than the recorded method supports.

## Machine validation and import

First validate without writing an artifact:

```powershell
python scripts/validation/import_public_event_human_reviews.py `
  --packets validation-data/candidates/blinded-observation-packets-v4.json `
  --review-root <sealed-review-submission-directory> `
  --identity-verification-root <identity-verification-directory> `
  --minimum-complete 12 `
  --check-only
```

After that command passes, write a new immutable status path; do not overwrite
the checked-in zero-review status:

```powershell
python scripts/validation/import_public_event_human_reviews.py `
  --packets validation-data/candidates/blinded-observation-packets-v4.json `
  --review-root <sealed-review-submission-directory> `
  --identity-verification-root <identity-verification-directory> `
  --minimum-complete 12 `
  --output validation-data/candidates/public-event-human-review-status-v5-reviewed.json
```

The importer rejects wrong packet hashes, duplicate identities, non-UTC times,
AI-only records, peer-output access, missing uncertainty, missing masks,
non-projected geometry, unsupported component types, and review disagreement
outside the frozen comparison rule. It never creates geometry or human records.

Re-evaluate with the unchanged contract and the new status:

```powershell
python scripts/validation/evaluate_public_event_strict_funnel.py `
  --human-reviews validation-data/candidates/public-event-human-review-status-v5-reviewed.json `
  --output validation-data/candidates/public-event-strict-funnel-v5-reviewed.json
```

## What human review cannot repair

Do not run a grouped split, AvaFrame integration, calibration, or holdout merely
because twelve review pairs import. Every selected event must also obtain public
primary evidence for an event-compatible terrain/snow surface, normal-to-slope
release thickness, provenance-bearing Profile-E forcing/snow state where
applicable, and an admissible projected-metre CRS/transform under unchanged
validation-contract v3. Reviewers must not manufacture those fields from crown
height, a bare-earth screening DTM, regional weather, or nominal pixel size.

Proceed downstream only if the strict evaluator reports at least 12 eligible
Profile-C events across six paths, two mountains, and three storms. Otherwise
keep `is_validated=false` and publish the remaining failed predicates.
