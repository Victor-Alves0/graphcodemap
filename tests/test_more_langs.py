"""Extractors Java, Kotlin, C#, C, C++ e PHP."""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

APP_JAVA = '''
import java.util.List;

public class Scheduler extends Base implements Runnable {
    public void execute(Task t) {
        process(t);
        Task x = new Task();
    }
    private int process(Task t) { return t.weight(); }
}
class Base {}
class Task {
    int weight() { return 1; }
}
'''

APP_KT = '''
import kotlin.math.abs

const val MAX = 4

open class Base(val n: Int)

class Scheduler(n: Int) : Base(n) {
    fun execute(t: Int): Int {
        return process(t)
    }
    fun process(t: Int): Int = abs(t)
}

interface Processor {
    fun handle(x: Int): Int
}

fun topLevel(x: Int) = Scheduler(x).execute(x)
'''

APP_CS = '''
using System;

namespace App {
    public class Scheduler : Base, IRunnable {
        public void Execute(Task t) {
            Process(t);
            var x = new Task();
        }
        private int Process(Task t) { return 1; }
    }
    public class Base {}
    public interface IRunnable {}
    public class Task {}
}
'''

APP_C = '''
#include <stdio.h>
#define MAX_TASKS 64

typedef struct Task { int id; } Task;

static int helper(int x);

int process_task(Task* t) {
    return helper(t->id);
}

static int helper(int x) { return x * 2; }
'''

APP_CPP = '''
#include <vector>
namespace app {

class Base {
public:
    virtual int run();
};

class Scheduler : public Base {
public:
    int run() override;
    int helper(int x) { return x * 2; }
};

int Scheduler::run() {
    return helper(1) + process(2);
}

int process(int x) { return x; }

}
'''

APP_PHP = '''<?php
use App\\Db\\Session;

const MAX = 4;

interface Processor {
    public function process(int $t): int;
}

class Scheduler extends Base implements Processor {
    public function process(int $t): int {
        return helper($t);
    }
    public function execute(int $t): int {
        return $this->process($t);
    }
}

class Base {}

function helper(int $t): int {
    return $t + 1;
}
'''


@pytest.fixture()
def cg3(tmp_path):
    files = {
        "App.java": APP_JAVA, "sched.kt": APP_KT, "App.cs": APP_CS,
        "core.c": APP_C, "engine.cpp": APP_CPP, "web.php": APP_PHP,
    }
    for name, content in files.items():
        (tmp_path / name).write_text(textwrap.dedent(content), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def fqns(rows):
    return {r["fqn"] for r in rows}


def test_all_parsed_clean(cg3):
    s = cg3.stats()
    assert s["files"] == 6
    assert set(s["by_language"]) == {"java", "kotlin", "csharp", "c", "cpp", "php"}


# -- Java ---------------------------------------------------------------------

def test_java_symbols_and_inherits(cg3):
    rows, _ = cg3.find_symbol("App.Scheduler.execute")
    assert "App.Scheduler.execute" in fqns(rows)
    sym, rows, _ = cg3.references("App.Base", kind="inherits")
    assert any(r["src_fqn"] == "App.Scheduler" for r in rows)


def test_java_call_and_new(cg3):
    sym, rows, _ = cg3.callers("App.Scheduler.process")
    assert any(r["other_fqn"] == "App.Scheduler.execute" for r in rows)
    sym, rows, _ = cg3.callers("App.Task")  # new Task()
    assert any(r["other_fqn"] == "App.Scheduler.execute" for r in rows)


# -- Kotlin -------------------------------------------------------------------

def test_kotlin_symbols(cg3):
    rows, _ = cg3.find_symbol("Scheduler", kind="class")
    assert "sched.Scheduler" in fqns(rows)
    rows, _ = cg3.find_symbol("Processor", kind="interface")
    assert "sched.Processor" in fqns(rows)
    rows, _ = cg3.find_symbol("MAX", kind="constant")
    assert "sched.MAX" in fqns(rows)
    rows, _ = cg3.find_symbol("topLevel", kind="function")
    assert "sched.topLevel" in fqns(rows)


def test_kotlin_inherits_and_calls(cg3):
    sym, rows, _ = cg3.references("sched.Base", kind="inherits")
    assert any(r["src_fqn"] == "sched.Scheduler" for r in rows)
    sym, rows, _ = cg3.callers("sched.Scheduler.process")
    assert any(r["other_fqn"] == "sched.Scheduler.execute" for r in rows)


# -- C# -----------------------------------------------------------------------

def test_csharp_symbols_and_namespace(cg3):
    rows, _ = cg3.find_symbol("Execute")
    assert "App.App.Scheduler.Execute" in fqns(rows)  # arquivo App + namespace App
    sym, rows, _ = cg3.references("App.App.Base", kind="inherits")
    assert any(r["src_fqn"] == "App.App.Scheduler" for r in rows)
    sym, rows, _ = cg3.callers("App.App.Scheduler.Process")
    assert any(r["other_fqn"] == "App.App.Scheduler.Execute" for r in rows)


# -- C ------------------------------------------------------------------------

def test_c_symbols(cg3):
    rows, _ = cg3.find_symbol("process_task")
    assert "core.process_task" in fqns(rows)
    rows, _ = cg3.find_symbol("MAX_TASKS", kind="constant")
    assert "core.MAX_TASKS" in fqns(rows)
    rows, _ = cg3.find_symbol("Task", kind="struct")
    assert "core.Task" in fqns(rows)


def test_c_call_dedup_prototype(cg3):
    # protótipo + definição de helper não podem virar ambiguidade
    sym, rows, _ = cg3.callers("core.helper")
    confs = [r["confidence"] for r in rows if r["other_fqn"] == "core.process_task"]
    assert confs and confs[0] == "inferred"


# -- C++ ----------------------------------------------------------------------

def test_cpp_out_of_class_definition(cg3):
    # int Scheduler::run() fora da classe → método de engine.app.Scheduler
    rows, _ = cg3.find_symbol("run", kind="method")
    assert "engine.app.Scheduler.run" in fqns(rows)


def test_cpp_inherits_and_calls(cg3):
    sym, rows, _ = cg3.references("engine.app.Base", kind="inherits")
    assert any(r["src_fqn"] == "engine.app.Scheduler" for r in rows)
    sym, rows, _ = cg3.callers("engine.app.Scheduler.helper")
    assert any(r["other_fqn"] == "engine.app.Scheduler.run" for r in rows)


# -- PHP ----------------------------------------------------------------------

def test_php_symbols_and_edges(cg3):
    rows, _ = cg3.find_symbol("Scheduler", kind="class")
    assert "web.Scheduler" in fqns(rows)
    sym, rows, _ = cg3.references("web.Base", kind="inherits")
    assert any(r["src_fqn"] == "web.Scheduler" for r in rows)
    sym, rows, _ = cg3.callers("web.helper")
    assert any(r["other_fqn"] == "web.Scheduler.process" for r in rows)
    # $this->process() → método da própria classe
    sym, rows, _ = cg3.callers("web.Scheduler.process")
    assert any(r["other_fqn"] == "web.Scheduler.execute" for r in rows)
