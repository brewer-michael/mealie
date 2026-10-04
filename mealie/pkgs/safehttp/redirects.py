"""
Fork: the redirects safehttp won't follow. httpx follows a `Location` to any scheme, and the curl transport then fetches
it: a redirect to `file:///etc/passwd` returned the file. The transport still checks the host of every hop; this
refuses a hop that leaves http(s), or goes from https to http (deliberately: a downgrade lets anyone on the network path
swap the page or image).

Use `check_redirect` as an httpx `response` event hook (`acheck_redirect` on an `AsyncClient`).
"""

import httpx

from .transport import InvalidDomainError

SAFE_SCHEMES = frozenset({"http", "https"})


class UnsafeRedirectError(InvalidDomainError):
    """
    A redirect to a scheme other than http(s), or from https to http (`downgrade`).

    It stays an `InvalidDomainError`, so a caller that only knows that one still refuses the fetch. Callers that tell a
    user why should catch this first: the host was allowed, so "not an allowed domain" is the wrong reason.
    """

    def __init__(self, message: str, *, downgrade: bool = False) -> None:
        super().__init__(message)
        self.downgrade = downgrade


def check_redirect(response: httpx.Response) -> None:
    """Raises `UnsafeRedirectError` when `response` redirects somewhere safehttp doesn't follow"""
    if not response.has_redirect_location:
        return

    source = response.request.url
    try:
        target = source.join(response.headers["Location"])
    except httpx.InvalidURL:
        return  # httpx refuses it when it builds the redirect

    if target.scheme not in SAFE_SCHEMES:
        raise UnsafeRedirectError(f"refusing to follow a redirect from {source} to a {target.scheme}: URL")
    if source.scheme == "https" and target.scheme == "http":
        raise UnsafeRedirectError(f"refusing to follow a redirect from {source} to plain http", downgrade=True)


async def acheck_redirect(response: httpx.Response) -> None:
    """`check_redirect` for an `AsyncClient`, whose event hooks are awaited"""
    check_redirect(response)
