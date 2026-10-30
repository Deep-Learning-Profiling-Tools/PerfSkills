#!/usr/bin/env python3
"""Extract source-search symbols from ordinary and templated GPU names."""

from __future__ import annotations

import re


LIBRARY_MARKERS = ("cub::", "cuda::", "std::", "CUDA memcpy", "CUDA memset")
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")
_CALL = re.compile(r"(?<![\w:])([A-Za-z_]\w*)\s*\(")
_NON_SYMBOLS = {"lambda", "instance", "operator"}


def _head(name: str) -> str | None:
    if any(marker in name for marker in LIBRARY_MARKERS):
        return None
    head = name.split("(", 1)[0].strip()
    head = re.sub(
        r"^(?:void|int|float|double|long|unsigned|static|__global__|__device__)\s+",
        "",
        head,
    )
    token = re.split(r"\s+", head)[-1].rsplit("::", 1)[-1]
    token = re.sub(r"<.*$", "", token)
    return token if _IDENTIFIER.match(token) else None


def _template_body(name: str) -> str:
    start = name.find("<")
    if start < 0:
        return ""
    depth = 0
    for index in range(start, len(name)):
        char = name[index]
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth == 0:
                return name[start + 1:index]
    return name[start + 1:]


def symbol_candidates(name: str) -> list[str]:
    """Return inner functors before their generic launch wrapper.

    Nsight often reports ``wrapper<functor(args...)>(...)``.  Searching the
    functor first keeps source attribution on the implementation rather than
    stopping at a generic launch helper.  The parser is lexical and remains
    independent of any benchmark or algorithm vocabulary.
    """
    outer = _head(name)
    if outer is None:
        return []
    nested = [item for item in _CALL.findall(_template_body(name))
              if item not in _NON_SYMBOLS and _IDENTIFIER.match(item)]
    result = []
    for item in nested + [outer]:
        if item not in result:
            result.append(item)
    return result
