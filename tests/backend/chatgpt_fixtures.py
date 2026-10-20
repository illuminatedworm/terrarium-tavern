"""Synthetic OAuth and Responses fixtures. Never read developer account credentials."""

import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from tavern.chatgpt import API_ROOT, ISSUER, SCOPES
from tavern.chatgpt.service import ChatGPTAccounts
from tavern.search.retrieval_config import Network

ACCESS = "fixture-access-token-only"
REFRESH = "fixture-refresh-token-only"
CLIENT = "oaiapp_fixture"
META = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/api/accounts/authorize",
    "token_endpoint": ISSUER + "/api/accounts/oauth/token",
    "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke",
    "jwks_uri": ISSUER + "/.well-known/jwks.json",
}


class IdentityFixture:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update(kid="test-key", alg="RS256", use="sig")
        self.urls, self.requests = [], []
        self.claim_overrides = {}
        self.token_overrides = {}
        self.exchange_error = None
        self.refresh_error = None
        self.revocation_status = 200
        self.refresh_count = 0
        self.catalog = [{"slug": "fixture", "display_name": "Fixture", "visibility": "list"}]

    def browser(self, url):
        self.urls.append(url)
        return True

    @property
    def params(self):
        return {key: value[0] for key, value in parse_qs(urlsplit(self.urls[-1]).query).items()}

    async def __call__(self, request):
        self.requests.append(request)
        if str(request.url) == ISSUER + "/.well-known/openid-configuration":
            return httpx.Response(200, json=META)
        if str(request.url) == META["jwks_uri"]:
            return httpx.Response(200, json={"keys": [self.jwk]})
        if str(request.url) == META["revocation_endpoint"]:
            return httpx.Response(self.revocation_status)
        if str(request.url) == API_ROOT + "/models":
            assert request.headers["authorization"].startswith("Bearer fixture-")
            return httpx.Response(200, json={"models": self.catalog})
        assert str(request.url) == META["token_endpoint"], str(request.url)
        data = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        assert data["resource"] == API_ROOT
        assert data["client_id"].startswith("oaiapp_")
        if data["grant_type"] == "refresh_token":
            self.refresh_count += 1
            assert "scope" not in data
            if self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error})
            return httpx.Response(
                200,
                json={
                    "access_token": f"fixture-access-{self.refresh_count}",
                    "refresh_token": f"fixture-refresh-{self.refresh_count}",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    **self.token_overrides,
                },
            )
        if self.exchange_error:
            return httpx.Response(400, json={"error": self.exchange_error})
        assert data["redirect_uri"] == self.params["redirect_uri"]
        assert data["code_verifier"]
        claims = {
            "iss": ISSUER,
            "aud": data["client_id"],
            "sub": "fixture-person",
            "email": "fixture@example.test",
            "nonce": self.params["nonce"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            **self.claim_overrides,
        }
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"})
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "id_token": token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": SCOPES,
                **self.token_overrides,
            },
        )

    def service(self, root):
        return ChatGPTAccounts(
            root,
            lambda: Network(api_proxy=False),
            http_transport=httpx.MockTransport(self),
            browser_open=self.browser,
        )


def seed_account(service, *, account_id=None, expires_at=None, scopes=SCOPES, client_id=CLIENT):
    account_id = account_id or uuid.uuid4().hex
    account = {
        "client_id": client_id,
        "subject": "fixture-person",
        "issuer": ISSUER,
        "email": "fixture@example.test",
        "label": "连接 1",
        "generation": uuid.uuid4().hex,
        "tokens": {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "id_token": "fixture-id-token-only",
            "expires_at": expires_at if expires_at is not None else time.time() + 3600,
            "earliest_refresh_at": 0,
            "scopes": scopes.split(),
            "saved_at": time.time(),
        },
    }
    with service.vault.locked():
        value = service.vault.load()
        value["host_id"] = value.get("host_id") or "urn:uuid:" + str(uuid.uuid4())
        value["accounts"][account_id] = account
        service.vault.save(value)
    return account_id, account


async def callback(service, fixture, *, client_id=CLIENT, **changes):
    query = {"code": "fixture-code", "state": fixture.params["state"], "client_id": client_id, **changes}
    query = {key: value for key, value in query.items() if value is not None}
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        return await client.get(fixture.params["redirect_uri"], params=query)


def completed(text="完成", calls=()):
    output = (
        [
            {
                "type": "message",
                "id": "fixture-message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ]
        if text
        else []
    )
    output += [
        {
            "type": "function_call",
            "id": "item-" + str(i),
            "call_id": "call-" + str(i),
            "namespace": "tavern",
            "name": name,
            "arguments": json.dumps(args),
            "status": "completed",
        }
        for i, (name, args) in enumerate(calls)
    ]
    return {
        "id": "fixture-response",
        "status": "completed",
        "model": "fixture",
        "output": output,
        "usage": {
            "input_tokens": 20,
            "output_tokens": 5,
            "total_tokens": 25,
            "input_tokens_details": {"cached_tokens": 12},
        },
    }


def sse(payload=None, *, events=None):
    if events is None:
        payload = payload or completed()
        text = "".join(
            part["text"]
            for item in payload["output"]
            if item["type"] == "message"
            for part in item["content"]
        )
        events = [{"type": "response.output_text.delta", "delta": text}] if text else []
        events.extend(
            {"type": "response.output_item.done", "output_index": index, "item": item}
            for index, item in enumerate(payload["output"])
        )
        events.append({"type": "response.completed", "response": {**payload, "output": []}})
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "x-request-id": "fixture-request"},
        text="".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events),
    )
