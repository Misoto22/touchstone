from __future__ import annotations

import base64
import dataclasses
from pathlib import Path

import pytest

from touchstone.hosted.crypto import (
    BundleIntegrityError,
    BundleManifest,
    decode_state_key,
    decrypt_bundle,
    encrypt_bundle,
)


def _key() -> bytes:
    return bytes(range(32))


def _manifest() -> BundleManifest:
    return BundleManifest(
        repository="acme/widgets",
        loop="code",
        schema_version=2,
        config_digest="config-digest",
        profile_digest="profile-digest",
        lineage="lineage-1",
        run_id="run-1",
        created_at="2026-08-24T12:00:00Z",
    )


def test_snapshot_round_trip_uses_authenticated_encryption(tmp_path: Path) -> None:
    state = tmp_path / "events.jsonl"
    state.write_text("private state", encoding="utf-8")

    bundle = encrypt_bundle(_manifest(), {"events.jsonl": state}, _key())
    restored = decrypt_bundle(bundle, _key(), tmp_path / "restored")

    assert restored.lineage == "lineage-1"
    assert (tmp_path / "restored/events.jsonl").read_text(encoding="utf-8") == "private state"
    assert "private state" not in bundle.to_json()


def test_tampered_ciphertext_or_manifest_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "events.jsonl"
    state.write_text("private state", encoding="utf-8")
    bundle = encrypt_bundle(_manifest(), {"events.jsonl": state}, _key())
    ciphertext = bytearray(bundle.ciphertext)
    ciphertext[0] ^= 1

    with pytest.raises(BundleIntegrityError):
        decrypt_bundle(
            dataclasses.replace(bundle, ciphertext=bytes(ciphertext)), _key(), tmp_path / "a"
        )
    with pytest.raises(BundleIntegrityError):
        decrypt_bundle(
            dataclasses.replace(
                bundle,
                manifest=dataclasses.replace(bundle.manifest, lineage="different"),
            ),
            _key(),
            tmp_path / "b",
        )


def test_key_length_paths_and_symlinks_are_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.write_text("state", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(state)

    with pytest.raises(ValueError, match="32 bytes"):
        encrypt_bundle(_manifest(), {"state": state}, b"short")
    with pytest.raises(ValueError, match="archive path"):
        encrypt_bundle(_manifest(), {"../state": state}, _key())
    with pytest.raises(ValueError, match="symlink"):
        encrypt_bundle(_manifest(), {"state": link}, _key())


def test_state_key_decoder_requires_exact_urlsafe_base64() -> None:
    encoded = base64.urlsafe_b64encode(_key()).decode()
    assert decode_state_key(encoded) == _key()
    with pytest.raises(ValueError, match="32 bytes"):
        decode_state_key(base64.urlsafe_b64encode(b"short").decode())
