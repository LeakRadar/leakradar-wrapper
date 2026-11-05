"""
leakradar/types.py and the script that writes it (scripts/generate_types.py).

The generator runs on small inline specs, one per rule. The shipped types.py is then checked as
it is: Python 3.8 syntax, importable, every forward reference resolves, and a few schemas carry
their documented keys. Refresh it with `python scripts/generate_types.py`, verify it with
`python scripts/generate_types.py --check`.
"""

import ast
import contextlib
import importlib.util
import inspect
import io
import itertools
import json
import pathlib
import sys
import tempfile
import types as pytypes
import typing
import unittest
from typing import Any, Dict, List, Literal, Optional, Union
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "leakradar" / "types.py"
EM_DASH = chr(0x2014)


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("generate_types", ROOT / "scripts" / "generate_types.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()
_numbers = itertools.count(1)

OBJECT = {"type": "object", "properties": {"id": {"type": "integer"}}}


def spec_of(schemas: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Test API", "version": "9.8.7"},
        "paths": {},
        "components": {"schemas": schemas},
    }
    spec.update(extra)
    return spec


def is_typed_dict(value: Any) -> bool:
    return isinstance(value, type) and issubclass(value, dict) and hasattr(value, "__total__")


def docs_of(source: str) -> Dict[str, str]:
    """Docstrings of a module, by name: "Name" (class or alias) and "Class.key" (fields)."""
    docs = {}
    body = ast.parse(source).body
    for node, after in zip(body, body[1:] + [None]):
        if isinstance(node, ast.ClassDef):
            docs[node.name] = ast.get_docstring(node) or ""
            fields = node.body
            for field, doc in zip(fields, fields[1:] + [None]):
                if isinstance(field, ast.AnnAssign) and _is_string(doc):
                    docs[node.name + "." + field.target.id] = inspect.cleandoc(doc.value.value)
        elif isinstance(node, ast.Assign) and _is_string(after):
            docs[node.targets[0].id] = inspect.cleandoc(after.value.value)
    return docs


def _is_string(node: Any) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


class GeneratorTestCase(unittest.TestCase):
    def build(self, schemas: Dict[str, Any], **extra: Any) -> Any:
        """Generate the module of these schemas and import it; self.source keeps its source."""
        self.source = gen.generate(spec_of(schemas, **extra))
        ast.parse(self.source, feature_version=(3, 8))
        name = f"generated_types_{next(_numbers)}"
        module = pytypes.ModuleType(name)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(self.source, name, "exec"), module.__dict__)
        return module

    def hints(self, typed_dict: Any) -> Dict[str, Any]:
        return typing.get_type_hints(typed_dict)

    def expression(self, schema: Any, schemas: Optional[Dict[str, Any]] = None, **options: Any) -> str:
        return gen.TypeGenerator(spec_of(schemas or {})).type_expression(schema, **options)


class NamingTest(GeneratorTestCase):
    def test_component_names_keep_letters_and_digits(self):
        module = self.build(
            {
                "PaginatedResponse_ExportBase_": OBJECT,
                "PasswordStrengthCategory-Input": {"type": "string", "enum": ["weak", "strong"]},
                "2fa_status": OBJECT,
            }
        )
        self.assertTrue(is_typed_dict(module.PaginatedResponseExportBase))
        self.assertEqual(module.PasswordStrengthCategoryInput, Literal["weak", "strong"])
        self.assertTrue(is_typed_dict(module.Schema2faStatus))
        self.assertIn("Named `PaginatedResponse_ExportBase_` in the API spec.", docs_of(self.source)["PaginatedResponseExportBase"])

    def test_names_colliding_after_sanitizing_get_a_number(self):
        generator = gen.TypeGenerator(spec_of({"Foo-Bar": OBJECT, "Foo_Bar": OBJECT, "FooBar": OBJECT, "FooBar2": OBJECT}))
        # The names that need no change keep them, the others follow in sorted order.
        self.assertEqual(
            generator.names, {"FooBar": "FooBar", "FooBar2": "FooBar2", "Foo-Bar": "FooBar3", "Foo_Bar": "FooBar4"}
        )
        module = self.build({"Foo-Bar": OBJECT, "Foo_Bar": OBJECT, "FooBar": OBJECT, "FooBar2": OBJECT})
        self.assertEqual(sorted(module.__all__), ["FooBar", "FooBar2", "FooBar3", "FooBar4"])

    def test_names_of_the_typing_module_and_keywords_are_not_shadowed(self):
        module = self.build(
            {
                "List": OBJECT,
                "Any": {"type": "string", "enum": ["a"]},
                "None": OBJECT,
                "Holder": {"type": "object", "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/List"}}}},
            }
        )
        self.assertEqual(sorted(module.__all__), ["AnySchema", "Holder", "ListSchema", "NoneSchema"])
        self.assertIs(module.List, typing.List)
        self.assertEqual(self.hints(module.Holder)["items"], List[module.ListSchema])

    def test_inline_objects_are_named_after_where_they_sit(self):
        module = self.build(
            {
                "Limits": {
                    "type": "object",
                    "properties": {
                        "daily_quota": {"type": "object", "properties": {"used": {"type": "integer"}}},
                        "families": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
                        "by_reason": {"type": "object", "additionalProperties": {"type": "object", "properties": {"n": {"type": "integer"}}}},
                        "pause": {"anyOf": [{"type": "object", "properties": {"until": {"type": "string"}}}, {"type": "null"}]},
                        "either": {
                            "oneOf": [
                                {"type": "object", "properties": {"a": {"type": "string"}}},
                                {"type": "object", "properties": {"b": {"type": "string"}}},
                            ]
                        },
                    },
                },
            }
        )
        hints = self.hints(module.Limits)
        self.assertEqual(hints["daily_quota"], module.LimitsDailyQuota)
        self.assertEqual(hints["families"], List[module.LimitsFamiliesItem])
        self.assertEqual(hints["by_reason"], Dict[str, module.LimitsByReasonValue])
        self.assertEqual(hints["pause"], Optional[module.LimitsPause])
        self.assertEqual(hints["either"], Union[module.LimitsEitherVariant1, module.LimitsEitherVariant2])
        self.assertEqual(set(self.hints(module.LimitsFamiliesItem)), {"name"})
        self.assertEqual(set(self.hints(module.LimitsEitherVariant2)), {"b"})
        self.assertEqual(docs_of(self.source)["LimitsFamiliesItem"], "An item of the `families` key of Limits.")

    def test_inline_objects_of_inline_objects(self):
        module = self.build(
            {
                "Outer": {
                    "type": "object",
                    "properties": {
                        "inner": {
                            "type": "object",
                            "description": "The inner part.",
                            "properties": {"deep": {"type": "object", "properties": {"x": {"type": "integer"}}}},
                        }
                    },
                }
            }
        )
        self.assertEqual(self.hints(module.OuterInner)["deep"], module.OuterInnerDeep)
        self.assertEqual(docs_of(self.source)["OuterInner"], "The inner part.")
        self.assertEqual(docs_of(self.source)["OuterInnerDeep"], "The `deep` key of OuterInner.")

    def test_an_inline_name_taken_by_a_component_gets_a_number(self):
        module = self.build(
            {
                "Parent": {"type": "object", "properties": {"child": {"type": "object", "properties": {"x": {"type": "integer"}}}}},
                "ParentChild": OBJECT,
            }
        )
        self.assertEqual(self.hints(module.Parent)["child"], module.ParentChild2)
        self.assertEqual(set(self.hints(module.ParentChild)), {"id"})

    def test_inline_objects_of_a_top_level_union(self):
        module = self.build({"Maybe": {"anyOf": [{"type": "object", "properties": {"x": {"type": "integer"}}}, {"type": "null"}]}})
        # An alias keeps its forward references at runtime: compare the source.
        self.assertIn('\nMaybe = Optional["MaybeVariant"]\n', self.source)
        self.assertEqual(self.hints(module.MaybeVariant), {"x": int})


class TypeRulesTest(GeneratorTestCase):
    def test_scalars(self):
        self.assertEqual(self.expression({"type": "string"}), "str")
        self.assertEqual(self.expression({"type": "string", "format": "date-time"}), "str")
        self.assertEqual(self.expression({"type": "string", "format": "binary"}), "bytes")
        self.assertEqual(self.expression({"type": "integer"}), "int")
        self.assertEqual(self.expression({"type": "number"}), "float")
        self.assertEqual(self.expression({"type": "boolean"}), "bool")
        self.assertEqual(self.expression({}), "Any")
        self.assertEqual(self.expression(True), "Any")

    def test_refs(self):
        schemas = {"Target": OBJECT}
        self.assertEqual(self.expression({"$ref": "#/components/schemas/Target"}, schemas), "Target")
        self.assertEqual(self.expression({"$ref": "#/components/schemas/Target", "description": "x"}, schemas), "Target")
        self.assertEqual(self.expression({"$ref": "#/components/schemas/Missing"}, schemas), "Any")
        self.assertEqual(self.expression({"$ref": "https://example.com/schema.json"}, schemas), "Any")
        module = self.build({"Target": OBJECT, "Holder": {"type": "object", "properties": {"t": {"$ref": "#/components/schemas/Target"}}}})
        self.assertIn('    t: "Target"\n', self.source)
        self.assertIs(self.hints(module.Holder)["t"], module.Target)

    def test_any_of_and_one_of(self):
        self.assertEqual(self.expression({"anyOf": [{"type": "string"}, {"type": "null"}]}), "Optional[str]")
        self.assertEqual(self.expression({"oneOf": [{"type": "integer"}, {"type": "string"}]}), "Union[int, str]")
        self.assertEqual(
            self.expression({"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]}), "Optional[Union[int, str]]"
        )
        self.assertEqual(self.expression({"anyOf": [{"type": "string"}, {"type": "string", "format": "email"}]}), "str")
        self.assertEqual(self.expression({"anyOf": [{"type": "string"}, {}]}), "Any")
        self.assertEqual(self.expression({"anyOf": [{"type": "null"}]}), "None")
        self.assertEqual(self.expression({"anyOf": [{"anyOf": [{"type": "integer"}, {"type": "null"}]}, {"type": "null"}]}), "Optional[int]")

    def test_nullable_forms(self):
        self.assertEqual(self.expression({"type": ["string", "null"]}), "Optional[str]")
        self.assertEqual(self.expression({"type": "string", "nullable": True}), "Optional[str]")
        self.assertEqual(self.expression({"type": ["integer", "string"]}), "Union[int, str]")

    def test_all_of(self):
        schemas = {"Target": OBJECT, "Other": {"type": "object", "properties": {"name": {"type": "string"}}}}
        self.assertEqual(self.expression({"allOf": [{"$ref": "#/components/schemas/Target"}]}, schemas), "Target")
        # A part that only documents does not make an intersection.
        self.assertEqual(
            self.expression({"allOf": [{"$ref": "#/components/schemas/Target"}, {"description": "doc"}]}, schemas), "Target"
        )
        self.assertEqual(self.expression({"allOf": [{"type": "string"}, {"maxLength": 3}]}), "str")
        module = self.build(
            dict(
                schemas,
                Merged={
                    "allOf": [
                        {"$ref": "#/components/schemas/Target"},
                        {"$ref": "#/components/schemas/Other"},
                        {"type": "object", "properties": {"extra": {"type": "boolean"}}},
                    ]
                },
                Alias={"allOf": [{"$ref": "#/components/schemas/Target"}], "description": "Same as Target."},
            )
        )
        self.assertEqual(self.hints(module.Merged), {"id": int, "name": str, "extra": bool})
        self.assertIs(module.Alias, module.Target)
        self.assertEqual(docs_of(self.source)["Alias"], "Same as Target.")

    def test_enums_and_consts(self):
        self.assertEqual(self.expression({"type": "string", "enum": ["csv", "txt"]}), 'Literal["csv", "txt"]')
        self.assertEqual(self.expression({"type": "integer", "enum": [1, 2, 2]}), "Literal[1, 2]")
        self.assertEqual(self.expression({"enum": ["a", None]}), 'Optional[Literal["a"]]')
        self.assertEqual(self.expression({"enum": [True, 1]}), "Literal[True, 1]")
        self.assertEqual(self.expression({"type": "string", "const": "leak"}), 'Literal["leak"]')
        self.assertEqual(self.expression({"const": True}), "Literal[True]")
        self.assertEqual(self.expression({"type": "number", "enum": [0.5, 1.5]}), "float")
        self.assertEqual(
            self.expression({"anyOf": [{"const": "a"}, {"const": "b"}, {"type": "null"}]}), 'Optional[Literal["a", "b"]]'
        )
        self.assertEqual(self.expression({"enum": ['say "hi"\\']}), 'Literal["say \\"hi\\"\\\\"]')
        self.assertEqual(self.expression({"enum": ["a" + EM_DASH + "b"]}), 'Literal["a\\u2014b"]')
        module = self.build({"Format": {"type": "string", "enum": ["csv", "txt"], "description": "A format."}})
        self.assertEqual(module.Format, Literal["csv", "txt"])
        self.assertEqual(docs_of(self.source)["Format"], "A format.")

    def test_arrays(self):
        self.assertEqual(self.expression({"type": "array", "items": {"type": "integer"}}), "List[int]")
        self.assertEqual(self.expression({"type": "array"}), "List[Any]")
        self.assertEqual(self.expression({"items": {"type": "string"}}), "List[str]")
        self.assertEqual(
            self.expression({"type": "array", "items": {"type": "array", "items": {"$ref": "#/components/schemas/T"}}}, {"T": OBJECT}),
            "List[List[T]]",
        )

    def test_additional_properties(self):
        self.assertEqual(self.expression({"type": "object"}), "Dict[str, Any]")
        self.assertEqual(self.expression({"type": "object", "additionalProperties": True}), "Dict[str, Any]")
        self.assertEqual(self.expression({"type": "object", "additionalProperties": {"type": "integer"}}), "Dict[str, int]")
        self.assertEqual(self.expression({"additionalProperties": {"type": "string"}}), "Dict[str, str]")
        self.assertEqual(
            self.expression(
                {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/T"}, "propertyNames": {"enum": ["a"]}},
                {"T": OBJECT},
            ),
            "Dict[str, T]",
        )
        # An object of known keys may have others: the TypedDict declares the known ones.
        module = self.build(
            {
                "Free": {"type": "object", "additionalProperties": True, "description": "Anything."},
                "Counts": {"type": "object", "additionalProperties": {"type": "integer"}},
                "Known": {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": {"type": "integer"}},
            }
        )
        self.assertEqual(module.Free, Dict[str, Any])
        self.assertEqual(module.Counts, Dict[str, int])
        self.assertEqual(self.hints(module.Known), {"a": str})
        self.assertEqual(docs_of(self.source)["Free"], "Anything.")

    def test_an_object_outside_the_components_is_a_dict(self):
        self.assertEqual(self.expression({"type": "object", "properties": {"a": {"type": "string"}}}), "Dict[str, Any]")


class RecursionTest(GeneratorTestCase):
    def test_a_schema_that_contains_itself(self):
        module = self.build(
            {
                "Node": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                        "parent": {"anyOf": [{"$ref": "#/components/schemas/Node"}, {"type": "null"}]},
                    },
                }
            }
        )
        hints = self.hints(module.Node)
        self.assertEqual(hints["children"], List[module.Node])
        self.assertEqual(hints["parent"], Optional[module.Node])

    def test_schemas_that_contain_each_other(self):
        module = self.build(
            {
                "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"type": "object", "properties": {"a": {"type": "array", "items": {"$ref": "#/components/schemas/A"}}}},
            }
        )
        self.assertIs(self.hints(module.A)["b"], module.B)
        self.assertEqual(self.hints(module.B)["a"], List[module.A])

    def test_aliases_of_aliases_and_cycles(self):
        module = self.build(
            {
                "First": {"$ref": "#/components/schemas/Second"},
                "Second": {"$ref": "#/components/schemas/Target"},
                "Target": OBJECT,
                "Loop1": {"$ref": "#/components/schemas/Loop2"},
                "Loop2": {"$ref": "#/components/schemas/Loop1"},
            }
        )
        self.assertIs(module.First, module.Target)
        self.assertIs(module.Second, module.Target)
        self.assertIs(module.Loop1, Any)
        self.assertIs(module.Loop2, Any)

    def test_a_pointer_loop_outside_the_components_ends(self):
        spec = spec_of({}, definitions={"a": {"$ref": "#/definitions/b"}, "b": {"$ref": "#/definitions/a"}})
        self.assertEqual(gen.TypeGenerator(spec).type_expression({"$ref": "#/definitions/a"}), "Any")


class KeysTest(GeneratorTestCase):
    def test_keys_that_are_not_identifiers_use_the_call_syntax(self):
        keys = ["content-type", "@timestamp", "1st", "from", "class", "None", "__private", "caf" + chr(0xE9), "ok"]
        module = self.build(
            {
                "Weird": {
                    "type": "object",
                    "description": "Odd keys.",
                    "properties": dict((key, {"type": "string", "description": "Key " + key}) for key in keys),
                }
            }
        )
        self.assertIn('Weird = TypedDict(\n    "Weird",\n', self.source)
        self.assertEqual(list(module.Weird.__annotations__), keys)
        self.assertIs(module.Weird.__total__, False)
        self.assertEqual(self.hints(module.Weird)["from"], str)
        self.assertEqual(docs_of(self.source)["Weird"], "Odd keys.")
        self.assertIn("        # Key from\n", self.source)

    def test_a_key_named_like_a_type_it_uses_uses_the_call_syntax(self):
        module = self.build(
            {
                "Item": OBJECT,
                "Box": {
                    "type": "object",
                    "properties": {"Item": {"$ref": "#/components/schemas/Item"}, "str": {"type": "string"}},
                },
                "Plain": {"type": "object", "properties": {"list": {"type": "array", "items": {"type": "integer"}}}},
            }
        )
        self.assertIn('Box = TypedDict(\n    "Box",\n', self.source)
        self.assertIs(self.hints(module.Box)["Item"], module.Item)
        self.assertIn("class Plain(TypedDict, total=False):", self.source)

    def test_every_typed_dict_is_total_false(self):
        module = self.build({"A": OBJECT, "B": {"type": "object", "properties": {"x-y": {"type": "integer"}}}})
        self.assertIs(module.A.__total__, False)
        self.assertIs(module.B.__total__, False)
        self.assertEqual(module.A(), {})


class DocsTest(GeneratorTestCase):
    def test_descriptions_become_docstrings(self):
        module = self.build(
            {
                "Export": {
                    "type": "object",
                    "description": "An export job.\n\nPoll it until `COMPLETED`.",
                    "properties": {
                        "id": {"type": "integer", "description": "Export ID."},
                        "old": {"type": "string", "deprecated": True},
                        "plain": {"type": "string"},
                    },
                }
            }
        )
        docs = docs_of(self.source)
        self.assertEqual(inspect.cleandoc(module.Export.__doc__), "An export job.\n\nPoll it until `COMPLETED`.")
        self.assertEqual(docs["Export.id"], "Export ID.")
        self.assertEqual(docs["Export.old"], "Deprecated.")
        self.assertNotIn("Export.plain", docs)

    def test_texts_are_cleaned_and_escaped(self):
        tricky = 'Fast ' + EM_DASH + ' or slow, see """docs""" in C:\\temp\n' + EM_DASH + ' a point\nends with "quote"'
        module = self.build({"Tricky": {"type": "object", "description": tricky, "properties": {"a": {"type": "string", "description": tricky}}}})
        expected = 'Fast, or slow, see """docs""" in C:\\temp\n- a point\nends with "quote"'
        self.assertEqual(inspect.cleandoc(module.Tricky.__doc__), expected)
        self.assertEqual(docs_of(self.source)["Tricky.a"], expected)
        self.assertNotIn(EM_DASH, self.source)

    def test_long_lines_are_wrapped_but_lists_and_tables_keep_their_layout(self):
        words = " ".join(f"word{number}" for number in range(40))
        text = words + "\n\n- " + words + "\n\n| a | " + words + " |"
        self.build({"Long": {"type": "object", "description": text, "properties": {"a": {"type": "string"}}}})
        lines = docs_of(self.source)["Long"].split("\n")
        self.assertTrue(all(len(line) <= 96 for line in lines if not line.startswith("|")))
        self.assertIn("  word", "\n".join(lines))  # the hanging indent of the list item
        self.assertIn("| a | " + words + " |", lines)


class OutputTest(GeneratorTestCase):
    SCHEMAS = {
        "Zeta": {"type": "object", "properties": {"a": {"$ref": "#/components/schemas/Alpha"}}},
        "Alpha": {"type": "string", "enum": ["x", "y"]},
        "Mid": {"type": "object", "properties": {"n": {"type": "object", "properties": {"m": {"type": "integer"}}}}},
    }

    def test_the_output_depends_on_the_spec_alone(self):
        first = gen.generate(spec_of(self.SCHEMAS))
        self.assertEqual(gen.generate(spec_of(self.SCHEMAS)), first)
        reordered = dict(reversed(list(self.SCHEMAS.items())))
        self.assertEqual(gen.generate(spec_of(reordered)), first)
        self.assertEqual(gen.generate(json.loads(json.dumps(spec_of(self.SCHEMAS)))), first)

    def test_header_imports_and_all(self):
        module = self.build(self.SCHEMAS)
        self.assertTrue(self.source.startswith('"""\nTypes of the LeakRadar API'))
        header = " ".join(ast.get_docstring(ast.parse(self.source)).split())
        self.assertIn("OpenAPI spec of the Test API (https://api.leakradar.io/openapi.json), API version 9.8.7", header)
        self.assertIn("Do not edit by hand", header)
        self.assertEqual(module.__all__, ["Alpha", "Mid", "MidN", "Zeta"])
        self.assertIn("\nfrom typing import Literal, TypedDict\n", self.source)
        positions = [self.source.index(name + " = ") if name == "Alpha" else self.source.index("class " + name) for name in module.__all__]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(self.source.endswith("\n") and not self.source.endswith("\n\n"))

    def test_only_the_used_typing_names_are_imported(self):
        self.build({"Only": {"type": "string", "enum": ["a"]}})
        self.assertIn("\nfrom typing import Literal\n", self.source)
        self.build({})
        self.assertNotIn("from typing", self.source)
        self.assertIn("__all__ = [\n]", self.source)

    def test_a_minimal_spec(self):
        source = gen.generate({"paths": {}})
        self.assertIn("API version unknown", ast.get_docstring(ast.parse(source)))


class HelperTest(GeneratorTestCase):
    PATHS = {
        "/tasks/{task_id}": {
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}}}}
        },
        "/lists": {
            "get": {
                "responses": {
                    "422": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                    "200": {"content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Task"}}}}},
                }
            }
        },
        "/report": {"get": {"responses": {"200": {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}}}},
        "/file": {"post": {"responses": {"307": {"description": "Redirect"}}}},
        "/shared": {"get": {"responses": {"200": {"$ref": "#/components/responses/TaskAnswer"}}}},
        "/free": {"patch": {"responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}}},
    }

    def setUp(self):
        self.spec = spec_of(
            {"Task": OBJECT, "Error": OBJECT, "Weird_Name": {"type": "object", "properties": {"in": {"type": "object", "properties": {"x": {"type": "string"}}}}}},
            paths=self.PATHS,
        )
        self.spec["components"]["responses"] = {
            "TaskAnswer": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}}
        }
        self.generator = gen.TypeGenerator(self.spec)

    def test_type_expression_options(self):
        refs, imports = set(), set()
        schema = {"anyOf": [{"type": "array", "items": {"$ref": "#/components/schemas/Task"}}, {"type": "null"}]}
        self.assertEqual(self.generator.type_expression(schema, refs=refs, imports=imports), "Optional[List[Task]]")
        self.assertEqual(refs, {"Task"})
        self.assertEqual(imports, {"Optional", "List"})
        self.assertEqual(self.generator.type_expression(schema, quote=True), 'Optional[List["Task"]]')
        self.assertEqual(self.generator.type_expression(schema, prefix="types."), "Optional[List[types.Task]]")

    def test_schemas_of_the_spec_are_found_by_identity(self):
        schemas = self.spec["components"]["schemas"]
        self.assertEqual(self.generator.type_expression(schemas["Weird_Name"]), "WeirdName")
        self.assertEqual(self.generator.type_expression(schemas["Weird_Name"]["properties"]["in"]), "WeirdNameIn")
        self.assertEqual(self.generator.type_expression(dict(schemas["Weird_Name"]["properties"]["in"])), "Dict[str, Any]")
        self.assertEqual(self.generator.names["Weird_Name"], "WeirdName")

    def test_response_type(self):
        self.assertEqual(self.generator.response_type("GET", "/tasks/{task_id}"), "Task")
        self.assertEqual(self.generator.response_type("get", "/lists"), "List[Task]")
        self.assertEqual(self.generator.response_type("GET", "/report"), "bytes")
        self.assertEqual(self.generator.response_type("PATCH", "/free"), "Dict[str, Any]")
        self.assertEqual(self.generator.response_type("GET", "/shared"), "Task")
        imports = set()
        self.assertEqual(self.generator.response_type("POST", "/file", imports=imports), "Any")
        self.assertEqual(imports, {"Any"})
        self.assertIsNone(self.generator.response_schema("POST", "/file"))
        with self.assertRaises(KeyError):
            self.generator.response_type("DELETE", "/tasks/{task_id}")

    def test_counts(self):
        self.assertEqual(self.generator.counts(), (4, 0))


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = pathlib.Path(directory.name)
        self.spec_path = self.directory / "openapi.json"
        self.spec_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(spec_of({"A": OBJECT})).encode("utf-8"))
        self.out = self.directory / "types.py"

    def run_main(self, *args: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = gen.main(["--spec", str(self.spec_path), "--out", str(self.out)] + list(args))
        self.output = output.getvalue()
        return code

    def test_write_then_check(self):
        self.assertEqual(self.run_main("--check"), 1)  # no file yet
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.out.read_bytes(), gen.generate(spec_of({"A": OBJECT})).encode("utf-8"))
        self.assertNotIn(b"\r\n", self.out.read_bytes())
        self.assertEqual(self.run_main("--check"), 0)
        self.assertIn("up to date", self.output)

    def test_check_accepts_crlf_and_reports_a_stale_file(self):
        self.run_main()
        self.out.write_bytes(self.out.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(self.run_main("--check"), 0)
        self.spec_path.write_text(json.dumps(spec_of({"A": OBJECT, "B": OBJECT})), encoding="utf-8")
        before = self.out.read_bytes()
        self.assertEqual(self.run_main("--check"), 1)
        self.assertIn("+class B(TypedDict, total=False):", self.output)
        self.assertIn("is out of date", self.output)
        self.assertEqual(self.out.read_bytes(), before)

    def test_a_url_is_fetched(self):
        with mock.patch.object(gen, "fetch", return_value=spec_of({"A": OBJECT})) as fetch:
            self.assertEqual(gen.load_spec("https://api.leakradar.io/openapi.json")["info"]["version"], "9.8.7")
        fetch.assert_called_once_with("https://api.leakradar.io/openapi.json")
        self.assertEqual(gen.load_spec(str(self.spec_path))["components"]["schemas"], {"A": OBJECT})


class ShippedTypesTest(unittest.TestCase):
    """leakradar/types.py as committed."""

    @classmethod
    def setUpClass(cls):
        cls.source = SHIPPED.read_bytes().decode("utf-8").replace("\r\n", "\n")
        from leakradar import types

        cls.types = types

    def typed_dicts(self) -> Dict[str, Any]:
        return dict((name, getattr(self.types, name)) for name in self.types.__all__ if is_typed_dict(getattr(self.types, name)))

    def keys(self, name: str) -> set:
        return set(getattr(self.types, name).__annotations__)

    def test_python_3_8_syntax_and_header(self):
        tree = ast.parse(self.source, feature_version=(3, 8))
        header = ast.get_docstring(tree)
        self.assertIn("Do not edit by hand", header)
        self.assertIn("API version", header)
        self.assertIn("https://api.leakradar.io/openapi.json", header)
        self.assertNotIn(EM_DASH, self.source)
        self.assertNotIn("from __future__", self.source)

    def test_every_name_exists_and_every_reference_resolves(self):
        names = self.types.__all__
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertTrue(hasattr(self.types, name), name)
        typed_dicts = self.typed_dicts()
        self.assertGreater(len(typed_dicts), 150)
        for name, typed_dict in typed_dicts.items():
            self.assertIs(typed_dict.__total__, False, name)
            self.assertTrue(typed_dict.__annotations__, name)
            typing.get_type_hints(typed_dict)  # raises on a dangling forward reference

    def test_documented_keys(self):
        self.assertLessEqual(
            {"task_id", "status", "kind", "scope", "running", "completed", "total", "updated", "error", "created_at", "finished_at"},
            self.keys("TaskStatusOut"),
        )
        hints = typing.get_type_hints(self.types.TaskStatusOut)
        self.assertEqual(hints["status"], Optional[Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELED"]])
        self.assertEqual(hints["error"], Optional[self.types.TaskError])
        self.assertLessEqual(
            {"id", "user_id", "filename", "type", "params", "status", "timestamp", "started_at", "finished_at"},
            self.keys("ExportBase"),
        )
        self.assertEqual(typing.get_type_hints(self.types.PaginatedResponseExportBase)["items"], List[self.types.ExportBase])
        self.assertLessEqual(
            {"lane", "credential_kind", "families", "daily", "concurrency", "pauses", "last_24h", "last_7d"},
            self.keys("RateLimitsResponse"),
        )
        self.assertIs(typing.get_type_hints(self.types.RateLimitsResponse)["daily"], self.types.RateLimitDaily)

    def test_webhook_delivery_models(self):
        for delivery, alert, kind in (
            ("CredentialAlertDelivery", "WebhookCredentialAlert", "leak"),
            ("RawAlertDelivery", "WebhookRawAlert", "raw"),
            ("DarkWebAlertDelivery", "WebhookDarkWebAlert", "dark_web"),
        ):
            self.assertEqual(self.keys(delivery), {"leaks"})
            alert_type = getattr(self.types, alert)
            self.assertIs(typing.get_type_hints(getattr(self.types, delivery))["leaks"], alert_type)
            hints = typing.get_type_hints(alert_type)
            self.assertEqual(hints["type"], Literal[kind])
            self.assertLessEqual({"run_id", "notification_id", "run_url", "target", "check_id", "section_index"}, set(hints))
        self.assertLessEqual({"unlocked_leaks", "previews", "new_leaks_count"}, self.keys("WebhookCredentialAlert"))
        self.assertLessEqual({"parts", "new_parts_count", "matching_lines_count"}, self.keys("WebhookRawAlert"))
        self.assertLessEqual({"items", "new_items_count"}, self.keys("WebhookDarkWebAlert"))

    def test_enums_are_literals(self):
        self.assertEqual(self.types.ExportFormatEnum, Literal["csv", "txt", "json"])
        self.assertNotIn("ExportFormatEnum", self.typed_dicts())


if __name__ == "__main__":
    unittest.main()
