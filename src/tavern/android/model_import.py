"""Bounded, single-write GGUF import from an HTTP request stream."""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterable
from pathlib import Path

BLOCK_BYTES = 1024 * 1024


async def write_model(stream: AsyncIterable[bytes], path: Path, expected: dict) -> int:
    checksum = hashlib.sha256()
    total = 0
    buffer = bytearray()

    async def finish_io(function, *args):
        # A cancelled await does not stop a filesystem worker. Join it before the
        # caller closes/removes the staging file or another import reuses its name.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    with path.open("wb") as output:

        def write(block):
            output.write(block)
            checksum.update(block)

        def flush():
            output.flush()
            os.fsync(output.fileno())

        async for chunk in stream:
            total += len(chunk)
            if total > expected["bytes"]:
                raise ValueError("模型文件超过预期大小")
            remaining = memoryview(chunk)
            while remaining:
                count = min(BLOCK_BYTES - len(buffer), len(remaining))
                buffer.extend(remaining[:count])
                remaining = remaining[count:]
                if len(buffer) == BLOCK_BYTES:
                    await finish_io(write, bytes(buffer))
                    buffer.clear()
        if buffer:
            await finish_io(write, bytes(buffer))
        if total != expected["bytes"] or checksum.hexdigest() != expected["sha256"]:
            raise ValueError("模型 SHA-256 或大小不匹配，请选择 " + expected["filename"])
        await finish_io(flush)
    return total
