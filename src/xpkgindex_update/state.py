"""Atomic progress and a persistent FIFO of verified update candidates."""
from __future__ import annotations

import json
from pathlib import Path

from .lua_edit import Invalid
from .manifest import descriptor_digest


def load_state(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Invalid("invalid updater state; refusing to discard progress") from exc
    if (not isinstance(state, dict) or not isinstance(state.get("cursor", ""), str)
            or not isinstance(state.get("waiting", {}), dict)
            or not isinstance(state.get("queue", []), list)):
        raise Invalid("invalid updater state schema")
    ids = set()
    for item in state.get("queue", []):
        if (not isinstance(item, dict) or not isinstance(item.get("record"), dict)
                or not isinstance(item["record"].get("id"), str)
                or item["record"]["id"] in ids
                or any(not isinstance(item.get(k, 0), int) or item.get(k, 0) < 0
                       for k in ("first_seen", "attempts", "retry_at"))):
            raise Invalid("invalid candidate queue")
        ids.add(item["record"]["id"])
    return state


def save_state(path: Path | None, state: dict, waiting: dict | None = None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if waiting is not None:
        state["waiting"] = waiting
    state["version"] = 1
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def enqueue(state: dict, records: list[dict], now: int) -> None:
    queue = state.setdefault("queue", [])
    for record in records:
        identity = record["id"]
        entry = next((q for q in queue if q["record"]["id"] == identity), None)
        if record["status"] == "update-ready":
            if entry is None:
                queue.append({"record": record, "first_seen": now, "attempts": 0, "retry_at": 0})
            else:
                if entry["record"].get("candidate") != record.get("candidate"):
                    entry.update(attempts=0, retry_at=0)
                entry["record"] = record  # Newest verified candidate, same FIFO position.
        elif entry and record["status"] not in ("budget-deferred", "package-deferred"):
            queue.remove(entry)


def prune_queue(state: dict, infos: list[dict]) -> None:
    infos = {i["id"]: i for i in infos}
    state["queue"] = [q for q in state.get("queue", [])
                      if (i := infos.get(q["record"]["id"])) is not None
                      and i.get("autoupdate") is not None
                      and q["record"].get("path") == i["path"]
                      and q["record"].get("descriptor_sha256") == descriptor_digest(i["text"])]


def eligible(state: dict, now: int) -> list[dict]:
    return [q["record"] for q in state.get("queue", []) if q.get("retry_at", 0) <= now]


def settle(state: dict, record: dict, outcome: dict, now: int) -> None:
    queue = state.setdefault("queue", [])
    item = next((q for q in queue if q["record"]["id"] == record["id"]), None)
    if item is None:
        return
    queue.remove(item)
    if outcome["status"] in ("created", "updated", "pending", "dismissed", "existing"):
        return  # An open PR owns CI/review; a later scan can enqueue a newer candidate.
    item["attempts"] = item.get("attempts", 0) + 1
    item["retry_at"] = now + min(86400, 3600 * 2 ** min(item["attempts"] - 1, 5))
    item["last_error"] = outcome.get("reason", outcome["status"])
    queue.append(item)  # Failed work cannot hold the front of the queue.
