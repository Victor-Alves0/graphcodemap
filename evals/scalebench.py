"""Prova de escala: indexar 100k+ arquivos e medir onde o motor dobra ou quebra.

Gera um repo sintético com DENSIDADE DE GRAFO real (imports + chamadas
cross-file), não arquivos isolados — assim os caminhos O(N) que importam são de
fato exercitados: `index_repo` (escrita em lote), `resolve_edges` (dict `by_name`
em memória = o suspeito nº1 de estouro de memória), a varredura de frescor
(`scan_source_stats`, o "landmine" do caminho de query) e o re-index incremental.

Mede, por N crescente:
  - tempo de indexação e se completa sem erro/OOM
  - PICO de memória do processo (working set)
  - tamanho do .db em disco
  - varredura de frescor (scan_source_stats) e custo de um "miss" de query
  - re-index incremental (editar 1 arquivo)
  - latência de find_symbol / callers / impact

Uso:  python -m evals.scalebench [N ...]      (default: 5000 20000 100000)
Honesto por construção: imprime números medidos, não promessas.
"""

from __future__ import annotations

import ctypes
import gc
import json
import os
import shutil
import sys
import time
from pathlib import Path

# permite rodar como script solto (sem instalar o pacote)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codegraph import CodeGraph                      # noqa: E402
from codegraph.indexer import scan_source_stats      # noqa: E402


# -- memória ------------------------------------------------------------------

def peak_rss_mb() -> float:
    """Pico de working set do processo (MB). Windows via psapi; POSIX via rusage."""
    if os.name == "nt":
        class _PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_uint32),
                        ("PageFaultCount", ctypes.c_uint32),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        k = ctypes.windll.kernel32
        # HANDLE é 64-bit: sem restype o ctypes trunca o pseudo-handle (-1) e a
        # chamada falha silenciosamente.
        k.GetCurrentProcess.restype = ctypes.c_void_p
        h = ctypes.c_void_p(k.GetCurrentProcess())
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return pmc.PeakWorkingSetSize / 1e6
        return 0.0
    import resource
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / 1e6 if sys.platform == "darwin" else ru / 1e3  # bytes vs KB


# -- geração do repo sintético ------------------------------------------------

def gen_repo(root: Path, n: int, per_dir: int = 500) -> float:
    """Cria N módulos Python com chamadas cross-file resolvíveis por nome.

    Cada módulo chama uma função (nome globalmente único) do módulo anterior do
    mesmo grupo → aresta de chamada dangling que o resolve_edges precisa casar.
    ~2N símbolos e ~N arestas cross-file: densidade de grafo realista.
    """
    t0 = time.perf_counter()
    if root.exists():
        shutil.rmtree(root)
    for i in range(n):
        g = i // per_dir
        d = root / f"pkg/g{g:04d}"
        d.mkdir(parents=True, exist_ok=True)
        prev = i - 1 if (i % per_dir) else i        # 1º do grupo chama a si
        body = [
            f"from pkg.g{g:04d}.m{prev:06d} import fn_{prev:06d}_0\n",
            f"def fn_{i:06d}_0(x):\n    return fn_{prev:06d}_0(x) + 1\n",
            f"def fn_{i:06d}_1(x):\n    return fn_{i:06d}_0(x) * 2\n",
        ]
        (d / f"m{i:06d}.py").write_text("".join(body), encoding="utf-8")
    return time.perf_counter() - t0


# -- benchmark ----------------------------------------------------------------

def run(n: int, workdir: Path) -> dict:
    root = workdir / f"repo_{n}"
    print(f"\n=== N={n} ===", flush=True)
    gen_s = gen_repo(root, n)
    nfiles = sum(1 for _ in root.rglob("*.py"))
    print(f"gerado: {nfiles} arquivos em {gen_s:.1f}s", flush=True)

    gc.collect()
    cg = CodeGraph(root)

    t0 = time.perf_counter()
    stats = cg.index()
    index_s = time.perf_counter() - t0
    print(f"index: {index_s:.1f}s  stats={ {k: stats.get(k) for k in ('indexed','symbols','edges')} }",
          flush=True)

    conn = cg.indexer.conn
    n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_certain = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='calls' AND confidence='certain'"
    ).fetchone()[0]
    n_calls = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='calls'").fetchone()[0]

    db_path = root / ".codegraph" / "graph.db"
    db_mb = db_path.stat().st_size / 1e6 if db_path.exists() else 0.0

    # varredura de frescor pura (scandir): o custo por "miss" em escala
    t0 = time.perf_counter()
    scanned = scan_source_stats(root)
    sweep_s = time.perf_counter() - t0

    # custo real de um miss de query (dispara read-repair _repair_all)
    t0 = time.perf_counter()
    cg.find_symbol("simbolo_que_nao_existe_xyz")
    miss_s = time.perf_counter() - t0

    # query hits: find_symbol / callers / impact num símbolo do meio
    mid = n // 2
    fqn = f"pkg.g{mid // 500:04d}.m{mid:06d}.fn_{mid:06d}_0"
    t0 = time.perf_counter()
    rows, _ = cg.find_symbol(f"fn_{mid:06d}_0")
    find_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    cg.callers(fqn)
    callers_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    cg.impact(fqn)
    impact_s = time.perf_counter() - t0

    # re-index incremental: editar 1 arquivo e reindexar (boot-scan diff)
    victim = root / f"pkg/g{mid // 500:04d}" / f"m{mid:06d}.py"
    src = victim.read_text(encoding="utf-8")
    victim.write_text(src + "\ndef fn_extra(x):\n    return x\n", encoding="utf-8")
    os.utime(victim, None)
    t0 = time.perf_counter()
    inc = cg.index()
    inc_s = time.perf_counter() - t0

    peak = peak_rss_mb()
    cg.close()

    r = {
        "n": n, "files": nfiles, "gen_s": round(gen_s, 1),
        "index_s": round(index_s, 1),
        "index_files_per_s": round(nfiles / index_s, 1) if index_s else None,
        "symbols": n_sym, "edges": n_edges, "calls": n_calls,
        "certain_calls": n_certain,
        "certain_pct": round(100 * n_certain / n_calls, 1) if n_calls else 0,
        "db_mb": round(db_mb, 1),
        "bytes_per_file": round(db_mb * 1e6 / nfiles) if nfiles else None,
        "sweep_s": round(sweep_s, 3), "sweep_scanned": len(scanned),
        "miss_query_s": round(miss_s, 3),
        "find_s": round(find_s, 3), "callers_s": round(callers_s, 3),
        "impact_s": round(impact_s, 3),
        "incremental_reindex_s": round(inc_s, 2),
        "peak_rss_mb": round(peak, 1),
    }
    print(json.dumps(r, indent=2), flush=True)
    return r


def run_existing(root: Path) -> dict:
    """Mede um repo REAL já presente no disco (sem geração). Não-destrutivo:
    o teste incremental usa um arquivo descartável criado e removido no fim."""
    root = Path(root).resolve()
    nfiles = sum(1 for _ in scan_source_stats(root))
    print(f"\n=== repo real: {root} ({nfiles} arquivos indexáveis) ===", flush=True)
    gc.collect()
    cg = CodeGraph(root)
    t0 = time.perf_counter()
    stats = cg.index()
    index_s = time.perf_counter() - t0
    print(f"index: {index_s:.1f}s", flush=True)

    conn = cg.indexer.conn
    n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_calls = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='calls'").fetchone()[0]
    db_path = root / ".codegraph" / "graph.db"
    db_mb = db_path.stat().st_size / 1e6 if db_path.exists() else 0.0

    t0 = time.perf_counter()
    scan_source_stats(root)
    sweep_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    cg.find_symbol("simbolo_que_nao_existe_xyz")
    miss_s = time.perf_counter() - t0

    inc_s = None
    scratch = root / "._codegraph_scale_probe.py"
    try:
        scratch.write_text("def _probe():\n    return 1\n", encoding="utf-8")
        t0 = time.perf_counter()
        cg.index()
        inc_s = time.perf_counter() - t0
    finally:
        scratch.unlink(missing_ok=True)
        cg.index()  # remove o arquivo-sonda do índice

    peak = peak_rss_mb()
    cg.close()
    r = {"repo": str(root), "files": nfiles, "index_s": round(index_s, 1),
         "index_files_per_s": round(nfiles / index_s, 1) if index_s else None,
         "symbols": n_sym, "edges": n_edges, "calls": n_calls,
         "db_mb": round(db_mb, 1),
         "bytes_per_file": round(db_mb * 1e6 / nfiles) if nfiles else None,
         "sweep_s": round(sweep_s, 3), "miss_query_s": round(miss_s, 3),
         "incremental_reindex_s": round(inc_s, 2) if inc_s else None,
         "peak_rss_mb": round(peak, 1),
         "parse_failed": stats.get("parse_failed")}
    print(json.dumps(r, indent=2), flush=True)
    return r


def main(argv: list[str]) -> None:
    if argv and argv[0] == "--repo":
        r = run_existing(Path(argv[1]))
        out = Path.cwd() / f"evals/scalebench-real-{int(time.time())}.json"
        out.write_text(json.dumps([r], indent=2), encoding="utf-8")
        print(f"\nescrito: {out}")
        return
    ns = [int(x) for x in argv] or [5000, 20000, 100000]
    workdir = Path(os.environ.get("SCALE_WORKDIR", Path.cwd() / "_scalework"))
    workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for n in ns:
        try:
            results.append(run(n, workdir))
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append({"n": n, "error": f"{type(e).__name__}: {e}"})
    out = Path.cwd() / f"evals/scalebench-{int(time.time())}.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n== resumo ==")
    hdr = ["n", "files", "index_s", "index_files_per_s", "peak_rss_mb",
           "db_mb", "sweep_s", "miss_query_s", "impact_s", "incremental_reindex_s"]
    print(" | ".join(hdr))
    for r in results:
        print(" | ".join(str(r.get(k, "-")) for k in hdr))
    print(f"\nescrito: {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
