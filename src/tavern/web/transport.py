"""Bind public-web DNS validation to the addresses used by direct connections."""
import asyncio
import ipaddress
import socket
from collections.abc import Iterable

import httpcore
import httpx


async def public_addresses(host: str, port: int) -> list[str]:
    rows = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("网页工具仅访问公共互联网地址")
    return addresses


class PublicNetworkBackend(httpcore.AnyIOBackend):
    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout):
                addresses = await public_addresses(host, port)
                for index, address in enumerate(addresses):
                    try:
                        # Pass only a validated numeric address to the socket backend.
                        # The pool retains the hostname for Host, TLS and origin isolation.
                        return await super().connect_tcp(
                            address, port, timeout, local_address, socket_options,
                        )
                    except (httpcore.ConnectError, httpcore.ConnectTimeout):
                        # Preserve multi-address connectivity without resolving again.
                        if index == len(addresses) - 1:
                            raise
        except TimeoutError as error:
            raise httpcore.ConnectTimeout() from error


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        # HTTPX's adapter owns _pool and translates its streams and exceptions.
        # Supply a public HTTPcore pool API rather than patching global DNS state.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=PublicNetworkBackend(),
        )
