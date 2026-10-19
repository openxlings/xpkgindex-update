"""Read literal Lua metadata without executing Lua; retain exact source ranges.

Expressions are opaque. Only the fields the updater consumes must be literal.
Comments, escaped quotes, and long strings cannot change bracket matching.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field


class Invalid(ValueError):
    """A declaration cannot safely be updated."""


@dataclass
class Token:
    value: str
    start: int
    end: int
    kind: str = "symbol"


def tokens(text: str) -> list[Token]:
    out = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        comment = text.startswith("--", i)
        start = i
        if comment:
            i += 2
        long = re.match(r"\[(=*)\[", text[i:])
        if long:
            close = "]" + long[1] + "]"
            end = text.find(close, i + len(long[0]))
            if end < 0:
                raise Invalid("unterminated Lua long string/comment")
            value = text[i + len(long[0]):end]
            i = end + len(close)
            if not comment:
                out.append(Token(value.removeprefix("\n"), start, i, "string"))
            continue
        if comment:
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if text[i] in "\"'":
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise Invalid("unterminated Lua string")
            raw = text[start:i]
            try:
                value = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                value = raw[1:-1]  # opaque build strings aren't consumed as metadata
            out.append(Token(value, start, i, "string"))
            continue
        word = re.match(r"[A-Za-z_][A-Za-z_0-9]*|[0-9]+(?:\.[0-9]+)?", text[i:])
        if word:
            i += len(word[0])
            out.append(Token(word[0], start, i, "word"))
        else:
            i += 1
            out.append(Token(text[start:i], start, i))
    return out


@dataclass
class Node:
    start: int
    end: int
    value: object = None
    fields: dict[str, Node] = field(default_factory=dict)
    kind: str = "literal"
    reference: Node | None = None
    bindings: dict[str, Node] = field(default_factory=dict)

    def definition(self) -> Node:
        return self.reference.definition() if self.reference else self

    def table(self) -> dict[str, Node]:
        if self.kind != "table":
            raise Invalid("expected a literal Lua table")
        return self.fields

    def string(self) -> str:
        if self.kind != "string":
            raise Invalid("expected a literal string")
        return str(self.value)

    def plain(self):
        if self.kind == "table":
            return {k: v.plain() for k, v in self.fields.items()}
        if self.kind == "expr":
            raise Invalid("dynamic metadata is not supported")
        return self.value


class Parser:
    def __init__(self, text: str):
        self.ts = tokens(text)
        self.i = 0
        self.bindings: dict[str, Node] = {}
        self.declarations: set[int] = set()
        self.reads: set[int] = set()
        self.used: set[str] = set()

    def expression(self) -> Node:
        start = self.i
        depth = []
        pairs = {"{": "}", "[": "]", "(": ")"}
        while self.i < len(self.ts):
            t = self.ts[self.i]
            if t.kind != "string":
                if not depth and t.value in (",", ";", "}"):
                    break
                if t.value in pairs:
                    depth.append(pairs[t.value])
                elif t.value in pairs.values():
                    if not depth or depth.pop() != t.value:
                        raise Invalid("unbalanced Lua expression")
            self.i += 1
        if self.i == start or depth:
            raise Invalid("invalid Lua expression")
        chunk = self.ts[start:self.i]
        target = None
        if chunk[0].kind == "word" and chunk[0].value in self.bindings:
            if len(chunk) == 1:
                target = self.bindings[chunk[0].value]
            elif (len(chunk) == 4 and chunk[1].value == "["
                  and chunk[2].kind == "string" and chunk[3].value == "]"):
                target = self.bindings[chunk[0].value].table().get(chunk[2].value)
                if target is None:
                    raise Invalid("missing shared table entry")
            if target is not None:
                self.used.add(chunk[0].value)
                self.reads.add(chunk[0].start)
                return Node(chunk[0].start, chunk[-1].end, target.value,
                            fields=target.fields, kind=target.kind, reference=target)
        if len(chunk) == 1:
            t = chunk[0]
            if t.kind == "string":
                return Node(t.start, t.end, t.value, kind="string")
            if t.value in ("true", "false", "nil"):
                return Node(t.start, t.end, {"true": True, "false": False, "nil": None}[t.value])
        if chunk[0].value == "{" and chunk[0].kind != "string":
            end = self.i
            self.i = start
            node = self.table()
            if self.i == end:
                return node
            self.i = end
        return Node(chunk[0].start, chunk[-1].end, kind="expr")

    def table(self) -> Node:
        opening = self.ts[self.i]
        self.i += 1
        fields = {}
        array = 0
        while self.i < len(self.ts):
            t = self.ts[self.i]
            if t.value == "}" and t.kind != "string":
                self.i += 1
                return Node(opening.start, t.end, fields=fields, kind="table")
            if t.value == "[" and t.kind != "string":
                key = self.ts[self.i + 1]
                if key.kind != "string" or [x.value for x in self.ts[self.i + 2:self.i + 4]] != ["]", "="]:
                    raise Invalid("nonliteral table key")
                name = key.value
                self.i += 4
            elif self.i + 1 < len(self.ts) and self.ts[self.i + 1].value == "=":
                name = t.value
                self.i += 2
            else:
                name = f"#{array}"
                array += 1
            value = self.expression()
            if name in fields:
                raise Invalid(f"duplicate Lua key: {name}")
            fields[name] = value
            if self.i < len(self.ts) and self.ts[self.i].value in (",", ";"):
                self.i += 1
        raise Invalid("unterminated table")

    def package(self) -> Node:
        for i in range(len(self.ts) - 2):
            if (self.ts[i].kind != "word" or
                    [t.value for t in self.ts[i:i + 3]] != ["package", "=", "{"]):
                continue
            # Only a prefix of literal local table declarations can supply
            # shared metadata. Nothing is executed, imported or evaluated.
            self.i = 0
            while self.i < i:
                if self.ts[self.i].value == ";":
                    self.i += 1
                    continue
                prefix = self.ts[self.i:self.i + 4]
                if (len(prefix) != 4 or prefix[0].value != "local" or
                        prefix[1].kind != "word" or prefix[2].value != "=" or prefix[3].value != "{"):
                    self.bindings.clear()
                    break
                name = prefix[1]
                if name.value in self.bindings:
                    raise Invalid("ambiguous shared table declaration")
                self.declarations.add(name.start)
                self.i += 3
                self.bindings[name.value] = self.table()
            self.i = i + 2
            node = self.table()
            for token in self.ts:
                if (token.kind == "word" and token.value in self.used and
                        token.start not in self.declarations | self.reads):
                    raise Invalid("shared tables must not be mutated, rebound or used outside literal references")
            if self.used and any(token.kind == "word" and token.value == "package" and
                                 token.start >= node.end for token in self.ts):
                raise Invalid("shared metadata cannot have later package access or mutation")
            node.bindings = self.bindings
            return node
        raise Invalid("missing package literal")


def parse(text: str) -> Node:
    return Parser(text).package()
