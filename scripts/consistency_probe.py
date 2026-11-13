#!/usr/bin/env python3
"""Read-only consistency probe for `fact-ledger-kernel` conformance.

Two gates and one advisory list, all static:

  P1  idempotency key    — a package that writes rows must have a uniqueness
                           anchor for the write (UNIQUE(...) / ON CONFLICT /
                           INSERT ... ON CONFLICT). A writer with no anchor
                           duplicates on retry.                     [gate]
  P2  whole-file rewrite — a function that reads a whole collection, mutates one
                           element, and writes the whole thing back. That makes
                           history mutable and loses concurrent appends. [gate]
  P3  unknown-as-empty   — `x.get(k) or ""` / `d.get(k, 0)` style defaults: the
                           idiom that turns "we did not look" into "there is
                           nothing".               [advisory — see below]

Why P3 is advisory and not a gate
--------------------------------
`dict.get(k, "")` is often *correct* (an optional display field really is empty).
A static scanner cannot tell "display default" from "unknown disguised as a fact",
so demanding it be red would produce noise that gets suppressed — and a suppressed
check is worse than none, because it teaches people to ignore the output.

What P3 does instead: lists every occurrence so a reviewer can look. The hard
requirement lives in the contract (R8) and in each consumer's own tests
(`fd_agent_run/fact.py` is the reference implementation).

The rest of the contract (R5 gate, R7 resume) needs behavioural tests and belongs
to each consumer, not to a scanner — a scanner that pretends to check everything
checks nothing.

Contract: writes nothing, ever. One line per finding, with the fix. Exit 1 on
gate findings, 0 otherwise, 2 on a usage error.

    python3 scripts/consistency_probe.py --root <pkg-dir> [--root ...]
    python3 scripts/consistency_probe.py --root <pkg-dir> --advisory
    python3 scripts/consistency_probe.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "build", "dist", "graphify-out", "archive", "tests"}

#: Method names that write a whole collection back over its file.
WHOLE_WRITE_ATTRS = {"write_text", "rewrite"}

#: SQL/py anchors that make a write idempotent.
IDEMPOTENCY_SQL = re.compile(r"\bUNIQUE\s*\(", re.IGNORECASE)
IDEMPOTENCY_PY = re.compile(r"ON\s+CONFLICT|on_conflict|IntegrityError", re.IGNORECASE)
#: Evidence that a module actually writes rows.
DB_WRITE = re.compile(r"\bINSERT\s+INTO\b|session\.add\(|\.add\(SemanticObservation", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    probe: str
    path: str
    line: int
    subject: str
    fix: str
    #: "gate" 影响退出码；"advisory" 只列给人看（静态无法判定语义）。
    severity: str = "gate"

    def render(self, root: Path) -> str:
        rel = os.path.relpath(self.path, root)
        mark = "" if self.severity == "gate" else " (advisory)"
        return f"{rel}:{self.line}: [{self.probe}{mark}] {self.subject} — fix: {self.fix}"


def _iter_files(root: Path, suffixes: tuple[str, ...]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(suffixes):
                yield Path(dirpath) / name


def probe_p2_whole_rewrite(root: Path) -> list[Finding]:
    """Flag 'read all -> mutate one field -> write all back' inside one function."""
    findings: list[Finding] = []
    for path in _iter_files(root, (".py",)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            writes_all = None
            mutates_field = None
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr in WHOLE_WRITE_ATTRS and writes_all is None:
                        writes_all = sub.lineno
                if isinstance(sub, ast.Assign) and mutates_field is None:
                    for target in sub.targets:
                        if isinstance(target, ast.Subscript):
                            mutates_field = sub.lineno
            if writes_all and mutates_field:
                findings.append(
                    Finding(
                        probe="P2",
                        path=str(path),
                        line=writes_all,
                        subject=f"{node.name}() mutates one field and writes the whole file back",
                        fix=(
                            "append a decision/revision fact and derive the current value; "
                            "never rewrite the store to change one field"
                        ),
                    )
                )
    return findings


def probe_p1_idempotency(root: Path) -> list[Finding]:
    """A package that writes rows must anchor the write on a unique key."""
    files = list(_iter_files(root, (".py", ".sql")))
    anchors = []
    writers = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if path.suffix == ".sql":
            if IDEMPOTENCY_SQL.search(text):
                anchors.append(str(path))
            if DB_WRITE.search(text):
                writers.append(str(path))
        else:
            # Inline schema strings are common (SQLite DDL kept next to the code),
            # so a Python module counts as anchored if it declares UNIQUE too.
            if IDEMPOTENCY_PY.search(text) or IDEMPOTENCY_SQL.search(text):
                anchors.append(str(path))
            if DB_WRITE.search(text):
                writers.append(str(path))
    if writers and not anchors:
        return [
            Finding(
                probe="P1",
                path=writers[0],
                line=1,
                subject=f"package writes rows ({len(writers)} file(s)) but has no uniqueness anchor",
                fix=(
                    "add UNIQUE(scope, idempotency_key) or INSERT ... ON CONFLICT DO NOTHING "
                    "so a retry cannot duplicate"
                ),
            )
        ]
    return []


#: "no content" defaults — the shapes an unknown value gets mistaken for.
EMPTY_DEFAULTS = ("", 0, 0.0, False, [], {})

#: 只对"事实性字段"报候选。收窄的理由是实测出来的：不收窄时
#: `fd-erp-vertical-templates` 有 165 处命中，绝大多数是遍历用的 `or []`，
#: 165 行清单没人会读——那和被压制的检查是同一个下场。
#: 这份集合故意小：它覆盖 R8 举出的形态（谁跑的/上游/退出码/耗时/依据/证据/来源）。
FACT_KEYS = {
    "owner", "upstreams", "upstream", "exit_code", "exit", "seconds", "duration",
    "reason", "evidence", "source", "source_used", "status", "value", "count",
    "rows", "fingerprint", "error", "error_head", "started_at", "finished_at",
}


def _literal_key(call: ast.Call) -> str | None:
    """取 `.get("k")` 里的字面量键名；非字面量（变量/表达式）返回 None。"""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _is_empty_default(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in ("", 0, 0.0, False)
    if isinstance(node, (ast.List, ast.Tuple)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    return False


def probe_p3_unknown_as_empty(root: Path) -> list[Finding]:
    """列出"未知被填成空/零/默认值"的可疑写法（**advisory，不影响退出码**）。

    两种形态：
      ``x.get(k) or ""``    —— 取不到就当空
      ``x.get(k, 0)``       —— 取不到就给零

    静态分析分不清"可选展示字段的合理默认"与"未知伪装成事实"，因此这里只列出、
    不判负。真正的约束在契约 R8，落点在各消费者自己的用例（参考实现见
    ``fd_agent_run/fact.py`` 的 ``UNKNOWN_MARKER``）。
    """
    findings: list[Finding] = []
    for path in _iter_files(root, (".py",)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                left = node.values[0] if node.values else None
                right = node.values[-1] if node.values else None
                if (
                    isinstance(left, ast.Call)
                    and isinstance(left.func, ast.Attribute)
                    and left.func.attr == "get"
                    and _literal_key(left) in FACT_KEYS
                    and right is not None
                    and _is_empty_default(right)
                ):
                    findings.append(Finding(
                        probe="P3", path=str(path), line=node.lineno,
                        subject=".get(...) or <empty> — 取不到就当成空",
                        fix=("区分「没去查」与「确实为空」：未探查用显式未知标记，"
                             "不要落成 '' / 0 / []"),
                        severity="advisory",
                    ))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and len(node.args) >= 2 \
                    and _literal_key(node) in FACT_KEYS \
                    and _is_empty_default(node.args[1]):
                findings.append(Finding(
                    probe="P3", path=str(path), line=node.lineno,
                    subject=".get(key, <empty>) — 缺值时给默认空/零",
                    fix=("若该键可能「没去查」，改用显式未知标记；只有确实可空时才用空默认"),
                    severity="advisory",
                ))
    return findings


def probe(root: Path) -> list[Finding]:
    return probe_p2_whole_rewrite(root) + probe_p1_idempotency(root)


def probe_advisory(root: Path) -> list[Finding]:
    return probe_p3_unknown_as_empty(root)


# ------------------------------------------------------------------ self-test

_FIXTURE_CLEAN = '''
"""A writer with a uniqueness anchor, honest unknown handling, no whole-file rewrite."""
import sqlite3

SCHEMA = "CREATE TABLE t (tenant TEXT, k TEXT, v TEXT, UNIQUE (tenant, k));"
UNKNOWN = "$unknown"


def land(conn, tenant, key, value):
    conn.execute("INSERT INTO t(tenant, k, v) VALUES (?, ?, ?)", (tenant, key, value))


def owner_of(fact):
    # 未知用显式标记表达，不落成空串
    return fact.get("owner", UNKNOWN)
'''

_FIXTURE_DIRTY = '''
"""Reads everything, changes one status, writes everything back."""
import json
from pathlib import Path


class Log:
    def __init__(self, path):
        self.path = Path(path)

    def events(self):
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]

    def rewrite(self, events):
        self.path.write_text("\\n".join(json.dumps(e) for e in events))


class Facts:
    def owner_of(self, fact):
        # 把"没去查"写成空串：正是 R8 要区分的形态
        return fact.get("owner") or ""

    def duration_of(self, fact):
        return fact.get("seconds", 0)


class Lane:
    def __init__(self, log):
        self.log = log

    def set_status(self, request_id, decision):
        events = self.log.events()
        target = next(e for e in events if e.get("request_id") == request_id)
        target["status"] = decision
        self.log.rewrite(events)
'''


def self_test() -> int:
    """A probe that cannot tell clean from dirty is worse than no probe."""
    with tempfile.TemporaryDirectory(prefix="probe-") as tmp:
        clean = Path(tmp) / "clean"
        dirty = Path(tmp) / "dirty"
        for d, src in ((clean, _FIXTURE_CLEAN), (dirty, _FIXTURE_DIRTY)):
            (d / "pkg").mkdir(parents=True)
            (d / "pkg" / "mod.py").write_text(src, encoding="utf-8")

        clean_findings = probe(clean)
        dirty_findings = probe(dirty)

        ok = True
        if clean_findings:
            print(f"FAIL clean fixture produced {len(clean_findings)} finding(s):")
            for f in clean_findings:
                print("  " + f.render(clean))
            ok = False
        else:
            print("ok  clean fixture: 0 findings")

        if not any(f.probe == "P2" for f in dirty_findings):
            print("FAIL dirty fixture: P2 did not fire (the read-all/rewrite-all shape)")
            ok = False
        else:
            print(f"ok  dirty fixture: {len(dirty_findings)} finding(s), P2 fired")

        # P3：收窄到事实字段后，事实字段名必须命中，遍历用的普通键必须不命中。
        p3 = probe_advisory(clean) + probe_advisory(dirty)
        if not any(f.probe == "P3" for f in p3):
            print("FAIL P3 did not fire on an owner/exit_code default")
            ok = False
        else:
            print(f"ok  P3 fired {sum(1 for f in p3 if f.probe == 'P3')} time(s)")
        if any(f.severity == "gate" for f in p3):
            print("FAIL P3 must be advisory, not a gate")
            ok = False

        return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="read-only consistency probe")
    ap.add_argument("--root", action="append", default=[], help="package dir to scan (repeatable)")
    ap.add_argument("--self-test", action="store_true", help="run the fixtures")
    ap.add_argument("--advisory", action="store_true",
                    help="also list P3 candidates (unknown-as-empty); never changes the exit code")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.root:
        print("error: --root is required (or --self-test)", file=sys.stderr)
        return 2

    code = 0
    for raw in args.root:
        root = Path(raw).resolve()
        if not root.is_dir():
            print(f"error: not a directory: {root}", file=sys.stderr)
            return 2
        findings = probe(root)
        for f in findings:
            print(f.render(root))
        if args.advisory:
            adv = probe_advisory(root)
            for f in adv:
                print(f.render(root))
            if adv:
                print(f"{root.name}: {len(adv)} P3 candidate(s) — 人工判断，不影响结论")
        if findings:
            code = 1
        elif not (args.advisory and probe_advisory(root)):
            print(f"{root.name}: clean (0 findings)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
