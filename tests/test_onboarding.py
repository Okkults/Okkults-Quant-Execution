"""Onboarding tests — routing, validation, atomic env writes, gated persistence."""

import os
import stat

from config.settings import load_settings, missing_required_fields
from core.envwriter import mask_secret, merge_env_updates, read_env_file, write_env_file
from core.onboarding import (
    OnboardingWizard,
    allocation_warning,
    alpaca_key_error,
    license_error,
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


# --------------------------------------------------------------------- #
# pure validation helpers
# --------------------------------------------------------------------- #
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
        warning = allocation_warning(0.30)
        assert "violates statistical risk parity" in warning
        assert "30.0%" in warning

    def test_mock_masking(self) -> None:
        assert mask_secret("ok_live_secretsecret") == "***ret"
        assert mask_secret("") == "<not set>"
        assert mask_secret("small") == "***"


# --------------------------------------------------------------------- #
# env writer: atomic + permissive-safe
# --------------------------------------------------------------------- #
class TestEnvWriter:
    def test_write_and_read_roundtrip(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        write_env_file({"OKKULTS_LICENSE_KEY": "ok_live_test", "BROKER": "alpaca"}, env_file)
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


# --------------------------------------------------------------------- #
# wizard E2E with scripted prompter
# --------------------------------------------------------------------- #
def quick_answers(profile: str = "standard") -> list[str]:
    """Scripted answers for one full wizard run ending with a risk profile."""
    return [
        valid_license(),
        "alpaca",
        "PKTEST123",
        "SKTEST456",
        "paper",
        "full_auto",
        profile,           # risk profile
        "true",            # failsafe
    ]


def fake_license_probe_factory(key: str):
    assert key.startswith("ok_paper_") or key.startswith("ok_live_")

    async def probe() -> str | None:
        return None  # server "accepts"

    return probe


async def fake_broker_probe() -> str:
    return "OK — connected, equity $50,000.00 (PAPER)"


class TestWizard:
    async def test_happy_path_writes_and_verifies(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(quick_answers()),
            env_path=str(env_file),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        answers = await wizard.run()
        assert answers.license_key == valid_license()
        written = read_env_file(env_file)
        assert written["OKKULTS_LICENSE_KEY"] == valid_license()
        assert written["EXECUTION_MODE"] == "full_auto"
        assert written["ALPACA_IS_PAPER"] == "true"
        assert written.get("PER_TRADE_NOTIONAL_CAP") not in ("", "None")
        mode = stat.S_IMODE(os.stat(env_file).st_mode)
        assert mode == 0o600

    async def test_failed_broker_probe_would_not_arm_but_env_persists(
        self, tmp_path
    ) -> None:
        env_file = tmp_path / ".env"

        async def failing_probe() -> str:
            return "FAIL — Alpaca rejected the credentials"

        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(quick_answers()),
            env_path=str(env_file),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=failing_probe,
        )
        answers = await wizard.run()
        # OpenClaw rule: verification failure never silently "succeeds"
        # (env is still written here, but health_check returned False -> the
        # terminal refuses to arm, see main.resolve_config + preflight)
        assert answers.license_key == valid_license()

    async def test_custom_lane_oversized_allocation_gets_capped(self, tmp_path) -> None:
        answers_script = [
            valid_license(),
            "alpaca",
            "PK1",
            "SK1",
            "paper",
            "semi_auto",
            "custom",     # risk profile -> walks every knob
            "0.40",       # portfolio
            "0.25",       # per-ticker -> triggers warning
            "n",          # decline override
            "0",
            "0",
            "0.07",
            "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(answers_script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.max_trade_allocation_pct == 0.10

    async def test_live_endpoint_requires_typed_ack(self, tmp_path) -> None:
        script = [
            valid_license(),
            "alpaca",
            "PK1",
            "SK1",
            "live",
            "nope",       # WRONG acknowledgment -> falls back to paper
            "full_auto",
            "standard",
            "true",
        ]
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        assert wizard.answers.alpaca_is_paper is True


# --------------------------------------------------------------------- #
# risk profiles (install-time custom profile selection)
# --------------------------------------------------------------------- #
class TestRiskProfiles:
    def profile_script(self, profile_answer: str, extra: list[str]) -> list[str]:
        """Shared pre-profile script; the profile answer tail is caller-built."""
        return [
            valid_license(),
            "alpaca",
            "PK1",
            "SK1",
            "paper",
            "full_auto",
            profile_answer,
            *extra,
        ]

    async def _run(self, tmp_path, script) -> OnboardingWizard:
        wizard = OnboardingWizard(
            prompter=ScriptedPrompter(script),
            env_path=str(tmp_path / ".env"),
            license_probe_factory=fake_license_probe_factory,
            broker_probe=fake_broker_probe,
        )
        await wizard.run()
        return wizard

    async def test_conservative_profile_caps(self, tmp_path) -> None:
        from core.onboarding import RISK_PROFILES

        wizard = await self._run(
            tmp_path, self.profile_script("conservative", ["true"])
        )
        assert wizard.answers.risk_profile == "conservative"
        assert wizard.answers.max_trade_allocation_pct == RISK_PROFILES["conservative"]["max_trade_allocation_pct"] == 0.05
        assert wizard.answers.max_portfolio_risk_pct == 0.20
        assert wizard.answers.disaster_stop_pct == 0.05
        written = read_env_file(tmp_path / ".env")
        assert written["MAX_TRADE_ALLOCATION_PCT"] == "0.05"

    async def test_standard_is_default_profile(self, tmp_path) -> None:
        wizard = await self._run(tmp_path, self.profile_script("", ["true"]))
        assert wizard.answers.risk_profile == "standard"
        assert wizard.answers.max_trade_allocation_pct == 0.10

    async def test_aggressive_declining_override_falls_back_to_standard(
        self, tmp_path
    ) -> None:
        wizard = await self._run(
            tmp_path, self.profile_script("aggressive", ["n", "true"])
        )
        assert wizard.answers.risk_profile == "standard"
        assert wizard.answers.max_trade_allocation_pct == 0.10
        assert wizard.answers.max_portfolio_risk_pct == 0.30

    async def test_aggressive_confirmed_override_keeps_aggressive(
        self, tmp_path
    ) -> None:
        wizard = await self._run(
            tmp_path, self.profile_script("aggressive", ["y", "true"])
        )
        assert wizard.answers.risk_profile == "aggressive"
        assert wizard.answers.max_trade_allocation_pct == 0.15
        assert wizard.answers.max_portfolio_risk_pct == 0.40
        written = read_env_file(tmp_path / ".env")
        assert written["MAX_PORTFOLIO_RISK_PCT"] == "0.4"

    async def test_custom_profile_walks_every_knob(self, tmp_path) -> None:
        script = [
            valid_license(),
            "alpaca",
            "PK1",
            "SK1",
            "paper",
            "full_auto",
            "custom",       # profile
            "0.25",         # portfolio
            "0.06",         # per-ticker
            "5000",         # notional cap
            "1000",         # cash reserve
            "0.08",         # disaster stop
            "false",        # failsafe
        ]
        wizard = await self._run(tmp_path, script)
        assert wizard.answers.risk_profile == "custom"
        assert wizard.answers.max_trade_allocation_pct == 0.06
        assert wizard.answers.per_trade_notional_cap == 5000.0
        assert wizard.answers.cash_reserve == 1000.0
        assert wizard.answers.disaster_stop_pct == 0.08
        assert wizard.answers.local_exit_failsafe is False
        written = read_env_file(tmp_path / ".env")
        assert written["PER_TRADE_NOTIONAL_CAP"] == "5000.0"
        assert written["LOCAL_EXIT_FAILSAFE"] == "false"


# --------------------------------------------------------------------- #
# config routing
# --------------------------------------------------------------------- #
class TestConfigRouting:
    def test_missing_fields_route_to_wizard(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        from config.settings import Settings

        settings = Settings(OKKULTS_LICENSE_KEY="", ALPACA_API_KEY="", ALPACA_SECRET_KEY="")
        assert set(missing_required_fields(settings)) == {
            "OKKULTS_LICENSE_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"
        }

    def test_complete_config_is_runnable(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        from config.settings import Settings

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
