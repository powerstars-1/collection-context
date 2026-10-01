"""Opt-in real OS store, using only disposable synthetic product items.

Never read existing services, keys, or user libraries. Exact items created by this
test are removed after verification; no enumeration, shell, or cloud request.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import uuid

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.model_setup import ModelSetup
from collection_context.infrastructure.macos_credentials import MacOSKeychain
from collection_context.infrastructure.system_secrets import SystemSecrets
from collection_context.library.store import LibraryStore


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("RUN_LOCAL_SYSTEM_KEYCHAIN") != "1",
    reason="opt-in native disposable keychain roundtrip",
)
def test_native_system_configuration_reopen_and_exact_cleanup(tmp_path):
    class OwnedItems(MacOSKeychain):
        def __init__(self):
            super().__init__()
            self.created = []
            self.reads = 0

        def create(self, service, account, value):
            super().create(service, account, value)
            self.created.append((service, account))

        def read(self, service, account):
            self.reads += 1
            return super().read(service, account)

    backend = OwnedItems()
    root = tmp_path / "原创 系统凭据"
    workspace = tmp_path / "原创 配置库"
    store = LibraryStore.initialize(workspace)
    secrets = None
    reopened = None
    try:
        secrets = SystemSecrets.initialize(root, _backend=backend)
        setup = ModelSetup(store, secrets)
        expected = {}
        for role in ("audio", "vision", "summary"):
            value = "synthetic-not-a-provider-key-" + uuid.uuid4().hex
            settings = setup.save(
                role=role,
                base_url="https://models.example.invalid/v1",
                model="synthetic-" + role,
                protocol="chat_audio" if role == "audio" else "chat",
                parameters={},
                timeout=5,
                api_key=value,
                expected_profile_id=None,
                credential_confirmed=True,
            )
            profile = store.snapshot()["model_profiles"][settings["roles"][role]["profile_id"]]
            expected[profile["credential_ref"]] = value
        assert backend.reads == 0
        assert settings["credential_storage"] == "macos_keychain"
        assert settings["model_requests"] == 0
        local_metadata = b"".join(path.read_bytes() for path in root.iterdir())
        public_state = json.dumps(store.snapshot(), ensure_ascii=False).encode()
        plaintext_absent = all(
            value.encode() not in local_metadata and value.encode() not in public_state
            for value in expected.values()
        )
        assert plaintext_absent is True
        secrets.close()
        secrets = None
        # Fresh handle performs native reads from exact registered references.
        reopened = SystemSecrets(root, _backend=OwnedItems())
        verified = all(hmac.compare_digest(reopened.get(ref), value) for ref, value in expected.items())
        assert verified is True
        with pytest.raises(ContextError) as missing:
            reopened.get("k_" + uuid.uuid4().hex)
        assert missing.value.code == "credential_missing"
    finally:
        if reopened is not None:
            reopened.close()
        if secrets is not None:
            secrets.close()
        store.close()
        # Only create() calls confirmed by this exact original test object.
        # Never attempt cleanup of uncertain or pre-existing system entries.
        cleanup_confirmed = True
        for service, account in backend.created:
            try:
                backend.delete(service, account)
                try:
                    backend.read(service, account)
                except ContextError as absent:
                    cleanup_confirmed = cleanup_confirmed and absent.code == "credential_missing"
                else:
                    cleanup_confirmed = False
            except ContextError:
                cleanup_confirmed = False
        assert cleanup_confirmed is True
