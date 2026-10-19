"""CLI shared by local development and the composite Action."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .adapters.mcpp_index import append
from .github import GitHub
from .lua_edit import Invalid
from .manifest import assert_preserved, check_package, inventory, validate_config
from .pull_request import propose, run


def config(root: Path) -> dict:
    path = root / ".github/xpkgindex-update.yml"
    result = {"version": 1, "adapter": "mcpp-index", "max_prs": 3}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, separator, value = line.partition(":")
            if not separator or key not in result:
                raise Invalid("config supports only version, adapter, max_prs")
            result[key] = value.strip().strip("\"'")
    if int(result["version"]) != 1 or result["adapter"] not in ("mcpp-index", "xim-pkgindex"):
        raise Invalid("unsupported config version/adapter")
    result["max_prs"] = int(result["max_prs"])
    if not 1 <= result["max_prs"] <= 50:
        raise Invalid("max_prs must be 1..50")
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
    for name in ("inspect", "check", "apply", "propose", "guard"):
        command = commands.add_parser(name)
        command.add_argument("--package")
        command.add_argument("--output")
        if name in ("apply", "propose"):
            command.add_argument("--plan", required=True)
        if name == "propose":
            command.add_argument("--base")
            command.add_argument("--draft", action="store_true")
        if name == "guard":
            command.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    root = args.workspace.resolve()
    try:
        policy = config(root)
        github = GitHub(args.token)
        if args.command in ("inspect", "check"):
            infos = inventory(root)
            records = []
            for info in infos:
                if args.package and info["id"] != args.package:
                    continue
                if args.command == "inspect":
                    records.append({k: info[k] for k in ("id", "path", "direct", "autoupdate")})
                    if info["autoupdate"] is not None:
                        validate_config(info)
                elif info["autoupdate"] is not None:
                    records.append(check_package(info, github))
            if args.package and not records:
                raise Invalid("selected package was not found or has no autoupdate declaration")
            base = run("git", "rev-parse", "HEAD", cwd=root)
            report = {"format": 1, "base_commit": base, "adapter": policy["adapter"],
                      "packages": records, "summary": dict(Counter(r.get("status", "declared") for r in records)),
                      "requests": github.requests, "downloaded_bytes": github.downloaded}
            write_report(report, args.output)
            return int(any(r.get("status") == "error" for r in records))
        if args.command == "guard":
            files = run("git", "ls-tree", "-r", "--name-only", args.base, "--", "pkgs", cwd=root).splitlines()
            checked = 0
            for filename in files:
                if not filename.endswith(".lua"):
                    continue
                path = root / filename
                if not path.exists():
                    raise Invalid(f"existing descriptor was removed: {filename}")
                before = run("git", "show", f"{args.base}:{filename}", cwd=root)
                assert_preserved(before, path.read_text(encoding="utf-8"))
                checked += 1
            write_report({"checked_descriptors": checked, "status": "preserved"}, args.output)
            return 0
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if plan.get("format") != 1 or plan.get("adapter") != policy["adapter"]:
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
                    changed = append(root, record, github)
                    outcomes.append({"id": record["id"], "status": "appended" if changed else "existing"})
        write_report({"packages": outcomes}, args.output)
        return int(any(record["status"] == "error" for record in outcomes))
    except (Invalid, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"xpkgindex-update: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
