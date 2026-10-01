"""Development staging guards: no private workspace or mismatched wheel enters Linux evidence."""

import importlib.util
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "linux_staging", Path(__file__).parents[1] / "tools/stage_linux_acceptance.py"
)
assert spec is not None and spec.loader is not None
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


def fixture_backend(tmp_path):
    backend = tmp_path / "backend"
    namespace = backend / "src/collection_context"
    (namespace / "interfaces/assets").mkdir(parents=True)
    (namespace / "__init__.py").write_text('"""Original sample"""', encoding="utf-8")
    (namespace / "interfaces/assets/index.html").write_text("<main>Original</main>", encoding="utf-8")
    (namespace / ".env.local").write_text("private_fixture_not_copied", encoding="utf-8")
    return backend


def candidate(tmp_path, backend, *, extra=None, missing=None, changed=None):
    wheel = tmp_path / "collection_context-0.2.0.dev0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, source in staging.source_members(backend).items():
            if name != missing:
                archive.writestr(name, b"changed" if name == changed else source.read_bytes())
        archive.writestr("collection_context-0.2.0.dev0.dist-info/METADATA", "Name: collection-context")
        if extra:
            archive.writestr(extra, "unexpected")
    return wheel


def test_whitelist_has_only_original_source_and_assets(tmp_path):
    backend = fixture_backend(tmp_path)
    wheel = candidate(tmp_path, backend)
    members = staging.validate_candidate(wheel, backend)
    assert set(members) == {
        "collection_context/__init__.py",
        "collection_context/interfaces/assets/index.html",
    }
    assert not any("env" in name for name in members)


def test_named_frontend_notice_is_validated_and_staged(tmp_path):
    backend = fixture_backend(tmp_path)
    assets = backend / "src/collection_context/interfaces/assets"
    (assets / "THIRD_PARTY_NOTICES.txt").write_text("Original legal notice", encoding="utf-8")
    (assets / "private.txt").write_text("Not an approved resource", encoding="utf-8")
    wheel = candidate(tmp_path, backend)
    members = staging.validate_candidate(wheel, backend)
    assert "collection_context/interfaces/assets/THIRD_PARTY_NOTICES.txt" in members
    assert "collection_context/interfaces/assets/private.txt" not in members


@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_notice_must_be_present_and_match_source(tmp_path, failure):
    backend = fixture_backend(tmp_path)
    (backend / "src/collection_context/interfaces/assets/THIRD_PARTY_NOTICES.txt").write_text(
        "Original legal notice", encoding="utf-8"
    )
    name = "collection_context/interfaces/assets/THIRD_PARTY_NOTICES.txt"
    wheel = candidate(tmp_path, backend, **{failure: name})
    with pytest.raises(ValueError, match="missing" if failure == "missing" else "does not match"):
        staging.validate_candidate(wheel, backend)


@pytest.mark.parametrize(
    "extra",
    [
        "app/old.py",
        ".env.local",
        "other.dist-info/secret",
        "collection_context/../../secret",
        "collection_context/interfaces/assets/private.txt",
        "collection_context/interfaces/assets/OTHER_NOTICE.txt",
    ],
)
def test_unexpected_members_are_rejected_before_any_staging(tmp_path, extra):
    backend = fixture_backend(tmp_path)
    wheel = candidate(tmp_path, backend, extra=extra)
    with pytest.raises(ValueError, match="unexpected"):
        staging.validate_candidate(wheel, backend)


@pytest.mark.parametrize(
    "name", ["collection_context/__init__.py", "collection_context/interfaces/assets/index.html"]
)
def test_source_or_asset_missing_is_not_install_proof(tmp_path, name):
    backend = fixture_backend(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        staging.validate_candidate(candidate(tmp_path, backend, missing=name), backend)


def test_source_mismatch_is_not_tested_wheel_proof(tmp_path):
    backend = fixture_backend(tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        staging.validate_candidate(
            candidate(tmp_path, backend, changed="collection_context/__init__.py"), backend
        )


def test_source_alias_outside_namespace_is_rejected(tmp_path):
    backend = fixture_backend(tmp_path)
    wheel = candidate(tmp_path, backend)
    source = backend / "src/collection_context/__init__.py"
    saved = tmp_path / "outside.py"
    source.rename(saved)
    source.symlink_to(saved)
    with pytest.raises(ValueError, match="does not match"):
        staging.validate_candidate(wheel, backend)
