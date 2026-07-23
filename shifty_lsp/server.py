"""shifty-lsp: an LSP server for Turtle/RDF ontology files.

Features:
  - Uses ontoenv to manage an ontology environment rooted at the enclosing
    Git repository (or workspace folder). All .ttl files in the repo are
    discovered and owl:imports are resolved/downloaded automatically.
  - On open/change/save, resolves the document's owl:imports, runs SHACL-AF
    inference + SHACL validation with shifty, using the imported ontologies
    as the shapes graph, and publishes violations/warnings/infos as
    diagnostics.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from lsprotocol import types
from pygls.lsp.server import LanguageServer

import rdflib
from rdflib import RDF, OWL, URIRef

import shifty
from ontoenv import OntoEnv

from shifty_lsp import __version__

log = logging.getLogger("shifty-lsp")

SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")

SEVERITY_MAP = {
    SH.Violation: types.DiagnosticSeverity.Error,
    SH.Warning: types.DiagnosticSeverity.Warning,
    SH.Info: types.DiagnosticSeverity.Information,
}

DEBOUNCE_SECONDS = 0.8


def _uri_to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` to find a .git directory; fall back to `start`."""
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


class EnvironmentManager:
    """Owns the OntoEnv for the workspace / git repository root."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.env: Optional[OntoEnv] = None
        self.root: Optional[Path] = None

    def ensure(self, root: Path) -> OntoEnv:
        with self.lock:
            if self.env is None or self.root != root:
                log.info("initializing ontoenv at %s", root)
                self.env = OntoEnv(
                    path=str(root),
                    search_directories=[str(root)],
                    includes=["*.ttl"],
                    strict=False,
                    create_or_use_cached=True,
                )
                self.root = root
            return self.env

    def refresh(self) -> None:
        """Re-run discovery (e.g. after files were saved/added)."""
        with self.lock:
            if self.env is not None:
                try:
                    self.env.update()
                except Exception:
                    log.exception("ontoenv update failed")


ENV = EnvironmentManager()

server = LanguageServer("shifty-lsp", __version__)

# uri -> threading.Timer for debounced validation
_timers: dict[str, threading.Timer] = {}
_timers_lock = threading.Lock()


def _schedule_validation(uri: str) -> None:
    with _timers_lock:
        old = _timers.pop(uri, None)
        if old is not None:
            old.cancel()
        timer = threading.Timer(DEBOUNCE_SECONDS, validate_document, args=(uri,))
        timer.daemon = True
        _timers[uri] = timer
        timer.start()


@server.feature(types.INITIALIZE)
def on_initialize(params: types.InitializeParams) -> None:
    root: Optional[Path] = None
    if params.workspace_folders:
        root = _uri_to_path(params.workspace_folders[0].uri)
    elif params.root_uri:
        root = _uri_to_path(params.root_uri)
    if root is not None:
        root = find_repo_root(root)
        try:
            ENV.ensure(root)
        except Exception:
            log.exception("failed to initialize ontoenv at %s", root)


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: types.DidOpenTextDocumentParams) -> None:
    # Automatically load the ontoenv environment for the file's repository
    # (creating it if necessary), pick up any new/changed files, and kick off
    # validation of the opened file.
    try:
        path = _uri_to_path(params.text_document.uri)
        env = ENV.ensure(find_repo_root(path.parent))
        with ENV.lock:
            env.update()
    except Exception:
        log.exception("ontoenv load failed on open")
    _schedule_validation(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(params: types.DidChangeTextDocumentParams) -> None:
    _schedule_validation(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(params: types.DidSaveTextDocumentParams) -> None:
    # A saved file may be a new ontology in the repo; let ontoenv discover it.
    ENV.refresh()
    _schedule_validation(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(params: types.DidCloseTextDocumentParams) -> None:
    uri = params.text_document.uri
    with _timers_lock:
        old = _timers.pop(uri, None)
        if old is not None:
            old.cancel()
    server.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=[])
    )


def _imports_of(graph: rdflib.Graph) -> list[str]:
    return [str(o) for o in graph.objects(None, OWL.imports)]


def _ontology_iri(graph: rdflib.Graph) -> Optional[str]:
    for s in graph.subjects(RDF.type, OWL.Ontology):
        if isinstance(s, URIRef):
            return str(s)
    return None


def _check_imports(
    env: OntoEnv, imports: list[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Ensure every declared import is present in the environment, fetching
    it if necessary. Returns (resolved_iris, [(iri, error_message), ...])."""
    resolved: list[str] = []
    failed: list[tuple[str, str]] = []
    for imp in imports:
        try:
            env.get_graph(imp)
            resolved.append(imp)
            continue
        except Exception:
            pass
        # Not cached: try to download it and capture the failure reason
        try:
            env.add(imp, fetch_imports=True)
            resolved.append(imp)
        except Exception as e:
            log.warning("import %s failed: %s", imp, e)
            failed.append((imp, str(e)))
    return resolved, failed


def _build_shapes_graph(
    env: OntoEnv, data_graph: rdflib.Graph, doc_path: Path
) -> tuple[rdflib.Graph, list[tuple[str, str]]]:
    """Resolve the document's owl:imports (downloading if needed) and merge
    the transitive closure of all imports into a shapes graph.

    Returns (shapes_graph, failed_imports) where failed_imports is a list of
    (import_iri, error_message) pairs for imports that could not be fetched."""
    shapes = rdflib.Graph()
    failed: list[tuple[str, str]] = []
    imports = _imports_of(data_graph)

    # Register the document itself so its imports are processed by ontoenv,
    # downloading anything not already cached.
    try:
        env.add(str(doc_path), fetch_imports=True)
    except Exception:
        log.exception("ontoenv add failed for %s", doc_path)

    # Explicitly verify each declared import resolved; collect failures so
    # they can be surfaced to the user as diagnostics.
    resolved, failed = _check_imports(env, imports)

    ont_iri = _ontology_iri(data_graph)
    if ont_iri:
        try:
            closure, _n = env.get_closure(ont_iri)
            # closure includes the doc's own triples; exclude them so the
            # schema comes only from imported ontologies
            for t in closure:
                if t not in data_graph:
                    shapes.add(t)
            if len(shapes) > 0:
                return shapes, failed
        except Exception:
            log.exception("get_closure failed for %s", ont_iri)

    # Fallback: fetch each resolved import's closure individually
    for imp in resolved:
        try:
            g, _n = env.get_closure(imp)
            for t in g:
                shapes.add(t)
        except Exception:
            log.warning("could not build closure for import %s", imp)
    return shapes, failed


def _find_line(text: str, node: rdflib.term.Node) -> int:
    """Best-effort: find the line where an IRI (or its final fragment) appears."""
    if not isinstance(node, URIRef):
        return 0
    iri = str(node)
    candidates = [f"<{iri}>", iri]
    # also try the local name (after # or last /)
    local = iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    if local:
        candidates.append(local)
    lines = text.splitlines()
    for candidate in candidates:
        for i, line in enumerate(lines):
            if candidate in line:
                return i
    return 0


def _report_to_diagnostics(
    report_graph: rdflib.Graph, text: str
) -> list[types.Diagnostic]:
    diagnostics: list[types.Diagnostic] = []
    for result in report_graph.subjects(RDF.type, SH.ValidationResult):
        severity_node = report_graph.value(result, SH.resultSeverity)
        severity = SEVERITY_MAP.get(severity_node, types.DiagnosticSeverity.Error)
        message = str(
            report_graph.value(result, SH.resultMessage) or "SHACL constraint violation"
        )
        focus = report_graph.value(result, SH.focusNode)
        path = report_graph.value(result, SH.resultPath)
        if path is not None:
            message += f" (path: {path})"
        line = _find_line(text, focus) if focus is not None else 0
        rng = types.Range(
            start=types.Position(line=line, character=0),
            end=types.Position(line=line, character=10**6),
        )
        diagnostics.append(
            types.Diagnostic(
                range=rng,
                message=message,
                severity=severity,
                source="shifty",
            )
        )
    return diagnostics


def validate_document(uri: str) -> None:
    path = _uri_to_path(uri)
    try:
        text = server.workspace.get_text_document(uri).source
    except Exception:
        # File is not open in the editor; read it from disk.
        try:
            text = path.read_text()
        except Exception:
            log.exception("could not read %s", path)
            return
    diagnostics: list[types.Diagnostic] = []

    try:
        data_graph = rdflib.Graph()
        data_graph.parse(data=text, format="turtle")
    except Exception as e:
        # Syntax error: extract the line number rdflib reports ("at line N")
        # if available.
        line = 0
        m = re.search(r"at line (\d+)", str(e))
        if m:
            line = max(0, int(m.group(1)) - 1)
        diagnostics.append(
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=line, character=0),
                    end=types.Position(line=line, character=10**6),
                ),
                message=f"Turtle parse error: {e}",
                severity=types.DiagnosticSeverity.Error,
                source="shifty-lsp",
            )
        )
        _publish(uri, diagnostics)
        return

    try:
        env = ENV.ensure(find_repo_root(path.parent))
        with ENV.lock:
            shapes, failed_imports = _build_shapes_graph(env, data_graph, path)

        for imp_iri, err in failed_imports:
            line = _find_line(text, URIRef(imp_iri))
            diagnostics.append(
                types.Diagnostic(
                    range=types.Range(
                        start=types.Position(line=line, character=0),
                        end=types.Position(line=line, character=10**6),
                    ),
                    message=f"owl:imports could not be resolved: {err}",
                    severity=types.DiagnosticSeverity.Error,
                    source="ontoenv",
                )
            )

        log.info(
            "validating %s (%d data triples, %d shapes triples)",
            path.name,
            len(data_graph),
            len(shapes),
        )
        conforms, report_graph, _text = shifty.validate(
            data_graph, shapes if len(shapes) else None, infer=True
        )
        if not conforms:
            diagnostics.extend(_report_to_diagnostics(report_graph, text))
    except Exception:
        log.exception("validation failed for %s", uri)

    _publish(uri, diagnostics)


CMD_REFRESH_ENVIRONMENT = "shifty-lsp.refreshEnvironment"
CMD_VALIDATE_WORKSPACE = "shifty-lsp.validateWorkspace"


@server.command(CMD_REFRESH_ENVIRONMENT)
def refresh_environment(*args) -> None:
    """Re-scan the repository, re-registering all .ttl files and refreshing
    the ontoenv environment; then revalidate all open documents."""
    log.info("command: refreshEnvironment")
    ENV.refresh()
    for uri in list(server.workspace.text_documents):
        _schedule_validation(uri)


@server.command(CMD_VALIDATE_WORKSPACE)
def validate_workspace(*args) -> None:
    """Validate every .ttl file in the repository and publish diagnostics
    for each one (including files that are not open in the editor)."""
    log.info("command: validateWorkspace")
    root = ENV.root
    if root is None:
        docs = list(server.workspace.text_documents)
        if not docs:
            return
        root = find_repo_root(_uri_to_path(docs[0]).parent)
    for ttl in sorted(root.rglob("*.ttl")):
        if ".ontoenv" in ttl.parts:
            continue
        validate_document(ttl.as_uri())


def _publish(uri: str, diagnostics: list[types.Diagnostic]) -> None:
    server.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SHIFTY_LSP_LOG", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    server.start_io()


if __name__ == "__main__":
    main()
