"""Concorrência: as duas formas seguras de acessar o índice em paralelo.

1. Conexões próprias por thread + `retry_on_locked` na camada de escrita →
   read-repair/watcher concorrentes não estouram 'database is locked'.
2. UMA conexão compartilhada MAS serializada por lock (como o servidor MCP faz)
   → sqlite não é thread-safe numa conexão; a serialização evita a corrupção
   ('bad parameter or other API misuse', 'another row available').

Regressão para o hardening de concorrência. Escritores editam arquivos DISTINTOS
(editar o mesmo arquivo de 2 threads é uma corrida de disco, não do índice)."""

from __future__ import annotations

import textwrap
import threading

import pytest

from codegraph import AmbiguousSymbol, CodeGraph, SymbolNotFound

# Exceções "aceitáveis" sob edição concorrente: se um arquivo é lido do disco no
# meio de uma escrita (truncate+rewrite de outra thread), a re-indexação pode não
# achar um símbolo por um instante — o sistema levanta HONESTAMENTE, não retorna
# dado errado, e a próxima query acha. O que NÃO se tolera é corrupção de estado
# (InterfaceError/'another row available'/'bad parameter') nem lock não-tratado.
_TOLERABLE = (AmbiguousSymbol, SymbolNotFound)


def _make_repo(root, n=10):
    for i in range(n):
        (root / f"m{i}.py").write_text(textwrap.dedent(f'''
            from m{(i + 1) % n} import f{(i + 1) % n}
            def f{i}(x):
                return f{(i + 1) % n}(x) + {i}
        '''), encoding="utf-8")
    return n


@pytest.mark.timeout(120)
def test_per_thread_connections_with_retry(tmp_path):
    n = _make_repo(tmp_path)
    CodeGraph(tmp_path).index()
    errors: list = []

    def reader():
        cg = CodeGraph(tmp_path)
        try:
            for _ in range(20):
                for call in (lambda: cg.callers("m3.f3"),
                             lambda: cg.callees("m5.f5"),
                             lambda: cg.find_symbol("f7")):
                    try:
                        call()
                    except _TOLERABLE:
                        pass
        except Exception as e:                       # noqa: BLE001
            errors.append(repr(e))
        finally:
            cg.close()

    def writer(k):
        cg = CodeGraph(tmp_path)
        try:
            p = tmp_path / f"m{k}.py"                 # arquivo distinto por writer
            for j in range(15):
                p.write_text(p.read_text(encoding="utf-8") + f"\n# {k}-{j}\n",
                             encoding="utf-8")
                try:
                    cg.query.callers(f"m{k}.f{k}")    # dispara read-repair (escreve)
                except _TOLERABLE:
                    pass
        except Exception as e:                       # noqa: BLE001
            errors.append(repr(e))
        finally:
            cg.close()

    ts = [threading.Thread(target=reader) for _ in range(4)]
    ts += [threading.Thread(target=writer, args=(k,)) for k in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == [], f"esperava zero erros, veio: {errors[:5]}"


@pytest.mark.timeout(120)
def test_shared_connection_serialized(tmp_path):
    _make_repo(tmp_path)
    cg = CodeGraph(tmp_path)
    cg.index()
    lock = threading.RLock()
    errors: list = []

    def reader():
        try:
            for _ in range(25):
                for call in (lambda: cg.callers("m3.f3"),
                             lambda: cg.impact("m1.f1", depth=3),
                             lambda: cg.reaches("m0.f0", sink="f9", depth=6)):
                    with lock:
                        try:
                            call()
                        except _TOLERABLE:
                            pass
        except Exception as e:                       # noqa: BLE001
            errors.append(repr(e))

    def writer(k):
        try:
            p = tmp_path / f"m{k}.py"
            for j in range(15):
                with lock:
                    p.write_text(p.read_text(encoding="utf-8") + f"\n# {k}-{j}\n",
                                 encoding="utf-8")
                    try:
                        cg.query.callers(f"m{k}.f{k}")
                    except _TOLERABLE:
                        pass
        except Exception as e:                       # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=reader) for _ in range(4)]
    ts += [threading.Thread(target=writer, args=(k,)) for k in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    cg.close()
    assert errors == [], f"esperava zero erros, veio: {errors[:5]}"
