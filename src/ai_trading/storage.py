"""Content-addressed, create-only artifacts. Hashes are verified on reads."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import Observation, timestamp


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def put(root: Path, content: bytes, suffix: str = ".json") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest(content) + suffix)
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("existing artifact is corrupted: " + str(path))
    return path


def read_verified(path: Path) -> bytes:
    content = path.read_bytes()
    if digest(content) != path.stem:
        raise ValueError("artifact hash mismatch: " + str(path))
    return content


def encode_observation(row: Observation) -> dict:
    return {key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in asdict(row).items()}


def save_observations(root: Path, rows: list[Observation]) -> Path:
    records = sorted((canonical(encode_observation(row)) for row in rows))
    return put(root, b"".join(record + b"\n" for record in records), ".jsonl")


def load_observations(path: Path) -> list[Observation]:
    rows = []
    for line in read_verified(path).splitlines():
        data = json.loads(line)
        for key in ("event_at", "published_at", "available_at", "ingested_at"):
            if data[key] is not None:
                data[key] = timestamp(data[key])
        rows.append(Observation(**data))
    return rows
