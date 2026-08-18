r"""Runtime-safe reader for immutable prediction products.

Products live under ``runtime\predictions\<product_id>\`` — a generated root of
their own, deliberately *not* a child of ``runtime\baked\``. The bake validates
``baked\`` against ``meta.json`` and ``python -m app.bake --force`` replaces that
whole directory atomically, so a product stored inside it would be destroyed by
an unrelated terrain rebuild and would break the bake's own checksum contract.
A sibling root gives products their own lifecycle while keeping the serving rule
intact: the application still reads only immutable generated artifacts.

This module imports pydantic and the standard library and nothing else. Reading a
product must never pull rasterio, pyproj, AvaFrame, or SNOWPACK into a request.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from avycore.products import PredictionProduct


PREDICTIONS_DIRECTORY = "predictions"
PRODUCT_FILENAME = "prediction-product.json"
CHECKSUMS_FILENAME = "checksums.json"


class PredictionProductError(RuntimeError):
    """Raised when a stored product is missing, corrupt, or inconsistent."""


def predictions_root(runtime_root: str | Path) -> Path:
    return Path(runtime_root).resolve() / PREDICTIONS_DIRECTORY


def prediction_product_root(runtime_root: str | Path, product_id: str) -> Path:
    """Resolve one product directory, refusing any identifier that escapes the root."""

    if not product_id.startswith("prediction-product-") or len(product_id) != 83:
        raise PredictionProductError(f"Invalid prediction product identifier: {product_id!r}")
    if not all(character in "0123456789abcdef" for character in product_id[19:]):
        raise PredictionProductError(f"Invalid prediction product identifier: {product_id!r}")
    root = predictions_root(runtime_root)
    candidate = (root / product_id).resolve()
    if candidate.parent != root:
        raise PredictionProductError(f"Prediction product path escapes its root: {product_id!r}")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_prediction_product(directory: str | Path) -> PredictionProduct:
    """Load and validate one product; a content mismatch is an error, not a warning."""

    root = Path(directory).resolve()
    document = root / PRODUCT_FILENAME
    if not document.is_file():
        raise PredictionProductError(f"Prediction product document is missing: {document}")
    try:
        product = PredictionProduct.model_validate_json(document.read_bytes())
    except ValueError as exc:
        raise PredictionProductError(f"Invalid prediction product {root.name}: {exc}") from exc
    if product.product_id != root.name:
        raise PredictionProductError(
            f"Prediction product identity {product.product_id} does not match directory {root.name}."
        )
    return product


def verify_prediction_product(directory: str | Path) -> PredictionProduct:
    """Load a product and re-check every stored file against its manifest."""

    root = Path(directory).resolve()
    product = load_prediction_product(root)
    manifest_path = root / CHECKSUMS_FILENAME
    if not manifest_path.is_file():
        raise PredictionProductError(f"Prediction product checksum manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PredictionProductError(f"Unreadable checksum manifest for {root.name}: {exc}") from exc

    present = {
        str(path.relative_to(root)).replace(os.sep, "/")
        for path in root.rglob("*")
        if path.is_file() and path.name != CHECKSUMS_FILENAME
    }
    declared = set(manifest)
    if present != declared:
        missing = sorted(declared - present)
        extra = sorted(present - declared)
        raise PredictionProductError(
            f"Prediction product {root.name} file set differs from its manifest; "
            f"missing={missing}, unexpected={extra}"
        )
    for relative, expected in sorted(manifest.items()):
        actual = _file_sha256(root / relative)
        if actual != expected:
            raise PredictionProductError(
                f"Prediction product {root.name} artifact {relative} failed its checksum."
            )
    return product


def list_prediction_products(runtime_root: str | Path) -> tuple[PredictionProduct, ...]:
    """List every valid stored product, newest identity order aside."""

    root = predictions_root(runtime_root)
    if not root.is_dir():
        return ()
    products = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not directory.name.startswith("prediction-product-"):
            continue
        products.append(load_prediction_product(directory))
    return tuple(products)


__all__ = [
    "CHECKSUMS_FILENAME",
    "PREDICTIONS_DIRECTORY",
    "PRODUCT_FILENAME",
    "PredictionProductError",
    "list_prediction_products",
    "load_prediction_product",
    "prediction_product_root",
    "predictions_root",
    "verify_prediction_product",
]
