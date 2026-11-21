"""Configuration errors and error-path formatting. Pure Python."""

from collections.abc import Iterable


class ConfigError(Exception):
    """Invalid or unresolvable Stage_Config.

    The message holds one line per issue, ``<stage>: <dotted.path>: <reason>``.
    """


class UnknownStageError(ConfigError):
    """The requested stage has no Stage_Config."""

    def __init__(self, stage: str, known: Iterable[str]) -> None:
        self.stage = stage
        self.known = tuple(sorted(known))
        super().__init__(f'Unknown stage "{stage}". Known stages: {", ".join(self.known)}')


def format_loc(loc: tuple[str | int, ...]) -> str:
    """Render a pydantic error location as a dotted path.

    ``("queues", 2, "name")`` gives ``queues[2].name``.
    """
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif path:
            path += f".{part}"
        else:
            path = part
    return path
