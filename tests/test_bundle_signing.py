import base64
from concurrent.futures import ThreadPoolExecutor
import stat

from cryptography.hazmat.primitives import serialization
import pytest

from medextract_dossier import signing
from medextract_dossier.bundle_format import PAYLOAD_TYPE


def test_pae_follows_the_dsse_reference_vector():
    assert signing._pae(b"http://example.com/HelloWorld", b"hello world") == (
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world")


def test_pair_is_reusable_and_missing_public_copy_can_be_recovered(tmp_path):
    private, public = tmp_path / "key.pem", tmp_path / "pub.pem"
    with ThreadPoolExecutor(max_workers=5) as pool:
        ids = list(pool.map(lambda _: signing.generate_keypair(private, public), range(10)))
    assert len(set(ids)) == 1
    assert stat.S_IMODE(private.stat().st_mode) & 0o077 == 0
    old_private, old_public = private.read_bytes(), public.read_bytes()
    public.unlink()
    assert signing.generate_keypair(private, public) == ids[0]
    assert old_private == private.read_bytes() and old_public == public.read_bytes()


def test_incompatible_public_key_is_preserved(tmp_path):
    private, public = tmp_path / "key.pem", tmp_path / "pub.pem"
    signing.generate_keypair(tmp_path / "other", public)
    prior = public.read_bytes()
    with pytest.raises((ValueError, OSError)):
        signing.generate_keypair(private, public)
    assert public.read_bytes() == prior and not private.exists()


def test_verify_preserves_authenticated_payload_bytes_and_key_fingerprints(tmp_path):
    private, public = tmp_path / "state" / "key.pem", tmp_path / "trusted" / "key.pub"
    fingerprint = signing.generate_keypair(private, public)
    raw = '{"synthetic":"Δ", "version":1}\n'.encode()
    envelope = signing.sign_manifest(raw, private)
    sig = envelope["signatures"][0]
    sig["sig"] = base64.urlsafe_b64encode(base64.b64decode(sig["sig"])).decode()
    assert signing.verify_signature(envelope, public) == (raw, fingerprint)
    pub = serialization.load_pem_public_key(public.read_bytes())
    preamble = f"DSSEv1 {len(PAYLOAD_TYPE)} {PAYLOAD_TYPE} {len(raw)} ".encode()
    pub.verify(base64.urlsafe_b64decode(sig["sig"]), preamble + raw)
    other = tmp_path / "other.pub"
    wrong = signing.generate_keypair(tmp_path / "other.pem", other)
    with pytest.raises(signing.SignatureError) as exc:
        signing.verify_signature(envelope, other)
    assert exc.value.supplied_keyid == wrong and exc.value.claimed_keyid == fingerprint


def test_same_key_paths_cannot_replace_an_identity(tmp_path):
    path = tmp_path / "same"
    with pytest.raises(ValueError):
        signing.generate_keypair(path, path)
    assert not path.exists()


def test_equivalent_public_pem_can_be_reused_after_newline_conversion(tmp_path):
    private, public = tmp_path / "k", tmp_path / "p"
    first = signing.generate_keypair(private, public)
    converted = public.read_bytes().replace(b"\n", b"\r\n")
    public.write_bytes(converted)
    assert signing.generate_keypair(private, public) == first
    assert public.read_bytes() == converted
