"""Shared constants and small file/config helpers for WorkshopAnalysis."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path


MIN_PYTHON = (3, 9)
SCRIPT_ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "WorkshopAnalysis"

SUPPORTED_GAME_TYPES = [
    {
        "Id": "source2",
        "Name": "Source 2",
        "Description": "Source 2 / VPK analysis with Source 2 Viewer CLI (ValveResourceFormat).",
    },
    {
        "Id": "unreal5",
        "Name": "Unreal Engine 5",
        "Description": "UE5 pak/utoc/ucas analysis with retoc/FModel-oriented tooling.",
    },
]


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def write_section(text):
    print()
    print("== {0} ==".format(text))

def ensure_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path

def read_json_file(path, default):
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(default)

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return copy.deepcopy(default)

    return json.loads(raw)

def save_json_file(path, value):
    path = Path(path)
    ensure_directory(path.parent)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

def merge_defaults(default, value):
    if not isinstance(default, dict) or not isinstance(value, dict):
        return value

    merged = copy.deepcopy(default)
    for key, item in value.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(item, dict):
            merged[key] = merge_defaults(merged[key], item)
        else:
            merged[key] = item
    return merged

def as_path(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser()

def path_is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
