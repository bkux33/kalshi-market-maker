"""Kalshi RSA-PSS request signing.

Signature = base64( RSA-PSS-SHA256( f"{timestamp_ms}{METHOD}{path}" ) ) where
``path`` is the URL path *without* query string, including the
``/trade-api/v2`` prefix. The same scheme authenticates the WebSocket handshake
(``GET /trade-api/ws/v2``).

The private key object is held privately and never included in ``repr``,
exceptions or logs.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiAuthError(RuntimeError):
    pass


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key: rsa.RSAPrivateKey):
        if not api_key_id:
            raise KalshiAuthError("api_key_id is required")
        self.api_key_id = api_key_id
        self.__key = private_key

    @classmethod
    def from_file(cls, api_key_id: str, path: str) -> "KalshiSigner":
        p = Path(path).expanduser()
        if not p.exists():
            raise KalshiAuthError(f"Private key file not found: {p}")
        try:
            key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        except Exception as exc:  # do not echo file contents
            raise KalshiAuthError(f"Could not parse private key file {p.name}: {type(exc).__name__}") from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise KalshiAuthError("Kalshi keys must be RSA private keys")
        return cls(api_key_id, key)

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        path = urlparse(path).path if "://" in path else path.split("?", 1)[0]
        msg = f"{timestamp_ms}{method.upper()}{path}".encode()
        sig = self.__key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str, timestamp_ms: Optional[str] = None) -> Dict[str, str]:
        ts = timestamp_ms or str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }

    def public_key_pem(self) -> bytes:
        return self.__key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def __repr__(self) -> str:
        return f"KalshiSigner(api_key_id={self.api_key_id[:4]}…)"

    __str__ = __repr__
