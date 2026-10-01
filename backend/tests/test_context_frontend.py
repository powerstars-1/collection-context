"""React UI keeps one original backend and its authentication boundaries."""
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "src/collection_context/interfaces/assets"
FRONTEND = Path(__file__).parents[2] / "frontend"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if "id" in fields:
            self.ids.append(fields["id"])
        if tag == "script" and "src" in fields:
            self.resources.append(fields["src"])
        if tag == "link" and fields.get("rel") == "stylesheet":
            self.resources.append(fields["href"])


def test_react_shell_is_static_and_resources_are_offline():
    page = Page()
    source = (ROOT / "index.html").read_text(encoding="utf-8")
    page.feed(source)
    assert page.resources == ["/assets/app.css", "/assets/app.js"]
    assert page.ids == ["root"]
    assert 'type="module"' in source
    assert source == (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert (ROOT / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8") == (
        FRONTEND / "THIRD_PARTY_NOTICES.md"
    ).read_text(encoding="utf-8")


def test_original_owner_components_and_shared_transport_are_used():
    app = (FRONTEND / "src/App.jsx").read_text(encoding="utf-8")
    library = (FRONTEND / "src/Library.jsx").read_text(encoding="utf-8")
    for component in ("<TopNav", "<SubNav", "<LegacyPanels", "<Library", "<Access"):
        assert component in app
    assert "<WorkbenchLayout" in library and "<MaterialList" in library
    assert "dangerouslySetInnerHTML" not in app + library
    api = (FRONTEND / "src/api.js").read_text(encoding="utf-8")
    assert "'X-CSRF-Token': csrf" in api
    assert "credentials: 'same-origin'" in api
    assert "localStorage" not in api and "https://" not in api
    assert "画面文字" in library and "source_url" in library


def test_shipped_styles_keep_components_and_reduced_motion():
    css = (ROOT / "app.css").read_text(encoding="utf-8")
    assert "xl\\:grid-cols-" in css
    assert "prefers-reduced-motion" in css
    assert "display: none !important" in css
    assert "rounded-3xl" in css and "rounded-2xl" in css


def test_ui_routes_preserve_csp_and_backend_authentication(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from collection_context.interfaces.http import create_app
    from collection_context.interfaces.security import AccessPolicy, Credential
    from collection_context.library.store import LibraryStore

    store = LibraryStore.initialize(tmp_path / "演示库")
    try:
        policy = AccessPolicy("http://127.0.0.1:8787", [Credential.from_token("p_fixture", "x" * 40)])
        with TestClient(create_app(store.files.root, policy), base_url=policy.origin) as client:
            for route in ("/", "/connect", "/activity", "/settings", "/access"):
                response = client.get(route)
                assert response.status_code == 200
                assert 'id="root"' in response.text
                assert "script-src 'self'" in response.headers["content-security-policy"]
            assert client.get("/assets/app.css").status_code == 200
            assert client.get("/assets/app.js").status_code == 200
            assert client.get("/v1/collections/overview").status_code == 401
            assert client.get("/assets/http.py").status_code == 404
    finally:
        store.close()
