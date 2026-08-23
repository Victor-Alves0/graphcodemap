"""Executable contracts for the remaining Java semantic-closure gaps.

These are strict characterizations: each ``xfail`` describes behaviour the
SAST engine must eventually provide, while an accidental XPASS forces the
contract to be reviewed and promoted to an ordinary regression test.
"""

from __future__ import annotations

import pytest

from codegraph import CodeGraph


def _path_findings(tmp_path, files: dict[str, str], *,
                   certain_call: tuple[str, str] | None = None,
                   **taint_options) -> list[dict]:
    for name, source in files.items():
        (tmp_path / name).write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        if certain_call is not None:
            caller, callee = certain_call
            conn = graph.indexer.conn
            sources = conn.execute(
                "SELECT id FROM symbols WHERE name=? AND kind='method'",
                (caller,),
            ).fetchall()
            targets = conn.execute(
                "SELECT id FROM symbols WHERE name=? AND kind='method'",
                (callee,),
            ).fetchall()
            assert len(sources) == len(targets) == 1
            changed = conn.execute(
                "UPDATE edges SET dst=?, confidence='certain', resolver='l1' "
                "WHERE src=? AND kind='calls' "
                "AND (dst_name=? OR dst_name LIKE ?)",
                (targets[0]["id"], sources[0]["id"], callee,
                 f"%.{callee}"),
            ).rowcount
            assert changed == 1
            conn.commit()
            graph.query._facts_cache.clear()
        result, _env = graph.taint(max_findings=100, **taint_options)
        return [
            finding for finding in result["findings"]
            if finding["sink"]["callee"] in {"File", "FileInputStream"}
        ]
    finally:
        graph.close()


def test_typed_properties_file_content_is_external_input(tmp_path):
    """A value loaded from a properties file is data, not trusted metadata."""
    findings = _path_findings(
        tmp_path,
        {
            "App.java": """
                import java.io.FileInputStream;
                import java.util.Properties;

                class App {
                    void handle() throws Exception {
                        Properties properties = new Properties();
                        properties.load(new FileInputStream("config.properties"));
                        String path = properties.getProperty("download.path");
                        new FileInputStream(path);
                    }
                }
            """,
        },
    )

    assert findings


@pytest.mark.parametrize("loaded", [False, True], ids=["programmatic", "overwrite"])
def test_programmatic_properties_literal_remains_clean(tmp_path, loaded):
    load = (
        'properties.load(new java.io.FileInputStream("config.properties"));'
        if loaded else ""
    )
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class App {{
                    void handle() throws Exception {{
                        java.util.Properties properties =
                            new java.util.Properties();
                        {load}
                        properties.setProperty("download.path", "fixed.txt");
                        String path = properties.getProperty("download.path");
                        new java.io.FileInputStream(path);
                    }}
                }}
            """,
        },
    )
    assert not findings


@pytest.mark.parametrize(
    ("else_write", "expected_findings"),
    [
        ("", True),
        ('else { properties.setProperty("download.path", "fixed.txt"); }',
         False),
    ],
    ids=["conditional-clean-fails-closed", "must-clean-on-both-arms"],
)
def test_properties_literal_overwrite_joins_branch_state(
        tmp_path, else_write, expected_findings):
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class App {{
                    void handle(boolean useFixed) throws Exception {{
                        java.util.Properties properties =
                            new java.util.Properties();
                        properties.load(new java.io.FileInputStream(
                            "config.properties"));
                        if (useFixed) {{
                            properties.setProperty(
                                "download.path", "fixed.txt");
                        }} {else_write}
                        String path = properties.getProperty("download.path");
                        new java.io.FileInputStream(path);
                    }}
                }}
            """,
        },
    )

    assert bool(findings) is expected_findings


@pytest.mark.parametrize(
    "overloads",
    [
        """
            String transform(String value) { return value; }
            String transform(int ignored) { return "safe.txt"; }
        """,
        """
            String transform(int ignored) { return "safe.txt"; }
            String transform(String value) { return value; }
        """,
    ],
    ids=["propagating-first", "constant-first"],
)
def test_overload_return_summaries_join_candidates_independent_of_order(
        tmp_path, overloads):
    """A constant overload cannot erase a sibling that propagates taint."""
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                final class Helper {{
                    {overloads}
                }}
                class App {{
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        Helper helper = new Helper();
                        String output = helper.transform(path);
                        new java.io.FileInputStream(output);
                    }}
                }}
            """,
        },
    )

    assert findings


@pytest.mark.parametrize("forwarders", [3, 4], ids=["juliet-53", "juliet-54"])
def test_default_scan_composes_transparent_forwarder_chain(tmp_path, forwarders):
    """Pure forwarding wrappers must not consume the vulnerability depth budget."""
    methods = []
    for index in range(1, forwarders + 1):
        target = f"forward{index + 1}(value)" if index < forwarders else (
            "new java.io.FileInputStream(value)"
        )
        methods.append(
            f"void forward{index}(String value) throws Exception {{ {target}; }}"
        )
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class App {{
                    {' '.join(methods)}
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        forward1(path);
                    }}
                }}
            """,
        },
    )

    assert findings


@pytest.mark.parametrize(
    "forward1_body",
    [
        "audit(value); forward2(value)",
        "forward2(value); observe(value)",
    ],
    ids=["effect", "fan-out"],
)
def test_nontransparent_forwarder_consumes_depth(tmp_path, forward1_body):
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class App {{
                    void audit(String value) {{ }}
                    void observe(String value) {{ }}
                    void forward2(String value) throws Exception {{
                        new java.io.FileInputStream(value);
                    }}
                    void forward1(String value) throws Exception {{
                        {forward1_body};
                    }}
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        forward1(request.getParameter("path"));
                    }}
                }}
            """,
        },
        depth=2,
    )
    assert not findings


def test_new_receiver_forwarders_across_files_are_depth_neutral(tmp_path):
    """A confined receiver allocation is part of one forwarding call."""
    findings = _path_findings(
        tmp_path,
        {
            "Entry.java": """
                class Entry {
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {
                        String value = request.getParameter("path");
                        new RelayOne().pass(value);
                    }
                }
            """,
            "RelayOne.java": """
                class RelayOne {
                    void pass(String value) throws Exception {
                        new RelayTwo().pass(value);
                    }
                }
            """,
            "RelayTwo.java": """
                class RelayTwo {
                    void pass(String value) throws Exception {
                        new RelayThree().pass(value);
                    }
                }
            """,
            "RelayThree.java": """
                class RelayThree {
                    void pass(String value) throws Exception {
                        new java.io.FileInputStream(value);
                    }
                }
            """,
        },
    )
    assert findings


def test_effectful_receiver_constructor_is_not_depth_neutral(tmp_path):
    findings = _path_findings(
        tmp_path,
        {
            "App.java": """
                class App {
                    static class Relay {
                        Relay(String observed) { audit(observed); }
                        static void audit(String value) { }
                        void pass(String value) throws Exception {
                            new java.io.FileInputStream(value);
                        }
                    }
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {
                        String value = request.getParameter("path");
                        new Relay(value).pass(value);
                    }
                }
            """,
        },
        depth=1,
    )
    assert not findings


@pytest.mark.parametrize("returned", ["built", '"fixed.txt"'],
                         ids=["builder-result", "unrelated-literal"])
def test_parametric_return_tracks_chained_builder_receiver(
        tmp_path, returned):
    """A chained top-call must retain its underlying local receiver slice."""
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class Transformer {{
                    String transform(String value) {{
                        StringBuilder builder = new StringBuilder(value);
                        String built = builder.append("suffix").toString();
                        return {returned};
                    }}
                }}
                class App {{
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        String result = new Transformer().transform(path);
                        new java.io.FileInputStream(result);
                    }}
                }}
            """,
        },
        certain_call=("handle", "transform"),
    )
    assert bool(findings) is (returned == "built")


@pytest.mark.parametrize(
    ("declaration", "store", "load"),
    [
        (
            "Holder box = new Holder()",
            "box.value = path",
            "String value = box.value",
        ),
        (
            "java.util.Vector<String> box = new java.util.Vector<>()",
            "box.add(path)",
            "String value = box.remove(0)",
        ),
        (
            "java.util.LinkedList<String> box = new java.util.LinkedList<>()",
            "box.add(path)",
            "String value = box.remove(0)",
        ),
        (
            "java.util.HashMap<Integer, String> box = new java.util.HashMap<>()",
            "box.put(0, path)",
            "String value = box.get(0)",
        ),
    ],
    ids=["field", "vector", "linked-list", "hash-map"],
)
def test_container_payload_crosses_a_typed_method_boundary(
    tmp_path, declaration, store, load
):
    """Passing a container must preserve the taint of its stored payload."""
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class Holder {{ String value; }}
                class App {{
                    void consume({declaration.split(' box =', 1)[0]} box)
                            throws Exception {{
                        {load};
                        new java.io.FileInputStream(value);
                    }}
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        {declaration};
                        {store};
                        consume(box);
                    }}
                }}
            """,
        },
    )

    assert findings


@pytest.mark.parametrize("collection", ["Vector", "LinkedList"])
@pytest.mark.parametrize("dirty", [True, False], ids=["dirty", "literal"])
def test_indexed_list_payload_crosses_new_receiver_boundary(
        tmp_path, collection, dirty):
    payload = "path" if dirty else '"fixed.txt"'
    findings = _path_findings(
        tmp_path,
        {
            "Entry.java": f"""
                class Entry {{
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        java.util.{collection}<String> values =
                            new java.util.{collection}<String>();
                        values.add(0, {payload});
                        values.add(1, {payload});
                        values.add(2, {payload});
                        new Consumer().accept(values);
                    }}
                }}
            """,
            "Consumer.java": f"""
                class Consumer {{
                    void accept(java.util.{collection}<String> values)
                            throws Exception {{
                        String value = values.remove(2);
                        new java.io.FileInputStream(value);
                    }}
                }}
            """,
        },
    )
    assert bool(findings) is dirty


@pytest.mark.parametrize(
    ("declaration", "store_dirty", "clean", "load"),
    [
        ("Holder box = new Holder()", "box.value = path",
         'box.value = "fixed"', "String value = box.value"),
        ("java.util.Vector<String> box = new java.util.Vector<>()",
         "box.add(path)", "box.remove(0); box.add(\"fixed\")",
         "String value = box.get(0)"),
        ("java.util.LinkedList<String> box = new java.util.LinkedList<>()",
         "box.add(path)", "box.remove(0); box.add(\"fixed\")",
         "String value = box.get(0)"),
        ("java.util.HashMap<Integer, String> box = new java.util.HashMap<>()",
         "box.put(0, path)", 'box.put(0, "fixed")',
         "String value = box.get(0)"),
    ],
    ids=["field", "vector", "linked-list", "hash-map"],
)
def test_clean_payload_overwrite_crosses_boundary_as_clean(
        tmp_path, declaration, store_dirty, clean, load):
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class Holder {{ String value; }}
                class App {{
                    void consume({declaration.split(' box =', 1)[0]} box)
                            throws Exception {{
                        {load};
                        new java.io.FileInputStream(value);
                    }}
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        {declaration};
                        {store_dirty};
                        {clean};
                        consume(box);
                    }}
                }}
            """,
        },
    )
    assert not findings


@pytest.mark.parametrize("collection", ["Vector", "HashMap"])
def test_conditional_container_payload_is_may_tainted(tmp_path, collection):
    if collection == "Vector":
        declaration = "java.util.Vector<String> box = new java.util.Vector<>()"
        dirty = "box.add(path)"
        clean = 'box.add("fixed")'
        load = "String value = box.get(0)"
        param_type = "java.util.Vector<String>"
    else:
        declaration = (
            "java.util.HashMap<Integer, String> box = new java.util.HashMap<>()"
        )
        dirty = "box.put(0, path)"
        clean = 'box.put(0, "fixed")'
        load = "String value = box.get(0)"
        param_type = "java.util.HashMap<Integer, String>"
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class App {{
                    void consume({param_type} box) throws Exception {{
                        {load};
                        new java.io.FileInputStream(value);
                    }}
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        {declaration};
                        if (request.isSecure()) {{ {dirty}; }}
                        else {{ {clean}; }}
                        consume(box);
                    }}
                }}
            """,
        },
    )
    assert findings


@pytest.mark.parametrize("dirty", [True, False], ids=["dirty", "literal"])
def test_serialized_payload_crosses_method_boundary(tmp_path, dirty):
    value = "path" if dirty else '"fixed.txt"'
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class App {{
                    void consume(byte[] encoded) throws Exception {{
                        java.io.ByteArrayInputStream bytes =
                            new java.io.ByteArrayInputStream(encoded);
                        java.io.ObjectInputStream input =
                            new java.io.ObjectInputStream(bytes);
                        String value = (String) input.readObject();
                        new java.io.FileInputStream(value);
                    }}
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        String path = request.getParameter("path");
                        java.io.ByteArrayOutputStream bytes =
                            new java.io.ByteArrayOutputStream();
                        java.io.ObjectOutputStream output =
                            new java.io.ObjectOutputStream(bytes);
                        output.writeObject({value});
                        byte[] encoded = bytes.toByteArray();
                        consume(encoded);
                    }}
                }}
            """,
        },
    )
    assert bool(findings) is dirty


@pytest.mark.parametrize("dirty", [True, False], ids=["dirty", "overwrite"])
def test_ordered_static_field_crosses_no_arg_call(tmp_path, dirty):
    overwrite = "" if dirty else 'Shared.path = "fixed.txt";'
    findings = _path_findings(
        tmp_path,
        {
            "Shared.java": "class Shared { static String path; }",
            "Consumer.java": """
                class Consumer {
                    void consume() throws Exception {
                        String value = Shared.path;
                        new java.io.FileInputStream(value);
                    }
                }
            """,
            "App.java": f"""
                class App {{
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {{
                        Shared.path = request.getParameter("path");
                        {overwrite}
                        new Consumer().consume();
                    }}
                }}
            """,
        },
    )
    assert bool(findings) is dirty


def test_invoked_lambda_executes_with_its_captured_taint(tmp_path):
    """A lambda sink is reachable only when the lambda is actually invoked."""
    findings = _path_findings(
        tmp_path,
        {
            "App.java": """
                class App {
                    void handle(javax.servlet.http.HttpServletRequest request) {
                        String path = request.getParameter("path");
                        Runnable task = () -> {
                            try { new java.io.FileInputStream(path); }
                            catch (Exception ignored) { }
                        };
                        task.run();
                    }
                }
            """,
        },
    )

    assert findings


def test_concrete_assignment_closes_abstract_dispatch(tmp_path):
    """The exact ``new BadAction`` assignment selects the bad override."""
    findings = _path_findings(
        tmp_path,
        {
            "Action.java": """
                abstract class Action { abstract void accept(String value) throws Exception; }
            """,
            "BadAction.java": """
                class BadAction extends Action {
                    void accept(String value) throws Exception {
                        new java.io.FileInputStream(value);
                    }
                }
            """,
            "App.java": """
                class App {
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {
                        String path = request.getParameter("path");
                        Action action = new BadAction();
                        action.accept(path);
                    }
                }
            """,
        },
    )

    assert findings


def test_concrete_safe_override_does_not_select_bad_sibling(tmp_path):
    findings = _path_findings(
        tmp_path,
        {
            "Action.java": """
                abstract class Action {
                    abstract void accept(String value) throws Exception;
                }
            """,
            "BadAction.java": """
                class BadAction extends Action {
                    void accept(String value) throws Exception {
                        new java.io.FileInputStream(value);
                    }
                }
            """,
            "SafeAction.java": """
                class SafeAction extends Action {
                    void accept(String value) { }
                }
            """,
            "App.java": """
                class App {
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {
                        String path = request.getParameter("path");
                        Action action = new SafeAction();
                        action.accept(path);
                    }
                }
            """,
        },
    )
    assert not findings


def test_tainted_system_property_write_overrides_literal_trust(tmp_path):
    """An earlier in-process write overrides the trusted ``user.dir`` default."""
    findings = _path_findings(
        tmp_path,
        {
            "App.java": """
                class App {
                    void handle(javax.servlet.http.HttpServletRequest request) {
                        String path = request.getParameter("path");
                        System.setProperty("user.dir", path);
                        new java.io.File(System.getProperty("user.dir"));
                    }
                }
            """,
        },
    )

    assert findings


@pytest.mark.parametrize("reset", ["literal", "clear"])
def test_system_property_clean_overwrite_restores_literal_trust(tmp_path, reset):
    overwrite = (
        'System.setProperty("user.dir", "fixed");'
        if reset == "literal" else 'System.clearProperty("user.dir");'
    )
    findings = _path_findings(
        tmp_path,
        {
            "App.java": f"""
                class App {{
                    void handle(javax.servlet.http.HttpServletRequest request) {{
                        String path = request.getParameter("path");
                        System.setProperty("user.dir", path);
                        {overwrite}
                        new java.io.File(System.getProperty("user.dir"));
                    }}
                }}
            """,
        },
    )
    assert not findings


def test_dynamic_system_property_write_is_conservative(tmp_path):
    findings = _path_findings(
        tmp_path,
        {
            "App.java": """
                class App {
                    void handle(javax.servlet.http.HttpServletRequest request) {
                        String key = request.getParameter("key");
                        String path = request.getParameter("path");
                        System.setProperty(key, path);
                        new java.io.File(System.getProperty("user.dir"));
                    }
                }
            """,
        },
    )
    assert findings


def test_untyped_domain_get_remains_fail_closed(tmp_path):
    """Only a proven Map lookup may detach its selector from the payload."""
    findings = _path_findings(
        tmp_path,
        {
            "App.java": """
                class Selector {
                    String get(String key) { return key; }
                }
                class App {
                    void handle(javax.servlet.http.HttpServletRequest request)
                            throws Exception {
                        String key = request.getParameter("path");
                        String value = new Selector().get(key);
                        new java.io.FileInputStream(value);
                    }
                }
            """,
        },
    )
    assert findings
