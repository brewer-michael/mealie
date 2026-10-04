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


class AIProviderLocalOnlyError(AIProviderError):
    """
    The request has to stay on this server's network (a "local only" recipe card, docs/ai/PHASE2.md §10), and
    none of the slot's providers is marked as running locally at a private address
    """

    def __init__(self, message: str = "None of the AI providers for this task runs on your own network.") -> None:
        super().__init__(message)


# ==========================================
# Recipe card ingestion (docs/ai/PHASE2.md §3.9)


class IngestPaused(Exception):
    """
    Recipe card ingestion is paused while a backup is restored: the pause marker is set, or the restore holds the
    ingest write lock. Nothing was written; try again once the restore is done.
    """


class IngestBusyError(Exception):
    """
    A backup restore gave up waiting for writes to finish (recipe card ingestion's, or another change to Mealie that
    holds the write section), before it changed anything. The restore can be tried again in a moment.
    """

    def __init__(self, message: str = "Mealie is still saving changes. Try the restore again.") -> None:
        super().__init__(message)
