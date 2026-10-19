"""SignalListener — persistent, authenticated WebSocket to the Okkults server.

Responsibilities (and nothing more):

* authenticate via license query string
* reconnect with exponential backoff (1s, 2s, 4s ... capped at 30s)
* exchange SYSTEM_PING / SYSTEM_PONG heartbeats every 30 s
* dispatch parsed BATCH_SIGNAL / EXIT_SIGNAL / CLOSE_POSITION events
* ferry FILL_CONFIRMATION telemetry upstream

It contains zero trading logic: handlers receive validated payloads and
decide everything themselves.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import websockets
from rich.console import Console
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from signals.protocol import (
    BATCH_SIGNAL_EVENT,
    CLOSE_POSITION_EVENT,
    EXIT_SIGNAL_EVENT,
    SYSTEM_PING_EVENT,
    SYSTEM_PONG_EVENT,
    BatchSignal,
    ClosePositionSignal,
    ExitSignal,
    SystemPong,
    parse_close_position,
    parse_exit_signal,
)

console = Console()

RECONNECT_BASE_S: float = 1.0
RECONNECT_MAX_S: float = 30.0
HEARTBEAT_INTERVAL_S: float = 30.0
OUTBOX_LIMIT: int = 1000


def backoff_delay(attempt: int, base: float = RECONNECT_BASE_S, cap: float = RECONNECT_MAX_S) -> float:
    """Exponential backoff: attempt 0 -> base, attempt 1 -> 2*base, ... capped."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    return min(base * (2**attempt), cap)


class BatchHandler(Protocol):
    async def __call__(self, batch: BatchSignal) -> None: ...


class ExitHandler(Protocol):
    async def __call__(self, exit_signal: ExitSignal) -> None: ...


class ClosePositionHandler(Protocol):
    async def __call__(self, close_signal: ClosePositionSignal) -> None: ...


class ListenerCallbacks:
    def __init__(
        self,
        on_batch: Callable[[BatchSignal], Awaitable[None]],
        on_exit: Callable[[ExitSignal], Awaitable[None]],
        on_close_position: Callable[[ClosePositionSignal], Awaitable[None]],
    ) -> None:
        self.on_batch = on_batch
        self.on_exit = on_exit
        self.on_close_position = on_close_position


class SignalListener:
    """Authenticates with ``?license=...`` and streams trading events."""

    def __init__(
        self,
        ws_url: str,
        license_key: str,
        callbacks: ListenerCallbacks,
        broker_status: Callable[[], bool] | None = None,
        connect_factory: Callable[[str], Awaitable[Any]] | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        if not license_key:
            raise ValueError("license key required to connect to the signal server")
        self._ws_url = ws_url
        self._license_key = license_key
        self._callbacks = callbacks
        self._broker_status = broker_status or (lambda: True)
        self._heartbeat_interval = heartbeat_interval
        # connect_factory allows tests to inject a stub socket provider.
        self._connect_factory = connect_factory or websockets.connect
        self._outbox: deque[str] = deque(maxlen=OUTBOX_LIMIT)
        self._conn: Any = None
        self._running = asyncio.Event()
        self._connected = asyncio.Event()
        self._last_heartbeat: float | None = None
        self._reconnect_attempt = 0

    # ------------------------------------------------------------------ #
    # public surface
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def last_heartbeat_at(self) -> float | None:
        return self._last_heartbeat

    def connection_age_seconds(self) -> float:
        if self._last_heartbeat is None:
            return 0.0
        return max(0.0, time.monotonic() - self._last_heartbeat)

    def _authed_url(self) -> str:
        """License goes in the query string; never logged in full."""
        return f"{self._ws_url}?license={self._license_key}"

    async def send(self, payload: dict) -> bool:
        """Queue-and-flush telemetry upstream; True if sent immediately."""
        text = json.dumps(payload)
        connection = self._conn
        if connection is None or not self._connected.is_set():
            self._outbox.append(text)
            return False
        try:
            await connection.send(text)
            return True
        except (ConnectionClosed, RuntimeError):
            self._outbox.append(text)
            return False

    async def run(self) -> None:
        """Main loop: connect, dispatch, reconnect. Runs until cancelled."""
        self._running.set()
        while self._running.is_set():
            await self._run_single_connection()
            if not self._running.is_set():
                break
            # Exponential backoff before the next attempt (1s, 2s, 4s ... 30s)
            delay = backoff_delay(self._reconnect_attempt)
            self._reconnect_attempt += 1
            console.print(
                f"[yellow]Signal server offline — reconnecting in [bold]{delay:.0f}s"
                f"[/bold] (attempt #{self._reconnect_attempt})[/yellow]"
            )
            try:
                await asyncio.wait_for(self._running.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._running.clear()
        self._connected.clear()
        connection, self._conn = self._conn, None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    async def _run_single_connection(self) -> None:
        url = self._authed_url()
        try:
            connection = await self._connect_factory(url)
        except (TimeoutError, InvalidURI, InvalidHandshake, OSError) as exc:
            self._log_connect_error(exc)
            return
        except Exception as exc:  # dependency-specific connection errors
            self._log_connect_error(exc)
            return

        self._conn = connection
        self._connected.set()
        self._reconnect_attempt = 0
        self._last_heartbeat = time.monotonic()
        console.print("[cyan][OK][/cyan] Authenticated to Okkults signal server")
        try:
            await self._flush_outbox(connection)
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(connection))
            try:
                await self._recv_loop(connection)
            finally:
                heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._connected.clear()
            self._conn = None
            try:
                await connection.close()
            except Exception:
                pass

    def _log_connect_error(self, exc: Exception) -> None:
        console.print(
            f"[yellow][!] Signal server unreachable (license kept private): {type(exc).__name__}[/yellow]"
        )

    async def _recv_loop(self, connection: Any) -> None:
        recv = connection.recv
        while self._running.is_set():
            try:
                message = await recv()
            except ConnectionClosed:
                console.print("[yellow][!] Signal connection closed[/yellow]")
                return
            except Exception as exc:  # network layer hiccup -> reconnect cycle
                console.print(f"[yellow][!] Signal recv error: {type(exc).__name__}[/yellow]")
                return
            await self._dispatch(message)

    async def _dispatch(self, message: object) -> None:
        raw = message if isinstance(message, (str, bytes, dict)) else None
        if raw is None:
            return
        try:
            data = raw if isinstance(raw, dict) else json.loads(raw)
        except json.JSONDecodeError:
            return

        event_name = data.get("event") if isinstance(data, dict) else None
        if event_name == SYSTEM_PING_EVENT:
            await self._handle_system_ping()
            return
        if event_name == SYSTEM_PONG_EVENT:
            pong = SystemPong.model_validate(data)
            self._last_heartbeat = time.monotonic()
            status = "connected" if pong.broker_connected else "disconnected"
            console.print(f"[dim]Heartbeat PONG — broker {status}[/dim]")
            return
        if event_name == BATCH_SIGNAL_EVENT:
            try:
                batch = BatchSignal.model_validate(data)
            except Exception:
                console.print("[red][!] Invalid BATCH_SIGNAL payload dropped[/red]")
                return
            await self._callbacks.on_batch(batch)
            return
        if event_name == EXIT_SIGNAL_EVENT:
            try:
                exit_signal = parse_exit_signal(data)
            except Exception:
                console.print("[red][!] Invalid EXIT_SIGNAL payload dropped[/red]")
                return
            await self._callbacks.on_exit(exit_signal)
            return
        if event_name == CLOSE_POSITION_EVENT:
            try:
                close_signal = parse_close_position(data)
            except Exception:
                console.print("[red][!] Invalid CLOSE_POSITION payload dropped[/red]")
                return
            await self._callbacks.on_close_position(close_signal)
            return
        # unknown event -> Ignore silently (forward compatibility)

    async def _handle_system_ping(self) -> None:
        pong = {
            "event": SYSTEM_PONG_EVENT,
            "broker_connected": bool(self._broker_status()),
        }
        await self._raw_send(pong)

    async def _heartbeat_loop(self, connection: Any) -> None:
        send = connection.send
        while self._connected.is_set():
            await asyncio.sleep(self._heartbeat_interval)
            if not self._connected.is_set():
                return
            ping = {"event": SYSTEM_PING_EVENT, "broker_connected": bool(self._broker_status())}
            try:
                await send(json.dumps(ping))
                self._last_heartbeat = time.monotonic()
            except Exception:
                return

    async def _raw_send(self, payload: dict) -> None:
        connection = self._conn
        if connection is None:
            self._outbox.append(json.dumps(payload))
            return
        try:
            send = connection.send
            await send(json.dumps(payload))
        except (ConnectionClosed, RuntimeError):
            self._outbox.append(json.dumps(payload))

    async def _flush_outbox(self, connection: Any) -> None:
        send = connection.send
        while self._outbox:
            text = self._outbox.popleft()
            try:
                await send(text)
            except Exception:
                self._outbox.appendleft(text)
                return


__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "RECONNECT_BASE_S",
    "RECONNECT_MAX_S",
    "ListenerCallbacks",
    "SignalListener",
    "backoff_delay",
]
