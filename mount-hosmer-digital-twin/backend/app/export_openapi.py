"""Export the canonical FastAPI contract consumed by frontend code generation."""

import json
from pathlib import Path


def main() -> None:
    from app.main import app

    target = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"
    target.write_text(json.dumps(app.openapi(), separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
