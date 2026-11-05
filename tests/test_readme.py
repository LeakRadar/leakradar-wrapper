"""
README.md and examples/: the method reference is complete and the code they show is valid.

- The "Method reference" section of README.md has one table row per public method of
  LeakRadarClient (LeakRadar has the same methods, with close() for aclose()), naming the
  operations of the method's docstring "API: <operationId>, <docs URL>" lines, with the same links.
  When a method or an API line changes, the failure prints the row README.md needs.
- Every python code block of README.md and every examples/*.py parses as Python 3.8.
- They import only public names of leakradar, leakradar.types and leakradar.webhooks, and every
  call on `client` (or of a client class) names a method that exists, with arguments its
  signature accepts.
- README.md, CHANGELOG.md and the examples never use the em dash or the en dash (house style),
  and hold ASCII characters only.
"""

import ast
import inspect
import pathlib
import re
import unittest
from typing import Dict, Iterator, List, Optional, Tuple

import leakradar
import leakradar.types
import leakradar.webhooks
from leakradar import AsyncLeakRadar, LeakRadar, LeakRadarClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
EXAMPLES = sorted((ROOT / "examples").glob("*.py"))

API_LINE = re.compile(r"^API: (?P<operation_id>\S+), (?P<url>\S+)$")
ROW = re.compile(r"^\| (?P<method>.+?) \| (?P<operation>.+) \|$")
METHOD_NAME = re.compile(r"`([a-z_][a-z0-9_]*)\(\)`")
LINK = re.compile(r"\[`([^`]+)`\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(?P<indent> *)```(?P<lang>[A-Za-z0-9]*)\s*$")
SECTION = "## Method reference"
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# What `from <module> import <name>` may name in README.md and examples/.
PUBLIC_MODULES = {
    "leakradar": leakradar,
    "leakradar.types": leakradar.types,
    "leakradar.webhooks": leakradar.webhooks,
}
CLIENT_CLASSES = {"LeakRadar": LeakRadar, "AsyncLeakRadar": AsyncLeakRadar, "LeakRadarClient": LeakRadarClient}
CLIENT_VARIABLE = "client"
SYNC_NAMES = {"aclose": "close"}
# paginate() keeps these keyword arguments; the others go to the listing method it is given.
PAGINATE_OWN = ("max_items", "items_key")

Source = Tuple[str, str]  # (where, code)


def public_methods(cls: type) -> Dict[str, List[Tuple[str, str]]]:
    """Public method name -> (operationId, docs URL) of each API line of its docstring."""
    methods: Dict[str, List[Tuple[str, str]]] = {}
    for name, member in inspect.getmembers(cls):
        if name.startswith("_") or not callable(member):
            continue
        lines = (API_LINE.match(line) for line in (inspect.getdoc(member) or "").splitlines())
        methods[name] = [(match.group("operation_id"), match.group("url")) for match in lines if match]
    return methods


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def method_reference_rows() -> List[Tuple[List[str], List[Tuple[str, str]]]]:
    """(method names, operation links) of each table row of the Method reference section."""
    text = readme_text()
    start = text.index("\n" + SECTION + "\n")
    end = text.find("\n## ", start + 1)
    rows = []
    for line in text[start : end if end != -1 else len(text)].splitlines():
        match = ROW.match(line)
        if match and line.startswith("| `"):
            rows.append((METHOD_NAME.findall(match.group("method")), LINK.findall(match.group("operation"))))
    return rows


def expected_row(name: str, lines: List[Tuple[str, str]]) -> str:
    links = ", ".join(f"[`{operation_id}`]({url})" for operation_id, url in lines)
    return f"| `{name}()` | {links or 'what it calls'} |"


def code_blocks(text: str, where: str) -> Iterator[Tuple[str, str, str]]:
    """(language, where, code) of every fenced code block, list item blocks dedented."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = FENCE.match(lines[index])
        index += 1
        if not match:
            continue
        indent = len(match.group("indent"))
        first = index
        while index < len(lines) and lines[index].strip() != "```":
            index += 1
        body = [line[indent:] if not line[:indent].strip() else line for line in lines[first:index]]
        index += 1
        yield match.group("lang"), f"{where}:{first + 1}", "\n".join(body) + "\n"


def readme_python() -> List[Source]:
    return [(where, code) for lang, where, code in code_blocks(readme_text(), "README.md") if lang == "python"]


def example_sources() -> List[Source]:
    return [(f"examples/{path.name}", path.read_text(encoding="utf-8")) for path in EXAMPLES]


def parse(where: str, code: str) -> ast.Module:
    return ast.parse(code, filename=where, feature_version=(3, 8))


def signature_of(method: str) -> Optional[inspect.Signature]:
    for cls in (LeakRadar, LeakRadarClient):
        member = getattr(cls, method, None)
        if not method.startswith("_") and callable(member):
            return inspect.signature(member)
    return None


def unbindable(signature: inspect.Signature, args: List[ast.expr], keywords: List[ast.keyword], bound: bool) -> str:
    """Why these arguments do not fit the signature ("" when they do, or cannot be told)."""
    if any(isinstance(arg, ast.Starred) for arg in args) or any(keyword.arg is None for keyword in keywords):
        return ""
    values = [None] * (len(args) + (1 if bound else 0))  # bound: the method takes self first
    try:
        signature.bind(*values, **{keyword.arg: None for keyword in keywords if keyword.arg})
    except TypeError as exc:
        return str(exc)
    return ""


class MethodReferenceTest(unittest.TestCase):
    def test_every_method_has_its_row(self):
        rows = method_reference_rows()
        self.assertGreater(len(rows), 100, "the Method reference tables were not found in README.md")
        by_name: Dict[str, List[Tuple[str, str]]] = {}
        for names, links in rows:
            self.assertTrue(names, f"a Method reference row names no method: {links}")
            self.assertNotIn(names[0], by_name, f"two Method reference rows for {names[0]}()")
            by_name[names[0]] = links
        methods = public_methods(LeakRadarClient)
        missing = [expected_row(name, lines) for name, lines in methods.items() if name not in by_name]
        self.assertEqual(missing, [], "README.md, Method reference: add these rows")
        unknown = sorted(set(by_name) - set(methods))
        self.assertEqual(unknown, [], "README.md, Method reference: rows for methods LeakRadarClient does not have")
        for name, lines in methods.items():
            with self.subTest(method=name):
                self.assertEqual(
                    by_name[name],
                    lines,
                    f"README.md, Method reference: the row of {name}() must be {expected_row(name, lines)}",
                )

    def test_sync_names_are_listed(self):
        listed = {name for names, _ in method_reference_rows() for name in names}
        for async_name, sync_name in SYNC_NAMES.items():
            self.assertTrue(hasattr(LeakRadar, sync_name) and hasattr(LeakRadarClient, async_name))
        expected = {SYNC_NAMES.get(name, name) for name in public_methods(LeakRadarClient)}
        self.assertEqual(set(public_methods(LeakRadar)), expected)
        self.assertEqual(sorted(set(public_methods(LeakRadar)) - listed), [])


class CodeTest(unittest.TestCase):
    def sources(self) -> List[Source]:
        readme, examples = readme_python(), example_sources()
        self.assertGreater(len(readme), 10, "no python code block found in README.md")
        self.assertGreaterEqual(len(examples), 5, "examples/ has no scripts")
        return readme + examples

    def test_code_parses_as_python_38(self):
        for where, code in self.sources():
            with self.subTest(where=where):
                parse(where, code)
                # Also what the compiler checks beyond the grammar (top-level await allowed in a snippet).
                compile(code, where, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)

    def test_only_public_names_are_imported(self):
        for where, code in self.sources():
            for node in ast.walk(parse(where, code)):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "leakradar":
                    module = PUBLIC_MODULES.get(node.module)
                    self.assertIsNotNone(module, f"{where}: import from leakradar, not from {node.module}")
                    for alias in node.names:
                        public = alias.name in getattr(module, "__all__", ()) or (
                            f"{node.module}.{alias.name}" in PUBLIC_MODULES
                        )
                        self.assertTrue(public, f"{where}: {node.module}.{alias.name} is not a public name")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "leakradar":
                            self.assertIn(alias.name, PUBLIC_MODULES, f"{where}: import {alias.name}")

    def test_client_calls_exist_and_fit(self):
        calls = 0
        for where, code in self.sources():
            for node in ast.walk(parse(where, code)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id in CLIENT_CLASSES:
                    calls += 1
                    problem = unbindable(inspect.signature(CLIENT_CLASSES[func.id]), node.args, node.keywords, False)
                    self.assertEqual(problem, "", f"{where}, line {node.lineno}: {func.id}(): {problem}")
                    continue
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == CLIENT_VARIABLE
                ):
                    continue
                calls += 1
                signature = signature_of(func.attr)
                self.assertIsNotNone(signature, f"{where}, line {node.lineno}: the clients have no {func.attr}()")
                assert signature is not None
                problem = unbindable(signature, node.args, node.keywords, True)
                self.assertEqual(problem, "", f"{where}, line {node.lineno}: {func.attr}(): {problem}")
                if func.attr == "paginate" and node.args:
                    self.check_listing(where, node)
        self.assertGreater(calls, 50, "no call of a client found in README.md and examples/")

    def check_listing(self, where: str, node: ast.Call) -> None:
        """The arguments paginate() passes on fit the listing method it is given."""
        listing = node.args[0]
        if not (isinstance(listing, ast.Attribute) and isinstance(listing.value, ast.Name)):
            return
        signature = signature_of(listing.attr)
        self.assertIsNotNone(signature, f"{where}, line {node.lineno}: the clients have no {listing.attr}()")
        assert signature is not None
        self.assertTrue(
            "page" in signature.parameters or "cursor" in signature.parameters,
            f"{where}, line {node.lineno}: {listing.attr}() is not a paginated listing",
        )
        keywords = [keyword for keyword in node.keywords if keyword.arg not in PAGINATE_OWN]
        problem = unbindable(signature, node.args[1:], keywords, True)
        self.assertEqual(problem, "", f"{where}, line {node.lineno}: paginate({listing.attr}, ...): {problem}")


class HouseStyleTest(unittest.TestCase):
    def test_no_em_or_en_dash(self):
        for path in [README, CHANGELOG] + EXAMPLES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for dash in (EM_DASH, EN_DASH):
                    self.assertNotIn(dash, line, f"{path.name}, line {number}: use a comma, a colon or parentheses")

    def test_ascii_only(self):
        for path in [README, CHANGELOG] + EXAMPLES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                others = sorted({f"U+{ord(char):04X}" for char in line if ord(char) > 127})
                self.assertEqual(others, [], f"{path.name}, line {number}: ASCII only")


if __name__ == "__main__":
    unittest.main()
