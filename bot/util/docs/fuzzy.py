"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

# help with: http://chairnerd.seatgeek.com/fuzzywuzzy-fuzzy-string-matching-in-python/

from __future__ import annotations

import heapq
import re
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any, TypeVar

T = TypeVar("T")

_word_regex = re.compile(r"\W", re.IGNORECASE)


def ratio(a: str, b: str) -> int:
    m = SequenceMatcher(None, a, b)
    return int(round(100 * m.ratio()))


def quick_ratio(a: str, b: str) -> int:
    m = SequenceMatcher(None, a, b)
    return int(round(100 * m.quick_ratio()))


def partial_ratio(a: str, b: str) -> int:
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    m = SequenceMatcher(None, short, long_)

    blocks = m.get_matching_blocks()

    scores = []
    for i, j, _n in blocks:
        start = max(j - i, 0)
        end = start + len(short)
        o = SequenceMatcher(None, short, long_[start:end])
        r = o.ratio()

        if 100 * r > 99:
            return 100
        scores.append(r)

    return int(round(100 * max(scores)))


def _sort_tokens(a: str) -> str:
    a = _word_regex.sub(" ", a).lower().strip()
    return " ".join(sorted(a.split()))


def token_sort_ratio(a: str, b: str) -> int:
    return ratio(_sort_tokens(a), _sort_tokens(b))


def quick_token_sort_ratio(a: str, b: str) -> int:
    return quick_ratio(_sort_tokens(a), _sort_tokens(b))


def partial_token_sort_ratio(a: str, b: str) -> int:
    return partial_ratio(_sort_tokens(a), _sort_tokens(b))


def _extraction_generator(
    query: str,
    choices: Mapping[str, T] | Iterable[str],
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
) -> Generator[tuple[str, int, T] | tuple[str, int], None, None]:
    if isinstance(choices, Mapping):
        for key, value in choices.items():
            score = scorer(query, key)
            if score >= score_cutoff:
                yield (key, score, value)
    else:
        for choice in choices:
            score = scorer(query, choice)
            if score >= score_cutoff:
                yield (choice, score)


def extract(
    query: str,
    choices: Mapping[str, T] | Iterable[str],
    *,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
    limit: int | None = 10,
) -> list[tuple[str, int, T] | tuple[str, int]]:
    it = _extraction_generator(query, choices, scorer, score_cutoff)
    key = lambda t: t[1]
    if limit is not None:
        return heapq.nlargest(limit, it, key=key)
    return sorted(it, key=key, reverse=True)


def extract_one(
    query: str,
    choices: Mapping[str, T] | Iterable[str],
    *,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
) -> tuple[str, int, T] | tuple[str, int] | None:
    it = _extraction_generator(query, choices, scorer, score_cutoff)
    key = lambda t: t[1]
    try:
        return max(it, key=key)
    except ValueError:
        # iterator returned nothing
        return None


def extract_or_exact(
    query: str,
    choices: Mapping[str, T] | Iterable[str],
    *,
    limit: int | None = None,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
) -> list[Any]:
    matches = extract(query, choices, scorer=scorer, score_cutoff=score_cutoff, limit=limit)
    if len(matches) == 0:
        return []

    if len(matches) == 1:
        return matches

    top = matches[0][1]
    second = matches[1][1]

    # check if the top one is exact or more than 30% more correct than the second
    if top == 100 or top > (second + 30):
        return [matches[0]]

    return matches


def extract_matches(
    query: str,
    choices: Mapping[str, T] | Iterable[str],
    *,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
) -> list[Any]:
    matches = extract(query, choices, scorer=scorer, score_cutoff=score_cutoff, limit=None)
    if len(matches) == 0:
        return []

    top_score = matches[0][1]
    to_return = []
    for match in matches:
        if match[1] != top_score:
            break
        to_return.append(match)
    return to_return


def finder(
    text: str,
    collection: Iterable[T],
    *,
    key: Callable[[T], str] | None = None,
    lazy: bool = True,
) -> Generator[T, None, None] | list[T]:
    """Fuzzy-find items in ``collection`` whose searchable text contains the
    characters of ``text`` in order (subsequence match), ranking the
    tightest / earliest matches first.
    """
    suggestions: list[tuple[int, int, T]] = []
    text = str(text)
    pat = ".*?".join(map(re.escape, text))
    regex = re.compile(pat, flags=re.IGNORECASE)
    for item in collection:
        to_search = key(item) if key else item
        r = regex.search(to_search)  # type: ignore[arg-type]
        if r:
            suggestions.append((len(r.group()), r.start(), item))

    def sort_key(tup: tuple[int, int, T]) -> tuple[int, int, str] | tuple[int, int]:
        if key:
            return tup[0], tup[1], key(tup[2])
        return tup[0], tup[1]

    ordered = sorted(suggestions, key=sort_key)
    if lazy:
        return (z for _, _, z in ordered)
    return [z for _, _, z in ordered]


def find(text: str, collection: Iterable[T], *, key: Callable[[T], str] | None = None) -> T | None:
    try:
        return finder(text, collection, key=key, lazy=False)[0]  # type: ignore[index]
    except IndexError:
        return None