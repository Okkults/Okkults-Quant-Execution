"""Application settings.

Loads configuration from (highest precedence first):
1. Environment variables
2. ``.env`` file in the working directory
3. Built-in defaults below

Broker credentials and the Okkults license key live only on this machine.

Safety invariants enforced here (not just in the wizard):
* per-ticker allocation can never exceed 10% of equity
* total simultaneous exposure can never exceed 30% of equity
* live trading requires ``ALPACA_IS_PAPER=false`` and ``LIVE_TRADING_ACK=YES``
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HARD_MAX_TRADE_PCT: float = 0.10
HARD_MAX_PORTFOLIO_PCT: float = 0.30
DEFAULT_TRADE_PCT: float = 0.05


class Settings(BaseSettings):
    """All runtime configuration for the Okkults execution client."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Okkults signal server -------------------------------------------
    OKKULTS_LICENSE_KEY: str = ""
    OKKULTS_WS_URL: str = "wss://okkults-api.okkults.workers.dev/v1/stream"

    # --- broker ------------------------------------------------------------
    BROKER: Literal["alpaca", "ibkr"] = "alpaca"
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    # Paper trading is the safe default. Going live requires both
    # ALPACA_IS_PAPER=false and an explicit typed acknowledgment.
    ALPACA_IS_PAPER: bool = True
    LIVE_TRADING_ACK: str = "NO"

    # --- execution behavior ------------------------------------------------
    EXECUTION_MODE: Literal["semi_auto", "full_auto"] = "full_auto"

    # --- risk caps ----------------------------------------------------------
    # 5% per ticker by default; 10% is a hard ceiling.
    MAX_TRADE_ALLOCATION_PCT: float = DEFAULT_TRADE_PCT
    # 30% total exposure at any one time; 30% is a hard ceiling.
    MAX_PORTFOLIO_RISK_PCT: float = HARD_MAX_PORTFOLIO_PCT
    PER_TRADE_NOTIONAL_CAP: float | None = None
    CASH_RESERVE: float = 0.0
    # SELL signals are ignored unless the operator explicitly opts in.
    ALLOW_SHORT_SELLING: bool = False

    # --- local failsafe ------------------------------------------------------
    # If the 9:30:15 AM EXIT_SIGNAL never arrives (server outage) and
    # positions opened by this client are still open, liquidate them at
    # 9:30:16 AM ET. Toggleable; never touches unrelated holdings.
    LOCAL_EXIT_FAILSAFE: bool = True

    @field_validator("OKKULTS_LICENSE_KEY")
    @classmethod
    def _license_prefix(cls, value: str) -> str:
        value = value.strip()
        if value and not (value.startswith("ok_live_") or value.startswith("ok_paper_")):
            raise ValueError(
                "OKKULTS_LICENSE_KEY must start with 'ok_live_' or 'ok_paper_'"
            )
        return value

    @field_validator("MAX_TRADE_ALLOCATION_PCT")
    @classmethod
    def _per_trade_cap(cls, value: float) -> float:
        if not 0.0 < value <= HARD_MAX_TRADE_PCT:
            raise ValueError(
                "MAX_TRADE_ALLOCATION_PCT must be greater than 0 and at most "
                f"{HARD_MAX_TRADE_PCT:.0%}"
            )
        return value

    @field_validator("MAX_PORTFOLIO_RISK_PCT")
    @classmethod
    def _portfolio_cap(cls, value: float) -> float:
        if not 0.0 < value <= HARD_MAX_PORTFOLIO_PCT:
            raise ValueError(
                "MAX_PORTFOLIO_RISK_PCT must be greater than 0 and at most "
                f"{HARD_MAX_PORTFOLIO_PCT:.0%}"
            )
        return value

    @field_validator("CASH_RESERVE")
    @classmethod
    def _cash_reserve(cls, value: float) -> float:
        if value < 0:
            raise ValueError("CASH_RESERVE cannot be negative")
        return value

    @field_validator("PER_TRADE_NOTIONAL_CAP", mode="before")
    @classmethod
    def _blank_notional_cap(cls, value: Any) -> Any:
        """An empty .env line means 'no cap', not a float-parse failure."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("PER_TRADE_NOTIONAL_CAP")
    @classmethod
    def _notional_cap(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("PER_TRADE_NOTIONAL_CAP must be positive when set")
        return value

    @model_validator(mode="after")
    def _live_requires_ack(self) -> Self:
        if not self.ALPACA_IS_PAPER and self.LIVE_TRADING_ACK.strip().upper() != "YES":
            raise ValueError(
                "Live trading requires LIVE_TRADING_ACK=YES. Re-run "
                "`python main.py --onboard` and type the acknowledgment."
            )
        return self


def missing_required_fields(settings: Settings) -> list[str]:
    """Return the names of required settings that are still empty.

    A config is "ready to run" when this returns an empty list.
    """
    missing: list[str] = []
    if not settings.OKKULTS_LICENSE_KEY:
        missing.append("OKKULTS_LICENSE_KEY")
    if settings.BROKER == "alpaca":
        if not settings.ALPACA_API_KEY:
            missing.append("ALPACA_API_KEY")
        if not settings.ALPACA_SECRET_KEY:
            missing.append("ALPACA_SECRET_KEY")
    elif settings.BROKER == "ibkr":
        missing.append("BROKER")  # ibkr driver not implemented yet
    return missing


def load_settings(**overrides: Any) -> Settings:
    """Build a validated Settings instance from env/.env/overrides."""
    return Settings(**overrides)


def license_kind(key: str) -> str:
    """Human label for a license key prefix, or 'unknown'."""
    if key.startswith("ok_live_"):
        return "live license"
    if key.startswith("ok_paper_"):
        return "paper license"
    return "unknown license"


__all__ = [
    "DEFAULT_TRADE_PCT",
    "HARD_MAX_PORTFOLIO_PCT",
    "HARD_MAX_TRADE_PCT",
    "Settings",
    "license_kind",
    "load_settings",
    "missing_required_fields",
]
