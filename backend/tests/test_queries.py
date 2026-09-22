"""Tests for the analytics database and its query layer.

These run against a temporary database built from synthetic matches, so
they neither touch nor depend on the real crawled dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analytics_db import (
    FLAG_FIRST_BLOOD,
    FLAG_TRADED,
    AnalyticsDB,
    _act_sort_key,
    _avg_tier,
    _patch_of,
)
from app.analytics.kills import enrich
from app.models import (
    DamageType, Kill, Match, MatchMeta, Plant, Player, Point, Round, Side,
)
from app.queries import Filters, QueryEngine
from app.reference import get_map


def _world_at(map_name: str, mx: float, my: float) -> Point:
    """World coordinates that land at a given minimap position.

    Inverts the calibration rather than hardcoding numbers, so the fixtures
    stay inside the map however the reference data changes.
    """
    info = get_map(map_name)
    assert info is not None
    return Point(
        (my - info.y_scalar) / info.y_multiplier,
        (mx - info.x_scalar) / info.x_multiplier,
    )


def _match(
    match_id: str,
    map_name: str = "Ascent",
    act: str = "e11a5",
    tier: int = 25,
    kills: int = 4,
) -> Match:
    meta = MatchMeta(
        match_id=match_id,
        map_id="/Game/Maps/Ascent/Ascent",
        map_name=map_name,
        mode="standard",
        mode_raw="Bomb",
        queue="competitive",
        started_at=1_700_000_000_000,
        game_length_ms=1_800_000,
        game_version="release-13.05-shipping-11-5350494",
        region="na",
        act=act,
    )
    players = [
        Player(puuid="atk", name="Atk", tag="1", team="Red", agent="Jett", agent_id="", tier=tier),
        Player(puuid="def", name="Def", tag="2", team="Blue", agent="Sage", agent_id="", tier=tier),
    ]
    round_kills = []
    for i in range(kills):
        round_kills.append(
            Kill(
                round_num=0,
                time_in_round_ms=10_000 + i * 5_000,
                time_in_match_ms=10_000 + i * 5_000,
                killer_puuid="atk" if i % 2 == 0 else "def",
                victim_puuid="def" if i % 2 == 0 else "atk",
                # Spread the points across distinct map cells, well inside
                # the calibrated bounds.
                victim_location=_world_at(map_name, 0.3 + i * 0.1, 0.35 + i * 0.08),
                killer_location=_world_at(map_name, 0.35 + i * 0.1, 0.4 + i * 0.08),
                damage_type=DamageType.ABILITY if i == 0 else DamageType.WEAPON,
                weapon_name="" if i == 0 else "Vandal",
                ability_name="Blade Storm" if i == 0 else "",
                killer_side=Side.ATTACK if i % 2 == 0 else Side.DEFENSE,
                killer_team="Red" if i % 2 == 0 else "Blue",
                victim_team="Blue" if i % 2 == 0 else "Red",
            )
        )
    plant = Plant(
        round_num=0, round_time_ms=40_000, site="A",
        location=_world_at(map_name, 0.42, 0.55), planter_puuid="atk", planter_team="Red",
        won=True, defused=False,
    )
    rnd = Round(
        number=0, winning_team="Red", result="Detonate",
        plant=plant, kills=round_kills,
        team_sides={"Red": Side.ATTACK, "Blue": Side.DEFENSE},
    )
    return Match(meta=meta, players=players, rounds=[rnd], teams={"Red": True, "Blue": False})


@pytest.fixture()
def db(tmp_path: Path) -> AnalyticsDB:
    database = AnalyticsDB(tmp_path / "a.db")
    info = get_map("Ascent")
    for i in range(6):
        m = _match(f"m{i}", tier=27 if i < 2 else 22, act="e11a5" if i < 4 else "e11a4")
        database.add_match(m, info, enrich(m))
    return database


@pytest.fixture()
def engine(db: AnalyticsDB) -> QueryEngine:
    return QueryEngine(db)


# --- derivation ---------------------------------------------------------
def test_patch_parsed_from_game_version():
    assert _patch_of("release-13.05-shipping-11-5350494") == "13.05"
    assert _patch_of("") == ""


def test_avg_tier_is_mean_of_players():
    m = _match("x", tier=24)
    assert _avg_tier(m) == 24


def test_acts_sort_by_episode_then_act():
    """String sort puts e9a3 after e11a5; these must order numerically."""
    acts = ["e9a3", "e11a5", "e10a1", "e11a2"]
    assert sorted(acts, key=_act_sort_key, reverse=True) == [
        "e11a5", "e11a2", "e10a1", "e9a3",
    ]


# --- storage ------------------------------------------------------------
def test_stores_matches_kills_and_plants(db: AnalyticsDB):
    stats = db.stats()
    assert stats["matches"] == 6
    assert stats["kills"] == 24  # 6 matches x 4 kills
    assert stats["plants"] == 6


def test_reingesting_replaces_rather_than_duplicates(db: AnalyticsDB):
    before = db.stats()["kills"]
    m = _match("m0")
    db.add_match(m, get_map("Ascent"), enrich(m))
    assert db.stats()["kills"] == before


def test_names_are_interned_to_small_ids(db: AnalyticsDB):
    """Text repeated per row is what made the first schema 2.7x larger."""
    maps = db.dim_ids("map")
    agents = db.dim_ids("agent")
    assert maps["Ascent"] == 1
    assert set(agents) == {"Jett", "Sage"}
    assert all(isinstance(v, int) for v in agents.values())


def test_positions_stored_as_scaled_integers(db: AnalyticsDB):
    """Positions are integers in [0, POS_SCALE], not floats.

    A REAL costs 8 bytes and a scaled int 2, which across millions of rows
    decides whether the published database fits in a serverless /tmp.
    """
    from app.analytics_db import POS_SCALE

    with db.connect() as conn:
        rows = conn.execute("SELECT vx, vy FROM kills").fetchall()
    assert rows
    for r in rows:
        assert isinstance(r["vx"], int)
        assert 0 <= r["vx"] <= POS_SCALE
        assert 0 <= r["vy"] <= POS_SCALE


def test_flags_pack_kill_context(db: AnalyticsDB):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT flags FROM kills WHERE t_ms = 10000 LIMIT 1"
        ).fetchone()
    assert row["flags"] & FLAG_FIRST_BLOOD


# --- filtering ----------------------------------------------------------
def test_map_filter_is_required_to_narrow(engine: QueryEngine):
    all_kills = engine.summary(Filters(map_name="Ascent"))["total"]
    assert all_kills == 24
    assert engine.summary(Filters(map_name="Nonexistent"))["total"] == 0


def test_rank_band_filter(engine: QueryEngine):
    """Radiant band is tier 27; only the first two matches qualify."""
    radiant = engine.summary(Filters(map_name="Ascent", ranks=["radiant"]))
    assert radiant["total"] == 8
    assert radiant["matches"] == 2


def test_act_filter(engine: QueryEngine):
    current = engine.summary(Filters(map_name="Ascent", acts=["e11a5"]))
    assert current["matches"] == 4


def test_agent_filter_uses_killer(engine: QueryEngine):
    jett = engine.summary(Filters(map_name="Ascent", agents=["Jett"]))
    # Jett kills on even indices: 2 of every 4.
    assert jett["total"] == 12


def test_side_filter(engine: QueryEngine):
    atk = engine.summary(Filters(map_name="Ascent", sides=["attack"]))
    dfn = engine.summary(Filters(map_name="Ascent", sides=["defense"]))
    assert atk["total"] + dfn["total"] == 24


def test_time_window_filter(engine: QueryEngine):
    early = engine.kill_points(Filters(map_name="Ascent", time_end=12_000))
    assert early["total"] == 6  # only the first kill of each match
    assert all(p["t"] <= 12_000 for p in early["points"])


def test_utility_filter_matches_ability_kills(engine: QueryEngine):
    util = engine.summary(Filters(map_name="Ascent", utility_only=True))
    assert util["total"] == 6  # one ability kill per match
    rows = engine.ability_breakdown(Filters(map_name="Ascent"))
    assert rows[0]["ability"] == "Blade Storm"
    assert rows[0]["agent"] == "Jett"


def test_filters_compose(engine: QueryEngine):
    combined = engine.summary(
        Filters(map_name="Ascent", ranks=["radiant"], acts=["e11a5"], agents=["Jett"])
    )
    assert 0 < combined["total"] <= 8


def test_unknown_map_short_circuits(engine: QueryEngine):
    """An unknown map must not fall through to a full table scan."""
    assert engine.kill_points(Filters(map_name="Nope"))["points"] == []


# --- query output -------------------------------------------------------
def test_kill_points_resolve_names_and_positions(engine: QueryEngine):
    result = engine.kill_points(Filters(map_name="Ascent"))
    p = result["points"][0]
    assert p["killer_agent"] in {"Jett", "Sage"}
    assert p["side"] in {"attack", "defense"}
    assert 0 <= p["victim_pos"]["x"] <= 1
    assert p["killer_pos"] is not None


def test_large_selections_are_sampled_deterministically(engine: QueryEngine):
    """Above the limit the API returns a stable uniform subset."""
    f = Filters(map_name="Ascent", limit=5)
    first = engine.kill_points(f)
    second = engine.kill_points(f)
    assert first["total"] == 24
    assert first["sampled"] is True
    assert len(first["points"]) <= 24
    assert [p["t"] for p in first["points"]] == [p["t"] for p in second["points"]]


def test_histogram_buckets_by_time(engine: QueryEngine):
    hist = engine.histogram(Filters(map_name="Ascent"), bucket_ms=5000)
    assert hist
    assert sum(h["count"] for h in hist) == 24
    assert all(h["t"] % 5000 == 0 for h in hist)


def test_plants_query_returns_positions_and_outcome(engine: QueryEngine):
    plants = engine.plants(Filters(map_name="Ascent"))
    assert len(plants) == 6
    assert all(p["site"] == "A" for p in plants)
    assert all(0 <= p["position"]["x"] <= 1 for p in plants)
    assert all(p["won"] for p in plants)


def test_facets_list_available_filter_values(db: AnalyticsDB):
    facets = db.facets()
    assert facets["maps"][0]["map_name"] == "Ascent"
    assert [a["act"] for a in facets["acts"]] == ["e11a5", "e11a4"]
    assert {a["agent"] for a in facets["agents"]} == {"Jett", "Sage"}
    assert facets["tier_range"] == [22, 27]


def test_summary_rates_are_fractions(engine: QueryEngine):
    s = engine.summary(Filters(map_name="Ascent"))
    assert 0.0 <= s["trade_rate"] <= 1.0
    assert 0.0 <= s["utility_rate"] <= 1.0


def test_engine_cache_can_be_invalidated(db: AnalyticsDB):
    """A newly seen agent must appear after an ingest, not stay cached."""
    engine = QueryEngine(db)
    assert engine.summary(Filters(map_name="Ascent", agents=["Omen"]))["total"] == 0

    # Build a match whose killer really is Omen, so enrich() records it.
    m = _match("new")
    m.players[0].agent = "Omen"
    db.add_match(m, get_map("Ascent"), enrich(m))

    engine.invalidate()
    assert engine.summary(Filters(map_name="Ascent", agents=["Omen"]))["total"] > 0


def test_unresolvable_filter_matches_nothing_not_everything(engine: QueryEngine):
    """A filter naming only unknown values must return no rows.

    Dropping such a filter silently returns the entire dataset, which looks
    to a user exactly like the filter being ignored.
    """
    assert engine.summary(Filters(map_name="Ascent", agents=["Nobody"]))["total"] == 0
    assert engine.summary(Filters(map_name="Ascent", acts=["e1a1"]))["total"] == 0
    assert engine.summary(Filters(map_name="Ascent", weapons=["Trombone"]))["total"] == 0
    # A mix of known and unknown keeps the known part.
    mixed = engine.summary(Filters(map_name="Ascent", agents=["Jett", "Nobody"]))
    assert mixed["total"] > 0


# --- ability filter -----------------------------------------------------
def test_ability_filter_narrows_to_one_ability(engine: QueryEngine):
    all_util = engine.summary(Filters(map_name="Ascent", utility_only=True))["total"]
    blade = engine.summary(Filters(map_name="Ascent", abilities=["Blade Storm"]))["total"]
    assert blade > 0
    assert blade == all_util  # the fixture's only ability kill


def test_unknown_ability_matches_nothing(engine: QueryEngine):
    assert engine.summary(Filters(map_name="Ascent", abilities=["Nope"]))["total"] == 0


def test_abilities_appear_in_facets(db: AnalyticsDB):
    facets = db.facets()
    assert facets["abilities"]
    row = facets["abilities"][0]
    assert row["ability"] == "Blade Storm"
    assert row["agent"] == "Jett"
    assert row["kills"] > 0


# --- zone cross-filter --------------------------------------------------
def test_zone_parses_and_normalises_any_drag_direction():
    """A box drawn bottom-right to top-left must work the same way."""
    forward = Filters.from_query({"zone": "0.2,0.3,0.6,0.7"})
    backward = Filters.from_query({"zone": "0.6,0.7,0.2,0.3"})
    assert forward.zone == (0.2, 0.3, 0.6, 0.7)
    assert backward.zone == forward.zone


def test_zero_area_zone_is_ignored():
    """A click without a drag should not select an empty sliver."""
    assert Filters.from_query({"zone": "0.5,0.5,0.5,0.5"}).zone is None
    assert Filters.from_query({"zone": "garbage"}).zone is None
    assert Filters.from_query({"zone": "1,2,3"}).zone is None


def test_zone_constrains_the_anchor_end_only(engine: QueryEngine, db: AnalyticsDB):
    """The box holds one end of the duel; the other is free to be anywhere.

    That is what makes this a cross-filter rather than a crop: selecting
    where killers stood shows where their victims fell, which may be well
    outside the box.
    """
    from app.analytics_db import from_pos

    with db.connect() as conn:
        row = conn.execute("SELECT vx, vy, kx, ky FROM kills LIMIT 1").fetchone()
    vx, vy = from_pos(row["vx"]), from_pos(row["vy"])
    pad = 0.01
    around_victim = (vx - pad, vy - pad, vx + pad, vy + pad)

    victim_side = engine.summary(
        Filters(map_name="Ascent", zone=around_victim, zone_anchor="victim")
    )["total"]
    killer_side = engine.summary(
        Filters(map_name="Ascent", zone=around_victim, zone_anchor="killer")
    )["total"]

    assert victim_side >= 1, "the kill whose victim defined the box must match"
    # The same tiny box around a death position should not also contain the
    # killer, so the two anchors select different sets.
    assert killer_side != victim_side


def test_zone_returns_points_outside_the_box(engine: QueryEngine, db: AnalyticsDB):
    from app.analytics_db import from_pos

    with db.connect() as conn:
        row = conn.execute(
            "SELECT kx, ky FROM kills WHERE kx IS NOT NULL LIMIT 1"
        ).fetchone()
    kx, ky = from_pos(row["kx"]), from_pos(row["ky"])
    pad = 0.02
    zone = (kx - pad, ky - pad, kx + pad, ky + pad)

    result = engine.kill_points(
        Filters(map_name="Ascent", zone=zone, zone_anchor="killer")
    )
    assert result["points"], "expected kills by someone inside the box"
    # Plotted positions are victims, which need not be inside the box.
    for point in result["points"]:
        assert 0.0 <= point["victim_pos"]["x"] <= 1.0


# --- weapon filter ------------------------------------------------------
def test_weapon_filter_narrows_the_selection(engine: QueryEngine):
    total = engine.summary(Filters(map_name="Ascent"))["total"]
    vandal = engine.summary(Filters(map_name="Ascent", weapons=["Vandal"]))["total"]
    assert 0 < vandal < total


def test_multiple_weapons_are_a_union(engine: QueryEngine, db: AnalyticsDB):
    """Selecting two weapons shows kills by either, not by both."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO dim (kind, id, name) VALUES ('weapon', 99, 'Operator')"
        )
        conn.execute("UPDATE kills SET weapon_id = 99 WHERE rowid = (SELECT MIN(rowid) FROM kills)")
    engine.invalidate()

    vandal = engine.summary(Filters(map_name="Ascent", weapons=["Vandal"]))["total"]
    operator = engine.summary(Filters(map_name="Ascent", weapons=["Operator"]))["total"]
    both = engine.summary(Filters(map_name="Ascent", weapons=["Vandal", "Operator"]))["total"]
    assert operator > 0
    assert both == vandal + operator


def test_unknown_weapon_matches_nothing(engine: QueryEngine):
    assert engine.summary(Filters(map_name="Ascent", weapons=["Trombone"]))["total"] == 0


def test_weapons_appear_in_facets(db: AnalyticsDB):
    facets = db.facets()
    assert facets["weapons"]
    assert facets["weapons"][0]["weapon"] == "Vandal"
    assert facets["weapons"][0]["kills"] > 0
    # Sorted by usage, so the UI can show the common ones first.
    counts = [w["kills"] for w in facets["weapons"]]
    assert counts == sorted(counts, reverse=True)


def test_crawler_refreshes_facets_before_the_reader_sees_them_as_stale():
    """The crawler must rebuild the cache before a request would.

    On a server both share one database file, so the cache goes stale as
    matches arrive. If the reader's tolerance were the smaller of the two,
    some unlucky request would recompute facets over every kill -- 45s at
    5M rows -- instead of the crawler absorbing it between batches.
    """
    import inspect

    from app.crawler import FACET_REFRESH_MATCHES

    tolerance = inspect.signature(
        AnalyticsDB._facets_are_stale
    ).parameters["tolerance"].default
    assert FACET_REFRESH_MATCHES < tolerance, (
        f"crawler rebuilds every {FACET_REFRESH_MATCHES} matches but the reader "
        f"calls the cache stale after {tolerance}; the crawler must go first"
    )


# --- player attribution -------------------------------------------------
def test_kills_record_who_played(db: AnalyticsDB):
    """Personal stats need identity, which agent ids alone cannot give.

    Two Jett players in one match are indistinguishable by agent, so the
    puuid is stored (interned) alongside it.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT killer_pid, victim_pid FROM kills WHERE killer_pid IS NOT NULL LIMIT 1"
        ).fetchone()
    assert row is not None, "kills should carry player attribution"
    assert row["killer_pid"] != row["victim_pid"]

    players = db.dim_ids("player")
    assert {"atk", "def"} <= set(players)


def test_player_ids_are_interned_not_repeated(db: AnalyticsDB):
    """A puuid is 36 bytes and appears twice per kill; ids are 4."""
    with db.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) n FROM dim WHERE kind='player'").fetchone()["n"]
    # Six matches, two players each, all the same two people.
    assert rows == 2


def test_migration_adds_columns_to_an_existing_database(tmp_path: Path):
    """A database built before attribution existed must gain it in place.

    `CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so
    without an explicit migration the new columns never appear -- the same
    trap that made the switch to integer positions silently do nothing.
    """
    import sqlite3

    from app.analytics_db import _migrate

    path = tmp_path / "old.db"
    # A database shaped like the pre-attribution schema.
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE kills (
            m INTEGER NOT NULL, map_id INTEGER NOT NULL, act_id INTEGER,
            avg_tier INTEGER, round_num INTEGER NOT NULL, t_ms INTEGER NOT NULL,
            side INTEGER NOT NULL, ka_id INTEGER, va_id INTEGER,
            weapon_id INTEGER, ability_id INTEGER, dmg_type INTEGER NOT NULL,
            vx INTEGER NOT NULL, vy INTEGER NOT NULL, kx INTEGER, ky INTEGER,
            flags INTEGER NOT NULL
        );
        INSERT INTO kills VALUES (1,1,1,25,0,1000,1,1,2,1,NULL,0,100,200,300,400,0);
        """
    )
    old.commit()
    old.close()

    applied = _migrate(sqlite3.connect(path))
    assert "kills.killer_pid" in applied and "kills.victim_pid" in applied

    # Opening it through AnalyticsDB must now succeed: SCHEMA builds
    # indexes over those columns and would fail if they were missing.
    opened = AnalyticsDB(path)
    with opened.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kills)")}
        assert {"killer_pid", "victim_pid"} <= cols
        # The existing row survives, with no attribution until backfill.
        row = conn.execute("SELECT COUNT(*) n FROM kills").fetchone()
        assert row["n"] == 1


def test_migration_is_idempotent(db: AnalyticsDB):
    """Every open runs it, so running it twice must be harmless."""
    from app.analytics_db import _migrate

    with db.connect() as conn:
        assert _migrate(conn) == []


def test_dim_id_survives_a_rolled_back_transaction(tmp_path: Path):
    """The name cache outlives the transaction that wrote the row.

    _dim_id caches name -> id in the process. If the transaction that
    inserted the row is rolled back, the cache still claims it exists,
    and the next insert of that name collided on the unique index. With
    hundreds of thousands of player puuids this stopped being theoretical.
    """
    database = AnalyticsDB(tmp_path / "d.db")

    # A batch that interns a name and then fails, as a bad payload would.
    with pytest.raises(RuntimeError):
        with database.connect() as conn:
            database._dim_id(conn, "player", "ghost-puuid")
            raise RuntimeError("payload blew up mid-batch")

    # The next batch interns the same name. Before the fix this raised
    # IntegrityError: the cache still held an id for the rolled-back row.
    with database.connect() as conn:
        again = database._dim_id(conn, "player", "ghost-puuid")
        assert again is not None

    # And the name resolves to exactly one committed row.
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) n FROM dim WHERE kind='player' AND name='ghost-puuid'"
        ).fetchone()["n"]
    assert rows == 1


# --- personal stats -----------------------------------------------------
def test_player_summary_counts_both_ends_of_the_duel(db: AnalyticsDB):
    engine = QueryEngine(db)
    summary = engine.player_summary("atk")
    # The fixture alternates: atk kills on even indices, dies on odd.
    assert summary["kills"] > 0
    assert summary["deaths"] > 0
    assert summary["tracked"] is True
    assert summary["matches"] == 6


def test_player_summary_of_an_unknown_player_is_empty_not_everything(db: AnalyticsDB):
    summary = QueryEngine(db).player_summary("never-seen")
    assert summary["tracked"] is False
    assert summary["kills"] == 0 and summary["deaths"] == 0


def test_kd_reports_kills_when_deaths_are_zero(db: AnalyticsDB):
    """Dividing by zero deaths would crash; a flawless player is not a bug."""
    engine = QueryEngine(db)
    with db.connect() as conn:
        conn.execute("UPDATE kills SET victim_pid = NULL WHERE victim_pid IS NOT NULL")
    engine.invalidate()
    summary = engine.player_summary("atk")
    assert summary["deaths"] == 0
    assert summary["kd"] == float(summary["kills"])


def test_player_role_either_matches_kills_and_deaths(engine: QueryEngine):
    """'either' must be the union of the two, with no double counting.

    A plain `killer_pid = ? OR victim_pid = ?` cannot use either partial
    index -- SQLite falls back to a scan, measured at 2,026ms versus
    0.1ms -- so this is expressed as a UNION of rowids. UNION rather than
    UNION ALL, or a kill where someone is both ends would count twice.
    """
    as_killer = engine.summary(Filters(map_name="Ascent", player="atk", player_role="killer"))
    as_victim = engine.summary(Filters(map_name="Ascent", player="atk", player_role="victim"))
    either = engine.summary(Filters(map_name="Ascent", player="atk", player_role="either"))
    assert as_killer["total"] > 0 and as_victim["total"] > 0
    assert either["total"] == as_killer["total"] + as_victim["total"]


def test_unknown_player_matches_nothing(engine: QueryEngine):
    assert engine.summary(Filters(map_name="Ascent", player="nobody"))["total"] == 0


def test_player_matches_lists_newest_first(db: AnalyticsDB):
    rows = QueryEngine(db).player_matches("atk", limit=10)
    assert len(rows) == 6
    starts = [r["started_at"] for r in rows]
    assert starts == sorted(starts, reverse=True)
    assert rows[0]["agent"] == "Jett"      # atk's agent in the fixture
    assert rows[0]["kills"] > 0


def test_match_detail_scoreboard_reconciles_with_its_kills(db: AnalyticsDB):
    detail = QueryEngine(db).match_detail("m0")
    assert detail is not None
    assert len(detail["kills"]) == 4
    # Every kill has exactly one killer and one victim, so the scoreboard
    # totals must add up to the number of kill rows.
    assert sum(p["kills"] for p in detail["scoreboard"]) == len(detail["kills"])
    assert sum(p["deaths"] for p in detail["scoreboard"]) == len(detail["kills"])
    assert {p["puuid"] for p in detail["scoreboard"]} == {"atk", "def"}


def test_match_detail_is_none_for_an_unknown_match(db: AnalyticsDB):
    assert QueryEngine(db).match_detail("no-such-match") is None


def test_match_filter_restricts_to_one_match(engine: QueryEngine):
    one = engine.summary(Filters(map_name="Ascent", match_id="m0"))
    every = engine.summary(Filters(map_name="Ascent"))
    assert one["total"] == 4
    assert every["total"] > one["total"]


def test_kill_points_say_which_end_the_subject_was_on(db: AnalyticsDB):
    """The combined heatmap needs to know whose kill each point was.

    It previously matched on puuid client-side, but kill points carry
    agent names, not player ids -- so every duel fell through to "death"
    and the whole map rendered red. The server sets the flag instead.
    """
    engine = QueryEngine(db)
    res = engine.kill_points(Filters(map_name="Ascent", player="atk", player_role="either"))
    flags = [p.get("mine") for p in res["points"]]

    assert all(f is not None for f in flags), "every point needs the flag"
    assert any(flags) and not all(flags), "the fixture has both kills and deaths"

    # It must agree with the dedicated single-role queries.
    kills = engine.summary(Filters(map_name="Ascent", player="atk", player_role="killer"))
    deaths = engine.summary(Filters(map_name="Ascent", player="atk", player_role="victim"))
    assert sum(1 for f in flags if f) == kills["total"]
    assert sum(1 for f in flags if not f) == deaths["total"]


def test_kill_points_omit_the_flag_when_no_player_is_named(engine: QueryEngine):
    """The global views have no subject, so the field would be meaningless."""
    res = engine.kill_points(Filters(map_name="Ascent"))
    assert res["points"]
    assert all("mine" not in p for p in res["points"])


def test_mine_flag_follows_the_named_player(db: AnalyticsDB):
    """Two players in the same duels must get opposite flags."""
    engine = QueryEngine(db)
    atk = engine.kill_points(Filters(map_name="Ascent", player="atk", player_role="either"))
    dfn = engine.kill_points(Filters(map_name="Ascent", player="def", player_role="either"))

    assert sum(1 for p in atk["points"] if p["mine"]) == sum(
        1 for p in dfn["points"] if not p["mine"]
    )
