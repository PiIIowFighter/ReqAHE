from unittest.mock import patch

import pytest

from reqahe.infra.llm_client import LLMResult, OpenAICompatibleClient, _is_retryable_error


def test_is_retryable_error_detects_connection_failures() -> None:
    assert _is_retryable_error("Connection error.")
    assert _is_retryable_error("HTTP 429 rate limit exceeded")
    assert not _is_retryable_error("Missing OPENAI_API_KEY or model.")


def test_require_chat_retries_transient_errors() -> None:
    client = OpenAICompatibleClient(api_key="k", model="m", max_retries=3, retry_base_seconds=0.01)
    responses = [
        LLMResult(False, "", "Connection error."),
        LLMResult(True, '{"action": "ask_question"}', None),
    ]

    with patch.object(client, "chat", side_effect=responses) as chat_mock:
        text = client.require_chat([{"role": "user", "content": "hi"}], purpose="test call")

    assert text == '{"action": "ask_question"}'
    assert chat_mock.call_count == 2


def test_require_chat_does_not_retry_auth_errors() -> None:
    client = OpenAICompatibleClient(api_key="k", model="m", max_retries=3, retry_base_seconds=0.01)

    with patch.object(client, "chat", return_value=LLMResult(False, "", "invalid api key")) as chat_mock:
        with pytest.raises(RuntimeError, match="invalid api key"):
            client.require_chat([{"role": "user", "content": "hi"}], purpose="test call")

    assert chat_mock.call_count == 1


def test_require_chat_retries_empty_responses() -> None:
    client = OpenAICompatibleClient(api_key="k", model="m", max_retries=3, retry_base_seconds=0.01)
    responses = [
        LLMResult(False, "", "empty model response (finish_reason=stop)"),
        LLMResult(True, '{"action": "finish_interview"}', None),
    ]

    with patch.object(client, "chat", side_effect=responses) as chat_mock:
        text = client.require_chat([{"role": "user", "content": "hi"}], purpose="test call")

    assert text == '{"action": "finish_interview"}'
    assert chat_mock.call_count == 2
