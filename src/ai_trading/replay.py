"""Rebuild normalized data from verified raw receipts without network access."""

import json
from pathlib import Path

from .cli import code_fingerprint
from .providers import normalize_daily
from .quality import inspect_daily
from .storage import canonical, digest, read_verified, save_observations


def replay(manifest_path: Path, output: Path) -> Path:
    manifest = json.loads(read_verified(manifest_path))
    if manifest["status"] != "passed":
        raise ValueError("cannot replay a failed ingestion as a valid dataset")
    if manifest["code_sha256"] != code_fingerprint():
        raise ValueError("code fingerprint changed; restore recorded source before replay")
    if manifest["config_sha256"] != digest(canonical(manifest["config"])):
        raise ValueError("configuration hash mismatch")
    if Path(manifest["processed"]).stem != manifest["processed_sha256"]:
        raise ValueError("processed identity mismatch")
    pages = []
    paths = manifest["raw_receipts"]
    hashes = manifest["raw_receipt_sha256"]
    if len(paths) != len(hashes):
        raise ValueError("receipt count mismatch")
    for path, expected_hash in zip(paths, hashes):
        path = Path(path)
        if path.stem != expected_hash:
            raise ValueError("receipt identity mismatch")
        receipt = json.loads(read_verified(path))
        body_path = path.parent.parent / "bodies" / (receipt["body_sha256"] + ".json")
        body = json.loads(read_verified(body_path))
        pages.append({"receipt": receipt, "rows": body["data"]})
    source = "fixture_jquants_v2" if manifest["synthetic"] else "jquants_v2"
    rows = normalize_daily(pages, source=source)
    issues = inspect_daily(rows)
    report = canonical({"schema_version": 1, "issues": [i.to_dict() for i in issues]})
    if digest(report) != manifest["quality_sha256"]:
        raise ValueError("quality report is not reproducible")
    result = save_observations(output, rows)
    if result.stem != manifest["processed_sha256"]:
        raise ValueError("processed data is not reproducible")
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(replay(args.manifest, args.output))


if __name__ == "__main__":
    main()
