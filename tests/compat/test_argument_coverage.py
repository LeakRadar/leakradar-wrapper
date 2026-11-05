"""
Every argument of every published method is exercised by the compatibility net.

test_golden_requests.py compares what the recorded calls send. An argument that no call
passes, or passes only with its default value, is never compared: the current client could
drop it, rename it or stop honouring it without any test noticing. This test reads the
published signatures (fixtures/surface-<version>.json) and the recorded calls
(fixtures/requests-<version>.json) and fails when, for a method of a published version (or its
`_request`, which integrations call directly):
- the method has no recorded call;
- a parameter is never passed, by name or by position;
- a parameter is only ever passed with its default value, or only as None when it has none;
- **kwargs never receives a keyword the signature does not name, or *args an extra argument.
Fix it in scripts/build_compat_fixtures.py (ARGUMENTS, EXTRA_CASES or INTERNAL_CASES), then
regenerate the fixtures.

The replays build their client themselves (token and transport): the constructor arguments are
exercised by test_constructor_arguments_still_apply instead, and every constructor argument of
a published version must have a check there.
"""

import asyncio
import json
import pathlib
import unittest

import httpx

from leakradar.client import LeakRadarClient

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
POSITIONAL = ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
NAMED = POSITIONAL + ("KEYWORD_ONLY",)

# Constructor argument -> what test_constructor_arguments_still_apply checks for it.
CONSTRUCTOR_CHECKS = {
    "token": "sent as Authorization: Bearer <token>",
    "user_agent": "sent as the User-Agent",
    "timeout": "the timeout of the request",
    "transport": "carries the API requests",
}


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def load(kind):
    """The fixtures of one kind ("surface" or "requests"), by version."""
    return {
        path.stem.split("-", 1)[1]: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURES.glob(kind + "-*.json")
    }


def passed_values(param, cases):
    """The values the recorded calls give to one named parameter."""
    values = []
    for case in cases:
        if param["name"] in case["kwargs"]:
            values.append(case["kwargs"][param["name"]])
        elif param["position"] is not None and len(case["args"]) > param["position"]:
            values.append(case["args"][param["position"]])
    return values


def coverage_gaps(params, cases):
    """What the recorded calls leave unexercised in one published signature (fixture form)."""
    named = {p["name"] for p in params if p["kind"] in NAMED}
    positional = sum(1 for p in params if p["kind"] in POSITIONAL)
    gaps = []
    for param in params:
        name, default = param["name"], param["default"]
        if param["kind"] == "VAR_KEYWORD":
            if not any(set(case["kwargs"]) - named for case in cases):
                gaps.append(f"**{name} never receives a keyword the signature does not name")
        elif param["kind"] == "VAR_POSITIONAL":
            if not any(len(case["args"]) > positional for case in cases):
                gaps.append(f"*{name} never receives an extra positional argument")
        else:
            values = passed_values(param, cases)
            if not values:
                gaps.append(f"`{name}` is never passed")
            elif default is not None and all(repr(value) == default for value in values):
                gaps.append(f"`{name}` is only passed with its default {default}")
            elif default is None and all(value is None for value in values):
                gaps.append(f"`{name}` has no default and is only passed as None")
    return gaps


class ArgumentCoverageTest(unittest.TestCase):
    surfaces = load("surface")
    requests = load("requests")

    def test_every_version_has_both_fixtures(self):
        self.assertTrue(self.surfaces)
        self.assertEqual(sorted(self.surfaces), sorted(self.requests))

    def test_every_published_argument_is_passed(self):
        for version in sorted(self.surfaces, key=version_key):
            recorded = self.requests[version]["methods"]
            for name, method in self.surfaces[version]["client"]["methods"].items():
                with self.subTest(version=version, method=name):
                    cases = recorded.get(name, [])
                    gaps = coverage_gaps(method["params"], cases) if cases else ["no recorded call"]
                    if gaps:
                        self.fail(
                            f"{version} {name}: " + "; ".join(gaps) + ". Add a call to ARGUMENTS, EXTRA_CASES "
                            "or INTERNAL_CASES in scripts/build_compat_fixtures.py and regenerate the fixtures."
                        )

    def test_the_gaps_are_found(self):
        # The check itself: each kind of gap is reported, and a call that fills it clears it.
        params = [
            {"name": "query", "kind": "POSITIONAL_OR_KEYWORD", "default": "None", "position": 0},
            {"name": "page", "kind": "POSITIONAL_OR_KEYWORD", "default": "1", "position": 1},
            {"name": "list_id", "kind": "POSITIONAL_OR_KEYWORD", "default": None, "position": 2},
            {"name": "scope", "kind": "KEYWORD_ONLY", "default": "'email'", "position": None},
            {"name": "filters", "kind": "VAR_KEYWORD", "default": None, "position": None},
        ]
        cases = [{"args": [], "kwargs": {"page": 1, "list_id": None, "scope": "email"}}]
        self.assertEqual(
            coverage_gaps(params, cases),
            [
                "`query` is never passed",
                "`page` is only passed with its default 1",
                "`list_id` has no default and is only passed as None",
                "`scope` is only passed with its default 'email'",
                "**filters never receives a keyword the signature does not name",
            ],
        )
        cases.append({"args": ["admin", 2, 5], "kwargs": {"scope": "domain", "url_domain": "example.com"}})
        self.assertEqual(coverage_gaps(params, cases), [])


class ConstructorArgumentsTest(unittest.TestCase):
    def test_every_published_constructor_argument_is_checked(self):
        for version, surface in sorted(load("surface").items()):
            for param in surface["client"]["init"]:
                with self.subTest(version=version, argument=param["name"]):
                    self.assertIn(
                        param["name"],
                        CONSTRUCTOR_CHECKS,
                        "a published constructor argument without a check in test_constructor_arguments_still_apply",
                    )

    def test_constructor_arguments_still_apply(self):
        # Positional order kept by every published version (transport since 0.2.0), then keywords.
        sent = []

        def handle(request):
            sent.append(request)
            return httpx.Response(200, json={})

        async def get_profile(client):
            try:
                await client.get_profile()
            finally:
                await client.aclose()

        transport = httpx.MockTransport(handle)
        builds = {
            "positional": lambda: LeakRadarClient("lr_ctor_token", "acme-soc/2.1", 12.5, transport),
            "keywords": lambda: LeakRadarClient(
                token="lr_ctor_token", user_agent="acme-soc/2.1", timeout=12.5, transport=transport
            ),
        }
        for form, build in builds.items():
            with self.subTest(form=form):
                del sent[:]
                asyncio.run(get_profile(build()))
                self.assertEqual(len(sent), 1, "the transport carries the request")
                request = sent[0]
                self.assertEqual(request.headers.get("authorization"), "Bearer lr_ctor_token")
                self.assertEqual(request.headers.get("user-agent"), "acme-soc/2.1")
                self.assertEqual(request.extensions.get("timeout"), httpx.Timeout(12.5).as_dict())


if __name__ == "__main__":
    unittest.main()
