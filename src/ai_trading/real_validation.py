"""Offline, observed-PIT comparison of archived J-Quants market inputs."""

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path

from .backtest import BacktestConfig, _bars, decimal
from .experiment import ExperimentStore, initialize, prepare, validate_study
from .market import membership, raw_observations, reference_rows, validate_market
from .ml import MLConfig
from .models import timestamp
from .providers import calendar_from_raw
from .quality import inspect_daily
from .storage import canonical, digest, load_observations, put

CAPITALS = ("100000", "200000", "300000")
STRATEGIES = ("buy_and_hold", "momentum", "ml")


def preflight(rows, config, study, evidence, split="development"):
    """Reject incomplete inputs before writing any experiment or claiming a result.

    Evidence strings are researcher supplied references, not proof that an
    external provider is complete. Their hashes bind the declaration to the
    exact reference observations used by the backtest.
    """
    validate_study(study)
    if split not in ("development", "validation"):
        raise ValueError("real comparison cannot open holdout")
    if study["universe_definition"]["type"] != "historical_point_in_time":
        raise ValueError("dated historical universe required")
    if config.market is None or config.market["data_provider"] != "jquants_v2":
        raise ValueError("archived J-Quants raw bundle required")
    if config.pit_mode != "observed" or config.knowledge_at is not None:
        raise ValueError("observed PIT only; historical time cannot be inferred")
    if config.ml is None or len(config.sessions) <= MLConfig(**config.ml).first_prediction:
        raise ValueError("ML configuration and prediction horizon required")
    if config.lot_size != 100 or config.volume_participation is None:
        raise ValueError("100-share lots and volume participation limit required")
    if not 0 < decimal(config.max_position_weight) < 1 or not 0 < decimal(config.min_cash_reserve_weight) < 1:
        raise ValueError("position cap and cash reserve required")
    if decimal(config.commission_bps) + decimal(config.commission_fixed) <= 0 or decimal(config.slippage_bps) <= 0:
        raise ValueError("nonzero fees and slippage required")
    market = config.market
    if market["calendar"]["source"] != "jquants_v2:markets/calendar":
        raise ValueError("archived J-Quants calendar required")
    if asdict(calendar_from_raw(evidence["calendar_bundle"])) != market["calendar"]:
        raise ValueError("calendar differs from archived raw response")
    masters = evidence["master_bundles"]
    if not isinstance(masters, list) or not masters:
        raise ValueError("dated master snapshots required")
    master_codes, seen_dates = {}, set()
    for master in masters:
        if master.get("provider") != "jquants_v2" or not master.get("receipts"):
            raise ValueError("invalid master bundle")
        for record in master["receipts"]:
            receipt = record["receipt"]
            day = receipt["params"]["date"]
            if day not in config.sessions or receipt["endpoint"] != "/equities/master" or receipt["source"] != "jquants_v2":
                raise ValueError("unexpected master snapshot")
            if digest(record["body"].encode("utf-8")) != receipt["body_sha256"]:
                raise ValueError("master raw hash mismatch")
            if timestamp(receipt["ingested_at"]) >= timestamp(day + "T18:00:00+09:00"):
                raise ValueError("master acquired after PIT decision: " + day)
            seen_dates.add(day)
            codes = master_codes.setdefault(day, set())
            for row in json.loads(record["body"])["data"]:
                if row.get("Date") != day or not isinstance(row.get("Code"), str):
                    raise ValueError("master date/code mismatch")
                code = "jquants_v2:" + row["Code"]
                if code in codes:
                    raise ValueError("duplicate master code")
                if code in config.universe and str(row.get("ProdCat")) != "011":
                    raise ValueError("candidate is not an ordinary listed share")
                codes.add(code)
    if seen_dates != set(config.sessions):
        raise ValueError("master snapshots do not cover every session")
    bundle = market["raw_data"]
    if not bundle.get("receipts") or any(
        r["receipt"].get("endpoint") != "/equities/bars/daily" or
        r["receipt"].get("source") != "jquants_v2"
        for r in bundle["receipts"]
    ):
        raise ValueError("J-Quants daily receipts required")
    expected = {
        "schema_version": 1,
        "period": [config.sessions[0], config.sessions[-1]],
        "universe": list(config.universe),
        "calendar_bundle_sha256": digest(canonical(evidence["calendar_bundle"])),
        "master_bundles_sha256": digest(canonical(masters)),
        "historical_universe_sha256": digest(canonical(market["historical_universe"])),
        "corporate_actions_sha256": digest(canonical(market["corporate_actions"])),
    }
    if any(evidence.get(k) != v for k, v in expected.items()):
        raise ValueError("reference coverage declaration does not match inputs")
    for key in ("lifecycle_source", "corporate_action_source", "trading_unit_source"):
        value = evidence.get(key)
        if not isinstance(value, str) or not value.strip() or "fixture" in value.lower():
            raise ValueError("independent reference coverage evidence required: " + key)
    for dataset in ("historical_universe", "corporate_actions"):
        for row in reference_rows(market, dataset):
            if "fixture" in row.source.lower() or row.availability_basis != "observed":
                raise ValueError("synthetic or reconstructed reference observation")
    if {r.entity_id for r in reference_rows(market, "historical_universe")} != set(config.universe):
        raise ValueError("historical lifecycle missing for a candidate symbol")
    validate_market(market, config.sessions, config.universe)
    period_end = timestamp(config.sessions[-1] + "T18:00:00+09:00")
    for row in raw_observations(market):
        day = json.loads(row.payload_json)["session_date"]
        if day not in config.sessions or row.available_at >= period_end or row.ingested_at >= period_end:
            raise ValueError("raw daily bundle crosses the accessible period")
    selected = prepare(rows, config, study, split)
    errors = [i for i in inspect_daily(selected) if i.severity == "error"]
    if errors:
        raise ValueError("invalid daily price or volume: " + errors[0].code)
    covered = 0
    for session in config.sessions:
        cutoff = timestamp(session + "T18:00:00+09:00")
        bars = _bars(selected, cutoff, config)
        active = membership(config, session, cutoff)
        if set(active) != set(config.universe) & master_codes[session]:
            raise ValueError("master/lifecycle membership mismatch: " + session)
        for symbol in active:
            bar = bars.get((symbol, session))
            if bar is None or bar["close"] is None or bar["volume"] is None:
                raise ValueError("missing PIT price/volume: " + symbol + " " + session)
            covered += 1
    if not covered:
        raise ValueError("no PIT-eligible listed symbols")
    return dict(status="ready_for_exploratory_comparison", split=split,
                sessions=len(config.sessions), symbol_sessions=covered,
                raw_sha256=market["raw_data_hash"],
                reference_declaration_sha256=digest(canonical(evidence)),
                calendar_sha256=expected["calendar_bundle_sha256"],
                master_sha256=expected["master_bundles_sha256"])


def compare(rows, config, study, evidence, store_root, split="development"):
    readiness = preflight(rows, config, study, evidence, split)
    initialize(store_root, study)
    store = ExperimentStore(store_root)
    groups = {}
    seed = config.ml.get("seed", 0)
    for capital in CAPITALS:
        ids = []
        for strategy in STRATEGIES:
            model = store.run(rows, replace(config, initial_cash=capital, strategy=strategy),
                              split=split, random_seed=seed)
            if model.status != "completed":
                raise ValueError("experiment failed: " + model.failure_reason)
            ids.append(model.experiment_id)
        groups[capital] = store.compare(ids)
    report = dict(readiness=readiness, label="exploratory_observed_PIT",
                  results=groups)
    put(Path(store_root) / "comparisons", canonical(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--study", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path, help="verified processed JSONL")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--split", choices=("development", "validation"), default="development")
    parser.add_argument("--check-only", action="store_true",
                        help="validate inputs without creating experiments")
    args = parser.parse_args()
    if not args.check_only and args.store is None:
        parser.error("--store is required for comparison")
    config = BacktestConfig(**json.loads(args.config.read_text(encoding="utf-8")))
    study = json.loads(args.study.read_text(encoding="utf-8"))
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    rows = load_observations(args.data)
    result = (preflight(rows, config, study, evidence, args.split) if args.check_only else
              compare(rows, config, study, evidence, args.store, args.split))
    print(json.dumps(result,
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
