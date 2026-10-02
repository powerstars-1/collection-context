"""One explicit HTTP-only runtime for desktop and standalone server entrypoints.

Do not infer workers, native event loops or protocol plugins from inherited
environment variables or installed optional packages. This module only describes
configuration: it imports neither the web framework nor execution capabilities.
"""

from __future__ import annotations

from typing import Literal, TypedDict


class WebRuntimeOptions(TypedDict):
    loop: Literal["asyncio"]
    http: Literal["h11"]
    ws: Literal["none"]
    interface: Literal["asgi3"]
    lifespan: Literal["on"]
    workers: int
    reload: bool
    proxy_headers: bool
    access_log: bool
    log_level: Literal["warning"]
    limit_concurrency: int
    timeout_keep_alive: int


def web_runtime_options() -> WebRuntimeOptions:
    # Each caller receives a fresh mapping. TLS, bind and authentication stay at
    # their entrypoint; none of these fixed choices can grant source/model access.
    return {
        "loop": "asyncio",
        "http": "h11",
        "ws": "none",
        "interface": "asgi3",
        "lifespan": "on",
        "workers": 1,
        "reload": False,
        "proxy_headers": False,
        "access_log": False,
        "log_level": "warning",
        "limit_concurrency": 20,
        "timeout_keep_alive": 5,
    }
