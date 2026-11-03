#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parents[2]
MODEL_DIR = SKILL_DIR / "model"
COVERAGE_FILE = SKILL_DIR / "coverage.json"
SKILL_FILE = SKILL_DIR / "SKILL.md"
REPLAY_FILE = SKILL_DIR / "scripts" / "replay_findings.py"
SPEC_FILE = "CronOccurrences.tla"

TLA_RELEASE = "v1.7.4"
TLA_URL = f"https://github.com/tlaplus/tlaplus/releases/download/{TLA_RELEASE}/tla2tools.jar"
TLA_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
MIN_JAVA_MAJOR = 11
TLC_TIMEOUT_SECONDS = 900

SKIPPED_FIELDS = frozenset({"kind", "type_comment"})
FACT_TOKEN = re.compile(r"^(?P<name>\w+)(?:\((?:(?P<keyword>\w+)=|'(?P<const>[^']*)')\))?$")
SYNC_LINE = re.compile(r"(?m)^Sync commit: ([0-9a-f]{40})$")


@dataclass
class ModelRun:
    cfg: str
    outcome: str
    generated: int = 0
    distinct: int = 0
    depth: int = 0
    seconds: float = 0.0
    out_file: Path | None = None
    trace: list[str] = field(default_factory=list)


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "hermes-agent-verify"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_tla_jar(explicit: str | None) -> Path:
    candidate = explicit or os.environ.get("TLA2TOOLS_JAR")
    if candidate:
        jar = Path(candidate)
        if not jar.is_file() or sha256_of(jar) != TLA_SHA256:
            raise SystemExit(f"{jar} is not the pinned tla2tools {TLA_RELEASE} (sha256 {TLA_SHA256})")
        return jar
    jar = cache_dir() / f"tla2tools-{TLA_RELEASE}.jar"
    if jar.is_file() and sha256_of(jar) == TLA_SHA256:
        return jar
    jar.parent.mkdir(parents=True, exist_ok=True)
    partial = jar.with_suffix(".part")
    try:
        with urllib.request.urlopen(TLA_URL, timeout=120) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise SystemExit(
            f"cannot download {TLA_URL}: {exc}\n"
            f"Fetch it any other way and pass --tla2tools PATH (sha256 must be {TLA_SHA256})."
        ) from exc
    if sha256_of(partial) != TLA_SHA256:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"downloaded tla2tools does not match the pinned sha256 {TLA_SHA256}")
    partial.replace(jar)
    return jar


def find_java() -> str:
    java = shutil.which("java")
    if not java:
        raise SystemExit(f"java {MIN_JAVA_MAJOR}+ is required to run TLC and was not found on PATH")
    probe = subprocess.run(
        [java, "-version"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60, check=False
    )
    match = re.search(r'version "(\d+)', probe.stderr)
    if not match or int(match.group(1)) < MIN_JAVA_MAJOR:
        raise SystemExit(f"java {MIN_JAVA_MAJOR}+ is required, found: {probe.stderr.strip().splitlines()[-1:]}")
    return java


def run_model(java: str, jar: Path, cfg: str, out_root: Path) -> ModelRun:
    workdir = out_root / cfg
    shutil.copytree(MODEL_DIR, workdir)
    out_file = workdir / "tlc.out"
    command = [
        java, "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC",
        "-workers", str(min(4, os.cpu_count() or 1)), "-metadir", str(workdir / "states"),
        "-config", f"{cfg}.cfg", SPEC_FILE,
    ]
    started = time.monotonic()
    try:
        with out_file.open("w", encoding="utf-8") as sink:
            subprocess.run(
                command, cwd=workdir, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                timeout=TLC_TIMEOUT_SECONDS, check=False,
            )
    except subprocess.TimeoutExpired:
        return ModelRun(cfg, "error:timeout", seconds=time.monotonic() - started, out_file=out_file)
    return parse_tlc_output(cfg, out_file, time.monotonic() - started)


def parse_tlc_output(cfg: str, out_file: Path, seconds: float) -> ModelRun:
    text = out_file.read_text(encoding="utf-8", errors="replace")
    counts = re.findall(r"(\d+) states generated, (\d+) distinct states found", text)
    depth = re.search(r"depth of the complete state graph search is (\d+)", text)
    violated = re.search(r"Error: Invariant (\w+) is violated", text)
    if violated:
        outcome = f"violates:{violated.group(1)}"
    elif "Model checking completed. No error has been found." in text:
        outcome = "pass"
    else:
        first_error = next((line for line in text.splitlines() if line.startswith("Error:")), "no result")
        outcome = f"error:{first_error[:80]}"
    generated, distinct = (int(counts[-1][0]), int(counts[-1][1])) if counts else (0, 0)
    trace = re.findall(r"^State \d+: <(\w+)", text, flags=re.MULTILINE)
    return ModelRun(cfg, outcome, generated, distinct, int(depth.group(1)) if depth else 0, seconds, out_file, trace)


def strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return body[1:]
    return body


def canonical(node: object) -> str:
    if isinstance(node, ast.AST):
        parts = []
        for name, value in ast.iter_fields(node):
            if name in SKIPPED_FIELDS or (name == "type_params" and not value):
                continue
            if name == "body" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                value = strip_docstring(value)
            parts.append(f"{name}={canonical(value)}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(canonical(item) for item in node) + "]"
    return repr(node)


def find_definition(tree: ast.Module, name: str) -> ast.AST | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return node
    return None


def callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def call_matches(call: ast.Call, token: str) -> bool:
    parsed = FACT_TOKEN.match(token)
    if not parsed or callee_name(call) != parsed.group("name"):
        return False
    if parsed.group("keyword"):
        return any(keyword.arg == parsed.group("keyword") for keyword in call.keywords)
    if parsed.group("const") is not None:
        first = call.args[0] if call.args else None
        return isinstance(first, ast.Constant) and first.value == parsed.group("const")
    return True


def missing_order(definition: ast.AST, tokens: list[str]) -> str | None:
    calls = sorted(
        (node for node in ast.walk(definition) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    cursor = (0, -1)
    for token in tokens:
        hit = next((c for c in calls if (c.lineno, c.col_offset) > cursor and call_matches(c, token)), None)
        if hit is None:
            return token
        cursor = (hit.lineno, hit.col_offset)
    return None


def load_coverage() -> dict:
    return json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))


def check_code(coverage: dict) -> tuple[list[str], dict[str, str]]:
    problems: list[str] = []
    current: dict[str, str] = {}
    for entry in coverage["covered"]:
        key = f"{entry['file']}::{entry['name']}"
        path = REPO_ROOT / entry["file"]
        if not path.is_file():
            problems.append(f"MISSING {key}: file is gone; the model no longer points at code")
            continue
        definition = find_definition(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), entry["name"])
        if definition is None:
            problems.append(f"MISSING {key}: definition moved or renamed; re-read the model action(s) {entry['actions']}")
            continue
        current[key] = hashlib.sha256(canonical(definition).encode("utf-8")).hexdigest()
        if current[key] != entry["sha256"]:
            problems.append(f"DRIFT   {key}: changed since the recorded sync; actions {entry['actions']} need another look")
        for fact in entry.get("facts", []):
            absent = missing_order(definition, fact["order"])
            if absent is not None:
                problems.append(
                    f"FACT    {key}: call order {fact['order']} broken at {absent!r}; "
                    f"mirrors {fact['mirrors']} ({fact['why']})"
                )
    return problems, current


def read_skill_sync() -> str | None:
    match = SYNC_LINE.search(SKILL_FILE.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def check_sync(coverage: dict) -> list[str]:
    in_skill = read_skill_sync()
    if in_skill != coverage["sync_commit"]:
        return [f"SYNC    SKILL.md says {in_skill} but coverage.json says {coverage['sync_commit']}; move them together"]
    known = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{in_skill}^{{commit}}"],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=60, check=False,
    )
    if known.returncode != 0:
        print(f"note: sync commit {in_skill[:12]} is not in this clone's history (shallow?); fetch it to diff against it")
    return []


def record(coverage: dict, sync: str) -> None:
    _, hashes = check_code(coverage)
    for entry in coverage["covered"]:
        entry["sha256"] = hashes[f"{entry['file']}::{entry['name']}"]
    coverage["sync_commit"] = sync
    COVERAGE_FILE.write_text(json.dumps(coverage, indent=2) + "\n", encoding="utf-8")
    text = SKILL_FILE.read_text(encoding="utf-8")
    SKILL_FILE.write_text(SYNC_LINE.sub(f"Sync commit: {sync}", text, count=1), encoding="utf-8")
    print(f"recorded {len(coverage['covered'])} fingerprints and sync commit {sync}")


def run_replay() -> tuple[str, str]:
    scratch = tempfile.mkdtemp(prefix="verify-cron-home-")
    env = dict(os.environ, HERMES_HOME=scratch)
    probe = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); import cron.jobs"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=120, check=False,
    )
    shutil.rmtree(scratch, ignore_errors=True)
    if probe.returncode != 0:
        reason = (probe.stderr.strip().splitlines() or ["import failed"])[-1]
        return "skipped", f"Hermes environment not importable by {sys.executable}: {reason}"
    done = subprocess.run(
        [sys.executable, "-I", str(REPLAY_FILE)], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, check=False, timeout=120,
    )
    verdicts = " | ".join(line for line in done.stdout.splitlines() if line.startswith("F-"))
    return {0: "reproduces", 3: "changed"}.get(done.returncode, "error"), verdicts or done.stderr.strip()[-200:]


def describe(run: ModelRun, entry: dict) -> str:
    states = f"{run.distinct:>8} states" if run.distinct else " " * 15
    label = entry.get("finding") or ""
    return f"{entry['kind']:<8} {run.cfg:<30} {run.outcome:<24} {label:<4} {states} {run.seconds:5.1f}s"


def main() -> int:
    parser = argparse.ArgumentParser(description="Model-check and drift-check the cron occurrence protocol.")
    parser.add_argument("--only", help="comma list of cfg names to run")
    parser.add_argument("--model-only", action="store_true")
    parser.add_argument("--code-only", action="store_true")
    parser.add_argument("--no-replay", action="store_true")
    parser.add_argument("--out", help="directory for TLC output (default: a new temp dir)")
    parser.add_argument("--tla2tools", help="path to the pinned tla2tools.jar")
    parser.add_argument("--record", action="store_true", help="re-fingerprint covered code (needs --sync)")
    parser.add_argument("--sync", help="commit SHA the verification was confirmed against")
    args = parser.parse_args()

    coverage = load_coverage()
    if args.record:
        if not args.sync or not re.fullmatch(r"[0-9a-f]{40}", args.sync):
            parser.error("--record needs --sync <40-hex commit SHA>")
        record(coverage, args.sync)
        return 0

    failures = 0
    if not args.model_only:
        problems = check_sync(coverage) + check_code(coverage)[0]
        print(f"CODE   {len(coverage['covered'])} covered definitions, sync {coverage['sync_commit'][:12]}")
        for problem in problems:
            print(f"  {problem}")
        failures += len(problems)
        if not problems:
            print("  every covered definition matches its recorded fingerprint and call-order facts")

    if not args.code_only:
        jar = ensure_tla_jar(args.tla2tools)
        java = find_java()
        out_root = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="verify-cron-tlc-"))
        wanted = set(args.only.split(",")) if args.only else None
        print(f"MODEL  TLC {TLA_RELEASE} (sha256 verified), output in {out_root}")
        for entry in coverage["runs"]:
            if wanted and entry["cfg"] not in wanted:
                continue
            run = run_model(java, jar, entry["cfg"], out_root)
            matched = run.outcome == entry["expect"]
            print(f"  {'OK  ' if matched else 'FAIL'} {describe(run, entry)}")
            if not matched:
                failures += 1
                print(f"       expected {entry['expect']}, got {run.outcome}: {entry['why']}")
                print(f"       trace: {' > '.join(run.trace[-14:])}")
        if not args.no_replay and not wanted:
            status, detail = run_replay()
            print(f"REPLAY {status}: {detail}")
            if status in ("changed", "error"):
                failures += 1

    print("RESULT " + ("PASS" if failures == 0 else f"FAIL ({failures} problem(s))"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
