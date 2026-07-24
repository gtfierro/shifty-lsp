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
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<http://example.org/shapes> a owl:Ontology .

ex:Person a rdfs:Class ;
    rdfs:label "Person" ;
    rdfs:comment "A human being." .

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
                    # auto-answer server->client requests (e.g. progress token creation)
                    if m.get("method") == "window/workDoneProgress/create" and "id" in m:
                        if not m.pop("_answered", None):
                            m["_answered"] = True
                            send(self.proc, {"jsonrpc": "2.0", "id": m["id"], "result": None})
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
            "capabilities": {"window": {"workDoneProgress": True}},
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

    # progress notifications should have been emitted
    begins = [m for m in reader.messages if m.get("method") == "$/progress"
              and m["params"]["value"].get("kind") == "begin"]
    assert begins, "expected $/progress begin notifications"
    print(f"OK: got {len(begins)} progress notification(s): "
          + begins[0]["params"]["value"]["title"])

    # a window/showMessage notification should accompany validation
    msg = reader.wait_for(
        lambda m: m.get("method") == "window/showMessage"
        and "shifty:" in m["params"]["message"],
        timeout=30,
    )
    print("OK: showMessage:", msg["params"]["message"])

    # textDocument/definition: cursor on the imported ontology IRI
    import_line = next(i for i, l in enumerate(DATA_TTL.splitlines()) if "owl:imports" in l)
    send(proc, {
        "jsonrpc": "2.0", "id": 5, "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": data_path.as_uri()},
            "position": {"line": import_line, "character": 20},
        },
    })
    resp = reader.wait_for(lambda m: m.get("id") == 5)
    got_uri = resp["result"]["uri"]
    want_uri = Path(os.path.realpath(tmp / "shapes.ttl")).as_uri()
    assert got_uri == want_uri, f"definition -> {got_uri}, want {want_uri}"
    print("OK: definition ->", got_uri)

    # textDocument/completion: typing "ex:P" offers terms from the imported ontology
    comp_path = tmp / "comp.ttl"
    comp_text = (
        "@prefix ex: <http://example.org/> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "<http://example.org/comp> a owl:Ontology ;\n"
        "    owl:imports <http://example.org/shapes> .\n"
        "ex:thing a ex:P"
    )
    comp_path.write_text(comp_text)
    send(proc, {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": comp_path.as_uri(), "languageId": "turtle",
                                    "version": 1, "text": comp_text}},
    })
    time.sleep(3)  # let validation run so the shapes cache is warm
    send(proc, {
        "jsonrpc": "2.0", "id": 6, "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": comp_path.as_uri()},
            "position": {"line": 4, "character": len("ex:thing a ex:P")},
        },
    })
    resp = reader.wait_for(lambda m: m.get("id") == 6)
    labels = {i["label"] for i in resp["result"]["items"]}
    print("OK: completion items:", sorted(labels))
    assert "ex:Person" in labels and "ex:PersonShape" in labels and "ex:name" in labels

    # textDocument/hover on ex:Person in data.ttl (defined in the imported ontology)
    person_line = next(i for i, l in enumerate(DATA_TTL.splitlines()) if "ex:Alice" in l)
    send(proc, {
        "jsonrpc": "2.0", "id": 7, "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": data_path.as_uri()},
            "position": {"line": person_line, "character": DATA_TTL.splitlines()[person_line].index("ex:Person") + 3},
        },
    })
    resp = reader.wait_for(lambda m: m.get("id") == 7)
    hover_md = resp["result"]["contents"]["value"]
    print("OK: hover ->", hover_md.replace("\n", " "))
    assert "Person" in hover_md and "A human being." in hover_md

    # workspace/executeCommand: refreshEnvironment
    send(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "workspace/executeCommand",
        "params": {"command": "shifty-lsp.refreshEnvironment", "arguments": []},
    })
    reader.wait_for(lambda m: m.get("id") == 2)
    print("OK: refreshEnvironment command")

    # workspace/executeCommand: validateFile returns conforms=false for data.ttl
    send(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "workspace/executeCommand",
        "params": {"command": "shifty-lsp.validateFile",
                   "arguments": [{"uri": data_path.as_uri()}]},
    })
    resp = reader.wait_for(lambda m: m.get("id") == 3)
    print("OK: validateFile command, result:", resp["result"])
    assert resp["result"] is False, "expected data.ttl to not conform"

    # validateFile on the conforming shapes.ttl returns true
    send(proc, {
        "jsonrpc": "2.0", "id": 4, "method": "workspace/executeCommand",
        "params": {"command": "shifty-lsp.validateFile",
                   "arguments": [(tmp / "shapes.ttl").as_uri()]},
    })
    resp = reader.wait_for(lambda m: m.get("id") == 4)
    print("OK: validateFile(shapes.ttl), result:", resp["result"])
    assert resp["result"] is True, "expected shapes.ttl to conform"

    send(proc, {"jsonrpc": "2.0", "id": 99, "method": "shutdown", "params": None})
    reader.wait_for(lambda m: m.get("id") == 99)
    send(proc, {"jsonrpc": "2.0", "method": "exit"})
    proc.wait(timeout=10)


if __name__ == "__main__":
    main()
