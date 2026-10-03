import httpx
import openai

from mealie.services.ai.errors import AIProviderError, AIProviderUnsupportedError, describe_provider_error


def _sdk_error() -> openai.AuthenticationError:
    response = httpx.Response(
        status_code=401,
        request=httpx.Request("GET", "https://internal-host.test/v1/models"),
        content=b"<html>top secret internal page</html>",
    )
    return openai.AuthenticationError(
        message="Error code: 401 - top secret detail", response=response, body={"error": "top secret detail"}
    )


def test_describes_type_and_status_only():
    assert describe_provider_error(_sdk_error()) == "AuthenticationError (HTTP 401)"


def test_describes_the_cause_of_a_wrapped_error():
    try:
        try:
            raise _sdk_error()
        except Exception as e:
            raise Exception(f"OpenAI Request Failed. {e}") from e
    except Exception as wrapped:
        assert describe_provider_error(wrapped) == "AuthenticationError (HTTP 401)"


def test_errors_without_a_status_are_named_only():
    assert describe_provider_error(ValueError("secret")) == "ValueError"


def test_our_own_errors_keep_their_message():
    assert describe_provider_error(AIProviderUnsupportedError("Not available yet.")) == "Not available yet."

    try:
        raise AIProviderError("Monthly limit reached.") from _sdk_error()
    except AIProviderError as e:
        assert describe_provider_error(e) == "Monthly limit reached."
