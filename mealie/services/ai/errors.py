"""Errors raised by the fork's AI provider layer (docs/ai/PHASE1.md)"""


class AIProviderError(Exception):
    """
    Base class for errors raised by the AI provider layer itself.

    Their messages are written by us, never copied from a provider's response, so they're safe to
    show to the user.
    """


class AIProviderUnsupportedError(AIProviderError):
    """The provider, or its protocol, can't do what was asked (e.g. audio attachments on Claude)"""


class AIProviderLimitReachedError(AIProviderError):
    """Every provider that could handle a request has used up its monthly token limit"""


class AIProviderRefusedError(AIProviderError):
    """The model declined the request (Claude's `refusal` stop reason)"""


class AIProviderOutputTruncatedError(AIProviderError):
    """The model ran out of output tokens before finishing its answer (Claude's `max_tokens` stop reason)"""


def is_rate_limit_error(error: BaseException) -> bool:
    """Whether `error` is a provider's rate limit (HTTP 429) response, from either SDK"""
    import anthropic
    import openai

    return isinstance(error, openai.RateLimitError | anthropic.RateLimitError)


def describe_provider_error(error: BaseException) -> str:
    """
    A user-safe description of a failed provider call: the error's type and HTTP status only, as
    upstream's connection test reports it (e.g. `AuthenticationError (HTTP 401)`).

    The provider's response body is never included: group managers can point `base_url` at any host,
    including internal ones, and must not be able to read that host's replies back through Mealie.
    Log the full error instead. `AIProviderError` messages are ours, so those are returned as-is.
    """
    if isinstance(error, AIProviderError):
        return str(error) or type(error).__name__

    cause = error.__cause__ or error
    if isinstance(cause, AIProviderError):
        return str(cause) or type(cause).__name__

    status = getattr(cause, "status_code", None)
    name = type(cause).__name__
    return f"{name} (HTTP {status})" if status else name
