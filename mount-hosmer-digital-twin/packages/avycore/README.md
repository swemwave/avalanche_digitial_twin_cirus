# AvyCore

AvyCore combines deterministic avalanche release scoring, release-zone extraction,
geometry conversion, fast and particle-ensemble runout simulation, and grounded
Ollama assistance in one typed Python distribution.

```python
from avycore.hazard import geometry, risk, runout
from avycore.assistant import chat, explain
from avycore.validation import load_validation_dataset, binary_mask_metrics
```

The model operates on caller-provided terrain and condition interfaces, so it does
not depend on a particular web application or source-data layout.

The validation package supplies a versioned, hash-checked contract for normalized
independent observations and evidence-owned spatial/endpoint metrics. It rasterizes
registered geometries and uncertainty bands on a bake-bound grid, requires complete
holdout cohorts, and hashes deterministic prediction provenance. Synthetic data,
model output, and imagery interpretation cannot be labelled as field validation;
calibration and holdout assignment is enforced at event level. Contract-valid field
data also requires explicit code-reviewed trust registration before a result can be
called independent validation. That registry is empty because the project does not
currently have eligible Mount Hosmer observations, so its characterized benchmarks
are software verification only, not evidence of physical accuracy.

> AvyCore is an experimental research package. Its results are not operational
> avalanche forecasts and must not replace professional forecasts or field assessment.

Maintained by John Stewart and released under the MIT License.
