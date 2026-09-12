"""Process-local async locks for critical sections (single-worker safe)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_MAX_LOCKS = 2048
_locks: dict[str, asyncio.Lock] = {}
# How many coroutines hold or wait for each key's lock. A lock reads as unlocked
# between its release and its next waiter waking, so ``locked()`` alone cannot
# tell an idle lock from one that is being handed over.
_users: dict[str, int] = {}
_registry_lock = asyncio.Lock()


def _prune_unused_locks() -> None:
    """Drop idle lock entries when the registry grows too large."""
    if len(_locks) < _MAX_LOCKS:
        return
    # Only a lock nobody holds or waits for is idle: dropping one mid-handover
    # would let a newcomer take a fresh lock while the waiter enters the old one.
    idle = [key for key in _locks if not _users.get(key)]
    for key in idle[: max(1, len(idle) // 2)]:
        del _locks[key]


async def _lock_for(key: str) -> asyncio.Lock:
    async with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            _prune_unused_locks()
            lock = asyncio.Lock()
            _locks[key] = lock
        _users[key] = _users.get(key, 0) + 1
        return lock


@asynccontextmanager
async def keyed_lock(key: str) -> AsyncIterator[None]:
    """
    Serialize coroutines that share ``key``.

    Suitable for MemoryStorage / single bot process. Combine with DB row locks
    for checkout so a second worker still cannot double-order.
    """
    lock = await _lock_for(key)
    try:
        async with lock:
            yield
    finally:
        _users[key] -= 1
        if not _users[key]:
            del _users[key]
