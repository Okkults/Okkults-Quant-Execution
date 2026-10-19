"""Okkults signal server — wire protocol models.

The client is math-blind: it trusts the server's pre-computed numbers and
validates structure only. No indicators, no formulas, ever.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

ET: ZoneInfo = ZoneInfo("America/New_York")

BATCH_SIGNAL_EVENT: Final = "BATCH_SIGNAL"
EXIT_SIGNAL_EVENT: Final = "EXIT_SIGNAL"
CLOSE_POSITION_EVENT: Final = "CLOSE_POSITION"
SYSTEM_PING_EVENT: Final = "SYSTEM_PING"
SYSTEM_PONG_EVENT: Final = "SYSTEM_PONG"
FILL_CONFIRMATION_EVENT: Final = "FILL_CONFIRMATION"
MAX_SIGNALS_PER_BATCH: int = 12


class ExitRules(BaseModel):
    """Exit hints handed over the wire for contract compatibility.

    The client does not place resting stops, so ``disaster_stop_pct`` is
    parsed but intentionally unused. All actual exits come from the server's
    EXIT_SIGNAL / CLOSE_POSITION messages plus the local failsafe.
    """

    disaster_stop_pct: float
    profit_target_pct: float | None = None
    trailing_stop_pct: float | None = None

    @field_validator("disaster_stop_pct")
    @classmethod
    def _sane_stop(cls, value: float) -> float:
        if not 0.0 < value < 1.0:
            raise ValueError("disaster_stop_pct must be a fraction between 0 and 1")
        return value


class EntrySignal(BaseModel):
    """One pre-earnings setup inside a batch payload."""

    signal_id: str
    ticker: str
    side: Annotated[str, Field(pattern=r"^(BUY|SELL)$")]
    reference_price: float = Field(gt=0)
    limit_price: float | None = None
    entry_cutoff_at: str
    exit_rules: ExitRules
    batch_id: str = ""


class BatchSignal(BaseModel):
    """1-12 sized setups for the 3:50 PM pre-earnings window."""

    event: Literal["BATCH_SIGNAL"] = BATCH_SIGNAL_EVENT
    batch_id: str
    signals: list[EntrySignal]

    @field_validator("signals")
    @classmethod
    def _batch_bounds(cls, value: list[EntrySignal]) -> list[EntrySignal]:
        if not 1 <= len(value) <= MAX_SIGNALS_PER_BATCH:
            raise ValueError(
                f"a batch must contain 1-{MAX_SIGNALS_PER_BATCH} signals, got {len(value)}"
            )
        return value


class ExitSignal(BaseModel):
    """Server-fired liquidation instruction (POST_EARNINGS_OPEN_CROSS)."""

    event: Literal["EXIT_SIGNAL"] = EXIT_SIGNAL_EVENT
    action: Annotated[str, Field(pattern=r"^EXIT_ALL$")]
    reason: str


class ClosePositionSignal(BaseModel):
    """Server-fired dynamic exit for a single position (e.g. target hit)."""

    event: Literal["CLOSE_POSITION"] = CLOSE_POSITION_EVENT
    ticker: str
    percentage: float = Field(gt=0, le=100)
    reason: str


class SystemPing(BaseModel):
    event: Literal["SYSTEM_PING"] = SYSTEM_PING_EVENT


class SystemPong(BaseModel):
    event: Literal["SYSTEM_PONG"] = SYSTEM_PONG_EVENT
    broker_connected: bool = False


def parse_wire_event(raw: str | bytes | dict) -> SystemPing | SystemPong | None:
    """Return parsed control-plane events, None if unknown/unparseable."""
    try:
        if isinstance(raw, dict):
            data = raw
        else:
            data = json.loads(raw)
        event_name = data.get("event")
    except (json.JSONDecodeError, AttributeError):
        return None

    if event_name == SYSTEM_PING_EVENT:
        return SystemPing.model_validate(data)
    if event_name == SYSTEM_PONG_EVENT:
        return SystemPong.model_validate(data)
    return None


def parse_batch_signal(data: dict) -> BatchSignal:
    """Parse ENTRY payload; raises pydantic ValidationError on malformed shape."""
    payload = dict(data)
    payload.setdefault("event", BATCH_SIGNAL_EVENT)
    return BatchSignal.model_validate(payload)


def parse_exit_signal(data: dict) -> ExitSignal:
    payload = dict(data)
    payload.setdefault("event", EXIT_SIGNAL_EVENT)
    return ExitSignal.model_validate(payload)


def parse_close_position(data: dict) -> ClosePositionSignal:
    payload = dict(data)
    payload.setdefault("event", CLOSE_POSITION_EVENT)
    return ClosePositionSignal.model_validate(payload)


def is_stale(entry_cutoff_at: str, now: datetime | None = None) -> bool:
    """True when an entry window has already closed — never trade stale."""
    cutoff = _parse_et(entry_cutoff_at)
    if cutoff is None:
        return True  # unparsable cutoff -> treat as stale / unusable
    now_et = (now or datetime.now(UTC)).astimezone(ET)
    return now_et > cutoff


def _parse_et(value: str | datetime | None) -> datetime | None:
    """Parse an ISO 8601 timestamp into a timezone-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ET)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed


class SignalSource(Protocol):
    """Handshake the SignalListener exposes to its handlers."""

    @property
    def connected(self) -> bool: ...

    @property
    def last_heartbeat_at(self) -> float | None: ...

    def connection_age_seconds(self) -> float: ...

