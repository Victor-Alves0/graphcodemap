"""Precisão/recall de DETECÇÃO DE VULNERABILIDADE no OWASP Benchmark.

Por que este arquivo existe: todos os nossos benchmarks até aqui mediam
*localização* e *alcançabilidade para agente*. Nenhum media detecção de
vulnerabilidade — e é a primeira coisa que qualquer pessoa de segurança pede.
Sem um número contra gabarito, "nosso taint é bom" é opinião.

O OWASP Benchmark (v1.2, Apache-2.0) tem 2.740 casos Java com gabarito
declarado: cada arquivo é uma vulnerabilidade REAL ou um look-alike seguro.
Os look-alikes são o ponto — é assim que se mede falso positivo de verdade.

METODOLOGIA (as ressalvas fazem parte do resultado):

1. Só as 7 categorias baseadas em TAINT são pontuadas: sqli, cmdi, pathtraver,
   xss, ldapi, xpathi, trustbound (1.698 casos). As outras 4 (weakrand, crypto,
   hash, securecookie) são mau uso de API/configuração — nosso motor não faz
   esse tipo de checagem e seria desonesto pontuar nelas, para bem ou para mal.

2. Detecção em nível de ARQUIVO E CATEGORIA: cada caso contém uma
   vulnerabilidade pretendida. Um alerta só conta como hit quando sua categoria
   normalizada coincide com o gabarito. Alertas de outra categoria são
   registrados em `wrong_category`, nunca promovidos a verdadeiros positivos.

3. Um só índice para todos os arquivos (é assim que a ferramenta roda de
   verdade), com o orçamento de tempo/passos desligado para não confundir
   "não achou" com "não teve tempo".

Uso:
    python evals/owaspbench.py <BenchmarkJava> [--limit N] [--json saida.json]
    python evals/owaspbench.py <BenchmarkJava> --report normalized.json
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegraph import CodeGraph  # noqa: E402
from codegraph.eval.security_bench import infer_graphcodemap_category  # noqa: E402

# categorias do Benchmark que são realmente fluxo fonte→sink
TAINT_CATEGORIES = {
    "sqli", "cmdi", "pathtraver", "xss", "ldapi", "xpathi", "trustbound",
}

NORMALIZED_TO_OWASP = {
    "sql-injection": "sqli",
    "command-injection": "cmdi",
    "path-traversal": "pathtraver",
    "xss": "xss",
    "ldap-injection": "ldapi",
    "xpath-injection": "xpathi",
    "trust-boundary": "trustbound",
}


def score_cases(cases: list[str], gt: dict[str, tuple[str, bool]],
                flagged: dict[str, set[str]]) -> tuple[dict, dict, int]:
    per_cat: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "tn": 0, "fn": 0})
    wrong_category = 0
    for name in cases:
        expected, real = gt[name]
        predicted = flagged.get(name, set())
        hit = expected in predicted
        wrong_category += sum(1 for category in predicted if category != expected)
        key = ("tp" if real and hit else "fn" if real else
               "fp" if hit else "tn")
        per_cat[expected][key] += 1
    total = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for counts in per_cat.values():
        for key in total:
            total[key] += counts[key]
    return per_cat, total, wrong_category


def score_normalized_report(bench: Path, report_data: dict,
                            limit: int | None = None) -> dict:
    """Pontua qualquer ferramenta que cumpra security-report.schema.json."""
    gt = load_ground_truth(bench)
    cases = sorted(n for n, (cat, _) in gt.items() if cat in TAINT_CATEGORIES)
    if limit:
        cases = cases[:limit]
    allowed = set(cases)
    flagged: dict[str, set[str]] = defaultdict(set)
    for finding in report_data.get("findings", []):
        sink = finding.get("sink") or {}
        stem = Path(sink.get("path") or "").stem
        category = NORMALIZED_TO_OWASP.get(finding.get("category"))
        if stem in allowed and category:
            flagged[stem].add(category)
    per_cat, total, wrong_category = score_cases(cases, gt, flagged)
    invocation = report_data.get("invocation") or {}
    tool = report_data.get("tool") or {}
    return {
        "cases": len(cases), "staged": len(cases),
        "index_s": 0.0, "taint_s": invocation.get("duration_s", 0.0),
        "per_category": {k: dict(v) for k, v in sorted(per_cat.items())},
        "total": total, "wrong_category": wrong_category,
        "flow_evidence": {},
        "tool": tool.get("name", "unknown"),
        "tool_version": tool.get("version", "unknown"),
        "source_status": invocation.get("status", "unknown"),
        "eligible": invocation.get("status") == "complete",
        "target_commit": (report_data.get("target") or {}).get("git_commit"),
    }


def load_ground_truth(bench: Path) -> dict[str, tuple[str, bool]]:
    """{BenchmarkTestNNNNN: (categoria, é_vulnerabilidade_real)}"""
    csv_path = bench / "expectedresults-1.2.csv"
    if not csv_path.is_file():
        sys.exit(f"gabarito não encontrado: {csv_path}")
    gt: dict[str, tuple[str, bool]] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#"):
                continue
            gt[row[0].strip()] = (row[1].strip(),
                                  row[2].strip().lower() == "true")
    return gt


def stage(bench: Path, names: list[str], dest: Path) -> int:
    """Copia os casos escolhidos para um diretório limpo (sem os helpers do
    Benchmark, que não são casos de teste e poluiriam a contagem)."""
    src = bench / "src" / "main" / "java" / "org" / "owasp" / "benchmark" / "testcode"
    n = 0
    for name in names:
        f = src / f"{name}.java"
        if f.is_file():
            shutil.copy(f, dest / f.name)
            n += 1
    return n


def run(bench: Path, limit: int | None) -> dict:
    gt = load_ground_truth(bench)
    cases = sorted(n for n, (cat, _) in gt.items() if cat in TAINT_CATEGORIES)
    if limit:
        cases = cases[:limit]

    tmp = Path(tempfile.mkdtemp(prefix="owaspbench-"))
    staged = stage(bench, cases, tmp)
    print(f"casos preparados: {staged} (de {len(cases)} nas categorias de taint)")

    t0 = time.monotonic()
    g = CodeGraph(tmp)
    g.index()
    t_index = time.monotonic() - t0

    t1 = time.monotonic()
    # sem teto: "não achou" tem que significar não achou, não "acabou o tempo"
    data, _env = g.taint(max_findings=10 ** 9)
    t_taint = time.monotonic() - t1

    flagged: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, str] = {}
    for fi in data["findings"]:
        stem = Path(fi["sink"]["site_path"]).stem
        normalized = infer_graphcodemap_category(fi["sink"]["callee"], fi)
        category = NORMALIZED_TO_OWASP.get(normalized)
        if category:
            flagged[stem].add(category)
        evidence.setdefault(stem, fi.get("flow_evidence", "?"))
    g.close()
    shutil.rmtree(tmp, ignore_errors=True)

    per_cat, total, wrong_category = score_cases(cases, gt, flagged)

    return {
        "cases": len(cases), "staged": staged,
        "index_s": round(t_index, 1), "taint_s": round(t_taint, 1),
        "per_category": {k: dict(v) for k, v in sorted(per_cat.items())},
        "total": total, "wrong_category": wrong_category,
        "flow_evidence": {k: v for k, v in list(evidence.items())[:5]},
    }


def _metrics(c: dict) -> tuple[float, float, float, float]:
    tp, fp, fn, tn = c["tp"], c["fp"], c["fn"], c["tn"]
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    return prec, rec, f1, rec - fpr        # OWASP score = TPR - FPR


def report(res: dict) -> str:
    lines = [
        "OWASP Benchmark v1.2 - detecção de vulnerabilidade (categorias de taint)",
        f"  {res['staged']} casos · index {res['index_s']}s · taint {res['taint_s']}s",
        "",
        f"  {'categoria':<12} {'TP':>4} {'FP':>4} {'FN':>5} {'TN':>5} "
        f"{'prec':>6} {'recall':>7} {'F1':>6} {'score':>7}",
        f"  {'-'*12} {'-'*4} {'-'*4} {'-'*5} {'-'*5} {'-'*6} {'-'*7} {'-'*6} {'-'*7}",
    ]
    if res.get("tool"):
        lines.insert(1, f"  tool={res['tool']} {res.get('tool_version', '')} "
                        f"status={res.get('source_status')} "
                        f"commit={(res.get('target_commit') or 'unknown')[:12]}")
    for cat, c in res["per_category"].items():
        p, r, f1, s = _metrics(c)
        lines.append(f"  {cat:<12} {c['tp']:>4} {c['fp']:>4} {c['fn']:>5} "
                     f"{c['tn']:>5} {p:>6.0%} {r:>7.0%} {f1:>6.2f} {s:>+7.2f}")
    p, r, f1, s = _metrics(res["total"])
    t = res["total"]
    lines += [
        f"  {'-'*12} {'-'*4} {'-'*4} {'-'*5} {'-'*5} {'-'*6} {'-'*7} {'-'*6} {'-'*7}",
        f"  {'TOTAL':<12} {t['tp']:>4} {t['fp']:>4} {t['fn']:>5} {t['tn']:>5} "
        f"{p:>6.0%} {r:>7.0%} {f1:>6.2f} {s:>+7.2f}",
        "",
        "  score = TPR - FPR (métrica oficial do OWASP; 0 = aleatório, 1 = perfeito)",
        f"  alertas descartados por categoria incorreta: {res.get('wrong_category', 0)}",
        "  hit = mesmo arquivo E mesma categoria normalizada do gabarito.",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bench", help="raiz do repo BenchmarkJava")
    ap.add_argument("--limit", type=int, default=None,
                    help="usa só os N primeiros casos (rodada rápida)")
    ap.add_argument("--json", default=None, help="grava o resultado bruto")
    ap.add_argument("--report", default=None,
                    help="pontua um security-report normalizado em vez de executar")
    args = ap.parse_args()

    if args.report:
        raw = json.loads(Path(args.report).read_text(encoding="utf-8"))
        res = score_normalized_report(Path(args.bench), raw, args.limit)
    else:
        res = run(Path(args.bench), args.limit)
    print()
    print(report(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"\noutput: {args.json}")
    return 0 if res.get("eligible", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
