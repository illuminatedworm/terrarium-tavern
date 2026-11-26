"""Type-sensitive equality for stored JSON values."""

from typing import Any


def same_json(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if type(left) is not type(right):
        return False  # A JSON boolean and a number can compare equal in Python.
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            same_json(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right
