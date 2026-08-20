r"""Terrain routes: the AOI metadata and the static baked tiles.

Nothing here computes anything. ``meta.json`` is read off disk and the tiles are
PNG files the bake already wrote -- no rasterio, no ``DATA\`` access. These live
with the assess service because they share the baked artifacts, but they are pure
static serving and are the obvious first thing to move behind a CDN.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import baked
from app.api.models import ExposureFeatureCollection, TwinMeta
from app.baked import exposure_features_path, imagery_tile_path, tile_path
from app.core.settings import get_settings

router = APIRouter(prefix="/api", tags=["terrain"])


def _resolve(locator, mountain: str | None, *args):
    """Build a baked-artifact path for the selected mountain.

    A malformed id must not reach the filesystem, so the id rule is applied inside
    ``app.mountains`` and a bad one becomes a 404 here rather than a traversal
    attempt or a silent fall back to Mount Hosmer.
    """
    from app.mountains import MountainError

    try:
        return locator(get_settings(), *args, mountain)
    except MountainError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _suffix(mountain: str | None) -> str:
    """Carry the selected mountain into the URLs this metadata hands the client.

    The templates go straight into MapLibre sources, which fetch them verbatim, so
    the id has to travel in the URL rather than in a header the map cannot set.
    """
    return f"?mountain={mountain}" if mountain else ""


@router.get("/twin/meta", response_model=TwinMeta, operation_id="getTwinMeta")
def twin_meta(mountain: str | None = None, bt=Depends(baked)) -> TwinMeta:
    """What the map needs to place the mountain and drape the mesh.

    The reprojection lattice is omitted -- it is a server-side concern used to build
    GeoJSON; the browser never needs it.
    """
    meta = {key: value for key, value in bt.meta.items() if key != "reproject"}
    tail = _suffix(mountain)
    meta["tiles"] = dict(meta.get("tiles", {}))
    meta["tiles"]["url_template"] = "/api/twin/tiles/{z}/{x}/{y}.png" + tail
    # A bake with no natural-colour imagery still carries the key, set to null, so
    # test on the value rather than on key presence: a portable single-DEM mountain
    # has no imagery at all and must still return its metadata.
    if isinstance(meta.get("imagery"), dict):
        meta["imagery"] = dict(meta["imagery"])
        meta["imagery"]["url_template"] = "/api/twin/imagery/{z}/{x}/{y}.png" + tail
    if isinstance(meta.get("exposure"), dict):
        # Only the parts the map needs: where to fetch the vectors, what the classes
        # mean, and the attribution it has to render whenever the layer is visible.
        meta["exposure"] = {**meta["exposure"], "url": "/api/twin/exposure" + tail}
    return TwinMeta.model_validate(meta)


@router.get(
    "/twin/exposure",
    response_model=ExposureFeatureCollection,
    operation_id="getTwinExposure",
)
def twin_exposure(mountain: str | None = None) -> FileResponse:
    """The baked exposure vectors. A static file the bake already wrote.

    No computation, no ``DATA\\`` access, no geospatial library -- the same shape as
    the tile routes. A bake without exposure simply has no file, and 404 is the
    honest answer: the client shows no layer rather than an empty valley.
    """
    path = _resolve(exposure_features_path, mountain)
    if not path.exists():
        raise HTTPException(status_code=404, detail="This bake carries no exposure layer.")
    return FileResponse(
        path,
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/twin/tiles/{z}/{x}/{y}.png")
def twin_tile(z: int, x: int, y: int, mountain: str | None = None) -> FileResponse:
    """A static baked terrain-RGB tile. No rasterio: it is a file on disk."""
    path = _resolve(tile_path, mountain, z, x, y)
    if not path.exists():
        # Tiles outside the 12x12 km AOI legitimately do not exist. MapLibre treats
        # a 404 as an empty tile, which is exactly right.
        raise HTTPException(status_code=404, detail="No tile here.")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/twin/imagery/{z}/{x}/{y}.png")
def twin_imagery_tile(z: int, x: int, y: int, mountain: str | None = None) -> FileResponse:
    """A static baked Sentinel-2 natural-colour tile. No source-data access."""
    path = _resolve(imagery_tile_path, mountain, z, x, y)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No imagery tile here.")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
    )
