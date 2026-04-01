from __future__ import annotations

import json
from dataclasses import asdict, dataclass

try:
    import keyring
except ImportError:  # pragma: no cover - optional dependency at runtime
    keyring = None


SERVICE_NAME = "trakt_tracker"
TOKEN_KEY = "oauth_tokens"


@dataclass(slots=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    created_at: int
    expires_in: int
    scope: str = ""
    token_type: str = "bearer"


class TokenStore:
    def load(self, account: str) -> TokenBundle | None:
        if keyring is None:
            return None
        payload = keyring.get_password(SERVICE_NAME, f"{account}:{TOKEN_KEY}")
        if not payload:
            return None
        data = json.loads(payload)
        return TokenBundle(**data)

    def save(self, account: str, bundle: TokenBundle) -> None:
        if keyring is None:
            raise RuntimeError("keyring is not installed")
        keyring.set_password(SERVICE_NAME, f"{account}:{TOKEN_KEY}", json.dumps(asdict(bundle)))

    def delete(self, account: str) -> None:
        if keyring is None:
            return
        try:
            keyring.delete_password(SERVICE_NAME, f"{account}:{TOKEN_KEY}")
        except Exception:
            pass
