# Field-validation owner requests

These messages are ready for the project owner to send. They have not been sent. 
They request candidate data only; no returned event becomes evidence until byte/hash 
verification and two independent human reviews pass the frozen protocol. AI output 
cannot count as an independent review.

Current eligible event/path/mountain/storm counts: **0 / 0 / 0 / 0**. Field 
predictions and metrics remain unauthorized.

## Recalculated identities

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `validation-data/experiments/public-data-field-validation-v2.json` | 6956 | `91586bd87f057222268dacfb3651f5dc22c02cd8fc0a26e5cd98f8f7228c2cb9` |
| `validation-data/candidates/public-event-strict-funnel-v5.json` | 441703 | `bf665aba57cf6167b19247a28a9a39e688b7e4105f7b4bbd87164200a2e038c1` |
| `validation-data/candidates/public-validation-source-audit-v2.json` | 30889 | `a4c1ef834d0f1da97f43d939c17da692f355a054066fa28ea85dc5c37e4b66ae` |
| `validation-data/acquisition/field-validation-owner-request-v1.json` | 45001 | `9090b7f66e067bf4161c70b3b7edf0519cdea51909432f3c322f33b7f90eb4b4` |
| `validation-data/acquisition/field-validation-owner-delivery-v1.schema.json` | 23004 | `b0fe1994b3909c89ee07391dc200047f7c7ee38506b51db65f36998267395e9f` |

The complete acquisition/workflow hash inventory is regenerated at 
`validation-data/acquisition/field-validation-acquisition-integrity-v1.json`.

## wsl-slf

Verified: 2026-08-15 UTC date

To: data@slf.ch
Cc: —
Subject: Research data request: dry dense-slab release/runout field cohort

Hello,

I am requesting original field-observation files from WSL Institute for Snow and Avalanche Research SLF for a non-operational research validation of dry dense-slab avalanche runout. The prototype does not replace public avalanche guidance or field assessment, and its scores are relative indices, not probabilities. Please provide all retainable candidate events behind the Sovilla Monte Pizzac 18-event mass-balance study and compatible Vallée de la Sionne/GEODAR campaigns, preferably at least 24 candidates. Please do not select events based on agreement with any model.

For every candidate event, please include the original owner files and a manifest conforming to field-validation-owner-delivery-v1.schema.json. The required files are: (1) the permission/licence record; (2) UTC event-time and dry dense-slab classification evidence; (3) pre-event snow-surface DEM plus CRS, horizontal/vertical datum realization, epoch and transformation record; (4) independently observed release geometry; (5) event-specific slope-normal release-thickness measurement; (6) event-specific release-density measurement; (7) component-attributed terminal dense-flow deposit polygon or endpoint; and (8) survey-coverage and detection/occlusion masks that distinguish observed negatives from unknown areas. Each physical observation needs its method and quantified uncertainty. Missing values must remain missing: please do not infer, substitute, back-calculate, or derive any required observation from a model.

Owner-specific originals requested:
- original event-level Monte Pizzac and Vallée de la Sionne photogrammetric/field release and deposit geometries, not digitized figures
- slope-normal crown/fracture-depth and release-density measurement sheets with sampling layout and uncertainty
- pre-event snow surfaces, post-event surveys, control points, calibration and coordinate-operation records
- survey footprints, occlusion/detection masks, terminal dense-flow attribution, event clocks and any clock-alignment residuals

For every delivered file, please give the unchanged relative path, byte count, lowercase SHA-256, stable source URI, copyright holder, licence name/URI, permitted use, redistribution status, and the path/hash of the licence or written-permission record covering that file. The official SLF data-service page states CC BY 4.0 for its service data; please confirm in writing whether that licence covers every underlying file in this special delivery and provide any additional or different terms file-by-file. The requested permission should explicitly cover private research storage, verification, format-preserving normalization, independent human review, deterministic calibration/holdout evaluation, publication of derived aggregate metrics/figures, reproducibility archiving, and whether original bytes may be redistributed. If redistribution is not permitted, we will keep originals private and publish only permitted metadata, hashes, and derived results.

We need at least 12 eligible events after two independent human reviews, spanning at least six paths, two mountains, and three storm cycles; 24–40 candidates are preferred because incomplete events will be excluded. Every event in the verified delivery cohort will be adjudicated, including exclusions, so none can be silently selected or omitted. Reviewer identity-verification records will be hashed; AI output never counts as a review, and disagreements require a separately verified third human. Please provide owner-proposed path/mountain/storm grouping evidence but no calibration or holdout labels. Holdout observations will remain sealed during calibration.

Thank you.

Official route verification:

- https://www.slf.ch/fr/services-et-produits/service-de-donnees-du-slf/ — Official SLF data-service page names data@slf.ch for data delivery questions and states the service's CC BY 4.0 terms.

## inrae-lautaret

Verified: 2026-08-15 UTC date

To: florence.naaim@inrae.fr, herve.bellot@inrae.fr
Cc: —
Subject: Research data request: dry dense-slab release/runout field cohort

Hello,

I am requesting original field-observation files from INRAE/IGE Lautaret avalanche test site for a non-operational research validation of dry dense-slab avalanche runout. The prototype does not replace public avalanche guidance or field assessment, and its scores are relative indices, not probabilities. Please provide all dry dense-slab Lautaret events with paired snow pits, release-state surveys, pre/post laser products and terminal runout observations, preferably at least 24 candidates across the site's distinct paths and storm cycles. Please do not select events based on agreement with any model.

For every candidate event, please include the original owner files and a manifest conforming to field-validation-owner-delivery-v1.schema.json. The required files are: (1) the permission/licence record; (2) UTC event-time and dry dense-slab classification evidence; (3) pre-event snow-surface DEM plus CRS, horizontal/vertical datum realization, epoch and transformation record; (4) independently observed release geometry; (5) event-specific slope-normal release-thickness measurement; (6) event-specific release-density measurement; (7) component-attributed terminal dense-flow deposit polygon or endpoint; and (8) survey-coverage and detection/occlusion masks that distinguish observed negatives from unknown areas. Each physical observation needs its method and quantified uncertainty. Missing values must remain missing: please do not infer, substitute, back-calculate, or derive any required observation from a model.

Owner-specific originals requested:
- raw and processed pre/post-event laser scans, snow-surface DEMs, control networks and transformation/calibration records
- original release, terminal deposit and endpoint mappings plus the complete searched survey domain
- event snow-pit slope-normal release thickness and density sheets with uncertainty and sampling times
- exact UTC trigger/release logs, path and storm-cycle evidence, imagery/photogrammetry products and detection/occlusion masks

For every delivered file, please give the unchanged relative path, byte count, lowercase SHA-256, stable source URI, copyright holder, licence name/URI, permitted use, redistribution status, and the path/hash of the licence or written-permission record covering that file. Please state the applicable INRAE licence for the underlying event files or include signed written research permission; a publication's licence will not be assumed to cover unpublished measurements. The requested permission should explicitly cover private research storage, verification, format-preserving normalization, independent human review, deterministic calibration/holdout evaluation, publication of derived aggregate metrics/figures, reproducibility archiving, and whether original bytes may be redistributed. If redistribution is not permitted, we will keep originals private and publish only permitted metadata, hashes, and derived results.

We need at least 12 eligible events after two independent human reviews, spanning at least six paths, two mountains, and three storm cycles; 24–40 candidates are preferred because incomplete events will be excluded. Every event in the verified delivery cohort will be adjudicated, including exclusions, so none can be silently selected or omitted. Reviewer identity-verification records will be hashed; AI output never counts as a review, and disagreements require a separately verified third human. Please provide owner-proposed path/mountain/storm grouping evidence but no calibration or holdout labels. Holdout observations will remain sealed during calibration.

Thank you.

Official route verification:

- https://monitoring-stations.ara.inrae.fr/ — Official INRAE monitoring portal directs snow-site data requests to Florence Naaim and Herve Bellot.
- https://www.inrae.fr/en/reports/understanding-avalanche-risk/multi-faceted-research — Official INRAE description identifies the Lautaret avalanche archive and measurements.

## ngi-ryggfonn

Verified: 2026-08-15 UTC date

To: heidi.hefre@ngi.no, peter.gauer@ngi.no
Cc: ngi@ngi.no
Subject: Research data request: dry dense-slab release/runout field cohort

Hello,

I am requesting original field-observation files from Norwegian Geotechnical Institute Ryggfonn programme for a non-operational research validation of dry dense-slab avalanche runout. The prototype does not replace public avalanche guidance or field assessment, and its scores are relative indices, not probabilities. Please provide the strongest complete dry dense-slab candidates from the full Ryggfonn archive, including the 2023/24 and 2024/25 campaigns, preferably 24 candidates before eligibility attrition. Please do not select events based on agreement with any model.

For every candidate event, please include the original owner files and a manifest conforming to field-validation-owner-delivery-v1.schema.json. The required files are: (1) the permission/licence record; (2) UTC event-time and dry dense-slab classification evidence; (3) pre-event snow-surface DEM plus CRS, horizontal/vertical datum realization, epoch and transformation record; (4) independently observed release geometry; (5) event-specific slope-normal release-thickness measurement; (6) event-specific release-density measurement; (7) component-attributed terminal dense-flow deposit polygon or endpoint; and (8) survey-coverage and detection/occlusion masks that distinguish observed negatives from unknown areas. Each physical observation needs its method and quantified uncertainty. Missing values must remain missing: please do not infer, substitute, back-calculate, or derive any required observation from a model.

Owner-specific originals requested:
- source GIS, pre-event laser snow surfaces, post-event deposit surveys and field notes behind report figures
- release geometry, slope-normal release thickness, release density and their sampling/uncertainty records
- survey footprints and known-absence/detection/occlusion masks with terminal dense-flow component attribution
- raw radar, pressure and velocity files with sensor coordinates, calibration, clock synchronization, valid intervals and uncertainty

For every delivered file, please give the unchanged relative path, byte count, lowercase SHA-256, stable source URI, copyright holder, licence name/URI, permitted use, redistribution status, and the path/hash of the licence or written-permission record covering that file. The public annual reports do not establish reusable rights for the underlying measurements; please include the exact licence or written research permission covering every delivered source file. The requested permission should explicitly cover private research storage, verification, format-preserving normalization, independent human review, deterministic calibration/holdout evaluation, publication of derived aggregate metrics/figures, reproducibility archiving, and whether original bytes may be redistributed. If redistribution is not permitted, we will keep originals private and publish only permitted metadata, hashes, and derived results.

We need at least 12 eligible events after two independent human reviews, spanning at least six paths, two mountains, and three storm cycles; 24–40 candidates are preferred because incomplete events will be excluded. Every event in the verified delivery cohort will be adjudicated, including exclusions, so none can be silently selected or omitted. Reviewer identity-verification records will be hashed; AI output never counts as a review, and disagreements require a separately verified third human. Please provide owner-proposed path/mountain/storm grouping evidence but no calibration or holdout labels. Holdout observations will remain sealed during calibration.

Thank you.

Official route verification:

- https://www.ngi.no/en/research-and-consulting/natural-hazards-container/avalanches-and-slides/avalanches-and-slush-flows/ryggfonn/ — Official NGI Ryggfonn page names Heidi Hefre and Peter Gauer and describes the current instrumented archive.

## seehore-partners

Verified: 2026-08-15 UTC date

To: michele.freppaz@unito.it, monica.barbero@polito.it
Cc: —
Subject: Research data request: dry dense-slab release/runout field cohort

Hello,

I am requesting original field-observation files from Universita di Torino / Politecnico di Torino Seehore partners for a non-operational research validation of dry dense-slab avalanche runout. The prototype does not replace public avalanche guidance or field assessment, and its scores are relative indices, not probabilities. Please provide every dry dense-slab Seehore event for which original release, snow-property, laser/GPS and terminal-deposit records survive; please do not limit the delivery to the five published impact events. Please do not select events based on agreement with any model.

For every candidate event, please include the original owner files and a manifest conforming to field-validation-owner-delivery-v1.schema.json. The required files are: (1) the permission/licence record; (2) UTC event-time and dry dense-slab classification evidence; (3) pre-event snow-surface DEM plus CRS, horizontal/vertical datum realization, epoch and transformation record; (4) independently observed release geometry; (5) event-specific slope-normal release-thickness measurement; (6) event-specific release-density measurement; (7) component-attributed terminal dense-flow deposit polygon or endpoint; and (8) survey-coverage and detection/occlusion masks that distinguish observed negatives from unknown areas. Each physical observation needs its method and quantified uncertainty. Missing values must remain missing: please do not infer, substitute, back-calculate, or derive any required observation from a model.

Owner-specific originals requested:
- original GPS/RTK release and terminal-deposit perimeters, benchmarks and transformations
- pre/post laser scans, event snow-surface products, raw imagery and instrument calibration/accuracy records
- normal-to-slope release depth and event-specific release density with sampling locations and uncertainty
- UTC event/trigger logs, pressure records, survey domain and detection/occlusion masks with dense-flow attribution

For every delivered file, please give the unchanged relative path, byte count, lowercase SHA-256, stable source URI, copyright holder, licence name/URI, permitted use, redistribution status, and the path/hash of the licence or written-permission record covering that file. The open-access article licence does not by itself license the underlying event measurements; please identify the owner of each file and provide an applicable data licence or written research permission. The requested permission should explicitly cover private research storage, verification, format-preserving normalization, independent human review, deterministic calibration/holdout evaluation, publication of derived aggregate metrics/figures, reproducibility archiving, and whether original bytes may be redistributed. If redistribution is not permitted, we will keep originals private and publish only permitted metadata, hashes, and derived results.

We need at least 12 eligible events after two independent human reviews, spanning at least six paths, two mountains, and three storm cycles; 24–40 candidates are preferred because incomplete events will be excluded. Every event in the verified delivery cohort will be adjudicated, including exclusions, so none can be silently selected or omitted. Reviewer identity-verification records will be hashed; AI output never counts as a review, and disagreements require a separately verified third human. Please provide owner-proposed path/mountain/storm grouping evidence but no calibration or holdout labels. Holdout observations will remain sealed during calibration.

Thank you.

Official route verification:

- https://unifind.unito.it/individual?uri=http%3A%2F%2Firises.unito.it%2Fresource%2Fperson%2F9997 — Current official Universita di Torino profile for Michele Freppaz.
- https://www.polito.it/personale?p=monica.barbero — Current official Politecnico di Torino profile for Monica Barbero.
- https://iris.polito.it/handle/11583/2765872 — Official institutional record for the five-event Seehore measurements paper.

## bfw-wlv-avaframedata

Verified: 2026-08-15 UTC date

To: felix.oesterle@bfw.gv.at, schneelawine@die-wildbach.at
Cc: anna.wirbel@bfw.gv.at, frank.perzl@bfw.gv.at
Subject: Research data request: dry dense-slab release/runout field cohort

Hello,

I am requesting original field-observation files from BFW Snow & Avalanches and WLV Fachbereich Lawinen for a non-operational research validation of dry dense-slab avalanche runout. The prototype does not replace public avalanche guidance or field assessment, and its scores are relative indices, not probabilities. Please provide original pre-conversion records for the six AvaFrameData v1.0 events and additional complete events from independent mountains, preferably 24 candidates before review attrition. Please do not select events based on agreement with any model.

For every candidate event, please include the original owner files and a manifest conforming to field-validation-owner-delivery-v1.schema.json. The required files are: (1) the permission/licence record; (2) UTC event-time and dry dense-slab classification evidence; (3) pre-event snow-surface DEM plus CRS, horizontal/vertical datum realization, epoch and transformation record; (4) independently observed release geometry; (5) event-specific slope-normal release-thickness measurement; (6) event-specific release-density measurement; (7) component-attributed terminal dense-flow deposit polygon or endpoint; and (8) survey-coverage and detection/occlusion masks that distinguish observed negatives from unknown areas. Each physical observation needs its method and quantified uncertainty. Missing values must remain missing: please do not infer, substitute, back-calculate, or derive any required observation from a model.

Owner-specific originals requested:
- source GIS and exact CRS operations, especially the Filisur EPSG:21781-to-EPSG:2056 operation
- original WLV event records 6534 and 6591, Eiskar drone/laser products, and Arzl UAS imagery, point cloud, orthophoto, surface model and deposit geometry
- event-specific slope-normal release thickness, density, uncertainty, UTC timing and pre-event snow surfaces
- survey known-absence domains, detection/occlusion masks and source-to-AvaFrameData derivation records

For every delivered file, please give the unchanged relative path, byte count, lowercase SHA-256, stable source URI, copyright holder, licence name/URI, permitted use, redistribution status, and the path/hash of the licence or written-permission record covering that file. The public AvaFrameData record states CC BY 4.0; please confirm whether that licence covers each original pre-conversion BFW/WLV source file and supply different owner permissions where it does not. The requested permission should explicitly cover private research storage, verification, format-preserving normalization, independent human review, deterministic calibration/holdout evaluation, publication of derived aggregate metrics/figures, reproducibility archiving, and whether original bytes may be redistributed. If redistribution is not permitted, we will keep originals private and publish only permitted metadata, hashes, and derived results.

We need at least 12 eligible events after two independent human reviews, spanning at least six paths, two mountains, and three storm cycles; 24–40 candidates are preferred because incomplete events will be excluded. Every event in the verified delivery cohort will be adjudicated, including exclusions, so none can be silently selected or omitted. Reviewer identity-verification records will be hashed; AI output never counts as a review, and disagreements require a separately verified third human. Please provide owner-proposed path/mountain/storm grouping evidence but no calibration or holdout labels. Holdout observations will remain sealed during calibration.

Thank you.

Official route verification:

- https://www.bfw.gv.at/en/departments-en/natural-hazards/snow-avalanches/ — Current official BFW Snow & Avalanches directory lists Felix Oesterle, Anna Wirbel, and Frank Perzl.
- https://www.bmluk.gv.at/themen/wald/wald-und-naturgefahren/wildbach--und-lawinenverbauung/organisation-kontakt/fz_geologie_lawinen.html — Official WLV avalanche centre lists schneelawine@die-wildbach.at and describes its documented-event data pool.

## parks-canada-rogers-pass

Verified: 2026-08-15 UTC date

To: mrg.information@pc.gc.ca
Cc: —
Subject: Research data request: dry dense-slab release/runout field cohort

Hello,

I am requesting original field-observation files from Parks Canada Glacier National Park / Rogers Pass avalanche programme for a non-operational research validation of dry dense-slab avalanche runout. The prototype does not replace public avalanche guidance or field assessment, and its scores are relative indices, not probabilities. Please refer this request to the Rogers Pass avalanche-program data steward and provide a de-identified cohort across independent named paths and storm cycles, preferably 24–40 candidates. Please do not select events based on agreement with any model.

For every candidate event, please include the original owner files and a manifest conforming to field-validation-owner-delivery-v1.schema.json. The required files are: (1) the permission/licence record; (2) UTC event-time and dry dense-slab classification evidence; (3) pre-event snow-surface DEM plus CRS, horizontal/vertical datum realization, epoch and transformation record; (4) independently observed release geometry; (5) event-specific slope-normal release-thickness measurement; (6) event-specific release-density measurement; (7) component-attributed terminal dense-flow deposit polygon or endpoint; and (8) survey-coverage and detection/occlusion masks that distinguish observed negatives from unknown areas. Each physical observation needs its method and quantified uncertainty. Missing values must remain missing: please do not infer, substitute, back-calculate, or derive any required observation from a model.

Owner-specific originals requested:
- original event-level field release/deposit polygons or surveyed terminal endpoints and explicit searched survey coverage
- pre-event snow-surface terrain, slope-normal release thickness, release density, UTC control/event timing and uncertainty
- path, mountain and storm-cycle grouping evidence without personal information
- detection/occlusion masks, observation methods, control records and immutable source-file inventory

For every delivered file, please give the unchanged relative path, byte count, lowercase SHA-256, stable source URI, copyright holder, licence name/URI, permitted use, redistribution status, and the path/hash of the licence or written-permission record covering that file. Please provide the applicable Government of Canada/Parks Canada data licence or written research permission for these non-public operational records, including any confidentiality, attribution and redistribution restrictions. The requested permission should explicitly cover private research storage, verification, format-preserving normalization, independent human review, deterministic calibration/holdout evaluation, publication of derived aggregate metrics/figures, reproducibility archiving, and whether original bytes may be redistributed. If redistribution is not permitted, we will keep originals private and publish only permitted metadata, hashes, and derived results.

We need at least 12 eligible events after two independent human reviews, spanning at least six paths, two mountains, and three storm cycles; 24–40 candidates are preferred because incomplete events will be excluded. Every event in the verified delivery cohort will be adjudicated, including exclusions, so none can be silently selected or omitted. Reviewer identity-verification records will be hashed; AI output never counts as a review, and disagreements require a separately verified third human. Please provide owner-proposed path/mountain/storm grouping evidence but no calibration or holdout labels. Holdout observations will remain sealed during calibration.

Thank you.

Official route verification:

- https://www.parks.canada.ca/pn-np/bc/glacier/info/contact — Current official Glacier National Park contact page lists mrg.information@pc.gc.ca and the Revelstoke office.
- https://parks.canada.ca/pn-np/mtn/securiteenmontagne-mountainsafety/avalanche/routes-highways.aspx — Official Parks Canada page describes the Rogers Pass highway programme and 135 avalanche paths.
