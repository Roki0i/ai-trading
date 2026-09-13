"""Strict JSON configuration; paths are relative to the config file."""

import json
from datetime import date
from pathlib import Path


RESEARCH = {"market": "JP", "frequency": "daily", "position": "long_only_cash",
            "rebalance": "weekly", "leverage": 1, "timezone": "Asia/Tokyo"}


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    expected = {"schema_version", "research", "storage", "provider", "pit", "dataset"}
    if set(config) != expected or config["schema_version"] != 1:
        raise ValueError("unsupported configuration schema")
    if config["research"] != RESEARCH:
        raise ValueError("research scope is fixed for Phase 0–1")
    if config["pit"] != {"mode": "observed", "boundary": "strict_before"}:
        raise ValueError("ingestion pipeline requires observed/strict_before PIT")
    if set(config["storage"]) != {"raw", "processed", "experiments"}:
        raise ValueError("invalid storage configuration")
    provider = config["provider"]
    if set(provider) != {"name", "mode", "fixture", "api_key_env"}:
        raise ValueError("invalid provider configuration; credentials belong in environment")
    if provider["name"] != "jquants_v2" or provider["mode"] not in ("fixture", "live"):
        raise ValueError("unsupported provider")
    if provider["api_key_env"] != "JQUANTS_API_KEY":
        raise ValueError("use JQUANTS_API_KEY environment variable")
    if set(config["dataset"]) != {"name", "date"} or config["dataset"]["name"] != "daily_bars":
        raise ValueError("only daily_bars ingestion is implemented")
    date.fromisoformat(config["dataset"]["date"])
    for group, keys in (("storage", ("raw", "processed", "experiments")),
                        ("provider", ("fixture",))):
        for key in keys:
            value = config[group][key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("nonempty path required")
            config[group][key] = str((path.resolve().parent / value).resolve())
    if len(set(config["storage"].values())) != 3:
        raise ValueError("storage directories must be distinct")
    return config
