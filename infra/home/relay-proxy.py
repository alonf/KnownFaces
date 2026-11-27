"""Container-local CONNECT proxy for Microsoft's Relay endpoints. No request logging."""

import asyncio
import ipaddress
import re
import socket


async def copy(source, target):
    while chunk := await source.read(65536):
        target.write(chunk)
        await target.drain()
    if target.can_write_eof():
        target.write_eof()


async def connect(reader, writer):
    far = None
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        line = request.split(b"\r\n", 1)[0]
        match = re.fullmatch(
            rb"CONNECT ([a-z0-9.-]+\.servicebus\.windows\.net):443 HTTP/1\.[01]",
            line,
            re.I,
        )
        if not match:
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            return
        host = match[1].decode("ascii")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM
        )
        public = [a[4][0] for a in addresses if ipaddress.ip_address(a[4][0]).is_global]
        if not public:
            return
        remote, far = await asyncio.wait_for(
            asyncio.open_connection(public[0], 443), 15
        )
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(copy(reader, far))
            tasks.create_task(copy(remote, writer))
    except (
        OSError,
        ValueError,
        TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        ExceptionGroup,
    ):
        pass
    finally:
        if far:
            far.close()
        writer.close()


async def main():
    server = await asyncio.start_server(connect, "0.0.0.0", 3128, limit=8192)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
