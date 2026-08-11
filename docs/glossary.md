# Digital Twin glossary

Project-specific meanings for terms that affect calculations or API semantics.

## Terrain and coordinates

**AOI (area of interest)**

The fixed 12 x 12 km Mount Hosmer study area. The analysis grid uses EPSG:26911
(UTM zone 11N); API geometry uses WGS84 longitude/latitude.

**DEM (digital elevation model)**

Bare-earth elevation. The bake mosaics BC LiDAR DEM tiles and uses Copernicus
GLO-30 only as gap fill. Elevation and horizontal distances are metres.

**DSM (digital surface model)**

Elevation of ground plus trees and structures. DSM minus DEM is used to estimate
canopy height during the bake.

**Slope**

Terrain steepness in degrees. The release model gives its greatest terrain score
to the 34-45 degree slab-release band. This is an uncalibrated rule, not a local
occurrence probability.

**Aspect**

The compass direction a slope faces, in degrees clockwise from true north: 0 is
north, 90 east, 180 south, and 270 west. Flat or unavailable aspect is `-1`.

**General curvature**

Local surface convexity/concavity. Positive convex rolls modestly increase the
release-capability term.

**Plan curvature**

Cross-slope shape. In this project, negative values are convergent or gully-like
and positive values are divergent. The sign convention is a load-bearing contract
between the bake and the hazard/runout code.

**Mask / NoData**

A cell without a valid required input. Missing cells remain masked; they must not
be replaced by zero or interpreted as safe terrain.

## Conditions and release

**New snow**

A user-supplied scenario depth in centimetres. It is not a live observation.

**Wind direction**

Meteorological `FROM` direction. A 225 degree wind comes from the southwest and
loads slopes facing approximately northeast (the downwind or lee direction).

**Release index**

A deterministic 0-100 relative score combining terrain capability with supplied
snow and wind loading. It is not a probability, forecast, or danger rating.

**Release zone**

A connected patch that passes the model's score, slope, forest, size, aspect, and
elevation segmentation rules. It indicates modeled release-capable terrain, not
that an avalanche will occur.

## Runout

**Runout**

The modeled area reached after release. The application offers a fast routing
engine and an advanced particle ensemble; neither is calibrated to observed Mount
Hosmer avalanches.

**Alpha angle / angle of reach**

The angle from release elevation to the runout toe. The fast engine uses regional
empirical alpha values and shows a farther-reaching uncertainty envelope.

**Voellmy model**

The advanced engine's friction formulation, combining Coulomb friction (`mu`) and
velocity-dependent turbulent resistance (`xi`). Current coefficients are regional
literature defaults, not Mount Hosmer calibration.

**Core vs uncertainty envelope**

The core is the central simulated footprint. The envelope deliberately shows a
wider plausible model spread; it is not a statistical confidence interval.

## System

**Bake**

The only process that reads `DATA/`. It creates masked NumPy terrain layers,
terrain/imagery tiles, metadata, and a coordinate-control lattice under
`runtime/baked/`.

**Baked runtime**

The serving path that reads only generated artifacts. It intentionally has no
rasterio, pyproj, xDEM, or GDAL dependency.

**Grounded assistant**

The optional local Ollama layer. It explains deterministic assessment output or
requests a deterministic what-if run; it does not compute hazard values itself.
