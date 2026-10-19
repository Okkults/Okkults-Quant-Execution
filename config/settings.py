"""Application settings.

Loads configuration from (highest precedence first):
1. Environment variables
2. ``.env`` file in the working directory
3. Optional ``config.yaml`` overlay (env vars still win)
4. Built-in defaults below

Broker credentials and the Okkults license key live only on this machine.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Paper trading is the safe default; going live is an explicit opt-in.
    ALPACA_IS_PAPER: bool = True

    # --- execution behavior ------------------------------------------------
    EXECUTION_MODE: Literal["semi_auto", "full_auto"] = "full_auto"

    # --- risk caps ----------------------------------------------------------
    MAX_PORTFOLIO_RISK_PCT: float = 0.30
    MAX_TRADE_ALLOCATION_PCT: float = 0.10
    PER_TRADE_NOTIONAL_CAP: float | None = None
    CASH_RESERVE: float = 0.0
    # Emergency stop. The server may override per-signal via exit_rules.
    DISASTER_STOP_PCT: float = 0.07

    # --- local failsafe ------------------------------------------------------
    # If the 9:30:15 AM EXIT_SIGNAL never arrives (server outage) and
    # positions are still open, liquidate locally at 9:30:16 AM ET.
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

    @field_validator("PER_TRADE_NOTIONAL_CAP", mode="before")
    @classmethod
    def _blank_notional_cap(cls, value: Any) -> Any:
        """An empty .env line means 'no cap', not a float-parse failure."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


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
    "Settings",
    "license_kind",
    "load_settings",
    "missing_required_fields",
]
