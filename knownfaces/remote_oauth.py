"""Validate single-tenant delegated v2 access tokens using Microsoft's keys."""

import hashlib
from dataclasses import dataclass

import jwt
from jwt import InvalidTokenError, PyJWKClient


@dataclass(frozen=True)
class Principal:
    actor: str
    scopes: frozenset[str]


class EntraVerifier:
    def __init__(self, policy):
        self.policy = policy
        self.keys = PyJWKClient(
            f"https://login.microsoftonline.com/{policy.tenant_id}/discovery/v2.0/keys",
            timeout=4,
            lifespan=300,
        )

    def verify(self, token):
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not 0 < len(kid) <= 256
        ):
            raise InvalidTokenError("Invalid signing header")
        claims = jwt.decode(
            token,
            self.keys.get_signing_key(kid).key,
            algorithms=["RS256"],
            issuer=self.policy.issuer,
            audience=str(self.policy.api_id),
            options={
                "require": [
                    "exp",
                    "nbf",
                    "iat",
                    "iss",
                    "aud",
                    "tid",
                    "ver",
                    "azp",
                    "oid",
                    "sub",
                    "scp",
                ],
                "strict_aud": True,
            },
        )
        expected = {
            "tid": str(self.policy.tenant_id),
            "ver": "2.0",
            "azp": str(self.policy.connector_id),
            "oid": str(self.policy.authorized_user_oid),
        }
        if (
            any(claims.get(k) != v for k, v in expected.items())
            or not isinstance(claims["scp"], str)
            or claims.get("idtyp") == "app"
        ):
            raise InvalidTokenError("Invalid delegated principal")
        actor = hashlib.sha256(
            f"{claims['tid']}:{claims['oid']}:{claims['azp']}".encode()
        ).hexdigest()
        return Principal(actor, frozenset(claims["scp"].split()))
