# Release-engine repair plan

**Status:** executed 18 August 2026. Sections 1-5 are done; section 6 was **not**
reached. The three defects were repaired in `avycore/snowpack/release_v2.py`
(a new module, so no frozen source or digest moved), 128 configurations were
searched on development blocks, and none met the predeclared success rule. The
search stopped on its PLATEAU condition and no reserved block was spent -- all
four remain sealed. Results:
[`release-config-search-v1.json`](../validation-data/results/release-config-search-v1.json);
write-up in [`validation-report.md`](validation-report.md#release-engine-repair-and-configuration-search-failed-its-success-rule).

The finding that matters: at equal terrain budget the repaired model intersects
1.5-2.7x more mapped-avalanche area than the same-area slope baseline on every
block, and still loses on event capture, because a 5%-overlap capture rule
rewards touching many outlines over covering any one of them. No loading-parameter
search closes that gap; the missing variable is snowpack stratigraphy.

The diagnosis below is preserved as written, before execution.

**Scope:** fix why `compute_release` / `extract_release_zones` produced almost no
release zones in the SPOT and CERRA hindcasts. This is a model-and-input defect
plan. It does **not** touch field-validation evidence, the strict cohort gate, or
`is_validated`, all of which remain blocked on data the project does not have.
See `strict-field-validation-plan.md` and `validation-report.md`.

## Why the release engine failed

Release localization, not runout, dominates both hindcast failures. In the CERRA
holdout, release-only capture was 440/1,798 and full end-to-end capture was
446/1,798: the entire runout stage added six events. Three defects were
identified by reading `packages/avycore/src/avycore/hazard/risk.py` against the
frozen hindcast inputs.

### Defect 1 — the wind-loading term was inert in every block

`run_spot_blind_hindcast.py:417` sets `wind_speed_kmh=float(np.mean(scalar_speeds))`,
where `scalar_speeds` accumulates every hour of the 72-hour storm window across
all nine ERA5 sample points. The value handed to the model is therefore a
**72-hour by 9-point scalar mean**, which is not a quantity any loading model
should consume: a storm that blows 45 km/h for six hours and 3 km/h for the rest
arrives as roughly 6 km/h.

`WIND_TRANSPORT_MIN_KMH = 15.0`, so the frozen block winds of 3.6-6.4 km/h all
produce `transport = 0.0` exactly. `WIND_LOADING_WEIGHT` is 0.75, the largest
single weight in the model, and it contributed zero in all five blocks. The model
ran on the snowfall term alone.

### Defect 2 — with wind inert, the threshold is arithmetically unreachable

Release score is `100 * capability * (LOADING_BASE + (1 - LOADING_BASE) * loading)`
with `LOADING_BASE = 0.20`, and `capability = slope_term * forest_damping *
(0.85 + 0.15 * convex_term)` is bounded above by 1.0. `RELEASE_THRESHOLD = 55.0`.

With `transport = 0`, `loading = 0.60 * min(new_snow / 50, 1)`, so the capability
a cell must reach is fixed by new snow alone:

| Block | New snow | Capability required | Zones produced |
|---|---:|---:|---:|
| Albula | 29.3 cm | 1.143 - unreachable | 0 |
| Silvretta | 33.8 cm | 1.049 - unreachable | 0 |
| Gotthard | 36.8 cm | 0.994 - needs a near-perfect cell | 0 |
| Glarus | 50.5 cm | 0.809 - reachable | 40 |
| W. Bernese (dev) | 60.9 cm | 0.809 - reachable | (dev) |

In Albula and Silvretta **no cell could pass regardless of terrain**. The
arithmetic reproduces the observed 0/40/0/0 pattern exactly. Equivalently: with
no wind contribution, the engine emits nothing below about 36.5 cm of new snow
even for a geometrically perfect cell, and nothing below about 46.6 cm for a
realistic 40 degree open slope with flat curvature.

This is the mechanism behind "three of four blocks produced zero release zones."
It is not a subtle miscalibration; it is a saturation failure.

### Defect 3 — the morphology step imposes an undeclared minimum zone size

`extract_release_zones` runs `binary_closing` and then
`binary_opening(smoothed, structure=np.ones((3, 3)))`. A 3x3 opening removes any
region that does not contain a full 3x3 block of candidate cells. At the 30 m
hindcast resolution the effective minimum is a solid 90 m by 90 m patch, i.e.
8,100 m2, not the `MIN_ZONE_AREA_M2 = 2500.0` the manifest advertises. At 30 m
resolution `SMOOTHING_RADIUS_M` also collapses: `radius = max(1, round(15 / 30))
= 1`, so the closing structure silently degrades to 3x3 as well.

## Integrity constraint - read before changing anything

The SPOT 2019 holdout (four blocks) and the CERRA 1999 holdout (five blocks)
have both been scored and their results viewed. **Any model change evaluated
against those blocks is development, not holdout.** Re-running them after a fix
produces a development number and must be labelled as one. Rewriting a frozen
experiment artifact to show a better score is out of bounds.

A clean re-test is available. `regime-hindcast-v1-holdout-blocks.json` selected
five of nine eligible blocks. Four eligible blocks were never used:

| Unused eligible block | Acquisition coverage | Avalanche-terrain fraction |
|---|---:|---:|
| row 1, col 4 | 0.79 | 0.537 |
| row 2, col 4 | 0.77 | 0.374 |
| row 6, col 9 | 0.73 | 0.233 |
| row 5, col 10 | 0.61 | 0.238 |

Reserve all four. Do not look at them until a fix is frozen.

## Ordered plan

### 1. Reproduce the diagnosis as a test

Add a test asserting the saturation bound directly: for the frozen Albula and
Silvretta forcing, `extract_release_zones` returns zero zones, and the required
capability exceeds 1.0. This pins the defect so a later change cannot silently
reintroduce it. Put it beside `tests/test_release_regimes.py`.

### 2. Fix the forcing aggregation before touching the model

Do not retune constants against a broken input. Replace the scalar
72-hour by 9-point wind mean with a storm-window statistic that preserves the
transport signal - a high quantile of hourly speed, or hours-above-threshold, or
the existing CERRA drift-potential index, which already accumulates a bounded
cubic excess-wind term only during transportable cold-snow hours
(`avycore.snow` / the regime hindcast path). Prefer reusing the CERRA index over
inventing a fourth wind statistic.

Record explicitly that a 25 km ERA5 or 5.5 km CERRA cell cannot resolve ridgetop
wind, so whatever statistic is chosen remains a transport *proxy*. Keep the
existing wind-**from** convention and the circular vector mean for direction;
only the speed reduction changes.

Supporting evidence that this is the dominant fault: the CERRA run improved the
wind pathway and capture rose from 2.15% to 24.81% on comparable terrain.

### 3. Re-derive the threshold instead of leaving it at 55

`RELEASE_THRESHOLD = 55.0` is an uncalibrated constant that currently sits above
what the input distribution can produce. After step 2, compute the achievable
score distribution over development terrain and choose the threshold from a
declared operating point (e.g. a target flagged-terrain budget), not by eye.
Document the derivation in the parameter manifest so the provenance hash moves
with it.

Constraints: keep `LOADING_BASE` meaningful - a benign day must still yield few
or no zones - and preserve the existing refusal to treat missing data as neutral.

### 4. Make the morphology honest

Either state the true effective minimum zone area in `parameter_manifest()`, or
replace the fixed 3x3 opening with a resolution-aware structure derived from
`MIN_ZONE_AREA_M2` and `grid.resolution_m`. Also fix the `SMOOTHING_RADIUS_M`
rounding so a 15 m radius at 30 m resolution is an explicit decision rather than
a silent `max(1, 0)`.

### 5. Re-score on development only

Re-run the SPOT and CERRA **development** blocks and the already-burned holdout
blocks, and report both as development. Expect the release-only capture to move;
if it does not, the diagnosis above is incomplete and step 2 should be revisited
before any further tuning.

Also re-check the same-area slope baseline. Beating 62.40% is the bar that
matters; a capture gain that does not beat a same-budget slope threshold is not
progress.

### 6. Freeze, then spend one unused block

Only after the fix is frozen - code, constants, manifest hash, and a written
acceptance rule - run exactly one of the four reserved blocks. Do not run all
four. Treat that block as spent whatever the outcome.

## Acceptance rule (declare before step 6)

Proposed, to be confirmed at freeze time:

- release-only event capture beats the same-area slope-only baseline in the
  reserved block;
- flagged eligible terrain stays within the declared budget;
- no block produces zero release zones for a storm window with mapped positives;
- deterministic replay reproduces the artifact byte-for-byte.

Failing this rule is a publishable negative result, exactly as the previous two
were. Do not weaken the rule after seeing the score.

## Out of scope

Field validation. None of this work adds an eligible holdout event, changes
`is_validated`, or licenses an accuracy claim. The positive-only capture metric
remains positive-only: unmapped terrain is unknown, never verified negative.
Improving release localization makes the engine better; it does not make it
validated.
