from __future__ import annotations

from firestop.ids import maintainer_id, package_id, release_id
from firestop.npm.packument import Dependency, Packument, Release
from firestop.npm.resolve import Resolver
from firestop.npm.writer import (
    collect_dependents,
    dependency_rows,
    dependent_count_rows,
    vertex_rows,
)
from firestop.schema.model import OPEN_INTERVAL_END, DependencyKind, Rel


def widget() -> Packument:
    return Packument(
        name="widget",
        releases=[
            Release(
                version="1.0.0",
                published_at=100,
                integrity="sha512-aaa",
                publisher="alice",
                dependencies=[Dependency("dep", "^1.0.0", DependencyKind.RUNTIME)],
            ),
            Release(version="1.1.0", published_at=500, publisher="bob"),
        ],
        maintainers=[("alice", "alice@example.com")],
        version_times={"1.0.0": 100, "1.1.0": 500},
    )


DEP_TIMELINE = {"dep": {"1.0.0": 50, "1.2.0": 300, "2.0.0": 400}}

# Releases that exist as vertices (edge endpoints must).
KNOWN = frozenset(
    {
        release_id("widget", "1.0.0"),
        release_id("widget", "1.1.0"),
        release_id("dep", "1.0.0"),
        release_id("dep", "1.2.0"),
        release_id("dep", "2.0.0"),
    }
)


def grouped_for(packument=None, timeline=None, known=None, resolver=None):
    return dependency_rows(
        packument or widget(),
        timeline if timeline is not None else DEP_TIMELINE,
        resolver or Resolver(),
        known if known is not None else KNOWN,
    )


def rows_for(packument=None, timeline=None, known=None, resolver=None):
    """The installable plane, which is what most of these assertions are about."""
    return grouped_for(packument, timeline, known, resolver)[Rel.DEPENDS_ON]


def dev_rows_for(packument=None, timeline=None, known=None, resolver=None):
    return grouped_for(packument, timeline, known, resolver)[Rel.DEV_DEPENDS_ON]


class TestVertexRows:
    def test_one_row_per_package(self):
        rows = vertex_rows(widget())

        assert len(rows.packages) == 1
        assert rows.packages[0]["id"] == package_id("widget")
        assert rows.packages[0]["name"] == "widget"
        assert rows.packages[0]["ecosystem"] == "npm"

    def test_first_published_is_carried_onto_the_package(self):
        assert vertex_rows(widget()).packages[0]["first_published"] == 100

    def test_dependent_count_starts_at_zero(self):
        # Not knowable until the dependency edges exist.
        assert vertex_rows(widget()).packages[0]["dependent_count"] == 0

    def test_one_row_per_release(self):
        rows = vertex_rows(widget())

        assert [row["version"] for row in rows.releases] == ["1.0.0", "1.1.0"]
        assert rows.releases[0]["id"] == release_id("widget", "1.0.0")

    def test_releases_carry_the_string_key_selectors_need(self):
        # The path procedures match endpoints on a string property, so a release
        # is unreachable as a traversal target without this.
        assert vertex_rows(widget()).releases[0]["key"] == "widget@1.0.0"

    def test_every_release_belongs_to_its_package(self):
        rows = vertex_rows(widget())

        assert len(rows.version_of) == 2
        assert all(row["target"] == package_id("widget") for row in rows.version_of)

    def test_maintainers_come_from_both_the_owner_list_and_publishers(self):
        rows = vertex_rows(widget())

        assert {row["username"] for row in rows.maintainers} == {"alice", "bob"}

    def test_only_listed_maintainers_get_publish_rights(self):
        # bob published a version but is no longer an owner, which is exactly the
        # distinction the identity plane exists to keep.
        rows = vertex_rows(widget())

        assert [row["target"] for row in rows.can_publish] == [package_id("widget")]
        assert rows.can_publish[0]["source"] == maintainer_id("alice")

    def test_published_edges_name_who_pushed_each_version(self):
        rows = vertex_rows(widget())
        by_source = {row["source"]: row for row in rows.published}

        assert by_source[maintainer_id("alice")]["target"] == release_id("widget", "1.0.0")
        assert by_source[maintainer_id("bob")]["target"] == release_id("widget", "1.1.0")

    def test_published_edges_carry_the_moment(self):
        rows = vertex_rows(widget())

        assert {row["at"] for row in rows.published} == {100, 500}

    def test_a_release_without_a_publisher_produces_no_published_edge(self):
        packument = Packument(
            name="widget",
            releases=[Release(version="1.0.0", published_at=100)],
            version_times={"1.0.0": 100},
        )

        assert vertex_rows(packument).published == []

    def test_rows_can_be_merged(self):
        rows = vertex_rows(widget())
        before = len(rows)
        rows.extend(vertex_rows(widget()))

        assert len(rows) == before * 2


class TestDependencyRows:
    def test_an_edge_is_written_per_resolution_window(self):
        # ^1.0.0 resolved to dep@1.0.0 then dep@1.2.0; 2.0.0 is out of range.
        assert [row["resolved_to"] for row in rows_for()] == ["1.0.0", "1.2.0"]

    def test_edges_point_from_dependent_release_to_dependency_release(self):
        rows = rows_for()

        assert rows[0]["source"] == release_id("widget", "1.0.0")
        assert rows[0]["target"] == release_id("dep", "1.0.0")

    def test_the_declared_range_is_kept_alongside_the_resolution(self):
        rows = rows_for()

        assert rows[0]["range"] == "^1.0.0"
        assert rows[0]["kind"] == "runtime"

    def test_validity_windows_are_written_and_the_last_stays_open(self):
        rows = rows_for()

        assert rows[0]["valid_from"] == 100
        assert rows[0]["valid_to"] == 300
        assert rows[1]["valid_to"] == OPEN_INTERVAL_END

    def test_a_window_starts_no_earlier_than_the_dependent(self):
        # dep@1.0.0 was published at 50, before widget@1.0.0 existed at 100.
        assert rows_for()[0]["valid_from"] == 100

    def test_each_pair_gets_exactly_one_edge(self):
        pairs = [(row["source"], row["target"]) for row in rows_for()]

        # No parallel edges, which is what lets the temporal model use plain
        # relationships instead of needing many between the same two vertices.
        assert len(pairs) == len(set(pairs))

    def test_edge_ids_are_deterministic(self):
        assert [row["id"] for row in rows_for()] == [row["id"] for row in rows_for()]

    def test_an_uncrawled_dependency_produces_no_edge(self):
        assert rows_for(timeline={}) == []

    def test_a_non_semver_spec_produces_no_edge(self):
        packument = Packument(
            name="widget",
            releases=[
                Release(
                    version="1.0.0",
                    published_at=100,
                    dependencies=[Dependency("dep", "workspace:*", DependencyKind.RUNTIME)],
                )
            ],
            version_times={"1.0.0": 100},
        )

        assert rows_for(packument) == []


class TestDevHorizon:
    # Transitive DEV edges are not installed.

    def mixed(self) -> Packument:
        return Packument(
            name="widget",
            releases=[
                Release(
                    version="1.0.0",
                    published_at=100,
                    dependencies=[
                        Dependency("dep", "^1.0.0", DependencyKind.RUNTIME),
                        Dependency("dep", "^1.0.0", DependencyKind.DEV),
                    ],
                )
            ],
            version_times={"1.0.0": 100},
        )

    def rows(self, *, with_dev: bool):
        return dependency_rows(self.mixed(), DEP_TIMELINE, Resolver(), KNOWN, with_dev=with_dev)

    def test_dev_edges_can_be_left_out_entirely(self):
        assert self.rows(with_dev=False)[Rel.DEV_DEPENDS_ON] == []

    def test_leaving_dev_out_does_not_touch_installable_edges(self):
        kept = self.rows(with_dev=True)[Rel.DEPENDS_ON]
        without = self.rows(with_dev=False)[Rel.DEPENDS_ON]

        assert kept and [row["id"] for row in kept] == [row["id"] for row in without]


class TestDevSeparation:
    # DEV edges are a separate rel type so traversals can omit them.

    def declared(self, *kinds: DependencyKind) -> Packument:
        return Packument(
            name="widget",
            releases=[
                Release(
                    version="1.0.0",
                    published_at=100,
                    dependencies=[Dependency("dep", "^1.0.0", kind) for kind in kinds],
                )
            ],
            version_times={"1.0.0": 100},
        )

    def test_a_dev_dependency_lands_on_the_build_time_plane(self):
        packument = self.declared(DependencyKind.DEV)

        assert rows_for(packument) == []
        assert {row["kind"] for row in dev_rows_for(packument)} == {"dev"}

    def test_runtime_peer_and_optional_all_stay_installable(self):
        for kind in (DependencyKind.RUNTIME, DependencyKind.PEER, DependencyKind.OPTIONAL):
            packument = self.declared(kind)

            assert {row["kind"] for row in rows_for(packument)} == {str(kind)}
            assert dev_rows_for(packument) == []

    def test_a_dependency_declared_both_ways_appears_on_both_planes(self):
        # Common enough: a typegen tool that is also imported at runtime. Each
        # plane carries its own relationship, so neither answer is lost.
        grouped = grouped_for(self.declared(DependencyKind.RUNTIME, DependencyKind.DEV))

        assert {row["kind"] for row in grouped[Rel.DEPENDS_ON]} == {"runtime"}
        assert {row["kind"] for row in grouped[Rel.DEV_DEPENDS_ON]} == {"dev"}

    def test_the_two_planes_do_not_share_relationship_ids(self):
        grouped = grouped_for(self.declared(DependencyKind.RUNTIME, DependencyKind.DEV))
        installable = {row["id"] for row in grouped[Rel.DEPENDS_ON]}
        build_time = {row["id"] for row in grouped[Rel.DEV_DEPENDS_ON]}

        assert installable and build_time
        assert not installable & build_time


class TestOneEdgePerPair:
    # Same (type, source, target) must collapse to one row.

    def declared_twice(self, first: DependencyKind, second: DependencyKind) -> Packument:
        return Packument(
            name="widget",
            releases=[
                Release(
                    version="1.0.0",
                    published_at=100,
                    dependencies=[
                        Dependency("dep", "^1.0.0", first),
                        Dependency("dep", ">=1.0.0 <2", second),
                    ],
                )
            ],
            version_times={"1.0.0": 100},
        )

    def test_two_declarations_resolving_alike_produce_one_edge_per_window(self):
        rows = rows_for(self.declared_twice(DependencyKind.RUNTIME, DependencyKind.PEER))
        ids = [row["id"] for row in rows]

        assert len(ids) == len(set(ids))

    def test_the_strongest_declaration_wins(self):
        rows = rows_for(self.declared_twice(DependencyKind.PEER, DependencyKind.RUNTIME))

        assert {row["kind"] for row in rows} == {"runtime"}

    def test_precedence_does_not_depend_on_declaration_order(self):
        forward = rows_for(self.declared_twice(DependencyKind.RUNTIME, DependencyKind.PEER))
        backward = rows_for(self.declared_twice(DependencyKind.PEER, DependencyKind.RUNTIME))

        assert {r["kind"] for r in forward} == {r["kind"] for r in backward} == {"runtime"}

    def test_peer_beats_optional(self):
        rows = rows_for(self.declared_twice(DependencyKind.OPTIONAL, DependencyKind.PEER))

        assert {row["kind"] for row in rows} == {"peer"}


class TestDanglingEndpoints:
    # Unwritten targets are expected (version cap).

    def test_a_resolution_to_an_unwritten_release_is_dropped(self):
        known = KNOWN - {release_id("dep", "1.0.0")}

        assert [row["resolved_to"] for row in rows_for(known=known)] == ["1.2.0"]

    def test_dropping_is_counted_rather_than_silent(self):
        resolver = Resolver()
        rows_for(known=KNOWN - {release_id("dep", "1.0.0")}, resolver=resolver)

        assert resolver.unwritten_targets == 1

    def test_an_unwritten_dependent_release_is_skipped_entirely(self):
        known = KNOWN - {release_id("widget", "1.0.0")}

        assert rows_for(known=known) == []

    def test_nothing_known_means_nothing_written(self):
        assert rows_for(known=frozenset()) == []


class TestDependentCounts:
    def setup_method(self):
        self.owner = {
            release_id("a", "1.0.0"): "a",
            release_id("a", "1.1.0"): "a",
            release_id("b", "1.0.0"): "b",
            release_id("dep", "1.0.0"): "dep",
        }

    def edge(self, source, target):
        return {"source": source, "target": target}

    def test_dependents_are_counted_per_package_not_per_release(self):
        edges = [
            self.edge(release_id("a", "1.0.0"), release_id("dep", "1.0.0")),
            self.edge(release_id("a", "1.1.0"), release_id("dep", "1.0.0")),
        ]
        dependents: dict[str, set[str]] = {}

        collect_dependents(edges, self.owner, dependents)

        assert dependents == {"dep": {"a"}}

    def test_distinct_dependents_accumulate(self):
        edges = [
            self.edge(release_id("a", "1.0.0"), release_id("dep", "1.0.0")),
            self.edge(release_id("b", "1.0.0"), release_id("dep", "1.0.0")),
        ]
        dependents: dict[str, set[str]] = {}

        collect_dependents(edges, self.owner, dependents)

        assert dependents["dep"] == {"a", "b"}

    def test_a_dependent_split_across_batches_is_not_counted_twice(self):
        dependents: dict[str, set[str]] = {}

        collect_dependents(
            [self.edge(release_id("a", "1.0.0"), release_id("dep", "1.0.0"))],
            self.owner,
            dependents,
        )
        collect_dependents(
            [self.edge(release_id("a", "1.1.0"), release_id("dep", "1.0.0"))],
            self.owner,
            dependents,
        )

        assert dependent_count_rows(dependents) == [{"id": package_id("dep"), "dependent_count": 1}]

    def test_a_package_depending_on_itself_is_not_a_dependent(self):
        dependents: dict[str, set[str]] = {}

        collect_dependents(
            [self.edge(release_id("a", "1.1.0"), release_id("a", "1.0.0"))],
            self.owner,
            dependents,
        )

        assert dependents == {}

    def test_unknown_endpoints_are_ignored(self):
        dependents: dict[str, set[str]] = {}

        collect_dependents([self.edge(1, 2)], self.owner, dependents)

        assert dependents == {}

    def test_count_rows_only_touch_the_one_property(self):
        rows = dependent_count_rows({"dep": {"a", "b"}})

        assert set(rows[0]) == {"id", "dependent_count"}
