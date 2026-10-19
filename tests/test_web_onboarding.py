"""Browser onboarding tests — localhost HTTP surface, token, and env writes."""

import json
import os
import stat
import urllib.error
import urllib.request

import pytest

from core.envwriter import read_env_file, write_env_file
from core.web_onboarding import WebOnboardingServer


async def fake_broker_probe(api_key: str, secret_key: str, paper: bool) -> str:
    del api_key, secret_key
    mode = "PAPER" if paper else "LIVE"
    return f"OK — connected, equity $50,000.00 ({mode})"


def fake_license_factory(key: str):
    assert key.startswith("ok_paper_") or key.startswith("ok_live_")

    async def probe() -> str | None:
        return None

    return probe


@pytest.fixture
def server(tmp_path):
    srv = WebOnboardingServer(
        env_path=tmp_path / ".env",
        broker_probe=fake_broker_probe,
        license_probe_factory=fake_license_factory,
    )
    srv.start()
    yield srv
    srv.stop()


def base_url(srv: WebOnboardingServer) -> str:
    return srv.url.split("/?")[0]


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post(url: str, payload: dict, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Okkults-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def valid_payload() -> dict:
    return {
        "trading_mode": "paper",
        "alpaca_key": "PKTEST123",
        "alpaca_secret": "SKTEST456",
        "max_trade_pct": 5,
        "max_portfolio_pct": 30,
        "allow_short_selling": False,
        "license_key": "ok_paper_" + "X" * 30,
        "execution_mode": "full_auto",
        "local_exit_failsafe": True,
    }


class TestPage:
    def test_requires_token(self, server) -> None:
        status, _ = get(base_url(server) + "/")
        assert status == 401

    def test_renders_bootstrap(self, server) -> None:
        status, body = get(server.url)
        assert status == 200
        assert "Choose an account" in body
        assert server.token in body


class TestVerify:
    def test_broker_verdict(self, server) -> None:
        status, data = post(
            base_url(server) + "/api/verify-broker",
            {"trading_mode": "paper", "alpaca_key": "PK1", "alpaca_secret": "SK1"},
            server.token,
        )
        assert status == 200
        assert data["ok"] is True
        assert data["verdict"].startswith("OK")

    def test_bad_token_rejected(self, server) -> None:
        status, _ = post(
            base_url(server) + "/api/verify-broker",
            {"trading_mode": "paper", "alpaca_key": "PK1", "alpaca_secret": "SK1"},
            "wrong",
        )
        assert status == 401


class TestSubmit:
    def test_writes_env_and_finishes(self, server, tmp_path) -> None:
        status, data = post(
            base_url(server) + "/api/submit", valid_payload(), server.token
        )
        assert status == 200
        assert data["ok"] is True
        env_file = tmp_path / ".env"
        written = read_env_file(env_file)
        assert written["ALPACA_API_KEY"] == "PKTEST123"
        assert written["ALPACA_IS_PAPER"] == "true"
        assert written["MAX_TRADE_ALLOCATION_PCT"] == "0.05"
        assert written["MAX_PORTFOLIO_RISK_PCT"] == "0.3"
        assert written["LIVE_TRADING_ACK"] == "NO"
        assert "DISASTER_STOP_PCT" not in written
        assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
        assert server.wait(timeout=2).alpaca_api_key == "PKTEST123"

    def test_rejects_oversized_risk(self, server, tmp_path) -> None:
        payload = valid_payload()
        payload["max_trade_pct"] = 15
        status, data = post(
            base_url(server) + "/api/submit", payload, server.token
        )
        assert status == 400
        assert data["ok"] is False
        assert not (tmp_path / ".env").exists()

    def test_live_requires_typed_ack(self, server, tmp_path) -> None:
        payload = valid_payload()
        payload["trading_mode"] = "live"
        status, data = post(
            base_url(server) + "/api/submit", payload, server.token
        )
        assert status == 400
        assert "I UNDERSTAND" in data["error"]
        assert not (tmp_path / ".env").exists()

    def test_live_ack_saves_yes(self, server, tmp_path) -> None:
        payload = valid_payload()
        payload["trading_mode"] = "live"
        payload["live_ack"] = "I UNDERSTAND"
        status, data = post(
            base_url(server) + "/api/submit", payload, server.token
        )
        assert status == 200
        written = read_env_file(tmp_path / ".env")
        assert written["ALPACA_IS_PAPER"] == "false"
        assert written["LIVE_TRADING_ACK"] == "YES"

    def test_blank_fields_keep_existing_credentials(self, server, tmp_path) -> None:
        env_file = tmp_path / ".env"
        write_env_file(
            {
                "ALPACA_API_KEY": "PKOLD",
                "ALPACA_SECRET_KEY": "SKOLD",
                "OKKULTS_LICENSE_KEY": "ok_paper_" + "Y" * 30,
            },
            env_file,
        )
        fresh = WebOnboardingServer(
            env_path=env_file,
            broker_probe=fake_broker_probe,
            license_probe_factory=fake_license_factory,
        )
        fresh.start()
        try:
            payload = valid_payload()
            payload["alpaca_key"] = ""
            payload["alpaca_secret"] = ""
            payload["license_key"] = ""
            status, data = post(
                base_url(fresh) + "/api/submit", payload, fresh.token
            )
            assert status == 200
            written = read_env_file(env_file)
            assert written["ALPACA_API_KEY"] == "PKOLD"
            assert written["OKKULTS_LICENSE_KEY"].startswith("ok_paper_")
        finally:
            fresh.stop()
