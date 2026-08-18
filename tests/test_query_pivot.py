from __future__ import annotations

import httpx
import respx

from firestop.hydra.client import HydraClient
from firestop.query.pivot import pivot
from firestop.query.typosquat import _edit_distance, scan
from tests.conftest import QUERY_URL, ndjson, page, value


def rows(columns: list[str], *records: list) -> dict:
    return page(
        columns,
        [[value(kind, item) for kind, item in record] for record in records],
    )


class TestMaintainerPivot:
    @respx.mock
    async def test_everything_the_same_account_can_publish_is_listed(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(200, json=rows(["username"], [("string", "dominictarr")])),
                httpx.Response(
                    200,
                    json=rows(
                        ["package", "dependents", "username"],
                        [("string", "event-stream"), ("integer", 400), ("string", "dominictarr")],
                        [("string", "through"), ("integer", 900), ("string", "dominictarr")],
                        [("string", "split"), ("integer", 120), ("string", "dominictarr")],
                    ),
                ),
            ]
        )

        found = await pivot(client, "event-stream")

        assert found.maintainers == ("dominictarr",)
        # The package asked about is not its own sibling.
        assert [sibling.package for sibling in found.siblings] == ["through", "split"]

    @respx.mock
    async def test_siblings_are_ordered_by_how_much_depends_on_them(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(200, json=rows(["username"], [("string", "someone")])),
                httpx.Response(
                    200,
                    json=rows(
                        ["package", "dependents", "username"],
                        [("string", "small"), ("integer", 2), ("string", "someone")],
                        [("string", "huge"), ("integer", 5000), ("string", "someone")],
                    ),
                ),
            ]
        )

        found = await pivot(client, "seed")

        assert [sibling.package for sibling in found.siblings] == ["huge", "small"]
        assert found.dependents == 5002

    @respx.mock
    async def test_a_package_with_no_recorded_maintainer_pivots_nowhere(self, client: HydraClient):
        route = respx.post(QUERY_URL).respond(json=rows(["username"]))

        found = await pivot(client, "orphan")

        assert found.siblings == []
        # No point asking the second question when the first returned nothing.
        assert len(route.calls) == 1

    @respx.mock
    async def test_two_accounts_on_one_package_are_both_reported(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=rows(["username"], [("string", "alice")], [("string", "bob")]),
                ),
                httpx.Response(
                    200,
                    json=rows(
                        ["package", "dependents", "username"],
                        [("string", "shared"), ("integer", 10), ("string", "alice")],
                        [("string", "shared"), ("integer", 10), ("string", "bob")],
                    ),
                ),
            ]
        )

        found = await pivot(client, "seed")

        assert found.siblings[0].shared == 2


class TestTyposquatRadar:
    def packages(self, *records) -> dict:
        return rows(["name", "dependents", "latest_at"], *records)

    def package(self, name: str, dependents: int) -> list:
        return [("string", name), ("integer", dependents), ("integer", 1_600_000_000)]

    def ownership(self, *pairs: tuple[str, str]) -> str:
        return ndjson(
            ["package", "username"],
            [[value("string", package), value("string", user)] for package, user in pairs],
        )

    @respx.mock
    async def test_a_one_letter_variant_of_a_popular_package_is_flagged(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=self.packages(self.package("lodash", 900), self.package("lodahs", 0)),
                ),
                httpx.Response(200, text=self.ownership(("lodash", "jdalton"))),
            ]
        )

        radar = await scan(client)

        assert [suspect.name for suspect in radar.suspects] == ["lodahs"]
        assert radar.suspects[0].resembles == "lodash"

    @respx.mock
    async def test_a_sibling_by_the_same_maintainer_is_ranked_below_a_stranger(
        self, client: HydraClient
    ):
        # `react-dom` is not squatting on `react`, and shared ownership is what
        # says so.
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=self.packages(
                        self.package("react", 900),
                        self.package("reacts", 1),
                        self.package("raect", 0),
                    ),
                ),
                httpx.Response(
                    200,
                    text=self.ownership(("react", "gaearon"), ("reacts", "gaearon")),
                ),
            ]
        )

        radar = await scan(client)
        ordered = [suspect.name for suspect in radar.suspects]

        assert ordered.index("raect") < ordered.index("reacts")
        assert radar.suspects[ordered.index("reacts")].shares_maintainer is True

    @respx.mock
    async def test_a_popular_package_is_not_a_suspect(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=self.packages(self.package("lodash", 900), self.package("lodahs", 800)),
                ),
                httpx.Response(200, text=self.ownership()),
            ]
        )

        radar = await scan(client)

        assert radar.suspects == []

    @respx.mock
    async def test_a_punctuation_variant_is_caught(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=self.packages(
                        self.package("node-fetch", 900), self.package("nodefetch", 0)
                    ),
                ),
                httpx.Response(200, text=self.ownership()),
            ]
        )

        radar = await scan(client)

        assert radar.suspects[0].trick == "punctuation"

    @respx.mock
    async def test_a_scoped_copy_is_caught(self, client: HydraClient):
        respx.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=self.packages(
                        self.package("express", 900), self.package("@acme/express", 0)
                    ),
                ),
                httpx.Response(200, text=self.ownership()),
            ]
        )

        radar = await scan(client)

        assert radar.suspects[0].trick == "scoped copy"


class TestEditDistance:
    def test_a_transposition_counts_as_one_edit(self):
        # Because swapping two letters is the typo people actually make.
        assert _edit_distance("lodahs", "lodash", 2) == 1

    def test_a_substitution_counts_as_one(self):
        assert _edit_distance("lodesh", "lodash", 2) == 1

    def test_an_insertion_counts_as_one(self):
        assert _edit_distance("lodashh", "lodash", 2) == 1

    def test_identical_names_are_zero_apart(self):
        assert _edit_distance("lodash", "lodash", 2) == 0

    def test_a_distant_name_is_abandoned_at_the_ceiling(self):
        assert _edit_distance("underscore", "lodash", 1) > 1
