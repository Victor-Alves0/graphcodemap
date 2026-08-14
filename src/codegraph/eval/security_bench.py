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
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

SCHEMA_VERSION = 1
ADAPTER_VERSION = "1.0"

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


def normalize_path(value: str | None, root: str | Path | None = None) -> str | None:
    if not value:
        return None
    raw = unquote(str(value)).strip()
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        raw = (parsed.netloc + parsed.path).lstrip("/") if parsed.netloc else parsed.path
        if re.match(r"^/[A-Za-z]:/", raw):
            raw = raw[1:]
    raw = raw.replace("\\", "/")
    if root:
        base = str(Path(root).resolve()).replace("\\", "/").rstrip("/")
        if raw.lower().startswith(base.lower() + "/"):
            raw = raw[len(base) + 1:]
    while raw.startswith("./"):
        raw = raw[2:]
    return raw or None


def _line(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def location(path: str | None, line: Any = None, column: Any = None,
             label: str | None = None, symbol: str | None = None,
             root: str | Path | None = None) -> dict | None:
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
                 evidence: str | None = None) -> dict:
    cwe_list = sorted({str(int(c)) for c in cwes if str(c).isdigit()}, key=int)
    finding = {
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
    status = git("status", "--porcelain", "--untracked-files=no")
    remote = git("config", "--get", "remote.origin.url")
    return {"commit": commit, "dirty": bool(status) if status is not None else None,
            "remote": remote}


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
                 errors: list[str] | None = None, extra: dict | None = None) -> dict:
    target_git = git_metadata(root)
    normalized_findings = deduplicate(findings)
    report = {
        "schema_version": SCHEMA_VERSION,
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
    report["summary"] = _summary(normalized_findings)
    validate_report(report)
    return report


def validate_report(report: dict) -> None:
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version ausente ou incompatível")
    for key in ("tool", "target", "invocation", "findings", "summary", "warnings", "errors"):
        if key not in report:
            raise ValueError(f"campo obrigatório ausente: {key}")
    if report["invocation"].get("status") not in {
            "complete", "partial", "failed", "unavailable"}:
        raise ValueError("invocation.status inválido")
    seen: set[str] = set()
    for finding in report["findings"]:
        for key in ("fingerprint", "rule_id", "category", "cwes", "sink"):
            if key not in finding:
                raise ValueError(f"finding sem {key}")
        if finding["fingerprint"] in seen:
            raise ValueError("fingerprint duplicado")
        seen.add(finding["fingerprint"])
        if not finding["sink"].get("path"):
            raise ValueError("finding sem sink.path")


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
                            label=sink_label, symbol=sink_raw.get("func_fqn"), root=root)
            if sink is None:
                warnings.append("GraphCodeMap finding sem sink path")
                continue
            source = location(src.get("path"), src.get("line"), label=src.get("what"),
                              symbol=src.get("func_fqn"), root=root)
            findings.append(make_finding(
                rule_id=f"graphcodemap/{category}",
                message=f"{src.get('what', 'input')} reaches {sink_label}",
                severity="warning", cwes=cwes, category=category,
                source=source, sink=sink, confidence=raw.get("confidence"),
                evidence=raw.get("flow_evidence")))
        status = "partial" if env.truncated else "complete"
        report = build_report(
            tool={"name": "graphcodemap", "version": __version__,
                  "adapter_version": ADAPTER_VERSION},
            root=root, findings=findings, status=status,
            duration_s=time.perf_counter() - started,
            peak_rss_mb=None, exit_code=0, memory_scope="process-tree",
            command=["graphcodemap", "taint", str(root)], warnings=warnings,
            errors=errors, extra={"index": index_stats, "refine": refine_stats,
                                  "limit_hit": data.get("limit_hit"),
                                  "db_path": str(Path(db_path).resolve())
                                  if db_path else None})
    finally:
        if g is not None:
            g.close()
        stop_rss.set()
        sampler.join(timeout=1.0)
    report["invocation"]["peak_rss_mb"] = round(peak_rss[0], 2) if peak_rss[0] else None
    return report
