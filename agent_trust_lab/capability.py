"""Capability tokens: cryptographically-enforced, attenuation-only delegation.

The core idea: an agent's *claim* to have authority ("I was told to refund
500") is worthless on its own. What matters is whether it holds a token whose
signature verifies against the trust root and whose scope/amount were never
escalated anywhere in the delegation chain. This module is what makes that
check real instead of a log line.
"""
import hmac
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Optional


class AuthorityError(Exception):
    pass


def _sign(payload: dict, key: bytes) -> str:
    msg = json.dumps(payload, sort_keys=True).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Token:
    issuer: str
    subject: str
    scope: str
    max_amount: float
    expires_at: float
    parent_sig: Optional[str]
    signature: str

    def payload(self) -> dict:
        return {
            "issuer": self.issuer,
            "subject": self.subject,
            "scope": self.scope,
            "max_amount": self.max_amount,
            "expires_at": self.expires_at,
            "parent_sig": self.parent_sig,
        }


class TokenAuthority:
    """Holds the root signing key. In production this would be an HSM/KMS;
    here it's an HMAC key so the whole thing runs free and locally."""

    def __init__(self, root_key: bytes):
        self._key = root_key

    def issue_root(self, subject: str, scope: str, max_amount: float, ttl: float = 60.0) -> Token:
        payload = {
            "issuer": "root",
            "subject": subject,
            "scope": scope,
            "max_amount": max_amount,
            "expires_at": time.time() + ttl,
            "parent_sig": None,
        }
        return Token(signature=_sign(payload, self._key), **payload)

    def delegate(self, parent: Token, subject: str, scope: str, max_amount: float, ttl: float = 60.0) -> Token:
        """Issue a child token. Enforces attenuation: a child can never exceed
        its parent's rights, no matter what the delegating agent 'intends'."""
        self.verify(parent)
        if max_amount > parent.max_amount:
            raise AuthorityError(
                f"delegation would escalate max_amount {max_amount} > parent {parent.max_amount}"
            )
        if scope != parent.scope:
            raise AuthorityError(f"delegation scope '{scope}' outside parent scope '{parent.scope}'")
        payload = {
            "issuer": parent.subject,
            "subject": subject,
            "scope": scope,
            "max_amount": max_amount,
            "expires_at": min(time.time() + ttl, parent.expires_at),
            "parent_sig": parent.signature,
        }
        return Token(signature=_sign(payload, self._key), **payload)

    def verify(self, token: Token) -> None:
        """Verifies signature + expiry. Raises AuthorityError on any failure."""
        expected = _sign(token.payload(), self._key)
        if not hmac.compare_digest(expected, token.signature):
            raise AuthorityError(f"invalid signature for token issued to '{token.subject}' (forged or tampered)")
        if time.time() > token.expires_at:
            raise AuthorityError(f"token for '{token.subject}' expired")

    def authorize(self, token: Token, subject: str, scope: str, amount: float) -> None:
        """The actual enforcement check a tool call makes before acting."""
        self.verify(token)
        if token.subject != subject:
            raise AuthorityError(f"token subject '{token.subject}' does not match caller '{subject}'")
        if token.scope != scope:
            raise AuthorityError(f"token scope '{token.scope}' does not cover action '{scope}'")
        if amount > token.max_amount:
            raise AuthorityError(
                f"requested amount {amount} exceeds authorized max {token.max_amount} for '{subject}'"
            )
