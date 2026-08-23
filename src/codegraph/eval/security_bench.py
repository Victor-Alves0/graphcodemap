"""Contrato reproduzível para benchmarks SAST competitivos.

Normaliza GraphCodeMap, SARIF (CodeQL/OpenTaint) e JSON do OpenGrep no mesmo
schema. O objetivo é impedir comparações por contagem que misturam commits,
categorias, locais ou execuções incompletas.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

SCHEMA_VERSION = 1
ADAPTER_VERSION = "1.1"
_MAX_ARGUMENT_LITERAL_BYTES = 128
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CWE_CATEGORY = {
    "22": "path-traversal",
    "78": "command-injection",
    "79": "xss",
    "89": "sql-injection",
    "90": "ldap-injection",
    "94": "code-injection",
    "327": "weak-crypto",
    "330": "weak-randomness",
    "501": "trust-boundary",
    "502": "unsafe-deserialization",
    "601": "open-redirect",
    "611": "xxe",
    "643": "xpath-injection",
    "918": "ssrf",
}
CATEGORY_CWE = {category: cwe for cwe, category in CWE_CATEGORY.items()}

_CATEGORY_WORDS = (
    ("sql-injection", ("sql injection", "sqli", "database query", ".execute", "sequelize.query")),
    ("command-injection", ("command injection", "cmdi", "os command", "shell injection", "exec(", "system(")),
    ("path-traversal", ("path traversal", "pathtraver", "directory traversal")),
    ("xss", ("cross-site scripting", "cross site scripting", " xss", "html injection")),
    ("ldap-injection", ("ldap injection", "ldapi")),
    ("xpath-injection", ("xpath injection", "xpathi")),
    ("trust-boundary", ("trust boundary", "trustbound")),
    ("ssrf", ("server-side request forgery", "server side request forgery", "ssrf")),
    ("xxe", ("external entity", " xxe")),
    ("unsafe-deserialization", ("deserial", "object input stream", "pickle.load")),
    ("open-redirect", ("open redirect", "unvalidated redirect")),
    ("code-injection", ("code injection", "eval(")),
    ("weak-randomness", ("weak random", "insecure random")),
    ("weak-crypto", ("weak crypt", "broken crypt")),
)

_SINK_CATEGORY = {
    # SQL
    **{name: "sql-injection" for name in (
        "execute", "executemany", "executescript", "executequery",
        "executeupdate", "executelargeupdate", "addbatch", "batchupdate",
        "query", "queryforobject", "queryforlist", "queryformap",
        "queryforrowset", "preparestatement", "preparecall", "mysqli_query",
        "mysqli_multi_query", "mysql_query", "pg_query", "sqlite_query",
        "sqlsrv_query", "oci_parse")},
    # command/code execution
    **{name: "command-injection" for name in (
        "exec", "execsync", "execfilesync", "system", "popen", "spawn",
        "spawnsync", "check_output", "check_call", "shell_exec", "passthru",
        "proc_open", "pcntl_exec", "processbuilder")},
    **{name: "code-injection" for name in (
        "eval", "compile", "function", "runinnewcontext", "runinthiscontext",
        "runincontext", "create_function")},
    # output/path/serialization
    **{name: "xss" for name in (
        "innerhtml", "insertadjacenthtml", "writeln", "println", "print",
        "write", "printf", "format", "send", "render_template_string")},
    **{name: "path-traversal" for name in (
        "open", "readfile", "fopen", "file_get_contents", "file_put_contents",
        "sendfile", "download", "readfilesync", "writefile", "writefilesync",
        "createreadstream", "createwritestream", "scandir", "opendir", "glob",
        # Java constructors and java.nio.file.Files APIs. These are already
        # sinks in the product catalog; the benchmark adapter must retain the
        # vulnerability kind instead of reporting a misleading `unknown`.
        "file", "fileinputstream", "fileoutputstream", "filereader",
        "filewriter", "randomaccessfile", "newinputstream", "newoutputstream",
        "newbufferedreader", "newbufferedwriter", "newbytechannel",
        "newdirectorystream", "readallbytes", "readalllines",
        "createfile", "createdirectories", "delete", "deleteifexists",
        "copy", "move", "walkfiletree")},
    **{name: "unsafe-deserialization" for name in (
        "load", "loads", "unserialize", "deserialize", "readobject")},
    **{name: "ldap-injection" for name in ("ldap_search", "ldap_bind", "search")},
    **{name: "xpath-injection" for name in ("xpath", "evaluate", "compileexpression")},
    **{name: "open-redirect" for name in ("redirect", "header")},
    **{name: "xxe" for name in ("parsexml", "parsexmlstring")},
    "setattribute": "trust-boundary",
    "putvalue": "trust-boundary",
    "command": "command-injection",
}


def _flatten(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten(item)


def infer_cwes(*values: Any) -> list[str]:
    found: set[str] = set()
    for text in _flatten(values):
        for match in re.finditer(r"(?i)\bCWE(?:[-_ /:]|%2[fF])*(\d{1,4})\b", text):
            found.add(str(int(match.group(1))))
    return sorted(found, key=int)


def infer_category(cwes: Iterable[str], *values: Any) -> str:
    for cwe in cwes:
        category = CWE_CATEGORY.get(str(cwe))
        if category:
            return category
    text = " ".join(_flatten(values)).lower()
    for category, needles in _CATEGORY_WORDS:
        if any(needle in text for needle in needles):
            return category
    return "unknown"


def infer_graphcodemap_category(sink: str, raw: Any = None) -> str:
    nested_sink = raw.get("sink", {}) if isinstance(raw, dict) else {}
    qualified = str(nested_sink.get("qualified") or "").lower()
    # `compile` sozinho é ambíguo (código vs XPath). O receptor sintático
    # preservado no finding separa `xpath.compile(expr)` de um compilador.
    if re.split(r"[.:#]", sink or "")[-1].lower() == "compile" \
            and ("xpath" in qualified or qualified.startswith("xp.")):
        return "xpath-injection"
    tail = re.split(r"[.:#]", sink or "")[-1].lower()
    return _SINK_CATEGORY.get(tail) or infer_category((), sink, raw)


def _is_safe_relative_path(value: object, *, allow_dot: bool = False) -> bool:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    normalized = value.strip().replace("\\", "/")
    if normalized == ".":
        return allow_dot
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(value.strip())
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return False
    return all(part not in {"", ".", ".."} for part in posix.parts)


def normalize_path(value: str | None, root: str | Path | None = None) -> str | None:
    if not value:
        return None
    raw = unquote(str(value)).strip()
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        raw = (f"//{parsed.netloc}{parsed.path}"
               if parsed.netloc else parsed.path)
        if re.match(r"^/[A-Za-z]:/", raw):
            raw = raw[1:]
    elif (parsed.scheme and root
          and not re.match(r"^[A-Za-z]:[\\/]", raw)):
        return None
    raw = raw.replace("\\", "/")
    if root:
        if any(part == ".." for part in PurePosixPath(raw).parts):
            return None
        base = Path(root).resolve()
        candidate = Path(raw) if (PurePosixPath(raw).is_absolute()
                                  or PureWindowsPath(raw).is_absolute()) \
            else base.joinpath(*PurePosixPath(raw).parts)
        try:
            relative = candidate.resolve(strict=False).relative_to(base)
        except (OSError, ValueError):
            return None
        normalized = relative.as_posix()
        return normalized if _is_safe_relative_path(normalized) else None
    while raw.startswith("./"):
        raw = raw[2:]
    return raw or None


def _line(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _one_based_column(value: Any) -> int | None:
    """Convert GraphCodeMap/tree-sitter zero-based columns for report JSON."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number + 1 if number >= 0 else None


def location(path: str | None, line: Any = None, column: Any = None,
             label: str | None = None, symbol: str | None = None,
             root: str | Path | None = None, byte_span: Any = None,
             argument_literals: Any = None,
             parameter: str | None = None) -> dict | None:
    path = normalize_path(path, root)
    if not path:
        return None
    out: dict[str, Any] = {"path": path, "line": _line(line)}
    col = _line(column)
    if col is not None:
        out["column"] = col
    if label:
        out["label"] = str(label)
    if symbol:
        out["symbol"] = str(symbol)
    if isinstance(byte_span, dict):
        start, end = byte_span.get("start"), byte_span.get("end")
        if (isinstance(start, int) and not isinstance(start, bool)
                and isinstance(end, int) and not isinstance(end, bool)
                and 0 <= start <= end):
            out["byte_span"] = {"start": start, "end": end}
    if isinstance(argument_literals, dict):
        published = {}
        for index, value in argument_literals.items():
            try:
                normalized_index = int(index)
            except (TypeError, ValueError):
                continue
            if (normalized_index < 0 or not isinstance(value, str)
                    or not value or not value.isprintable()
                    or len(value.encode("utf-8"))
                    > _MAX_ARGUMENT_LITERAL_BYTES):
                continue
            published[str(normalized_index)] = value
        if published:
            out["argument_literals"] = published
    if (isinstance(parameter, str) and parameter.isprintable()
            and 0 < len(parameter.encode("utf-8"))
            <= _MAX_ARGUMENT_LITERAL_BYTES):
        out["parameter"] = parameter
    return out


def _fingerprint(finding: dict) -> str:
    stable = {
        "category": finding["category"],
        "cwes": finding["cwes"],
        "source": finding.get("source"),
        "sink": finding["sink"],
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def make_finding(*, rule_id: str, message: str = "", severity: str = "warning",
                 cwes: Iterable[str] = (), category: str = "unknown",
                 source: dict | None = None, sink: dict,
                 confidence: str | None = None,
                 evidence: str | None = None,
                 steps: list[dict] | None = None) -> dict:
    cwe_list = sorted({str(int(c)) for c in cwes if str(c).isdigit()}, key=int)
    finding: dict[str, object] = {
        "rule_id": str(rule_id or "unknown"),
        "category": category or "unknown",
        "cwes": cwe_list,
        "severity": str(severity or "warning").lower(),
        "message": str(message or ""),
        "source": source,
        "sink": sink,
        "confidence": confidence,
        "evidence": evidence,
    }
    if steps:
        finding["steps"] = steps
    finding["fingerprint"] = _fingerprint(finding)
    return finding


def git_metadata(root: str | Path) -> dict:
    root = Path(root).resolve()

    def git(*args: str) -> str | None:
        try:
            p = subprocess.run(["git", "-C", str(root), *args], text=True,
                               capture_output=True, timeout=10, check=False)
            return p.stdout.strip() if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    commit = git("rev-parse", "HEAD")
    # Scope dirtiness to the scanned root, but include untracked inputs: an
    # untracked source file can change findings just as much as a tracked edit.
    status = git("status", "--porcelain", "--untracked-files=all", "--", ".")
    remote = git("config", "--get", "remote.origin.url")
    top_level = git("rev-parse", "--show-toplevel")
    if remote:
        remote = remote.strip().rstrip("/")
        scp = re.fullmatch(r"git@([^:]+):(.+)", remote)
        ssh = re.fullmatch(r"ssh://git@([^/]+)/(.+)", remote)
        if scp:
            remote = f"https://{scp.group(1)}/{scp.group(2)}"
        elif ssh:
            remote = f"https://{ssh.group(1)}/{ssh.group(2)}"
    scan_subdir = None
    if top_level:
        try:
            relative = root.relative_to(Path(top_level).resolve())
            scan_subdir = relative.as_posix() or "."
        except ValueError:
            pass
    return {"commit": commit, "dirty": bool(status) if status is not None else None,
            "remote": remote, "top_level": top_level,
            "scan_subdir": scan_subdir}


def _source_tree_sha256(repo_root: Path) -> str | None:
    """Hash the executable GraphCodeMap Python source deterministically."""
    source_root = repo_root / "src" / "codegraph"
    try:
        files = sorted(path for path in source_root.rglob("*.py")
                       if path.is_file())
        if not files:
            return None
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.relative_to(repo_root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()
    except OSError:
        return None


def _engine_provenance(tool: dict) -> dict:
    repo_root = Path(__file__).resolve().parents[3]
    metadata = git_metadata(repo_root)
    source_hash = _source_tree_sha256(repo_root)
    provenance = dict(tool)
    if (isinstance(metadata["commit"], str)
            and isinstance(metadata["dirty"], bool)
            and (not metadata["dirty"] or source_hash is not None)):
        provenance.update({
            "git_commit": metadata["commit"],
            "git_dirty": metadata["dirty"],
            "git_remote": metadata["remote"],
            "source_tree_sha256": source_hash,
        })
    return provenance


def _driver_rules(run: dict) -> dict[str, dict]:
    driver = (run.get("tool") or {}).get("driver") or {}
    return {str(rule.get("id")): rule for rule in driver.get("rules", [])
            if isinstance(rule, dict) and rule.get("id")}


def _sarif_loc(item: dict | None, root=None) -> dict | None:
    physical = (item or {}).get("physicalLocation") or {}
    artifact = physical.get("artifactLocation") or {}
    region = physical.get("region") or {}
    logical = (item or {}).get("logicalLocations") or []
    symbol = logical[0].get("fullyQualifiedName") if logical else None
    message = (item or {}).get("message") or {}
    return location(artifact.get("uri"), region.get("startLine"),
                    region.get("startColumn"),
                    message.get("text") or message.get("markdown"), symbol, root)


def _sarif_flow(result: dict, root=None) -> tuple[dict | None, dict | None]:
    points: list[dict] = []
    for flow in result.get("codeFlows") or []:
        for thread in flow.get("threadFlows") or []:
            for item in thread.get("locations") or []:
                loc = _sarif_loc(item.get("location") or item, root)
                if loc:
                    points.append(loc)
    return ((points[0], points[-1]) if points else (None, None))


def _sarif_message(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    message = value.get("message")
    if isinstance(message, dict):
        return str(message.get("text") or message.get("markdown") or "").strip()
    return str(message or "").strip()


def sarif_execution_health(data: object) -> tuple[bool, list[str], int | None]:
    """Return SARIF execution partial/errors/exit evidence, fail-closed.

    A SARIF run may omit ``invocations`` entirely, but every invocation that is
    present must publish the required ``executionSuccessful`` boolean. Error
    notifications and non-zero embedded exit codes are execution failures even
    when findings were serialized successfully.
    """
    if not isinstance(data, dict):
        return True, ["SARIF root must be an object"], None
    runs = data.get("runs")
    if not isinstance(runs, list) or not runs:
        return True, ["SARIF runs must be a non-empty array"], None
    errors: list[str] = []
    exit_codes: list[int] = []
    unknown_failure_exit = False
    for run_index, run in enumerate(runs):
        if not isinstance(run, dict):
            errors.append(f"SARIF run {run_index} must be an object")
            unknown_failure_exit = True
            continue
        invocations = run.get("invocations")
        if invocations is None:
            continue
        if not isinstance(invocations, list):
            errors.append(f"SARIF run {run_index} invocations must be an array")
            unknown_failure_exit = True
            continue
        for invocation_index, invocation in enumerate(invocations):
            prefix = f"SARIF run {run_index} invocation {invocation_index}"
            if not isinstance(invocation, dict):
                errors.append(f"{prefix} must be an object")
                unknown_failure_exit = True
                continue
            success = invocation.get("executionSuccessful")
            if success is not True:
                state = "false" if success is False else "missing or invalid"
                errors.append(f"{prefix} executionSuccessful is {state}")
            embedded_exit = invocation.get("exitCode")
            if "exitCode" in invocation:
                if isinstance(embedded_exit, bool) or not isinstance(embedded_exit, int):
                    errors.append(f"{prefix} exitCode must be an integer")
                    unknown_failure_exit = True
                else:
                    exit_codes.append(embedded_exit)
                    if embedded_exit != 0:
                        errors.append(f"{prefix} exitCode is {embedded_exit}")
            elif success is not True:
                unknown_failure_exit = True
            notifications = invocation.get("toolExecutionNotifications") or []
            if not isinstance(notifications, list):
                errors.append(
                    f"{prefix} toolExecutionNotifications must be an array")
                unknown_failure_exit = True
                continue
            for notification_index, notification in enumerate(notifications):
                if not isinstance(notification, dict):
                    errors.append(
                        f"{prefix} notification {notification_index} must be an object")
                    continue
                if notification.get("level") == "error":
                    detail = _sarif_message(notification) or "tool execution error"
                    errors.append(
                        f"{prefix} notification {notification_index}: {detail}")
    unique_errors = list(dict.fromkeys(errors))
    nonzero = next((code for code in exit_codes if code != 0), None)
    exit_code = nonzero if nonzero is not None else (
        None if unique_errors and unknown_failure_exit else 0)
    return bool(unique_errors), unique_errors, exit_code


def normalize_sarif(data: dict, *, tool_hint: str | None = None,
                    root: str | Path | None = None) -> tuple[list[dict], dict, list[str]]:
    findings: list[dict] = []
    warnings: list[str] = []
    versions: list[str] = []
    names: list[str] = []
    for run in data.get("runs") or []:
        driver = (run.get("tool") or {}).get("driver") or {}
        names.append(str(driver.get("name") or ""))
        versions.append(str(driver.get("semanticVersion") or driver.get("version") or ""))
        rules = _driver_rules(run)
        for result in run.get("results") or []:
            rule_id = str(result.get("ruleId") or result.get("rule", {}).get("id") or "unknown")
            rule = rules.get(rule_id, {})
            message_obj = result.get("message") or {}
            message = message_obj.get("text") or message_obj.get("markdown") or ""
            metadata = [rule, result.get("properties") or {}, rule_id, message]
            cwes = infer_cwes(metadata)
            category = infer_category(cwes, metadata)
            primary = None
            if result.get("locations"):
                primary = _sarif_loc(result["locations"][0], root)
            source, flow_sink = _sarif_flow(result, root)
            sink = primary or flow_sink
            if sink is None:
                warnings.append(f"{rule_id}: resultado SARIF sem local físico")
                continue
            findings.append(make_finding(
                rule_id=rule_id, message=message,
                severity=result.get("level") or "warning", cwes=cwes,
                category=category, source=source, sink=sink,
                confidence=(result.get("properties") or {}).get("precision"),
                evidence="sarif-code-flow" if source else "sarif-location"))
    tool = {"name": tool_hint or next((n for n in names if n), "sarif"),
            "version": next((v for v in versions if v), "unknown"),
            "adapter_version": ADAPTER_VERSION}
    return deduplicate(findings), tool, warnings


def _dict_locations(value: Any, root=None) -> list[dict]:
    out: list[dict] = []
    if isinstance(value, dict):
        path = value.get("path") or value.get("file")
        start = value.get("start") or value.get("location") or {}
        if path and isinstance(start, dict):
            loc = location(path, start.get("line"), start.get("col") or start.get("column"),
                           value.get("content") or value.get("label"), root=root)
            if loc:
                out.append(loc)
        for child in value.values():
            out.extend(_dict_locations(child, root))
    elif isinstance(value, list):
        for child in value:
            out.extend(_dict_locations(child, root))
    return out


def normalize_opengrep(data: dict, *, root: str | Path | None = None,
                       tool_hint: str = "opengrep") -> tuple[list[dict], dict, list[str]]:
    findings: list[dict] = []
    warnings: list[str] = []
    for result in data.get("results") or []:
        extra = result.get("extra") or {}
        metadata = extra.get("metadata") or {}
        rule_id = str(result.get("check_id") or "unknown")
        message = str(extra.get("message") or "")
        cwes = infer_cwes(metadata, rule_id, message)
        category = infer_category(cwes, metadata, rule_id, message)
        start = result.get("start") or {}
        sink = location(result.get("path"), start.get("line"), start.get("col"),
                        extra.get("lines"), root=root)
        if sink is None:
            warnings.append(f"{rule_id}: resultado OpenGrep sem path")
            continue
        trace_locations = _dict_locations(extra.get("dataflow_trace"), root)
        source = trace_locations[0] if trace_locations else None
        findings.append(make_finding(
            rule_id=rule_id, message=message,
            severity=extra.get("severity") or "warning", cwes=cwes,
            category=category, source=source, sink=sink,
            confidence=str(metadata.get("confidence")) if metadata.get("confidence") else None,
            evidence="opengrep-dataflow" if source else "opengrep-pattern"))
    version = ((data.get("version") or data.get("engine_requested") or
                (data.get("tool") or {}).get("version") or "unknown"))
    tool = {"name": tool_hint, "version": str(version),
            "adapter_version": ADAPTER_VERSION}
    errors = data.get("errors") or []
    warnings.extend(f"OpenGrep error: {e}" for e in errors)
    return deduplicate(findings), tool, warnings


def deduplicate(findings: Iterable[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for finding in findings:
        unique.setdefault(finding["fingerprint"], finding)
    return sorted(unique.values(), key=lambda f: (
        f["sink"]["path"], f["sink"].get("line") or 0,
        f["category"], f["rule_id"]))


def compare_reports(left: dict, right: dict) -> dict:
    """Compara sem fingir equivalência de linhas entre engines.

    `exact_sink` exige path/line/categoria. `file_category` é a régua usada em
    corpus como OWASP, onde ferramentas podem marcar a concatenação ou a call
    um linha depois para a mesma vulnerabilidade.
    """
    def keys(report: dict, exact: bool) -> set[tuple]:
        out = set()
        for finding in report.get("findings", []):
            sink = finding["sink"]
            key: tuple[object, ...] = (sink["path"], finding["category"])
            if exact:
                key += (sink.get("line"),)
            out.add(key)
        return out

    def overlap(a: set, b: set) -> dict:
        common = a & b
        return {"left": len(a), "right": len(b), "common": len(common),
                "left_only": len(a - b), "right_only": len(b - a),
                "jaccard": round(len(common) / len(a | b), 3) if a | b else 1.0}

    if left.get("target", {}).get("git_commit") != right.get("target", {}).get("git_commit"):
        raise ValueError("relatórios de commits diferentes não são comparáveis")
    return {
        "target_commit": left.get("target", {}).get("git_commit"),
        "left_tool": left.get("tool", {}).get("name"),
        "right_tool": right.get("tool", {}).get("name"),
        "exact_sink": overlap(keys(left, True), keys(right, True)),
        "file_category": overlap(keys(left, False), keys(right, False)),
    }


def _summary(findings: list[dict]) -> dict:
    return {
        "findings": len(findings),
        "by_category": dict(sorted(Counter(f["category"] for f in findings).items())),
        "by_cwe": dict(sorted(Counter(cwe for f in findings for cwe in f["cwes"]).items(),
                              key=lambda item: int(item[0]))),
        "with_source": sum(1 for f in findings if f.get("source")),
    }


def build_report(*, tool: dict, root: str | Path, findings: list[dict],
                 status: str = "complete", duration_s: float = 0.0,
                 peak_rss_mb: float | None = None, exit_code: int | None = 0,
                 memory_scope: str | None = None,
                 command: list[str] | None = None, warnings: list[str] | None = None,
                 errors: list[str] | None = None, extra: dict | None = None,
                 truncated: bool = False,
                 analysis: dict | None = None) -> dict:
    if status == "complete" and truncated is not False:
        raise ValueError("status complete exige truncated=false")
    if status == "complete" and errors:
        raise ValueError("status complete exige errors=[]")
    target_git = git_metadata(root)
    normalized_findings = deduplicate(findings)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "truncated": bool(truncated),
        "tool": tool,
        "target": {"name": Path(root).resolve().name,
                   "git_commit": target_git["commit"],
                   "git_dirty": target_git["dirty"],
                   "git_remote": target_git["remote"]},
        "invocation": {
            "status": status,
            "duration_s": round(float(duration_s), 3),
            "peak_rss_mb": round(float(peak_rss_mb), 2) if peak_rss_mb is not None else None,
            "memory_scope": memory_scope or (
                "root-process" if peak_rss_mb is not None else "unavailable"),
            "exit_code": exit_code,
            "command": command,
            "platform": sys.platform,
            "python": sys.version.split()[0],
        },
        "findings": normalized_findings,
        "warnings": list(warnings or []),
        "errors": list(errors or []),
        "extra": extra or {},
    }
    if (target_git["remote"] and target_git["commit"]
            and target_git["scan_subdir"] is not None):
        report["subject"] = {
            "repository": target_git["remote"],
            "commit": target_git["commit"],
            "scan_subdir": target_git["scan_subdir"],
        }
    if analysis:
        report["analysis"] = analysis
    report["summary"] = _summary(normalized_findings)
    validate_report(report, root=root)
    return report


def validate_report(report: dict, *, root: str | Path | None = None) -> None:
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version ausente ou incompatível")
    for key in ("status", "truncated", "tool", "target", "invocation",
                "findings", "summary", "warnings", "errors"):
        if key not in report:
            raise ValueError(f"campo obrigatório ausente: {key}")
    if report["invocation"].get("status") not in {
            "complete", "partial", "failed", "unavailable"}:
        raise ValueError("invocation.status inválido")
    if report["status"] != report["invocation"].get("status"):
        raise ValueError("status diverge de invocation.status")
    if report["status"] == "complete" \
            and report["invocation"].get("exit_code") != 0:
        raise ValueError("status complete exige invocation.exit_code zero")
    if not isinstance(report["truncated"], bool):
        raise ValueError("truncated deve ser boolean")
    if (not isinstance(report["errors"], list)
            or not all(isinstance(error, str) for error in report["errors"])):
        raise ValueError("errors deve ser array de strings")
    if report["status"] == "complete" and report["truncated"]:
        raise ValueError("status complete exige truncated=false")
    if report["status"] == "complete" and report["errors"]:
        raise ValueError("status complete exige errors=[]")
    target = report.get("target")
    if not isinstance(target, dict):
        raise ValueError("target deve ser objeto")
    target_commit = target.get("git_commit")
    if (target_commit is not None
            and (not isinstance(target_commit, str)
                 or not _GIT_COMMIT_RE.fullmatch(target_commit))):
        raise ValueError("target.git_commit inválido")
    if not isinstance(target.get("git_dirty"), (bool, type(None))):
        raise ValueError("target.git_dirty deve ser boolean ou null")
    if not isinstance(target.get("git_remote"), (str, type(None))):
        raise ValueError("target.git_remote deve ser string ou null")
    subject = report.get("subject")
    if subject is not None:
        if not isinstance(subject, dict):
            raise ValueError("subject deve ser objeto")
        if not _is_safe_relative_path(subject.get("scan_subdir"), allow_dot=True):
            raise ValueError("subject.scan_subdir deve ser relativo ao repositório")
        if (not isinstance(subject.get("commit"), str)
                or not _GIT_COMMIT_RE.fullmatch(subject["commit"])):
            raise ValueError("subject.commit inválido")
        if target.get("git_commit") != subject.get("commit"):
            raise ValueError("target.git_commit diverge de subject.commit")
        target_remote = str(target.get("git_remote") or "").rstrip("/")
        subject_remote = str(subject.get("repository") or "").rstrip("/")
        if target_remote != subject_remote:
            raise ValueError("target.git_remote diverge de subject.repository")
    analysis = report.get("analysis")
    engine = analysis.get("engine") if isinstance(analysis, dict) else None
    if isinstance(engine, dict):
        provenance_keys = {"git_commit", "git_dirty", "source_tree_sha256"}
        if provenance_keys.intersection(engine):
            commit = engine.get("git_commit")
            dirty = engine.get("git_dirty")
            tree_hash = engine.get("source_tree_sha256")
            if (not isinstance(commit, str)
                    or not _GIT_COMMIT_RE.fullmatch(commit)
                    or not isinstance(dirty, bool)):
                raise ValueError("analysis.engine provenance inválida")
            if (tree_hash is not None
                    and (not isinstance(tree_hash, str)
                         or not _SHA256_RE.fullmatch(tree_hash))):
                raise ValueError("analysis.engine source_tree_sha256 inválido")
            if dirty and tree_hash is None:
                raise ValueError(
                    "analysis.engine dirty exige source_tree_sha256")
    seen: set[str] = set()
    for finding in report["findings"]:
        for key in ("fingerprint", "rule_id", "category", "cwes", "sink"):
            if key not in finding:
                raise ValueError(f"finding sem {key}")
        if finding["fingerprint"] in seen:
            raise ValueError("fingerprint duplicado")
        seen.add(finding["fingerprint"])
        endpoints = [finding.get("source"), finding.get("sink")]
        endpoints.extend(finding.get("steps") or [])
        for endpoint in endpoints:
            if endpoint is None:
                continue
            path = endpoint.get("path") if isinstance(endpoint, dict) else None
            if not _is_safe_relative_path(path):
                raise ValueError("finding contém path não relativo")
            if root is not None and normalize_path(path, root) != path:
                raise ValueError("finding path escapa da raiz ou atravessa symlink")


def current_peak_rss_mb() -> float | None:
    """Peak RSS do processo atual, sem tornar psutil dependência obrigatória."""
    return _process_rss_mb(os.getpid(), peak=True)


def _process_rss_mb(pid: int, *, peak: bool = False) -> float | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                counters = Counters()
                counters.cb = ctypes.sizeof(counters)
                ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb)
                value = counters.PeakWorkingSetSize if peak else counters.WorkingSetSize
                return value / (1024 * 1024) if ok else None
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        wanted = "VmHWM:" if peak else "VmRSS:"
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(wanted):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return None


def _child_pids(parent: int) -> set[int]:
    pairs: list[tuple[int, int]] = []
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessEntry(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                                 ctypes.POINTER(ProcessEntry)]
            kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(ProcessEntry)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot in (0, ctypes.c_void_p(-1).value):
                return set()
            try:
                entry = ProcessEntry()
                entry.dwSize = ctypes.sizeof(entry)
                ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                while ok:
                    pairs.append((int(entry.th32ProcessID),
                                  int(entry.th32ParentProcessID)))
                    ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception:
            return set()
    else:
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat.read_text().split()
                pairs.append((int(fields[0]), int(fields[3])))
            except (OSError, ValueError, IndexError):
                continue
    descendants: set[int] = set()
    frontier = {parent}
    while frontier:
        children = {pid for pid, ppid in pairs if ppid in frontier}
        children -= descendants
        descendants |= children
        frontier = children
    return descendants


def _process_tree_rss_mb(pid: int) -> float | None:
    values = [_process_rss_mb(child) for child in {pid, *_child_pids(pid)}]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _tree_peak_sampler(pid: int) -> tuple[threading.Event, list[float], threading.Thread]:
    """Sample a live process tree while in-process work creates child tools."""
    stop = threading.Event()
    initial = _process_tree_rss_mb(pid)
    peak = [initial or 0.0]

    def sample() -> None:
        while not stop.wait(0.05):
            rss = _process_tree_rss_mb(pid)
            if rss is not None:
                peak[0] = max(peak[0], rss)

    thread = threading.Thread(target=sample, name="security-rss-sampler", daemon=True)
    thread.start()
    return stop, peak, thread


def _terminate_process_tree(pid: int) -> None:
    targets = [*_child_pids(pid), pid]
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            for target in targets:
                handle = kernel32.OpenProcess(0x0001, False, target)
                if handle:
                    kernel32.TerminateProcess(handle, 1)
                    kernel32.CloseHandle(handle)
            return
        except Exception:
            pass
    for target in targets:
        try:
            os.kill(target, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (OSError, ProcessLookupError):
            continue


def execute(command: list[str], *, cwd: str | Path | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None) -> dict:
    """Executa sem shell, mede tempo/RSS do processo raiz e preserva logs."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="gcm-security-exec-") as td:
        stdout_path, stderr_path = Path(td) / "stdout", Path(td) / "stderr"
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                proc = subprocess.Popen(command, cwd=cwd, env=env,
                                        stdout=stdout, stderr=stderr)
                peak = 0.0
                timed_out = False
                while proc.poll() is None:
                    rss = _process_tree_rss_mb(proc.pid)
                    if rss is not None:
                        peak = max(peak, rss)
                    if timeout_s is not None and time.perf_counter() - started > timeout_s:
                        timed_out = True
                        _terminate_process_tree(proc.pid)
                        break
                    time.sleep(0.05)
                proc.wait()
            stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            return {"exit_code": proc.returncode, "timed_out": timed_out,
                    "duration_s": time.perf_counter() - started,
                    "peak_rss_mb": peak or None, "stdout": stdout_text,
                    "stderr": stderr_text}
        except OSError as exc:
            return {"exit_code": None, "timed_out": False,
                    "duration_s": time.perf_counter() - started,
                    "peak_rss_mb": None, "stdout": "", "stderr": str(exc)}


def _refine_report_health(stats: dict | None) -> tuple[bool, list[str], list[str]]:
    """Translate L1 health into the normalized report envelope.

    L1 keeps its aggregate error count separate from the detailed per-resolver
    errors.  The benchmark report must retain both the partial state and those
    details; otherwise a semantically incomplete refinement looks eligible to
    downstream scorers.  Finding truncation remains a separate taint concern.
    """
    if stats is None:
        return False, [], []

    unavailable = stats.get("unavailable") or []
    raw_errors = stats.get("errors") or 0
    partial = bool(
        stats.get("partial")
        or stats.get("status") == "partial"
        or raw_errors
        or unavailable
    )

    top_warnings = [str(value) for value in stats.get("warnings") or ()]
    error_markers: set[str] = set()
    warnings: list[str] = []
    errors: list[str] = []
    runs = stats.get("runs") or ()
    for run in runs:
        resolver = str(run.get("resolver") or "resolver desconhecido")
        root = run.get("root")
        context = f"{resolver} ({root})" if root else resolver
        for value in run.get("errors") or ():
            detail = str(value)
            errors.append(f"L1 {context}: {detail}")
            error_markers.add(f"{context}: ERROR: {detail}")
        for value in run.get("warnings") or ():
            detail = f"{context}: {value}"
            if detail not in top_warnings:
                warnings.append(f"L1: {detail}")

    # O agregador L1 também espelha erros de runs[] em warnings com ``ERROR``.
    # Não os duplique em duas severidades no envelope normalizado.
    warnings.extend(
        f"L1: {value}" for value in top_warnings
        if value not in error_markers
    )

    if isinstance(raw_errors, (list, tuple)):
        errors.extend(f"L1: {value}" for value in raw_errors)
    elif raw_errors and not errors:
        errors.append(
            f"L1: refinamento reportou {int(raw_errors)} erro(s) "
            "sem detalhe estruturado"
        )

    # Defensive compatibility with third-party/older refiners that expose an
    # unavailable resolver without the current friendly warning.
    if unavailable and not warnings:
        for item in unavailable:
            languages = ", ".join(item.get("languages") or ()) or "desconhecida"
            resolver = item.get("resolver") or item.get("server") or "desconhecido"
            warnings.append(
                f"L1: resolver indisponível para {languages} ({resolver})"
            )

    return partial, list(dict.fromkeys(warnings)), list(dict.fromkeys(errors))


def run_graphcodemap(root: str | Path, *, refine: bool = False,
                     max_findings: int = 10 ** 9,
                     db_path: str | Path | None = None) -> dict:
    from codegraph import CodeGraph, __version__
    from codegraph import l1

    root = Path(root).resolve()
    started = time.perf_counter()
    warnings: list[str] = []
    errors: list[str] = []
    stop_rss, peak_rss, sampler = _tree_peak_sampler(os.getpid())
    g = None
    try:
        g = CodeGraph(root, db_path=db_path)
        index_stats = g.index()
        refine_stats = l1.refine(g.indexer) if refine else None
        refine_partial, refine_warnings, refine_errors = (
            _refine_report_health(refine_stats)
        )
        warnings.extend(refine_warnings)
        errors.extend(refine_errors)
        data, env = g.taint(max_findings=max_findings)
        warnings.extend(env.warnings)
        findings: list[dict] = []
        for raw in data.get("findings", []):
            src, sink_raw = raw.get("origin") or {}, raw.get("sink") or {}
            # A categoria pertence à API chamada no site (`evaluate`, `exec`),
            # não ao alvo interno que L1 eventualmente resolveu. Usar
            # `callee_fqn` primeiro fazia o mesmo achado XPath mudar para
            # `unknown` depois do JDTLS, embora código e sink fossem idênticos.
            sink_label = sink_raw.get("callee") or sink_raw.get("callee_fqn") or "unknown"
            cwes = infer_cwes(sink_label, raw)
            category = infer_graphcodemap_category(sink_label, raw)
            if not cwes and category in CATEGORY_CWE:
                cwes = [CATEGORY_CWE[category]]
            sink = location(sink_raw.get("site_path"), sink_raw.get("line"),
                            _one_based_column(sink_raw.get("column")),
                            label=sink_label, symbol=sink_raw.get("func_fqn"),
                            root=root, byte_span=sink_raw.get("byte_span"))
            if sink is None:
                warnings.append("GraphCodeMap finding sem sink path")
                continue
            source = location(
                src.get("path"), src.get("line"),
                _one_based_column(src.get("column")), label=src.get("what"),
                symbol=src.get("func_fqn"), root=root,
                byte_span=src.get("byte_span"),
                argument_literals=src.get("argument_literals"),
                parameter=src.get("parameter"),
            )
            steps = []
            for raw_step in raw.get("steps") or ():
                normalized = location(
                    raw_step.get("site_path"), raw_step.get("line"),
                    _one_based_column(raw_step.get("column")),
                    label=raw_step.get("callee"),
                    symbol=raw_step.get("func_fqn"), root=root,
                    byte_span=raw_step.get("byte_span"),
                )
                if normalized is None:
                    continue
                normalized.update({
                    "callee": raw_step.get("callee"),
                    "callee_fqn": raw_step.get("callee_fqn"),
                    "arg_index": raw_step.get("arg_index"),
                    "confidence": raw_step.get("confidence"),
                    "resolved": bool(raw_step.get("resolved")),
                })
                steps.append(normalized)
            findings.append(make_finding(
                rule_id=f"graphcodemap/{category}",
                message=f"{src.get('what', 'input')} reaches {sink_label}",
                severity="warning", cwes=cwes, category=category,
                source=source, sink=sink, confidence=raw.get("confidence"),
                evidence=raw.get("flow_evidence"), steps=steps))
        status = "partial" if env.truncated or refine_partial else "complete"
        tool = {"name": "graphcodemap", "version": __version__,
                "adapter_version": ADAPTER_VERSION}
        config_path = root / ".codegraph" / "taint.json"
        config_hash = (hashlib.sha256(config_path.read_bytes()).hexdigest()
                       if config_path.is_file() else None)
        report = build_report(
            tool=tool,
            root=root, findings=findings, status=status,
            duration_s=time.perf_counter() - started,
            peak_rss_mb=None, exit_code=0, memory_scope="process-tree",
            command=["graphcodemap", "taint", str(root)], warnings=warnings,
            errors=errors, extra={"index": index_stats, "refine": refine_stats,
                                  "limit_hit": data.get("limit_hit"),
                                  "db_path": str(Path(db_path).resolve())
                                  if db_path else None},
            truncated=env.truncated,
            analysis={
                "engine": _engine_provenance(tool),
                "config": {
                    "refine": bool(refine),
                    "max_findings": max_findings,
                    "taint_config_sha256": config_hash,
                },
            })
    finally:
        if g is not None:
            g.close()
        stop_rss.set()
        sampler.join(timeout=1.0)
    report["invocation"]["peak_rss_mb"] = round(peak_rss[0], 2) if peak_rss[0] else None
    return report
