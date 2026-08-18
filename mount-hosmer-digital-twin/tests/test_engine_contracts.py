"""Contracts and deterministic selection for portable avalanche engines."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from avycore.engines import (
    AVAFRAME_COM1DFA,
    AVAFRAME_FLOWPY,
    BC_PRA,
    DISCLAIMER,
    ENGINE_CONTRACT_SCHEMA_VERSION,
    NORMALIZED_RESULT_SCHEMA_VERSION,
    R_AVAFLOW,
    ArtifactRef,
    AvailabilityStatus,
    AvalancheRegime,
    CRSContract,
    DeclaredInput,
    EngineAvailability,
    EngineRegistry,
    EngineRunRequest,
    EngineSelectionError,
    EngineStage,
    ExecutionBoundary,
    GridContract,
    InputKind,
    MaskContract,
    NormalizedRunoutResult,
    OutputQuantity,
    RasterField,
    RunProvenance,
    SelectionPolicy,
    StaticEnginePlugin,
    build_result,
    canonical_engine_registry,
)


HASH = "1" * 64


def _artifact(uri: str) -> ArtifactRef:
    return ArtifactRef(uri=uri, sha256=HASH, byte_size=1, media_type="application/octet-stream")


def _crs(definition: str = "EPSG:32611") -> CRSContract:
    return CRSContract(
        definition=definition,
        projected=True,
        horizontal_unit="m",
        coordinate_order="x,y",
        vertical_datum=None,
        vertical_datum_status="unknown",
    )


def _grid(crs: CRSContract | None = None) -> GridContract:
    return GridContract(
        crs=crs or _crs(),
        shape=(2, 3),
        affine_transform=(5.0, 0.0, 500000.0, 0.0, -5.0, 5500000.0),
        cell_size_x_m=5.0,
        cell_size_y_m=5.0,
        origin_semantics="upper_left_outer_corner",
    )


def _mask(grid: GridContract | None = None) -> MaskContract:
    selected_grid = grid or _grid()
    return MaskContract(
        artifact=_artifact("mask.npy"),
        valid_cells=selected_grid.shape[0] * selected_grid.shape[1],
        masked_cells=0,
        combined_from=("source_nodata",),
    )


def _scalar(name: str, value: float | bool, unit: str | None) -> DeclaredInput:
    return DeclaredInput(
        name=name,
        kind=InputKind.FLAG if isinstance(value, bool) else InputKind.SCALAR,
        unit=unit,
        value=value,
        status="provided",
        source_sha256=hashlib.sha256(name.encode()).hexdigest(),
    )


def _avaframe_request(*, omit: str | None = None, regime: AvalancheRegime = AvalancheRegime.DENSE_DRY):
    grid = _grid()
    values = [
        DeclaredInput(
            name="terrain_dem",
            kind=InputKind.RASTER,
            unit="m",
            artifact=_artifact("dem.tif"),
            grid=grid,
            mask=_mask(grid),
            status="provided",
            source_sha256=HASH,
        ),
        DeclaredInput(
            name="release_area",
            kind=InputKind.VECTOR,
            artifact=_artifact("release.geojson"),
            crs=grid.crs,
            status="provided",
            source_sha256=HASH,
        ),
        _scalar("release_thickness", 0.8, "m"),
        _scalar("release_density", 200.0, "kg m-3"),
        _scalar("voellmy_mu", 0.155, "1"),
        _scalar("voellmy_xi", 4000.0, "m s-2"),
        _scalar("entrainment_enabled", False, None),
        _scalar("simulation_time", 40.0, "s"),
        _scalar("time_step", 0.1, "s"),
    ]
    return EngineRunRequest(
        schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
        site_id="synthetic.utm11",
        research_disclaimer=DISCLAIMER,
        stage=EngineStage.RUNOUT,
        regime=regime,
        inputs=tuple(item for item in values if item.name != omit),
        requested_outputs=(OutputQuantity.RUNOUT_EXTENT, OutputQuantity.FLOW_DEPTH),
        requested_engine_id=AVAFRAME_COM1DFA.engine_id,
        scenario_sha256=HASH,
        seed=12345,
    )


def _available_plugin() -> StaticEnginePlugin:
    return StaticEnginePlugin(
        AVAFRAME_COM1DFA,
        EngineAvailability(
            engine_id=AVAFRAME_COM1DFA.engine_id,
            status=AvailabilityStatus.AVAILABLE,
            reason="test environment",
            detected_version="2.1",
            executable_sha256=HASH,
        ),
    )


def test_coordinate_order_and_projected_units_are_explicit():
    with pytest.raises(ValidationError, match="x,y order"):
        CRSContract(
            definition="EPSG:32611",
            projected=True,
            horizontal_unit="m",
            coordinate_order="longitude,latitude",
            vertical_datum=None,
            vertical_datum_status="unknown",
        )
    with pytest.raises(ValidationError, match="longitude,latitude"):
        CRSContract(
            definition="EPSG:4326",
            projected=False,
            horizontal_unit="degree",
            coordinate_order="x,y",
            vertical_datum=None,
            vertical_datum_status="unknown",
        )


def test_raster_inputs_require_grid_and_explicit_unknown_mask():
    with pytest.raises(ValidationError, match="explicit grid and mask"):
        DeclaredInput(
            name="terrain_dem",
            kind=InputKind.RASTER,
            unit="m",
            artifact=_artifact("dem.tif"),
            status="provided",
            source_sha256=HASH,
        )


def test_normalized_units_and_mask_population_are_enforced():
    grid = _grid()
    with pytest.raises(ValidationError, match="requires unit 'm'"):
        RasterField(
            quantity=OutputQuantity.FLOW_DEPTH,
            unit="cm",
            artifact=_artifact("depth.npy"),
            mask=_mask(grid),
            grid=grid,
            dtype="float32",
            semantics="test",
        )
    with pytest.raises(ValidationError, match="Mask cell counts"):
        RasterField(
            quantity=OutputQuantity.FLOW_DEPTH,
            unit="m",
            artifact=_artifact("depth.npy"),
            mask=MaskContract(
                artifact=_artifact("mask.npy"),
                valid_cells=2,
                masked_cells=2,
                combined_from=("source",),
            ),
            grid=grid,
            dtype="float32",
            semantics="test",
        )


def test_selection_is_deterministic_and_registration_order_independent():
    request = _avaframe_request()
    reports = []
    for plugins in ((_available_plugin(),), tuple(reversed((_available_plugin(),)))):
        registry = EngineRegistry()
        for plugin in plugins:
            registry.register(plugin)
        selected, report = registry.select(
            request,
            policy=SelectionPolicy(engine_order=(AVAFRAME_COM1DFA.engine_id,)),
        )
        assert selected.descriptor.engine_id == AVAFRAME_COM1DFA.engine_id
        reports.append(report)
    assert reports[0] == reports[1]


def test_missing_physical_input_fails_visibly_without_fallback():
    registry = EngineRegistry()
    registry.register(_available_plugin())
    with pytest.raises(EngineSelectionError, match="release_density") as caught:
        registry.select(_avaframe_request(omit="release_density"))
    assert caught.value.report.decisions[-1].status == "missing_or_invalid_inputs"


def test_unsupported_regime_fails_visibly():
    registry = EngineRegistry()
    registry.register(_available_plugin())
    with pytest.raises(EngineSelectionError, match="No engine declares") as caught:
        registry.select(_avaframe_request(regime=AvalancheRegime.POWDER_CLOUD))
    assert caught.value.report.decisions[-1].status == "unsupported_regime"


def test_declared_parameter_and_spatial_applicability_are_machine_checked():
    bad_range = _avaframe_request()
    payload = bad_range.model_dump(mode="json")
    for item in payload["inputs"]:
        if item["name"] == "release_thickness":
            item["value"] = 0.0
    bad_range = EngineRunRequest.model_validate(payload)
    registry = EngineRegistry()
    registry.register(_available_plugin())
    with pytest.raises(EngineSelectionError, match="outside declared range"):
        registry.select(bad_range)

    other_crs_payload = _avaframe_request().model_dump(mode="json")
    for item in other_crs_payload["inputs"]:
        if item["name"] == "release_area":
            item["crs"]["definition"] = "EPSG:26911"
    with pytest.raises(EngineSelectionError, match="identical CRS"):
        registry.select(EngineRunRequest.model_validate(other_crs_payload))


def test_unavailable_engines_publish_specific_reasons_and_never_placeholder_physics():
    inventory = {
        descriptor.engine_id: availability
        for descriptor, availability in canonical_engine_registry().inventory()
    }
    for descriptor in (BC_PRA, AVAFRAME_COM1DFA, AVAFRAME_FLOWPY, R_AVAFLOW):
        availability = inventory[descriptor.engine_id]
        assert availability.status == AvailabilityStatus.UNAVAILABLE
        assert len(availability.reason) >= 30
        assert availability.detected_version is None


def test_normalized_result_identity_is_content_addressed():
    grid = _grid()
    field = RasterField(
        quantity=OutputQuantity.RUNOUT_EXTENT,
        unit="1",
        artifact=_artifact("runout.npy"),
        mask=_mask(grid),
        grid=grid,
        dtype="bool",
        valid_min=0.0,
        valid_max=1.0,
        semantics="positive flow thickness",
    )
    content = {
        "schema_version": NORMALIZED_RESULT_SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "site_id": "synthetic.utm11",
        "stage": EngineStage.RUNOUT,
        "regime": AvalancheRegime.DENSE_DRY,
        "provenance": RunProvenance(
            engine_id=AVAFRAME_COM1DFA.engine_id,
            engine_version="2.1",
            adapter_version="test",
            license_spdx="EUPL-1.2",
            execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
            executable_sha256=HASH,
            environment_sha256=HASH,
            adapter_sha256=HASH,
            selection_sha256=HASH,
            configuration_sha256=HASH,
            input_manifest_sha256=HASH,
            output_manifest_sha256=HASH,
            scenario_sha256=HASH,
            seed=1,
            source_urls=(AVAFRAME_COM1DFA.source_url,),
        ),
        "validation": AVAFRAME_COM1DFA.validation,
        "uncertainty": (),
        "warnings": (),
        "limitations": ("test output only",),
        "runout_extent": field,
        "runout_polygons": None,
        "flow_depth": None,
        "flow_velocity": None,
        "flow_pressure": None,
        "runout_area_m2": 25.0,
        "aoi_status": "complete_within_domain",
    }
    first = build_result(NormalizedRunoutResult, content)
    second = build_result(NormalizedRunoutResult, content)
    assert first.result_id == second.result_id
    tampered = first.model_dump(mode="json")
    tampered["runout_area_m2"] = 50.0
    with pytest.raises(ValidationError, match="identity"):
        NormalizedRunoutResult.model_validate(tampered)


def _runout_content(**overrides):
    """Minimal valid runout content; overrides exercise one rule at a time."""

    grid = _grid()
    field = RasterField(
        quantity=OutputQuantity.RUNOUT_EXTENT,
        unit="1",
        artifact=_artifact("runout.npy"),
        mask=_mask(grid),
        grid=grid,
        dtype="bool",
        valid_min=0.0,
        valid_max=1.0,
        semantics="positive flow thickness",
    )
    content = {
        "schema_version": NORMALIZED_RESULT_SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "site_id": "synthetic.utm11",
        "stage": EngineStage.RUNOUT,
        "regime": AvalancheRegime.DENSE_DRY,
        "provenance": RunProvenance(
            engine_id=AVAFRAME_COM1DFA.engine_id,
            engine_version="2.1",
            adapter_version="test",
            license_spdx="EUPL-1.2",
            execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
            executable_sha256=HASH,
            environment_sha256=HASH,
            adapter_sha256=HASH,
            selection_sha256=HASH,
            configuration_sha256=HASH,
            input_manifest_sha256=HASH,
            output_manifest_sha256=HASH,
            scenario_sha256=HASH,
            seed=1,
            source_urls=(AVAFRAME_COM1DFA.source_url,),
        ),
        "validation": AVAFRAME_COM1DFA.validation,
        "uncertainty": (),
        "warnings": (),
        "limitations": ("test output only",),
        "runout_extent": field,
        "runout_polygons": None,
        "flow_depth": None,
        "flow_velocity": None,
        "flow_pressure": None,
        "runout_area_m2": 25.0,
        "aoi_status": "complete_within_domain",
    }
    content.update(overrides)
    return content, grid


def test_a_published_quantity_cannot_also_be_declared_unsupported():
    from avycore.engines import UnsupportedOutput

    content, grid = _runout_content()
    energy = RasterField(
        quantity=OutputQuantity.ENERGY_LINE_HEIGHT,
        unit="m",
        artifact=_artifact("energy.npy"),
        mask=_mask(grid),
        grid=grid,
        dtype="float32",
        valid_min=0.0,
        valid_max=10.0,
        semantics="energy-line height",
    )
    content["energy_line_height"] = energy
    content["unsupported_outputs"] = (
        UnsupportedOutput(
            quantity=OutputQuantity.ENERGY_LINE_HEIGHT,
            reason="claimed unsupported while also published",
        ),
    )
    with pytest.raises(ValidationError, match="declared unsupported but a normalized field"):
        build_result(NormalizedRunoutResult, content)


def test_a_runout_result_cannot_declare_its_own_extent_unsupported():
    from avycore.engines import UnsupportedOutput

    content, _ = _runout_content()
    content["unsupported_outputs"] = (
        UnsupportedOutput(quantity=OutputQuantity.RUNOUT_EXTENT, reason="nonsense"),
    )
    with pytest.raises(ValidationError, match="cannot declare its own extent unsupported"):
        build_result(NormalizedRunoutResult, content)


def test_unsupported_declarations_must_be_unique():
    from avycore.engines import UnsupportedOutput

    content, _ = _runout_content()
    duplicate = UnsupportedOutput(
        quantity=OutputQuantity.FLOW_VELOCITY, reason="no momentum equation"
    )
    content["unsupported_outputs"] = (duplicate, duplicate)
    with pytest.raises(ValidationError, match="must be unique"):
        build_result(NormalizedRunoutResult, content)


@pytest.mark.parametrize(
    "quantity,wrong_unit",
    [
        (OutputQuantity.ENERGY_LINE_HEIGHT, "1"),
        (OutputQuantity.TRAVEL_ANGLE, "radian"),
        (OutputQuantity.ARRIVAL_TIME, "min"),
    ],
)
def test_new_quantities_reject_a_non_canonical_unit(quantity, wrong_unit):
    grid = _grid()
    with pytest.raises(ValidationError, match="requires unit"):
        RasterField(
            quantity=quantity,
            unit=wrong_unit,
            artifact=_artifact("field.npy"),
            mask=_mask(grid),
            grid=grid,
            dtype="float32",
            valid_min=0.0,
            valid_max=1.0,
            semantics="wrong unit",
        )


def test_field_for_returns_each_published_quantity_and_none_otherwise():
    content, grid = _runout_content()
    angle = RasterField(
        quantity=OutputQuantity.TRAVEL_ANGLE,
        unit="degree",
        artifact=_artifact("angle.npy"),
        mask=_mask(grid),
        grid=grid,
        dtype="float32",
        valid_min=0.0,
        valid_max=90.0,
        semantics="travel angle",
    )
    content["travel_angle"] = angle
    result = build_result(NormalizedRunoutResult, content)
    assert result.field_for(OutputQuantity.TRAVEL_ANGLE) is angle
    assert result.field_for(OutputQuantity.RUNOUT_EXTENT) is result.runout_extent
    assert result.field_for(OutputQuantity.ARRIVAL_TIME) is None


def test_a_normalized_field_must_declare_a_projected_metre_grid_in_xy_order():
    with pytest.raises(ValidationError, match="metre units and x,y order"):
        CRSContract(
            definition="EPSG:32611",
            projected=True,
            horizontal_unit="degree",
            coordinate_order="longitude,latitude",
            vertical_datum=None,
            vertical_datum_status="unknown",
        )
    with pytest.raises(ValidationError, match="degree units and longitude,latitude order"):
        CRSContract(
            definition="EPSG:4326",
            projected=False,
            horizontal_unit="m",
            coordinate_order="x,y",
            vertical_datum=None,
            vertical_datum_status="unknown",
        )
    with pytest.raises(ValidationError, match="known vertical datum must be named"):
        CRSContract(
            definition="EPSG:32611",
            projected=True,
            horizontal_unit="m",
            coordinate_order="x,y",
            vertical_datum=None,
            vertical_datum_status="known",
        )
