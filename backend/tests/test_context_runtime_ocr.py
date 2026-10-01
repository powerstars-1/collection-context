"""Original inert model fixtures; never confuse snapshot guards with OCR quality."""

import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from collection_context.application import runtime_setup as setup
from collection_context.application.contracts import ContextError
from collection_context.infrastructure import runtime_download as downloads
from collection_context.infrastructure import runtime_ocr as ocr
from collection_context.infrastructure import runtime_ocr_layout as layout
from collection_context.infrastructure.runtime_dependencies import RuntimeDependencies, _host
from collection_context.infrastructure.runtime_installation import ArtifactPlan, RuntimeInstaller, ToolSpec


@pytest.fixture
def installed(tmp_path, monkeypatch):
    models = {
        role: (filename, len(body := ("original inert " + role).encode()), hashlib.sha256(body).hexdigest())
        for role, (filename, _, _) in ocr.OCR_MODELS.items()
    }
    monkeypatch.setattr(ocr, "OCR_MODELS", models)
    monkeypatch.setattr(ocr, "ocr_engine_state", lambda: "available_not_functionally_verified")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for role, (filename, _, _) in models.items():
            member = zipfile.ZipInfo("models/" + filename)
            member.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(member, ("original inert " + role).encode())
    body = stream.getvalue()
    path = tmp_path / "original.zip"
    path.write_bytes(body)
    host = _host()
    plan = ArtifactPlan(
        id="original-ocr",
        host_system=host["system"],
        host_arch=host["arch"],
        version="3.9.2",
        source_url=ocr.OCR_SOURCE,
        bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        archive_type="zip",
        tools=tuple(
            ToolSpec(
                role, "models/" + name, size, digest, "3.9.2", "LicenseRef-OCR-Development", ocr.OCR_BUILD
            )
            for role, (name, size, digest) in models.items()
        ),
    )
    root, library = tmp_path / "runtime", tmp_path / "absent-library"
    installer = RuntimeInstaller(
        root, library_dir=library, catalog={plan.id: plan}, _archive_source=lambda _: path
    )
    report = installer.install(plan.id, installation_confirmed=True)
    return root, library, plan, report


def test_installed_model_triple_is_non_executable_and_descriptor_snapshot(installed):
    root, library, _, report = installed
    assert report["installed_roles"] == sorted(ocr.OCR_MODELS) and report["functional_verified"] is False
    registry = RuntimeDependencies(root, library_dir=library)
    for role in ocr.OCR_MODELS:
        model = registry.resolve(role)
        assert stat.S_IMODE(model.path.stat().st_mode) == 0o600
        assert registry.read_model(role) == ("original inert " + role).encode()
        assert model.functional_verified is False
    assert not library.exists()


def test_loader_uses_private_copies_then_removes_them(installed, monkeypatch):
    root, library, _, _ = installed
    captured = []

    def engine(path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert {p.name for p in path.iterdir()} == {name for name, _, _ in ocr.OCR_MODELS.values()}
        captured.append(path)
        return SimpleNamespace(state="test-engine-not-quality-proof")

    monkeypatch.setattr(ocr, "CpuOcr", engine)
    assert ocr.load_runtime_ocr(root, library_dir=library).state == "test-engine-not-quality-proof"
    assert len(captured) == 1 and not captured[0].exists() and not library.exists()


@pytest.mark.parametrize("mutation", ["partial", "build", "parent", "execute", "bytes", "link", "hardlink"])
def test_model_receipt_or_file_mutation_never_loads_engine(installed, monkeypatch, tmp_path, mutation):
    root, library, _, _ = installed
    registry = RuntimeDependencies(root, library_dir=library)
    path = registry.resolve("ocr_det").path
    receipt_path = root / "runtime-dependencies.json"
    receipt = json.loads(receipt_path.read_bytes())
    if mutation == "partial":
        del receipt["tools"]["ocr_cls"]
    elif mutation == "build":
        receipt["tools"]["ocr_cls"]["build_version"] = "another-generation"
    elif mutation == "parent":
        receipt["tools"]["ocr_cls"]["relative_path"] = "other/models/cls.onnx"
    elif mutation == "execute":
        path.chmod(0o700)
    elif mutation == "bytes":
        path.write_bytes(b"x" * path.stat().st_size)
    elif mutation == "link":
        path.unlink()
        path.symlink_to(tmp_path / "outside.onnx")
    else:
        os.link(path, tmp_path / "hardlink.onnx")
    receipt_path.write_text(json.dumps(receipt))
    monkeypatch.setattr(ocr, "CpuOcr", lambda _: pytest.fail("invalid resource loaded"))
    with pytest.raises(ContextError):
        ocr.load_runtime_ocr(root, library_dir=library)
    assert not library.exists()


@pytest.mark.parametrize("mutation", ["partial", "build", "parent", "oversize"])
def test_incomplete_or_mixed_model_plan_refused_before_install(installed, mutation):
    _, _, plan, _ = installed
    tools = list(plan.tools)
    if mutation == "partial":
        tools.pop()
    elif mutation == "build":
        tools[0] = replace(tools[0], build_version="different")
    elif mutation == "parent":
        tools[0] = replace(tools[0], relative_path="another/det.onnx")
    else:
        tools[0] = replace(tools[0], bytes=64_000_001)
    with pytest.raises(ContextError):
        RuntimeInstaller._validate(replace(plan, tools=tuple(tools)))


@pytest.mark.parametrize("role", ["ffmpeg", "bash", "", [], None])
def test_model_snapshot_api_cannot_be_used_for_executables(installed, role):
    root, library, _, _ = installed
    with pytest.raises(ContextError):
        RuntimeDependencies(root, library_dir=library).read_model(role)


@pytest.mark.parametrize("state", ["ocr_runtime_missing", "ocr_runtime_version_mismatch"])
def test_missing_or_changed_engine_rejected_before_reading_models(tmp_path, monkeypatch, state):
    monkeypatch.setattr(ocr, "ocr_engine_state", lambda: state)
    monkeypatch.setattr(ocr, "RuntimeDependencies", lambda *a, **kw: pytest.fail("models read too soon"))
    with pytest.raises(ContextError, match="固定CPU"):
        ocr.load_runtime_ocr(tmp_path / "runtime", library_dir=tmp_path / "library")
    assert not list(tmp_path.iterdir())


def test_bundled_ocr_install_source_never_constructs_network(tmp_path, monkeypatch):
    marker = tmp_path / "fixed-compiled-resource"
    monkeypatch.setattr(downloads, "bundled_ocr_archive", lambda: marker)
    monkeypatch.setattr(downloads, "PublicHTTP", lambda *a, **kw: pytest.fail("network constructed"))
    plan = setup.CATALOG[layout.OCR_ID]
    with downloads.RuntimeDownloads(tmp_path, catalog={plan.id: plan}) as source:
        assert source(plan) == marker
    changed = replace(plan, source_url="https://example.org/wrong")
    with downloads.RuntimeDownloads(tmp_path, catalog={changed.id: changed}) as source:
        with pytest.raises(ContextError):
            source(changed)
    assert not list(tmp_path.iterdir())


def test_ocr_options_require_own_engine_and_bundle_not_media(monkeypatch):
    monkeypatch.setattr(setup, "_host_availability", lambda: "available")
    monkeypatch.setattr(setup, "ocr_engine_state", lambda: "ocr_runtime_missing")
    monkeypatch.setattr(setup, "bundled_ocr_state", lambda: pytest.fail("missing engine bundle inspected"))
    assert setup._ocr_availability() == "ocr_runtime_missing"
    monkeypatch.setattr(setup, "ocr_engine_state", lambda: "available_not_functionally_verified")
    monkeypatch.setattr(setup, "bundled_ocr_state", lambda: "bundled_component_missing")
    assert setup._ocr_availability() == "bundled_component_missing"
    monkeypatch.setattr(setup, "bundled_ocr_state", lambda: "available")
    assert setup._ocr_availability() == "available"
