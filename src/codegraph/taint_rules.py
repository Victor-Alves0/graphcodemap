"""Regras de taint: sources (entrada não-confiável), sinks (operações
perigosas) e sanitizers (limpam o dado). Casadas pelo ÚLTIMO segmento do nome
da chamada (é como o call graph resolve nomes), portanto heurísticas por
convenção — ponto de partida honesto, ajustável por repositório.

Override: um arquivo `.codegraph/taint.json` na raiz do repo, com listas que
são UNIDAS às defaults (e um bloco opcional `remove` para tirar entradas):

    {
      "sources":   ["my_input"],
      "sinks":     ["run_shell"],
      "sanitizers":["my_escape"],
      "remove":    {"sinks": ["call", "run"]}
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Entrada não-confiável: o RETORNO destas chamadas nasce tainted.
_SOURCES = {
    # Python
    "input", "raw_input", "getenv", "get_json", "recv", "recvfrom",
    # comuns a web frameworks (nomes de método frequentes). ATENÇÃO: o
    # casamento é case-sensitive e usa o nome COMO ESCRITO no código — havia
    # aqui "getparameter" em minúsculas, que nunca casou com o `getParameter`
    # real do servlet: regra morta por anos.
    "getParameter", "getParameterValues", "getHeader", "getQueryString",
    "getInputStream", "getReader", "getRequestURI", "getRequestURL",
    # JS/Node
    "prompt",
}

# Operações perigosas: se um dado tainted alcança um argumento aqui → achado.
_SINKS = {
    # execução de código / shell (`exec` está em `_BARE_SINKS`: ver lá)
    "eval", "system", "popen", "Popen", "spawn", "spawnSync",
    "execSync", "execFileSync", "check_output", "check_call", "compile",
    "__import__",
    # SQL
    "execute", "executemany", "executescript", "executeQuery", "query",
    # (des)serialização perigosa / templates
    "loads", "load", "render_template_string", "literal_eval",
    # JS DOM/eval-like
    "innerHTML", "insertAdjacentHTML", "writeln", "setTimeout", "Function",
}

# Limpam o dado: o RETORNO de uma chamada a estes é considerado seguro.
_SANITIZERS = {
    "escape", "quote", "quote_plus", "sanitize", "clean", "escape_string",
    "secure_filename", "int", "float", "bool", "escapeHtml", "encodeURIComponent",
    "parseInt", "parseFloat",
}


# Sinks que só valem SEM receptor, isto é, a função global.
#
# `open(caminho)` é path traversal; `Image.open(arquivo_enviado)` é abrir uma
# imagem, e acusá-lo seria mentir. Pelo nome nu os dois são "open" — foi
# exatamente o falso positivo que apareceu quando o `open` entrou no catálogo.
# Um nome aqui NÃO casa quando a chamada tem receptor; se alguma forma
# qualificada também for perigosa (`io.open`), ela entra em `sinks` explícita.
#
# `exec` está aqui pelo mesmo motivo, descoberto em código real: o Mongoose
# fecha toda consulta com `Todo.find({}).sort('-updated_at').exec(cb)`, e pelo
# nome nu isso era "execução de comando".
#
# Mas o default é só o default: uma linguagem que declare o mesmo nome como
# sink NORMAL vence (ver `catalog_for`). Java precisa disso — a forma real é
# `Runtime r = Runtime.getRuntime(); … r.exec(cmd)`, com o receptor numa
# variável local, que nenhuma regra qualificada consegue nomear. Restringir
# `exec` a chamadas sem receptor derrubou o recall de cmdi do OWASP Benchmark
# de 26% para 3%; a régua por linguagem existe por causa dessa medição.
_BARE_SINKS = {"open", "exec"}

# Sinks em que SÓ O PRIMEIRO ARGUMENTO é perigoso.
#
# `cur.execute(q, params)` com `q` literal e placeholders é a forma SEGURA de
# consultar — o dado do usuário vai em `params` justamente para não ser
# interpretado como SQL. Sujeira chegando no argumento 1 não é injeção; é o
# mecanismo que a impede. Sem esta distinção o motor acusava quem acertou,
# medido num app real (dvpwa `dao/review.py`) e em 10 casos do Benchmark.
#
# Mesma ideia para template: em `render_template_string(tpl, **ctx)` o sink é o
# TEMPLATE; passar dado do usuário como contexto é o uso correto.
#
# Os modelos do CodeQL trazem esse índice em cada linha (`Argument[0]`); nós o
# descartamos na importação porque o motor ainda não o usava. Aqui está o
# começo do uso — por enquanto só onde o ganho foi medido.
_ARG0_ONLY = {
    # SQL: o argumento 0 é a consulta, o resto é ligação de parâmetros
    "execute", "executemany", "executescript", "executeQuery", "executeUpdate",
    "executeLargeUpdate", "addBatch", "batchUpdate", "queryForObject",
    "queryForList", "queryForMap", "queryForRowSet", "queryForInt",
    "queryForLong", "prepareStatement", "prepareCall", "query",
    # template
    "render_template_string",
}


@dataclass(frozen=True)
class TaintRules:
    sources: frozenset[str]
    sinks: frozenset[str]
    sanitizers: frozenset[str]
    bare_sinks: frozenset[str] = frozenset()
    arg0_only: frozenset[str] = frozenset()

    def is_sink(self, callee: str, qualified: str | None,
                arg_index: int | None = None) -> bool:
        """A chamada é um sink? Casa pelo `receptor.método` OU pelo nome nu.

        Um nome em `bare_sinks` só casa SEM receptor: é o que separa
        `open(caminho)` de `Image.open(arquivo)`.

        Um nome em `arg0_only` só casa no PRIMEIRO argumento: em
        `execute(consulta, params)` o dado do usuário em `params` é a defesa,
        não a falha. `arg_index` negativo é kwarg — posição desconhecida, então
        continua valendo, que é o lado seguro."""
        if callee in self.bare_sinks:
            casou = qualified is None
        else:
            casou = ((qualified is not None and qualified in self.sinks)
                     or callee in self.sinks)
        if casou and arg_index is not None and arg_index > 0 \
                and callee in self.arg0_only:
            return False
        return casou


def default_rules() -> TaintRules:
    return TaintRules(frozenset(_SOURCES), frozenset(_SINKS), frozenset(_SANITIZERS),
                      frozenset(_BARE_SINKS), frozenset(_ARG0_ONLY))


# Suplemento CURADO À MÃO, por linguagem. Complementa o catálogo gerado com
# APIs perigosas que as regras do OpenTaint não cobriam. Critério de inclusão:
# o nome tem que ser DISTINTIVO o bastante para casar pelo último segmento sem
# disparar em código comum (`queryForObject` sim; `println` não).
#
# Lacuna conhecida e declarada: os sinks de XSS em Java são `println`/`print`/
# `write` num PrintWriter de resposta, e de trust-boundary são `setAttribute`.
# São genéricos demais para nome-nu — ficam de fora até existir casamento
# QUALIFICADO por receptor/pacote. Enquanto isso o recall de XSS é ZERO, e é
# melhor dizer isso do que fingir cobertura.
_CURATED: dict[str, dict[str, set[str]]] = {
    "java": {
        "sinks": {
            # JDBC / Spring JdbcTemplate
            "executeUpdate", "executeLargeUpdate", "addBatch", "batchUpdate",
            "queryForObject", "queryForList", "queryForMap", "queryForRowSet",
            "queryForInt", "queryForLong", "createStatement",
            # processo. `exec` NORMAL (não bare-only): em Java o receptor é uma
            # variável local (`Runtime r = …; r.exec(cmd)`), impossível de
            # nomear por regra qualificada. Declarar aqui reverte o default
            # bare-only para repositórios Java — ver `_BARE_SINKS`.
            "ProcessBuilder", "exec",
            # XPath / expressão
            "evaluate", "compileExpression", "getValue", "setValue",
            # XSS: QUALIFICADOS (receptor.método). `println` puro pegaria todo
            # System.out.println do mundo; `getWriter.println` só pega escrita
            # na resposta HTTP, que é o sink de verdade.
            "getWriter.println", "getWriter.print", "getWriter.write",
            "getWriter.printf", "getWriter.format", "getWriter.append",
            "getOutputStream.write", "getOutputStream.print",
            # trust boundary: só na sessão, não em qualquer objeto
            "getSession.setAttribute", "getSession.putValue",
        },
        # Escape de saída. Medido no OWASP Benchmark: sanitizador não modelado
        # respondia por 15% de TODOS os falsos positivos, e três nomes
        # explicavam 46 dos 54 casos — `encodeForHTML` (ESAPI, 28),
        # `htmlEscape` (Spring, 13) e `escapeHtml` (Commons Lang, 5).
        # Não modelar o escape é acusar exatamente quem se defendeu.
        "sanitizers": {
            # OWASP ESAPI
            "encodeForHTML", "encodeForHTMLAttribute", "encodeForJavaScript",
            "encodeForCSS", "encodeForURL", "encodeForXML",
            "encodeForXMLAttribute", "encodeForXPath", "encodeForLDAP",
            "encodeForDN", "encodeForSQL", "encodeForOS", "encodeForVBScript",
            # Spring
            "htmlEscape", "javaScriptEscape",
            # Apache Commons Lang / Text
            "escapeHtml3", "escapeHtml4", "escapeXml", "escapeXml10",
            "escapeXml11", "escapeEcmaScript", "escapeJava",
            "escapeJavaScript", "escapeSql",
            # OWASP Java Encoder
            "forHtml", "forHtmlContent", "forHtmlAttribute", "forJavaScript",
            "forUri", "forUriComponent", "forXml", "forCssString",
            "forXmlContent", "forXmlAttribute",
        },
    },
    # --- PHP -----------------------------------------------------------------
    # O CodeQL não publica modelos MaD para PHP e o OpenTaint também não cobria,
    # então este bloco é levantado à mão a partir do que aparece em app PHP
    # vulnerável real (DVWA). As fontes são as superglobais, tratadas em
    # `dataflow._BARE_SOURCE_NAMES` porque não têm receptor.
    "php": {
        "sinks": {
            # SQL — nomes de função, não método: PHP expõe tudo global
            "mysqli_query", "mysqli_multi_query", "mysqli_real_query",
            "mysql_query", "pg_query", "pg_send_query", "sqlite_query",
            "sqlsrv_query", "oci_parse",
            # comando
            "shell_exec", "passthru", "proc_open", "pcntl_exec",
            # execução de código
            "assert", "create_function", "call_user_func", "call_user_func_array",
            # arquivo → path traversal / LFI
            "file_get_contents", "file_put_contents", "readfile", "fopen",
            "unlink", "move_uploaded_file", "copy", "rename", "scandir",
            "opendir", "glob",
            # desserialização
            "unserialize",
            # LDAP / XPath
            "ldap_search", "ldap_bind", "xpath",
            # cabeçalho controlado pelo usuário → open redirect / splitting
            "header",
        },
        "sanitizers": {
            "htmlspecialchars", "htmlentities", "strip_tags", "addslashes",
            "mysqli_real_escape_string", "mysql_real_escape_string",
            "pg_escape_string", "pg_escape_literal", "intval", "floatval",
            "urlencode", "rawurlencode", "basename", "escapeshellarg",
            "escapeshellcmd", "filter_var", "preg_quote",
        },
    },
    # --- Node/Express/Koa/Fastify --------------------------------------------
    # Quase tudo aqui é QUALIFICADO (`receptor.método`). O motivo é o mesmo do
    # Java: `send`, `write`, `end`, `open`, `redirect` são nomes comuns demais
    # para casar nus — `emitter.send` e `stream.write` não são vulnerabilidade.
    # `res.send` é. Sem o qualificado, ou o recall de XSS/redirect em Node é
    # zero, ou o ruído torna o resultado inútil; não há meio-termo por nome nu.
    "javascript": {
        "sinks": {
            # saída HTTP → XSS refletido
            "res.send", "res.write", "res.end", "res.json", "res.jsonp",
            "response.send", "response.write", "response.end", "response.json",
            "reply.send", "reply.type",                      # Fastify
            "ctx.body",                                      # Koa (atribuição)
            # redirecionamento controlado pelo usuário → open redirect
            "res.redirect", "response.redirect", "ctx.redirect",
            # arquivo servido/lido por caminho do usuário → path traversal
            "res.sendFile", "res.download", "response.sendFile",
            "fs.readFile", "fs.readFileSync", "fs.writeFile", "fs.writeFileSync",
            "fs.appendFile", "fs.appendFileSync", "fs.createReadStream",
            "fs.createWriteStream", "fs.unlink", "fs.unlinkSync",
            "fs.readdir", "fs.readdirSync", "fs.open", "fs.openSync",
            "fs.rename", "fs.renameSync", "fs.rmdir", "fs.copyFile",
            # processo. `exec` nu (o idioma do `const {exec} = require(...)`)
            # vem de _BARE_SINKS; aqui ficam os receptores reais do módulo.
            "child_process.exec", "childProcess.exec", "cp.exec",
            "execFile", "execFileSync", "fork",
            # execução de código fora do eval: o módulo `vm`
            "runInNewContext", "runInThisContext", "runInContext",
            # desserialização insegura (node-serialize e afins)
            "unserialize", "deserialize",
            # XXE: libxmljs com entidades ligadas
            "parseXmlString", "parseXml",
            # DOM
            "document.write", "document.writeln",
        },
        "sanitizers": {
            # `path.basename`/`path.resolve` não sanitizam de verdade e ficam
            # de fora de propósito: dizer que sanitizam esconderia traversal.
            "escapeHTML", "encodeURI", "encodeURIComponent", "escapeExpression",
            "validator.escape", "xss", "sanitizeHtml", "DOMPurify.sanitize",
        },
    },
    # --- Python/Django/Flask -------------------------------------------------
    "python": {
        "sinks": {
            # processo (o nome nu `system`/`popen` já está no default)
            "subprocess.run", "subprocess.call", "subprocess.check_output",
            "subprocess.check_call", "subprocess.Popen", "os.system", "os.popen",
            # arquivo por caminho do usuário → path traversal. O `open` nu está
            # em `_BARE_SINKS`, não aqui: com receptor ele é outra função
            # (`Image.open` abre imagem, não caminho).
            "io.open", "codecs.open", "os.open",
            "os.remove", "os.unlink", "os.rmdir", "os.rename",
            "shutil.rmtree", "shutil.copy", "shutil.copyfile", "shutil.move",
            "send_file", "send_from_directory",
            # XSS em Django: `mark_safe` DESLIGA o escape do template, que é o
            # ponto. `HttpResponse` ficou de fora depois de medir — toda view
            # devolve uma, e o conteúdo em geral já passou por template escapado;
            # como sink ele acusava código correto.
            "mark_safe",
            # redirecionamento controlado pelo usuário
            "redirect",
            # XML → XXE (qualificado: `parse` nu casaria com tudo)
            "etree.fromstring", "etree.parse", "minidom.parseString",
            "sax.parseString", "pulldom.parseString",
            # SSRF: só as formas em que a URL é o PRIMEIRO argumento. Medi
            # `requests.request("PATCH", url, data=payload)` acusando dado do
            # usuário indo no CORPO para uma URL constante — isso não é SSRF, e
            # o modelo de sink não distingue argumento, então a forma sai.
            "urlopen", "requests.get",
            # desserialização (o nu `load`/`loads` já está no default)
            "yaml.unsafe_load", "marshal.loads", "dill.loads",
        },
        "sanitizers": {
            "escape_html", "conditional_escape", "urlencode", "shlex_quote",
            "os.path.basename", "werkzeug.utils.secure_filename",
        },
    },
}
# TypeScript e TSX são o mesmo ecossistema de execução que JavaScript: as APIs
# perigosas são idênticas, e manter listas separadas só criaria divergência.
_CURATED["typescript"] = _CURATED["tsx"] = _CURATED["javascript"]


def _curated(lang: str, bucket: str) -> set[str]:
    return set(_CURATED.get(lang, {}).get(bucket, ()))


def catalog_for(languages) -> TaintRules:
    """Regras default + o catálogo por FRAMEWORK das linguagens presentes.

    O catálogo (`taint_catalog.py`, semeado das regras MIT do OpenTaint) é o que
    separa "funciona em código de exemplo" de "funciona no repo do cliente":
    sem ele, `exec.Command` do Go ou `FileOutputStream` do Java não são sinks
    conhecidos e a vulnerabilidade passa batido.

    Aplicado só às linguagens que o repo REALMENTE tem — carregar os 124 sinks
    de Java num repo Go só aumentaria a chance de colisão de nome à toa."""
    src, snk, san = set(_SOURCES), set(_SINKS), set(_SANITIZERS)
    bare = set(_BARE_SINKS)
    try:
        from .taint_catalog import CATALOG
    except ImportError:                       # catálogo é opcional
        return TaintRules(frozenset(src), frozenset(snk), frozenset(san),
                          frozenset(bare), frozenset(_ARG0_ONLY))
    try:
        from .taint_catalog_codeql import CATALOG_CODEQL
    except ImportError:                       # também opcional
        CATALOG_CODEQL = {}
    for lang in languages or ():
        for cat in (CATALOG, CATALOG_CODEQL):
            b = cat.get(lang)
            if not b:
                continue
            src |= set(b.get("sources", ()))
            snk |= set(b.get("sinks", ()))
            san |= set(b.get("sanitizers", ()))
    for lang in languages or ():
        src |= _curated(lang, "sources")
        snk |= _curated(lang, "sinks")
        san |= _curated(lang, "sanitizers")
        bare |= _curated(lang, "bare_sinks")
    # Uma linguagem presente que declare o nome como sink NORMAL vence a
    # restrição bare-only. Empate resolvido a favor do RECALL: perder uma
    # execução de comando é pior que acusar um homônimo.
    bare -= snk
    return TaintRules(frozenset(src), frozenset(snk), frozenset(san),
                      frozenset(bare), frozenset(_ARG0_ONLY))


def load_rules(root: Path, languages=None) -> TaintRules:
    base = catalog_for(languages)
    src, snk, san = set(base.sources), set(base.sinks), set(base.sanitizers)
    bare = set(base.bare_sinks)
    cfg = root / ".codegraph" / "taint.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        src |= set(data.get("sources", []))
        snk |= set(data.get("sinks", []))
        san |= set(data.get("sanitizers", []))
        bare |= set(data.get("bare_sinks", []))
        rem = data.get("remove", {}) or {}
        src -= set(rem.get("sources", []))
        snk -= set(rem.get("sinks", []))
        san -= set(rem.get("sanitizers", []))
        # `remove.sinks` também tira de `bare_sinks`: para quem escreve o
        # arquivo existe UMA lista de sinks, e a distinção é detalhe interno.
        bare -= set(rem.get("sinks", [])) | set(rem.get("bare_sinks", []))
    return TaintRules(frozenset(src), frozenset(snk), frozenset(san),
                      frozenset(bare), frozenset(base.arg0_only))
