"""Onboarding tests — requested order, validation, verification, env writes."""

import os
import stat
from pathlib import Path

import pytest

from config.settings import Settings, load_settings, missing_required_fields
from core.envwriter import mask_secret, merge_env_updates, read_env_file, write_env_file
from core.onboarding import (
    OnboardingWizard,
    allocation_warning,
    alpaca_key_error,
    license_error,
    risk_pct_error,
)


class ScriptedPrompter:
    """Plays a scripted sequence of wizard answers."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.questions: list[str] = []

    def _next(self, prompt: str) -> str:
        self.questions.append(prompt)
        return self._answers.pop(0) if self._answers else ""

    def text(self, prompt: str, default: str = "") -> str:
        return self._next(prompt) or default

    def secret(self, prompt: str) -> str:
        return self._next(prompt)

    def choice(self, prompt: str, options: list[str], default: str) -> str:
        return self._next(prompt) or default

    def float(self, prompt: str, default: float) -> float:
        raw = self._next(prompt)
        return float(raw) if raw else default

    def confirm(self, prompt: str, default: bool = True) -> bool:
        raw = self._next(prompt)
        return default if raw == "" else raw.lower() in ("y", "true", "yes")


def valid_license() -> str:
    return "ok_paper_" + "X" * 30


def paper_answers(profile: str = "standard") -> list[str]:
    """Full paper-mode script in the required onboarding order."""
    del profile
    return [
        "paper",           # 1. paper/live
        "PKTEST123",       # 2. Alpaca key id
        "SKTEST456",       # 3. Alpaca secret
        "5",               # 4. risk per trade (%)
        "30",              # 5. max total risk at one time (%)
        "false",           # 6. allow short selling
        valid_license(),   # 7. Okkults API key
        "full_auto",       # 8. execution mode
        "true",            # 9. local failsafe
    ]


async def fake_broker_probe(api_key: str, secret_key: str, paper: bool) -> str:
    assert api_key
    assert secret_key
    return f"OK — connected, equity $50,000.00 ({'PAPER' if paper else 'LIVE'})"


def fake_license_probe_factory(key: str):
    assert key.startswith("ok_paper_") or key.startswith("ok_live_")

    async def probe() -> str | None:
        return None  # server "accepts"

    return probe


class TestValidation:
    def test_license_shapes(self) -> None:
        assert license_error("ok_live_ABCDEF") is None
        assert license_error("ok_paper_ABCDEF") is None
        assert license_error("") is not None
        assert license_error("sk-123") is not None
        assert license_error("ok_live_") is not None

    def test_alpaca_key_errors(self) -> None:
        assert alpaca_key_error("PK123", "SK456") is None
        assert alpaca_key_error("", "SK456") is not None
        assert alpaca_key_error("PK123", "PK123") is not None

    def test_allocation_warning_text(self) -> None:
        warning = allocation_warning(0.15)
        assert "violates statistical risk parity" in warning
        assert "15.0%" in warning
        assert "hard 10% cap" in warning

    def test_risk_pct_error(self) -> None:
        assert risk_pct_error("trade", 0.05, maximum=0.10) is None
        assert "cannot exceed 10%" in (
            risk_pct_error("trade", 0.15, maximum=0.10) or ""
        )
        assert "greater than 0%" in (
            risk_pct_error("trade", 0.0, maximum=0.10) or ""
        )

    def test_mock_masking(self) -> None:
        assert mask_secret("ok_live_secretsecret") == "***ret"
        assert mask_secret("") == "<not set>"
        assert mask_secret("small") == "***"


class TestEnvWriter:
    def test_write_and_read_roundtrip(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        write_env_file(
            {"OKKULTS_LICENSE_KEY": "ok_live_test", "BROKER": "alpaca"}, env_file
        )
        data = read_env_file(env_file)
        assert data["OKKULTS_LICENSE_KEY"] == "ok_live_test"

    def test_file_is_chmod_600(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        write_env_file({"BROKER": "alpaca"}, env_file)
        mode = stat.S_IMODE(os.stat(env_file).st_mode)
        assert mode == 0o600

    def test_merge_keeps_current_on_blank(self) -> None:
        merged = merge_env_updates(
            {"OKKULTS_LICENSE_KEY": "ok_live_current"}, {"OKKULTS_LICENSE_KEY": ""}
        )
        assert merged["OKKULTS_LICENSE_KEY"] == "ok_live_current"

    def test_secrets_never_on_stdout(self, tmp_path, capsys) -> None:
        env_file = tmp_path / ".env"
        write_env_file({"ALPACA_SECRET_KEY": "supersecret123"}, env_file)
        captured = capsys.readouterr()
        assert "supersecret123" not in captured.out


class TestWizard:
    async def test_happy_path_writes_and_verifies(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        prompter = ScriptedPrompter(paper_answers())
        wizard = OnboardingWizard(
            prompter=prompter,
            env_path=str(env_file),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        answers = await wizard.run()

        assert answers.license_key == valid_license()
        assert answers.alpaca_is_paper is True
        assert answers.max_trade_allocation_pct == 0.05
        assert answers.max_portfolio_risk_pct == 0.30
        written = read_env_file(env_file)
        assert written["OKKULTS_LICENSE_KEY"] == valid_license()
        assert written["MAX_TRADE_ALLOCATION_PCT"] == "0.05"
        assert written["MAX_PORTFOLIO_RISK_PCT"] == "0.3"
        assert written["ALPACA_IS_PAPER"] == "true"
        assert written["LIVE_TRADING_ACK"] == "NO"
        assert written["ALLOW_SHORT_SELLING"] == "false"
        assert "DISASTER_STOP_PCT" not in written
        mode = stat.S_IMODE(os.stat(env_file).st_mode)
        assert mode == 0o600

        # Paper/live decision comes first, and risk is decided before the
        # Okkults key is requested.
        assert "Trading mode" in prompter.questions[0]
        trade_i = next(
            i for i, q in enumerate(prompter.questions) if "Max risk per trade" in q
        )
        license_i = next(
            i for i, q in enumerate(prompter.questions) if "License key" in q
        )
        assert trade_i < license_i

    async def test_reonboard_removes_legacy_stop_key(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        write_env_file({"DISASTER_STOP_PCT": "0.07"}, env_file)
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(paper_answers()),
            env_path=str(env_file),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert "DISASTER_STOP_PCT" not in read_env_file(env_file)

    async def test_live_requires_typed_ack(self, tmp_path) -> None:
        script = [
            "live",
            "nope",           # wrong acknowledgment -> falls back to paper
            "PK1", "SK1", "5", "30", "false", valid_license(),
            "full_auto", "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.alpaca_is_paper is True

    async def test_live_ack_saves_live_env(self, tmp_path) -> None:
        script = [
            "live",
            "I UNDERSTAND",
            "PK1", "SK1", "5", "30", "false", valid_license(),
            "full_auto", "true",
        ]
        env_file = tmp_path / ".env"
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(env_file),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.alpaca_is_paper is False
        assert read_env_file(env_file)["LIVE_TRADING_ACK"] == "YES"
        # The written config must re-load cleanly (live safety gate passes).
        loaded = load_settings(_env_file=str(env_file))
        assert loaded.ALPACA_IS_PAPER is False

    async def test_broker_failure_does_not_write(self, tmp_path) -> None:
        env_file = tmp_path / ".env"

        async def failing_probe(api_key: str, secret_key: str, paper: bool) -> str:
            del api_key, secret_key, paper
            return "FAIL — Alpaca rejected the credentials"

        script = ["paper", "PK1", "SK1", "n"]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(env_file),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=failing_probe,
        )
        with pytest.raises(SystemExit):
            await wizard.run()
        assert not Path(env_file).exists()

    async def test_broker_failure_can_retry(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        calls = {"n": 0}

        async def flaky_probe(api_key: str, secret_key: str, paper: bool) -> str:
            del api_key, secret_key, paper
            calls["n"] += 1
            if calls["n"] == 1:
                return "FAIL — Alpaca rejected the credentials"
            return "OK — connected"

        script = [
            "paper", "PK1", "SK1", "y", "PK2", "SK2",
            "5", "30", "false", valid_license(), "full_auto", "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(env_file),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=flaky_probe,
        )
        await wizard.run()
        assert calls["n"] >= 2  # retried, then health-checked again
        assert read_env_file(env_file)["ALPACA_API_KEY"] == "PK2"

    async def test_risk_caps_reask_instead_of_accepting_oversize(self, tmp_path) -> None:
        script = [
            "paper", "PK1", "SK1",
            "15", "5",       # per-trade 15% rejected, then 5%
            "40", "30",      # portfolio 40% rejected, then 30%
            "false", valid_license(), "full_auto", "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.max_trade_allocation_pct == 0.05
        assert wizard.answers.max_portfolio_risk_pct == 0.30

    async def test_risk_fraction_is_rejected(self, tmp_path) -> None:
        script = [
            "paper", "PK1", "SK1",
            "0.05", "5",     # fraction rejected, then 5%
            "30", "false", valid_license(), "full_auto", "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.max_trade_allocation_pct == 0.05

    async def test_portfolio_below_trade_is_rejected(self, tmp_path) -> None:
        script = [
            "paper", "PK1", "SK1",
            "10", "5", "30",  # 5% portfolio < 10% trade -> re-ask 30%
            "false", valid_license(), "full_auto", "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.max_trade_allocation_pct == 0.10
        assert wizard.answers.max_portfolio_risk_pct == 0.30

    async def test_rejected_license_reasks(self, tmp_path) -> None:
        def rejecting_factory(key: str):
            async def probe() -> str | None:
                if "bad" in key:
                    return "signal server rejected the license (InvalidStatus)"
                return None

            return probe

        good = "ok_paper_" + "G" * 30
        bad = "ok_paper_bad"
        script = [
            "paper", "PK1", "SK1", "5", "30", "false",
            bad, good, "full_auto", "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=rejecting_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.license_key == good


class TestRiskProfilesInSettings:
    def test_settings_enforce_hard_caps(self) -> None:
        with pytest.raises(ValueError):
            Settings(MAX_TRADE_ALLOCATION_PCT=0.15)
        with pytest.raises(ValueError):
            Settings(MAX_PORTFOLIO_RISK_PCT=0.31)

    def test_live_requires_acknowledgment(self) -> None:
        with pytest.raises(ValueError):
            Settings(ALPACA_IS_PAPER=False, LIVE_TRADING_ACK="NO")
        settings = Settings(ALPACA_IS_PAPER=False, LIVE_TRADING_ACK="YES")
        assert settings.ALPACA_IS_PAPER is False

    def test_defaults_are_conservative(self) -> None:
        settings = Settings()
        assert settings.MAX_TRADE_ALLOCATION_PCT == 0.05
        assert settings.MAX_PORTFOLIO_RISK_PCT == 0.30
        assert settings.ALLOW_SHORT_SELLING is False


class TestConfigRouting:
    def test_missing_fields_route_to_wizard(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        settings = Settings(
            OKKULTS_LICENSE_KEY="", ALPACA_API_KEY="", ALPACA_SECRET_KEY=""
        )
        assert set(missing_required_fields(settings)) == {
            "OKKULTS_LICENSE_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"
        }

    def test_complete_config_is_runnable(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        settings = Settings(
            OKKULTS_LICENSE_KEY=valid_license(),
            ALPACA_API_KEY="PK",
            ALPACA_SECRET_KEY="SK",
        )
        assert missing_required_fields(settings) == []

    def test_load_settings_roundtrip_from_env_text(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        write_env_file(
            {
                "OKKULTS_LICENSE_KEY": valid_license(),
                "BROKER": "alpaca",
                "ALPACA_API_KEY": "PKX",
                "ALPACA_SECRET_KEY": "SKX",
                "PER_TRADE_NOTIONAL_CAP": "",
            },
            tmp_path / ".env",
        )
        settings = load_settings(_env_file=str(tmp_path / ".env"))
        assert settings.PER_TRADE_NOTIONAL_CAP is None  # blank line -> no cap
        assert missing_required_fields(settings) == []
