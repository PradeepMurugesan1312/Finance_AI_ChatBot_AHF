"""BTP platform integration: destination resolution, on-premise proxy."""

from ahf_finance_agent.btp.destinations import (
    DestinationError,
    ResolvedDestination,
    resolve_destination,
)

__all__ = ["DestinationError", "ResolvedDestination", "resolve_destination"]
