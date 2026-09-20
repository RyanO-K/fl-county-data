"""The gunicorn hooks in scripts/gunicorn.conf.py must undo what a preloaded
master hands its forked workers, and must never raise: an error here would
stop every worker from booting."""
import importlib.util
import logging
import sys
import threading
import types
from pathlib import Path

import pytest

CONF = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"


@pytest.fixture
def hooks():
    spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", CONF)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server():
    return types.SimpleNamespace(log=logging.getLogger("test-gunicorn"),
                                 cfg=types.SimpleNamespace(preload_app=False))


@pytest.fixture
def fake_app(monkeypatch):
    """Stand-in for the app module as a fresh child sees it after a fork
    that happened mid-warm-up."""
    started = []
    lock = threading.Lock()
    lock.acquire()  # held by a thread that no longer exists in the child
    app = types.SimpleNamespace(
        _facets_inflight={"[]": threading.Event()},
        _facets_lock=lock,
        _facets_cache={},
        warm_facets=lambda: started.append(threading.current_thread().name),
    )
    monkeypatch.setitem(sys.modules, "app", app)
    return app, started


def test_post_fork_repairs_inherited_state(hooks, server, fake_app):
    app, started = fake_app
    old_lock = app._facets_lock
    hooks.post_fork(server, types.SimpleNamespace(pid=123))
    assert app._facets_inflight == {}
    assert app._facets_lock is not old_lock and not app._facets_lock.locked()
    for t in threading.enumerate():
        if t.name == hooks.WARMUP_THREAD:
            t.join(5)
    assert started == [hooks.WARMUP_THREAD]


def test_post_fork_leaves_a_healthy_child_alone(hooks, server, monkeypatch):
    lock = threading.Lock()
    app = types.SimpleNamespace(_facets_inflight={}, _facets_lock=lock,
                                _facets_cache={"[]": (0, {})}, warm_facets=lambda: None)
    monkeypatch.setitem(sys.modules, "app", app)
    before = {t.name for t in threading.enumerate()}
    hooks.post_fork(server, types.SimpleNamespace(pid=1))
    assert app._facets_lock is lock
    assert hooks.WARMUP_THREAD not in {t.name for t in threading.enumerate()} - before


def test_hooks_tolerate_missing_app_or_attributes(hooks, server, monkeypatch):
    monkeypatch.delitem(sys.modules, "app", raising=False)
    hooks.post_fork(server, types.SimpleNamespace(pid=1))
    monkeypatch.setitem(sys.modules, "app", types.SimpleNamespace())
    hooks.post_fork(server, types.SimpleNamespace(pid=1))
    hooks.when_ready(server)  # no warm-up thread exists: returns at once


def test_when_ready_waits_for_the_warmup_thread(hooks, server):
    release = threading.Event()
    t = threading.Thread(target=release.wait, name=hooks.WARMUP_THREAD, daemon=True)
    t.start()
    threading.Timer(0.2, release.set).start()
    hooks.when_ready(server)
    assert not t.is_alive()
