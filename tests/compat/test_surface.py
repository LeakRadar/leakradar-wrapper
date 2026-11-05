"""
The public surface of every published version still holds in the current SDK.

tests/compat/fixtures/surface-<version>.json freezes what each release on PyPI exposes
(scripts/build_compat_fixtures.py). Code written against any of them must keep working after
an upgrade: the same names import from `leakradar` and `leakradar.client`; every method still
exists and is still a coroutine; positional parameters keep their names and order; keyword
parameters and defaults are kept; no new parameter is required; exceptions keep their bases,
their constructor and their message.

ALLOWED lists what a published version already changed, with the version that shipped it:
a fixture older than that version may differ on that point, the fixture of that version and
every later one may not, so the list can never hide a new break.
"""

import asyncio
import inspect
import json
import pathlib
import unittest

import httpx

import leakradar
import leakradar.client
from leakradar.client import LeakRadarClient

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# (where, what) -> (version that shipped the change, why). `where` is a method of
# LeakRadarClient, "__init__", a class, or a module ("leakradar", "leakradar.client"); a key
# ending in ":*" covers every parameter. Version "*" allows the change in every fixture.
ALLOWED = {
    ("__init__", "default:user_agent"): (
        "*",
        "The default User-Agent names the SDK version: it is how the API counts the versions in use.",
    ),
    ("_request", "**kwargs"): (
        "0.1.4",
        "_request(method, endpoint, **kwargs) became explicit keywords (params, json, headers): "
        "other httpx arguments (data, files, timeout...) are refused since 0.1.4.",
    ),
    ("export_advanced", "positional"): (
        "0.1.4",
        "export_advanced(username, ...) became export_advanced(format=None, **filters) in 0.1.4: "
        "the filters are keyword arguments since, a first positional argument is the format.",
    ),
    ("export_advanced", "keyword:*"): (
        "0.1.4",
        "0.1.4 folded the named filters of export_advanced into **filters: keyword calls work unchanged.",
    ),
    ("export_unlocked_leaks", "positional"): (
        "0.1.4",
        "0.1.4 inserted list_id, list_none and format before unlocked_at_min.",
    ),
    ("search_advanced", "positional"): (
        "0.1.5",
        "0.1.2 to 0.1.5 inserted filters (the *_not variants, username_hash, password_hash) in the "
        "middle of the positional filters; the last order dates from 0.1.5.",
    ),
    ("search_email", "positional"): ("0.1.4", "0.1.4 inserted page_size after page."),
    ("unlock_domain_leaks", "positional"): ("0.1.4", "0.1.4 inserted is_email before max_leaks."),
    ("unlock_email_leaks", "positional"): ("0.1.4", "0.1.4 inserted list_id before search."),
    ("test_notification_method_with_data", "positional"): (
        "0.2.0",
        "0.2.0 inserted notification_type before data (the API reads no body any more); a dict passed "
        "as second argument is still recognised and handled as 0.1.x data (test_golden_requests).",
    ),
}
for _removed in (
    "export_metadata_leaks",
    "export_metadata_search",
    "export_metadata_urls",
    "metadata_detail",
    "metadata_leaks",
    "metadata_leaks_count",
    "queue_metadata_unlock_task",
    "search_metadata",
    "unlock_metadata_leaks",
):
    ALLOWED[(_removed, "removed")] = (
        "0.1.6",
        "The API removed the pages metadata search (/search/metadata...); 0.1.6 dropped its methods.",
    )


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def load_surfaces():
    paths = sorted(FIXTURES.glob("surface-*.json"), key=lambda p: version_key(p.stem.split("-", 1)[1]))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def allowed(where, what, version):
    """The ALLOWED key that excuses this difference in the fixture of `version`, else None."""
    for key in ((where, what), (where, what.split(":", 1)[0] + ":*")):
        entry = ALLOWED.get(key)
        if entry and (entry[0] == "*" or version_key(version) < version_key(entry[0])):
            return key
    return None


def describe(func, skip_first=True):
    """The parameters of a callable, in the form of the fixtures."""
    try:
        params = list(inspect.signature(func).parameters.values())[1 if skip_first else 0:]
    except ValueError:  # a builtin without signature, such as Exception.__init__ on some versions
        return [
            {"name": "args", "kind": "VAR_POSITIONAL", "default": None, "position": None},
            {"name": "kwargs", "kind": "VAR_KEYWORD", "default": None, "position": None},
        ]
    described, position = [], 0
    for param in params:
        positional = param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
        described.append(
            {
                "name": param.name,
                "kind": param.kind.name,
                "default": None if param.default is param.empty else repr(param.default),
                "position": position if positional else None,
            }
        )
        position += positional
    return described


def current_method(name):
    """(coroutine, parameters) of a current LeakRadarClient method, None when it is gone."""
    try:
        raw = inspect.getattr_static(LeakRadarClient, name)
    except AttributeError:
        return None
    func = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
    if not callable(func):
        return None
    return inspect.iscoroutinefunction(func), describe(func, skip_first=not isinstance(raw, staticmethod))


def parameter_differences(where, old, new):
    """(where, what, message) for every way `new` breaks a call written against `old`."""
    found = []
    old_positional = [p["name"] for p in old if p["position"] is not None]
    new_positional = [p["name"] for p in new if p["position"] is not None]
    if new_positional[: len(old_positional)] != old_positional:
        found.append((where, "positional", f"positional parameters {old_positional} became {new_positional}"))
    new_by_name = {p["name"]: p for p in new}
    new_kinds = {p["kind"] for p in new}
    for param in old:
        name, kind = param["name"], param["kind"]
        if kind in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            if kind not in new_kinds:
                found.append((where, "**kwargs" if kind == "VAR_KEYWORD" else "*args", f"{kind} `{name}` is gone"))
            continue
        current = new_by_name.get(name)
        if current is None and "VAR_KEYWORD" in new_kinds:
            found.append((where, f"keyword:{name}", f"`{name}` is no longer a parameter, **kwargs takes it"))
        elif current is None or current["kind"] not in ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"):
            found.append((where, f"keyword:{name}", f"`{name}` can no longer be passed by name"))
        elif param["default"] is not None and current["default"] != param["default"]:
            found.append((where, f"default:{name}", f"`{name}` default {param['default']} became {current['default']}"))
    old_names = {p["name"] for p in old}
    for param in new:
        if param["name"] in old_names or param["kind"] in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            continue
        if param["default"] is None:
            found.append((where, f"required:{param['name']}", f"new parameter `{param['name']}` has no default"))
    return found


def surface_differences(surface):
    """Every difference between a published surface and the current package."""
    found = []
    for name in surface["package"]["names"]:
        if not hasattr(leakradar, name):
            found.append(("leakradar", f"name:{name}", f"`leakradar.{name}` is gone"))
    for name in surface["package"]["all"]:
        if name not in getattr(leakradar, "__all__", ()):
            found.append(("leakradar", f"all:{name}", f"`{name}` left leakradar.__all__"))
    for name in surface["client_module"]["names"]:
        if not hasattr(leakradar.client, name):
            found.append(("leakradar.client", f"name:{name}", f"`leakradar.client.{name}` is gone"))

    for name, cls in surface["classes"].items():
        current = getattr(leakradar.client, name, None)
        if not inspect.isclass(current):
            continue  # reported as a missing name
        mro = [c.__name__ for c in current.__mro__]
        lost = [base for base in cls["mro"] if base not in mro]
        if lost:
            found.append((name, "bases", f"{name} no longer derives from {lost}"))
        if "init" in cls:
            found += parameter_differences(name, cls["init"], describe(current.__init__))
        sample = cls.get("sample")
        if sample:
            try:
                error = current(*sample["args"])
            except Exception as exc:
                found.append((name, "construct", f"{name}(*{sample['args']}) raises {exc!r}"))
                continue
            if str(error) != sample["str"]:
                found.append((name, "str", f"str() {sample['str']!r} became {str(error)!r}"))
            for attr, value in sample["attrs"].items():
                if getattr(error, attr, None) != value:
                    found.append((name, f"attr:{attr}", f"{attr} {value!r} became {getattr(error, attr, None)!r}"))

    client = surface["client"]
    for attr, value in client["attributes"].items():
        if repr(getattr(LeakRadarClient, attr, None)) != value:
            found.append(("LeakRadarClient", f"attr:{attr}", f"{attr} {value} became {getattr(LeakRadarClient, attr, None)!r}"))
    found += parameter_differences("__init__", client["init"], describe(LeakRadarClient.__init__))
    for name, method in client["methods"].items():
        current = current_method(name)
        if current is None:
            found.append((name, "removed", f"LeakRadarClient.{name} is gone"))
            continue
        coroutine, params = current
        if coroutine != method["coroutine"]:
            found.append((name, "coroutine", f"{name} is {'now' if coroutine else 'no longer'} a coroutine"))
        found += parameter_differences(name, method["params"], params)
    return found


def instance_attributes():
    """Attribute name -> value of a fresh client (closed again)."""
    client = LeakRadarClient(token="t")
    try:
        return dict(vars(client))
    finally:
        asyncio.run(client.aclose())


async def default_user_agent():
    """The User-Agent a client built without one sends."""
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={})

    client = LeakRadarClient(token="t", transport=httpx.MockTransport(handle))
    try:
        await client.get_profile()
    finally:
        await client.aclose()
    return sent[0].headers["user-agent"]


class PublishedSurfaceTest(unittest.TestCase):
    surfaces = load_surfaces()

    def test_fixtures_cover_the_published_versions(self):
        versions = [s["version"] for s in self.surfaces]
        for version in ("0.1.4", "0.1.6", "0.2.0"):
            self.assertIn(version, versions)

    def test_every_published_surface_still_holds(self):
        for surface in self.surfaces:
            for where, what, message in surface_differences(surface):
                with self.subTest(version=surface["version"], where=where, what=what):
                    if allowed(where, what, surface["version"]) is None:
                        self.fail(f"{surface['version']}: {message} (add it to ALLOWED only if it shipped already)")

    def test_every_allowed_entry_matches_a_published_difference(self):
        used = set()
        for surface in self.surfaces:
            for where, what, _ in surface_differences(surface):
                key = allowed(where, what, surface["version"])
                if key:
                    used.add(key)
        self.assertEqual(sorted(set(ALLOWED) - used), [], "ALLOWED entries that excuse nothing: remove them")

    def test_instances_keep_their_attributes(self):
        attributes = instance_attributes()
        for surface in self.surfaces:
            for name in surface["client"]["instance_attributes"]:
                with self.subTest(version=surface["version"], attribute=name):
                    self.assertIn(name, attributes)
        self.assertIsInstance(attributes["_client"], httpx.AsyncClient)

    def test_default_user_agent_names_the_version(self):
        # How the API counts the versions in use: the default User-Agent must keep this form.
        self.assertEqual(asyncio.run(default_user_agent()), f"LeakRadar-Python-Client/{leakradar.__version__}")


if __name__ == "__main__":
    unittest.main()
