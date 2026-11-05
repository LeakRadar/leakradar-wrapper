"""
Python 3.8 compatibility of the shipped code, its tests, scripts and examples (requires-python >=3.8).

Every file must parse as Python 3.8 (`ast.parse(feature_version=(3, 8))`) and must not use, where
Python evaluates it at runtime, what later versions added: builtin generics (`list[int]`), `X | Y`
unions, dict union, `str.removeprefix`, `functools.cache`, `zoneinfo`, `math.lcm`, `anext`...
Annotations are evaluated at runtime unless the module has `from __future__ import annotations`.
In leakradar/ they always count, string annotations included: integrations read them with
`typing.get_type_hints()`, which evaluates them on the user's Python.

Newer syntax that `feature_version` lets through (parenthesized context managers, PEP 701
f-strings, relaxed decorators) is reported by ruff (target-version py38) and by the Python 3.8
CI job, which also catches what no static check sees (behaviour changes between versions).
"""

import ast
import pathlib
import unittest
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ("leakradar/**/*.py", "scripts/**/*.py", "tests/**/*.py", "examples/**/*.py")

Version = Tuple[int, int]

# Subscripting these where Python evaluates it needs 3.9 (PEP 585): builtin -> typing alias.
BUILTIN_GENERICS = {
    "list": "List", "dict": "Dict", "tuple": "Tuple", "set": "Set", "frozenset": "FrozenSet", "type": "Type",
}
GENERIC_CLASSES = {  # and every collections.abc class
    "asyncio.Future", "asyncio.Queue", "asyncio.Task", "collections.ChainMap", "collections.Counter",
    "collections.OrderedDict", "collections.defaultdict", "collections.deque", "contextlib.AbstractContextManager",
    "contextlib.AbstractAsyncContextManager", "os.PathLike", "queue.Queue", "queue.SimpleQueue", "re.Match",
    "re.Pattern",
}
BUILTIN_TYPES = set(BUILTIN_GENERICS) | {"bool", "bytearray", "bytes", "complex", "float", "int", "object", "str"}

NEW_MODULES: Dict[str, Version] = {"graphlib": (3, 9), "zoneinfo": (3, 9), "tomllib": (3, 11)}
NEW_NAMES: Dict[str, Version] = {
    "argparse.BooleanOptionalAction": (3, 9), "ast.unparse": (3, 9), "asyncio.to_thread": (3, 9),
    "functools.cache": (3, 9), "importlib.resources.files": (3, 9), "math.lcm": (3, 9),
    "random.randbytes": (3, 9), "typing.Annotated": (3, 9),
    "contextlib.aclosing": (3, 10), "inspect.get_annotations": (3, 10), "itertools.pairwise": (3, 10),
    "types.NoneType": (3, 10), "types.UnionType": (3, 10), "typing.Concatenate": (3, 10),
    "typing.ParamSpec": (3, 10), "typing.TypeAlias": (3, 10), "typing.TypeGuard": (3, 10),
    "typing.is_typeddict": (3, 10),
    "asyncio.Runner": (3, 11), "asyncio.TaskGroup": (3, 11), "asyncio.timeout": (3, 11),
    "asyncio.timeout_at": (3, 11), "datetime.UTC": (3, 11), "enum.StrEnum": (3, 11),
    "typing.LiteralString": (3, 11), "typing.Never": (3, 11), "typing.NotRequired": (3, 11),
    "typing.Required": (3, 11), "typing.Self": (3, 11), "typing.Unpack": (3, 11), "typing.assert_never": (3, 11),
    "typing.assert_type": (3, 11), "typing.reveal_type": (3, 11),
    "itertools.batched": (3, 12), "typing.override": (3, 12),
    "typing.ReadOnly": (3, 13), "typing.TypeIs": (3, 13),
}
NEW_METHODS: Dict[str, Version] = {
    "removeprefix": (3, 9), "removesuffix": (3, 9), "is_relative_to": (3, 9), "with_stem": (3, 9),
    "assertNoLogs": (3, 10), "bit_count": (3, 10),
    "enterAsyncContext": (3, 11), "enterClassContext": (3, 11), "enterContext": (3, 11),
}
NEW_BUILTINS: Dict[str, Version] = {
    "aiter": (3, 10), "anext": (3, 10), "EncodingWarning": (3, 10),
    "BaseExceptionGroup": (3, 11), "ExceptionGroup": (3, 11),
}
NEW_KEYWORDS: Dict[Tuple[str, str], Version] = {  # (function or method name, keyword argument)
    ("get_type_hints", "include_extras"): (3, 9),
    ("TemporaryDirectory", "ignore_cleanup_errors"): (3, 10), ("dataclass", "kw_only"): (3, 10),
    ("dataclass", "slots"): (3, 10), ("write_text", "newline"): (3, 10), ("zip", "strict"): (3, 10),
    ("read_text", "newline"): (3, 13),
}
# A `try` whose handlers catch one of these guards the imports and attributes it probes.
PROBE_ERRORS = {"AttributeError", "BaseException", "Exception", "ImportError", "ModuleNotFoundError", "NameError"}


def _version(version: Version) -> str:
    return f"Python {version[0]}.{version[1]}"


def _imported_names(tree: ast.AST) -> Dict[str, str]:
    """Local name -> dotted origin, for every import of the module."""
    names: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                names[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                names[alias.asname or alias.name] = f"{module}.{alias.name}"
    return names


def _bound_names(body: Iterable[ast.AST], args: Optional[ast.arguments] = None) -> Set[str]:
    """Names a scope binds (arguments, assignments, definitions), without entering nested scopes."""
    names: Set[str] = set()
    if args is not None:
        for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
            if arg is not None:
                names.add(arg.arg)
    pending = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        pending.extend(ast.iter_child_nodes(node))
    return names


def _caught(handlers: List[ast.ExceptHandler]) -> Set[str]:
    """Exception class names the handlers of a `try` catch (a bare except catches BaseException)."""
    names: Set[str] = set()
    for handler in handlers:
        if handler.type is None:
            names.add("BaseException")
            continue
        for node in ast.walk(handler.type):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


class Checker(ast.NodeVisitor):
    """Collects (line, message) for what a Python 3.8 interpreter would fail on."""

    def __init__(self, tree: ast.Module, package: bool):
        self.aliases = _imported_names(tree)
        lazy = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        self.package = package
        self.annotations_evaluated = package or not lazy
        self.scopes: List[Tuple[str, Set[str]]] = [("module", _bound_names(tree.body))]
        self.annotation_depth = 0
        self.probing = 0  # inside the body of a `try` that catches a missing import or attribute
        self.isinstance_unions: Set[int] = set()
        self.findings: Set[Tuple[int, str]] = set()

    def report(self, node: ast.AST, message: str) -> None:
        self.findings.add((getattr(node, "lineno", 0), message))

    # Name resolution

    def is_builtin(self, name: str) -> bool:
        if name in self.aliases:
            return False
        for index, (kind, names) in enumerate(self.scopes):
            # A class body's names are not visible from its methods.
            if name in names and (kind != "class" or index == len(self.scopes) - 1):
                return False
        return True

    def dotted(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def generic_class(self, node: ast.AST) -> Optional[str]:
        """The name of a class whose subscript needs Python 3.9, else None."""
        if isinstance(node, ast.Name) and node.id in BUILTIN_GENERICS and self.is_builtin(node.id):
            return node.id
        path = self.dotted(node)
        if path and (path in GENERIC_CLASSES or path.startswith("collections.abc.")):
            return path
        return None

    def is_type(self, node: ast.AST) -> bool:
        """An operand that makes `a | b` a type union rather than a bitwise or set operation."""
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if isinstance(node, ast.Name) and node.id in BUILTIN_TYPES and self.is_builtin(node.id):
            return True
        if isinstance(node, ast.Subscript):
            return self.generic_class(node.value) is not None or (self.dotted(node.value) or "").startswith("typing.")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.is_type(node.left) or self.is_type(node.right)
        return (self.dotted(node) or "").startswith("typing.")

    def is_dict(self, node: ast.AST) -> bool:
        if isinstance(node, (ast.Dict, ast.DictComp)):
            return True
        func = node.func if isinstance(node, ast.Call) else None
        return isinstance(func, ast.Name) and func.id == "dict" and self.is_builtin("dict")

    # Scopes and annotations

    def check_annotation(self, node: Optional[ast.AST]) -> None:
        if node is None or not self.annotations_evaluated:
            return
        self.annotation_depth += 1
        self.visit(node)
        self.annotation_depth -= 1

    def visit_scope(self, kind: str, body: List[ast.stmt], args: Optional[ast.arguments] = None) -> None:
        self.scopes.append((kind, _bound_names(body, args)))
        for statement in body:
            self.visit(statement)
        self.scopes.pop()

    def visit_function(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        args = node.args
        for expression in node.decorator_list + args.defaults + args.kw_defaults:
            if expression is not None:
                self.visit(expression)
        for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
            if arg is not None:
                self.check_annotation(arg.annotation)
        self.check_annotation(node.returns)
        self.visit_scope("function", node.body, args)

    visit_FunctionDef = visit_function
    visit_AsyncFunctionDef = visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in node.args.defaults + node.args.kw_defaults:
            if expression is not None:
                self.visit(expression)
        self.scopes.append(("function", _bound_names([], node.args)))
        self.visit(node.body)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in node.decorator_list + node.bases + [keyword.value for keyword in node.keywords]:
            self.visit(expression)
        bases = {(self.dotted(b.value if isinstance(b, ast.Subscript) else b) or "").split(".")[-1] for b in node.bases}
        if "Generic" in bases and bases & {"NamedTuple", "TypedDict"}:
            self.report(node, f"a generic TypedDict or NamedTuple needs {_version((3, 11))}")
        self.visit_scope("class", node.body)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)
        if self.scopes[-1][0] != "function":  # local variable annotations are never evaluated
            self.check_annotation(node.annotation)

    # Guards: TYPE_CHECKING blocks, version gates, try/except probes

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if (self.dotted(test) or "").split(".")[-1] == "TYPE_CHECKING":
            for statement in node.orelse:
                self.visit(statement)
            return
        self.visit(test)
        newer_body = newer_orelse = False
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            left = test.left.value if isinstance(test.left, ast.Subscript) else test.left
            if self.dotted(left) == "sys.version_info":
                newer_body = isinstance(test.ops[0], (ast.Gt, ast.GtE))
                newer_orelse = isinstance(test.ops[0], (ast.Lt, ast.LtE))
        elif isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.func.id == "hasattr":
            # hasattr(module, "name") probes the running Python
            newer_body = bool(test.args) and isinstance(test.args[0], ast.Name) and test.args[0].id in self.aliases
        for statement in [] if newer_body else node.body:
            self.visit(statement)
        for statement in [] if newer_orelse else node.orelse:
            self.visit(statement)

    def visit_Try(self, node: ast.Try) -> None:
        probe = 1 if _caught(node.handlers) & PROBE_ERRORS else 0
        self.probing += probe
        for statement in node.body:
            self.visit(statement)
        self.probing -= probe
        for child in node.handlers + node.orelse + node.finalbody:
            self.visit(child)

    # Constructs

    def visit_Subscript(self, node: ast.Subscript) -> None:
        name = self.generic_class(node.value)
        if name:
            alias = "typing." + BUILTIN_GENERICS[name] if name in BUILTIN_GENERICS else "its typing alias"
            self.report(node, f"`{name}[...]` is evaluated at runtime and needs {_version((3, 9))}: use {alias}")
        if self.annotation_depth and (self.dotted(node.value) or "").split(".")[-1] == "Literal":
            self.visit(node.value)  # Literal["a | b"] holds values, not annotations
            return
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.BitOr) and id(node) not in self.isinstance_unions:
            if self.annotation_depth or self.is_type(node.left) or self.is_type(node.right):
                self.report(node, f"`X | Y` is evaluated at runtime and needs {_version((3, 10))}: use Union or Optional")
            elif self.is_dict(node.left) or self.is_dict(node.right):
                self.report(node, f"dict union `a | b` needs {_version((3, 9))}: use {{**a, **b}}")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.op, ast.BitOr) and self.is_dict(node.value):
            self.report(node, f"dict union `a |= b` needs {_version((3, 9))}: use a.update(b)")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in ("isinstance", "issubclass") and len(node.args) == 2:
            union = node.args[1]
            if isinstance(union, ast.BinOp) and isinstance(union.op, ast.BitOr):
                self.isinstance_unions.add(id(union))
                self.report(node, f"{name}() with `X | Y` needs {_version((3, 10))}: pass a tuple")
        for keyword in node.keywords:
            version = NEW_KEYWORDS.get((name, keyword.arg or ""))
            if version and (name != "zip" or self.is_builtin("zip")):
                self.report(node, f"`{name}({keyword.arg}=...)` needs {_version(version)}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not self.probing:
            path = self.dotted(node) or ""
            if path in NEW_NAMES:
                self.report(node, f"`{path}` needs {_version(NEW_NAMES[path])}")
            elif node.attr in NEW_METHODS:
                self.report(node, f"`.{node.attr}()` needs {_version(NEW_METHODS[node.attr])}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        version = NEW_BUILTINS.get(node.id)
        if version and isinstance(node.ctx, ast.Load) and not self.probing and self.is_builtin(node.id):
            self.report(node, f"`{node.id}` needs {_version(version)}")

    def visit_Constant(self, node: ast.Constant) -> None:
        # In the package, typing.get_type_hints() evaluates string annotations too.
        if not (self.annotation_depth and self.package and isinstance(node.value, str)):
            return
        try:
            annotation = ast.parse(node.value.strip(), mode="eval")
        except SyntaxError:
            return
        ast.increment_lineno(annotation, node.lineno - 1)
        self.visit(annotation.body)

    def check_module(self, node: ast.AST, module: str) -> None:
        for name, version in NEW_MODULES.items():
            if module == name or module.startswith(name + "."):
                self.report(node, f"module `{name}` needs {_version(version)}")

    def visit_Import(self, node: ast.Import) -> None:
        if not self.probing:
            for alias in node.names:
                self.check_module(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.probing or node.level or not node.module:
            return
        self.check_module(node, node.module)
        for alias in node.names:
            path = f"{node.module}.{alias.name}"
            if path in NEW_NAMES:
                self.report(node, f"`from {node.module} import {alias.name}` needs {_version(NEW_NAMES[path])}")


def check(source: bytes, filename: str = "<snippet>", package: bool = False) -> List[str]:
    """What keeps `source` from running on Python 3.8, as "file:line: message" lines."""
    try:
        tree = ast.parse(source, filename, feature_version=(3, 8))
    except SyntaxError as error:
        return [f"{filename}:{error.lineno}: does not parse as Python 3.8: {error.msg}"]
    checker = Checker(tree, package)
    checker.visit(tree)
    return [f"{filename}:{line}: {message}" for line, message in sorted(checker.findings)]


class PythonCompatTest(unittest.TestCase):
    def test_sources_run_on_python_3_8(self):
        paths = sorted({path for pattern in SOURCES for path in ROOT.glob(pattern)})
        names = [path.relative_to(ROOT).as_posix() for path in paths]
        self.assertIn("leakradar/client.py", names)
        for path, name in zip(paths, names):
            with self.subTest(path=name):
                findings = check(path.read_bytes(), name, package=name.startswith("leakradar/"))
                if findings:
                    self.fail("needs a Python newer than 3.8:\n" + "\n".join(findings))


class CheckerTest(unittest.TestCase):
    """The checker itself: what it must report, and Python 3.8 code it must leave alone."""

    REPORTED = {
        "builtin generic in a signature": "def f(x: list[int]) -> None: pass",
        "builtin generic at runtime": "Alias = dict[str, int]",
        "class-level annotation": "class C:\n    x: tuple[int, ...] = ()",
        "collections.abc generic": "from collections.abc import Mapping\nAlias = Mapping[str, int]",
        "union in a signature": "def f(x: int | None): pass",
        "union of classes in a signature": "class A: pass\nclass B: pass\ndef f(x: A | B): pass",
        "union at runtime": "Alias = str | None",
        "union in isinstance": "isinstance(1, int | str)",
        "dict union": "a = {} | {'a': 1}",
        "dict union assignment": "a = {}\na |= {'a': 1}",
        "str.removeprefix": "'ab'.removeprefix('a')",
        "functools.cache": "import functools\n@functools.cache\ndef f(): pass",
        "functools.cache import": "from functools import cache",
        "zoneinfo": "import zoneinfo",
        "math.lcm": "import math\nmath.lcm(2, 3)",
        "anext": "async def f(it):\n    return await anext(it)",
        "asyncio.to_thread": "import asyncio\nasync def f():\n    await asyncio.to_thread(print)",
        "typing.NotRequired": "from typing import NotRequired",
        "zip strict": "zip([1], [2], strict=True)",
        "write_text newline": "import pathlib\npathlib.Path('a').write_text('', newline='')",
        "generic TypedDict": "from typing import Generic, TypedDict, TypeVar\nT = TypeVar('T')\n"
        "class P(TypedDict, Generic[T]):\n    items: T",
        "match statement": "match x:\n    case 1:\n        pass",
    }
    REPORTED_IN_PACKAGE = {
        "string annotation": "def f(x: 'list[int]'): pass",
        "annotation under the future import": "from __future__ import annotations\ndef f(x: int | None): pass",
    }
    ACCEPTED = {
        "typing generics": "from typing import Dict, List, Optional, Type\n"
        "def f(x: List[int], t: Type[int]) -> Optional[Dict[str, int]]: pass",
        "local variable annotation": "def f():\n    x: list[int] = []\n    return x",
        "future import outside the package": "from __future__ import annotations\n"
        "def f(x: list[int]) -> int | None: pass",
        "TYPE_CHECKING block": "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from typing import Self\n    Alias = list[int]",
        "version gate": "import sys\nif sys.version_info >= (3, 9):\n    from functools import cache\nelse:\n"
        "    cache = None",
        "guarded import": "try:\n    import zoneinfo\nexcept ImportError:\n    zoneinfo = None",
        "hasattr probe": "import asyncio\nif hasattr(asyncio, 'to_thread'):\n    run = asyncio.to_thread",
        "set, int and flag unions": "import re\na = {1} | {2}\nb = 1 | 2\nc = re.I | re.M",
        "shadowed builtin": "def f(type):\n    return type[0]",
        "subscripted value": "items = [1]\nfirst = items[0]",
    }
    ACCEPTED_IN_PACKAGE = {
        "Literal values": "from typing import Literal\ndef f(x: Literal['a | b', 'list[int]']): pass",
        "forward reference": "from typing import List\ndef f(x: 'List[C]'): pass\nclass C: pass",
    }

    def test_reports_newer_constructs(self):
        cases = [(name, source, False) for name, source in self.REPORTED.items()]
        cases += [(name, source, True) for name, source in self.REPORTED_IN_PACKAGE.items()]
        for name, source, package in cases:
            with self.subTest(name):
                self.assertTrue(check(source.encode(), package=package), source)

    def test_accepts_python_38_code(self):
        cases = [(name, source, False) for name, source in self.ACCEPTED.items()]
        cases += [(name, source, True) for name, source in self.ACCEPTED_IN_PACKAGE.items()]
        for name, source, package in cases:
            with self.subTest(name):
                self.assertEqual(check(source.encode(), package=package), [])


if __name__ == "__main__":
    unittest.main()
