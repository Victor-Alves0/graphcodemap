"""Catálogo de sources/sinks/sanitizers por linguagem (GERADO).

NÃO EDITE À MÃO — gerado por `scripts/import_taint_catalog.py`.

Semeado a partir das regras do OpenTaint (github.com/seqra/opentaint,
`rules/` sob licença **MIT**, Copyright 2026 Seqra Team), das quais extraímos
apenas os NOMES DE API e sua categoria — não a DSL de regras do Semgrep.

O motor casa chamadas pelo ÚLTIMO SEGMENTO do nome (`exec.Command` → `Command`),
então o catálogo é um conjunto de nomes por linguagem. É intencionalmente menos
expressivo que uma regra Semgrep (sem tipos, sem contexto de import) — a
contrapartida é que roda no nosso motor incremental e sempre fresco.

O usuário continua podendo ajustar tudo em `.codegraph/taint.json`.
"""

from __future__ import annotations


CATALOG: dict[str, dict[str, frozenset[str]]] = {
    "go": {
        "sources": frozenset({
            "Cookie", "Cookies", "FormFile", "FormValue",
            "GetBool", "GetFiles", "GetFloat", "GetInt",
            "GetInt16", "GetInt32", "GetInt64", "GetInt8",
            "GetSession", "GetString", "GetStrings", "GetUint16",
            "GetUint32", "GetUint64", "GetUint8", "Getenv",
            "LookupEnv", "MultipartReader", "ParseForm", "PostFormValue",
            "Referer", "UserAgent",
        }),
        "sinks": frozenset({
            "Chdir", "Chmod", "Chown", "Chtimes",
            "CombinedOutput", "Command", "CommandContext", "CreateTemp",
            "DirFS", "Exec", "ExecContext", "ForkExec",
            "Fprint", "Fprintf", "Fprintln", "Lchown",
            "LookPath", "Lstat", "MkdirAll", "MkdirTemp",
            "NewEncoder", "NewRequest", "NewRequestWithContext", "OpenFile",
            "ParseFiles", "ParseGlob", "PostForm", "Prepare",
            "PrepareContext", "QueryContext", "QueryRow", "QueryRowContext",
            "ReadDir", "ReadFile", "Readlink", "Remove",
            "RemoveAll", "Rename", "ServeFile", "ServeJSON",
            "StartProcess", "Symlink", "TempDir", "TempFile",
            "Truncate", "WriteFile", "WriteString",
        }),
    },
    "java": {
        "sources": frozenset({
            "getCookie", "getCurrentInstance", "getExternalContext", "getRequestCookieMap",
            "getRequestHeaderMap", "getRequestParameterMap", "getSubmittedFileName", "parseRequest",
        }),
        "sinks": frozenset({
            "ClassPathResource", "Cookie", "FileDataSource", "FileInputStream",
            "FileOutputStream", "FileReader", "FileSystemResource", "FileWriter",
            "GetMethod", "HttpGet", "HttpHeaders", "LdapName",
            "PreparedBatch", "PreparedStatementCreatorFactory", "RandomAccessFile", "RequestMapping",
            "ResponseEntity", "Script", "StreamSource", "Update",
            "accepted", "addHeader", "badRequest", "cleanDirectory",
            "contentType", "copyDirectory", "copyFile", "copyFileToDirectory",
            "createDirectories", "createDirectory", "createFile", "createLink",
            "createNativeQuery", "createNewFile", "createQuery", "createSQLQuery",
            "createScript", "createSymbolicLink", "createTempDirectory", "createTempFile",
            "createUpdate", "created", "delete", "deleteDirectory",
            "deleteIfExists", "deleteOnExit", "deleteQuietly", "execute",
            "executeQuery", "executeStatement", "exists", "forceDelete",
            "forceDeleteOnExit", "forceMkDir", "forceMkDirParent", "forward",
            "getOrDefault", "getOutputStream", "getRequestDispatcher", "getWriter",
            "include", "internalServerError", "isDirectory", "isFile",
            "iterateFiles", "iterateFilesAndDirs", "listFiles", "listFilesAndDirs",
            "lookup", "mkdirs", "moveFile", "moveToDirectory",
            "newBufferedReader", "newBufferedWriter", "newByteChannel", "newDirectoryStream",
            "newInputStream", "newOutputStream", "newPreparedStatementCreator", "newQuery",
            "notExists", "openInputStream", "openOutputStream", "populate",
            "prepare", "prepareBatch", "prepareCall", "prepareStatement",
            "preparedQuery", "readAllBytes", "readAllLines", "readFileToByteArray",
            "readFileToString", "readLines", "readSymbolicLink", "search",
            "select", "sendError", "sendRedirect", "setContentType",
            "setDescription", "setDisposition", "setExecutable", "setFilter",
            "setGrouping", "setHeader", "setLastModifiedTime", "setOwner",
            "setPosixFilePermissions", "setQueryString", "setReadable", "setResult",
            "setSubject", "setValue", "setWritable", "sqlRestriction",
            "status", "streamFiles", "unprocessableContent", "unprocessableEntity",
            "walkFileTree", "writeByteArrayToFile", "writeLines", "writeStringToFile",
        }),
    },
}
