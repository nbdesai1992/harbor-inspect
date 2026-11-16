#!/usr/bin/env python3
"""harbor-inspect: read Harbor tasks without running them.

Harbor's own CLI runs, lints and publishes tasks. This tool only reads. It
answers three questions about a task directory or a folder of them:

    anatomy  TASK      which file is for whom, and when it is used in a trial
    census   DATASET   what a set of tasks looks like in aggregate
    flags    PATH      things that make a task hard to reproduce or easy to game

Standard library only, Python 3.11+. Not affiliated with the Harbor project.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import tomllib
from collections import Counter
from pathlib import Path

__version__ = "0.2.0"

SECRET_RE = re.compile(r"\$\{([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)")
INSTALL_RE = re.compile(r"\b(apt-get install|apk add|pip3? install|uv pip install|uvx|npm (i|install)|curl [^\n|]*\|\s*(ba)?sh)")
ANSWERISH = re.compile(r"(^|[/_.-])(gold|expected|answer[_-]?key|answers?|solution|ground[_-]?truth)([/_.-]|$)", re.I)
HOST_RE = re.compile(r"host\.docker\.internal")


def find_tasks(path: Path) -> list[Path]:
    if (path / "task.toml").is_file():
        return [path]
    return sorted(p.parent for p in path.rglob("task.toml") if ".git" not in p.parts)


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _from_lines(dockerfile: Path) -> list[str]:
    if not dockerfile.is_file():
        return []
    out = []
    for line in dockerfile.read_text(errors="replace").splitlines():
        m = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", line, re.I)
        if m and m.group(1).lower() != "scratch":
            out.append(m.group(1))
    return out


def _image_kind(ref: str) -> str:
    if "@sha256:" in ref:
        return "digest"
    if "$" in ref:
        return "build-arg"
    tag = ref.rsplit(":", 1)[1] if ":" in ref.rsplit("/", 1)[-1] else ""
    return "latest" if tag in ("", "latest") else "tag"


def read_task(path: Path) -> dict:
    """Everything that can be known about a task from its files."""
    cfg = tomllib.loads((path / "task.toml").read_text(errors="replace"))
    env, ver, agent = cfg.get("environment", {}), cfg.get("verifier", {}), cfg.get("agent", {})
    steps = [s.get("name") for s in cfg.get("steps", [])]

    # Verifier mode, by the resolution table in Harbor's separate-verifier docs.
    mode, has_env = ver.get("environment_mode"), "environment" in ver
    verifier_mode = "invalid" if (mode == "shared" and has_env) else \
        "separate" if (mode == "separate" or has_env) else "shared"

    env_dir, tests_dir, sol_dir = path / "environment", path / "tests", path / "solution"
    compose = next((env_dir / n for n in ("docker-compose.yaml", "docker-compose.yml") if (env_dir / n).is_file()), None)
    images = ([env["docker_image"]] if env.get("docker_image") else []) + _from_lines(env_dir / "Dockerfile")
    if verifier_mode == "separate":
        vimg = ver.get("environment", {}).get("docker_image")
        if vimg:
            verifier_image = "verifier.environment.docker_image"
        elif (tests_dir / "Dockerfile").is_file() or (tests_dir / "docker-compose.yaml").is_file():
            verifier_image = "tests/Dockerfile"
        else:
            verifier_image = "copy of agent environment"
    else:
        verifier_image = "agent container"

    test_files = [p for p in tests_dir.rglob("*") if p.is_file()] if tests_dir.is_dir() else []
    step_tests = [p for p in (path / "steps").rglob("tests/*") if p.is_file()] if (path / "steps").is_dir() else []
    test_text = "\n".join(p.read_text(errors="replace") for p in test_files + step_tests
                          if p.suffix in (".sh", ".py", ".bat", ".yaml", ".yml", ".toml", "") and p.stat().st_size < 400_000)
    sol_text = "\n".join(p.read_text(errors="replace") for p in sol_dir.glob("solve.*")) if sol_dir.is_dir() else ""
    entry = next((p for p in (tests_dir / "test.sh", tests_dir / "test.bat") if p.is_file()), None)
    entry_text = entry.read_text(errors="replace") if entry else ""

    instr_files = [path / "instruction.md"] if (path / "instruction.md").is_file() else \
        sorted((path / "steps").glob("*/instruction.md")) if (path / "steps").is_dir() else []
    instruction = "\n".join(p.read_text(errors="replace") for p in instr_files)
    body = re.sub(r"\A\s*(<!--.*?-->\s*)+", "", instruction, flags=re.S)

    secrets = sorted({m.group(1) for m in SECRET_RE.finditer(json.dumps(ver.get("env", {})))})
    mcp = env.get("mcp_servers", []) or []
    legacy = env.get("allow_internet")
    network = env.get("network_mode") or ("no-network" if legacy is False else "public")
    env_files = [str(p.relative_to(env_dir)) for p in env_dir.rglob("*") if p.is_file()] if env_dir.is_dir() else []
    # Folders that Docker builds as a context: environment/ with a Dockerfile and no prebuilt image,
    # and tests/ with its own Dockerfile. The Dockerfile and compose files are read directly, so
    # only the files a build can COPY are recorded.
    contexts = [d for d in (env_dir, tests_dir) if (d / "Dockerfile").is_file()
                and ((d is env_dir and not env.get("docker_image")) or (d is tests_dir and verifier_image == "tests/Dockerfile"))]
    context_files = [(d.name, str(p.relative_to(d)), p.stat().st_size, str(p))
                     for d in contexts for p in d.rglob("*") if p.is_file() and not p.is_symlink()
                     and p.name not in ("Dockerfile", ".dockerignore", "docker-compose.yaml", "docker-compose.yml")]

    return {
        "path": str(path), "name": (cfg.get("task") or {}).get("name") or path.name,
        "schema_version": str(cfg.get("schema_version", "unset")),
        "steps": steps, "os": env.get("os", "linux"),
        "instruction_words": len(body.split()), "canary": bool(re.search(r"canary", instruction[:600] + (path / "task.toml").read_text(errors="replace")[:600]
                                                           + test_text + sol_text, re.I)),
        "agent_timeout": agent.get("timeout_sec"), "verifier_timeout": ver.get("timeout_sec", 600),
        "agent_user": agent.get("user"), "cpus": env.get("cpus"), "memory_mb": env.get("memory_mb"),
        "gpus": env.get("gpus") or 0,
        "network": network, "agent_network": agent.get("network_mode"), "verifier_network": ver.get("network_mode"),
        "images": images, "image_kinds": [_image_kind(i) for i in images],
        "prebuilt_image": bool(env.get("docker_image")), "compose": bool(compose),
        "mcp_servers": [{"name": m.get("name"), "transport": m.get("transport", "sse"), "url": m.get("url")} for m in mcp],
        "verifier_mode": verifier_mode, "verifier_image": verifier_image,
        "artifacts": [a if isinstance(a, str) else a.get("source") for a in cfg.get("artifacts", [])],
        "verifier_secrets": secrets,
        "has_solution": (sol_dir / "solve.sh").is_file() or (sol_dir / "solve.bat").is_file()
                        or any((path / "steps").glob("*/solution/solve.*")),
        "has_entrypoint": bool(entry) or bool(step_tests),
        "writes_reward": bool(re.search(r"reward\.(txt|json)", test_text)),
        "reward_kind": "json" if "reward.json" in test_text else "txt" if "reward.txt" in test_text else "unknown",
        "uses_pytest": "pytest" in test_text, "uses_rewardkit": "rewardkit" in test_text,
        "verifier_installs": bool(INSTALL_RE.search(entry_text)),
        "host_dependency": bool(HOST_RE.search(instruction + json.dumps(mcp))),
        "answerish_env_files": [f for f in env_files if ANSWERISH.search(f)][:5],
        "metadata_keys": sorted((cfg.get("metadata") or {}).keys()),
        "difficulty": (cfg.get("metadata") or {}).get("difficulty"),
        "bytes": {"environment": _size(env_dir), "tests": _size(tests_dir), "solution": _size(sol_dir)},
        "context_files": context_files, "context_collisions": [],
    }


def mark_context_collisions(tasks: list[dict]) -> None:
    """Find files that share a build-context path and size across tasks while differing in content.

    Docker's builder reuses a file from an earlier build whose context folder has the same name
    when the file's path, size and timestamp all match. Every Harbor task calls its folders
    environment/ and tests/, so two tasks with such a pair can be built with each other's file
    once their timestamps agree, which unpacking an archive makes likely.
    """
    groups: dict[tuple, list[tuple[dict, str]]] = {}
    for t in tasks:
        for context, rel, size, full in t["context_files"]:
            groups.setdefault((context, rel, size), []).append((t, full))
    for (context, rel, _), members in groups.items():
        if len(members) < 2:
            continue
        digests = {}
        for t, full in members:
            digests.setdefault(hashlib.sha256(Path(full).read_bytes()).hexdigest(), []).append(t)
        if len(digests) > 1:
            for owners in digests.values():
                for t in owners:
                    t["context_collisions"].append(f"{context}/{rel}")
    for t in tasks:
        t.pop("context_files", None)


# ---------------------------------------------------------------- flags

FLAGS = {
    "shared-verifier": "The verifier runs in the agent's container, so the agent's files and processes are in reach of the grader.",
    "verifier-installs-at-test-time": "test.sh installs packages while grading. A network or index failure becomes a reward of zero.",
    "verifier-needs-secret": "Grading needs a host API key, which usually means a model judge: rewards cost money and can vary between runs.",
    "unpinned-image": "A base image is referenced by tag, not digest, so the environment can change without the task changing.",
    "image-not-in-task": "The Dockerfile builds on an image the task does not define or pin. The task is not self-contained.",
    "host-dependency": "The task talks to a service on the host machine (host.docker.internal) that the task directory does not start.",
    "no-solution": "No solution/solve.sh, so the oracle agent cannot confirm the task is solvable.",
    "no-reward-write": "Nothing under tests/ mentions reward.txt or reward.json. Harbor needs one of them.",
    "no-agent-timeout": "No agent timeout is set, so a stuck trial runs until someone stops it.",
    "root-agent": "The agent runs as root (no [agent].user), which widens what it can alter in a shared sandbox.",
    "answer-like-file-in-environment": "A file in environment/ has a name that suggests an answer key. Everything there is visible to the agent.",
    "no-canary": "No canary string in the instruction, config, tests or solution, so leaked copies are hard to detect in training data.",
    "build-context-collision": "A file that a Docker build copies in has the same path and size as a different file in another scanned task. "
                               "Docker can build one task with the other's copy when timestamps also match, as they do after unpacking an archive.",
}


def flags_for(t: dict) -> list[str]:
    out = []
    if t["verifier_mode"] == "shared":
        out.append("shared-verifier")
    if t["verifier_installs"]:
        out.append("verifier-installs-at-test-time")
    if t["verifier_secrets"]:
        out.append("verifier-needs-secret")
    if any(k in ("tag", "latest") for k in t["image_kinds"]):
        out.append("unpinned-image")
    if any(k == "latest" and "/" in i and not t["prebuilt_image"] for i, k in zip(t["images"], t["image_kinds"])):
        out.append("image-not-in-task")
    if t["host_dependency"]:
        out.append("host-dependency")
    if not t["has_solution"]:
        out.append("no-solution")
    if not t["writes_reward"]:
        out.append("no-reward-write")
    if t["agent_timeout"] is None:
        out.append("no-agent-timeout")
    if t["agent_user"] in (None, "root", 0):
        out.append("root-agent")
    if t["answerish_env_files"]:
        out.append("answer-like-file-in-environment")
    if not t["canary"]:
        out.append("no-canary")
    if t["context_collisions"]:
        out.append("build-context-collision")
    return out


# ---------------------------------------------------------------- commands


def cmd_anatomy(args) -> int:
    path = Path(args.path)
    t = read_task(path)
    sep = t["verifier_mode"] == "separate"
    rows = [
        ("task.toml", "harness", f"schema {t['schema_version']}; agent timeout {t['agent_timeout']}s; "
                                 f"verifier {t['verifier_mode']}; network {t['network']}"),
        ("instruction.md" if not t["steps"] else "steps/*/instruction.md", "agent",
         f"{t['instruction_words']} words, passed as the prompt and never written into the sandbox"),
        ("environment/", "agent sandbox",
         f"{t['bytes']['environment']:,} bytes; image from " + (", ".join(t["images"]) or "(none found)")
         + ("; extra services via docker-compose" if t["compose"] else "")),
        ("tests/", "verifier only",
         f"{t['bytes']['tests']:,} bytes; runs in " + t["verifier_image"]
         + ("" if sep else " AFTER the agent finishes, in the same container")),
        ("  tests/test.sh", "verifier entrypoint", f"must write /logs/verifier/reward.{t['reward_kind'] if t['reward_kind'] != 'unknown' else 'txt'}"),
        ("solution/", "oracle agent only",
         (f"{t['bytes']['solution']:,} bytes; copied to /solution and run by `-a oracle`" if t["has_solution"] else "absent")),
    ]
    if t["artifacts"]:
        rows.insert(5, ("  artifacts", "agent -> verifier", "copied out of the agent sandbox for grading: " + ", ".join(t["artifacts"])))
    if t["mcp_servers"]:
        rows.insert(3, ("  mcp servers", "agent tools", ", ".join(f"{m['name']} ({m['transport']})" for m in t["mcp_servers"])))
    print(f"{t['name']}  ({path})\n")
    for name, who, note in rows:
        print(f"  {name:28s} {who:20s} {note}")
    fl = flags_for(t)
    print("\n  flags: " + (", ".join(fl) if fl else "none"))
    return 0


def summarize(tasks: list[dict]) -> dict:
    n = len(tasks)

    def share(pred) -> float:
        return round(sum(1 for t in tasks if pred(t)) / n, 3) if n else 0.0

    def med(key):
        vals = [t[key] for t in tasks if isinstance(t[key], (int, float))]
        return statistics.median(vals) if vals else None

    flag_counts = Counter(f for t in tasks for f in flags_for(t))
    return {
        "tasks": n,
        "schema_versions": dict(Counter(t["schema_version"] for t in tasks).most_common()),
        "verifier_mode": dict(Counter(t["verifier_mode"] for t in tasks).most_common()),
        "network": dict(Counter(t["network"] for t in tasks).most_common()),
        "reward_kind": dict(Counter(t["reward_kind"] for t in tasks).most_common()),
        "share_prebuilt_image": share(lambda t: t["prebuilt_image"]),
        "share_all_images_digest_pinned": share(lambda t: t["images"] and all(k == "digest" for k in t["image_kinds"])),
        "share_compose": share(lambda t: t["compose"]),
        "share_mcp": share(lambda t: t["mcp_servers"]),
        "share_multi_step": share(lambda t: t["steps"]),
        "share_with_solution": share(lambda t: t["has_solution"]),
        "share_pytest": share(lambda t: t["uses_pytest"]),
        "share_verifier_installs": share(lambda t: t["verifier_installs"]),
        "share_verifier_needs_secret": share(lambda t: t["verifier_secrets"]),
        "share_non_root_agent": share(lambda t: t["agent_user"] not in (None, "root", 0)),
        "share_canary": share(lambda t: t["canary"]),
        "median_instruction_words": med("instruction_words"),
        "median_agent_timeout_sec": med("agent_timeout"),
        "median_memory_mb": med("memory_mb"),
        "flags": dict(flag_counts.most_common()),
    }


def load(paths: list[str]) -> list[dict]:
    tasks = [read_task(p) for path in paths for p in find_tasks(Path(path))]
    mark_context_collisions(tasks)
    return tasks


def cmd_census(args) -> int:
    tasks = load(args.path)
    if not tasks:
        print("no task.toml found under", *args.path, file=sys.stderr)
        return 2
    s = summarize(tasks)
    if args.json:
        print(json.dumps({"summary": s, "tasks": tasks} if args.tasks else s, indent=1))
        return 0
    print(f"{s['tasks']} tasks under {', '.join(args.path)}\n")
    for key in ("schema_versions", "verifier_mode", "network", "reward_kind"):
        print(f"  {key:34s} " + ", ".join(f"{k}: {v}" for k, v in s[key].items()))
    for key, val in s.items():
        if key.startswith("share_"):
            print(f"  {key[6:].replace('_', ' '):34s} {val:.0%}")
    for key in ("median_instruction_words", "median_agent_timeout_sec", "median_memory_mb"):
        print(f"  {key.replace('_', ' '):34s} {s[key]}")
    print("\n  flags (tasks affected)")
    for f, c in s["flags"].items():
        print(f"    {f:34s} {c:5d}  {c / s['tasks']:.0%}")
    return 0


def cmd_flags(args) -> int:
    tasks = load(args.path)
    worst = 0
    for t in tasks:
        fl = [f for f in flags_for(t) if f not in (args.ignore or [])]
        worst = max(worst, len(fl))
        if fl or args.all:
            print(f"{t['name']}: " + (", ".join(fl) or "clean"))
    if args.explain:
        seen = {f for t in tasks for f in flags_for(t)}
        print()
        for f in FLAGS:
            if f in seen:
                print(f"{f}\n    {FLAGS[f]}")
    return 1 if (worst and args.strict) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harbor-inspect", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("anatomy", help="show which file is for whom in one task")
    a.add_argument("path")
    a.set_defaults(fn=cmd_anatomy)
    c = sub.add_parser("census", help="summarize every task under one or more directories")
    c.add_argument("path", nargs="+")
    c.add_argument("--json", action="store_true")
    c.add_argument("--tasks", action="store_true", help="with --json, include the per-task records")
    c.set_defaults(fn=cmd_census)
    f = sub.add_parser("flags", help="list reproducibility and grading risks per task")
    f.add_argument("path", nargs="+")
    f.add_argument("--ignore", action="append")
    f.add_argument("--all", action="store_true", help="also list clean tasks")
    f.add_argument("--explain", action="store_true")
    f.add_argument("--strict", action="store_true", help="exit 1 if any task has a flag")
    f.set_defaults(fn=cmd_flags)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
