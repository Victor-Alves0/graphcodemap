"""Frescor watcher-aware (docs/DESIGN.md §2.2/§2.3).

Quando um watcher vivo mantém o índice quente, a query pula a varredura de
frescor O(N) — o índice já reflete o que o watcher observou. A garantia forte é
preservada nos caminhos SEM watcher e DURANTE o debounce do watcher (evento
anotado, ainda não aplicado → `is_current()` False → varre). Um backstop
periódico cobre eventos que o watchdog possa ter perdido.

Testes determinísticos: contam quantas vezes a varredura roda, com um watcher
FALSO cujo estado controlamos — sem depender de timing de FS real.
"""

from __future__ import annotations

import types

import codegraph.query as q


class _FakeWatcher:
    def __init__(self, current: bool) -> None:
        self._current = current

    def is_current(self) -> bool:
        return self._current


def _count_sweeps(monkeypatch):
    calls = {"n": 0}
    real = q.scan_source_stats

    def counting(root, spec=None, scopes=None):
        calls["n"] += 1
        return real(root, spec, scopes)

    monkeypatch.setattr(q, "scan_source_stats", counting)
    return calls


def test_no_watcher_sweeps_every_miss(cg, monkeypatch):
    # sem watcher: garantia forte — varre a CADA resultado vazio
    calls = _count_sweeps(monkeypatch)
    cg.find_symbol("inexistente_a")
    cg.find_symbol("inexistente_b")
    assert calls["n"] == 2


def test_watcher_current_skips_sweep(cg, monkeypatch):
    calls = _count_sweeps(monkeypatch)
    cg.query.attach_watcher(_FakeWatcher(current=True))
    cg.query._last_full_sweep = 0.0            # 1ª chamada dispara o backstop
    cg.find_symbol("inexistente_a")            # backstop (last=0) → varre 1x
    cg.find_symbol("inexistente_b")            # watcher drenado, dentro do backstop → pula
    cg.find_symbol("inexistente_c")            # idem → pula
    assert calls["n"] == 1


def test_watcher_not_current_still_sweeps(cg, monkeypatch):
    # watcher com evento pendente (debounce) → NÃO garante frescor → varre
    calls = _count_sweeps(monkeypatch)
    cg.query.attach_watcher(_FakeWatcher(current=False))
    cg.find_symbol("inexistente_a")
    cg.find_symbol("inexistente_b")
    assert calls["n"] == 2


def test_backstop_forces_sweep_even_when_current(cg, monkeypatch):
    # backstop imediato: mesmo com watcher drenado, varre (rede p/ eventos perdidos)
    calls = _count_sweeps(monkeypatch)
    cg.query.attach_watcher(_FakeWatcher(current=True))
    cg.query._sweep_backstop = 0.0
    cg.find_symbol("inexistente_a")
    cg.find_symbol("inexistente_b")
    assert calls["n"] == 2


def test_watched_skip_still_sees_watcher_applied_edit(cg, repo, monkeypatch):
    # o watcher (falso, drenado) fez o índice já conter o novo símbolo; mesmo
    # pulando a varredura, a query o encontra — pular é seguro quando fresco.
    calls = _count_sweeps(monkeypatch)
    cg.query.attach_watcher(_FakeWatcher(current=True))
    cg.query._last_full_sweep = 1e18           # backstop longe → sempre pula
    # simula o watcher tendo aplicado a edição: indexa um arquivo novo direto
    (repo / "app" / "novo.py").write_text(
        "def recem_indexada():\n    return 1\n", encoding="utf-8")
    cg.indexer.index_file("app/novo.py")
    rows, _ = cg.find_symbol("recem_indexada")
    assert any(r["fqn"] == "app.novo.recem_indexada" for r in rows)
    assert calls["n"] == 0                      # nenhuma varredura O(N) rodou


def test_watcher_is_current_transitions():
    # lógica de is_current() sem iniciar um Observer real (determinístico)
    from codegraph.watcher import Watcher

    w = Watcher(".")
    assert w.is_current() is False             # observer não iniciado
    w._observer = types.SimpleNamespace(is_alive=lambda: True)
    assert w.is_current() is True              # vivo e drenado
    w._pending.add("x.py")
    assert w.is_current() is False             # evento pendente (debounce)
    w._pending.clear()
    w._full_rescan = True
    assert w.is_current() is False             # rescan pendente
    w._full_rescan = False
    assert w.is_current() is True
    w._observer = types.SimpleNamespace(is_alive=lambda: False)
    assert w.is_current() is False             # observer morto
