"""Centralised failure classification for the download strategy ladder.

Before this module, deciding "what went wrong and what do we try next" was done
by scattered substring checks in at least four places (``downloader`` had its own
``_is_unsupported_url_error`` plus inline ``'HTTP Error 429' in text`` tests,
``auto_pipeline`` had ``is_retryable_media_error``/``_should_fallback_to_sniff``).
They disagreed in subtle ways and each new failure mode had to be taught to all
of them.

Everything now funnels through :func:`classify`, and the ladder in
``core.strategy`` reads only :class:`FailureKind` - never raw strings.

Ordering matters a great deal here. Real yt-dlp errors mention several things at
once: a SABR failure is reported alongside "HTTP Error 403", and the PO token
message contains the word "token". Rules are therefore evaluated most-specific
first, and :data:`_RULES` order is part of the contract the tests pin down.
"""

from __future__ import annotations

import unicodedata
from enum import Enum
from typing import Callable

__all__ = [
    "FailureKind",
    "Strategy",
    "STRATEGY_ORDER",
    "classify",
    "fold_ascii",
    "is_fatal",
    "next_strategy",
    "normalize_message",
    "should_change_cookie_source",
    "should_fallback_to_sniff",
    "should_retry_same_source",
]


class FailureKind(Enum):
    """Why a download attempt failed, in terms the ladder can act on."""

    DRM_PROTECTED = "drm_protected"
    PO_TOKEN_REQUIRED = "po_token_required"
    SABR_ONLY = "sabr_only"
    COOKIE_DB_LOCKED = "cookie_db_locked"
    UNSUPPORTED_URL = "unsupported_url"
    BLOB_ONLY = "blob_only"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"
    TOKEN_EXPIRED = "token_expired"
    NOT_FOUND = "not_found"
    NETWORK = "network"
    UNKNOWN = "unknown"


class Strategy(str, Enum):
    """Rungs of the escalation ladder, in the order they are attempted."""

    DIRECT = "direct"
    PLUGIN = "plugin"
    COOKIE = "cookie"
    SNIFF = "sniff"
    MSE = "mse"
    RECORD = "record"


STRATEGY_ORDER: tuple[Strategy, ...] = (
    Strategy.DIRECT,
    Strategy.PLUGIN,
    Strategy.COOKIE,
    Strategy.SNIFF,
    Strategy.MSE,
    Strategy.RECORD,
)

# Failures where climbing the ladder cannot help. DRM is here for legal reasons,
# not technical ones: the tool does not circumvent content protection, so every
# further rung would be a pointless request against a server that is correctly
# refusing us.
FATAL_KINDS: frozenset[FailureKind] = frozenset({FailureKind.DRM_PROTECTED})

# Where to resume for each failure kind. A rung already in ``tried`` is skipped,
# so a repeated failure keeps moving up instead of looping.
_PREFERRED_NEXT: dict[FailureKind, tuple[Strategy, ...]] = {
    FailureKind.PO_TOKEN_REQUIRED: (Strategy.PLUGIN,),
    FailureKind.SABR_ONLY: (Strategy.PLUGIN,),
    FailureKind.COOKIE_DB_LOCKED: (Strategy.COOKIE,),
    FailureKind.AUTH_REQUIRED: (Strategy.COOKIE, Strategy.SNIFF),
    FailureKind.TOKEN_EXPIRED: (Strategy.SNIFF,),
    FailureKind.UNSUPPORTED_URL: (Strategy.SNIFF, Strategy.MSE),
    FailureKind.BLOB_ONLY: (Strategy.MSE, Strategy.RECORD),
    FailureKind.NOT_FOUND: (Strategy.SNIFF,),
    FailureKind.RATE_LIMITED: (Strategy.DIRECT,),
    FailureKind.NETWORK: (Strategy.DIRECT,),
    FailureKind.UNKNOWN: (Strategy.SNIFF, Strategy.MSE, Strategy.RECORD),
}


def fold_ascii(value: str) -> str:
    """Strip Vietnamese diacritics so accented and unaccented text both match.

    Status messages in this codebase are inconsistently accented (console
    encoding issues on Windows led to both forms being emitted), so matching has
    to be insensitive to it.

    ``đ``/``Đ`` are handled before NFKD on purpose: they are distinct letters
    rather than ``d`` plus a combining mark, so NFKD leaves them untouched and
    the subsequent ASCII encode would *delete* them - turning "được" into "uc"
    instead of "duoc" and silently breaking every match on an accented string.
    """
    text = str(value or "").replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def normalize_message(error: BaseException | str) -> str:
    """Flatten an error to a single lowercase line with yt-dlp's prefix removed."""
    text = str(error or "").replace("\r", " ").replace("\n", " ").strip()
    while text.lower().startswith("error:"):
        text = text[6:].strip()
    return " ".join(text.split()).lower()


def _contains_any(*tokens: str) -> Callable[[str, str], bool]:
    def matcher(text: str, folded: str) -> bool:
        return any(token in text or token in folded for token in tokens)

    return matcher


def _drm_matcher(text: str, folded: str) -> bool:
    # "drm" alone is too eager - it appears in unrelated CDN hostnames - so it
    # only counts alongside a real protection-system name or an explicit refusal.
    strong = ("widevine", "playready", "fairplay", "clearkey")
    if any(token in text for token in strong):
        return True
    return "drm" in text and any(
        token in text or token in folded
        for token in ("protected", "encrypted", "license", "khong bypass", "bao ve")
    )


# Most specific first. See the module docstring for why this order is load-bearing.
_RULES: tuple[tuple[FailureKind, Callable[[str, str], bool]], ...] = (
    (FailureKind.DRM_PROTECTED, _drm_matcher),
    (FailureKind.PO_TOKEN_REQUIRED, _contains_any("po token", "po_token", "proof of origin", "proof-of-origin")),
    (FailureKind.SABR_ONLY, _contains_any("sabr streaming", "sabr-only", "only has sabr", "forcing sabr")),
    (FailureKind.COOKIE_DB_LOCKED, _contains_any(
        "could not copy chrome cookie database",
        "could not copy cookie database",
        "cookie database is locked",
    )),
    (FailureKind.UNSUPPORTED_URL, _contains_any(
        "unsupported url",
        "no suitable extractor",
        "khong duoc yt-dlp ho tro",
    )),
    (FailureKind.BLOB_ONLY, _contains_any("blob media", "blob url", "chi lo blob")),
    # Explicit HTTP status codes outrank the keyword heuristics below. yt-dlp
    # embeds the failing URL in its message, and signed URLs carry "token=" /
    # "expires=" query params, so a 404 on a signed link would otherwise be
    # misread as an expired token and sent down the wrong branch of the ladder.
    (FailureKind.RATE_LIMITED, _contains_any("http error 429", "too many requests", "throttle.htm")),
    (FailureKind.AUTH_REQUIRED, _contains_any(
        "http error 401",
        "http error 403",
        "unauthorized",
        "forbidden",
        "login required",
        "private video",
        "members-only",
    )),
    (FailureKind.NOT_FOUND, _contains_any("http error 404", "not found")),
    # Deliberately not matching a bare "token": it appears in almost every
    # signed media URL. Only wording that indicates staleness counts.
    (FailureKind.TOKEN_EXPIRED, _contains_any(
        "expired",
        "signature",
        "token expired",
        "invalid token",
        "bad token",
        "token mismatch",
    )),
    (FailureKind.NETWORK, _contains_any(
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "temporary failure in name resolution",
        "getaddrinfo failed",
        "unable to download webpage",
    )),
)


def classify(error: BaseException | str) -> FailureKind:
    """Map an error to the single kind that best describes it."""
    text = normalize_message(error)
    if not text:
        return FailureKind.UNKNOWN
    folded = fold_ascii(text)
    for kind, matcher in _RULES:
        if matcher(text, folded):
            return kind
    return FailureKind.UNKNOWN


def is_fatal(kind: FailureKind) -> bool:
    """True when no further rung of the ladder is worth attempting."""
    return kind in FATAL_KINDS


def next_strategy(kind: FailureKind, tried: set[str] | frozenset[str] | None = None) -> Strategy | None:
    """Pick the next rung to attempt, or ``None`` to stop.

    ``tried`` holds the :class:`Strategy` values already attempted. Preferred
    rungs for the failure kind come first; if all are exhausted we fall through
    to the next untried rung in ladder order so an unusual error still escalates
    rather than dead-ending.
    """
    if is_fatal(kind):
        return None

    attempted = {str(item) for item in (tried or set())}
    for strategy in _PREFERRED_NEXT.get(kind, ()):
        if strategy.value not in attempted:
            return strategy
    for strategy in STRATEGY_ORDER:
        if strategy.value not in attempted:
            return strategy
    return None


# --------------------------------------------------------------------------
# Compatibility predicates
#
# These keep the call sites in ``downloader`` and ``auto_pipeline`` readable and
# preserve the public names that ``web_ui.server`` and the tests already import.
# --------------------------------------------------------------------------

def should_fallback_to_sniff(error: BaseException | str) -> bool:
    """Whether a direct yt-dlp failure is worth retrying via the browser sniffer."""
    kind = classify(error)
    if is_fatal(kind):
        return False
    return kind in {
        FailureKind.UNSUPPORTED_URL,
        FailureKind.AUTH_REQUIRED,
        FailureKind.TOKEN_EXPIRED,
        FailureKind.BLOB_ONLY,
        FailureKind.NOT_FOUND,
    }


def should_retry_same_source(error: BaseException | str) -> bool:
    """Transient failures where backing off and retrying the same URL is right."""
    return classify(error) in {FailureKind.RATE_LIMITED, FailureKind.NETWORK}


def should_change_cookie_source(error: BaseException | str) -> bool:
    """Failures that point at the credentials rather than the URL."""
    return classify(error) in {FailureKind.AUTH_REQUIRED, FailureKind.COOKIE_DB_LOCKED}
