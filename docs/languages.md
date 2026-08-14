# Languages & Resolvers

GraphCodeMap works on *any* tree-sitter-supported language out of the box, and
gets progressively smarter as more layers apply. Support comes in three tiers,
plus an optional semantic layer (L1) that promotes edges to `certain`.

## The three tiers

### 1. Dedicated extractors (23)

Refined extraction: scoped fully-qualified names, imports, calls resolved at the
name site, inheritance/implementation.

> Python, TypeScript/TSX, JavaScript, Rust, Go, Java, Kotlin, C#, C,
> C++/CUDA/Metal, PHP, Ruby, Lua/Luau, Swift, Scala, Clojure/ClojureScript,
> **Terraform/HCL**, and the web tier **HTML** + **CSS/SCSS**.

**Terraform/HCL** is block-oriented: resources, data sources, variables, outputs,
modules, and providers are addressed by their Terraform address
(`aws_instance.web`, `var.region`), and `references` edges resolve by fqn-suffix
match — so you can ask *what depends on `var.region`?* across a config.

**The web tier** is the one place a cross-language edge exists at L0. CSS/SCSS
*defines* selectors (`css_class`/`css_id`, plus SCSS `@mixin`/`@function`); HTML
contributes `id` anchors and treats `class="…"` as *usage*; and usage is also
emitted from **`className=`/`class=` in JSX/TSX** — where a React/Vue codebase
actually consumes its classes. The result: `references` edges link both markup and
components to the stylesheet that defines a class, which makes two real questions
answerable — *who uses `.menu-item`?* and *which classes are dead?* Asset
dependencies (`<script src>`, `<link href>`, `@import`, `@use`) resolve to the
target **file** (itself a `kind='file'` symbol); external packages match nothing
and stay honestly unresolved.

### 2. Generic tier

Structural heuristics over any tree-sitter grammar — symbols and containment
without hand-tuned fqn/call extraction:

> Zig, PowerShell, Elixir, Objective-C, Julia, Vue, Svelte, Astro,
> Groovy/Gradle, Dart, Verilog/SystemVerilog, SQL, Fortran, Pascal/Delphi, Bash,
> Apex, Razor, XML project files.

### 3. Docs & data

Markdown (headings as sections) and JSON/YAML/TOML (top-level keys).

Binary/document formats (`.pdf`, `.docx`) and structureless formats (`.sln`,
`.dfm`) stay out of the structural graph by design.

## Dataflow & taint coverage

`dataflow`, `taint`, and `reaches` cover all **19 dedicated code-language
identifiers** (Python, JavaScript, TypeScript/TSX, Java, C#, C/C++/CUDA, Go,
Rust, Ruby, PHP, Kotlin, Swift, Scala, Lua/Luau and Clojure). Eighteen use the
flow-sensitive engine; Clojure currently uses the conservative fallback.
Markup and config languages have no dataflow, and say so.

## L1 semantic resolution (promoting edges to `certain`)

L0 gives you `inferred`/`possible` edges from syntax. **L1** runs a real language
server (or jedi for Python) and promotes a call edge to `certain` when exactly one
in-repo definition is found — including instance-method calls that name-based
resolution can only ever mark `possible`.

**Every dedicated code language has a resolver wired**; markup/config languages
do not need one. Resolution uses one generic LSP client plus two special cases:

- **Python** — via [jedi](https://github.com/davidhalter/jedi), in-process (no
  subprocess), enabled by `pip install "graphcodemap[l1]"`.
- **JS/TS** — via the TypeScript language service (needs `node` + `typescript@5`).
- **Everything else** — via one generic LSP client that speaks
  `textDocument/definition`.

### Resolver status

| Status | Languages |
|---|---|
| **Validated against a live server** | Python (jedi), JS/TS (tsserver), Go (`gopls`), Rust (`rust-analyzer`), Lua (`lua-language-server`), Clojure (`clojure-lsp`), Java (`jdtls`), PHP (`intelephense`) |
| **Wired, inert until the toolchain is present** | C/C++ (`clangd`), Ruby (`solargraph`), Kotlin (`kotlin-language-server`), C# (`csharp-ls`), Scala (`metals`), Swift (`sourcekit-lsp`) |

PHP's `intelephense` is the first server shipped as an **npm package** rather
than a native binary. What `npm` puts on `PATH` is a shim (`.cmd` on Windows),
which `CreateProcess` cannot launch without a shell — so discovery prefers the
real entrypoint (`lib/intelephense.js`) and runs it under `node`. Measured on
DVWA (169 PHP files): **0 → 659 `certain` edges**, 75% of all call edges, with
taint findings unchanged.

Java's `jdtls` is notable as the first **launcher-based** server (it runs on the
JVM via an Eclipse launcher, `java -jar <equinox-launcher> …`, not a bare `PATH`
binary) — proving the client generalizes beyond a single executable.

Measured on the OWASP Benchmark Maven checkout (2,766 Java source files), JDTLS
1.60.0 promoted **12,937** call edges with zero resolver errors. GraphCodeMap
uses those Java promotions for return pruning only when the body supplies an
independent proof: folded local control flow, or a locally-created list using
`add/remove/get` with constant indices. Virtual/interface dispatch, reflection
and arbitrary aliases remain conservative because resolving the call target
does not prove which value returns at runtime. Gradle is the remaining
real-project validation gap.

### Activating a resolver

An LSP-backed language activates **automatically when its server is on `PATH`**
(or pointed at via `CODEGRAPH_<SERVER>`, e.g. `CODEGRAPH_JDTLS` for the JDT LS
install dir). Then:

```bash
codegraph refine        # promote edges to certain across the index
# or fold it into indexing:
codegraph index --l1
```

Resolution quality depends on the server finding the project — a `go.mod`,
`Cargo.toml`, `compile_commands.json`, or a build tool at the right root. The
generic client waits for async servers to finish indexing before querying, and
handles monorepos by grouping files under their project root (one server per root).

### Why L1 matters

A `certain` edge is a **semantic fact, not a name guess** — so an agent can trust a
`reaches`/`impact`/`callers` answer and *stop*, instead of re-verifying by reading
files. In our reachability benchmark this made the graph arm both more correct and
~2.4× cheaper in tokens than a grep/read baseline (see
[evals/RESULTS.md](../evals/RESULTS.md)). Adding L1 to a language is what turns
"the graph is sometimes worth it" into "the graph wins" there.

Languages without an active resolver keep honest `inferred`/`possible` edges —
never a fabricated `certain`.

## Adding a language

- **A new generic-tier language** is often just a grammar mapping.
- **A new LSP resolver** is typically a **~10-line config** on
  [`l1/lsp_base.py`](../src/codegraph/l1/lsp_base.py): the server command, the
  root markers, and the language id.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the workflow (write the test battery
first).
