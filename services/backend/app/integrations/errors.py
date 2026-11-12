"""Provider failures, reduced to safe categories (spec §10). Never tokens or raw responses."""

from __future__ import annotations

import contextlib
from datetime import timedelta

import httpx


class ProviderError(Exception):
    def __init__(self, category: str, *, retryable: bool, retry_after: timedelta | None = None):
        super().__init__(category)
        self.category = category
        self.retryable = retryable
        self.retry_after = retry_after


class AuthRevoked(ProviderError):
    """invalid_grant: the refresh token is dead (revoked, or expired after 7 days in Google Testing)."""

    def __init__(self) -> None:
        super().__init__("needs_reauth", retryable=False)


class AccessExpired(ProviderError):
    def __init__(self) -> None:
        super().__init__("access_expired", retryable=True)


class CursorExpired(ProviderError):
    """Google 410 on a sync token: discard it and run a full sync."""

    def __init__(self) -> None:
        super().__init__("sync_token_expired", retryable=True)


class PreconditionFailed(ProviderError):
    """412: the event changed at the provider since the version we hold (spec §10)."""

    def __init__(self) -> None:
        super().__init__("precondition_failed", retryable=False)


class Gone(ProviderError):
    """The event no longer exists at the provider."""

    def __init__(self) -> None:
        super().__init__("event_gone", retryable=False)


class Unrepresentable(ProviderError):
    """A local change the provider has no way to express (e.g. a rule Outlook can't store)."""

    def __init__(self, reason: str) -> None:
        super().__init__("unsupported_change", retryable=False)
        self.reason = reason


def retry_after(response: httpx.Response) -> timedelta | None:
    value = response.headers.get("Retry-After", "")
    return timedelta(seconds=int(value)) if value.isdigit() else None


def raise_for(response: httpx.Response, *, event: bool = False) -> None:
    """Map a failed response to a ProviderError. `event` marks per-event calls, where 404/410 mean
    the event is gone rather than the calendar."""
    if response.is_success:
        return
    status = response.status_code
    if status == 401:
        raise AccessExpired()
    reason = ""
    with contextlib.suppress(ValueError, AttributeError, IndexError, TypeError):
        error = response.json().get("error", {})
        reason = error.get("errors", [{}])[0].get("reason", "") or error.get("code", "")
    if status == 412 or (status == 409 and reason == "ErrorIrresolvableConflict"):  # Graph says either
        raise PreconditionFailed()
    if event and status in (404, 410):
        raise Gone()
    if status == 410:
        raise CursorExpired()
    if status == 429 or (status == 403 and "rateLimit" in reason):
        raise ProviderError("rate_limited", retryable=True, retry_after=retry_after(response))
    if status >= 500:
        raise ProviderError("provider_unavailable", retryable=True, retry_after=retry_after(response))
    if status == 404:
        raise ProviderError("calendar_not_found", retryable=False)
    if status == 403:
        raise ProviderError("access_denied", retryable=False)
    raise ProviderError(f"provider_error_{status}", retryable=False)
