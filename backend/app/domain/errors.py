"""Domain errors. Messages must never contain secrets."""


class DomainError(Exception):
    """Base class for expected, user-facing errors."""


class ConfigMissing(DomainError):
    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(f"Missing settings in .env: {', '.join(names)}")


class CampaignNotFound(DomainError):
    pass


class CampaignInvalid(DomainError):
    pass


class EngagementError(DomainError):
    """One Instagram call failed for one comment (privacy setting, deleted comment, ...). The run continues.
    retryable: Meta's own "temporary error, try again" (codes 1/2) — the person goes back to the queue."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class EngagementBlocked(DomainError):
    """The whole run must stop: bad/expired token, missing permission, rate limit, app access level.
    maybe_sent: False when the request surely didn't create anything (stopped before sending, Meta answered
    with an error, or the connection never opened). True only when the answer was lost (e.g. a timeout)."""

    def __init__(self, message: str, maybe_sent: bool = False):
        super().__init__(message)
        self.maybe_sent = maybe_sent
