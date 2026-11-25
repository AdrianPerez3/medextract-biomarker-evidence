"""Ed25519 key handling and the DSSE envelope for the v1 bundle manifest."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import tempfile

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .bundle_format import PAYLOAD_TYPE


class SignatureError(ValueError):
    def __init__(self, message, *, supplied_keyid=None, claimed_keyid=None):
        super().__init__(message)
        self.supplied_keyid = supplied_keyid
        self.claimed_keyid = claimed_keyid


def _pae(media_type: bytes, payload: bytes) -> bytes:
    return b" ".join((b"DSSEv1", str(len(media_type)).encode("ascii"), media_type,
                      str(len(payload)).encode("ascii"), payload))


def _fingerprint(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _key_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError("Expected a regular key file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError("Cannot read the configured key file") from exc


def load_public_key(path: Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(_key_bytes(path))
    except UnsupportedAlgorithm as exc:
        raise ValueError("Expected an Ed25519 public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Expected an Ed25519 public key")
    return key


def _private(path: Path, source_bytes=()) -> Ed25519PrivateKey:
    pem = _key_bytes(path)
    if pem.strip() and any(pem.strip() in raw for raw in source_bytes):
        raise ValueError("a captured source contains the signing key")
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ValueError("Expected an unencrypted Ed25519 PKCS#8 PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Expected an Ed25519 private key")
    return key


def _publish_once(path: Path, data: bytes) -> bool:
    """Publish complete bytes without replacing a file created by another process."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".key-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def generate_keypair(private_key: Path, public_key: Path) -> str:
    """Create a new identity or reuse the existing matching pair at these paths."""
    private_path, public_path = private_key.resolve(), public_key.resolve()
    if (private_key.is_symlink() or public_key.is_symlink()
            or private_path.is_relative_to(public_path) or public_path.is_relative_to(private_path)):
        raise ValueError("Choose two separate regular files for the private and public keys")
    if not private_key.exists():
        if public_key.exists() and not private_key.exists():
            raise ValueError("A public key already exists without its private key; choose a new pair of paths")
        candidate = Ed25519PrivateKey.generate()
        private_bytes = candidate.private_bytes(serialization.Encoding.PEM,
                                                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        _publish_once(private_key, private_bytes)
    key = _private(private_key).public_key()
    public_bytes = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = _fingerprint(key)
    if public_key.exists() or not _publish_once(public_key, public_bytes):
        if _fingerprint(load_public_key(public_key)) != fingerprint:
            raise ValueError("Existing public key differs from the private key; both files were preserved")
    return fingerprint


def sign_manifest(payload: bytes, private_key: Path, *, source_bytes=()) -> dict:
    key = _private(private_key, source_bytes)
    signature = key.sign(_pae(PAYLOAD_TYPE.encode("ascii"), payload))
    return {"payloadType": PAYLOAD_TYPE, "payload": base64.b64encode(payload).decode("ascii"),
            "signatures": [{"keyid": _fingerprint(key.public_key()),
                            "sig": base64.b64encode(signature).decode("ascii")}]}


def _decode64(value) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Expected a base64 string")
    return base64.b64decode(value, altchars=b"-_", validate=True)


def verify_signature(envelope: dict, public_key: Path | Ed25519PublicKey) -> tuple[bytes, str]:
    """Verify the typed bytes against the recipient's Ed25519 key and return those same bytes."""
    supplied = claimed = None
    try:
        if not isinstance(envelope, dict):
            raise ValueError("Expected a DSSE envelope")
        signatures = envelope.get("signatures")
        if not isinstance(signatures, list) or len(signatures) != 1 or not isinstance(signatures[0], dict):
            raise ValueError("Expected one signature in the v1 DSSE envelope")
        entry = signatures[0]
        claimed = entry.get("keyid")
        if not isinstance(claimed, str):
            claimed = None
            raise ValueError("Missing manifest signer fingerprint")
        key = public_key if isinstance(public_key, Ed25519PublicKey) else load_public_key(public_key)
        supplied = _fingerprint(key)
        if claimed != supplied:
            raise ValueError("Supplied public key differs from the claimed signer")
        if envelope.get("payloadType") != PAYLOAD_TYPE:
            raise ValueError("Unsupported manifest payload type")
        payload = _decode64(envelope.get("payload"))
        key.verify(_decode64(entry.get("sig")), _pae(PAYLOAD_TYPE.encode("ascii"), payload))
        return payload, supplied
    except InvalidSignature as exc:
        raise SignatureError("Manifest signature does not match the supplied public key",
                             supplied_keyid=supplied, claimed_keyid=claimed) from exc
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise SignatureError(str(exc), supplied_keyid=supplied, claimed_keyid=claimed) from exc
