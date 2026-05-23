from typing import Optional, Union


class Server:
    def start(self) -> str:
        return "listening"


class Engine:
    def start(self) -> str:
        return "vroom"


def with_optional(e: Optional[Engine]) -> None:
    e.start()  # should resolve to Engine.start, not Server.start


def with_pipe_none(e: Engine | None) -> None:
    e.start()  # should resolve to Engine.start


def with_union_none(e: Union[Engine, None]) -> None:
    e.start()  # should resolve to Engine.start


def with_union_two(thing: Union[Engine, Server]) -> None:
    thing.start()  # should resolve to Engine.start (first match)
