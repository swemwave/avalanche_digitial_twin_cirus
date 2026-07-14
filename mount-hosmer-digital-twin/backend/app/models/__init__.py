"""Scientific models. Each takes terrain + conditions and returns scored rasters.

Every model in this package obeys three rules:

1. It never invents a value. Missing input -> the output says "unavailable",
   and the component is dropped from BOTH the numerator and the denominator of
   any weighted score. Missing snowfall is not zero snowfall.
2. It never reports a probability. Outputs are relative indices, 0-100 or 0-1,
   because nothing here has been calibrated against an observed avalanche.
3. It carries provenance. Every output records whether it came from an
   observation, an interpolation, or a model.
"""
