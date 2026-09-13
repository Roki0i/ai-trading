"""Offline-first acquisition command with immutable provenance manifests."""

import argparse
import json
import platform
import subprocess
from pathlib import Path

from . import __version__
from .config import load_config
from .providers import FixtureTransport, JQuantsTransport, acquire_daily, normalize_daily
from .quality import inspect_daily
from .storage import canonical, digest, put, save_observations


def code_fingerprint() -> str:
    package = Path(__file__).parent
    return digest(canonical({p.name: digest(p.read_bytes()) for p in sorted(package.glob("*.py"))}))


def run(config_path: Path) -> dict:
    config = load_config(config_path)
    provider = config["provider"]
    transport = (FixtureTransport(Path(provider["fixture"])) if provider["mode"] == "fixture"
                 else JQuantsTransport())
    raw_root = Path(config["storage"]["raw"])
    pages, receipts = acquire_daily(transport, config["dataset"]["date"], raw_root)
    # Fixture observations have their own source namespace and cannot masquerade as market data.
    source = "fixture_jquants_v2" if provider["mode"] == "fixture" else "jquants_v2"
    rows = normalize_daily(pages, source=source)
    issues = inspect_daily(rows)
    report_path = put(Path(config["storage"]["experiments"]) / "quality",
                      canonical({"schema_version": 1, "issues": [i.to_dict() for i in issues]}))
    valid = not any(issue.severity == "error" for issue in issues)
    processed = save_observations(Path(config["storage"]["processed"]) / source / "daily_bars" / "v1", rows) if valid else None
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                         cwd=Path(__file__).parent, stderr=subprocess.DEVNULL).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = None
    manifest = {
        "schema_version": 1, "kind": "data_ingestion", "status": "passed" if valid else "failed",
        "synthetic": provider["mode"] == "fixture", "config": config,
        "config_sha256": digest(canonical(config)), "package_version": __version__,
        "python": platform.python_version(), "git_commit": commit,
        "code_sha256": code_fingerprint(), "raw_receipts": receipts,
        "raw_receipt_sha256": [Path(path).stem for path in receipts],
        "processed": str(processed) if processed else None,
        "processed_sha256": processed.stem if processed else None,
        "quality_report": str(report_path), "quality_sha256": report_path.stem,
        "row_count": len(rows), "availability_policy": "observed_strict_before",
    }
    path = put(Path(config["storage"]["experiments"]) / "manifests", canonical(manifest))
    return {"status": manifest["status"], "manifest": str(path), "rows": len(rows),
            "warnings": sum(i.severity == "warning" for i in issues)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/research.json"))
    args = parser.parse_args()
    try:
        result = run(args.config)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        parser.exit(1, "Data ingestion failed: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "passed":
        parser.exit(1, "Data quality errors; processed data was not published.\n")


if __name__ == "__main__":
    main()
