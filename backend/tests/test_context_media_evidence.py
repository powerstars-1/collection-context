from __future__ import annotations

import base64
import hashlib
from contextlib import closing

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.media_evidence import MediaEvidence
from collection_context.infrastructure.media import FrameCandidate, PreparedFrame
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs

# Synthetic raster protocol fixture, not actual OCR or visual-model evidence.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZ1kAAAAASUVORK5CYII="
)


@pytest.fixture
def media(tmp_path):
    with closing(LibraryStore.initialize(tmp_path / "图文演示库")) as store:
        item = store.upsert(
            {"native_id": "91000007", "title": "原创图文", "media_type": "image"},
            kind="link",
            scope_id="s_link",
        )["item"]
        identity = PreparedInputs(store).prepare_images(item["id"], [(PNG, "image/png"), (PNG, "image/png")])
        yield store, item["id"], identity


def test_ordered_pages_zero_writes_and_no_internal_paths(media):
    store, ref, identity = media
    before = store.snapshot()
    reader = MediaEvidence(store)
    listing = reader.listing(ref)
    assert listing["model_requests"] == 0 and listing["accuracy"] == "not_verified"
    assert [f["page_number"] for f in listing["frames"]] == [1, 2]
    assert "path" not in str(listing) and "content-vault" not in str(listing)
    assert reader.image(ref, identity, "f_000000") == (PNG, "image/png")
    assert store.snapshot() == before


def test_no_media_does_not_download_or_infer(media):
    store, _, _ = media
    item = store.upsert({"native_id": "91000008", "title": "尚未准备"}, kind="link", scope_id="s_link")[
        "item"
    ]
    with pytest.raises(ContextError, match="尚未准备"):
        MediaEvidence(store).listing(item["id"])


def test_unknown_or_wrong_frame_version_is_rejected(media):
    store, ref, identity = media
    reader = MediaEvidence(store)
    with pytest.raises(ContextError) as err:
        reader.image(ref, "u_" + "0" * 64, "f_000000")
    assert err.value.code == "version_changed"
    with pytest.raises(ContextError) as err:
        reader.image(ref, identity, "f_999999")
    assert err.value.code == "not_found"


def test_excluded_reference_cannot_read_existing_frame_url(media):
    store, ref, identity = media
    store.exclude(ref, True)
    with pytest.raises(ContextError) as err:
        MediaEvidence(store).image(ref, identity, "f_000000")
    assert err.value.code == "not_found"


def test_changed_frame_is_not_returned(media):
    store, ref, identity = media
    payload = PreparedInputs(store).load(identity)
    path = payload["frames"][0]["blob"]["path"]
    (store.files.root / path).write_bytes(b"<html>not an image</html>")
    with pytest.raises(ContextError) as err:
        MediaEvidence(store).image(ref, identity, "f_000000")
    assert err.value.code == "media_changed"


def test_raster_only_and_path_traversal(media):
    store, ref, identity = media
    reader = MediaEvidence(store)
    for invalid in ("../../x", "/etc/passwd", "f_1?path=secret"):
        with pytest.raises(ContextError):
            reader.image(ref, identity, invalid)
    other = store.upsert(
        {"native_id": "91000009", "title": "伪图片", "media_type": "image"},
        kind="link",
        scope_id="s_link",
    )["item"]
    other_input = PreparedInputs(store).prepare_images(
        other["id"], [(b'<svg onload="alert(1)"/>', "image/png")]
    )
    with pytest.raises(ContextError) as err:
        reader.image(other["id"], other_input, "f_000000")
    assert err.value.code == "unsupported_media"


def test_frame_read_does_not_load_retained_video(media, monkeypatch):
    store, _, _ = media
    item = store.upsert(
        {"native_id": "91000010", "title": "原视频读取隔离", "media_type": "video"},
        kind="link",
        scope_id="s_link",
    )["item"]
    inputs = PreparedInputs(store)
    frame = PreparedFrame(
        FrameCandidate("f_000001", 1, 20, ("fixture",), 0),
        PNG,
        hashlib.sha256(PNG).hexdigest(),
        "image/png",
        None,
    )
    identity = inputs.save(
        item["id"],
        content_hash=item["content_hash"],
        originals=[(b"synthetic retained video", "video/mp4")],
        audio=[],
        frames=[frame],
        coverage={"has_audio": False},
        processor_version="fixture",
        strategy_hash="fixture",
        kind="video",
    )
    original = inputs.manifest(identity)["originals"][0]["path"]
    read = store.files.read

    def checked(path, **kwargs):
        assert path != original, "frame preview must not load the retained video"
        return read(path, **kwargs)

    monkeypatch.setattr(store.files, "read", checked)
    reader = MediaEvidence(store)
    assert reader.listing(item["id"])["frames"][0]["seconds"] == 20
    assert reader.image(item["id"], identity, "f_000001")[0] == PNG


def test_http_frames_require_auth_and_recheck_exclusion(media):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from collection_context.interfaces.http import create_app
    from collection_context.interfaces.security import AccessPolicy, Credential

    store, ref, identity = media
    token = "fixture_" + "r" * 40
    policy = AccessPolicy("http://127.0.0.1:8787", [Credential.from_token("p_reader", token)])
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(store.files.root, policy), base_url=policy.origin) as client:
        route = f"/v1/collections/{ref}/frames"
        image = route + f"/{identity}/f_000000"
        assert client.get(image).status_code == 401
        result = client.get(route, headers=headers)
        assert result.status_code == 200
        response = client.get(image, headers=headers)
        assert response.content == PNG and response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get(image + "?path=/etc/passwd", headers=headers).status_code == 400
        store.exclude(ref, True)
        assert client.get(image, headers=headers).status_code == 404
