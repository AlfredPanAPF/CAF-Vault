"""The login-form fill (graph.signin._fill_login) against a real login form in
a real Chromium. Opt-in, since it needs Playwright's Chromium:

    CAF_E2E=1 uv run pytest tests/test_signin_fill.py -q

No network and no database: the form is loaded from an inline string and its
onsubmit records what was typed. This proves the selectors and the one-page /
two-step state machine against a real DOM; the sidecar orchestration around it
is covered with fakes in tests/test_signin.py.
"""
import asyncio
import os

import pytest

from graph import signin

pytestmark = pytest.mark.skipif(
    not os.environ.get("CAF_E2E"), reason="opt-in browser test (CAF_E2E=1)")

EMAIL = "analyst@example.com"
PASSWORD = "correct-horse-battery-staple"

# a one-page form: both fields visible at once. onsubmit records "email|password".
SINGLE = """<!doctype html><meta charset="utf-8"><title>Sign in</title>
<form onsubmit="window.__submitted=email.value+'|'+password.value;return false">
  <input type="email" id="email" name="email" autocomplete="username">
  <input type="password" id="password" name="password" autocomplete="current-password">
  <button type="submit">Sign in</button>
</form>"""

# a two-step form: the password field is hidden until the email step is sent,
# the way accounts.ft.com and the Dow Jones SSO split it
TWO_STEP = """<!doctype html><meta charset="utf-8"><title>Sign in</title>
<form onsubmit="return step()">
  <input type="email" id="email" name="email">
  <input type="password" id="password" name="password" style="display:none">
  <button type="submit" id="btn">Continue</button>
</form>
<script>
  var stage = 1;
  function step() {
    if (stage === 1) { password.style.display = ""; stage = 2; btn.textContent = "Sign in"; return false; }
    window.__submitted = email.value + "|" + password.value;
    return false;
  }
</script>"""

NO_FORM = '<!doctype html><meta charset="utf-8"><title>Checking</title><h1>One moment</h1>'


async def _fill(html):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            await signin._fill_login(page, EMAIL, PASSWORD)
            return await page.evaluate("window.__submitted || null")
        finally:
            await browser.close()


def test_fill_one_page_form():
    assert asyncio.run(_fill(SINGLE)) == f"{EMAIL}|{PASSWORD}"


def test_fill_two_step_form():
    assert asyncio.run(_fill(TWO_STEP)) == f"{EMAIL}|{PASSWORD}"


def test_fill_raises_when_there_is_no_form(monkeypatch):
    # a challenge page instead of the form: the email field never appears, and
    # _fill_login raises so the caller hands back the live view
    monkeypatch.setattr(signin, "FIELD_MS", 800)
    monkeypatch.setattr(signin, "STEP_MS", 800)
    with pytest.raises(Exception):
        asyncio.run(_fill(NO_FORM))
