"""Generate shared NLP schemas from the typed Brain contract owner."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.nlp.contracts import (WorkerRequest, WorkerResponse, PackCompatibility,
                               EnrichmentRecord, EnrichmentSnapshot, Readiness, DeploymentResult)

ROOT = Path(__file__).resolve().parents[1]

def documents():
    return {model.__name__ + ".schema.json": model.model_json_schema()
            for model in (WorkerRequest, WorkerResponse, PackCompatibility,
                          EnrichmentRecord, EnrichmentSnapshot, Readiness, DeploymentResult)}

if __name__ == "__main__":
    target = ROOT / "shared/nlp/v1"
    target.mkdir(parents=True, exist_ok=True)
    for name, schema in documents().items():
        (target / name).write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
