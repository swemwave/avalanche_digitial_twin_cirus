# Limitations (Stage 3)

**Read this before making any claim about what the model can do.** This is a research prototype, not an
operational tool.

## The headline

- **This is not an operational avalanche forecast.** It must never replace Avalanche Canada forecasts or
  field assessment. Every hazard/release number is a **relative index (0–100), never a probability and
  never a forecast**, and carries a disclaimer attached in code (`app/core/model_config.py::DISCLAIMER`).
- **Nothing is calibrated.** The risk model, the release threshold, and the runout alpha angles are
  UNCALIBRATED values from the avalanche-terrain literature and Canadian Rockies practice. **None** is
  fitted to an observed Mount Hosmer avalanche, because **no historical avalanche record exists** for this
  mountain — so the model cannot be, and has not been, validated against ground truth.

## The risk model

- **Conditions are user-supplied slider values, not measurements.** New snow, wind speed, wind direction,
  and release size come from the UI. The app does no weather, snow, or satellite ingestion. A slider
  scenario is a what-if, not an observation.
- **No snowpack profile.** The model has no layer/weak-layer data. It cannot see buried surface hoar,
  facets, or crusts — the mechanism behind most avalanche fatalities. It reasons only about *terrain
  capability* (slope, aspect, curvature, forest) modulated by a *slider loading* term (new snow + wind).
- **It is a release estimate, not an occurrence prediction.** A "release zone" is terrain the model
  considers capable of releasing under the given sliders. It is **not** a statement that an avalanche will
  occur there, and a below-threshold day is **not** a statement that the mountain is safe (see I3 below).

## Runout simulation

- Two engines (fast alpha-angle routing; advanced particle ensemble). Neither is validated against an
  observed Mount Hosmer runout. Alpha angles are published Canadian Rockies ranges, not local
  back-analysis. Every runout carries an explicit uncertainty envelope; a line drawn with false precision
  is worse than no line.
- For interactivity, a synchronous assessment simulates runout only for the highest-scoring zones
  (top-12 fast / top-6 advanced). All release zones are still shown; the others are simply not run out.

## Terrain and the bake

- The 3D mesh and all terrain layers are **baked once, offline**, from 5 m BC-LiDAR (mosaicked to ~99.9 %
  AOI coverage), with Copernicus GLO-30 as gap-fill only. The running service reads no source data.
- The 171 `.laz` point clouds are not used (the DEM rasters are already derived from them). Re-deriving a
  finer-than-1 m surface is out of scope.
- Baked layers load as masked arrays, so a masked/NaN pixel stays missing, not zero.
- **The current bake contains no satellite imagery.** `meta.json` carries no `imagery` key and there is no
  `runtime/baked/imagery/` directory, so the winter Sentinel-2 natural-colour drape described elsewhere is
  absent and the mesh renders with hillshade only. The code degrades gracefully (the imagery layer is
  simply not advertised); restoring it requires re-running the bake against `DATA/`.

## Performance and resource use

- **One assessment peaks at ~1477 MB of resident memory** on the 2400×2400 (5.8 M cell) grid — in both
  `fast` and `advanced` modes, since the peak comes from holding several full-grid float64 arrays at once
  rather than from the runout engine. That is large for a 12×12 km AOI and is the single clearest
  optimisation target in the codebase (candidates: float32 where precision allows, releasing intermediate
  grids earlier, or tiling the release-zone extraction).
- CPython does not return freed arrays to the OS, so a warm process sits near that peak and a *second*
  assessment starts high rather than from cold. Anything hosting this must be sized for the peak, not the
  average: at a 2 GB container limit the process was OOM-killed mid-request, which surfaces to a user as an
  assessment that simply fails.
- A `fast` assessment takes ~9 s on 1 vCPU. `advanced` (particle ensemble) is far slower — `assess.py`
  caps it at 6 zones (`MAX_ADVANCED_ZONES`) against 12 for fast mode, for exactly this reason.
- Assessments are **deterministic**: identical conditions produce an identical hazard score across
  machines and architectures (verified locally and on x86 cloud hardware).

## Deployment

See [`deployment.md`](deployment.md) for the full runbook. Limitations specific to the deployed form:

- **The deployed app is served over plain HTTP.** Adding HTTPS requires a domain and a certificate; until
  then browsers will flag it, and it should not carry anything sensitive.
- **The AI assistant depends on an operator's own machine being awake**, reached through a Cloudflare
  Tunnel. The map, terrain and hazard model are unaffected by its absence; only the assistant degrades,
  to a clean 503. A quick tunnel is also **public and unauthenticated** — the hostname is unguessable but
  anyone who learns it can send prompts to that machine, so it should be raised for a session and taken
  down afterwards.
- The deployed hazard model and the local one are the same code path; the assistant calls the assess
  service rather than computing anything itself, so the single-place-attaches-the-disclaimer property
  (invariant I3, below) holds identically in both shapes.

## Data provenance and safety

- **Missing data is reported as missing, never converted to zero or read as "safe" (invariant I3).** When
  no release zone crosses the threshold, hazard falls back to the 95th percentile of the release estimate
  on avalanche terrain and is labelled as such — a quiet day is a low number with a reason, not a zero.
- **The AI assistant never invents the safety disclaimer or the numbers.** It explains an assessment the
  deterministic code produced, and scenario chat is parse-to-sliders → run the real `/assess` → narrate.
  The disclaimer is appended in code, never generated. Without a local Ollama server the assistant returns
  503 and the rest of the app is unaffected.
- Any output here requires independent validation before any operational use.
