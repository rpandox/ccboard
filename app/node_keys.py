"""Node identity, request signatures and public rotation proofs. Only this module reads the seed.

Cryptography is imported on use. Caches are keyed by data directory so the two-board test transport
can host two identities in one process. A pin is changed only after all request checks succeed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .config import settings
from .db import iso

STARTED = int(time.time())
WINDOW = 120
ROTATION_WINDOW = 30 * 86400
_lock = threading.RLock()
_keys = {}
_reports = {}
_nonces = {}


def _ed():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    return ed25519


def mode():
    return getattr(settings, "node_keys", "prefer")


def _path():
    return Path(settings.data_dir) / "node-key"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _raw(value, size):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) != (size * 8 + 5) // 6:
        raise ValueError("bad encoding")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != size or _b64(raw) != value:
        raise ValueError("bad encoding")
    return raw


def valid_public(pub):
    try:
        _raw(pub, 32)
        return True
    except (ValueError, TypeError):
        return False


def key_id(pub):
    return hashlib.sha256(_raw(pub, 32)).hexdigest()[:16]


def reset():
    with _lock:
        _keys.clear()
        _reports.clear()
        _nonces.clear()


def load_or_create() -> bool:
    """Load once or create atomically, returning availability, never private material."""
    if mode() == "off":
        return False
    p = _path()
    with _lock:
        if p in _keys:
            return True
        report = {"persistent": False, "corrupt": p.with_name("node-key.bad").exists()}
        try:
            ed = _ed()
            key = None
            try:
                with p.open("rb") as f:
                    text = f.read(128)
            except FileNotFoundError:
                text = None
            if text is not None:
                try:
                    if len(text) != 52 or not text.startswith(b"ed25519:") or not text.endswith(b"\n"):
                        raise ValueError("bad key file")
                    key = ed.Ed25519PrivateKey.from_private_bytes(_raw(text[8:-1].decode("ascii"), 32))
                    report["persistent"] = True
                except (ValueError, UnicodeError):
                    report["corrupt"] = True
                    p.chmod(0o600)
                    bad = p.with_name("node-key.bad")
                    if not bad.exists():
                        p.replace(bad)
            if key is None:
                key = ed.Ed25519PrivateKey.generate()
                try:
                    from . import nodes
                    nodes._write_id_file(p, "ed25519:" + _b64(key.private_bytes_raw()))
                    report["persistent"] = True
                except OSError:
                    pass
            _keys[p] = key
        except OSError:
            try:
                _keys[p] = _ed().Ed25519PrivateKey.generate()
            except Exception:
                pass
        except Exception:
            # An unavailable provider or unsupported algorithm leaves the board unkeyed.
            pass
        _reports[p] = report
        return p in _keys


def available() -> bool:
    return load_or_create()


def public() -> str | None:
    with _lock:
        return _b64(_keys[_path()].public_key().public_bytes_raw()) if available() else None


def _sign(message):
    with _lock:
        if not available():
            raise ValueError("node key unavailable")
        return _b64(_keys[_path()].sign(message))


def _verify(pub, sig, message):
    try:
        _ed().Ed25519PublicKey.from_public_bytes(_raw(pub, 32)).verify(_raw(sig, 64), message)
        return True
    except Exception:
        return False


def _lines(*parts):
    return "\n".join(parts).encode("utf-8")


def sign_confirm(proof):
    from . import nodes
    pub = public()
    return {"node_id": nodes.node_id(), **({"pubkey": pub, "sig": _sign(_lines("ccboard-pair-confirm-answer/1", proof, nodes.node_id()))} if pub else {})}


def verify_confirm(answer, proof, nid, pub):
    return (isinstance(answer, dict) and answer.get("node_id") == nid and answer.get("pubkey") == pub
            and _verify(pub, answer.get("sig"), _lines("ccboard-pair-confirm-answer/1", proof, nid)))


def body_bytes(body):
    return body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else b""


def _message(method, target, body, timestamp, nonce, recipient_id, sender_id, token, acting_user):
    return _lines("ccboard-node-req/1", method.upper(), target, hashlib.sha256(body).hexdigest(), timestamp, nonce,
                  recipient_id, sender_id, hashlib.sha256(token.encode()).hexdigest(), acting_user or "")


def _rotation_path():
    return _path().with_name("node-key-rotation.json")


def _rotation():
    try:
        data = json.loads(_rotation_path().read_text())
        if isinstance(data, dict) and isinstance(data.get("chain"), list) and len(data["chain"]) <= 16:
            return data
    except (OSError, ValueError):
        pass
    return {"chain": [], "seen": []}


def _rotation_message(r):
    return _lines("ccboard-key-rotate/1", r["old"], r["new"], str(r["at"]))


def _active_chain():
    from . import nodes
    pub = public()
    chain = _rotation()["chain"]
    # A proof may have been saved just before a failed seed write. Keep only the committed prefix.
    last = next((i for i in range(len(chain) - 1, -1, -1) if isinstance(chain[i], dict) and chain[i].get("new") == pub), None)
    if last is None:
        return []
    return [r for r in chain[:last + 1] if isinstance(r, dict) and type(r.get("at")) is int and nodes._now() - r["at"] <= ROTATION_WINDOW]


def rotation_header():
    chain = _active_chain()
    statement = chain[0] if len(chain) == 1 else chain
    return _b64(json.dumps(statement, separators=(",", ":")).encode()) if chain else None


def rotated_public(pinned, statement):
    from . import nodes
    try:
        if not isinstance(statement, str) or len(statement) > 10000 or not re.fullmatch(r"[A-Za-z0-9_-]+", statement):
            return None
        chain = json.loads(base64.urlsafe_b64decode(statement + "=" * (-len(statement) % 4)))
        if isinstance(chain, dict):
            chain = [chain]
        if not isinstance(chain, list) or not 1 <= len(chain) <= 16:
            return None
        pub, advanced = pinned, False
        for r in chain:
            if not isinstance(r, dict) or set(r) != {"old", "new", "at", "sig"}:
                return None
            if r["old"] != pub:
                if advanced:
                    return None
                continue
            if (type(r["at"]) is not int or not -WINDOW <= nodes._now() - r["at"] <= ROTATION_WINDOW
                    or not valid_public(r["new"]) or not _verify(pub, r["sig"], _rotation_message(r))):
                return None
            pub, advanced = r["new"], True
        return pub if advanced else None
    except (ValueError, TypeError, KeyError):
        return None


def rotate():
    from . import nodes
    with _lock:
        old = public()
        if not old or not _reports[_path()]["persistent"]:
            raise ValueError("A saved key is required to rotate")
        chain = _active_chain()
        if len(chain) >= 16:
            raise ValueError("Wait for the oldest rotation to expire before rotating again")
        new = _ed().Ed25519PrivateKey.generate()
        r = {"old": old, "new": _b64(new.public_key().public_bytes_raw()), "at": int(nodes._now())}
        r["sig"] = _sign(_rotation_message(r))
        # Public proof first: a crash before the seed replacement leaves the old identity intact.
        nodes._write_id_file(_rotation_path(), json.dumps({"chain": chain + [r], "seen": []}))
        nodes._write_id_file(_path(), "ed25519:" + _b64(new.private_bytes_raw()))
        _keys[_path()] = new
        return card()


def acknowledge(recipient_id, token, headers):
    if not available() or headers.get("x-ccboard-key-seen") != key_id(public()):
        return
    from . import nodes
    with _lock:
        r = _rotation()
        peer = recipient_id + ":" + nodes._digest(token)
        if r["chain"] and peer not in r.get("seen", []):
            r.setdefault("seen", []).append(peer)
            try:
                nodes._write_id_file(_rotation_path(), json.dumps(r))
            except OSError:
                pass


def sign_headers(method, target, body, *, recipient_id, token, acting_user=None):
    from . import nodes
    with _lock:
        pub = public()
        if not pub:
            return {}
        timestamp, nonce = str(int(nodes._now())), secrets.token_urlsafe(16)
        sig = _sign(_message(method, target, body_bytes(body), timestamp, nonce, recipient_id or "", nodes.node_id(), token, acting_user))
        headers = {"X-CCBoard-Signature": f"v1.{key_id(pub)}.{timestamp}.{nonce}.{sig}"}
        statement = rotation_header()
        if statement and (recipient_id or "") + ":" + nodes._digest(token) not in _rotation().get("seen", []):
            headers["X-CCBoard-Key-Rotation"] = statement
        return headers


def conflict(nid, pub, db):
    from . import nodes
    return any(p.get("node_id") == nid and p.get("pubkey") and p["pubkey"] != pub for p in nodes.peers(db))


def pin_in(peer, pub, source, db):
    from . import nodes
    db.node_pair_update(peer["peer_id"], peer_pubkey=pub, peer_key_source=source, peer_key_at=iso(nodes._now()))
    peer.update(pubkey=pub, key_source=source, key_at=iso(nodes._now()))
    nodes.audit("in", peer["peer_id"], "key_rotated" if source == "rotation" else "key_pinned", True, key_id(pub) + " " + source, db=db)


def verify_request(method, target, body, *, headers, peer, recipient_id, token, db, defer_pin=False):
    """Return a fixed refusal word or None. Nonces and pin changes commit together after verification."""
    from . import nodes
    with _lock:
        current = db.node_pair_get(peer["peer_id"])
        pinned = current.get("peer_pubkey") if current else peer.get("pubkey")
        signature = headers.get("x-ccboard-signature")
        if not signature:
            return "signature_required" if pinned or mode() == "require" else None
        if not pinned and mode() == "require":
            return "key_required"
        if not pinned and mode() == "off":
            return None
        try:
            version, kid, timestamp, nonce, sig = signature.split(".")
            if version != "v1" or not re.fullmatch(r"[0-9a-f]{16}", kid) or not re.fullmatch(r"[0-9]{1,12}", timestamp):
                return "bad_signature"
            _raw(nonce, 16)
        except (ValueError, TypeError):
            return "bad_signature"
        pub, source = pinned, None
        if pinned and kid != key_id(pinned):
            pub = rotated_public(pinned, headers.get("x-ccboard-key-rotation"))
            if not pub or key_id(pub) != kid:
                return "key_changed"
            source = "rotation"
        elif not pinned:
            pub = headers.get("x-ccboard-pubkey")
            if not valid_public(pub) or key_id(pub) != kid:
                return "bad_signature"
            if conflict(peer.get("node_id"), pub, db):
                return "key_changed"
            source = "upgrade"
        sender = headers.get("x-ccboard-node", "")
        if peer.get("node_id") and sender != peer["node_id"]:
            return "bad_signature"
        if not _verify(pub, sig, _message(method, target, body, timestamp, nonce, recipient_id, sender, token, headers.get("x-ccboard-acting-user"))):
            return "bad_signature"
        now = nodes._now()
        if abs(now - int(timestamp)) > WINDOW:
            return "clock"
        cache = _nonces.setdefault((str(_path()), peer["peer_id"]), OrderedDict())
        while cache and next(iter(cache.values())) < now - 300:
            cache.popitem(last=False)
        if nonce in cache:
            return "replay"
        if int(timestamp) < STARTED:
            return "stale"
        if source:
            if defer_pin:
                peer["_key_pin"] = (pinned, pub, source)
            else:
                pin_in(peer, pub, source, db)
        cache[nonce] = now
        if len(cache) > 4096:
            cache.popitem(last=False)
        peer.update(key_verified=True, pubkey=pub)
        return None


def finish_request(peer, db):
    """Commit an upgrade only after scopes and the independent device check also allow the call."""
    pending = peer.pop("_key_pin", None)
    if pending:
        before, pub, source = pending
        with _lock:
            row = db.node_pair_get(peer["peer_id"])
            if not row or row["revoked_at"] or row.get("peer_pubkey") not in (before, pub):
                return "key_changed"
            if row.get("peer_pubkey") != pub:
                pin_in(peer, pub, source, db)
    return None


def check_card(peer, value, db):
    """Compare a card to the current pin; a rotation needs the old key's public proof."""
    from . import nodes
    with _lock:
        peer = nodes.peer(peer["peer_id"], db) or peer
        key = value.get("key") if isinstance(value, dict) else None
        pub = key.get("pub") if isinstance(key, dict) else None
        pinned = peer.get("pubkey")
        source = None
        if pinned and pub != pinned:
            if valid_public(pub) and rotated_public(pinned, key.get("rotation")) == pub:
                source = "rotation"
            else:
                nodes._note(db, peer["peer_id"], ok=False, error="key_changed", repair=True)
                nodes.audit("out", peer["peer_id"], "key_changed", False, "key_changed " + key_id(pinned), db=db)
                return "key_changed"
        elif not pinned and mode() != "off":
            if mode() == "require":
                nodes._note(db, peer["peer_id"], ok=False, error="key_required", repair=True)
                return "key_required"
            if valid_public(pub):
                if conflict(peer.get("node_id"), pub, db):
                    return "key_changed"
                source = "upgrade"
        if source:
            nodes.put_peer({**peer, "pubkey": pub, "key_source": source, "key_at": iso(nodes._now())}, db=db, create=False)
            nodes.audit("out", peer["peer_id"], "key_rotated" if source == "rotation" else "key_pinned", True, key_id(pub) + " " + source, db=db)
        return None


def card():
    pub = public()
    if not pub:
        return None
    try:
        created = iso(_path().stat().st_mtime)
    except OSError:
        created = None
    out = {"alg": "ed25519", "pub": pub, "id": key_id(pub), "created_at": created}
    rotation = rotation_header()
    if rotation:
        out["rotation"] = rotation
    return out


def doctor(db):
    from . import nodes
    if mode() == "off":
        return "warn", "CCBOARD_NODE_KEYS=off: no key is made or sent; existing pins still require signatures"
    if not available():
        return "fail", "Node key unavailable: check that cryptography is installed; pair again after fixing it"
    report = _reports[_path()]
    if report["corrupt"]:
        return "fail", "The node key file was corrupt; every pair must be made again"
    if not report["persistent"]:
        return "fail", "The data directory cannot save the node key; it lives for this run only; pair again after fixing it"
    try:
        if _path().stat().st_mode & 0o777 != 0o600:
            return "fail", "The node-key file must have mode 0600; remove affected pairs and pair again if it was exposed"
    except OSError:
        return "fail", "The node-key file cannot be read; pair again after fixing it"
    unkeyed = [p.get("name") or "a node" for p in nodes.peers(db) if not p.get("legacy") and not p.get("pubkey")]
    detail = f"CCBOARD_NODE_KEYS={mode()}: Ed25519 key present, 0600, cryptography importable"
    return ("warn", detail + "; unkeyed: " + ", ".join(unkeyed) + "; pair again") if unkeyed else ("pass", detail)
