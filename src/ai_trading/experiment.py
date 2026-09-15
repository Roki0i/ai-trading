"""Offline reproducible experiments. Holdout requires a separate explicit access path."""
import argparse
import json
import platform
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .backtest import BacktestConfig, run_backtest
from .storage import canonical, digest, encode_observation
from .models import Observation, timestamp
from .market import provenance as market_provenance


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    created_at: str
    strategy: str
    strategy_parameters: dict
    universe_definition: dict
    evaluation_period: dict
    initial_capital: str
    fee_settings: dict
    slippage_settings: dict
    input_data_hash: str
    config_hash: str
    git_commit: object
    random_seed: int
    metrics: dict
    result_hash: str
    status: str
    failure_reason: object
    market_provenance: object = None


def validate_study(study):
    if set(study) != {"periods", "universe_definition"}:
        raise ValueError("study requires periods and universe_definition")
    periods = study["periods"]
    if set(periods) != {"development", "validation", "holdout"}:
        raise ValueError("all three periods required")
    previous = None
    for name in ("development", "validation", "holdout"):
        p = periods[name]
        start, end = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        if start > end or (previous is not None and previous >= start):
            raise ValueError("periods must be ordered and disjoint")
        previous = end
    u = study["universe_definition"]
    if u["type"] not in ("static_point_in_time", "historical_point_in_time") or not u["evidence"].strip():
        raise ValueError("static PIT universe with evidence required")
    if timestamp(u["known_at"]) >= timestamp(periods["development"]["start"] + "T00:00:00+09:00"):
        raise ValueError("future universe information forbidden")
    if not u["symbols"] or len(set(u["symbols"])) != len(u["symbols"]):
        raise ValueError("unique universe required")


def initialize(root, study):
    validate_study(study)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    content = canonical(study)
    path = root / "study.json"
    try:
        with path.open("xb") as f:
            f.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("study is locked; periods/universe cannot be relabeled")
    return digest(content)


def environment():
    package = Path(__file__).resolve().parent
    sources = {p.name: p.read_text() for p in sorted(package.glob("*.py"))}
    def git(*args):
        try:
            return subprocess.check_output(["git", *args], cwd=package,
                                           stderr=subprocess.DEVNULL).decode().strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    return dict(python=platform.python_version(), implementation=platform.python_implementation(),
                platform=platform.platform(), machine=platform.machine(), git_commit=git("rev-parse", "HEAD"),
                git_status=git("status", "--porcelain"), code_hash=digest(canonical(sources)),
                sources=sources, dependencies=[], execution_policy="phase2_weekly_next_close_v1")


def decode_rows(records):
    result = []
    for record in records:
        record = dict(record)
        for key in ("event_at", "published_at", "available_at", "ingested_at"):
            if record[key] is not None:
                record[key] = timestamp(record[key])
        result.append(Observation(**record))
    return result


def prepare(rows, config, study, split):
    if config.pit_mode != "observed":
        raise ValueError("Phase 3 requires observed PIT; historical reconstructions forbidden")
    p = study["periods"][split]
    if any(not p["start"] <= d <= p["end"] for d in config.sessions):
        raise ValueError("sessions cross evaluation boundary")
    if tuple(sorted(study["universe_definition"]["symbols"])) != config.universe:
        raise ValueError("universe differs from frozen study")
    if study['universe_definition']['type'] == 'historical_point_in_time' and config.market is None:
        raise ValueError('historical universe requires market context')
    if config.market is not None:
        from .market import validate_market
        validate_market(config.market, config.sessions, config.universe)
    end = timestamp(config.sessions[-1] + "T18:00:00+09:00")
    selected = []
    for r in rows:
        if r.dataset != "daily_bars" or r.entity_id not in config.universe:
            continue
        if r.event_at >= end or r.available_at >= end or r.ingested_at >= end:
            continue
        d = json.loads(r.payload_json)["session_date"]
        if r.event_at != timestamp(d + "T00:00:00+09:00"):
            raise ValueError("bar event/session mismatch")
        if d in config.sessions:
            if r.availability_basis != "observed":
                raise ValueError("only observed data allowed")
            selected.append(r)
    if not selected:
        raise ValueError("no eligible observations in evaluation sessions")
    if config.market is not None:
        from .market import raw_observations
        raw_records = {canonical(encode_observation(r)) for r in raw_observations(config.market)}
        if any(canonical(encode_observation(r)) not in raw_records for r in selected):
            raise ValueError('processed observation has no matching immutable raw lineage')
    return selected


def outcome(records, raw_config, study, split):
    try:
        config = BacktestConfig(**raw_config)
        if config.ml is not None:
            if split == 'holdout':
                raise ValueError('ML model selection cannot access holdout')
        rows = prepare(decode_rows(records), config, study, split)
        result = run_backtest(rows, config)
        # Ledger links every execution to the decision and subsequent account state.
        result["trade_history"] = [dict(fill=f, order=result["orders"][f["order_id"] - 1])
                                   for f in result["fills"]]
        return dict(status="completed", failure_reason=None, result=result)
    except Exception as exc:
        return dict(status="failed", failure_reason=type(exc).__name__ + ": " + str(exc), result=None)


class ExperimentStore:
    def __init__(self, root):
        self.root = Path(root)
        self.study = json.loads((self.root / "study.json").read_text())
        validate_study(self.study)

    def run(self, rows, config, split="development", random_seed=0, *, holdout=False):
        if split not in self.study["periods"]:
            raise ValueError("unknown split")
        if split == "holdout" and not holdout:
            raise PermissionError("holdout requires dedicated access")
        if type(random_seed) is not int:
            raise ValueError("integer seed required")
        raw = asdict(config) if isinstance(config, BacktestConfig) else dict(config)
        if raw.get('ml') is not None:
            if split == 'holdout':
                raise PermissionError('ML model selection cannot access holdout')
            if raw['ml'].get('seed', 0) != random_seed:
                raise ValueError('ML seed must equal experiment random_seed')
        if raw.get('market') is not None:
            if split == 'holdout':
                raise PermissionError('Phase 5 holdout remains sealed')
            raw = json.loads(canonical(raw))
            cutoff = timestamp(self.study['periods'][split]['end'] + 'T18:00:00+09:00')
            # Raw payloads are also inputs: never archive another split through config.
            from .market import raw_observations
            for r in raw_observations(raw['market']):
                d = json.loads(r.payload_json)['session_date']
                p = self.study['periods'][split]
                if not p['start'] <= d <= p['end'] or r.available_at >= cutoff or r.ingested_at >= cutoff:
                    raise PermissionError('raw bundle crosses accessible split/knowledge boundary')
            for dataset in ('historical_universe', 'corporate_actions'):
                raw['market'][dataset] = [r for r in raw['market'][dataset]
                    if timestamp(r['available_at']) < cutoff and timestamp(r['ingested_at']) < cutoff]
        # Even failed attempts must never archive another split's data.
        period = self.study["periods"][split]
        start = timestamp(period["start"] + "T00:00:00+09:00")
        end = timestamp(period["end"] + "T18:00:00+09:00")
        rows = [r for r in rows if start <= r.event_at < end and
                r.available_at < end and r.ingested_at < end and
                r.entity_id in self.study["universe_definition"]["symbols"] and r.dataset == "daily_bars"]
        # Valid runs freeze only accessible inputs. Invalid configurations retain attempted input.
        try:
            checked = BacktestConfig(**raw)
            rows = prepare(rows, checked, self.study, split)
            raw = asdict(checked)
        except (ValueError, KeyError, TypeError):
            pass
        records = sorted((encode_observation(r) for r in rows), key=canonical)
        cfg = dict(backtest=raw, study=self.study, split=split, random_seed=random_seed)
        env = environment()
        out = outcome(records, raw, self.study, split)
        result = out["result"] or {}
        eid = uuid4().hex
        model = Experiment(eid, datetime.now(timezone.utc).isoformat(), raw.get("strategy", "equal_weight"),
                           {k: raw.get(k) for k in ("lookback", "top_n", "ml")}, self.study["universe_definition"],
                           dict(split=split, **self.study["periods"][split]), raw.get("initial_cash", "1000000"),
                           {k: raw.get(k, "0") for k in ("commission_bps", "commission_fixed")},
                           dict(slippage_bps=raw.get("slippage_bps", "0")), digest(canonical(records)),
                           digest(canonical(cfg)), env["git_commit"], random_seed, result.get("metrics", {}),
                           digest(canonical(out)), out["status"], out["failure_reason"],
                           market_provenance(raw['market']) if raw.get('market') else None)
        artifacts = {"config": cfg, "inputs": records, "environment": env, "outcome": out,
                     "daily_equity": result.get("snapshots", []), "orders": result.get("orders", []),
                     "fills": result.get("fills", []), "trade_history": result.get("trade_history", []),
                     "metrics": model.metrics, "experiment": asdict(model)}
        folder = self.root / ("holdout" if split == "holdout" else "research") / eid
        folder.mkdir(parents=True, mode=0o700)
        hashes = {}
        for name, value in artifacts.items():
            content = canonical(value)
            (folder / (name + ".json")).write_bytes(content)
            hashes[name] = digest(content)
        seal = canonical(hashes)
        (folder / (digest(seal) + ".manifest.json")).write_bytes(seal)
        return model

    def read(self, eid, *, holdout=False):
        if not re.fullmatch(r"[0-9a-f]{32}", eid):
            raise ValueError("invalid experiment_id")
        folder = self.root / "research" / eid
        if not folder.exists():
            if (self.root / "holdout" / eid).exists() and not holdout:
                raise PermissionError("holdout access forbidden")
            folder = self.root / "holdout" / eid
        seals = list(folder.glob("*.manifest.json"))
        if len(seals) != 1:
            raise ValueError("missing/ambiguous manifest")
        seal = seals[0].read_bytes()
        if digest(seal) != seals[0].name.split(".")[0]:
            raise ValueError("manifest hash mismatch")
        hashes = json.loads(seal)
        expected = {"config", "inputs", "environment", "outcome", "daily_equity", "orders", "fills",
                    "trade_history", "metrics", "experiment"}
        if set(hashes) != expected:
            raise ValueError("artifact inventory mismatch")
        artifacts = {}
        for name, hash_value in hashes.items():
            content = (folder / (name + ".json")).read_bytes()
            if digest(content) != hash_value:
                raise ValueError("artifact hash mismatch: " + name)
            artifacts[name] = json.loads(content)
        cfg, model = artifacts["config"], artifacts["experiment"]
        if cfg["split"] == "holdout" and not holdout:
            raise PermissionError("holdout access forbidden")
        if cfg["study"] != self.study or model["experiment_id"] != eid:
            raise ValueError("study/identity mismatch")
        for field, name in (("input_data_hash", "inputs"), ("config_hash", "config"), ("result_hash", "outcome")):
            if model[field] != hashes[name]:
                raise ValueError(field + " mismatch")
        env = artifacts["environment"]
        if digest(canonical(env["sources"])) != env["code_hash"]:
            raise ValueError("source snapshot mismatch")
        out = artifacts["outcome"]
        if cfg['backtest'].get('market') is not None:
            expected_market = market_provenance(cfg['backtest']['market'])
            if model.get('market_provenance') != expected_market:
                raise ValueError('market provenance mismatch')
            if out['status'] == 'completed' and out['result']['market_provenance'] != expected_market:
                raise ValueError('result market provenance mismatch')
        if (model["git_commit"] != env["git_commit"] or model["metrics"] != artifacts["metrics"] or
                model["status"] != out["status"] or model["failure_reason"] != out["failure_reason"]):
            raise ValueError("experiment metadata mismatch")
        return artifacts

    def verify(self, eid, *, holdout=False):
        a = self.read(eid, holdout=holdout)
        env = environment()
        for field in ("code_hash", "python", "implementation", "machine", "platform"):
            if env[field] != a["environment"][field]:
                raise ValueError("environment mismatch: " + field + "; restore saved source/runtime")
        c = a["config"]
        replay = outcome(a["inputs"], c["backtest"], c["study"], c["split"])
        if canonical(replay) != canonical(a["outcome"]):
            raise ValueError("result replay mismatch")
        r = replay["result"] or {}
        for name, key in (("daily_equity", "snapshots"), ("orders", "orders"), ("fills", "fills"),
                          ("trade_history", "trade_history"), ("metrics", "metrics")):
            if a[name] != r.get(key, {} if name == "metrics" else []):
                raise ValueError("derived artifact mismatch: " + name)
        return a["experiment"]

    def list(self):
        return [self.read(p.name)["experiment"] for p in sorted((self.root / "research").glob("*")) if p.is_dir()]

    def compare(self, ids=None):
        models = self.list() if ids is None else [self.read(eid)["experiment"] for eid in ids]
        reference, report = None, []
        for model in models:
            a = self.read(model["experiment_id"])
            c = dict(a["config"]["backtest"])
            for key in ("strategy", "lookback", "top_n"):
                c.pop(key, None)
            condition = canonical([c, a["config"]["study"], a["config"]["split"], model["input_data_hash"],
                                   model["random_seed"], a["environment"]])
            if reference is not None and reference != condition:
                raise ValueError("incomparable data/configuration/code/environment")
            reference = condition
            report.append({k: model[k] for k in ("experiment_id", "strategy", "status", "failure_reason", "metrics")})
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run", "baseline", "holdout-run", "list", "compare", "show", "verify", "holdout-verify"):
        p = sub.add_parser(name)
        p.add_argument("--store", type=Path, required=True)
        if name == "init":
            p.add_argument("--study", type=Path, required=True)
        if name in ("run", "baseline", "holdout-run"):
            p.add_argument("--config", type=Path, required=True)
            p.add_argument("--data", type=Path, required=True)
            p.add_argument("--split", choices=("development", "validation"), default="development")
            p.add_argument("--seed", type=int, default=0)
        if name in ("show", "verify", "holdout-verify"):
            p.add_argument("experiment_id")
        if name == "compare":
            p.add_argument("ids", nargs="*")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = initialize(args.store, json.loads(args.study.read_text()))
        else:
            store = ExperimentStore(args.store)
            if args.command in ("run", "baseline", "holdout-run"):
                from .storage import load_observations
                raw = json.loads(args.config.read_text())
                rows = load_observations(args.data)
                strategies = ("buy_and_hold", "equal_weight", "momentum") if args.command == "baseline" else (raw.get("strategy", "equal_weight"),)
                result = [asdict(store.run(rows, dict(raw, strategy=s),
                          "holdout" if args.command == "holdout-run" else args.split,
                          args.seed, holdout=args.command == "holdout-run")) for s in strategies]
            elif args.command == "list":
                result = store.list()
            elif args.command == "show":
                result = store.read(args.experiment_id)
            elif args.command == "compare":
                result = store.compare(args.ids or None)
            else:
                result = store.verify(args.experiment_id, holdout=args.command == "holdout-verify")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, list) and any(r.get("status") == "failed" for r in result):
            return 1
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
