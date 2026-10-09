"""Software passkey for tests: P-256 key, "none" attestation, real signatures, returned in the browser's JSON shape."""
import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

FLAGS_UP, FLAGS_UV, FLAGS_AT = 0x01, 0x04, 0x40


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftPasskey:
    def __init__(self, origin, rp_id="localhost", user_verified=True):
        self.origin, self.rp_id = origin, rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.user_handle = None
        self.count = 0
        self.flags = FLAGS_UP | (FLAGS_UV if user_verified else 0)

    def _client_data(self, kind, challenge, origin=None):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options):
        self.user_handle = unb64u(options["user"]["id"])
        nums = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")}
        auth = (hashlib.sha256(options["rp"]["id"].encode()).digest() + bytes([self.flags | FLAGS_AT]) +
                struct.pack(">I", self.count) + bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id +
                cbor2.dumps(cose))
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(self._client_data("webauthn.create", options["challenge"])),
                             "attestationObject": b64u(att), "transports": ["internal"]},
                "clientExtensionResults": {}}

    def get(self, options, origin=None):
        allowed = [c["id"] for c in options.get("allowCredentials") or []]
        if allowed and b64u(self.cred_id) not in allowed:
            raise AssertionError("this passkey is not in allowCredentials")
        self.count += 1
        auth = hashlib.sha256(options["rpId"].encode()).digest() + bytes([self.flags]) + struct.pack(">I", self.count)
        client = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        resp = {"clientDataJSON": b64u(client), "authenticatorData": b64u(auth), "signature": b64u(sig)}
        if self.user_handle:
            resp["userHandle"] = b64u(self.user_handle)
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key", "response": resp,
                "clientExtensionResults": {}}
