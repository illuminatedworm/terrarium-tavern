"""OpenAI public-client OAuth primitives and a short-lived loopback callback."""

import asyncio
import hmac
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt

from tavern.chatgpt import ISSUER
from tavern.chatgpt.errors import ChatGPTError, response_error

DISCOVERY = ISSUER + "/.well-known/openid-configuration"


def check_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    raise response_error(payload, response.status_code, response.headers.get("x-request-id"))


async def metadata(client):
    response = await client.get(DISCOVERY)
    check_response(response)
    result = response.json()
    expected = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/api/accounts/authorize",
        "token_endpoint": ISSUER + "/api/accounts/oauth/token",
        "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke",
        "jwks_uri": ISSUER + "/.well-known/jwks.json",
    }
    if not isinstance(result, dict) or any(result.get(key) != value for key, value in expected.items()):
        # Discovery must not become an arbitrary URL that receives credentials.
        raise ChatGPTError("OpenAI 身份服务配置与受支持端点不符", code="invalid_discovery")
    return expected


async def validate_identity(client, config, token, client_id, *, nonce=None, subject=None):
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ValueError("invalid signing key")
        response = await client.get(config["jwks_uri"])
        check_response(response)
        document = response.json()
        if not isinstance(document, dict):
            raise ValueError("invalid signing keys")
        keys = jwt.PyJWKSet.from_dict(document)
        key = next(key for key in keys.keys if key.key_id == header["kid"] and key.algorithm_name == "RS256")
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=client_id,
            options={"require": ["iss", "aud", "sub", "exp", "iat"]},
        )
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError("missing identity")
        if nonce is not None and (
            not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce)
        ):
            raise ValueError("nonce mismatch")
        if subject is not None and claims["sub"] != subject:
            raise ValueError("identity mismatch")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
            raise ValueError("authorized party mismatch")
        return claims
    except ChatGPTError:
        raise
    except (jwt.PyJWTError, ValueError, KeyError, StopIteration, TypeError):
        raise ChatGPTError("ChatGPT 身份校验失败，未保存此次登录", code="invalid_identity") from None


class LoopbackCallback:
    """No access log, redirects, cookies or browser scripts on this listener."""

    def __init__(self, callback):
        self.callback, self.server, self.tasks = callback, None, set()
        self.uri = ""

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=8192)
        port = self.server.sockets[0].getsockname()[1]
        self.uri = f"http://127.0.0.1:{port}/auth/callback"
        return self.uri

    async def stop(self):
        if self.server:
            self.server.close()
        # Python 3.12 waits for accepted connections as well as the listener.
        # Cancel their OAuth exchanges before waiting, otherwise cancel/expiry deadlocks.
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.server and asyncio.current_task() not in self.tasks:
            await self.server.wait_closed()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        status, message = 400, "登录回调无效。请回到 Terrarium Tavern 重试。"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(head) > 8192:
                raise ValueError("headers too large")
            first, *lines = head.decode("ascii").split("\r\n")
            method, target, protocol = first.split(" ")
            headers = {}
            for line in lines:
                if not line:
                    continue
                name, value = line.split(":", 1)
                name = name.lower()
                if name in headers:
                    raise ValueError("duplicate header")
                headers[name] = value.strip()
            url = urlsplit(target)
            if (
                method != "GET"
                or protocol not in ("HTTP/1.0", "HTTP/1.1")
                or url.scheme
                or url.netloc
                or url.path != "/auth/callback"
                or headers.get("host") != urlsplit(self.uri).netloc
            ):
                raise ValueError("invalid callback target")
            values = parse_qs(url.query, keep_blank_values=True, max_num_fields=12)
            if any(len(value) != 1 for value in values.values()):
                raise ValueError("duplicate OAuth parameter")
            status, message = await self.callback({key: value[0] for key, value in values.items()})
        except (
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            httpx.HTTPError,
        ):
            pass
        finally:
            # Only fixed application-authored messages are returned, never tokens or callback values.
            body = (
                "<!doctype html><html lang=zh-CN><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Terrarium Tavern · ChatGPT</title><h1>Terrarium Tavern</h1><p>"
                + message
                + "</p></html>"
            ).encode()
            try:
                writer.write(
                    (
                        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Bad Request'}\r\n"
                        "Content-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\n"
                        "Referrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\n"
                        "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\n"
                        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                    ).encode()
                    + body
                )
                await writer.drain()
            except (OSError, RuntimeError, asyncio.CancelledError):
                pass
            finally:
                writer.close()
                self.tasks.discard(task)
