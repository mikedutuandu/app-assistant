"""Writes docs/app-assistant.schema.json: the JSON Schema of the pack an app serves at
`{site}/.well-known/app-assistant.json`, generated from `ServedPack` (src/assistant/packs.py).

Run it with `uv run python scripts/pack_schema.py` after changing the pack's shape, and commit the file: a test
fails while the committed schema and the model differ.
"""

import json
from pathlib import Path

from assistant.packs import pack_json_schema

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "app-assistant.schema.json"


def render() -> str:
    return json.dumps(pack_json_schema(), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    SCHEMA_PATH.write_text(render())
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
