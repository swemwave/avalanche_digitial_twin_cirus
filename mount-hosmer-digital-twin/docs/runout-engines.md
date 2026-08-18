# Runout engines: verified upstream identities

What each external runout engine actually is, which upstream implementation runs,
under what licence, and what it can and cannot produce.

**Related:** [`prediction-products.md`](prediction-products.md) ·
[`limitations.md`](limitations.md) · [`architecture.md`](architecture.md).

---

## 1. Why two Flow-Py identities exist

Flow-Py and AvaFrame `com4FlowPy` are the **same published model in two upstream
distributions**, and the project keeps them as two engine identities so a result
can never be ambiguous about which one produced it.

**Canonical standalone distribution** — [`github.com/avaframe/FlowPy`](https://github.com/avaframe/FlowPy)

| | |
|---|---|
| Licence | GPL-3.0-or-later (`LICENSE.txt` is the GNU GPL v3, 29 June 2007) |
| Latest release | `v1.0.3`, 14 June 2022, commit `7b061599355cef584491d69eae2686307d286901` |
| Last commit | `27ad81d3e804e4e9d85a9773fca10ee7dc428183`, 20 June 2022 ("bug killed"), untagged |
| Repository status | **Archived read-only since 17 September 2024** |
| Paper | Neuhauser et al. (2022), *Geosci. Model Dev.* **15**, 2423–2442, [doi:10.5194/gmd-15-2423-2022](https://doi.org/10.5194/gmd-15-2423-2022) |
| Dependencies | GDAL, numpy, **PyQt5**, rasterio |

**AvaFrame port** — [`com4FlowPy`](https://docs.avaframe.org/en/latest/moduleCom4FlowPy.html)

| | |
|---|---|
| Licence | EUPL-1.2 (AvaFrame) |
| Pinned version | AvaFrame `2.1` (`backend/requirements-avaframe.txt`) |
| Upstream status | Documented as "currently under heavy development", **not in AvaFrame's automatic test coverage** |

### Why the standalone adapter is fail-closed

`runout.flowpy_upstream` exists and is deliberately unavailable. Three findings,
each verified against the repository itself, explain why:

1. **The released command line ignores its arguments.** In tag `v1.0.3`,
   `main.py` reads `argv = sys.argv[1:]` and then **reassigns** `argv` to a
   hardcoded Osttirol example inside its `__main__` block. Whatever an adapter
   passes is discarded. The blob SHA-256 of that `main.py` is
   `200ec899a12a4c1eebbbd6b3d0c49efd2b10f25418650312d3d16cf351169e76`.
2. **Only an untagged commit fixes it.** Commit `27ad81d…` comments that
   reassignment out (`main.py` SHA-256
   `6171fd592acc83ba4285e2de2b72456334c72604f9c65e1b90f09c2e7d4096f1`), but no
   upstream release carries the fix and the repository is archived.
3. **`main.py` imports PyQt5 unconditionally**, so even terminal mode requires a
   GUI toolkit in the isolated environment.

The adapter records both reviewed commit identities and hashes an operator-supplied
checkout's `main.py` against them. The recorded digests are over **LF-normalized
text**, which is what git stores and therefore what the same commit yields on every
platform; hashing raw bytes would reject a valid checkout on Windows, where git
hands the file back with CRLF endings. A checkout matching either is reported
`misconfigured` with the specific reason; anything else is `unavailable`. It never
falls back to the AvaFrame port — substituting one implementation for the other is
exactly the confusion these two identities exist to prevent.

### Proving which implementation ran

`runout.avaframe_flowpy` executes the AvaFrame port and writes
`upstream-implementation.json` into every normalized bundle: the provider
(`avaframe.com4FlowPy`), the upstream family (`flow-py`), the AvaFrame version,
and the name, byte size and SHA-256 of **every `.py` and `.ini` file in the
executed `com4FlowPy` package**. That inventory's hash is bound into the result
identity, so a normalized Flow-Py result can be traced to the exact module bytes
that produced it.

## 2. Engine capabilities

| engine | extent | depth | velocity | pressure | energy-line height | travel angle | arrival time |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| `runout.avaframe_com1dfa` | yes | yes | yes | yes | — | not comparable (§2.1) | — |
| `runout.avaframe_flowpy` | yes | — | — | — | yes | yes | — |
| `runout.flowpy_upstream` | (declared) | — | — | — | (declared) | (declared) | — |

A dash is not silence: each engine publishes an `unsupported_outputs` record
naming the quantity and the reason. The important ones:

- **Flow-Py has no flow depth**, because it routes a dimensionless flux and solves
  no depth-averaged mass balance.
- **Flow-Py has no flow velocity.** Its `z_delta` is an *energy-line height*.
  Upstream's own configuration file documents the sliding-block conversion
  `max_v = sqrt(max_z * 19.62)` — that is a bound derived from an assumed friction
  model, not a simulated flow velocity, so it is not published as one.
- **Flow-Py has no arrival time**; it is a time-independent routing model.
- **com1DFA publishes no energy-line height or travel angle in this slice.**
  com1DFA *can* export a peak travel angle (`pta`). It is not the same quantity as
  Flow-Py's `fpTravelAngleMax`, and §2.1 is the characterization that establishes
  that, so the cross-engine travel-angle comparison stays `unsupported` with that
  reason attached.

### 2.1 `pta` is not `fpTravelAngleMax`

Read from the **pinned AvaFrame 2.1 sources themselves**, not from documentation
and not from whether the two produce similar numbers. Digests of the files the
characterization rests on, as installed by `backend/requirements-avaframe.txt`:

| file | SHA-256 |
|---|---|
| `com1DFA/DFAfunctionsCython.pyx` | `24ac032d7456ecc99da92fbdb9405ee09eb9f7fd8a8f84770803c1eeab4bfadc` |
| `com4FlowPy/flowClass.py` | `27a0bfccc04999c1ac5261ac392cb020682eb1928a05eb787e1bce410c6be8a0` |
| `com4FlowPy/flowCore.py` | `b864600db0e9d5a9ddbd7740a8bb3496f92eeadce6a6b1b81d6217e4a68d3b0f` |

**com1DFA `pta`** — `computeTrajectoryAngleC` and `computeFDC` in
`DFAfunctionsCython.pyx`. Per particle,

```text
gamma = atan((z0[parentID] - z) / s)          # degrees
```

`z0[parentID]` is the elevation of the particle's parent **at t = 0**; `s` is
`trajectoryLengthXY`, the particle's *own realized* path length accumulated
step by step as `s += |(x_new - x, y_new - y)|` while it moves. The raster field
is a nearest-cell maximum over the particles present at that step
(`travelAngleField[indy, indx] = max(..., trajectoryAngle)`), and `pta` is then
the maximum of that field **over simulation time**.

**com4FlowPy `fpTravelAngleMax`** — `calcDistMin` / `calc_fp_travelangle` in
`flowClass.py`, aggregated in `flowCore.py`. Per cell,

```text
max_gamma = atan((startcell.altitude - altitude) / min_distance)   # degrees
```

`min_distance` is the **shortest** routed path from the release cell, accumulated
as `min over parents of ( sqrt(Δx² + Δy²) + parent.min_distance )`, where Δx and Δy
are cell-index differences times the cell size — a discrete 8-connected raster
path length. The per-cell value is a maximum over
release cells, and upstream's own docstring states the intent: *"The travel-angle
along the shortest flow-path is equivalent to the maximum travel angle along all
paths from the startcell to this cell."*

They share an algebraic form and nothing else that matters:

| | com1DFA `pta` | com4FlowPy `fpTravelAngleMax` |
|---|---|---|
| horizontal length | realized particle trajectory, integrated over timesteps | shortest 8-connected raster path |
| selection among paths | whichever particles happened to be in the cell | explicitly the **minimum**-length path |
| time | maximum over the simulation | none; the model is time independent |
| what sets the path | Voellmy friction, SPH pressure gradient, entrainment | routing persistence, exponent, alpha |
| discretization | continuous particle positions | cell-centre steps quantized to 1 and √2, which cannot represent an intermediate direction and so over-estimate a straight path's length |
| unreached cells | field initialized to `0`, indistinguishable from a real 0° | initialized to `-9999`, written only where flux arrived |

So a com1DFA value answers *"over the whole simulation, what is the steepest
release-to-here angle any particle that passed through here had actually
travelled?"* and a Flow-Py value answers *"what is the steepest release-to-here
angle achievable along any routed path?"*. The first is a time-peak of a
dynamics-dependent trajectory; the second is a static shortest-path extremum.
One coincidence worth noting so it is not mistaken for agreement: both report
**0° at the release itself** — com1DFA because `s == 0` is special-cased to
`tanGamma = 0`, com4FlowPy because a start cell never calls
`calc_fp_travelangle` and keeps its initialized `max_gamma = 0`.

**Conclusion: not equivalent, so nothing is published.** `pta` stays out of the
requested `resType` list, `runout.avaframe_com1dfa` keeps `travel_angle` in its
`unsupported_outputs` with this reason, and `compare_runout_results` therefore
reports the travel-angle metric as `unsupported` with `value: null` rather than
as a difference between two numbers. Enabling the comparison would require a
stated transformation between the two definitions — not a demonstration that
their numbers happen to be close, which would be exactly the reasoning this
project refuses.

## 3. Normalization rules

**"Unaffected" is a measurement, not a gap.** com4FlowPy writes `-9999` into cells
the process never reached. That is the model saying *no flux routed here*, so those
cells become a valid zero in the energy-line raster and `false` in the runout
extent. Only unknown terrain enters the mask. Getting this backwards would either
turn unknown ground into a confident zero or turn a modelled "no runout" into
"unknown".

**A travel angle is only defined where a path arrived.** The travel-angle field
therefore carries its *own* mask (`terrain_mask | ~reached`), and the adapter
asserts that the unmasked travel-angle domain equals the reached valid domain. A
zero travel angle on an unreached cell would be an invented measurement.

**Single tile, single process.** Upstream tiles the domain and merges overlapping
tiles with max/sum reductions, and distributes release cells over a multiprocessing
pool. The adapter selects one tile that comfortably contains the grid and one
process, which removes the merge reduction from the numerical answer and makes
byte-identical replay achievable. Note that `tileSize` and `tileOverlap` in
`com4FlowPyCfg.ini` are **metres**, not kilometres.

**Forest, infrastructure and variable-parameter modules are disabled**, because
this slice supplies no layer for them.

## 4. Analytical verification

### AvaFrame com1DFA — `avaSimilaritySol`

The official upstream similarity solution passes its preregistered analytical,
mass-balance, grid, unit, CRS, mask and boundary checks. See
[`limitations.md`](limitations.md) and
`validation-data/benchmarks/avaframe-2.1-avaSimilaritySol/`.

### Flow-Py — planar energy line

Flow-Py's routing rule is
`z_delta(next) = z_delta(current) + dz - ds*tan(alpha)`, clipped to `[0, max_z]`.
Summed along a straight path from the release cell the intermediate terms
telescope, so on a planar slope the energy-line height is exactly
`(z_release - z) - s*tan(alpha)` and the flow stops at the last cell where that is
positive. Both are closed form, which makes this an analytical check rather than a
snapshot comparison.

The case is a 35° plane for 60 cells followed by a 5° runout plane on a 5 m grid,
with a single release cell and `alpha = 25°`, `exp = 8`, `flux_threshold = 3e-4`,
`max_z = 8848 m` (chosen so the clip is not exercised). Acceptance thresholds live
in a self-hashed preregistered document that is verified before the engine runs, so
a limit cannot be relaxed after a result has been seen.

| metric | limit | result |
|---|---:|---:|
| energy-line height, max absolute error | 0.01 m | **3.34e-06 m** |
| stopping-row difference | 0 cells | **0 cells** |
| straight-line travel angle, max absolute error | 0.01° | **1.90e-06°** |
| travel length, max absolute error | 0.01 m | **0.0 m** |

Unit, CRS, coordinate-order, mask, domain-truncation and unsupported-output
invariants also passed, and two runs produced the identical `result_id`. The frozen
record is under `validation-data/benchmarks/flowpy-energy-line/`.

Reproduce it with:

```powershell
python scripts\run_flowpy_energy_line_benchmark.py `
  --avaframe-python .venv-avaframe\Scripts\python.exe `
  --output-root <new-output-directory>
```

**This verifies one idealized analytical case in software.** It is not calibration,
not field validation, and not evidence of accuracy at Mount Hosmer or anywhere else.

## 5. Comparing the two engines

`scripts\run_synthetic_engine_comparison.py` and `python -m app.pipeline run` drive
both engines from **one normalized release and one terrain**: com1DFA receives the
release polygons, com4FlowPy receives the release raster, and neither consumes the
other's output. On the synthetic case at the script's default settings (`alpha = 25°`, com1DFA
Voellmy `mu = 0.155`, `xi = 4000 m s-2`, release thickness 0.8 m, density
200 kg m-3, 40 s simulation time, 0.1 s timestep, seed 12345):

| metric | value |
|---|---:|
| extent intersection over union | 0.429 |
| symmetric-difference area | 52 100 m² |
| com1DFA-only area | 17 550 m² |
| Flow-Py-only area | 34 550 m² |
| maximum reach, com1DFA | 455.4 m |
| maximum reach, Flow-Py | 313.2 m |
| maximum reach difference | +142.2 m |
| common valid coverage | 1.00 |

Depth, velocity, pressure and arrival-time comparisons report `unsupported` with
Flow-Py's reason attached, rather than a zero difference.

**These numbers are disagreement, not accuracy.** They say the answer depends
materially on which model is chosen; they do not say either model is right. Both
are uncalibrated here, and agreement between two uncalibrated models would be no
better as evidence.

## 6. r.avaflow

`runout.r_avaflow` remains an availability boundary with no runner: no
version-bound image, exact redistribution licence record, configuration mapping, or
normalized output parser has been reviewed.
