"""Authenticated, path-safe hosted state bundles."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
import sqlite3
import tarfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class BundleIntegrityError(ValueError):
    """Ciphertext, manifest binding, or archive membership is invalid."""


@dataclass(frozen=True, slots=True)
class BundleManifest:
    repository: str
    loop: str
    schema_version: int
    config_digest: str
    profile_digest: str
    lineage: str
    run_id: str
    created_at: str
    files: tuple[str, ...] = ()
    version: int = 1


@dataclass(frozen=True, slots=True)
class EncryptedBundle:
    manifest: BundleManifest
    nonce: bytes
    ciphertext: bytes
    ciphertext_digest: str

    def to_json(self) -> str:
        payload = {
            "version": 1,
            "manifest": asdict(self.manifest),
            "nonce": _encode(self.nonce),
            "ciphertext": _encode(self.ciphertext),
            "ciphertext_digest": self.ciphertext_digest,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> EncryptedBundle:
        try:
            payload = json.loads(raw)
            manifest_raw = dict(payload["manifest"])
            manifest_raw["files"] = tuple(manifest_raw.get("files", ()))
            return cls(
                BundleManifest(**manifest_raw),
                _decode(str(payload["nonce"])),
                _decode(str(payload["ciphertext"])),
                str(payload["ciphertext_digest"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BundleIntegrityError("encrypted bundle envelope is invalid") from exc


def decode_state_key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded.encode(), altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("TOUCHSTONE_STATE_KEY must be URL-safe base64") from exc
    _validate_key(key)
    return key


def encrypt_bundle(
    manifest: BundleManifest,
    files: Mapping[str, Path],
    key: bytes,
) -> EncryptedBundle:
    _validate_key(key)
    names = tuple(sorted(files))
    for name, path in files.items():
        _validate_archive_path(name)
        if path.is_symlink():
            raise ValueError(f"bundle source is a symlink: {path}")
        if not path.is_file():
            raise ValueError(f"bundle source is not a regular file: {path}")
    bound = replace(manifest, files=names)
    payload = _tar_payload(files)
    nonce = secrets.token_bytes(12)
    aad = _manifest_bytes(bound)
    ciphertext = AESGCM(key).encrypt(nonce, payload, aad)
    digest = f"sha256:{hashlib.sha256(ciphertext).hexdigest()}"
    return EncryptedBundle(bound, nonce, ciphertext, digest)


def decrypt_bundle(
    bundle: EncryptedBundle,
    key: bytes,
    destination: Path,
) -> BundleManifest:
    _validate_key(key)
    actual = f"sha256:{hashlib.sha256(bundle.ciphertext).hexdigest()}"
    if not secrets.compare_digest(actual, bundle.ciphertext_digest):
        raise BundleIntegrityError("ciphertext digest does not match")
    try:
        payload = AESGCM(key).decrypt(
            bundle.nonce,
            bundle.ciphertext,
            _manifest_bytes(bundle.manifest),
        )
    except InvalidTag as exc:
        raise BundleIntegrityError("bundle authentication failed") from exc
    _restore_tar(payload, bundle.manifest.files, destination)
    return bundle.manifest


def _tar_payload(files: Mapping[str, Path]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in sorted(files):
            content = _consistent_bytes(files[name])
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def _consistent_bytes(path: Path) -> bytes:
    if path.suffix not in {".sqlite", ".sqlite3", ".db"}:
        return path.read_bytes()
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    destination = sqlite3.connect(":memory:")
    try:
        source.backup(destination)
        return destination.serialize()
    finally:
        source.close()
        destination.close()


def _restore_tar(payload: bytes, expected: tuple[str, ...], destination: Path) -> None:
    root = destination.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                _validate_archive_path(member.name)
                if not member.isfile():
                    raise BundleIntegrityError("bundle contains a non-regular archive member")
                target = (root / member.name).resolve()
                if not target.is_relative_to(root):
                    raise BundleIntegrityError("bundle archive path escapes destination")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BundleIntegrityError("bundle archive member cannot be read")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(extracted.read())
                seen.append(member.name)
    except tarfile.TarError as exc:
        raise BundleIntegrityError("bundle archive is invalid") from exc
    if tuple(sorted(seen)) != tuple(sorted(expected)):
        raise BundleIntegrityError("bundle archive membership does not match manifest")


def _manifest_bytes(manifest: BundleManifest) -> bytes:
    return json.dumps(
        asdict(manifest),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("hosted state key must be exactly 32 bytes")


def _validate_archive_path(name: str) -> None:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or name.endswith("/"):
        raise ValueError(f"invalid bundle archive path {name!r}")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


def _decode(value: str) -> bytes:
    return base64.b64decode(value.encode(), altchars=b"-_", validate=True)


__all__ = [
    "BundleIntegrityError",
    "BundleManifest",
    "EncryptedBundle",
    "decode_state_key",
    "decrypt_bundle",
    "encrypt_bundle",
]
