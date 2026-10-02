"""Same read-only use cases across committed and explicitly selected legacy layouts."""

from typing import Any, Protocol


class ReadService(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int = 3,
        filters: dict[str, Any] | None = None,
        offset: int = 0,
        version: str | None = None,
    ) -> dict[str, Any]: ...
    def read(
        self,
        ref: str,
        *,
        artifact: str = "original",
        offset: int = 0,
        max_chars: int = 4000,
        version: str | None = None,
    ) -> dict[str, Any]: ...
    def status(self, ref: str) -> dict[str, Any]: ...
    def list_items(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        version: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def overview(self) -> dict[str, Any]: ...
