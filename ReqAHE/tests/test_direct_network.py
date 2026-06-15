import os
from unittest.mock import patch

from reqahe.config import apply_direct_network_env, parse_bool


def test_parse_bool() -> None:
    assert parse_bool("false") is False
    assert parse_bool("true") is True
    assert parse_bool(True) is True
    assert parse_bool(None, default=True) is True


def test_apply_direct_network_env_clears_proxy_and_sets_no_proxy(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_TRUST_ENV", "false")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("OPENAI_NO_PROXY", "open.bigmodel.cn,localhost,127.0.0.1")

    apply_direct_network_env()

    assert "HTTP_PROXY" not in os.environ
    assert "HTTPS_PROXY" not in os.environ
    assert "open.bigmodel.cn" in os.environ["NO_PROXY"]


def test_apply_direct_network_env_skips_when_trust_env_enabled(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_TRUST_ENV", "true")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")

    apply_direct_network_env()

    assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:7890"
