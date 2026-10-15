"""Shared harness for the browser tests (tests/test_browser_*.py).

Starts a throwaway seeded sizer (tools/manual_devserver.py on a temp SQLite
file, free port) and a headless Chrome through Playwright. Both are optional
on a developer machine: when Playwright or a Chrome binary is missing, or the
browser cannot start (a sandboxed shell), the tests are skipped, not failed.

Chrome is looked up in .tools/ (tools/shots.sh fetches it there) and then in
Playwright's own cache.
"""
import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PW = "Manual-Pass1!"
USER = "user@partnerco.example"       # owner of what the tests create
TADMIN = "admin@partnerco.example"    # same tenant: can view, cannot edit


def _chrome():
    pats = [os.path.join(ROOT, ".tools", "chrome-headless-shell-*", "chrome-headless-shell"),
            os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/"
                               "chrome-headless-shell-*/chrome-headless-shell")]
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("browser") / "sizer.db")
    port = _free_port()
    env = dict(os.environ, MANUAL_PW=PW)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "tools", "manual_devserver.py"), db,
         "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(240):
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            break
        except Exception:
            if proc.poll() is not None:
                pytest.skip("dev server did not start")
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.skip("dev server did not come up")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    exe = _chrome()
    if not exe:
        pytest.skip("no headless Chrome (run tools/shots.sh once to fetch it)")
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch(executable_path=exe)
    except Exception as exc:     # sandboxed shell, missing libs, ...
        pw.stop()
        pytest.skip("Chrome cannot start here: %s" % str(exc).splitlines()[0])
    yield b
    b.close()
    pw.stop()


class Session:
    """One signed-in browser context; ``api`` calls run inside the page, so
    they are same-origin and carry the session cookie like the app's own."""

    def __init__(self, browser, base, email):
        self.base = base
        self.ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(base + "/")
        r = self.api("POST", "/api/auth/login", {"email": email, "password": PW})
        assert r["ok"], r

    def api(self, method, path, body=None):
        return self.page.evaluate(
            """async ([method, path, body]) => {
                const opts = {method, headers: {'Content-Type': 'application/json'}};
                if (body !== null) opts.body = JSON.stringify(body);
                const r = await fetch(path, opts);
                let data = null;
                try { data = await r.json(); } catch (e) {}
                return {ok: r.ok, status: r.status, data};
            }""", [method, path, body])

    def goto(self, path, settle=800):
        self.page.goto(self.base + path)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(settle)

    def close(self):
        self.ctx.close()


def dumps(o):
    return json.dumps(o, sort_keys=True)
