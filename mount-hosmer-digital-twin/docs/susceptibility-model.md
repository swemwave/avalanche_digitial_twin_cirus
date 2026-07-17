# Susceptibility Model

> ⚠️ **Superseded (pre-Stage-3).** The dynamic/combined susceptibility model this describes was **removed**
> in Stage 3 (it depended on weather/snow/satellite ingestion, all cut). The current risk model is the
> simplified, slider-driven release estimate in `backend/app/risk.py`; see [`limitations.md`](limitations.md).
> Kept for history only.

The susceptibility module is an explainable rules-based research prototype. It is not a validated avalanche forecast, avalanche detector, or supervised machine-learning classifier.

All outputs must display this disclaimer:

```text
Experimental research prototype. This output has not been validated for operational avalanche forecasting and must not be used as a replacement for professional avalanche forecasts or field assessment.
```

## Configuration

Weights are stored in:

```text
backend\config\susceptibility_weights.yaml
```

The API reports the configuration file hash in each event susceptibility summary.

## Static Terrain Susceptibility

Static terrain susceptibility is a 0-100 raster derived from:

- slope
- elevation
- aspect
- ESA WorldCover land cover
- DEM ruggedness
- DEM curvature approximation
- ridge/gully topographic-position proxy

The output is:

```text
runtime\processed\static\terrain_susceptibility.tif
```

Slope is the dominant factor. The highest terrain score is assigned to avalanche-relevant slope angles, with open and bare terrain scoring higher than forested or water classes.

## Dynamic Condition Index

The dynamic condition index is event-specific and uses available event-window summaries:

- ECCC recent snowfall
- ECCC recent precipitation
- BC snow-station SWE change
- BC snow-station snow-depth change
- ECCC rapid warming
- ECCC wind speed or gust
- satellite snow-cover percentage
- Landsat surface-temperature signal
- Avalanche Canada current forecast context, recorded but not scored for historical event dates

Each component records:

- source
- timestamp
- units
- original value
- normalized value
- weight
- weighted value
- missing-data status
- explanation

Missing values are not converted to zero. They are excluded from the available-weight denominator and are listed in warnings.

## Combined Prototype Index

When dynamic input coverage meets the configured minimum, the combined raster is:

```text
combined_index =
terrain_susceptibility * terrain_weight
+ dynamic_condition_index * dynamic_weight
```

Default weights:

```yaml
combined:
  terrain_weight: 0.65
  dynamic_weight: 0.35
```

Generated per-event outputs:

```text
runtime\processed\events\<event_id>\susceptibility_summary.json
runtime\processed\events\<event_id>\combined_susceptibility.tif
runtime\processed\events\<event_id>\combined_susceptibility.metadata.json
runtime\previews\events\<event_id>\combined_susceptibility.png
```

## Current Event Scores

The current local run generated combined outputs for:

- `MH_20260116T183016Z`
- `MH_20260430T182949Z`

The scores are research indices only. They indicate where static terrain susceptibility overlaps with available event-condition signals; they do not confirm that an avalanche occurred.
