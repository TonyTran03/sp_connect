"""Server-issued device identities for queue ownership."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from http.cookies import SimpleCookie
from pathlib import Path


class DeviceSessions:
    cookie_name = "better_jam_session"

    def __init__(self, secret_file: Path, secure_cookie: bool = False) -> None:
        self.secret_file = secret_file
        self.secure_cookie = secure_cookie
        if secret_file.exists():
            self.secret = secret_file.read_bytes()
        else:
            self.secret = secrets.token_bytes(32)
            secret_file.write_bytes(self.secret)

    def resolve(self, cookie_header: str | None) -> tuple[str, str, bool]:
        token = self._read_cookie(cookie_header)
        device_id = self.verify(token) if token else None
        if device_id:
            return device_id, token, False
        device_id = str(uuid.uuid4())
        token = f"{device_id}.{self._signature(device_id)}"
        return device_id, token, True

    def verify(self, token: str) -> str | None:
        try:
            device_id, signature = token.rsplit(".", 1)
            uuid.UUID(device_id)
        except (ValueError, AttributeError):
            return None
        if hmac.compare_digest(signature, self._signature(device_id)):
            return device_id
        return None

    def set_cookie_header(self, token: str) -> str:
        secure = "; Secure" if self.secure_cookie else ""
        return (
            f"{self.cookie_name}={token}; Path=/; HttpOnly; "
            f"SameSite=Lax; Max-Age=31536000{secure}"
        )

    def _signature(self, device_id: str) -> str:
        return hmac.new(self.secret, device_id.encode(), hashlib.sha256).hexdigest()

    def _read_cookie(self, header: str | None) -> str | None:
        if not header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(header)
        except Exception:
            return None
        morsel = cookies.get(self.cookie_name)
        return morsel.value if morsel else None
