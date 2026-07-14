"""Typing-oriented re-exports of the client base classes and the
package logging subsystem.

Annotate code against these interfaces instead of a concrete backend,
so any transport can be substituted::

    from contree_client.types import ContreeSyncClient


    def biggest_image(client: ContreeSyncClient) -> str:
        ...

Every implementation logs into a child of :data:`logger`. The base
logger level is explicitly set to ``logging.ERROR``, so the library
stays silent unless the user opts in::

    from contree_client.types import set_log_level

    set_log_level(logging.DEBUG)
"""

from .base import ContreeAsyncClient, ContreeClientBase, ContreeSyncClient
from .runtime import logger


def set_log_level(level: int | str) -> None:
    """Set the level of the package logger and all its children."""
    logger.setLevel(level)


__all__ = [
    "ContreeAsyncClient",
    "ContreeClientBase",
    "ContreeSyncClient",
    "logger",
    "set_log_level",
]
