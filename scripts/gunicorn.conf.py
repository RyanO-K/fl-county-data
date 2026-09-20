"""Gunicorn hooks for the deployed app. Gunicorn loads ./gunicorn.conf.py
from its working directory, which the start command sets to this folder
(--chdir scripts), so no flag is needed.

app.py starts a background thread at import time that scans the database
to warm the facets cache. If gunicorn preloads the application (--preload
or GUNICORN_CMD_ARGS), that import runs in the master while workers are
forked, and each child inherits the thread's half-done state with no thread
left to finish it: the in-flight marker for the facets key being computed
(every request for that key then waits on it forever, pinning a worker
thread until the worker is fully stuck) and, if the fork lands inside a
SQLite critical section, SQLite's internal locks (every database call in
that worker then blocks, and the instance never answers a request). That
took the demo site down on 2026-09-19.

These hooks make that safe whether or not preload is on:
- when_ready runs in the master before any worker is forked: wait for the
  warm-up thread, so workers are never forked mid-scan and inherit a
  complete cache instead of a broken one.
- post_fork runs in each child before it serves: drop inherited in-flight
  markers, replace a lock that was held at fork time, and start a fresh
  warm-up when the child's cache is empty.
Without preload nothing has been imported when they run and both are no-ops.
Every step is best effort and tolerates a renamed or missing attribute, so a
refactor of app.py can never stop the server from starting.
"""
import sys
import threading

WARMUP_THREAD = "facets-warmup"   # the name app.py gives its thread
WARMUP_WAIT_S = 240               # under Render's 5-minute port-scan window


def _warmup_thread():
    return next((t for t in threading.enumerate() if t.name == WARMUP_THREAD), None)


def when_ready(server):
    """Master, before forking: don't fork while the warm-up scan is running."""
    t = _warmup_thread()
    if t is None:
        return
    server.log.info("waiting for %s before forking workers", WARMUP_THREAD)
    t.join(WARMUP_WAIT_S)
    if t.is_alive():
        server.log.warning("%s still running after %ss; forking anyway",
                           WARMUP_THREAD, WARMUP_WAIT_S)


def post_fork(server, worker):
    """Child, before serving: undo whatever a preloaded master handed down."""
    app = sys.modules.get("app")
    if app is None:
        return
    inflight = getattr(app, "_facets_inflight", None)
    if isinstance(inflight, dict) and inflight:
        server.log.warning("worker %s: dropping %d inherited in-flight facet computations",
                           worker.pid, len(inflight))
        inflight.clear()
    lock = getattr(app, "_facets_lock", None)
    if lock is not None and getattr(lock, "locked", lambda: False)():
        server.log.warning("worker %s: facets lock was held at fork; replacing it", worker.pid)
        app._facets_lock = threading.Lock()
    cache = getattr(app, "_facets_cache", None)
    warm = getattr(app, "warm_facets", None)
    if isinstance(cache, dict) and not cache and callable(warm):
        threading.Thread(target=warm, name=WARMUP_THREAD, daemon=True).start()
