"""Canonical API contract; FastAPI publishes this and the frontend generates from it."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from avycore.scenario import Scenario


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class GridMeta(ApiModel):
    crs: str
    resolution_m: float
    width: int
    height: int


class TileMeta(ApiModel):
    url_template: str
    tile_size: int
    min_zoom: int
    max_zoom: int
    count: int


class ImageryMeta(TileMeta):
    kind: str
    captured_at_utc: str
    cloud_percent: float
    source_resolution_m: float
    visual_context_only: Literal[True]


class TerrainMeta(ApiModel):
    lidar_fraction: float | None
    valid_fraction: float | None
    effective_source_resolution_m: float | None
    coverage_by_source_label: dict[str, float] = Field(default_factory=dict)
    source_codes: dict[str, str] = Field(default_factory=dict)


class ForestMeta(ApiModel):
    source_codes: dict[str, str] = Field(default_factory=dict)
    coverage_by_source_label: dict[str, float] = Field(default_factory=dict)


class BakeIdentity(ApiModel):
    bake_sha256: str


class ExposureClassMeta(ApiModel):
    name: str
    label: str
    code: int
    weight: float = Field(
        ge=0, le=1, description="Uncalibrated relative consequence weight, never a loss model."
    )
    buffer_m: float | None
    tags: str
    derived: bool
    feature_count: int
    grid_cell_count: int


class ExposureMeta(ApiModel):
    """What the baked exposure layer is, and the boundary it may not cross."""

    url: str = Field(description="Static GeoJSON of the classified display features.")
    attribution: str
    licence: str
    classes: list[ExposureClassMeta] = Field(default_factory=list)
    feature_count: int | None = None
    limitation: str
    used_in_release_model: Literal[False] = False
    used_in_runout_model: Literal[False] = False


class TwinMeta(ApiModel):
    schema_: str = Field(alias="schema")
    disclaimer: str
    grid: GridMeta
    aoi_bbox_wgs84: tuple[float, float, float, float]
    aoi_corners_wgs84: list[tuple[float, float]]
    center_wgs84: tuple[float, float]
    tiles: TileMeta
    imagery: ImageryMeta | None = None
    exposure: ExposureMeta | None = None
    identity: BakeIdentity
    terrain: TerrainMeta
    forest: ForestMeta
    warnings: list[str]


ExposureClass = Literal[
    "inferred_settlement",
    "highway_major",
    "building_mapped",
    "railway",
    "road_local",
    "track_trail",
]


class ExposureFeatureProperties(ApiModel):
    exposure_class: ExposureClass
    exposure_label: str
    exposure_weight: float = Field(ge=0, le=1)
    derived: bool = Field(
        description="True where the outline was inferred, not mapped. Never a survey."
    )
    name: str | None = None
    ref: str | None = None


class ExposureFeature(ApiModel):
    type: Literal["Feature"]
    geometry: dict[str, Any]
    properties: ExposureFeatureProperties


class ExposureFeatureCollection(ApiModel):
    """The static display vectors served from ``runtime/baked/exposure``."""

    type: Literal["FeatureCollection"]
    features: list[ExposureFeature]
    attribution: str
    licence: str
    derived_classes: list[str] = Field(default_factory=list)
    limitation: str


ReleaseSize = Literal["small", "medium", "large", "very_large"]
SimulationMode = Literal["fast", "advanced"]
FlowRegime = Literal["dry_slab", "wet_snow", "powder", "mixed"]


class AssessRequest(BaseModel):
    """Structured scenario or legacy/simple controls, never both.

    Legacy numbers remain as a compatibility adapter for the assistant and older
    clients. Omitted numbers are unknown; they are not replaced with zero.
    """

    model_config = ConfigDict(extra="forbid")

    new_snow_cm: float | None = Field(default=None, ge=0, le=300)
    wind_speed_kmh: float | None = Field(default=None, ge=0, le=200)
    wind_direction_deg: float | None = Field(default=None, ge=0, lt=360)
    release_size: ReleaseSize | None = None
    # Optional active inputs. Omitted means unknown, and an unknown value
    # reproduces the temperature/regime/alpha-free result exactly.
    air_temperature_c: float | None = Field(
        default=None,
        ge=-60,
        le=40,
        description=(
            "Classifies new precipitation as snow or rain (0-2 degC band). Reducing the "
            "dry-slab loading term is not a finding that a warm day is safer."
        ),
    )
    flow_regime: FlowRegime | None = Field(
        default=None,
        description="Selects the published friction set for runout. Uncalibrated, like the default.",
    )
    alpha_angle_override_deg: float | None = Field(
        default=None,
        ge=15,
        le=40,
        description="Sensitivity override of the angle of reach, clamped to a reviewed envelope.",
    )
    simulation_mode: SimulationMode = "fast"
    seed: int | None = None
    scenario: Scenario | None = None

    @model_validator(mode="after")
    def one_input_contract(self) -> "AssessRequest":
        legacy = (
            self.new_snow_cm,
            self.wind_speed_kmh,
            self.wind_direction_deg,
            self.release_size,
            self.air_temperature_c,
            self.flow_regime,
            self.alpha_angle_override_deg,
        )
        if self.scenario is not None and any(value is not None for value in legacy):
            raise ValueError("Supply either scenario or legacy/simple fields, not both.")
        return self


class Conditions(ApiModel):
    new_snow_cm: float
    wind_speed_kmh: float
    wind_direction_deg: float
    wind_direction_compass: str
    release_size: ReleaseSize
    provenance: str
    air_temperature_c: float | None = None
    flow_regime: FlowRegime | None = None
    alpha_angle_override_deg: float | None = None
    precipitation_snow_fraction: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Share of new precipitation classified as snow; null when no temperature given.",
    )
    effective_new_snow_cm: float | None = Field(
        default=None, description="The depth that actually loaded the dry-slab term."
    )


class Polygon(ApiModel):
    type: Literal["Polygon"]
    coordinates: list[list[list[float]]]


class MultiPolygon(ApiModel):
    type: Literal["MultiPolygon"]
    coordinates: list[list[list[list[float]]]]


class LineString(ApiModel):
    type: Literal["LineString"]
    coordinates: list[list[float]]


HazardBasis = Literal[
    "release_reach_exposure",
    "release_and_reach",
    "release_and_exposure",
    "release_only",
]


class HazardComponent(ApiModel):
    """One normalised 0-1 term of the composite index, and whether it exists."""

    value: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Null means unavailable. It never means zero.",
    )
    available: bool
    parts: dict[str, float | None] = Field(default_factory=dict)
    measurements: dict[str, Any] = Field(default_factory=dict)
    unavailable_reason: str | None = None


class ExposureHazardComponent(HazardComponent):
    classes: list[str] = Field(
        default_factory=list,
        description="Exposure class labels under the runout, most consequential first.",
    )


class ReleaseHazardComponent(ApiModel):
    value: float = Field(ge=0, le=1)
    available: Literal[True] = True
    estimated_release_score: float


class ZoneHazardComponents(ApiModel):
    """The decomposed per-zone index, published beside its total."""

    zone_id: str
    hazard_index: float = Field(ge=0, le=100)
    basis: HazardBasis
    basis_label: str
    components_available: dict[str, bool]
    release: ReleaseHazardComponent
    reach: HazardComponent
    exposure: ExposureHazardComponent
    terrain_and_reach_index: float = Field(
        ge=0,
        le=100,
        description="The index before exposure. Exposure can only raise this, never lower it.",
    )
    exposure_uplift_points: float = Field(ge=0)
    is_probability: Literal[False] = False
    is_calibrated: Literal[False] = False


class ReleaseZone(ApiModel):
    zone_id: str
    area_m2: float
    area_hectares: float
    mean_slope_deg: float
    max_slope_deg: float
    dominant_aspect_deg: float | None
    dominant_aspect_compass: str | None
    elevation_min_m: float | None
    elevation_max_m: float | None
    elevation_mean_m: float | None
    forest_fraction: float
    estimated_release_score: float
    main_reasons: list[str]
    hazard_index: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Composite 0-100 relative index for this zone: release x reach, raised by exposure. "
            "Uncalibrated; never a probability or danger rating."
        ),
    )
    hazard_band: str | None = None
    hazard_color: str | None = Field(
        default=None,
        description="Band colour, so the client never re-implements the band thresholds.",
    )
    hazard_components: ZoneHazardComponents | None = None


class ZoneFeature(ApiModel):
    type: Literal["Feature"]
    id: str
    geometry: Polygon | MultiPolygon
    properties: ReleaseZone


class ZoneCollection(ApiModel):
    type: Literal["FeatureCollection"]
    features: list[ZoneFeature]
    zone_count: int
    disclaimer: str
    warnings: list[str]


class HazardDetail(ApiModel):
    method: str
    zone_count: int | None = None
    max_zone_score: float | None = None
    component_contributing_zone_count: dict[str, int] | None = Field(
        default=None,
        description="How many zones contributed each component of the composite index.",
    )
    zone_count_by_basis: dict[str, int] | None = None


class BandThreshold(ApiModel):
    upper: float
    band: str
    color: str


class AreaHazardComponents(ApiModel):
    """The decomposition behind area_hazard_index, and what the mean hid."""

    method: str
    aggregation: str | None = None
    zone_count: int
    zone_count_by_basis: dict[str, int] = Field(default_factory=dict)
    contributing_zone_count: dict[str, int] = Field(default_factory=dict)
    zones_with_mapped_exposure: int | None = None
    area_weighted_component_mean: dict[str, float | None] = Field(default_factory=dict)
    area_weighted_terrain_and_reach_index: float | None = None
    dilution: str | None = None
    comparability: str | None = None
    simulation_cap: str | None = None
    no_zone_fallback: dict[str, Any] | None = None
    exposure_layer: dict[str, Any] = Field(default_factory=dict)
    band_thresholds: list[BandThreshold] = Field(default_factory=list)
    is_probability: Literal[False] = False
    is_calibrated: Literal[False] = False


class ModelIdentity(ApiModel):
    model_version: str
    config_sha256: str = Field(
        description="SHA-256 of the release and runout parameters used for this assessment."
    )
    bake_sha256: str = Field(
        description="SHA-256 of the terrain layers, source lineage, grid, and bake implementation."
    )


class CoverageScope(ApiModel):
    required_layers: list[str]
    grid_cell_count: int
    valid_cell_count: int
    missing_cell_count: int
    valid_fraction: float


class AssessmentCoverage(ApiModel):
    denominator: str
    release_model: CoverageScope
    runout_model: CoverageScope


class ConditionProvenance(ApiModel):
    kind: Literal["legacy_simple_scenario", "structured_observation_scenario"]
    is_measurement: bool
    scenario_sha256: str
    classification: str


class TerrainBakeProvenance(ApiModel):
    bake_sha256: str
    source_fingerprint_sha256: str | None = None
    processing_sha256: str | None = None


class ImageryProvenance(ApiModel):
    role: Literal["visual_context_only"]
    used_in_release_or_runout: Literal[False]


class CategoricalSourceMix(ApiModel):
    footprint_cell_count: int
    known_source_cell_count: int
    missing_source_cell_count: int
    cell_count_by_source_label: dict[str, int]
    fraction_of_footprint_by_source_label: dict[str, float]


class FootprintSourceMix(ApiModel):
    terrain: CategoricalSourceMix
    forest: CategoricalSourceMix


class AssessmentFootprintSourceMix(ApiModel):
    release_zones: FootprintSourceMix
    runout_core: FootprintSourceMix
    runout_envelope: FootprintSourceMix


class AssessmentProvenance(ApiModel):
    conditions: ConditionProvenance
    terrain_bake: TerrainBakeProvenance
    terrain_source_coverage_by_label: dict[str, float]
    forest_source_coverage_by_label: dict[str, float]
    footprint_source_mix: AssessmentFootprintSourceMix
    imagery: ImageryProvenance
    serving_data_source: Literal["runtime_baked_only"]


class UnquantifiedUncertainty(ApiModel):
    quantified: Literal[False]
    reason: str


class RunoutUncertainty(ApiModel):
    kind: str
    is_confidence_interval: Literal[False]
    envelope_includes_core: Literal[True]
    core_area_m2: float | None
    envelope_area_m2: float | None
    interpretation: str


class AssessmentUncertainty(ApiModel):
    conditions: UnquantifiedUncertainty
    release_potential: UnquantifiedUncertainty
    runout: RunoutUncertainty


class FieldValidationStatus(ApiModel):
    status: Literal["unavailable"]
    eligible_observation_count: Literal[0]
    dataset_ids: list[str]
    reason: str


class CalibrationStatus(ApiModel):
    status: Literal["not_calibrated"]
    eligible_observation_count: Literal[0]
    reason: str


class SoftwareVerificationStatus(ApiModel):
    status: Literal["characterized_benchmarks"]
    benchmark_version: str
    scope: list[str]
    interpretation: str


class ValidationDataContractStatus(ApiModel):
    status: Literal["ingestion_scaffolding"]
    schema_version: str
    normalized_projected_coordinates_required: Literal[True]
    explicit_calibration_holdout_partitions: Literal[True]
    canonical_geometry_rasterization: Literal[True]
    prediction_identity_required: Literal[True]
    code_reviewed_dataset_registry_required: Literal[True]
    trusted_dataset_count: int
    end_to_end_field_validation_ready: Literal[False]


class AssessmentValidation(ApiModel):
    field_validation: FieldValidationStatus
    calibration: CalibrationStatus
    software_verification: SoftwareVerificationStatus
    validation_data_contract: ValidationDataContractStatus


class RunoutZone(ApiModel):
    zone_id: str
    runout_area_m2: float | None = None
    horizontal_reach_m: float | None = None
    vertical_drop_m: float | None = None
    max_velocity_ms: float | None = None
    alpha_angle_achieved_deg: float | None = None
    alpha_envelope_exceeded: bool | None = None
    particle_cell_visits: int | None = Field(
        default=None,
        description="Advanced-mode particle visits accumulated across cells and time steps.",
    )
    maximum_particle_visits_per_cell: float | None = None


class Runout(ApiModel):
    status: Literal["computed", "no_release_zone", "unavailable_missing_inputs"]
    core_area_m2: float | None
    uncertainty_area_m2: float | None = Field(
        description=(
            "Total outer sensitivity-envelope area including the central footprint; not a "
            "band-only area or a statistical confidence interval."
        )
    )
    uncertainty_area_definition: str
    uncertainty_is_confidence_interval: Literal[False]
    max_velocity_ms: float | None
    runout_polygons: list[Polygon | MultiPolygon]
    uncertainty_polygons: list[Polygon | MultiPolygon]
    main_paths: list[LineString]
    per_zone: list[RunoutZone]


class ScenarioCompleteness(ApiModel):
    required_parameters: list[str]
    required_input_count: int
    known_required_input_count: int
    unknown_required_parameters: list[str]
    provenance_complete_count: int
    unsupported_supplied_input_count: int


class ScenarioConditionCoverage(ApiModel):
    denominator: str
    grid_cell_count: int
    joint_supported_cell_count: int
    joint_unsupported_cell_count: int
    joint_supported_fraction: float
    per_input: dict[str, dict[str, Any]]
    scope_rasterization_version: str
    cell_inclusion_rule: str


class ScenarioReproducibility(ApiModel):
    scenario_sha256: str
    scenario_contract_sha256: str
    numerical_replay_sha256: str
    canonicalization_version: str
    deterministic: Literal[True]
    model_version: str
    config_sha256: str
    bake_sha256: str
    engine: str
    random_seed: int | None
    statement: str


class ScenarioAdvisory(ApiModel):
    """A deterministic statement about a record the equations cannot use.

    Advisories are derived from what the user recorded; none of them ever changes a
    computed value, which is what ``changed_the_number`` states explicitly.
    """

    advisory_id: str
    severity: Literal["critical", "warning", "note"]
    title: str
    detail: str
    parameters: list[str] = Field(default_factory=list)
    overrides_model: bool = Field(
        description="Standard practice treats this recorded evidence as outranking the model."
    )
    changed_the_number: Literal[False] = False


class AdvisorySummary(ApiModel):
    count: int
    count_by_severity: dict[str, int] = Field(default_factory=dict)
    field_evidence_overrides_model: bool
    statement: str


class ScenarioReport(ApiModel):
    schema_version: str
    mode: Literal["simple", "advanced"]
    classification: Literal[
        "terrain_only",
        "hypothetical",
        "partially_observation_constrained",
        "fully_specified_research_scenario",
    ]
    classification_basis: list[str]
    conditions_used: bool
    result_scope: Literal["whole_area", "supported_area_only", "unavailable_missing_inputs"]
    completeness: ScenarioCompleteness
    condition_coverage: ScenarioConditionCoverage
    inputs: list[dict[str, Any]]
    unsupported_inputs: list[dict[str, Any]]
    advisories: list[ScenarioAdvisory] = Field(default_factory=list)
    advisory_summary: AdvisorySummary | None = None
    assumptions: list[str]
    uncertainty_propagated: Literal[False]
    uncertainty_statement: str
    warnings: list[str]
    limitations: list[str]
    reproducibility: ScenarioReproducibility


class AssessResult(ApiModel):
    schema_version: Literal[3]
    generated_at_utc: str
    duration_seconds: float
    model: ModelIdentity
    conditions: Conditions | None
    simulation_mode: SimulationMode
    engine: str
    release_size: ReleaseSize | None
    random_seed: int | None = Field(
        description="Seed used by advanced particle mode; null for deterministic fast routing."
    )
    release_potential_index: float | None = Field(
        ge=0,
        le=100,
        description="Uncalibrated 0-100 relative index; never a probability or danger rating.",
    )
    release_potential_band: str | None
    release_potential_color: str | None
    hazard_score: float | None = Field(
        deprecated=True,
        description="Legacy alias of release_potential_index; not avalanche danger.",
    )
    hazard_detail: HazardDetail
    risk_level: str | None = Field(deprecated=True, description="Legacy alias of release_potential_band.")
    risk_color: str | None = Field(deprecated=True, description="Legacy alias of release_potential_color.")
    area_hazard_index: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Area-weighted mean of the per-zone composite hazard index (release x reach, raised "
            "by exposure). A DIFFERENT quantity from release_potential_index. Uncalibrated 0-100 "
            "relative index; never a probability, forecast or danger rating. Null when no "
            "release zone crossed the threshold."
        ),
    )
    area_hazard_band: str | None = None
    area_hazard_color: str | None = None
    peak_zone_index: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Highest single-zone index, published so the area mean's dilution is visible.",
    )
    peak_zone_id: str | None = None
    peak_zone_basis: HazardBasis | None = None
    no_zone_release_percentile_index: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "The below-threshold fallback, under its own name: the 95th percentile of the "
            "release estimate on avalanche terrain. Shares no terms with area_hazard_index and "
            "must never be read as one. Null whenever release zones exist."
        ),
    )
    hazard_components: AreaHazardComponents
    release: dict[str, Any]
    release_zones: ZoneCollection
    runout: Runout
    zones: list[ReleaseZone]
    coverage: AssessmentCoverage
    provenance: AssessmentProvenance
    uncertainty: AssessmentUncertainty
    validation: AssessmentValidation
    scenario: ScenarioReport
    warnings: list[str]
    disclaimer: str
    is_probability: Literal[False]
    is_operational_forecast: Literal[False]


class ExplainRequest(BaseModel):
    assessment: AssessResult | dict[str, Any]


class ChatTurn(BaseModel):
    role: str
    text: str


class ChatRequest(BaseModel):
    """A conversational turn.

    ``message`` is bounded because it is the one field that flows into the language
    model's prompt. The body ceiling in ``api.middleware`` sizes the *assessment* --
    a big storm's release-zone and runout GeoJSON -- so without a separate limit here
    a caller could put megabytes of text in front of the model. 4 000 characters is
    far more than any real question and cheap to reject.
    """

    message: str = Field(min_length=1, max_length=4000)
    assessment: AssessResult | dict[str, Any] | None = None
    history: list[ChatTurn] | None = None


class ExplainResult(ApiModel):
    explanation: str
    disclaimer: str
    model: str
    is_operational_forecast: Literal[False]


class ChatResult(ApiModel):
    reply: str
    kind: Literal["scenario", "answer", "chat", "advice"]
    parsed_conditions: Conditions | None
    assessment: AssessResult | None
    disclaimer: str
    model: str
    is_operational_forecast: Literal[False]
