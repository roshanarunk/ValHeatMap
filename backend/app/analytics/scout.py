"""Pre-Match Opponent Scouting analytics module.

Analyzes an opponent's historical tendencies on a specific map and agent:
- Opening duel win rates and first-contact frequency
- Tactical playstyle classification and tendency badges
- Habitual rotation corridors on Attack and Defense
- First blood kill and death hotspot coordinates
- Actionable counter-play tips
- Multi-player lobby threat assessment
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..analytics_db import (
    FLAG_ADVANTAGE_DEATH,
    FLAG_CLUTCH_KILL,
    FLAG_FIRST_BLOOD,
    FLAG_ISOLATED,
    FLAG_POST_PLANT,
    FLAG_ROUND_WON,
    FLAG_SUPPORTED,
    FLAG_TRADE_KILL,
    FLAG_TRADED,
    SIDE_NAME,
    AnalyticsDB,
    from_pos,
)
from ..queries import Filters, QueryEngine
from ..reference import get_agent


def scout_player(
    db: AnalyticsDB,
    engine: QueryEngine,
    puuid: str,
    name: str,
    tag: str,
    region: str | None,
    map_name: str,
    agent: str | None = None,
) -> dict[str, Any]:
    """Generate a comprehensive tactical scouting dossier for a player on a map."""
    pid = engine._ids("player").get(puuid)
    if pid is None:
        db.invalidate_dim_cache()
        engine._dims.pop("player", None)
        pid = engine._ids("player").get(puuid)

    maps_by_lower = {k.lower(): v for k, v in engine._ids("map").items()}
    map_id = maps_by_lower.get(map_name.lower())

    if pid is None or map_id is None:
        return {
            "riot_id": f"{name}#{tag}" if tag else name,
            "puuid": puuid,
            "name": name,
            "tag": tag,
            "region": region or "na",
            "found": True,
            "has_data": False,
            "matches_on_map": 0,
            "map_name": map_name,
            "agent": agent or "",
            "message": f"No recorded matches on {map_name} in the database yet.",
            "top_agents": [],
            "tactical_tags": [],
            "counter_tips": ["No match history on this map yet. Play standard defaults."],
            "defense_rotations": [],
            "attack_rotations": [],
            "first_blood_points": [],
        }

    # 1. Map agent breakdown
    top_agents: list[dict[str, Any]] = []
    with db.connect() as conn:
        agent_rows = conn.execute(
            """SELECT ka.name as agent_name,
                      COUNT(DISTINCT k.m) as matches,
                      SUM(k.killer_pid = ?) as kills,
                      SUM(k.victim_pid = ?) as deaths,
                      SUM(k.killer_pid = ? AND (k.flags & ?) != 0) as first_bloods
               FROM kills k
               JOIN dim ka ON ka.kind = 'agent' AND ka.id = (CASE WHEN k.killer_pid = ? THEN k.ka_id ELSE k.va_id END)
               WHERE (k.killer_pid = ? OR k.victim_pid = ?) AND k.map_id = ?
               GROUP BY ka.name
               ORDER BY matches DESC, kills DESC""",
            (pid, pid, pid, FLAG_FIRST_BLOOD, pid, pid, pid, map_id),
        ).fetchall()

    for r in agent_rows:
        ag_name = r["agent_name"] or "Unknown"
        ag_info = get_agent(ag_name)
        k_cnt = r["kills"] or 0
        d_cnt = r["deaths"] or 0
        top_agents.append(
            {
                "agent": ag_name,
                "role": ag_info.role if ag_info else "",
                "icon": ag_info.icon if ag_info else "",
                "matches": r["matches"] or 0,
                "kills": k_cnt,
                "deaths": d_cnt,
                "kd": round(k_cnt / d_cnt, 2) if d_cnt else float(k_cnt),
                "first_bloods": r["first_bloods"] or 0,
            }
        )

    # Determine focus agent: user selected agent, or their #1 played on this map
    scout_agent = agent
    if not scout_agent and top_agents:
        scout_agent = top_agents[0]["agent"]

    ag_info = get_agent(scout_agent) if scout_agent else None
    agent_icon = ag_info.icon if ag_info else ""
    agent_role = ag_info.role if ag_info else ""

    # 2. Player summary stats on map (and agent if applicable)
    f = Filters(map_name=map_name, agents=[scout_agent] if scout_agent else [])
    summary = engine.player_summary(puuid, f)

    # If scoped to an agent returned 0 matches, fallback to all agents on this map
    if summary.get("matches", 0) == 0 and scout_agent:
        f_all = Filters(map_name=map_name)
        summary = engine.player_summary(puuid, f_all)

    matches_cnt = summary.get("matches", 0)
    has_data = matches_cnt > 0

    # 3. Top rotation corridors
    def_rotations: list[dict[str, Any]] = []
    atk_rotations: list[dict[str, Any]] = []

    with db.connect() as conn:
        rot_rows = conn.execute(
            """SELECT from_zone, to_zone, side,
                      COUNT(*) as count,
                      AVG((t_end_ms - t_start_ms) / 1000.0) as avg_duration_s,
                      SUM(won) as rounds_won
               FROM rotations
               WHERE map_id = ? AND player_pid = ?
               GROUP BY from_zone, to_zone, side
               ORDER BY count DESC
               LIMIT 25""",
            (map_id, pid),
        ).fetchall()

    for r in rot_rows:
        cnt = r["count"] or 0
        w_cnt = r["rounds_won"] or 0
        entry = {
            "from_zone": r["from_zone"],
            "to_zone": r["to_zone"],
            "count": cnt,
            "win_rate": round(w_cnt / cnt, 4) if cnt else 0.0,
            "avg_duration_s": round(r["avg_duration_s"] or 0.0, 1),
            "side": "defense" if r["side"] == 2 else ("attack" if r["side"] == 1 else "other"),
        }
        if r["side"] == 2 and len(def_rotations) < 4:
            def_rotations.append(entry)
        elif r["side"] == 1 and len(atk_rotations) < 4:
            atk_rotations.append(entry)

    # 4. First blood spatial points (kills and deaths)
    fb_points: list[dict[str, Any]] = []
    with db.connect() as conn:
        fb_rows = conn.execute(
            """SELECT round_num, t_ms, side, vx, vy, kx, ky,
                      (killer_pid = ?) as is_killer
               FROM kills
               WHERE (killer_pid = ? OR victim_pid = ?)
                 AND map_id = ?
                 AND (flags & ?) != 0
               ORDER BY t_ms ASC
               LIMIT 40""",
            (pid, pid, pid, map_id, FLAG_FIRST_BLOOD),
        ).fetchall()

    for r in fb_rows:
        is_k = bool(r["is_killer"])
        # If player was killer, their position was kx, ky (or vx, vy if killer pos missing)
        # If player was victim, their death position was vx, vy
        px = from_pos(r["kx"]) if (is_k and r["kx"] is not None) else from_pos(r["vx"])
        py = from_pos(r["ky"]) if (is_k and r["ky"] is not None) else from_pos(r["vy"])
        fb_points.append(
            {
                "round": r["round_num"],
                "t_ms": r["t_ms"],
                "side": SIDE_NAME.get(r["side"], "none"),
                "is_killer": is_k,
                "x": px,
                "y": py,
            }
        )

    # 5. Tactical Badges & Counter Tips
    tactical_tags = _derive_tactical_tags(summary, matches_cnt)
    counter_tips = _derive_counter_tips(summary, tactical_tags, def_rotations, atk_rotations, map_name)

    return {
        "riot_id": f"{name}#{tag}" if tag else name,
        "puuid": puuid,
        "name": name,
        "tag": tag,
        "region": region or "na",
        "found": True,
        "has_data": has_data,
        "matches_on_map": matches_cnt,
        "map_name": map_name,
        "agent": scout_agent or "",
        "agent_icon": agent_icon,
        "agent_role": agent_role,
        "kd": summary.get("kd", 0.0),
        "kills": summary.get("kills", 0),
        "deaths": summary.get("deaths", 0),
        "opening_duels": summary.get("opening_duels", 0),
        "first_bloods": summary.get("first_bloods", 0),
        "first_deaths": summary.get("first_deaths", 0),
        "opening_win_rate": summary.get("opening_win_rate", 0.0),
        "trade_rate": summary.get("trade_rate", 0.0),
        "clutch_win_rate": summary.get("clutch_win_rate", 0.0),
        "clutches_won": summary.get("clutches_won", 0),
        "clutches_faced": summary.get("clutches_faced", 0),
        "advantage_throw_rate": summary.get("advantage_throw_rate", 0.0),
        "advantage_rounds_thrown": summary.get("advantage_rounds_thrown", 0),
        "support_rate": summary.get("support_rate", 0.0),
        "impact_kill_rate": summary.get("impact_kill_rate", 0.0),
        "top_agents": top_agents,
        "tactical_tags": tactical_tags,
        "counter_tips": counter_tips,
        "defense_rotations": def_rotations,
        "attack_rotations": atk_rotations,
        "first_blood_points": fb_points,
    }


def _derive_tactical_tags(s: dict[str, Any], matches: int) -> list[dict[str, str]]:
    tags: list[dict[str, str]] = []
    duels = s.get("opening_duels", 0)
    win_rate = s.get("opening_win_rate", 0.0)
    support_rate = s.get("support_rate", 0.0)
    adv_throw_rate = s.get("advantage_throw_rate", 0.0)
    adv_thrown = s.get("advantage_rounds_thrown", 0)
    clutches_won = s.get("clutches_won", 0)
    clutch_wr = s.get("clutch_win_rate", 0.0)
    impact_rate = s.get("impact_kill_rate", 0.0)
    kills = s.get("kills", 0)
    deaths = s.get("deaths", 0)
    trade_rate = s.get("trade_rate", 0.0)

    # Opening Duel Threat
    if duels >= 4 and win_rate >= 0.58:
        tags.append({
            "tag": "🎯 Opening Duel Threat",
            "type": "threat",
            "desc": f"Wins {round(win_rate * 100)}% of first duels ({s.get('first_bloods', 0)}/{duels}). High early round frag danger.",
        })
    elif duels >= 4 and win_rate <= 0.40:
        tags.append({
            "tag": "⚠️ Opening Liability",
            "type": "weakness",
            "desc": f"Frequently contests early fights but loses {round((1 - win_rate) * 100)}% of opening duels. Prime target to exploit.",
        })
    elif duels >= 6 and matches > 0 and (duels / matches) >= 2.5:
        tags.append({
            "tag": "⚡ Hyper-Aggressive Entry",
            "type": "playstyle",
            "desc": "Regularly initiates team contact in early round choke points.",
        })

    # Clutch Threat
    if clutches_won >= 1 and clutch_wr >= 0.25:
        tags.append({
            "tag": "🏆 Clutch Specialist",
            "type": "threat",
            "desc": f"Dangerous in 1vX scenarios with a {round(clutch_wr * 100)}% conversion rate ({clutches_won} clutches won).",
        })

    # Man Advantage Throw
    if adv_thrown >= 1 and adv_throw_rate >= 0.28:
        tags.append({
            "tag": "🛑 Man-Advantage Overpeeker",
            "type": "weakness",
            "desc": f"Prone to dying aggressively when up numbers ({round(adv_throw_rate * 100)}% throw rate). Do not surrender when down players.",
        })

    # Micro-spacing / Support
    if deaths >= 5 and support_rate >= 0.45:
        tags.append({
            "tag": "🤝 Disciplined Spacing",
            "type": "playstyle",
            "desc": f"Plays closely with teammates ({round(support_rate * 100)}% supported deaths). Expect trade re-frags.",
        })
    elif deaths >= 5 and support_rate < 0.25:
        tags.append({
            "tag": "👤 Isolated Anchor / Lurker",
            "type": "weakness",
            "desc": f"Often holds positions alone without immediate support ({round((1 - support_rate) * 100)}% isolated deaths).",
        })

    # High Impact Fragger
    if kills >= 10 and impact_rate >= 0.85:
        tags.append({
            "tag": "🔥 High Round Impact",
            "type": "threat",
            "desc": f"{round(impact_rate * 100)}% of frags occur in winnable, high-leverage rounds.",
        })

    return tags


def _derive_counter_tips(
    s: dict[str, Any],
    tags: list[dict[str, str]],
    def_rots: list[dict[str, Any]],
    atk_rots: list[dict[str, Any]],
    map_name: str,
) -> list[str]:
    tips: list[str] = []
    tag_names = {t["tag"] for t in tags}

    if "🎯 Opening Duel Threat" in tag_names:
        tips.append("Do not dry-peek early opening angles against this player; use utility (flashes, recon, smokes) before contesting main corridors.")
    elif "⚠️ Opening Liability" in tag_names:
        tips.append("Hold disciplined crosshairs on common opening peeks. They consistently offer free first bloods by overpeeking.")

    if "🛑 Man-Advantage Overpeeker" in tag_names:
        tips.append("When you are down a player (4v5 or 3v4), hold defensive crossfires. This player often throws by aggressively hunting.")

    if "👤 Isolated Anchor / Lurker" in tag_names:
        tips.append("Flood their defensive site together. When isolated, their teammates rarely trade their death.")

    if "🏆 Clutch Specialist" in tag_names:
        tips.append("In late 2v1 or 3v1 scenarios, play together for crossfire trades; never give them consecutive 1v1 duels.")

    # Corridors
    if def_rots:
        fav_def = def_rots[0]
        tips.append(
            f"On Defense, their primary rotation is {fav_def['from_zone']} ➔ {fav_def['to_zone']} "
            f"({round(fav_def['win_rate'] * 100)}% win rate, ~{fav_def['avg_duration_s']}s). Catch them mid-rotation."
        )

    if not tips:
        tips.append(f"Play disciplined fundamentals on {map_name}. Trade teammates and deny early space.")

    return tips


def generate_lobby_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate high-level tactical briefing and threat ranking for the scouted lobby."""
    valid = [r for r in reports if r.get("has_data")]
    if not valid:
        return {
            "top_threat": None,
            "weak_link": None,
            "playstyle_notes": ["Insufficient historical data in the local database for this lobby."],
        }

    # Threat: highest opening duel win rate or highest K/D
    threat_candidates = sorted(
        valid,
        key=lambda r: (r.get("opening_duels", 0) >= 3, r.get("opening_win_rate", 0), r.get("kd", 0)),
        reverse=True,
    )
    top_threat = threat_candidates[0] if threat_candidates else None

    # Weak link: lowest opening win rate (with attempts) or highest advantage throw rate
    weak_candidates = sorted(
        valid,
        key=lambda r: (
            r.get("advantage_throw_rate", 0) >= 0.25,
            -r.get("trade_rate", 0),
            -r.get("opening_win_rate", 1.0) if r.get("opening_duels", 0) >= 2 else 0,
        ),
        reverse=True,
    )
    weak_link = weak_candidates[0] if weak_candidates else None

    notes: list[str] = []
    if top_threat and top_threat.get("opening_duels", 0) >= 3:
        notes.append(
            f"Primary Opening Fragger: {top_threat['riot_id']} ({top_threat.get('agent', 'Unknown')}) "
            f"with {round(top_threat.get('opening_win_rate', 0) * 100)}% opening duel WR."
        )

    if weak_link and weak_link != top_threat:
        notes.append(
            f"Exploitable Target: {weak_link['riot_id']} ({weak_link.get('agent', 'Unknown')}) "
            f"— trade rate is {round(weak_link.get('trade_rate', 0) * 100)}%."
        )

    return {
        "top_threat": {
            "riot_id": top_threat["riot_id"],
            "agent": top_threat.get("agent", ""),
            "agent_icon": top_threat.get("agent_icon", ""),
            "kd": top_threat.get("kd", 0.0),
            "opening_win_rate": top_threat.get("opening_win_rate", 0.0),
            "reason": "Highest opening duel danger and combat efficiency.",
        } if top_threat else None,
        "weak_link": {
            "riot_id": weak_link["riot_id"],
            "agent": weak_link.get("agent", ""),
            "agent_icon": weak_link.get("agent_icon", ""),
            "trade_rate": weak_link.get("trade_rate", 0.0),
            "reason": "Low teammate trade support or prone to man-advantage throws.",
        } if weak_link else None,
        "playstyle_notes": notes,
    }
