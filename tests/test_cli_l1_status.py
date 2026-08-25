"""Contrato da telemetria L1 exposta pela CLI."""

from __future__ import annotations

import json

from codegraph import cli, l1


class MissingJava:
    languages = ("java",)
    cmd_name = "jdtls"
    cmd_env = "CODEGRAPH_JDTLS"


def _healthy(language: str, promoted: int = 0):
    class Healthy:
        languages = (language,)
        root_markers = ()

        def __init__(self, *_args, **_kwargs):
            pass

        def refine_file(self, *_args):
            return promoted

        def close(self):
            pass

    Healthy.__name__ = f"Healthy{language.title()}"
    return Healthy


def _write(tmp_path, rel: str, source: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _field(output: str, name: str):
    line = next(line for line in output.splitlines()
                if line.strip().startswith(f"{name}:"))
    return json.loads(line.split(":", 1)[1].strip())


def test_index_refine_java_only_without_jdtls_is_partial(
        tmp_path, monkeypatch, capsys):
    _write(tmp_path, "Main.java", "class Main {}\n")
    monkeypatch.setattr(l1, "all_resolvers", lambda: [MissingJava])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [])

    code = cli.main(["--root", str(tmp_path), "index", "--refine"])
    output = capsys.readouterr().out

    assert code == 1
    assert "status: partial; partial: true" in output
    assert _field(output, "applicable") == ["java"]
    assert _field(output, "attempted") == []
    assert _field(output, "unavailable")[0]["languages"] == ["java"]
    assert "resolver L1 indisponível" in _field(output, "warnings")[0]


def test_index_refine_mixed_repo_reports_attempted_and_unavailable(
        tmp_path, monkeypatch, capsys):
    _write(tmp_path, "Main.java", "class Main {}\n")
    _write(tmp_path, "main.py", "def run():\n    return 1\n")
    healthy_python = _healthy("python")
    monkeypatch.setattr(l1, "all_resolvers",
                        lambda: [MissingJava, healthy_python])
    monkeypatch.setattr(l1, "available_resolvers",
                        lambda _root: [healthy_python])

    code = cli.main(["--root", str(tmp_path), "index", "--l1"])
    output = capsys.readouterr().out

    assert code == 1
    assert _field(output, "applicable") == ["java", "python"]
    assert _field(output, "attempted") == ["python"]
    assert _field(output, "unavailable")[0]["languages"] == ["java"]


def test_refine_zero_promotions_complete_is_success(tmp_path, monkeypatch,
                                                    capsys):
    _write(tmp_path, "Main.java", "class Main {}\n")
    assert cli.main(["--root", str(tmp_path), "index"]) == 0
    capsys.readouterr()
    healthy_java = _healthy("java", promoted=0)
    monkeypatch.setattr(l1, "all_resolvers", lambda: [healthy_java])
    monkeypatch.setattr(l1, "available_resolvers",
                        lambda _root: [healthy_java])

    code = cli.main(["--root", str(tmp_path), "refine"])
    output = capsys.readouterr().out

    assert code == 0
    assert "0 aresta(s)" in output
    assert "status: complete; partial: false" in output
    assert _field(output, "unavailable") == []
    assert _field(output, "warnings") == []


def test_refine_success_with_promotions_remains_zero_exit(tmp_path, monkeypatch,
                                                          capsys):
    _write(tmp_path, "Main.java", "class Main { void run() {} }\n")
    assert cli.main(["--root", str(tmp_path), "index"]) == 0
    capsys.readouterr()
    healthy_java = _healthy("java", promoted=1)
    monkeypatch.setattr(l1, "all_resolvers", lambda: [healthy_java])
    monkeypatch.setattr(l1, "available_resolvers",
                        lambda _root: [healthy_java])

    code = cli.main(["--root", str(tmp_path), "refine"])
    output = capsys.readouterr().out

    assert code == 0
    assert "1 aresta(s)" in output
    assert _field(output, "applicable") == ["java"]
    assert _field(output, "attempted") == ["java"]


def test_repeated_index_l1_reuses_snapshot_without_starting_resolver(
        tmp_path, monkeypatch, capsys):
    _write(tmp_path, "Main.java", "class Main { void run() {} }\n")
    starts = []

    class CountedJava:
        languages = ("java",)
        root_markers = ()

        def __init__(self, *_args, **_kwargs):
            starts.append(1)

        def refine_file(self, *_args):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(l1, "all_resolvers", lambda: [CountedJava])
    monkeypatch.setattr(l1, "available_resolvers", lambda _root: [CountedJava])

    assert cli.main(["--root", str(tmp_path), "index", "--l1"]) == 0
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), "index", "--l1"]) == 0
    output = capsys.readouterr().out

    assert len(starts) == 1
    assert "snapshot semântico reutilizado" in output
    assert _field(output, "attempted") == ["java"]


def test_refine_real_resolver_failure_returns_nonzero(tmp_path, monkeypatch,
                                                      capsys):
    _write(tmp_path, "Main.java", "class Main {}\n")
    assert cli.main(["--root", str(tmp_path), "index"]) == 0
    capsys.readouterr()

    class BrokenJava:
        languages = ("java",)
        root_markers = ()

        def __init__(self, *_args, **_kwargs):
            raise OSError("JDTLS falhou ao iniciar")

    monkeypatch.setattr(l1, "all_resolvers", lambda: [BrokenJava])
    monkeypatch.setattr(l1, "available_resolvers",
                        lambda _root: [BrokenJava])

    code = cli.main(["--root", str(tmp_path), "refine"])
    output = capsys.readouterr().out

    assert code == 1
    assert "status: partial; partial: true" in output
    assert _field(output, "attempted") == ["java"]
    assert _field(output, "unavailable") == []
    assert any("JDTLS falhou ao iniciar" in warning
               for warning in _field(output, "warnings"))
