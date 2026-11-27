import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path


class Access:
    def __init__(self, directory):
        path = Path(directory) / "credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with os.fdopen(
                os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
            ) as f:
                json.dump(
                    {
                        role: secrets.token_urlsafe(32)
                        for role in ("admin", "ingest", "read")
                    },
                    f,
                )
        self.keys = json.loads(path.read_text())
        self.sessions = {}

    def principal(self, headers):
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            for role, secret in self.keys.items():
                if hmac.compare_digest(token.encode(), secret.encode()):
                    return role
        raise PermissionError("A valid access token is required")

    @staticmethod
    def allow(role, action):
        if role != "admin" and role != action:
            raise PermissionError(f"Requires {action} access")

    def state_key(self):
        return hashlib.sha256(
            ("KnownFaces/MRTR/" + self.keys["admin"]).encode()
        ).digest()
