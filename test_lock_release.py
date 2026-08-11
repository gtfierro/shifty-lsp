"""Regression test: the server must not hold the store lock while idle.

An open writable OntoEnv connection takes an exclusive lock on `.ontoenv/`.
While the server holds it, another process's writable `connect()` fails
outright and a *read-only* `connect()` blocks indefinitely — so no development
script can touch the environment while a file is open in the editor.

Opens a document, waits for validation, and checks that another process can
both read and write the environment shortly afterwards; then edits the document
again and checks the server still works after having released the lock.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from test_e2e import Reader, send

SHAPES_TTL = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<http://example.org/shapes> a owl:Ontology .

ex:Person a rdfs:Class ; rdfs:label "Person" .

ex:PersonShape a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [ sh:path ex:name ; sh:minCount 1 ] .
"""

DATA_TTL = """\
@prefix ex: <http://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

<http://example.org/data> a owl:Ontology ;
    owl:imports <http://example.org/shapes> .

ex:Bob a ex:Person .
"""

# how long to allow for the idle release (server default is 5s)
RELEASE_TIMEOUT = 25.0


def other_process(tmp: Path, read_only: bool, timeout: float = 10.0) -> str:
    """Connect to the environment from a separate process, as a dev script would."""
    code = f"""
        from ontoenv import OntoEnv
        env = OntoEnv.connect({str(tmp)!r}, read_only={read_only})
        names = env.get_ontology_names()
        if not {read_only}:
            env.update()
        env.close()
        print("ok", len(names))
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"BLOCKED (>{timeout}s)"
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return out[-1] if out else f"rc={proc.returncode}"


def wait_until_free(tmp: Path, deadline: float) -> str:
    """Poll until another process can open the environment writable."""
    while time.time() < deadline:
        result = other_process(tmp, read_only=False, timeout=5.0)
        if result.startswith("ok"):
            return result
        time.sleep(1.0)
    return result


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="shifty-lsp-lock-"))
    (tmp / "shapes.ttl").write_text(SHAPES_TTL)
    data_path = tmp / "data.ttl"
    data_path.write_text(DATA_TTL)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "shifty_lsp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(tmp / "server.log", "w"),
        env=dict(os.environ, SHIFTY_LSP_LOG="INFO"),
        cwd=Path(__file__).parent,
    )
    reader = Reader(proc)
    send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"processId": None, "rootUri": tmp.as_uri(),
                   "workspaceFolders": [{"uri": tmp.as_uri(), "name": "test"}],
                   "capabilities": {}},
    })
    reader.wait_for(lambda m: m.get("id") == 1)
    send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    send(proc, {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": data_path.as_uri(), "languageId": "turtle",
                                    "version": 1, "text": DATA_TTL}},
    })

    def wait_for_diagnostics(timeout=120):
        return reader.wait_for(
            lambda m: m.get("method") == "textDocument/publishDiagnostics"
            and m["params"]["uri"] == data_path.as_uri()
            and m["params"]["diagnostics"],
            timeout=timeout,
        )

    wait_for_diagnostics()
    print("OK: validated, diagnostics published")

    # the document stays open, as it would in the editor
    result = wait_until_free(tmp, time.time() + RELEASE_TIMEOUT)
    assert result.startswith("ok"), f"writable connect from another process: {result}"
    print("OK: another process can open the environment writable while the file is open")

    result = other_process(tmp, read_only=True, timeout=10.0)
    assert result.startswith("ok"), f"read-only connect from another process: {result}"
    print("OK: another process can open it read-only (this used to hang forever)")

    # ...and the server still works after releasing: edit the buffer, expect
    # fresh diagnostics, which means it reopened the environment on demand
    reader.messages.clear()
    send(proc, {
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": data_path.as_uri(), "version": 2},
                   "contentChanges": [{"text": DATA_TTL + "\nex:Carol a ex:Person .\n"}]},
    })
    msg = wait_for_diagnostics()
    print(f"OK: reopened on demand, {len(msg['params']['diagnostics'])} diagnostic(s) after edit")

    send(proc, {"jsonrpc": "2.0", "id": 99, "method": "shutdown", "params": None})
    reader.wait_for(lambda m: m.get("id") == 99)
    send(proc, {"jsonrpc": "2.0", "method": "exit"})
    proc.wait(timeout=30)

    log = (tmp / "server.log").read_text()
    assert "releasing the ontoenv store lock" in log, "server never released the lock"
    print("OK: release is logged")


if __name__ == "__main__":
    main()
