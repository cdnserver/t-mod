"""Shared music runtime exceptions without importing the Discord runtime."""


class MusicRuntimeError(RuntimeError):
    pass


__all__ = ["MusicRuntimeError"]
