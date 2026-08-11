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

import atexit
import logging
import os
import re
import signal
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from lsprotocol import types
from pygls.lsp.server import LanguageServer

import rdflib
from rdflib import RDF, OWL, URIRef

import shifty
from ontoenv import CatalogRecoveryError, OntoEnv, UnresolvedImportError

from shifty_lsp import __version__

log = logging.getLogger("shifty-lsp")

SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")

SEVERITY_MAP = {
    SH.Violation: types.DiagnosticSeverity.Error,
    SH.Warning: types.DiagnosticSeverity.Warning,
    SH.Info: types.DiagnosticSeverity.Information,
}

DEBOUNCE_SECONDS = 0.8

WRITER_THREAD_PREFIX = "ontoenv-writer"

# How long the environment may sit unused before the server closes it and hands
# the store lock back to other processes. Set SHIFTY_LSP_IDLE_RELEASE=0 to keep
# the environment open for the lifetime of the server instead.
ENV_IDLE_SECONDS = float(os.environ.get("SHIFTY_LSP_IDLE_RELEASE", "5"))

# Another process may hold the store lock; back off and try again (linearly).
BUSY_RETRY_SECONDS = 2.0
BUSY_RETRIES = 5


def _uri_to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` to find a .git directory; fall back to `start`."""
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


class _Writer:
    """Serializes every ontoenv mutation onto a single background thread.

    Mutations anywhere else go wrong in two ways:

      * pygls runs handlers inline on its event loop, so an update() or a
        download started from a request blocks every other message — including
        `shutdown` — for as long as it takes, which is what gets the server
        killed by the editor while it is writing.
      * A mutation that is abandoned part-way leaves ontoenv's
        `catalog.pending` marker behind; the next connect() then raises
        CatalogRecoveryError and has to rebuild the whole catalog.

    A single non-daemon worker means writes never overlap, and a write that
    has started always runs to completion: stop() cancels only jobs that have
    not begun, and ThreadPoolExecutor joins its worker at interpreter exit.
    """

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=WRITER_THREAD_PREFIX
        )
        self._lock = threading.Lock()
        self._stopped = False

    def submit(self, fn: Callable, *args, **kwargs) -> Optional[Future]:
        """Queue a mutation. Returns None once the writer has been stopped."""
        with self._lock:
            if self._stopped:
                log.debug("writer stopped; dropping %s", getattr(fn, "__name__", fn))
                return None
            future = self._pool.submit(fn, *args, **kwargs)
        # Once the queue drains, start counting down to releasing the store
        # lock. The callback runs on the writer thread as each job finishes, so
        # the timer is re-armed by every subsequent job.
        future.add_done_callback(lambda _f: _arm_idle_release())
        return future

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        # cancel_futures drops what has not started; wait=True lets the job
        # that is currently mutating the environment finish cleanly.
        self._pool.shutdown(wait=True, cancel_futures=True)


_writer = _Writer()


def _on_writer_thread() -> bool:
    return threading.current_thread().name.startswith(WRITER_THREAD_PREFIX)


class EnvironmentBusy(Exception):
    """Another process holds the store lock on `.ontoenv/`."""


class EnvironmentManager:
    """Owns the OntoEnv for the workspace / git repository root.

    The environment is held only while it is being used. An open writable
    connection takes an exclusive lock on the store: while the server holds it,
    another process's writable connect() fails outright and a *read-only*
    connect() blocks indefinitely. That makes running scripts against the same
    `.ontoenv/` impossible while a file is open in the editor, so the server
    releases the environment once it has been idle (see `_arm_idle_release`).
    Reopening costs milliseconds — it is only the initial source scan that is
    expensive, and that is not repeated.

    Anything that opens or mutates the environment (ensure, refresh, release,
    close) belongs on the writer thread. Readers use borrow(), which holds the
    lock so the environment can't be released underneath them."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.env: Optional[OntoEnv] = None
        self.root: Optional[Path] = None
        self.stopped = False
        # roots whose sources have been scanned in this process; a reopen must
        # not repeat the scan
        self.scanned: set[Path] = set()

    def peek(self) -> Optional[OntoEnv]:
        """The environment if one is already open; never opens one."""
        return self.env

    @contextmanager
    def borrow(self):
        """Yield the environment for read-only use, or None if it is closed.

        Holds the lock for the duration, so an idle release can't close the
        environment while it is being read."""
        with self.lock:
            yield self.env

    def ensure(self, root: Path) -> OntoEnv:
        with self.lock:
            if self.stopped:
                raise RuntimeError("ontoenv environment is shutting down")
            if self.env is None or self.root != root:
                if self.env is not None:
                    # release the previous environment's store/lock before
                    # opening another one
                    self._close_locked()
                self.env = self._connect(root)
                self.root = root
                if root not in self.scanned:
                    log.info("scanning ontology sources at %s", root)
                    # connect() reopens an existing environment as-is; it does
                    # not rescan configured sources. Discovery happens here,
                    # once per root — reopening after an idle release must not
                    # pay for it again.
                    try:
                        self.env.update()
                    except Exception:
                        log.exception("initial ontoenv update failed")
                    self.scanned.add(root)
                    _namespace_index_invalidate()
            return self.env

    @staticmethod
    def _connect(root: Path) -> OntoEnv:
        options = dict(
            search_directories=[str(root)],
            includes=["*.ttl"],
            strict=False,
        )
        try:
            return OntoEnv.connect(str(root), **options)
        except CatalogRecoveryError:
            # A previous server process was killed mid-mutation (editors do
            # SIGKILL the language server) and left a recovery marker.
            log.warning("ontoenv catalog needs recovery at %s; rebuilding", root)
            return OntoEnv.recover(str(root), **options)
        except ValueError as e:
            if "lock" in str(e).lower():
                raise EnvironmentBusy(str(e)) from e
            raise

    def release(self) -> None:
        """Close the environment but stay ready to reopen it.

        This is what hands the store lock back to other processes; `root` and
        the set of scanned roots are kept so the next use is a cheap reopen."""
        with self.lock:
            if self.env is None:
                return
            log.info("releasing the ontoenv store lock (idle)")
            self._close_locked()

    def refresh(self, invalidate: bool = False, force: bool = False) -> None:
        """Re-run discovery (e.g. after files were saved/added).

        Does not invalidate the shapes cache by default: if a document's
        owl:imports set is unchanged, re-resolving and re-merging the closure
        is wasted work. Pass invalidate=True (the refreshEnvironment command)
        to force rebuilding shapes graphs, e.g. after editing an ontology
        that other files import. force=True additionally makes ontoenv bypass
        its timestamp/cache-age checks when rescanning sources."""
        with self.lock:
            if self.root is not None:
                try:
                    self.ensure(self.root).update(force=force)
                    _namespace_index_invalidate()
                except EnvironmentBusy as e:
                    log.info("ontoenv is in use by another process: %s", e)
                except Exception:
                    log.exception("ontoenv update failed")
        if invalidate:
            _invalidate_shapes_cache()

    def _close_locked(self) -> None:
        env, self.env = self.env, None
        if env is None:
            return
        try:
            env.close()
        except Exception:
            log.exception("closing ontoenv failed")

    def close(self) -> None:
        """Release the environment (flushing pending writes) so the next
        process doesn't have to recover the catalog.

        Closing also latches `stopped`: a request that arrives after shutdown
        must not reopen the environment and start an update() seconds before
        the process exits."""
        with self.lock:
            self.stopped = True
            if self.env is not None:
                self._close_locked()
            self.root = None


ENV = EnvironmentManager()

server = LanguageServer("shifty-lsp", __version__)

# Set during initialize: does the client support window/workDoneProgress?
_client_work_done_progress = False


def _progress_begin(title: str, message: Optional[str] = None) -> Optional[str]:
    """Start a work-done progress notification; returns the token (or None if
    the client doesn't support progress)."""
    if not _client_work_done_progress:
        return None
    token = f"shifty-lsp-{uuid.uuid4()}"
    try:
        server.work_done_progress.create(token).result(timeout=5)
        server.work_done_progress.begin(
            token,
            types.WorkDoneProgressBegin(title=title, message=message, cancellable=False),
        )
        return token
    except Exception:
        log.debug("could not create progress token", exc_info=True)
        return None


def _progress_report(token: Optional[str], message: str) -> None:
    if token is None:
        return
    try:
        server.work_done_progress.report(token, types.WorkDoneProgressReport(message=message))
    except Exception:
        log.debug("progress report failed", exc_info=True)


def _progress_end(token: Optional[str], message: Optional[str] = None) -> None:
    if token is None:
        return
    try:
        server.work_done_progress.end(token, types.WorkDoneProgressEnd(message=message))
    except Exception:
        log.debug("progress end failed", exc_info=True)

# uri -> threading.Timer for debounced validation
_timers: dict[str, threading.Timer] = {}
_timers_lock = threading.Lock()

# uri -> monotonically increasing counter; bumped every time a validation is
# scheduled. A running validation whose generation is stale (the user kept
# typing) discards its diagnostics instead of publishing outdated results.
_validation_gen: dict[str, int] = {}


def _schedule_validation(uri: str) -> None:
    with _timers_lock:
        old = _timers.pop(uri, None)
        if old is not None:
            old.cancel()
        gen = _validation_gen.get(uri, 0) + 1
        _validation_gen[uri] = gen
        timer = threading.Timer(DEBOUNCE_SECONDS, _run_validation, args=(uri,))
        timer.daemon = True
        _timers[uri] = timer
        timer.start()


def _run_validation(uri: str) -> None:
    # Pop ourselves out of the timer table first, so _is_stale() only sees
    # *newer* scheduled runs.
    with _timers_lock:
        _timers.pop(uri, None)
    # The timer thread only queues the work; validation registers the document
    # with ontoenv and may fetch imports, so it runs on the writer thread.
    _writer.submit(_validation_job, uri)


def _validation_job(uri: str, attempt: int = 1) -> None:
    # If the user kept typing while this job sat in the queue, drop it here —
    # before it touches the environment — rather than part-way through.
    if _is_stale(uri):
        log.debug("skipping superseded validation for %s", uri)
        return
    try:
        validate_document(uri)
    except EnvironmentBusy as e:
        # Another process (a script, the ontoenv CLI) holds the store lock.
        # Back off and try again rather than reporting a spurious failure.
        if attempt > BUSY_RETRIES:
            log.warning("giving up on %s: ontoenv still busy (%s)", uri, e)
            return
        delay = BUSY_RETRY_SECONDS * attempt
        log.info("ontoenv busy; retrying %s in %.0fs", os.path.basename(uri), delay)
        timer = threading.Timer(
            delay, lambda: _writer.submit(_validation_job, uri, attempt + 1)
        )
        timer.daemon = True
        timer.start()


def _open_environment(root: Path) -> None:
    """Writer job: connect (and run the initial scan) for `root`."""
    try:
        ENV.ensure(root)
    except EnvironmentBusy as e:
        log.info("ontoenv is in use by another process: %s", e)
    except Exception:
        log.exception("ontoenv load failed for %s", root)


def _open_environment_async(start: Path) -> None:
    """Kick off opening the environment enclosing `start` in the background."""
    _writer.submit(_open_environment, find_repo_root(start))


# timer that closes the environment once nothing has used it for a while
_idle_timer: Optional[threading.Timer] = None
_idle_lock = threading.Lock()


def _arm_idle_release() -> None:
    """(Re)start the countdown to releasing the store lock."""
    if ENV_IDLE_SECONDS <= 0 or ENV.peek() is None:
        return
    with _idle_lock:
        global _idle_timer
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(ENV_IDLE_SECONDS, _idle_release)
        _idle_timer.daemon = True
        _idle_timer.start()


def _idle_release() -> None:
    # Closing is queued like any other environment operation, so it can never
    # land in the middle of a validation.
    _writer.submit(ENV.release)


def _cancel_idle_release() -> None:
    with _idle_lock:
        global _idle_timer
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None


def _cancel_pending(uri: str) -> None:
    with _timers_lock:
        old = _timers.pop(uri, None)
        if old is not None:
            old.cancel()


def _is_stale(uri: str) -> bool:
    """True if a newer validation was scheduled while this one was running.
    Manual validations (validateFile command) bypass this check."""
    with _timers_lock:
        timer = _timers.get(uri)
        return timer is not None and timer.is_alive()


@server.feature(types.INITIALIZE)
def on_initialize(params: types.InitializeParams) -> None:
    global _client_work_done_progress
    if params.capabilities.window and params.capabilities.window.work_done_progress:
        _client_work_done_progress = True
    # NOTE: deliberately do NOT open the ontoenv environment here — it can
    # take seconds (first-time store creation, lock contention) and would
    # block the initialize response, during which the client rejects all
    # requests as "method not supported". The env is created lazily on first
    # use (did_open / validate / commands).
    root: Optional[Path] = None
    if params.workspace_folders:
        root = _uri_to_path(params.workspace_folders[0].uri)
    elif params.root_uri:
        root = _uri_to_path(params.root_uri)
    if root is not None:
        ENV.root = find_repo_root(root)  # cheap; just records the root


_background_stopped = threading.Event()


def _stop_background() -> None:
    """Quiesce the server: stop scheduling work, let the ontoenv mutation that
    is currently running finish, then close the environment (which flushes
    pending writes and drops the store lock).

    The in-flight write is deliberately *not* cancelled: interrupting a
    mutation is exactly what leaves the catalog needing recovery."""
    if _background_stopped.is_set():
        return
    _background_stopped.set()
    _cancel_idle_release()
    with _timers_lock:
        for timer in _timers.values():
            timer.cancel()
        _timers.clear()
    started = time.monotonic()
    _writer.stop()
    waited = time.monotonic() - started
    if waited > 1.0:
        log.info("waited %.1fs for an ontoenv write to finish before closing", waited)
    ENV.close()


@server.feature(types.SHUTDOWN)
def on_shutdown(*args) -> None:
    _stop_background()


@server.feature(types.EXIT)
def on_exit(*args) -> None:
    # pygls calls the user's exit handler before sys.exit, so clients that skip
    # `shutdown` still get a clean close here.
    _stop_background()


atexit.register(_stop_background)


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: types.DidOpenTextDocumentParams) -> None:
    # Load the ontoenv environment for the file's repository (creating it if
    # necessary) on the writer thread, then validate the opened file. Both are
    # queued rather than run here: opening an environment for a large repo
    # takes seconds, and this handler runs on pygls' event loop.
    _open_environment_async(_uri_to_path(params.text_document.uri).parent)
    _schedule_validation(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(params: types.DidChangeTextDocumentParams) -> None:
    _schedule_validation(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(params: types.DidSaveTextDocumentParams) -> None:
    # A saved file may be a new ontology in the repo; let ontoenv discover it
    # on the writer thread, ahead of the validation queued below.
    _writer.submit(ENV.refresh)
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


# Imports that could not be fetched, iri -> (when, error message). A broken or
# unreachable owl:imports must not trigger a fresh download attempt (and so a
# fresh mutation) on every keystroke. Entries expire so a transient network
# failure heals on its own; refreshEnvironment clears them immediately.
_unresolvable: dict[str, tuple[float, str]] = {}

UNRESOLVABLE_RETRY_SECONDS = 60.0


def _recent_failure(iri: str) -> Optional[str]:
    entry = _unresolvable.get(iri)
    if entry is None:
        return None
    when, message = entry
    if time.monotonic() - when > UNRESOLVABLE_RETRY_SECONDS:
        del _unresolvable[iri]
        return None
    return message


def _check_imports(
    env: OntoEnv, imports: list[str], fetch: bool
) -> tuple[list[str], list[tuple[str, str]]]:
    """Check that every declared import is present in the environment.

    With `fetch` (writer thread only), anything missing is downloaded; without
    it, a missing import is simply reported. Returns
    (resolved_iris, [(iri, error_message), ...])."""
    resolved: list[str] = []
    failed: list[tuple[str, str]] = []
    for imp in imports:
        try:
            # read-only, store-backed view: cheap presence check, no copy
            env.get_graph(imp)
            resolved.append(imp)
            continue
        except (ValueError, UnresolvedImportError):
            # not in the environment, or a known-unresolved import target
            pass
        except Exception:
            log.exception("unexpected error looking up import %s", imp)
        known_failure = _recent_failure(imp)
        if known_failure is not None:
            failed.append((imp, known_failure))
            continue
        if not fetch:
            # Read-only context (an LSP request): leave the download to the
            # next validation instead of mutating from a request handler.
            failed.append((imp, "not fetched yet; the next validation resolves it"))
            continue
        # Not cached: try to download it and capture the failure reason.
        # A failed add() rolls its own mutation back as of ontoenv 0.6.1 (0.6.0
        # left a `catalog.pending` marker behind, which forced the *next*
        # connect() to rebuild the whole catalog), hence the floor in
        # pyproject.toml.
        try:
            env.add(imp, fetch_imports=True)
            resolved.append(imp)
        except Exception as e:
            log.warning("import %s failed: %s", imp, e)
            _unresolvable[imp] = (time.monotonic(), str(e))
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
    # Writing is only allowed from the writer thread; a read-only caller gets
    # whatever the environment already holds.
    may_write = _on_writer_thread()

    # Register the document itself so its imports are processed by ontoenv,
    # downloading anything not already cached. update(location) replaces the
    # stored graph for an already-known file; a plain add() would instead
    # no-op *and* mark the source as fresh, so later update() calls would skip
    # the file we are actively editing.
    if may_write:
        try:
            env.update(str(doc_path))
        except Exception:
            log.exception("ontoenv update failed for %s", doc_path)

    # Explicitly verify each declared import resolved; collect failures so
    # they can be surfaced to the user as diagnostics.
    resolved, failed = _check_imports(env, imports, fetch=may_write)

    ont_iri = _ontology_iri(data_graph)
    if ont_iri:
        try:
            # copy_closure (not get_closure) because the result is mutated
            # below and cached for reuse; get_closure returns a read-only view
            # tied to the environment snapshot.
            closure, _n = env.copy_closure(ont_iri)
            # closure includes the doc's own triples; exclude them so the
            # schema comes only from imported ontologies
            for t in data_graph:
                closure.remove(t)
            if len(closure) > 0:
                return closure, failed
        except Exception:
            log.exception("copy_closure failed for %s", ont_iri)

    # Fallback: merge each resolved import's closure individually
    for imp in resolved:
        try:
            env.copy_closure(imp, graph=shapes)
        except Exception:
            log.warning("could not build closure for import %s", imp)
    return shapes, failed


def _find_lines(text: str, node: rdflib.term.Node) -> set[int]:
    """Best-effort: find every line where an IRI (or its prefixed/local form)
    appears in the document."""
    if not isinstance(node, URIRef):
        return {0}
    iri = str(node)
    local = iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    # candidate substrings, most specific first: full IRI in angle brackets,
    # prefixed form (ns:local for each declared prefix), bare local name
    candidates = [f"<{iri}>"]
    for prefix, base in _document_prefixes(text).items():
        if iri.startswith(base):
            candidates.append(f"{prefix}:{iri[len(base):]}")
    if local:
        candidates.append(local)
    lines = text.splitlines()
    # use the first candidate form that matches anywhere, then report all of
    # its occurrences; match on token boundaries so `ex:Person` doesn't hit
    # `ex:PersonShape`
    for candidate in candidates:
        if candidate.startswith("<"):
            hits = {i for i, line in enumerate(lines) if candidate in line}
        else:
            pat = re.compile(r"(?<![\w.:-])" + re.escape(candidate) + r"(?![\w.-])")
            hits = {i for i, line in enumerate(lines) if pat.search(line)}
        if hits:
            return hits
    return {0}


def _find_line(text: str, node: rdflib.term.Node) -> int:
    return min(_find_lines(text, node))


# uri -> most recent shapes graph used for validation (drives completion)
_last_shapes: dict[str, rdflib.Graph] = {}

# Shapes-graph cache: uri -> (generation, sorted imports, shapes, failed imports).
# Building the merged closure graph is expensive (100k+ triples), so we reuse
# it across validations until the environment changes or the document's
# owl:imports change.
_shapes_cache: dict[str, tuple[int, tuple[str, ...], rdflib.Graph, list]] = {}
_env_generation = 0


def _invalidate_shapes_cache() -> None:
    global _env_generation
    _env_generation += 1
    _shapes_cache.clear()
    _last_shapes.clear()
    # give previously unreachable imports another chance
    _unresolvable.clear()


# prefix -> (namespace IRI, location of the ontology that declares it or None).
# Harvested on the writer thread so code actions keep working — and stay fast —
# while the environment is closed: get_namespaces() alone takes seconds on an
# environment the size of Brick, and used to run on every code-action request.
_namespace_index: dict[str, tuple[str, Optional[str]]] = {}
_namespace_index_stale = True


def _namespace_index_invalidate() -> None:
    global _namespace_index_stale
    _namespace_index_stale = True


def _refresh_namespace_index(env: OntoEnv) -> None:
    """Writer job step: snapshot the environment's prefixes."""
    global _namespace_index_stale
    if not _namespace_index_stale:
        return
    index: dict[str, tuple[str, Optional[str]]] = {}
    try:
        for prefix, ns in env.get_namespaces().items():
            index[prefix] = (str(ns), None)
    except Exception:
        log.exception("could not read namespaces from the environment")
        return
    # remember which ontology declares each prefix, so the quickfix can offer
    # an owl:imports alongside the @prefix line
    try:
        for name in env.get_ontology_names():
            try:
                ont = env.get_ontology(name)
                declared = ont.namespace_map or {}
            except Exception:
                continue
            for prefix in declared:
                ns, location = index.get(prefix, (None, None))
                if ns is not None and location is None:
                    index[prefix] = (ns, ont.location or name)
    except Exception:
        log.exception("could not map prefixes to ontologies")
    _namespace_index.clear()
    _namespace_index.update(index)
    _namespace_index_stale = False
    log.info("indexed %d namespace prefixes", len(_namespace_index))


def _imports_in_text(text: str) -> list[str]:
    """owl:imports of a buffer, falling back to a regex when it doesn't parse
    (common mid-typing)."""
    graph = rdflib.Graph()
    try:
        graph.parse(data=text, format="turtle")
    except Exception:
        return re.findall(r"owl:imports\s+<([^>]+)>", text)
    return _imports_of(graph)


def _shapes_for_request(uri: str, text: str) -> Optional[rdflib.Graph]:
    """Shapes graph for a read-only LSP request (hover, completion, …).

    Never mutates the environment. Request handlers run on pygls' event loop,
    and a fetch started from one is precisely the mutation that gets abandoned
    when the editor supersedes the request or shuts the server down. Anything
    missing here is filled in by the next validation, on the writer thread."""
    graph = _last_shapes.get(uri)
    if graph is not None:
        return graph
    shapes = rdflib.Graph()
    with ENV.borrow() as env:
        if env is None:
            # Not validated yet, or the environment is currently released.
            # Let validation open it and build the real graph rather than
            # taking the store lock from a request handler.
            _schedule_validation(uri)
            return None
        for imp in _imports_in_text(text):
            try:
                env.copy_closure(imp, graph=shapes)
            except Exception:
                log.debug("import %s is not in the environment yet", imp)
    # deliberately not cached in _last_shapes: it may be missing imports that
    # have not been fetched yet, and validation publishes the complete graph
    return shapes


def _get_shapes(env: OntoEnv, data_graph: rdflib.Graph, doc_path: Path, uri: str):
    """Like _build_shapes_graph, but cached per document."""
    imports = tuple(sorted(_imports_of(data_graph)))
    cached = _shapes_cache.get(uri)
    if cached is not None and cached[0] == _env_generation and cached[1] == imports:
        log.info("shapes cache hit for %s", doc_path.name)
        return cached[2], list(cached[3])
    shapes, failed = _build_shapes_graph(env, data_graph, doc_path)
    _shapes_cache[uri] = (_env_generation, imports, shapes, list(failed))
    return shapes, failed

RDFS = rdflib.Namespace("http://www.w3.org/2000/01/rdf-schema#")

_TYPE_KINDS = {
    OWL.Class: types.CompletionItemKind.Class,
    RDFS.Class: types.CompletionItemKind.Class,
    OWL.ObjectProperty: types.CompletionItemKind.Property,
    OWL.DatatypeProperty: types.CompletionItemKind.Property,
    OWL.AnnotationProperty: types.CompletionItemKind.Property,
    RDF.Property: types.CompletionItemKind.Property,
    OWL.NamedIndividual: types.CompletionItemKind.Value,
}


def _completion_items_for_prefix(
    graph: rdflib.Graph, prefix: str, namespace: str
) -> list[types.CompletionItem]:
    """All qname candidates in `namespace` found in the graph."""
    terms: set[URIRef] = set()
    for s, p, o in graph:
        for t in (s, p, o):
            if isinstance(t, URIRef) and str(t).startswith(namespace):
                terms.add(t)
    items: list[types.CompletionItem] = []
    for term in sorted(terms):
        local = str(term)[len(namespace):]
        if not local:
            continue
        kind = types.CompletionItemKind.Keyword
        for t, k in _TYPE_KINDS.items():
            if (term, RDF.type, t) in graph:
                kind = k
                break
        label = graph.value(term, RDFS.label)
        comment = graph.value(term, RDFS.comment)
        doc = None
        if comment:
            doc = str(comment)[:500]
        elif label:
            doc = str(label)
        items.append(
            types.CompletionItem(
                label=f"{prefix}:{local}",
                kind=kind,
                detail=str(label) if label else None,
                documentation=doc,
                filter_text=local,
            )
        )
    return items


def _document_prefixes(text: str) -> dict[str, str]:
    return dict(re.findall(r"(?:@prefix|PREFIX)\s+([\w-]+):\s*<([^>]+)>", text))


_SYMBOL_KINDS = [
    (OWL.Ontology, types.SymbolKind.Package),
    (OWL.Class, types.SymbolKind.Class),
    (RDFS.Class, types.SymbolKind.Class),
    (OWL.ObjectProperty, types.SymbolKind.Property),
    (OWL.DatatypeProperty, types.SymbolKind.Property),
    (OWL.AnnotationProperty, types.SymbolKind.Property),
    (RDF.Property, types.SymbolKind.Property),
    (SH.NodeShape, types.SymbolKind.Interface),
    (SH.PropertyShape, types.SymbolKind.Interface),
]


def _line_range(text: str, line: int) -> types.Range:
    lines = text.splitlines()
    end = len(lines[line]) if line < len(lines) else 0
    return types.Range(
        start=types.Position(line=line, character=0),
        end=types.Position(line=line, character=end),
    )


@server.feature(types.TEXT_DOCUMENT_REFERENCES)
@server.thread()
def references(params: types.ReferenceParams) -> Optional[list[types.Location]]:
    """Find every occurrence of the term under the cursor: in this file and
    in every other .ttl file in the repository."""
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        iri = _iri_at_position(doc.source, params.position.line, params.position.character)
        if not iri:
            return None
        node = URIRef(iri)
        locations: list[types.Location] = []
        for line in sorted(_find_lines(doc.source, node)):
            locations.append(
                types.Location(uri=params.text_document.uri, range=_line_range(doc.source, line))
            )
        root = ENV.root or find_repo_root(_uri_to_path(params.text_document.uri).parent)
        self_path = os.path.realpath(_uri_to_path(params.text_document.uri))
        for ttl in sorted(root.rglob("*.ttl")):
            if ".ontoenv" in ttl.parts or os.path.realpath(ttl) == self_path:
                continue
            try:
                text = ttl.read_text()
            except Exception:
                continue
            if iri not in text and str(node).rsplit("#", 1)[-1].rsplit("/", 1)[-1] not in text:
                continue
            for line in sorted(_find_lines(text, node)):
                locations.append(types.Location(uri=ttl.as_uri(), range=_line_range(text, line)))
        return locations or None
    except Exception:
        log.exception("references failed")
        return None


@server.feature(
    types.TEXT_DOCUMENT_CODE_ACTION,
    types.CodeActionOptions(code_action_kinds=[types.CodeActionKind.QuickFix]),
)
@server.thread()
def code_action(params: types.CodeActionParams) -> Optional[list[types.CodeAction]]:
    """Quickfix: a prefixed name is used whose prefix is not declared, but is
    a known namespace in the ontology environment -> offer to add the @prefix
    declaration and an owl:imports statement for the matching ontology."""
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        text = doc.source
        declared = _document_prefixes(text)
        used = set(re.findall(r"\b([A-Za-z_][\w-]*):[\w.-]+", text))
        undeclared = used - set(declared) - {"http", "https"}
        if not undeclared:
            return None

        # Namespaces known to the environment, from the index built during
        # validation, plus whatever the document's own shapes graph declares.
        # This handler never opens or reads the environment: doing so would
        # take the store lock away from other processes for the duration.
        known: dict[str, str] = {p: ns for p, (ns, _loc) in _namespace_index.items()}
        # ontology that declares each prefix, for the accompanying owl:imports
        import_for = {
            p: loc for p, (_ns, loc) in _namespace_index.items() if loc is not None
        }
        graph = _last_shapes.get(params.text_document.uri)
        if graph is not None:
            known.update({p: str(ns) for p, ns in graph.namespaces()})
        if not known:
            # nothing indexed yet — validation populates it
            _schedule_validation(params.text_document.uri)
            return None

        # insertion point: after the last @prefix line
        lines = text.splitlines()
        insert_line = 0
        for i, line in enumerate(lines):
            if line.lstrip().startswith(("@prefix", "PREFIX")):
                insert_line = i + 1

        doc_ont = re.search(r"<([^>]+)>\s+a\s+owl:Ontology", text)
        actions: list[types.CodeAction] = []
        for p in sorted(undeclared):
            ns = known.get(p)
            if ns is None:
                continue
            edits = [
                types.TextEdit(
                    range=types.Range(
                        start=types.Position(line=insert_line, character=0),
                        end=types.Position(line=insert_line, character=0),
                    ),
                    new_text=f"@prefix {p}: <{ns}> .\n",
                )
            ]
            imp = import_for.get(p)
            title = f"Declare prefix '{p}:'"
            if imp and doc_ont:
                edits.append(
                    types.TextEdit(
                        range=types.Range(
                            start=types.Position(line=len(lines), character=0),
                            end=types.Position(line=len(lines), character=0),
                        ),
                        new_text=f"\n<{doc_ont.group(1)}> owl:imports <{imp}> .\n",
                    )
                )
                title += f" and import {imp}"
            actions.append(
                types.CodeAction(
                    title=title,
                    kind=types.CodeActionKind.QuickFix,
                    edit=types.WorkspaceEdit(changes={params.text_document.uri: edits}),
                )
            )
        return actions or None
    except Exception:
        log.exception("code action failed")
        return None


@server.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
@server.thread()
def document_symbol(
    params: types.DocumentSymbolParams,
) -> Optional[list[types.DocumentSymbol]]:
    """Outline of the file: the ontology declaration plus every typed subject
    (classes, properties, shapes, individuals), navigable via gO / outline
    plugins / Telescope."""
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        text = doc.source
        graph = rdflib.Graph()
        graph.parse(data=text, format="turtle")
    except Exception:
        return None
    prefixes = _document_prefixes(text)
    lines = text.splitlines()

    def qname(iri: str) -> str:
        for p, base in prefixes.items():
            if iri.startswith(base):
                return f"{p}:{iri[len(base):]}"
        return iri

    symbols: list[types.DocumentSymbol] = []
    for s in sorted(set(graph.subjects(RDF.type, None)), key=str):
        if not isinstance(s, URIRef):
            continue
        types_ = list(graph.objects(s, RDF.type))
        kind = types.SymbolKind.Variable
        for t, k in _SYMBOL_KINDS:
            if t in types_:
                kind = k
                break
        line = min(_find_lines(text, s))
        rng = types.Range(
            start=types.Position(line=line, character=0),
            end=types.Position(line=line, character=len(lines[line])),
        )
        symbols.append(
            types.DocumentSymbol(
                name=qname(str(s)),
                detail=", ".join(qname(str(t)) for t in types_),
                kind=kind,
                range=rng,
                selection_range=rng,
            )
        )
    return symbols or None


@server.feature(types.TEXT_DOCUMENT_HOVER)
@server.thread()
def hover(params: types.HoverParams) -> Optional[types.Hover]:
    """Show documentation for the term under the cursor: rdfs:label,
    rdfs:comment, types, and supertypes from the ontology environment."""
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        iri = _iri_at_position(doc.source, params.position.line, params.position.character)
        if not iri:
            return None
        graph = _shapes_for_request(params.text_document.uri, doc.source)
        if graph is None:
            return None

        term = URIRef(iri)
        sections: list[str] = []
        label = graph.value(term, RDFS.label)
        if label:
            sections.append(f"**{label}**")
        types_ = [t for t in graph.objects(term, RDF.type)]
        if types_:
            local = lambda u: str(u).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            sections.append("Type: " + ", ".join(f"`{local(t)}`" for t in types_))
        supers = [s for s in graph.objects(term, RDFS.subClassOf) if isinstance(s, URIRef)]
        if supers:
            local = lambda u: str(u).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            sections.append("Subclass of: " + ", ".join(f"`{local(s)}`" for s in supers))
        comment = graph.value(term, RDFS.comment)
        if comment:
            sections.append(str(comment))
        if not sections:
            return None
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown, value="\n\n".join(sections)
            )
        )
    except Exception:
        log.exception("hover failed")
        return None


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=[":"], resolve_provider=False),
)
@server.thread()
def completion(params: types.CompletionParams) -> Optional[types.CompletionList]:
    """Prefix-aware completion: `brick:IAQ|` completes terms from the Brick
    namespace using the merged ontology environment (imports closure)."""
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        lines = doc.source.splitlines()
        pos = params.position
        if pos.line >= len(lines):
            return None
        m = re.search(r"([A-Za-z_][\w-]*):([\w.-]*)$", lines[pos.line][: pos.character])
        if not m:
            return None
        prefix = m.group(1)
        namespace = _document_prefixes(doc.source).get(prefix)
        if namespace is None:
            return None

        # Not validated yet, or the buffer doesn't currently parse (common
        # mid-typing): fall back to the closure of whatever imports the
        # environment already holds.
        graph = _shapes_for_request(params.text_document.uri, doc.source)
        if graph is None:
            return None

        items = _completion_items_for_prefix(graph, prefix, namespace)
        return types.CompletionList(is_incomplete=False, items=items)
    except Exception:
        log.exception("completion failed")
        return None


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
        value = report_graph.value(result, SH.value)
        path = report_graph.value(result, SH.resultPath)
        if path is not None:
            message += f" (path: {path})"
        # If the violation identifies a specific offending value, report on
        # the value node; otherwise report on the focus node.
        target = value if value is not None else focus
        lines = _find_lines(text, target) if target is not None else {0}
        for line in sorted(lines):
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


def validate_document(uri: str) -> bool:
    """Validate a document; returns True if it conforms (no violations)."""
    token = _progress_begin("shifty-lsp", f"validating {os.path.basename(_uri_to_path(uri))}")
    try:
        return _validate_document(uri, token)
    finally:
        _progress_end(token)


def _validate_document(uri: str, progress_token: Optional[str]) -> bool:
    path = _uri_to_path(uri)
    try:
        text = server.workspace.get_text_document(uri).source
    except Exception:
        # File is not open in the editor; read it from disk.
        try:
            text = path.read_text()
        except Exception:
            log.exception("could not read %s", path)
            return False
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
        return False

    try:
        _progress_report(progress_token, "resolving owl:imports…")
        env = ENV.ensure(find_repo_root(path.parent))
        with ENV.lock:
            shapes, failed_imports = _get_shapes(env, data_graph, path, uri)
            _refresh_namespace_index(env)
        _last_shapes[uri] = shapes
        # Everything below works on detached rdflib graphs, so hand the store
        # lock back now instead of holding it through the SHACL run — which is
        # by far the longest part of a validation.
        if ENV_IDLE_SECONDS > 0:
            ENV.release()

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
        _progress_report(
            progress_token,
            f"shifty: inference + validation ({len(data_graph)} data triples, "
            f"{len(shapes)} shape triples)…",
        )
        conforms, report_graph, _text = shifty.validate(
            data_graph, shapes if len(shapes) else None, infer=True
        )
        _progress_report(progress_token, "publishing diagnostics…")
        if not conforms:
            diagnostics.extend(_report_to_diagnostics(report_graph, text))
    except EnvironmentBusy:
        # let the caller retry once the other process is done; publishing
        # anything now would just flash stale diagnostics
        raise
    except Exception:
        log.exception("validation failed for %s", uri)
        conforms = False

    if _is_stale(uri):
        log.info("discarding stale validation results for %s", path.name)
        return conforms
    _publish(uri, diagnostics)
    n_errors = sum(1 for d in diagnostics if d.severity == types.DiagnosticSeverity.Error)
    n_warnings = sum(
        1 for d in diagnostics if d.severity == types.DiagnosticSeverity.Warning
    )
    if not diagnostics:
        summary = f"{path.name}: conforms \u2713"
        msg_type = types.MessageType.Info
    else:
        summary = f"{path.name}: {n_errors} error(s), {n_warnings} warning(s)"
        msg_type = (
            types.MessageType.Error if n_errors else types.MessageType.Warning
        )
    try:
        server.window_show_message(
            types.ShowMessageParams(type=msg_type, message=f"shifty: {summary}")
        )
    except Exception:
        log.debug("show_message failed", exc_info=True)
    # Any error-severity diagnostic (failed import, validation error, parse
    # error) means the document does not conform.
    return conforms and not any(
        d.severity == types.DiagnosticSeverity.Error for d in diagnostics
    )


def _iri_at_position(text: str, line: int, character: int) -> Optional[str]:
    """Extract the IRI (angle-bracket or prefixed name) under the cursor."""
    lines = text.splitlines()
    if line >= len(lines):
        return None
    row = lines[line]
    # angle-bracket IRI: <http://...>
    for m in re.finditer(r"<([^<>\s]+)>", row):
        if m.start() <= character <= m.end():
            return m.group(1)
    # prefixed name: foo:bar
    for m in re.finditer(r"\b([A-Za-z_][\w-]*):([\w.-]+)", row):
        if m.start() <= character <= m.end():
            prefixes = dict(
                re.findall(r"(?:@prefix|PREFIX)\s+([\w-]+):\s*<([^>]+)>", text)
            )
            base = prefixes.get(m.group(1))
            if base:
                return base + m.group(2)
    return None


def _materialize_remote(env: OntoEnv, name: str, location: str) -> Optional[str]:
    """Serialize a remote ontology's graph out of the ontoenv store into a
    local cache .ttl file so the editor can open it; returns the file URI."""
    if ENV.root is None:
        return None
    cache_dir = ENV.root / ".ontoenv" / "lsp-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") + ".ttl"
    out = cache_dir / safe
    if not out.exists():
        try:
            # copy_graph, not get_graph: this writes a detached snapshot to
            # disk, so take a real graph rather than a store-backed view
            g = env.copy_graph(name)
            g.serialize(destination=str(out), format="turtle")
            log.info("materialized %s -> %s", name, out)
        except Exception:
            log.exception("could not materialize %s", name)
            return None
    return out.as_uri()


def _ontology_file(env: OntoEnv, iri: str) -> Optional[str]:
    """Resolve an ontology IRI (or its source URL) to a local file URI the
    editor can open."""
    ont = None
    try:
        ont = env.get_ontology(iri)
    except Exception:
        # maybe the IRI is the *location* of a known ontology (e.g. the
        # versioned download URL rather than the ontology name)
        for name in env.get_ontology_names():
            try:
                candidate = env.get_ontology(name)
            except Exception:
                continue
            if candidate.location == iri:
                ont = candidate
                break
    if ont is None:
        return None
    if ont.location.startswith("file://"):
        return ont.location
    return _materialize_remote(env, ont.name, ont.location)


def _definition_job(root: Path, iri: str) -> Optional[str]:
    """Writer job: resolve an ontology IRI to a local file URI."""
    try:
        env = ENV.ensure(root)
    except EnvironmentBusy as e:
        log.info("ontoenv is in use by another process: %s", e)
        return None
    except Exception:
        log.exception("could not open the environment for goto-definition")
        return None
    return _ontology_file(env, iri)


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
@server.thread()
def definition(params: types.DefinitionParams) -> Optional[types.Location]:
    """Goto-definition: jump from an ontology IRI (e.g. in an owl:imports
    statement) to the local .ttl file for that ontology. Remote ontologies
    are materialized from the ontoenv store into .ontoenv/lsp-cache/."""
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        iri = _iri_at_position(doc.source, params.position.line, params.position.character)
        if not iri:
            return None
        # Resolving an IRI to a file needs the environment, and materializing a
        # remote ontology writes a cache file, so this runs as a writer job:
        # the handler is threaded, so waiting doesn't block the event loop.
        future = _writer.submit(
            _definition_job, find_repo_root(_uri_to_path(params.text_document.uri).parent), iri
        )
        if future is None:
            return None
        target = future.result(timeout=60)
        if target is None:
            return None
        zero = types.Range(
            start=types.Position(line=0, character=0),
            end=types.Position(line=0, character=0),
        )
        log.info("definition: %s -> %s", iri, target)
        return types.Location(uri=target, range=zero)
    except Exception:
        log.exception("definition failed")
        return None


CMD_REFRESH_ENVIRONMENT = "shifty-lsp.refreshEnvironment"
CMD_VALIDATE_FILE = "shifty-lsp.validateFile"


@server.command(CMD_REFRESH_ENVIRONMENT)
@server.thread()
def refresh_environment(*args) -> None:
    """Re-scan the repository, re-registering all .ttl files, invalidating
    cached shapes graphs, and revalidating all open documents."""
    log.info("command: refreshEnvironment")
    future = _writer.submit(ENV.refresh, invalidate=True, force=True)
    if future is not None:
        try:
            future.result()
        except Exception:
            log.exception("refreshEnvironment failed")
    for uri in list(server.workspace.text_documents):
        _schedule_validation(uri)


@server.command(CMD_VALIDATE_FILE)
@server.thread()
def validate_file(*args) -> bool:
    """Validate a single file (the current document) and publish diagnostics.

    Expects the document URI as the first argument. Returns True if the file
    conforms, False otherwise."""
    log.info("command: validateFile")
    uri: Optional[str] = None
    if args and isinstance(args[0], dict):
        # clients often pass the TextDocumentIdentifier
        uri = args[0].get("uri")
    elif args and isinstance(args[0], str):
        uri = args[0]
    if uri is None:
        docs = list(server.workspace.text_documents)
        if not docs:
            return True
        uri = docs[0]
    _cancel_pending(uri)  # don't let a queued debounced run override this one
    # Runs on the writer thread like every other validation; this handler is
    # threaded, so waiting for the result doesn't block the event loop.
    future = _writer.submit(validate_document, uri)
    if future is None:
        return False
    try:
        conforms = future.result()
    except EnvironmentBusy as e:
        log.warning("could not validate %s: ontoenv is in use (%s)", uri, e)
        conforms = False
    except Exception:
        log.exception("validation failed for %s", uri)
        conforms = False
    log.info("validateFile(%s) -> %s", uri, conforms)
    return conforms


def _publish(uri: str, diagnostics: list[types.Diagnostic]) -> None:
    server.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def _install_signal_handlers() -> None:
    """Editors terminate language servers rather than asking them to exit.
    SIGTERM/SIGHUP are catchable, so use them to close the environment the same
    way `shutdown` does. (SIGKILL isn't, which is why every mutation is kept
    short and serialized.)"""

    def handler(signum, _frame):
        log.info("received signal %s; shutting down", signum)
        _stop_background()
        # pygls' reader thread is parked in a blocking read on stdin that may
        # never return, so a normal exit would hang here (and get us SIGKILLed
        # instead). The environment is closed and flushed at this point, so
        # leaving right away is safe.
        logging.shutdown()
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (AttributeError, OSError, ValueError):
            log.debug("could not install handler for %s", sig, exc_info=True)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SHIFTY_LSP_LOG", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _install_signal_handlers()
    try:
        server.start_io()
    finally:
        _stop_background()


if __name__ == "__main__":
    main()
