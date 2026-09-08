"""Experiment and negative-result registry.

A research program that does not remember its failures repeats them. This module
is the memory: an append-only, hash-chained ledger of every hypothesis the
project has tested, what it was tested against, what the effect was, and whether
it was accepted.

Two properties are enforced in code rather than by convention:

1. **Negative results are first-class.** ``rejected`` entries are kept forever and
   ``previously_rejected`` makes them queryable, so a feature that failed a
   walk-forward test cannot re-enter the backlog next quarter as a fresh idea.
   Re-testing a rejected hypothesis is allowed; re-testing it *unknowingly* is
   what this prevents.

2. **Acceptance requires an interval that excludes zero.** ``record_experiment``
   refuses to write an ``accepted`` decision whose confidence interval contains
   no improvement, matching the promotion rule already used for model
   validation. A positive point estimate is not a result.

Entries chain by hash: each record commits to the previous record's digest, so
editing or deleting history is detectable with ``verify_registry`` rather than
merely discouraged. The registry stores research metadata only. It never stores
prices, positions, or anything that could be read as trading authorization.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import repo_path
from .private_research import stable_json

DECISIONS = ("accepted", "rejected", "inconclusive")
GENESIS_HASH = "sha256:genesis"

_REQUIRED_FIELDS = ("hypothesis", "rationale", "dataset_version", "target", "baseline", "test_method")


def default_registry_path() -> Path:
    return repo_path("data", "research", "experiment_registry.jsonl")


def hypothesis_key(hypothesis: str) -> str:
    """Normalize a hypothesis into a stable lookup key.

    Wording drifts between researchers and between months; the key exists so
    "trailing-5 form beats season average" and "Trailing 5 form beats season
    average!" collide instead of registering as two separate discoveries.
    """

    normalized = "".join(character.lower() if character.isalnum() else " " for character in str(hypothesis))
    return "-".join(normalized.split())


def _entry_hash(body: Mapping[str, Any], previous_hash: str) -> str:
    payload = {"body": body, "previous_hash": previous_hash}
    return f"sha256:{hashlib.sha256(stable_json(payload).encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class ExperimentRecord:
    """One tested hypothesis and the evidence behind its verdict."""

    hypothesis: str
    rationale: str
    dataset_version: str
    target: str
    baseline: str
    test_method: str
    decision: str
    effect_size: float | None = None
    effect_metric: str | None = None
    confidence_interval: Sequence[float] | None = None
    sample_size: int | None = None
    researcher: str | None = None
    model_version: str | None = None
    notes: str | None = None
    tags: Sequence[str] = field(default_factory=tuple)

    def body(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "hypothesis_key": hypothesis_key(self.hypothesis),
            "rationale": self.rationale,
            "dataset_version": self.dataset_version,
            "target": self.target,
            "baseline": self.baseline,
            "test_method": self.test_method,
            "decision": self.decision,
            "effect_size": self.effect_size,
            "effect_metric": self.effect_metric,
            "confidence_interval": list(self.confidence_interval) if self.confidence_interval else None,
            "sample_size": self.sample_size,
            "researcher": self.researcher,
            "model_version": self.model_version,
            "notes": self.notes,
            "tags": sorted(self.tags),
        }


def _validate(record: ExperimentRecord) -> None:
    if record.decision not in DECISIONS:
        raise ValueError(f"unknown_decision:{record.decision}")
    for name in _REQUIRED_FIELDS:
        if not str(getattr(record, name) or "").strip():
            raise ValueError(f"experiment_requires_{name}")
    interval = record.confidence_interval
    if interval is not None:
        if len(interval) != 2:
            raise ValueError("confidence_interval_requires_lower_and_upper")
        if float(interval[0]) > float(interval[1]):
            raise ValueError("confidence_interval_bounds_out_of_order")
    if record.decision == "accepted":
        if interval is None:
            raise ValueError("accepted_experiment_requires_confidence_interval")
        lower, upper = float(interval[0]), float(interval[1])
        if lower <= 0 <= upper:
            raise ValueError("accepted_experiment_requires_interval_excluding_zero")
        if record.sample_size is None:
            raise ValueError("accepted_experiment_requires_sample_size")


def read_experiments(path: str | Path | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the registry oldest first. A missing registry is empty, not an error."""

    registry_path = Path(path) if path else default_registry_path()
    if not registry_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in registry_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:] if limit else rows


def _last_hash(path: Path) -> str:
    rows = read_experiments(path)
    if not rows:
        return GENESIS_HASH
    return str(rows[-1].get("entry_hash") or GENESIS_HASH)


def record_experiment(
    record: ExperimentRecord,
    *,
    path: str | Path | None = None,
    recorded_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Append one experiment. Existing lines are never rewritten."""

    _validate(record)
    registry_path = Path(path) if path else default_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    if recorded_at is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    elif isinstance(recorded_at, datetime):
        timestamp = recorded_at.astimezone(timezone.utc).isoformat()
    else:
        timestamp = str(recorded_at)

    body = record.body()
    body["recorded_at"] = timestamp
    previous_hash = _last_hash(registry_path)
    entry = dict(body)
    entry["previous_hash"] = previous_hash
    entry["entry_hash"] = _entry_hash(body, previous_hash)
    with registry_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def verify_registry(path: str | Path | None = None) -> dict[str, Any]:
    """Recompute the hash chain and report the first break.

    A break means a line was edited or removed after the fact. That is a finding
    about the research process, not a recoverable error, so it is reported with
    the offending index rather than repaired.
    """

    rows = read_experiments(path)
    previous_hash = GENESIS_HASH
    for index, row in enumerate(rows):
        body = {key: value for key, value in row.items() if key not in {"entry_hash", "previous_hash"}}
        expected = _entry_hash(body, previous_hash)
        if row.get("previous_hash") != previous_hash or row.get("entry_hash") != expected:
            return {"valid": False, "entry_count": len(rows), "broken_at_index": index}
        previous_hash = str(row["entry_hash"])
    return {"valid": True, "entry_count": len(rows), "broken_at_index": None}


def negative_results(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Every rejected hypothesis. The most reusable output of the program."""

    return [row for row in read_experiments(path) if row.get("decision") == "rejected"]


def previously_rejected(hypothesis: str, *, path: str | Path | None = None) -> list[dict[str, Any]]:
    """Prior rejections of this hypothesis, newest last.

    Call this before spending time on an idea. An empty list is not evidence the
    idea works; it only means the project has not tested it yet.
    """

    key = hypothesis_key(hypothesis)
    return [row for row in negative_results(path) if row.get("hypothesis_key") == key]


def significance_review(
    path: str | Path | None = None, *, false_discovery_rate: float = 0.05
) -> dict[str, Any]:
    """Re-judge every recorded experiment against the whole family of tests.

    An individually significant result is not a discovery when it was selected
    from a backlog of dozens. This reads the registry, derives an approximate
    p-value from each recorded effect and interval, and applies
    Benjamini-Hochberg across all of them.

    The field to read is ``demoted``: accepted findings that stop being
    significant once the rest of the program is counted. Run this before citing
    any registry entry as a result.
    """

    from .evaluation.power import review_experiment_significance

    return review_experiment_significance(
        read_experiments(path), false_discovery_rate=false_discovery_rate
    )


def registry_summary(path: str | Path | None = None) -> dict[str, Any]:
    """Counts by decision, plus chain integrity."""

    rows = read_experiments(path)
    counts = {decision: 0 for decision in DECISIONS}
    for row in rows:
        decision = str(row.get("decision"))
        if decision in counts:
            counts[decision] += 1
    integrity = verify_registry(path)
    return {
        "entry_count": len(rows),
        "decisions": counts,
        "distinct_hypotheses": len({row.get("hypothesis_key") for row in rows}),
        "chain_valid": integrity["valid"],
        "broken_at_index": integrity["broken_at_index"],
        "first_recorded_at": rows[0].get("recorded_at") if rows else None,
        "last_recorded_at": rows[-1].get("recorded_at") if rows else None,
    }
