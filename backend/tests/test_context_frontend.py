"""RedNote Desk visual migration must keep the original product/auth boundaries."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "src/collection_context/interfaces/assets"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.resources = []
        self.navigation = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if "id" in fields:
            self.ids.append(fields["id"])
        if tag == "script" and "src" in fields:
            self.resources.append(fields["src"])
        if tag == "link" and fields.get("rel") == "stylesheet":
            self.resources.append(fields["href"])
        if tag == "a" and "data-page" in fields:
            self.navigation.append((fields["href"], fields["data-page"]))


def test_rednote_shell_keeps_three_pages_and_offline_assets():
    page = Page()
    text = (ROOT / "index.html").read_text(encoding="utf-8")
    page.feed(text)
    assert len(page.ids) == len(set(page.ids))
    assert page.resources == ["/assets/app.css", "/assets/app.js"]
    assert page.navigation == [("/", "materials"), ("/connect", "connect"), ("/activity", "activity")]
    for expected in ("hero-count", "hero-pending", "hero-jobs", "login-form", "source-list", "task-list"):
        assert expected in page.ids
    assert 'class="app-shell"' in text and 'class="hero-card card"' in text
    assert "shared-cookie" not in text and "发布中心" not in text


def test_rednote_theme_and_safe_product_script_remain_separate():
    css = (ROOT / "app.css").read_text(encoding="utf-8")
    script = (ROOT / "app.js").read_text(encoding="utf-8")
    assert "--bg: #f5ebde" in css and "--accent: #e9573f" in css
    assert "--teal: #136f67" in css and "207e2baad766cd2015f9577076ae7e0eacb9921c" in css
    assert "@media (max-width: 680px)" in css and "prefers-reduced-motion" in css
    assert "localStorage" not in script and "innerHTML" not in script
    assert '"X-CSRF-Token": csrf' in script and 'credentials: "same-origin"' in script


def test_served_rednote_assets_preserve_csp_and_require_session(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from collection_context.interfaces.http import create_app
    from collection_context.interfaces.security import AccessPolicy, Credential
    from collection_context.library.store import LibraryStore

    store = LibraryStore.initialize(tmp_path / "演示库")
    try:
        policy = AccessPolicy("http://127.0.0.1:8787", [Credential.from_token("p_fixture", "x" * 40)])
        with TestClient(create_app(store.files.root, policy), base_url=policy.origin) as client:
            for route in ("/", "/connect", "/activity"):
                response = client.get(route)
                assert response.status_code == 200
                assert "hero-card" in response.text
                assert "script-src 'self'" in response.headers["content-security-policy"]
            assert client.get("/assets/app.css").status_code == 200
            assert client.get("/assets/app.js").status_code == 200
            assert client.get("/v1/collections/overview").status_code == 401
    finally:
        store.close()
