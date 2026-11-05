"""
Generate leakradar/types.py from the OpenAPI spec of the LeakRadar API.

    python scripts/generate_types.py
    python scripts/generate_types.py --spec path/to/openapi.json
    python scripts/generate_types.py --spec https://api.leakradar.io/openapi.json --check

Every schema of components.schemas becomes a type: a TypedDict (total=False) for an object,
a Literal alias for an enum, an alias for the rest (Dict[str, Any] for an object without
properties, List[...], Optional[...]). The file depends on the spec alone: types sorted by
name, fields in the order of the spec, no timestamp. --check writes nothing and exits with 1
when the file is not the one the spec gives.

TypeGenerator.type_expression gives the Python type of any schema object of the spec, and
TypeGenerator.response_type the type of an operation's answer: the client's annotations are
built with them.

Names:
- a component keeps its name minus what is not a letter or a digit
  (PaginatedResponse_ExportBase_ -> PaginatedResponseExportBase);
- an object declared inline is named after where it sits: Parent + Field (the `daily` object
  of RateLimitsResponse -> RateLimitsResponseDaily), + "Item" for the items of an array,
  + "Value" for the values of a map, + "Variant1", "Variant2"... for the objects of a union;
- a name already taken gets a number (FooBar2), the components whose name needs no change
  first; a name of the typing module, True, False or None gets "Schema" (List -> ListSchema).
"""

import argparse
import difflib
import json
import pathlib
import re
import sys
import textwrap
import unicodedata
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DEFAULT_SPEC = "https://api.leakradar.io/openapi.json"
OUTPUT = pathlib.Path(__file__).resolve().parent.parent / "leakradar" / "types.py"
WIDTH = 100  # docstrings and comments wrap at this column

TYPING_NAMES = ("Any", "Dict", "List", "Literal", "Optional", "TypedDict", "Union")
RESERVED = frozenset(TYPING_NAMES + ("False", "None", "True"))
# The keywords of every supported Python version: keyword.kwlist depends on the one running.
KEYWORDS = frozenset(
    (
        "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
        "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
        "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
        "try", "while", "with", "yield",
    )
)
# The keys of a schema that say something about its type (the others only document it).
TYPE_KEYS = frozenset(
    ("$ref", "type", "properties", "items", "enum", "const", "anyOf", "oneOf", "allOf", "additionalProperties")
)
EM_DASH = chr(0x2014)
# Characters a string literal of the output always escapes (invisible, or banned from the texts).
ESCAPED = frozenset((0x2014, 0x2028, 0x2029, 0xFEFF))

# A type is a tuple: ("name", "str"), ("ref", "ExportBase"), ("literal", (("str", "csv"), ...)),
# ("list", item type), ("dict", value type) or ("union", (member, ...)).
Type = Tuple[Any, ...]
ANY: Type = ("name", "Any")
NONE: Type = ("name", "None")
STR: Type = ("name", "str")
BYTES: Type = ("name", "bytes")
INT: Type = ("name", "int")
FLOAT: Type = ("name", "float")
BOOL: Type = ("name", "bool")
DICT_ANY: Type = ("dict", ANY)


def fetch(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "leakradar-wrapper-types", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def load_spec(source: str) -> Dict[str, Any]:
    """The spec at an http(s) URL or in a local JSON file."""
    if re.match(r"(?i)https?://", source):
        return fetch(source)
    with open(source, "rb") as handle:
        return json.loads(handle.read().decode("utf-8-sig"))


def _pascal(text: str) -> str:
    """daily_reset_at -> DailyResetAt, PaginatedResponse_ExportBase_ -> PaginatedResponseExportBase."""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return "".join(part[0].upper() + part[1:] for part in re.findall(r"[A-Za-z0-9]+", ascii_text))


def _class_name(raw: str) -> str:
    name = _pascal(raw)
    return name if name and not name[0].isdigit() else "Schema" + name


def _plain_key(key: str) -> bool:
    """A key the class syntax of TypedDict can declare as is."""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)) and key not in KEYWORDS and not key.startswith("__")


def _unescape(token: str) -> str:
    """A JSON pointer token of a $ref (RFC 6901, inside a URI fragment)."""
    return urllib.parse.unquote(token).replace("~1", "/").replace("~0", "~")


def _py_str(value: str) -> str:
    """A double-quoted Python string literal, the same on every Python version."""
    out = ['"']
    for char in value:
        code = ord(char)
        if char in '\\"':
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif code < 0x20 or 0x7F <= code < 0xA0 or 0xD800 <= code < 0xE000 or code in ESCAPED:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _clean(text: Any) -> str:
    """A description made fit for a docstring: no control character, no em dash, no trailing space."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = re.sub(f"(?m)^( *){EM_DASH} *", r"\1- ", text)
    text = re.sub(f" *{EM_DASH} *", ", ", text)
    text = "".join(
        char
        for char in text
        if char == "\n" or not (ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0 or 0xD800 <= ord(char) < 0xE000)
    )
    text = text.replace(chr(0x2028), "\n").replace(chr(0x2029), "\n").replace(chr(0xFEFF), "")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def _wrap(text: str, width: int) -> List[str]:
    """Wrap the long lines of a text, leaving tables, code and list layout alone."""
    out: List[str] = []
    fenced = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            fenced = not fenced
        if fenced or stripped.startswith(("```", "|")) or line.startswith("    ") or len(line) <= width:
            out.append(line)
            continue
        lead = line[: len(line) - len(stripped)]
        marker = re.match(r"(?:[-*+]|\d+[.)]) +", stripped)
        hang = lead + " " * (len(marker.group(0)) if marker else 0)
        wrapped = textwrap.wrap(
            line, width=width, subsequent_indent=hang, break_long_words=False, break_on_hyphens=False
        )
        out.extend(wrapped or [line])
    return out


def _escape_doc(text: str) -> str:
    text = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return text[:-1] + '\\"' if text.endswith('"') else text


def _docstring(text: str, indent: str) -> str:
    body = _escape_doc("\n".join(_wrap(text, WIDTH - len(indent))))
    if "\n" not in body and len(indent) + len(body) + 6 <= WIDTH:
        return indent + '"""' + body + '"""'
    lines = [indent + '"""']
    lines.extend(indent + line if line else "" for line in body.split("\n"))
    lines.append(indent + '"""')
    return "\n".join(lines)


def _comment(text: str, indent: str) -> List[str]:
    return [indent + ("# " + line if line else "#") for line in _wrap(text, WIDTH - len(indent) - 2)]


def _literal(values: Sequence[Any]) -> Optional[Type]:
    """Literal[...] of enum or const values, None when one of them cannot be a Literal (a float)."""
    members: List[Tuple[str, Any]] = []
    nullable = False
    for value in values:
        if value is None:
            nullable = True
            continue
        if isinstance(value, bool):
            member: Tuple[str, Any] = ("bool", value)
        elif isinstance(value, int):
            member = ("int", value)
        elif isinstance(value, str):
            member = ("str", value)
        else:
            return None
        if member not in members:
            members.append(member)
    if not members:
        return NONE if nullable else None
    literal: Type = ("literal", tuple(members))
    return _union([literal, NONE]) if nullable else literal


def _union(types: Iterable[Type]) -> Type:
    """The union of types: flattened, without duplicates, its literals merged into one."""
    flat: List[Type] = []
    for kind in types:
        flat.extend(kind[1] if kind[0] == "union" else [kind])
    if ANY in flat:
        return ANY
    out: List[Optional[Type]] = []
    literal_values: List[Tuple[str, Any]] = []
    literal_at: Optional[int] = None
    for kind in flat:
        if kind[0] == "literal":
            if literal_at is None:
                literal_at = len(out)
                out.append(None)
            literal_values.extend(value for value in kind[1] if value not in literal_values)
        elif kind not in out:
            out.append(kind)
    if literal_at is not None:
        out[literal_at] = ("literal", tuple(literal_values))
    members = [kind for kind in out if kind is not None]
    if not members:
        return ANY
    return members[0] if len(members) == 1 else ("union", tuple(members))


def _literal_source(member: Tuple[str, Any]) -> str:
    kind, value = member
    if kind == "str":
        return _py_str(value)
    if kind == "bool":
        return "True" if value else "False"
    return str(value)


def _render(kind: Type, quote: bool, prefix: str, refs: Set[str], imports: Set[str]) -> str:
    """The source of a type. refs collects the generated names it uses, imports the typing ones."""
    tag = kind[0]
    if tag == "name":
        if kind[1] in TYPING_NAMES:
            imports.add(kind[1])
        return kind[1]
    if tag == "ref":
        refs.add(kind[1])
        name = prefix + kind[1]
        return f'"{name}"' if quote else name
    if tag == "literal":
        imports.add("Literal")
        return "Literal[" + ", ".join(_literal_source(member) for member in kind[1]) + "]"
    if tag == "list":
        imports.add("List")
        return "List[" + _render(kind[1], quote, prefix, refs, imports) + "]"
    if tag == "dict":
        imports.add("Dict")
        return "Dict[str, " + _render(kind[1], quote, prefix, refs, imports) + "]"
    members = [member for member in kind[1] if member != NONE]
    if not members:
        return "None"
    if len(members) == 1:
        inner = _render(members[0], quote, prefix, refs, imports)
    else:
        imports.add("Union")
        inner = "Union[" + ", ".join(_render(member, quote, prefix, refs, imports) for member in members) + "]"
    if len(members) == len(kind[1]):
        return inner
    imports.add("Optional")
    return "Optional[" + inner + "]"


def _identifiers(kind: Type) -> Set[str]:
    """Every name the source of a type uses: generated types, typing names and builtins."""
    tag = kind[0]
    if tag in ("name", "ref"):
        return {kind[1]}
    if tag == "literal":
        return {"Literal"}
    if tag == "list":
        return {"List"} | _identifiers(kind[1])
    if tag == "dict":
        return {"Dict", "str"} | _identifiers(kind[1])
    names = {"Optional", "Union"}
    for member in kind[1]:
        names |= _identifiers(member)
    return names


class TypeGenerator:
    """
    Python types of the schemas of an OpenAPI spec.

    - names: component schema name -> Python name.
    - type_expression(schema): the Python type of a schema object of the spec, as source code.
    - response_type(method, path): the Python type of an operation's answer.
    - render(): the source of leakradar/types.py.
    """

    def __init__(self, spec: Dict[str, Any]) -> None:
        self.spec = spec
        schemas = (spec.get("components") or {}).get("schemas") or {}
        self.schemas: Dict[str, Any] = dict(schemas) if isinstance(schemas, dict) else {}
        self.names: Dict[str, str] = {}
        self._taken: Set[str] = set()
        self._components: Dict[int, str] = {}  # id() of a component schema -> its Python name
        self._inline: Dict[int, str] = {}  # id() of an inline object schema -> its Python name
        self._inline_objects: Dict[str, Tuple[Dict[str, Any], str]] = {}  # name -> (schema, where it sits)

        exact = sorted(raw for raw in self.schemas if _class_name(raw) == raw)
        others = sorted(raw for raw in self.schemas if _class_name(raw) != raw)
        for raw in exact + others:
            self.names[raw] = self._claim(_class_name(raw))
            if isinstance(self.schemas[raw], dict):
                self._components[id(self.schemas[raw])] = self.names[raw]
        for raw in sorted(self.schemas, key=lambda raw: self.names[raw]):
            name, schema = self.names[raw], self.schemas[raw]
            if self._is_object(schema):
                self._walk_properties(schema, name)
            else:
                self._walk(schema, name, name, free=False)

    # Public helpers

    def type_expression(
        self,
        schema: Any,
        *,
        quote: bool = False,
        prefix: str = "",
        refs: Optional[Set[str]] = None,
        imports: Optional[Set[str]] = None,
    ) -> str:
        """
        The Python type of a schema object of this spec, as source code: "TaskStatusOut",
        "List[LeakDetails]", "Optional[str]", "Dict[str, Any]", "bytes" for a binary string.

        - schema: a {"$ref": ...}, a component, a property, the schema of a response body...
          An inline object is found by identity: pass the dict of this spec, not a copy.
          An object that is not in the types (inline in an operation) gives Dict[str, Any].
        - quote: write the generated names as strings (forward references, as types.py does).
        - prefix: put before each generated name, for instance "types." for a module that
          imports leakradar.types rather than the names.
        - refs: a set that receives the generated names the expression uses (to import them);
          imports: a set that receives the typing names it uses (Any, Dict, List, Literal...).
        """
        refs = set() if refs is None else refs
        imports = set() if imports is None else imports
        return _render(self._type(schema), quote, prefix, refs, imports)

    def response_schema(self, method: str, path: str) -> Optional[Dict[str, Any]]:
        """
        The schema of the answer of an operation: the JSON body of its first 2xx status with a
        body, {"type": "string", "format": "binary"} for a file, None without any (a redirect).
        KeyError when the operation is not in the spec.
        """
        operation = ((self.spec.get("paths") or {}).get(path) or {}).get(method.lower())
        if not isinstance(operation, dict):
            raise KeyError(f"{method.upper()} {path} is not in the spec")
        responses = operation.get("responses") or {}
        for status in sorted(str(code) for code in responses):
            response = responses.get(status) or {}
            if "$ref" in response:  # #/components/responses/...
                response = self._resolve(response["$ref"]) or {}
            content = response.get("content") or {}
            if not status.startswith("2") or not content:
                continue
            for media_type in sorted(content):
                if media_type == "application/json" or media_type.endswith("+json"):
                    schema = (content[media_type] or {}).get("schema")
                    return schema if isinstance(schema, dict) else {}
            return {"type": "string", "format": "binary"}
        return None

    def response_type(
        self,
        method: str,
        path: str,
        *,
        quote: bool = False,
        prefix: str = "",
        refs: Optional[Set[str]] = None,
        imports: Optional[Set[str]] = None,
    ) -> str:
        """type_expression of response_schema(method, path): "bytes" for a file, "Any" without a body."""
        schema = self.response_schema(method, path)
        if schema is None:
            if imports is not None:
                imports.add("Any")
            return "Any"
        return self.type_expression(schema, quote=quote, prefix=prefix, refs=refs, imports=imports)

    def counts(self) -> Tuple[int, int]:
        """(TypedDicts, aliases) that render() writes."""
        objects = sum(1 for schema in self.schemas.values() if self._is_object(schema))
        return objects + len(self._inline_objects), len(self.schemas) - objects

    def render(self) -> str:
        """The source of leakradar/types.py."""
        imports: Set[str] = set()
        blocks: List[Tuple[str, str]] = []
        aliases: Dict[str, str] = {}  # Name = Other: needs Other defined above it
        alias_docs: Dict[str, str] = {}
        for raw, schema in self.schemas.items():
            name = self.names[raw]
            doc = _clean(schema.get("description")) if isinstance(schema, dict) else ""
            if name != raw:
                doc = (doc + "\n\n" if doc else "") + f"Named `{raw}` in the API spec."
            if self._is_object(schema):
                blocks.append((name, self._typed_dict(name, schema, doc, imports)))
                continue
            kind = self._type_of(schema, 0)
            if kind[0] == "ref":
                aliases[name] = kind[1]
                alias_docs[name] = doc
                continue
            # `X = None` would be a variable, not an alias.
            blocks.append((name, self._alias(name, ANY if kind == NONE else kind, doc, imports)))
        for name, (schema, where) in self._inline_objects.items():
            doc = _clean(schema.get("description")) or where[0].upper() + where[1:] + "."
            blocks.append((name, self._typed_dict(name, schema, doc, imports)))
        blocks.sort()
        blocks.extend(
            (name, self._alias(name, ANY if target is None else ("name", target), alias_docs[name], imports))
            for name, target in self._ordered_aliases(aliases)
        )

        names = sorted(name for name, _ in blocks)
        lines = [self._header(), ""]
        used = sorted(name for name in imports if name in TYPING_NAMES)
        if used:
            lines.extend(["from typing import " + ", ".join(used), ""])
        lines.append("__all__ = [")
        lines.extend("    " + _py_str(name) + "," for name in names)
        lines.append("]")
        return "\n".join(lines) + "".join("\n\n\n" + text for _, text in blocks) + "\n"

    # Naming

    def _claim(self, candidate: str) -> str:
        if candidate in RESERVED:
            candidate += "Schema"
        name, number = candidate, 2
        while name in self._taken:
            name = f"{candidate}{number}"
            number += 1
        self._taken.add(name)
        return name

    def _walk_properties(self, node: Dict[str, Any], name: str) -> None:
        # Only the properties declared here: those of a $ref'd schema are named with their owner.
        for key, sub in self._properties(node, follow_refs=False).items():
            self._walk(sub, name + _pascal(key), f"the `{key}` key of {name}")

    def _walk(self, node: Any, context: str, where: str, free: bool = True) -> None:
        """Name the inline objects of a schema: an object found right here is named `context`,
        unless `free` is False (context is then the name of the schema itself)."""
        if not isinstance(node, dict) or "$ref" in node:
            return
        if self._is_object(node):
            if id(node) not in self._inline and id(node) not in self._components:
                name = self._claim(context)
                self._inline[id(node)] = name
                self._inline_objects[name] = (node, where)
                self._walk_properties(node, name)
            return
        for part in self._parts(node):
            self._walk(part, context, where, free)
        variants = self._variants(node)
        objects = [id(variant) for variant in variants if self._is_object(variant)]
        number = 0
        for variant in variants:
            if id(variant) in objects and (len(objects) > 1 or not free):
                number += 1
                suffix = f"Variant{number}" if len(objects) > 1 else "Variant"
                self._walk(variant, context + suffix, where)
            else:
                self._walk(variant, context, where, free)
        if isinstance(node.get("items"), dict):
            self._walk(node["items"], context + "Item", "an item of " + where)
        if isinstance(node.get("additionalProperties"), dict):
            self._walk(node["additionalProperties"], context + "Value", "a value of " + where)

    def _ordered_aliases(self, aliases: Dict[str, str]) -> List[Tuple[str, Optional[str]]]:
        """The `Name = Other` aliases, each after the alias it points to; None in a cycle."""
        order: List[Tuple[str, Optional[str]]] = []
        state: Dict[str, str] = {}

        def visit(name: str) -> bool:
            if state.get(name) == "done":
                return True
            if state.get(name) == "active":
                return False
            state[name] = "active"
            target = aliases[name]
            sound = visit(target) if target in aliases else True
            state[name] = "done"
            order.append((name, target if sound else None))
            return sound

        for name in sorted(aliases):
            visit(name)
        return order

    # Schemas

    def _resolve(self, ref: Any) -> Any:
        """The node a local $ref points to, None when it points elsewhere or nowhere."""
        if not isinstance(ref, str) or not ref.startswith("#"):
            return None
        node: Any = self.spec
        for token in ref[1:].split("/")[1:]:
            token = _unescape(token)
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                return None
        return node

    def _parts(self, node: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The allOf parts that say something about the type (not only a description)."""
        parts = node.get("allOf")
        if not isinstance(parts, list):
            return []
        return [part for part in parts if isinstance(part, dict) and TYPE_KEYS.intersection(part)]

    def _variants(self, node: Dict[str, Any]) -> List[Any]:
        for key in ("anyOf", "oneOf"):
            if isinstance(node.get(key), list) and node[key]:
                return list(node[key])
        return []

    def _properties(self, node: Any, follow_refs: bool = True, seen: Optional[Set[int]] = None) -> Dict[str, Any]:
        """The properties of an object schema, those of its allOf parts included."""
        seen = set() if seen is None else seen
        if not isinstance(node, dict) or id(node) in seen:
            return {}
        seen.add(id(node))
        if "$ref" in node:
            return self._properties(self._resolve(node["$ref"]), follow_refs, seen) if follow_refs else {}
        merged: Dict[str, Any] = {}
        for part in self._parts(node):
            merged.update(self._properties(part, follow_refs, seen))
        if isinstance(node.get("properties"), dict):
            merged.update(node["properties"])
        return merged

    def _is_object(self, node: Any) -> bool:
        """An object with known keys: it becomes a TypedDict."""
        if not isinstance(node, dict) or "$ref" in node:
            return False
        if isinstance(node.get("properties"), dict) and node["properties"]:
            return True
        parts = self._parts(node)
        if len(parts) == 1:
            return self._is_object(parts[0])
        return len(parts) > 1 and bool(self._properties(node))

    # Types

    def _type(self, node: Any, depth: int = 0) -> Type:
        if isinstance(node, dict):
            if id(node) in self._components:
                return ("ref", self._components[id(node)])
            if id(node) in self._inline:
                return self._nullable(node, ("ref", self._inline[id(node)]))
        return self._type_of(node, depth)

    def _type_of(self, node: Any, depth: int) -> Type:
        """The type a schema describes, without looking up its own name."""
        if depth > 64 or not isinstance(node, dict):
            return ANY
        if "$ref" in node:
            return self._ref_type(node["$ref"], depth)
        if "const" in node:
            literal = _literal([node["const"]])
            if literal is not None:
                return self._nullable(node, literal)
        if isinstance(node.get("enum"), list) and node["enum"]:
            literal = _literal(node["enum"])
            if literal is not None:
                return self._nullable(node, literal)
        if self._is_object(node):  # outside the components: no TypedDict for it
            return self._nullable(node, DICT_ANY)
        parts = self._parts(node)
        if parts:
            known = [kind for kind in (self._type(part, depth + 1) for part in parts) if kind != ANY]
            return self._nullable(node, known[0] if known else ANY)
        variants = self._variants(node)
        if variants:
            return self._nullable(node, _union(self._type(variant, depth + 1) for variant in variants))
        kind = node.get("type")
        if isinstance(kind, list):
            return self._nullable(node, _union(self._simple(node, item, depth) for item in kind))
        return self._nullable(node, self._simple(node, kind, depth))

    def _simple(self, node: Dict[str, Any], kind: Any, depth: int) -> Type:
        if kind == "string":
            return BYTES if node.get("format") == "binary" else STR
        if kind == "integer":
            return INT
        if kind == "number":
            return FLOAT
        if kind == "boolean":
            return BOOL
        if kind == "null":
            return NONE
        if kind == "array" or (kind is None and "items" in node):
            items = node.get("items")
            return ("list", self._type(items, depth + 1) if isinstance(items, dict) else ANY)
        if kind == "object" or (kind is None and "additionalProperties" in node):
            extra = node.get("additionalProperties")
            return ("dict", self._type(extra, depth + 1) if isinstance(extra, dict) else ANY)
        return ANY

    def _ref_type(self, ref: Any, depth: int) -> Type:
        prefix = "#/components/schemas/"
        if isinstance(ref, str) and ref.startswith(prefix) and "/" not in ref[len(prefix):]:
            raw = _unescape(ref[len(prefix):])
            if raw in self.names:
                return ("ref", self.names[raw])
        target = self._resolve(ref)
        return self._type(target, depth + 1) if target is not None else ANY

    def _nullable(self, node: Dict[str, Any], kind: Type) -> Type:
        """Add None for `nullable: true` (OpenAPI 3.0) or a "null" in a list of types (3.1)."""
        types = node.get("type")
        if node.get("nullable") is True or (isinstance(types, list) and "null" in types):
            return _union([kind, NONE])
        return kind

    # Source

    def _field_doc(self, node: Any) -> str:
        if not isinstance(node, dict):
            return ""
        doc = _clean(node.get("description"))
        if node.get("deprecated") is True:
            doc = (doc + "\n\n" if doc else "") + "Deprecated."
        return doc

    def _typed_dict(self, name: str, node: Dict[str, Any], doc: str, imports: Set[str]) -> str:
        imports.add("TypedDict")
        fields: List[Tuple[str, str, str]] = []
        used: Set[str] = set()
        for key, sub in self._properties(node).items():
            kind = self._type(sub)
            used |= _identifiers(kind)
            fields.append((key, _render(kind, True, "", set(), imports), self._field_doc(sub)))

        # The class syntax declares identifiers only, and a key named like a type the
        # annotations use would shadow it for type checkers: the call syntax has neither limit.
        if all(_plain_key(key) and key not in used for key, _, _ in fields):
            lines = [f"class {name}(TypedDict, total=False):"]
            if doc:
                lines.extend([_docstring(doc, "    "), ""])
            for key, source, field_doc in fields:
                lines.append(f"    {key}: {source}")
                if field_doc:
                    lines.append(_docstring(field_doc, "    "))
            return "\n".join(lines)

        lines = [f"{name} = TypedDict(", "    " + _py_str(name) + ",", "    {"]
        for key, source, field_doc in fields:
            if field_doc:
                lines.extend(_comment(field_doc, "        "))
            lines.append("        " + _py_str(key) + ": " + source + ",")
        lines.extend(["    },", "    total=False,", ")"])
        if doc:
            lines.append(_docstring(doc, ""))
        return "\n".join(lines)

    def _alias(self, name: str, kind: Type, doc: str, imports: Set[str]) -> str:
        lines = [name + " = " + _render(kind, True, "", set(), imports)]
        if doc:
            lines.append(_docstring(doc, ""))
        return "\n".join(lines)

    def _header(self) -> str:
        info = self.spec.get("info") or {}
        title = " ".join(_clean(info.get("title")).split()) or "API"
        version = " ".join(_clean(str(info.get("version") or "")).split()) or "unknown"
        paragraphs = (
            "Types of the LeakRadar API: one TypedDict per object schema of its OpenAPI spec.",
            f"Generated by scripts/generate_types.py from the OpenAPI spec of the {title} ({DEFAULT_SPEC}), "
            f"API version {version}. Do not edit by hand: regenerate with `python scripts/generate_types.py`.",
            "Every TypedDict is total=False: the API may leave a key out, and a key documented as nullable may "
            "hold None. Enums are Literal aliases. Nothing changes at runtime: the client returns plain dicts, "
            "these types describe them to type checkers and editors.",
        )
        return _docstring("\n\n".join(paragraphs), "")


def generate(spec: Dict[str, Any]) -> str:
    """The source of leakradar/types.py for a spec."""
    return TypeGenerator(spec).render()


def _emit(text: str) -> None:
    """Print, even what the console encoding cannot show."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(text.encode(encoding, "replace").decode(encoding) + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", default=DEFAULT_SPEC, help="URL or path of the OpenAPI spec (default: %(default)s)")
    parser.add_argument("--out", default=str(OUTPUT), help="file to write (default: leakradar/types.py)")
    parser.add_argument("--check", action="store_true", help="write nothing, exit with 1 when the file is out of date")
    args = parser.parse_args(argv)

    generator = TypeGenerator(load_spec(args.spec))
    source = generator.render()
    typed_dicts, aliases = generator.counts()
    out = pathlib.Path(args.out)
    if args.check:
        # A Windows checkout may have turned the line endings into CRLF.
        current = out.read_bytes().decode("utf-8").replace("\r\n", "\n") if out.exists() else ""
        if current == source:
            _emit(f"{out} is up to date ({typed_dicts} TypedDicts, {aliases} aliases).")
            return 0
        diff = list(difflib.unified_diff(current.splitlines(), source.splitlines(), str(out), args.spec, lineterm=""))
        for line in diff[:200]:
            _emit(line)
        if len(diff) > 200:
            _emit(f"... {len(diff) - 200} more lines")
        _emit(f"{out} is out of date: run python scripts/generate_types.py --spec {args.spec}")
        return 1
    out.write_bytes(source.encode("utf-8"))
    _emit(f"{typed_dicts} TypedDicts and {aliases} aliases written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
