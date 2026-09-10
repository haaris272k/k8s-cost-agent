"""Read and write the local artifacts exchanged by pipeline stages.

Keeping file handling here gives every stage the same UTF-8 encoding, JSON
formatting, directory creation, and error messages.
"""

import json
from pathlib import Path
from typing import Any


def read_json(path: Path, label: str) -> Any:
    """Read JSON and give missing or malformed inputs a path-specific error."""
    try:
        with path.open(encoding="utf-8") as input_file:
            return json.load(input_file)
    except FileNotFoundError as error:
        raise ValueError(f"{label} file does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} file contains invalid JSON: {path}") from error


def read_json_list(path: Path, label: str) -> list[Any]:
    """Read JSON and require the top-level value to be a list."""
    records = read_json(path, label)
    if not isinstance(records, list):
        raise ValueError(f"{label} must contain a JSON list: {path}")
    return records


def write_json(path: Path, value: Any) -> None:
    """Write stable, human-readable JSON and create its parent directory."""
    # A fresh checkout has no artifacts directory because generated outputs are
    # ignored by Git. Create it on first use.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output_file:
        json.dump(value, output_file, indent=2)
        output_file.write("\n")


def write_text(path: Path, value: str) -> None:
    """Write UTF-8 text and create its parent directory when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
