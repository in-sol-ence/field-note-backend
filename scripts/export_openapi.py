"""Emit an OpenAPI doc carrying the dossier + event schemas, for Go codegen.

Built straight from the pydantic models rather than from FastAPI's live app:
the /preprocess route streams, so it declares no response model and the types
would never reach components.schemas on their own.

    uv run python scripts/export_openapi.py > openapi.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic.json_schema import models_json_schema  # noqa: E402

from schema import ErrorEvent, ProductDossier, ResultEvent, StageEvent  # noqa: E402

MODELS = (ProductDossier, StageEvent, ErrorEvent, ResultEvent)


def build() -> dict:
    _, defs = models_json_schema(
        [(m, "validation") for m in MODELS],
        ref_template="#/components/schemas/{model}",
    )
    return {
        "openapi": "3.0.3",
        "info": {"title": "field-note T0", "version": "0.1.0"},
        "paths": {},
        "components": {"schemas": defs["$defs"]},
    }


if __name__ == "__main__":
    json.dump(build(), sys.stdout, indent=2)
    sys.stdout.write("\n")
