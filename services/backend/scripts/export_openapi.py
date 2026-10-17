"""Write the OpenAPI document that packages/contracts generates TypeScript from."""

import json
import sys
from pathlib import Path

from app.api.app import create_app
from app.config import Settings

OUT = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "openapi.json"


def main() -> None:
    app = create_app(Settings(data_dir=Path("unused"), token="unused", port=0))
    OUT.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
