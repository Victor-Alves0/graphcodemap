/**
 * Serviço L1 para JS/TS: TypeScript LanguageService via stdin/stdout.
 * Protocolo JSON-lines: {id, file, line(1-based), col(0-based)} →
 * {id, file, line} da definição (só quando única e dentro do repo), ou {id}.
 *
 * Uso: node ts_service.js <caminho-do-modulo-typescript> <raiz-do-repo>
 */
"use strict";

const path = require("path");
const fs = require("fs");
const readline = require("readline");

const ts = require(path.resolve(process.argv[2]));
const root = path.resolve(process.argv[3]);

const EXTS = new Set([".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"]);
const SKIP_DIRS = new Set(["node_modules", ".git", ".codegraph", "dist", "build",
  "coverage", ".next", "vendor"]);
const MAX_FILES = 4000;

function collect(dir, out) {
  if (out.length >= MAX_FILES) return;
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
  for (const e of entries) {
    if (out.length >= MAX_FILES) return;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (!SKIP_DIRS.has(e.name) && !e.name.startsWith(".")) collect(full, out);
    } else if (EXTS.has(path.extname(e.name).toLowerCase())) {
      out.push(full);
    }
  }
}

const files = [];
collect(root, files);

const snapshots = new Map();
function snapshot(fileName) {
  if (!snapshots.has(fileName)) {
    let text = "";
    try { text = fs.readFileSync(fileName, "utf8"); } catch {}
    snapshots.set(fileName, ts.ScriptSnapshot.fromString(text));
  }
  return snapshots.get(fileName);
}

const host = {
  getScriptFileNames: () => files,
  getScriptVersion: () => "1",
  getScriptSnapshot: (f) => (fs.existsSync(f) ? snapshot(f) : undefined),
  getCurrentDirectory: () => root,
  getCompilationSettings: () => ({
    allowJs: true, checkJs: false, noEmit: true,
    target: ts.ScriptTarget.ES2020,
    module: ts.ModuleKind.CommonJS,
    moduleResolution: ts.ModuleResolutionKind.NodeJs,
  }),
  getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
  fileExists: fs.existsSync,
  readFile: (f) => { try { return fs.readFileSync(f, "utf8"); } catch { return undefined; } },
};

const service = ts.createLanguageService(host, ts.createDocumentRegistry());

function toOffset(fileName, line, col) {
  const snap = snapshot(fileName);
  const text = snap.getText(0, snap.getLength());
  let idx = 0;
  for (let l = 1; l < line; l++) {
    idx = text.indexOf("\n", idx);
    if (idx === -1) return -1;
    idx += 1;
  }
  return idx + col;
}

function lineOf(fileName, offset) {
  const sf = service.getProgram().getSourceFile(fileName);
  if (!sf) return null;
  return sf.getLineAndCharacterOfPosition(offset).line + 1;
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (raw) => {
  let req = null;
  try { req = JSON.parse(raw); } catch { return; }
  const out = { id: req.id };
  try {
    const fileName = path.resolve(root, req.file);
    const offset = toOffset(fileName, req.line, req.col);
    if (offset >= 0) {
      const defs = (service.getDefinitionAtPosition(fileName, offset) || [])
        .filter((d) => !d.fileName.endsWith(".d.ts") &&
                       path.resolve(d.fileName).startsWith(root));
      const uniq = new Map();
      for (const d of defs) uniq.set(d.fileName + ":" + d.textSpan.start, d);
      if (uniq.size === 1) {
        const d = uniq.values().next().value;
        const line = lineOf(d.fileName, d.textSpan.start);
        if (line !== null) {
          out.file = path.relative(root, d.fileName).split(path.sep).join("/");
          out.line = line;
        }
      }
    }
  } catch {}
  process.stdout.write(JSON.stringify(out) + "\n");
});
rl.on("close", () => process.exit(0));
