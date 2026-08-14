"""Benchmark SAST normalizado e reproduzível.

Modos:
  graphcodemap  executa o motor local e normaliza os achados;
  sarif         importa CodeQL, OpenTaint ou qualquer SARIF 2.1;
  opengrep      importa o JSON de `opengrep scan --json`;
  doctor        mostra quais executores estão disponíveis.

Todo relatório carimba commit/dirty do alvo. Um import sem commit identificável
é permitido para inspeção, mas não deve entrar em placar competitivo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph.eval.security_bench import (  # noqa: E402
    ADAPTER_VERSION,
    build_report,
    compare_reports,
    execute,
    normalize_opengrep,
    normalize_sarif,
    run_graphcodemap,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    suffix = ".exe" if sys.platform == "win32" else ""
    local = _PROJECT_ROOT / "tools" / "securitybench" / f"{name}{suffix}"
    return str(local) if local.is_file() else None


def _resolve_rule_ids(ruleset: str, requested: list[str]) -> list[str]:
    root = Path(ruleset).resolve()
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*") if p.suffix.lower() in {".yml", ".yaml"})
    by_bare: dict[str, list[str]] = {}
    full: set[str] = set()
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = file.name if root.is_file() else file.relative_to(root).as_posix()
        for match in re.finditer(r"(?m)^\s*-\s+id:\s*['\"]?([^\s'\"#]+)", text):
            value = f"{rel}:{match.group(1)}"
            full.add(value)
            by_bare.setdefault(match.group(1), []).append(value)
    resolved: list[str] = []
    for value in requested:
        normalized = value.replace("\\", "/").replace("#", ":")
        if normalized in full:
            resolved.append(normalized)
        elif normalized in by_bare and len(by_bare[normalized]) == 1:
            resolved.append(by_bare[normalized][0])
        elif normalized in by_bare:
            raise ValueError(f"rule-id ambíguo {value!r}: {by_bare[normalized]}")
        else:
            raise ValueError(f"rule-id não encontrado no ruleset: {value}")
    return resolved


def _version(command: list[str]) -> str | None:
    try:
        p = subprocess.run(command, text=True, capture_output=True, timeout=15,
                           check=False)
        text = (p.stdout or p.stderr).strip().splitlines()
        if not text:
            return None
        line = text[0][:200]
        match = re.search(r"(?i)\bversion\s+([^\s,;]+)", line)
        return match.group(1) if match else line
    except (OSError, subprocess.TimeoutExpired):
        return None


def doctor() -> dict:
    rows = {}
    for name, args in {
        "codeql": ["codeql", "version"],
        "opengrep": ["opengrep", "--version"],
        "opentaint": ["opentaint", "--version"],
    }.items():
        path = _binary(name)
        rows[name] = {"available": bool(path), "path": path,
                      "version": _version([path, *args[1:]]) if path else None}
    rows["graphcodemap"] = {"available": True, "path": sys.executable,
                            "version": _version(
                                [sys.executable, "-c",
                                 "import codegraph; print(codegraph.__version__)"])}
    return rows


def import_report(kind: str, input_path: Path, root: Path,
                  tool_name: str | None = None) -> dict:
    started = __import__("time").perf_counter()
    raw = json.loads(input_path.read_text(encoding="utf-8-sig"))
    if kind == "sarif":
        findings, tool, warnings = normalize_sarif(
            raw, tool_hint=tool_name, root=root)
    else:
        findings, tool, warnings = normalize_opengrep(
            raw, tool_hint=tool_name or "opengrep", root=root)
    partial = bool(raw.get("errors")) if isinstance(raw, dict) else False
    return build_report(
        tool=tool, root=root, findings=findings,
        status="partial" if partial else "complete",
        duration_s=__import__("time").perf_counter() - started,
        peak_rss_mb=None, command=[kind, str(input_path)], warnings=warnings,
        extra={"input": str(input_path.resolve()),
               "adapter_version": ADAPTER_VERSION})


def run_external(kind: str, root: Path, *, config: str | None = None,
                 database: str | None = None, queries: list[str] | None = None,
                 rule_ids: list[str] | None = None,
                 timeout_s: float | None = None) -> dict:
    executable_name = {"run-opengrep": "opengrep", "run-opentaint": "opentaint",
                       "run-codeql": "codeql"}[kind]
    executable = _binary(executable_name)
    version_args = {"codeql": ["version"], "opengrep": ["--version"],
                    "opentaint": ["--version"]}[executable_name]
    version = _version([executable, *version_args]) if executable else None
    tool = {"name": executable_name, "version": version or "unknown",
            "adapter_version": ADAPTER_VERSION}
    if executable is None:
        return build_report(
            tool=tool, root=root, findings=[], status="unavailable",
            exit_code=None, command=None,
            errors=[f"executável não encontrado no PATH: {executable_name}"])

    with tempfile.TemporaryDirectory(prefix=f"gcm-{executable_name}-") as td:
        sarif_path = Path(td) / "results.sarif"
        tool_log = Path(td) / "tool.log"
        if kind == "run-opengrep":
            if not config:
                raise ValueError("run-opengrep exige --config")
            command = [executable, "scan", "--json", "--disable-version-check",
                       "--config", config, str(root)]
        elif kind == "run-opentaint":
            command = [executable, "scan", "--quiet", "--color", "never",
                       "--log-file", str(tool_log),
                       "--output", str(sarif_path)]
            if config:
                command += ["--ruleset", config]
            resolved_rules = (_resolve_rule_ids(config, rule_ids)
                              if config and rule_ids else (rule_ids or []))
            for rule_id in resolved_rules:
                command += ["--rule-id", rule_id]
            command.append(str(root))
        else:
            if not database or not queries:
                raise ValueError("run-codeql exige --database e ao menos um --queries")
            command = [executable, "database", "analyze", database, *queries,
                       "--format=sarif-latest", f"--output={sarif_path}"]

        env = os.environ.copy()
        local_tools = _PROJECT_ROOT / "tools" / "securitybench"
        if local_tools.is_dir():
            # Inclui o mvn.exe isolado que delega ao Maven pinado. O
            # autobuilder Java do OpenTaint usa CreateProcess("mvn") e não
            # consegue executar o mvn.cmd oficial do Maven no Windows.
            env["PATH"] = str(local_tools) + os.pathsep + env.get("PATH", "")
        result = execute(command, timeout_s=timeout_s, env=env)
        warnings: list[str] = []
        errors: list[str] = []
        findings: list[dict] = []
        parsed_tool = tool
        parser_partial = False
        try:
            if kind == "run-opengrep":
                native = json.loads(result["stdout"])
                parser_partial = bool(native.get("errors"))
                findings, parsed_tool, warnings = normalize_opengrep(
                    native, root=root, tool_hint="opengrep")
                parsed_tool["version"] = version or parsed_tool["version"]
            elif sarif_path.is_file():
                native = json.loads(sarif_path.read_text(encoding="utf-8-sig"))
                findings, parsed_tool, warnings = normalize_sarif(
                    native, root=root, tool_hint=executable_name)
                parsed_tool["version"] = version or parsed_tool["version"]
            else:
                raise ValueError("a ferramenta não produziu o arquivo de resultado")
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            errors.append(f"saída inválida: {exc}")
        if result["stderr"].strip():
            warnings.append(result["stderr"].strip()[-4000:])
            if "Traceback (most recent call last)" in result["stderr"]:
                errors.append("a ferramenta emitiu traceback no stderr")
        if executable_name == "opentaint" and rule_ids and not findings:
            # Zero achados pode ser legítimo, mas um filtro que não casa com
            # nenhuma regra gera o mesmo zero. O CLI não expõe a contagem no
            # SARIF; carimbamos o risco em vez de tratá-lo como evidência TN.
            warnings.append(
                "zero findings com --rule-id: confirme no rule-load-trace que "
                "ao menos uma regra foi carregada")
        if tool_log.is_file() and (result["exit_code"] not in (0,) or errors):
            log_tail = tool_log.read_text(encoding="utf-8", errors="replace")[-8000:]
            if log_tail.strip():
                warnings.append("tool log tail:\n" + log_tail.strip())
        if result["timed_out"]:
            errors.append(f"timeout após {timeout_s}s")
        elif result["exit_code"] not in (0,):
            errors.append(f"exit code {result['exit_code']}")
        status = ("failed" if not findings and errors else
                  "partial" if errors or parser_partial else "complete")
        return build_report(
            tool=parsed_tool, root=root, findings=findings, status=status,
            duration_s=result["duration_s"], peak_rss_mb=result["peak_rss_mb"],
            memory_scope="process-tree" if result["peak_rss_mb"] is not None else None,
            exit_code=result["exit_code"], command=command,
            warnings=warnings, errors=errors,
            extra={"timeout_s": timeout_s,
                   "memory_note": "peak sum of the sampled process tree"})


def render(report: dict) -> str:
    inv, target, summary = report["invocation"], report["target"], report["summary"]
    commit = (target.get("git_commit") or "unknown")[:12]
    dirty = " dirty" if target.get("git_dirty") else ""
    lines = [
        f"{report['tool']['name']} {report['tool'].get('version', 'unknown')} "
        f"· {target['name']}@{commit}{dirty}",
        f"status={inv['status']} findings={summary['findings']} "
        f"source-traces={summary['with_source']} duration={inv['duration_s']}s "
        f"peak-rss={inv['peak_rss_mb'] if inv['peak_rss_mb'] is not None else '?'} MB",
    ]
    if summary["by_category"]:
        lines.append("categorias: " + ", ".join(
            f"{key}={value}" for key, value in summary["by_category"].items()))
    if report["warnings"]:
        lines.append(f"warnings={len(report['warnings'])}")
    if report["errors"]:
        lines.append(f"errors={len(report['errors'])}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("doctor")
    compare = sub.add_parser("compare")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--out", help="JSON da comparação (além do stdout)")

    own = sub.add_parser("graphcodemap")
    own.add_argument("root")
    own.add_argument("--refine", action="store_true")
    own.add_argument("--max-findings", type=int, default=10 ** 9)
    own.add_argument("--db-path", help="banco isolado para A/B sem reutilizar o índice do alvo")
    own.add_argument("--out", required=True)

    for name in ("sarif", "opengrep"):
        parser = sub.add_parser(name)
        parser.add_argument("input")
        parser.add_argument("--root", required=True)
        parser.add_argument("--tool-name")
        parser.add_argument("--out", required=True)
    for name in ("run-opengrep", "run-opentaint", "run-codeql"):
        parser = sub.add_parser(name)
        parser.add_argument("--root", required=True)
        parser.add_argument("--out", required=True)
        parser.add_argument("--timeout-s", type=float)
        if name == "run-opengrep":
            parser.add_argument("--config", required=True)
        if name == "run-opentaint":
            parser.add_argument("--config", help="ruleset local fixado; omitir usa builtin")
            parser.add_argument("--rule-id", action="append",
                                help="filtro repetível de regra")
        if name == "run-codeql":
            parser.add_argument("--database", required=True)
            parser.add_argument("--queries", action="append", required=True)
    args = ap.parse_args()

    if args.mode == "doctor":
        data = doctor()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if all(row["available"] for row in data.values()) else 2
    if args.mode == "compare":
        left = json.loads(Path(args.left).read_text(encoding="utf-8"))
        right = json.loads(Path(args.right).read_text(encoding="utf-8"))
        result = compare_reports(left, right)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            output = Path(args.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        print(rendered)
        return 0
    if args.mode == "graphcodemap":
        report = run_graphcodemap(args.root, refine=args.refine,
                                  max_findings=args.max_findings,
                                  db_path=args.db_path)
    elif args.mode.startswith("run-"):
        report = run_external(
            args.mode, Path(args.root), config=getattr(args, "config", None),
            database=getattr(args, "database", None),
            queries=getattr(args, "queries", None),
            rule_ids=getattr(args, "rule_id", None), timeout_s=args.timeout_s)
    else:
        report = import_report(args.mode, Path(args.input), Path(args.root),
                               args.tool_name)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report))
    print(f"output: {out}")
    return 0 if report["invocation"]["status"] in {"complete", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
