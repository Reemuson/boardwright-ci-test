# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Minimal, lossless-enough S-expression reader for KiCad source files.

The reader is used only for *read-only* analysis (fingerprints and parity
checks). It never writes KiCad files, so it cannot corrupt authored sources.
Quoted strings keep their quoting so that ``"1"`` and ``1`` stay distinct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Union

from .errors import BoardwrightError

Atom = str
Node = Union[Atom, "SList"]


@dataclass
class SList:
    items: list[Node]

    @property
    def head(self) -> str:
        if self.items and isinstance(self.items[0], str):
            return self.items[0]
        return ""

    def children(self, head: str) -> Iterator["SList"]:
        for item in self.items:
            if isinstance(item, SList) and item.head == head:
                yield item

    def child(self, head: str) -> "SList | None":
        return next(self.children(head), None)

    def value(self, index: int = 1) -> str:
        if len(self.items) > index and isinstance(self.items[index], str):
            return unquote(self.items[index])
        return ""


def unquote(atom: str) -> str:
    if len(atom) >= 2 and atom[0] == '"' and atom[-1] == '"':
        body = atom[1:-1]
        out: list[str] = []
        index = 0
        while index < len(body):
            char = body[index]
            if char == "\\" and index + 1 < len(body):
                nxt = body[index + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)
    return atom


def parse(text: str) -> SList:
    """Parse ``text`` and return its single top-level list."""
    stack: list[SList] = []
    root: SList | None = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
        elif char == "(":
            node = SList([])
            if stack:
                stack[-1].items.append(node)
            elif root is not None:
                raise BoardwrightError("S-expression has more than one top-level list.")
            else:
                root = node
            stack.append(node)
            index += 1
        elif char == ")":
            if not stack:
                raise BoardwrightError(f"Unbalanced ')' at offset {index}.")
            stack.pop()
            index += 1
        elif char == '"':
            end = index + 1
            while end < length:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == '"':
                    break
                end += 1
            if end >= length:
                raise BoardwrightError(f"Unterminated string at offset {index}.")
            _append_atom(stack, text[index : end + 1], index)
            index = end + 1
        else:
            end = index
            while end < length and text[end] not in ' \t\r\n()"':
                end += 1
            _append_atom(stack, text[index:end], index)
            index = end
    if stack:
        raise BoardwrightError("Unbalanced '(' in S-expression.")
    if root is None:
        raise BoardwrightError("No S-expression found.")
    return root


def _append_atom(stack: list[SList], atom: str, offset: int) -> None:
    if not stack:
        raise BoardwrightError(f"Atom outside list at offset {offset}.")
    stack[-1].items.append(atom)


def canonical(node: Node) -> str:
    """Whitespace-independent serialisation used for hashing."""
    if isinstance(node, str):
        return node
    return "(" + " ".join(canonical(item) for item in node.items) + ")"
