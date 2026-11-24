"""
Checkpointer factory for the master LangGraph application.

A checkpointer gives the graph server-side memory: every super-step is
persisted under a ``thread_id`` so the next request only has to send the new
user message, and so ``interrupt()`` based follow-up questions can be resumed.

Backends (selected with the ``CHECKPOINTER`` environment variable):
  memory (default) — ``MemorySaver``; per-process, lost on restart
  sqlite           — ``AsyncSqliteSaver`` persisted at ``CHECKPOINT_DB_PATH``
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
import os
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple
from langgraph.checkpoint.memory import MemorySaver

from agents.report_memory import REPORT_READING_THREAD_PREFIX

DEFAULT_CHECKPOINT_DB_PATH = "checkpoints.sqlite"
DEFAULT_REPORT_CHECKPOINT_DB_PATH = "report-reading-checkpoints.sqlite"
_BACKEND_ALIASES = {
    "": "memory",
    "memory": "memory",
    "inmemory": "memory",
    "in-memory": "memory",
    "memorysaver": "memory",
    "sqlite": "sqlite",
    "aiosqlite": "sqlite",
}


class CheckpointerConfigurationError(ValueError):
    """Raised when the checkpointer environment variables are unusable."""


class ReportCheckpointRouter(BaseCheckpointSaver[str]):
    """Share one graph while keeping the existing chat and report thread stores."""

    def __init__(self, chat: BaseCheckpointSaver, reports: BaseCheckpointSaver) -> None:
        super().__init__()
        self.chat = chat
        self.reports = reports

    def _thread_store(self, thread_id: str) -> BaseCheckpointSaver:
        return self.reports if thread_id.startswith(REPORT_READING_THREAD_PREFIX) else self.chat

    def _store(self, config: RunnableConfig | None) -> BaseCheckpointSaver:
        return self._thread_store((config or {}).get("configurable", {}).get("thread_id", ""))

    def with_allowlist(self, extra_allowlist):
        return type(self)(self.chat.with_allowlist(extra_allowlist), self.reports.with_allowlist(extra_allowlist))

    def get_next_version(self, current: str | None, channel: None) -> str:
        # Both supported backends use the same monotonic string format.
        return self.reports.get_next_version(current, channel)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._store(config).get_tuple(config)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await self._store(config).aget_tuple(config)

    def list(self, config: RunnableConfig | None, **kwargs: Any) -> Iterator[CheckpointTuple]:
        return self._store(config).list(config, **kwargs)

    async def alist(self, config: RunnableConfig | None, **kwargs: Any) -> AsyncIterator[CheckpointTuple]:
        async for checkpoint in self._store(config).alist(config, **kwargs):
            yield checkpoint

    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions) -> RunnableConfig:
        return self._store(config).put(config, checkpoint, metadata, new_versions)

    async def aput(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
                   new_versions: ChannelVersions) -> RunnableConfig:
        return await self._store(config).aput(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                   task_path: str = "") -> None:
        self._store(config).put_writes(config, writes, task_id, task_path)

    async def aput_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                          task_path: str = "") -> None:
        await self._store(config).aput_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self._thread_store(thread_id).delete_thread(thread_id)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._thread_store(thread_id).adelete_thread(thread_id)


def resolve_checkpointer_backend(env: dict[str, str] | None = None) -> str:
    """Normalize ``CHECKPOINTER`` into one of ``memory`` / ``sqlite``."""
    values = os.environ if env is None else env
    raw = values.get("CHECKPOINTER", "").strip().lower().replace("_", "")
    backend = _BACKEND_ALIASES.get(raw)
    if backend is None:
        supported = "memory, sqlite"
        raise CheckpointerConfigurationError(
            f"Unsupported CHECKPOINTER '{raw}'. Supported backends: {supported}"
        )
    return backend


def resolve_checkpoint_db_path(env: dict[str, str] | None = None) -> str:
    """Return the SQLite file path used by the ``sqlite`` backend."""
    values = os.environ if env is None else env
    return values.get("CHECKPOINT_DB_PATH", "").strip() or DEFAULT_CHECKPOINT_DB_PATH


def create_checkpointer(env: dict[str, str] | None = None) -> BaseCheckpointSaver:
    """Build the checkpointer selected by the environment."""
    backend = resolve_checkpointer_backend(env)
    if backend == "memory":
        return MemorySaver()

    try:
        asyncio.get_running_loop()
    except RuntimeError as exc:  # pragma: no cover - depends on import context
        raise CheckpointerConfigurationError(
            "CHECKPOINTER=sqlite needs an active event loop; build the graph "
            "from the running server (uvicorn) or use CHECKPOINTER=memory."
        ) from exc

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db_path = resolve_checkpoint_db_path(env)
    connection = aiosqlite.connect(db_path, check_same_thread=False)
    connection.daemon = True
    return AsyncSqliteSaver(connection)
