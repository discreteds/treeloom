from typing import Union, Optional, Dict, List


def with_union(x: Union[int, str]) -> None:
    pass


def with_optional(y: Optional[str]) -> None:
    pass


def with_pipe(z: int | str) -> None:
    pass


def with_pipe_none(w: str | None) -> None:
    pass


def with_generic_dict(d: Dict[str, int]) -> None:
    pass


def with_generic_list(items: List[str]) -> None:
    pass


def with_nested_generic(data: Dict[str, List[int]]) -> None:
    pass
