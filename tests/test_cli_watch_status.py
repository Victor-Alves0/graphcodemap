from __future__ import annotations

import time
from types import SimpleNamespace

from codegraph import cli
from codegraph import watcher as watcher_module


class _Indexer:
    def __init__(self, *_args):
        self.closed = False

    def index_repo(self):
        return {"indexed": 2}

    def close(self):
        self.closed = True


def test_foreground_watch_emits_initial_and_ready_states(
        tmp_path, monkeypatch, capsys):
    lifecycle = []

    class FakeWatcher:
        def __init__(self, *_args):
            lifecycle.append("created")

        def start(self):
            lifecycle.append("started")

        def stop(self):
            lifecycle.append("stopped")

    monkeypatch.setattr(cli, "Indexer", _Indexer)
    monkeypatch.setattr(watcher_module, "Watcher", FakeWatcher)
    monkeypatch.setattr(time, "sleep", lambda _seconds: (_ for _ in ()).throw(
        KeyboardInterrupt()))

    result = cli.cmd_watch(SimpleNamespace(root=str(tmp_path), db=None))
    output = capsys.readouterr()

    assert result == 0
    assert "watch: estado inicial" in output.out
    assert "watch: pronto" in output.out
    assert lifecycle == ["created", "started", "stopped"]


def test_foreground_watch_reports_initial_index_error(
        tmp_path, monkeypatch, capsys):
    class BrokenIndexer(_Indexer):
        def index_repo(self):
            raise OSError("database unavailable")

    monkeypatch.setattr(cli, "Indexer", BrokenIndexer)

    result = cli.cmd_watch(SimpleNamespace(root=str(tmp_path), db=None))
    output = capsys.readouterr()

    assert result == 1
    assert "watch: estado inicial" in output.out
    assert "watch: erro" in output.err
    assert "database unavailable" in output.err


def test_foreground_watch_reports_observer_start_error(
        tmp_path, monkeypatch, capsys):
    lifecycle = []

    class BrokenWatcher:
        def __init__(self, *_args):
            pass

        def start(self):
            raise RuntimeError("observer unavailable")

        def stop(self):
            lifecycle.append("stopped")

    monkeypatch.setattr(cli, "Indexer", _Indexer)
    monkeypatch.setattr(watcher_module, "Watcher", BrokenWatcher)

    result = cli.cmd_watch(SimpleNamespace(root=str(tmp_path), db=None))
    output = capsys.readouterr()

    assert result == 1
    assert "watch: estado inicial" in output.out
    assert "watch: erro ao iniciar observador" in output.err
    assert "observer unavailable" in output.err
    assert lifecycle == ["stopped"]


def test_foreground_watch_preserves_start_error_when_cleanup_also_fails(
        tmp_path, monkeypatch, capsys):
    class DoublyBrokenWatcher:
        def __init__(self, *_args):
            pass

        def start(self):
            raise RuntimeError("primary start failure")

        def stop(self):
            raise OSError("secondary cleanup failure")

    monkeypatch.setattr(cli, "Indexer", _Indexer)
    monkeypatch.setattr(watcher_module, "Watcher", DoublyBrokenWatcher)

    result = cli.cmd_watch(SimpleNamespace(root=str(tmp_path), db=None))
    output = capsys.readouterr()

    assert result == 1
    assert "RuntimeError: primary start failure" in output.err
    assert "erro secundário" in output.err
    assert "OSError: secondary cleanup failure" in output.err


def test_reaches_accepts_entry_alias(monkeypatch):
    seen = {}

    def fake_reaches(args):
        seen["symbol"] = args.symbol
        seen["entry"] = args.entry
        return 0

    monkeypatch.setattr(cli, "cmd_reaches", fake_reaches)

    assert cli.main(["reaches", "--entry", "pkg.handle", "--sink", "http"]) == 0
    assert seen == {"symbol": None, "entry": "pkg.handle"}


def test_reaches_requires_positional_or_entry(capsys):
    args = SimpleNamespace(
        symbol=None, entry=None, sink="http", via=None, depth=8,
        max_paths=20, deadline_ms=None, max_steps=None,
    )

    assert cli.cmd_reaches(args) == 2
    assert "SYMBOL ou --entry SYMBOL" in capsys.readouterr().err
