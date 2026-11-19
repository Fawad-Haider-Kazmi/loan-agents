"""Exception hierarchy.

The split between *transient* and *permanent* errors drives failure handling:
transient errors are retried automatically with back-off; permanent errors
fail the case immediately and alert a human operator.
"""


class LoanSystemError(Exception):
    """Base class for every error raised by this system."""


class TransientError(LoanSystemError):
    """A temporary problem (timeout, 503...). Safe to retry."""


class PermanentError(LoanSystemError):
    """A problem that retrying will not fix."""


class CreditBureauUnavailable(TransientError):
    pass


class BankingUnavailable(TransientError):
    pass


class DocumentParseError(PermanentError):
    pass


class AuthorizationError(PermanentError):
    """Raised when something tries to move money without valid authorization."""


class IllegalTransition(LoanSystemError):
    """Raised when the workflow state machine is asked to make an invalid move."""
