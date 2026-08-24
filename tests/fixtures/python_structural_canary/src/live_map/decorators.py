from collections.abc import Callable


def traced(fn: Callable) -> Callable:
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper
