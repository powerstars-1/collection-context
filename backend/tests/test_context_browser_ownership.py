"""Product profile ownership; SDK is a fake and no platform page is visited."""

from __future__ import annotations

import builtins
import os
import select
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import browser as browser_module
from collection_context.infrastructure.browser import BrowserSession
from collection_context.infrastructure.browser_ownership import (
    PROFILE_MARKER,
    PROFILE_MARKER_BODY,
    BrowserLease,
)
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.platform_safety import detect_platform_safety

needs_native = pytest.mark.skipif(
    not detect_platform_safety().runtime_supported, reason="actual native profile lease unavailable"
)


def code(action):
    with pytest.raises(ContextError) as caught:
        action()
    return caught.value.code


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "原创 中文 空格账号"
    root.mkdir(mode=0o700)
    with SafeFiles(root) as files:
        files.write(PROFILE_MARKER, PROFILE_MARKER_BODY)
    return root


@pytest.fixture
def sdk(monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    events = []
    hooks = SimpleNamespace(start=None, launch=None, close=None, stop=None)

    def event(name):
        events.append(name)
        callback = getattr(hooks, name)
        if callback is not None:
            callback()

    context = SimpleNamespace(close=lambda: event("close"))

    def launch(*args, **kwargs):
        event("launch")
        return context

    runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch), stop=lambda: event("stop")
    )

    def start():
        event("start")
        return runtime

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: SimpleNamespace(start=start))
    return events, hooks


@needs_native
def test_foreign_or_invalid_profile_never_gets_lock_or_sdk(tmp_path, sdk):
    profile = tmp_path / "foreign"
    profile.mkdir(mode=0o700)
    (profile / "original-data.txt").write_bytes(b"original nonsecret data")
    before = sorted(path.name for path in profile.iterdir())
    assert code(lambda: BrowserSession(profile).__enter__()) == "foreign_login_profile"
    assert sorted(path.name for path in profile.iterdir()) == before
    assert sdk[0] == []
    for body in (b"{}", b"[]", b"bad", b'{"schema_version":true,"owner":"collection-context"}'):
        with SafeFiles(profile) as files:
            files.write(PROFILE_MARKER, body, replace=(profile / PROFILE_MARKER).exists())
        assert code(lambda: BrowserLease(profile)) == "foreign_login_profile"
        assert not (profile / ".collection-context-browser.lock").exists()


@needs_native
def test_same_process_and_thread_busy_do_not_start_second_sdk(profile, sdk):
    with BrowserSession(profile) as first:
        assert sdk[0] == ["start", "launch"]
        assert code(lambda: BrowserSession(profile).__enter__()) == "browser_busy"
        assert code(first.__enter__) == "browser_busy"
        result = []

        def second():
            result.append(code(lambda: BrowserSession(profile).__enter__()))

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive() and result == ["browser_busy"]
        assert sdk[0] == ["start", "launch"]
    assert sdk[0] == ["start", "launch", "close", "stop"]
    with BrowserLease(profile):
        pass


@needs_native
def test_browser_remains_busy_through_context_close_and_driver_stop(profile, sdk):
    events, hooks = sdk
    observations = []
    hooks.close = lambda: observations.append(code(lambda: BrowserLease(profile)))
    hooks.stop = lambda: observations.append(code(lambda: BrowserLease(profile)))
    with BrowserSession(profile):
        pass
    assert observations == ["browser_busy", "browser_busy"]
    assert events == ["start", "launch", "close", "stop"]
    with BrowserLease(profile):
        pass


@needs_native
@pytest.mark.parametrize("stage", ["start", "launch"])
def test_start_failure_releases_lease_after_confirmed_sdk_cleanup(profile, sdk, stage):
    def fail():
        raise RuntimeError("synthetic SDK failure; do not echo")

    setattr(sdk[1], stage, fail)
    assert code(lambda: BrowserSession(profile).__enter__()) == "browser_start_failed"
    assert sdk[0] == (["start"] if stage == "start" else ["start", "launch", "stop"])
    with BrowserLease(profile):
        pass


@needs_native
def test_missing_sdk_releases_claimed_profile_lease(profile, monkeypatch):
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("synthetic missing SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert code(lambda: BrowserSession(profile).__enter__()) == "browser_unavailable"
    with BrowserLease(profile):
        pass


@needs_native
@pytest.mark.parametrize("stage", ["close", "stop"])
def test_shutdown_failure_does_not_grant_another_browser(profile, sdk, stage):
    def fail():
        raise RuntimeError("synthetic close uncertainty")

    session = BrowserSession(profile).__enter__()
    setattr(sdk[1], stage, fail)
    assert code(session.close) == "browser_stop_failed"
    assert code(lambda: BrowserSession(profile).__enter__()) == "browser_busy"
    assert code(session.__enter__) == "browser_busy"
    assert sdk[0].count("start") == 1 and sdk[0].count("launch") == 1
    # An explicit successful close attempt is required before releasing ownership.
    setattr(sdk[1], stage, None)
    session.close()
    session.close()
    with BrowserLease(profile):
        pass


@needs_native
@pytest.mark.parametrize("kind", ["metadata", "metadata_same_bytes", "marker", "marker_same_bytes", "root"])
def test_swap_detection_before_context_reuse_and_sdk_cleanup(profile, sdk, kind):
    session = BrowserSession(profile).__enter__()
    lease = session._lease
    assert lease is not None
    if kind == "root":
        profile.rename(profile.with_name("detached-original"))
        profile.mkdir(mode=0o700)
        with SafeFiles(profile) as files:
            files.write(PROFILE_MARKER, PROFILE_MARKER_BODY)
    else:
        target = PROFILE_MARKER if kind.startswith("marker") else lease.path
        body = (
            PROFILE_MARKER_BODY
            if kind == "marker_same_bytes"
            else lease.body
            if kind == "metadata_same_bytes"
            else b"{}"
        )
        with SafeFiles(profile) as files:
            files.write(target, body, replace=True)
    assert code(lambda: session.context) == "browser_ownership_changed"
    assert code(lambda: session.__exit__(None, None, None)) == "browser_ownership_changed"
    assert sdk[0][-2:] == ["close", "stop"]
    assert session._lease is None


@needs_native
def test_replaced_lock_metadata_still_cannot_launch_second_browser(profile, sdk):
    session = BrowserSession(profile).__enter__()
    lease = session._lease
    assert lease is not None
    with SafeFiles(profile) as files:
        files.write(lease.path, lease.body, replace=True)
    try:
        assert code(lambda: BrowserSession(profile).__enter__()) == "browser_busy"
        assert sdk[0] == ["start", "launch"]
        assert code(session.check) == "browser_ownership_changed"
    finally:
        session.close()


@needs_native
def test_profile_changed_after_validation_gets_no_new_lock_write(profile, monkeypatch):
    import collection_context.infrastructure.browser_ownership as ownership

    original = ownership._FileLease.__init__
    replacement = profile.with_name("replacement")
    replacement.mkdir(mode=0o700)
    with SafeFiles(replacement) as files:
        files.write(PROFILE_MARKER, PROFILE_MARKER_BODY)

    def changed(lease, root, **kwargs):
        profile.rename(profile.with_name("detached"))
        replacement.rename(profile)
        return original(lease, root, **kwargs)

    monkeypatch.setattr(ownership._FileLease, "__init__", changed)
    assert code(lambda: BrowserLease(profile)) == "browser_ownership_changed"
    assert sorted(path.name for path in profile.iterdir()) == [PROFILE_MARKER]


@needs_native
def test_marker_changed_during_sdk_start_prevents_browser_launch(profile, sdk):
    def changed():
        with SafeFiles(profile) as files:
            files.write(PROFILE_MARKER, PROFILE_MARKER_BODY, replace=True)

    sdk[1].start = changed
    assert code(lambda: BrowserSession(profile).__enter__()) == "browser_ownership_changed"
    assert sdk[0] == ["start", "stop"]


@needs_native
def test_two_different_product_profiles_do_not_share_one_global_lock(profile, tmp_path, sdk):
    second = tmp_path / "separate-profile"
    with BrowserSession(profile):
        with BrowserSession(second):
            assert sdk[0].count("launch") == 2


def test_unsupported_runtime_rejected_before_creating_profile(tmp_path, monkeypatch):
    def unsupported():
        raise ContextError("unsupported_platform", "synthetic unavailable ownership")

    monkeypatch.setattr(browser_module, "require_ownership_runtime", unsupported)
    profile = tmp_path / "untouched"
    assert code(lambda: BrowserSession(profile).__enter__()) == "unsupported_platform"
    assert not profile.exists()


@needs_native
@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_BROWSER_LEASE") != "1", reason="opt-in native temporary process lock"
)
def test_actual_child_process_contention_and_kernel_release(profile, sdk):
    """Real Mac/POSIX subprocess, original temporary marker; no Chromium launch."""
    script = (
        "from pathlib import Path; import sys; "
        "from collection_context.infrastructure.browser_ownership import BrowserLease; "
        "lease=BrowserLease(Path(sys.argv[1])); print('owned',flush=True); "
        "sys.stdin.readline(); lease.close()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(profile)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 10)[0], "temporary lease child did not become ready"
        assert child.stdout.readline().strip() == "owned"
        assert code(lambda: BrowserSession(profile).__enter__()) == "browser_busy"
        assert sdk[0] == []
        # Kernel ownership ends with process death, not a PID/time-based takeover.
        child.kill()
        child.wait(timeout=5)
        with BrowserLease(profile):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
