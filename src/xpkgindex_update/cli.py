"""CLI shared by local development and the composite Action."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from .adapters import get_adapter
from .github import GitHub
from .lua_edit import Invalid, parse, tokens
from .pull_request import propose, run


def config(root: Path) -> dict:
    path = root / ".github/xpkgindex-update.yml"
    result = {"version": 1, "adapter": "mcpp-index", "max_prs": 3, "request_budget": 500,
              "resource_bytes": 4 * 1024**3, "download_bytes": 16 * 1024**3,
              "max_packages": 50}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, separator, value = line.partition(":")
            if not separator or key not in result:
                raise Invalid("unsupported central configuration key")
            result[key] = value.strip().strip("\"'")
    if int(result["version"]) != 1 or result["adapter"] not in ("mcpp-index", "xim-pkgindex"):
        raise Invalid("unsupported config version/adapter")
    result["max_prs"] = int(result["max_prs"])
    if not 1 <= result["max_prs"] <= 50:
        raise Invalid("max_prs must be 1..50")
    for key in ("request_budget", "resource_bytes", "download_bytes", "max_packages"):
        result[key] = int(result[key])
        if result[key] < 1:
            raise Invalid(f"{key} must be positive")
    return result


def write_report(report: dict, output: str | None) -> None:
    serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if output:
        Path(output).write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## xpkgindex-update\n\n| Package | Status | Version | Detail |\n|---|---|---|---|\n")
            for record in report.get("packages", []):
                values = [record.get("id", ""), record.get("status", ""),
                          record.get("candidate", record.get("current", "")), record.get("reason", "")]
                stream.write("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--token", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "check", "apply", "propose", "guard", "runtime-diff"):
        command = commands.add_parser(name)
        command.add_argument("--package")
        command.add_argument("--output")
        if name == "check":
            command.add_argument("--state-file", type=Path)
        if name in ("apply", "propose"):
            command.add_argument("--plan", required=True)
        if name == "propose":
            command.add_argument("--base")
            command.add_argument("--draft", action="store_true")
        if name in ("guard", "runtime-diff"):
            command.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    root = args.workspace.resolve()
    try:
        policy = config(root)
        adapter = get_adapter(policy["adapter"])
        github = GitHub(args.token, request_budget=policy["request_budget"],
                        resource_bytes=policy["resource_bytes"], download_bytes=policy["download_bytes"])
        if args.command in ("inspect", "check"):
            infos, records = [], []
            for path in sorted((root / "pkgs").glob("*/*.lua")):
                if args.package and path.stem != args.package.split(":")[-1]:
                    continue
                try:
                    # Unrelated legacy package expressions do not opt into updating.
                    pkg = parse(path.read_text(encoding="utf-8")).fields
                    extension = pkg.get("extensions")
                    enabled = extension and "autoupdate" in extension.table()
                    if args.command == "check" and not enabled:
                        continue
                    infos.append(adapter.package_info(path, root))
                except (Invalid, KeyError, OSError) as exc:
                    declared = any(t.kind == "word" and t.value == "autoupdate"
                                   or t.kind == "string" and t.value == "autoupdate"
                                   for t in tokens(path.read_text(encoding="utf-8")))
                    if declared or args.command == "inspect":
                        records.append({"id": path.stem, "path": path.relative_to(root).as_posix(),
                                        "status": "error" if declared else "unsupported", "reason": str(exc)})
            state = {}
            state_path = getattr(args, "state_file", None)
            if state_path and state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if not isinstance(state, dict) or not isinstance(state.get("cursor", ""), str):
                        state = {}
                except (OSError, ValueError):
                    state = {}
            infos.sort(key=lambda info: info["id"])
            if args.command == "check" and not args.package and state.get("cursor"):
                cursor = state["cursor"]
                infos = [i for i in infos if i["id"] > cursor] + [i for i in infos if i["id"] <= cursor]
            processed = 0
            now = int(time.time())
            waiting = state.get("waiting", {})
            for info in infos:
                if args.package and info["id"] != args.package:
                    continue
                if args.command == "inspect":
                    records.append({k: info[k] for k in ("id", "path", "direct", "autoupdate")})
                    if info["autoupdate"] is not None:
                        adapter.validate_config(info)
                elif info["autoupdate"] is not None:
                    if (processed >= policy["max_packages"] or github.requests >= policy["request_budget"]
                            or github.downloaded >= policy["download_bytes"]):
                        records.append({"id": info["id"], "path": info["path"], "status": "budget-deferred",
                                        "reason": "package budget exhausted"})
                        continue
                    record = adapter.check_package(info, github)
                    processed += 1
                    state["cursor"] = info["id"]
                    if record["status"] == "waiting-assets":
                        first = waiting.setdefault(info["id"], now)
                        if now - first >= 7 * 86400:
                            record.update(status="error", reason="resources unavailable for 7 days: " + record["reason"])
                    else:
                        waiting.pop(info["id"], None)
                    records.append(record)
            if args.package and not records:
                raise Invalid("selected package was not found or has no autoupdate declaration")
            if state_path and not args.package:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state["waiting"] = waiting
                temporary = state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(state), encoding="utf-8")
                temporary.replace(state_path)
            base = run("git", "rev-parse", "HEAD", cwd=root)
            report = {"format": adapter.FORMAT, "base_commit": base, "adapter": policy["adapter"],
                      "packages": records, "summary": dict(Counter(r.get("status", "declared") for r in records)),
                      "requests": github.requests, "downloaded_bytes": github.downloaded}
            write_report(report, args.output)
            return int(any(r.get("status") in ("error", "policy-required", "existing-version-conflict") for r in records))
        if args.command == "runtime-diff":
            from .manifest import runtime_signature
            changed = run("git", "diff", "--name-only", args.base, "HEAD", "--", "pkgs", cwd=root).splitlines()
            records = []
            for filename in changed:
                if not filename.endswith(".lua"):
                    continue
                path = root / filename
                try:
                    before = subprocess.check_output(["git", "show", f"{args.base}:{filename}"],
                                                     cwd=root, text=True, encoding="utf-8")
                    same = runtime_signature(before) == runtime_signature(path.read_text(encoding="utf-8"))
                except (Invalid, OSError):
                    same = False
                records.append({"path": filename, "status": "metadata-only" if same else "runtime-changed"})
            write_report({"packages": records}, args.output)
            return 0
        if args.command == "guard":
            files = run("git", "ls-tree", "-r", "--name-only", args.base, "--", "pkgs", cwd=root).splitlines()
            checked = 0
            for filename in files:
                if not filename.endswith(".lua"):
                    continue
                path = root / filename
                if not path.exists():
                    raise Invalid(f"existing descriptor was removed: {filename}")
                before = subprocess.check_output(["git", "show", f"{args.base}:{filename}"],
                                                     cwd=root, text=True, encoding="utf-8")
                if before != path.read_text(encoding="utf-8"):
                    adapter.guard(before, path.read_text(encoding="utf-8"))
                checked += 1
            write_report({"checked_descriptors": checked, "status": "preserved"}, args.output)
            return 0
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if plan.get("format") != adapter.FORMAT or plan.get("adapter") != policy["adapter"]:
            raise Invalid("unsupported plan format/adapter")
        if args.package:
            plan["packages"] = [r for r in plan["packages"] if r["id"] == args.package]
        if args.command == "propose":
            outcomes = []
            for record in plan["packages"]:
                if record["status"] != "update-ready":
                    continue
                remaining = policy["max_prs"] - sum(o["status"] == "created" for o in outcomes)
                if remaining <= 0:
                    break
                try:
                    outcomes.extend(propose(root, {**plan, "packages": [record]}, github,
                                            base=args.base, draft=args.draft, limit=remaining))
                except (Invalid, OSError) as exc:
                    outcomes.append({"id": record["id"], "status": "error", "reason": str(exc)})
        else:
            if run("git", "rev-parse", "HEAD", cwd=root) != plan["base_commit"]:
                raise Invalid("index HEAD changed since check")
            outcomes = []
            for record in plan["packages"]:
                if record["status"] == "update-ready":
                    try:
                        changed = adapter.append(root, record, github)
                        outcomes.append({"id": record["id"], "status": "appended" if changed else "existing"})
                    except (Invalid, OSError) as exc:
                        outcomes.append({"id": record["id"], "status": "error", "reason": str(exc)})
        write_report({"packages": outcomes}, args.output)
        return int(any(record["status"] == "error" for record in outcomes))
    except (Invalid, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"xpkgindex-update: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
