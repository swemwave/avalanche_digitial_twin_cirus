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

## Data provenance and safety

- **Missing data is reported as missing, never converted to zero or read as "safe" (invariant I3).** When
  no release zone crosses the threshold, hazard falls back to the 95th percentile of the release estimate
  on avalanche terrain and is labelled as such — a quiet day is a low number with a reason, not a zero.
- **The AI assistant never invents the safety disclaimer or the numbers.** It explains an assessment the
  deterministic code produced, and scenario chat is parse-to-sliders → run the real `/assess` → narrate.
  The disclaimer is appended in code, never generated. Without a local Ollama server the assistant returns
  503 and the rest of the app is unaffected.
- Any output here requires independent validation before any operational use.
