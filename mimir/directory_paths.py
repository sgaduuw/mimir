"""Shared directory URL spelling for routes, navigation and sitemaps."""

from string import ascii_lowercase

DIRECTORY_LETTERS = (*ascii_lowercase, "other")


def directory_letter(name: str) -> str:
    """ASCII initial, or the stable bucket for numbers and other characters."""
    first = name.strip()[:1].lower()
    return first if first in ascii_lowercase and first else "other"


def subsystem_directory_path(letter: str | None = None) -> str:
    return "/subsystems/" + (f"{letter}/" if letter is not None else "")


def maintainer_directory_path(letter: str | None = None) -> str:
    return "/maintainers/" + (f"{letter}/" if letter is not None else "")
