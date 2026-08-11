"""Regression test: an unresolvable owl:imports must not poison the catalog.

ontoenv writes a `.ontoenv/catalog.pending` recovery marker while a mutation is
in flight and removes it when the new catalog is published. An `add()` that
fails part-way (unreachable host, 404, unparseable file) leaves the marker
behind, so the *next* `connect()` raises CatalogRecoveryError and has to rescan
every stored graph. The server used to hit that on every edit of a file with a
broken import.

Opens such a file, shuts the server down cleanly, and checks that the
environment can be reopened without recovery.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from test_e2e import Reader, send

DATA_TTL = """\
@prefix ex: <http://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

<http://example.org/data> a owl:Ontology ;
    owl:imports <http://nonexistent.invalid/does-not-exist.ttl> .

ex:Alice a ex:Person .
"""


def start_server(tmp: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "shifty_lsp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(tmp / "server.log", "w"),
        env=dict(os.environ, SHIFTY_LSP_LOG="DEBUG"),
        cwd=Path(__file__).parent,
    )


def open_document(proc: subprocess.Popen, tmp: Path, data_path: Path) -> Reader:
    reader = Reader(proc)
    send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "processId": None,
            "rootUri": tmp.as_uri(),
            "workspaceFolders": [{"uri": tmp.as_uri(), "name": "test"}],
            "capabilities": {},
        },
    })
    reader.wait_for(lambda m: m.get("id") == 1)
    send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    send(proc, {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": data_path.as_uri(), "languageId": "turtle",
                                    "version": 1, "text": DATA_TTL}},
    })

    msg = reader.wait_for(
        lambda m: m.get("method") == "textDocument/publishDiagnostics"
        and any(d["source"] == "ontoenv" for d in m["params"]["diagnostics"]),
        timeout=120,
    )
    diags = [d["message"] for d in msg["params"]["diagnostics"]]
    print("OK: unresolved import reported:", json.dumps(diags))
    return reader


def run_server(tmp: Path, data_path: Path) -> None:
    """Open the document, wait for the failed import, then shut down politely."""
    proc = start_server(tmp)
    reader = open_document(proc, tmp, data_path)
    send(proc, {"jsonrpc": "2.0", "id": 99, "method": "shutdown", "params": None})
    reader.wait_for(lambda m: m.get("id") == 99)
    send(proc, {"jsonrpc": "2.0", "method": "exit"})
    proc.wait(timeout=30)


def assert_clean(tmp: Path, when: str) -> None:
    marker = tmp / ".ontoenv" / "catalog.pending"
    assert not marker.exists(), f"interrupted-mutation marker left behind {when}: {marker}"
    print(f"OK: no catalog.pending marker {when}")

    from ontoenv import CatalogRecoveryError, OntoEnv

    try:
        env = OntoEnv.connect(str(tmp))
    except CatalogRecoveryError as e:
        raise AssertionError(f"environment needs recovery {when}: {e}")
    env.close()
    print(f"OK: environment reopens cleanly {when}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="shifty-lsp-recovery-"))
    data_path = tmp / "data.ttl"
    data_path.write_text(DATA_TTL)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)

    run_server(tmp, data_path)
    assert_clean(tmp, "after shutdown")

    # a second server run over the same environment must not have to recover
    run_server(tmp, data_path)
    log = (tmp / "server.log").read_text()
    assert "needs recovery" not in log, "second run had to recover the catalog"
    print("OK: second run did not have to recover the catalog")

    # Editors terminate language servers rather than asking them to exit: a
    # SIGTERM in the middle of the initial scan must still close the store.
    proc = start_server(tmp)
    Reader(proc)
    send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"processId": None, "rootUri": tmp.as_uri(),
                   "workspaceFolders": [{"uri": tmp.as_uri(), "name": "test"}],
                   "capabilities": {}},
    })
    send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    send(proc, {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": data_path.as_uri(), "languageId": "turtle",
                                    "version": 1, "text": DATA_TTL}},
    })
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("server did not exit on SIGTERM")
    assert code == 0, f"SIGTERM exit code {code}"
    print("OK: exited on SIGTERM")
    assert_clean(tmp, "after SIGTERM")


if __name__ == "__main__":
    main()
