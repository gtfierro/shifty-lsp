"""End-to-end smoke test: start shifty-lsp over stdio, open a Turtle file
that imports a shapes ontology from the same repo and violates it, and check
that SHACL violations arrive as diagnostics."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SHAPES_TTL = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<http://example.org/shapes> a owl:Ontology .

ex:PersonShape a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [
        sh:path ex:name ;
        sh:minCount 1 ;
        sh:datatype xsd:string ;
    ] .
"""

DATA_TTL = """\
@prefix ex: <http://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

<http://example.org/data> a owl:Ontology ;
    owl:imports <http://example.org/shapes> .

ex:Alice a ex:Person ; ex:name "Alice" .
ex:Bob a ex:Person .
"""


def send(proc, msg):
    body = json.dumps(msg).encode()
    proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    proc.stdin.flush()


class Reader:
    def __init__(self, proc):
        self.proc = proc
        self.messages = []
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            headers = {}
            line = self.proc.stdout.readline()
            if not line:
                return
            while line.strip():
                k, v = line.decode().split(":", 1)
                headers[k.strip().lower()] = v.strip()
                line = self.proc.stdout.readline()
            body = self.proc.stdout.read(int(headers["content-length"]))
            with self.lock:
                self.messages.append(json.loads(body))

    def wait_for(self, pred, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for m in self.messages:
                    if pred(m):
                        return m
            time.sleep(0.1)
        raise TimeoutError("timed out waiting for LSP message")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="shifty-lsp-test-"))
    (tmp / "shapes.ttl").write_text(SHAPES_TTL)
    data_path = tmp / "data.ttl"
    data_path.write_text(DATA_TTL)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)

    env = dict(os.environ, SHIFTY_LSP_LOG="DEBUG")
    proc = subprocess.Popen(
        [sys.executable, "-m", "shifty_lsp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(tmp / "server.log", "w"),
        env=env,
        cwd=Path(__file__).parent,
    )
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
        "params": {
            "textDocument": {
                "uri": data_path.as_uri(),
                "languageId": "turtle",
                "version": 1,
                "text": DATA_TTL,
            }
        },
    })

    msg = reader.wait_for(
        lambda m: m.get("method") == "textDocument/publishDiagnostics"
        and m["params"]["diagnostics"],
        timeout=120,
    )
    diags = msg["params"]["diagnostics"]
    print(json.dumps(diags, indent=2))
    assert any(d["severity"] == 1 for d in diags), "expected an error diagnostic"
    assert any("name" in d["message"] or "Person" in d["message"] for d in diags)
    print(f"\nOK: got {len(diags)} diagnostic(s)")

    send(proc, {"jsonrpc": "2.0", "id": 99, "method": "shutdown", "params": None})
    reader.wait_for(lambda m: m.get("id") == 99)
    send(proc, {"jsonrpc": "2.0", "method": "exit"})
    proc.wait(timeout=10)


if __name__ == "__main__":
    main()
