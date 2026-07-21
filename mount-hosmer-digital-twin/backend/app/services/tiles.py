"""Terrain-RGB tiles, so MapLibre can render the mountain in 3D.

MapLibre builds a terrain mesh from a `raster-dem` source, which means it needs
elevation delivered as PNG tiles in Web Mercator with the height packed into the
colour channels. Our DEM is a 5 m COG in UTM zone 11N, so something has to
reproject it and encode it. That something is here.

The encoding is Mapbox Terrain-RGB, which MapLibre reads natively:

    height = -10000 + (R * 65536 + G * 256 + B) * 0.1

That gives 0.1 m of vertical resolution over a -10 km .. +1667 km range -- far finer
than the 1 m LiDAR beneath it, so the encoding is not what limits the model.

**NoData must not become sea level**, and a transparent pixel does not save you: the
decoder ignores alpha and reads the colour, so RGB(0, 0, 0) is -10,000 m. Gaps are
filled to the AOI's valley floor instead. See :func:`_encode_terrain_rgb`.

These tiles are for *rendering only*. No model reads them, and the fill never
reaches a score -- in the COG a gap is still masked and still excluded.

Tiles are cached on disk. Rendering one costs a warped read of a 412 MB COG, and
the same handful of tiles is requested on every pan.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import reproject, transform_bounds

TILE_SIZE = 256
WEB_MERCATOR = "EPSG:3857"

#: Half the circumference of the Earth in Web Mercator metres.
ORIGIN = 20037508.342789244

#: Mapbox Terrain-RGB constants.
BASE = -10000.0
INTERVAL = 0.1

MAX_ZOOM = 16
MIN_ZOOM = 8


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Web Mercator bounds of an XYZ tile. Y counts down from the north."""
    span = (2.0 * ORIGIN) / (2**z)
    left = -ORIGIN + x * span
    top = ORIGIN - y * span
    return left, top - span, left + span, top


def _encode_terrain_rgb(elevation: np.ma.MaskedArray, floor_m: float) -> bytes:
    """Pack elevation into RGB. NoData is filled to ``floor_m``, not left at zero.

    **Transparency does not work here, and it is worth saying why.** The obvious way
    to represent a gap is a transparent pixel, and it is wrong: the Terrain-RGB
    decoder ignores the alpha channel entirely and reads the colour. A transparent
    pixel is still RGB(0, 0, 0), which decodes to **-10,000 m**. The result is not a
    hole in the mesh -- it is the whole mountain standing on a cliff plunging ten
    kilometres into an abyss, which is exactly the "missing became zero" failure this
    codebase exists to avoid, wearing a different hat.

    So gaps are filled to the floor of the AOI: a flat plinth at valley level. It is
    visibly flat and visibly not terrain, and it is only ever *rendered* -- no model
    reads these tiles. Every number the model produces comes from the COG, where a
    gap is still masked and still excluded.
    """
    from PIL import Image

    values = np.asarray(elevation.filled(np.nan), dtype="float64")
    values = np.where(np.isfinite(values), values, floor_m)

    # Clamp into the range the encoding can represent, so a corrupt pixel lands on a
    # neighbouring colour rather than wrapping to an absurd height.
    packed = np.clip((values - BASE) / INTERVAL, 0, 256**3 - 1).astype("uint32")

    rgb = np.zeros((*values.shape, 3), dtype="uint8")
    rgb[..., 0] = (packed >> 16) & 0xFF
    rgb[..., 1] = (packed >> 8) & 0xFF
    rgb[..., 2] = packed & 0xFF

    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _read_tile(source: Path, z: int, x: int, y: int) -> np.ma.MaskedArray | None:
    """Warp the DEM into one Web Mercator tile. None when the tile misses the AOI.

    Reprojected straight into the tile's own grid rather than through a WarpedVRT --
    a VRT refuses boundless reads, and every tile at the edge of a 12 x 12 km AOI is
    a boundless read.
    """
    left, bottom, right, top = tile_bounds(z, x, y)

    with rasterio.open(source) as dataset:
        # Does this tile touch the data at all? If not, say so rather than warping a
        # 412 MB raster to produce 256x256 pixels of nothing.
        west, south, east, north = transform_bounds(
            dataset.crs, WEB_MERCATOR, *dataset.bounds, densify_pts=21
        )
        if right <= west or left >= east or top <= south or bottom >= north:
            return None

        destination = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(dataset, 1),
            destination=destination,
            src_transform=dataset.transform,
            src_crs=dataset.crs,
            src_nodata=dataset.nodata,
            dst_transform=transform_from_bounds(left, bottom, right, top, TILE_SIZE, TILE_SIZE),
            dst_crs=WEB_MERCATOR,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

    return np.ma.array(destination, mask=~np.isfinite(destination))


@lru_cache(maxsize=4)
def _floor_elevation_cached(source: str, mtime: float) -> float:
    with rasterio.open(source) as dataset:
        # The overview is a 1/16-scale read: enough to find the valley floor without
        # pulling 412 MB through memory on the first tile request.
        sample = dataset.read(
            1, out_shape=(dataset.height // 16, dataset.width // 16), masked=True
        )
    return float(sample.min()) if sample.count() else 0.0
