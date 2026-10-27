"""Browser test: an assistant's connection request reaches the consent page
through the normal login (docs/mcp/03-auth.md).

An anonymous visit to /oauth/authorize goes to the shell with a return path;
after signing in the browser lands on the consent page, which names the
client; Allow sends the browser to the client's callback with a code.
Skipped when no headless Chrome can start.

Run: .venv/bin/python -m pytest tests/test_browser_oauth_login.py -q
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.request
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import PW, USER, Session, browser, server  # noqa: E402,F401


def _register(base):
    body = json.dumps({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                       "client_name": "Claude", "token_endpoint_auth_method": "none"}).encode()
    req = urllib.request.Request(base + "/oauth/register", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["client_id"]


def test_login_returns_to_the_consent_page(server, browser):
    s = Session(browser, server, USER)
    client_id = _register(server)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    authorize = ("/oauth/authorize?response_type=code&client_id=%s&redirect_uri=%s&state=s1"
                 "&code_challenge=%s&code_challenge_method=S256&scope=sizing"
                 % (client_id, "https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback", challenge))
    # Signed out: the shell, with the way back in the URL.
    s.page.evaluate("fetch('/api/auth/logout', {method: 'POST'})")
    s.page.wait_for_timeout(500)
    s.page.goto(server + authorize)
    s.page.wait_for_timeout(1500)
    assert "next=" in s.page.url and s.page.url.startswith(server + "/?")
    # Sign in through the modal: the page goes to the consent page.
    s.page.fill("#auth-email", USER)
    s.page.fill("#auth-password", PW)
    s.page.click("#auth-submit")
    s.page.wait_for_url("**/oauth/authorize**", timeout=10000)
    assert "Claude" in s.page.content() and "claude.ai" in s.page.content()
    # Allow: off to the callback with a code. The callback is intercepted so
    # the browser never reaches claude.ai (which would redirect onwards).
    s.page.route("https://claude.ai/**",
                 lambda route: route.fulfill(status=200, content_type="text/plain", body="ok"))
    with s.page.expect_request("https://claude.ai/**", timeout=10000) as hit:
        s.page.click("button[value=allow]")
    target = hit.value.url
    assert target.startswith("https://claude.ai/api/mcp/auth_callback?")
    q = {k: v[0] for k, v in parse_qs(urlparse(target).query).items()}
    assert q["state"] == "s1" and q["code"] and q["iss"] == server
    s.close()
