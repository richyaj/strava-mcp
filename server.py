import os
import sqlite3
import httpx
from datetime import datetime, timedelta
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()
mcp = FastMCP("strava")

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE  = "https://www.strava.com/api/v3"
DB_PATH          = os.path.join(os.path.dirname(__file__), "strava.db")


# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id              INTEGER PRIMARY KEY,
                name            TEXT,
                type            TEXT,
                date            TEXT,
                year            TEXT,
                month           TEXT,
                distance_km     REAL,
                duration_min    REAL,
                elevation_m     REAL,
                avg_heartrate   REAL,
                max_heartrate   REAL,
                avg_speed_kph   REAL,
                suffer_score    REAL,
                pr_count        INTEGER,
                calories        REAL,
                synced_at       TEXT
            )
        """)
        conn.commit()

init_db()


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """Exchange refresh token for a fresh access token."""
    response = httpx.post(STRAVA_TOKEN_URL, data={
        "client_id":     os.getenv("STRAVA_CLIENT_ID"),
        "client_secret": os.getenv("STRAVA_CLIENT_SECRET"),
        "refresh_token": os.getenv("STRAVA_REFRESH_TOKEN"),
        "grant_type":    "refresh_token",
    })
    return response.json()["access_token"]

def headers():
    return {"Authorization": f"Bearer {get_access_token()}"}


# ── Sync ───────────────────────────────────────────────────────────────────────

@mcp.tool()
def sync_activities(full_sync: bool = False) -> dict:
    """Fetch activities from Strava and store them in the local SQLite database.
    full_sync=False (default): only fetches new activities since the last sync.
    full_sync=True: re-fetches everything from the beginning of time.
    Run this once to populate the DB, then again after each new workout."""

    h          = headers()
    synced_at  = datetime.utcnow().isoformat()
    after_ts   = None

    if not full_sync:
        with get_db() as conn:
            row = conn.execute("SELECT MAX(date) as last_date FROM activities").fetchone()
            if row["last_date"]:
                after_ts = int(datetime.fromisoformat(row["last_date"]).timestamp())

    all_activities = []
    page = 1
    while True:
        params = {"per_page": 200, "page": page}
        if after_ts:
            params["after"] = after_ts

        response = httpx.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=h, params=params, timeout=30
        ).json()

        if not response:
            break

        all_activities.extend(response)
        page += 1

    with get_db() as conn:
        for a in all_activities:
            conn.execute("""
                INSERT OR REPLACE INTO activities
                (id, name, type, date, year, month, distance_km, duration_min,
                 elevation_m, avg_heartrate, max_heartrate, avg_speed_kph,
                 suffer_score, pr_count, calories, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                a["id"], a["name"], a["type"],
                a["start_date_local"],
                a["start_date_local"][:4],
                a["start_date_local"][:7],
                round(a.get("distance", 0) / 1000, 2),
                round(a.get("moving_time", 0) / 60, 1),
                a.get("total_elevation_gain", 0),
                a.get("average_heartrate"),
                a.get("max_heartrate"),
                round(a.get("average_speed", 0) * 3.6, 2),
                a.get("suffer_score"),
                a.get("pr_count", 0),
                a.get("calories"),
                synced_at
            ))
        conn.commit()

    return {
        "status":             "success",
        "activities_synced":  len(all_activities),
        "sync_type":          "full" if full_sync else "incremental",
        "synced_at":          synced_at,
    }


# ── DB query tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def get_db_stats() -> dict:
    """Returns stats about the local database — total activities, date range, last sync.
    Good first call to check the DB is populated before running analysis."""
    with get_db() as conn:
        total    = conn.execute("SELECT COUNT(*) as count FROM activities").fetchone()["count"]
        oldest   = conn.execute("SELECT MIN(date) as d FROM activities").fetchone()["d"]
        newest   = conn.execute("SELECT MAX(date) as d FROM activities").fetchone()["d"]
        last_sync = conn.execute("SELECT MAX(synced_at) as s FROM activities").fetchone()["s"]
        by_type  = conn.execute("""
            SELECT type, COUNT(*) as count
            FROM activities GROUP BY type ORDER BY count DESC
        """).fetchall()
        return {
            "total_activities": total,
            "oldest_activity":  oldest,
            "newest_activity":  newest,
            "last_synced":      last_sync,
            "by_type":          [dict(r) for r in by_type],
        }


@mcp.tool()
def query_activities(
    activity_type: str  = None,
    year: int           = None,
    month: str          = None,
    min_distance_km: float = None,
    limit: int          = 50
) -> list:
    """Query activities from the local DB with optional filters.
    activity_type: e.g. 'Run', 'Ride', 'Swim'
    year: e.g. 2024
    month: e.g. '2024-03'
    min_distance_km: only return activities at least this long."""
    sql    = "SELECT * FROM activities WHERE 1=1"
    params = []

    if activity_type:
        sql += " AND type = ?";    params.append(activity_type)
    if year:
        sql += " AND year = ?";    params.append(str(year))
    if month:
        sql += " AND month = ?";   params.append(month)
    if min_distance_km:
        sql += " AND distance_km >= ?"; params.append(min_distance_km)

    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


@mcp.tool()
def get_yearly_summary() -> list:
    """Aggregate stats per year and sport — distance, duration, elevation, PRs, calories.
    Reads from the local DB so no API call is needed."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                year,
                type,
                COUNT(*)                     AS total_activities,
                ROUND(SUM(distance_km), 1)   AS total_distance_km,
                ROUND(SUM(duration_min), 0)  AS total_duration_min,
                ROUND(SUM(elevation_m), 0)   AS total_elevation_m,
                ROUND(AVG(avg_heartrate), 1) AS avg_heartrate,
                SUM(pr_count)                AS total_prs,
                ROUND(SUM(calories), 0)      AS total_calories
            FROM activities
            GROUP BY year, type
            ORDER BY year DESC, total_distance_km DESC
        """).fetchall()
        return [dict(row) for row in rows]


@mcp.tool()
def get_monthly_summary(year: int = None) -> list:
    """Aggregate stats per month, optionally filtered to a specific year.
    Great for spotting seasonal training patterns."""
    sql    = """
        SELECT
            month,
            type,
            COUNT(*)                      AS total_activities,
            ROUND(SUM(distance_km), 1)    AS total_distance_km,
            ROUND(SUM(duration_min), 0)   AS total_duration_min,
            ROUND(SUM(elevation_m), 0)    AS total_elevation_m,
            ROUND(AVG(avg_speed_kph), 2)  AS avg_speed_kph,
            SUM(pr_count)                 AS total_prs
        FROM activities
    """
    params = []
    if year:
        sql += " WHERE year = ?"; params.append(str(year))
    sql += " GROUP BY month, type ORDER BY month DESC"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


# ── Live Strava tools ──────────────────────────────────────────────────────────

@mcp.tool()
def get_recent_activities(num_activities: int = 10) -> list:
    """Fetch the most recent activities live from Strava (not the local DB).
    Useful for checking the latest workout before a sync."""
    activities = httpx.get(
        f"{STRAVA_API_BASE}/athlete/activities",
        headers=headers(),
        params={"per_page": num_activities}
    ).json()
    return [{
        "id":            a["id"],
        "name":          a["name"],
        "type":          a["type"],
        "date":          a["start_date_local"],
        "distance_km":   round(a["distance"] / 1000, 2),
        "duration_min":  round(a["moving_time"] / 60, 1),
        "elevation_m":   a["total_elevation_gain"],
        "avg_heartrate": a.get("average_heartrate"),
        "avg_speed_kph": round(a["average_speed"] * 3.6, 2),
    } for a in activities]


@mcp.tool()
def get_athlete_stats() -> dict:
    """Fetch all-time aggregate stats from Strava (totals for runs, rides, swims etc.)."""
    h       = headers()
    athlete = httpx.get(f"{STRAVA_API_BASE}/athlete", headers=h).json()
    stats   = httpx.get(f"{STRAVA_API_BASE}/athletes/{athlete['id']}/stats", headers=h).json()
    return stats


@mcp.tool()
def get_activity_detail(activity_id: int) -> dict:
    """Deep dive into a single workout — splits, laps, and best efforts.
    Use get_recent_activities or query_activities first to find the activity_id."""
    h        = headers()
    activity = httpx.get(
        f"{STRAVA_API_BASE}/activities/{activity_id}",
        headers=h, params={"include_all_efforts": True}
    ).json()
    laps = httpx.get(f"{STRAVA_API_BASE}/activities/{activity_id}/laps", headers=h).json()

    return {
        "name":          activity.get("name"),
        "date":          activity.get("start_date_local"),
        "type":          activity.get("type"),
        "distance_km":   round(activity.get("distance", 0) / 1000, 2),
        "duration_min":  round(activity.get("moving_time", 0) / 60, 1),
        "elevation_m":   activity.get("total_elevation_gain"),
        "avg_heartrate": activity.get("average_heartrate"),
        "max_heartrate": activity.get("max_heartrate"),
        "calories":      activity.get("calories"),
        "description":   activity.get("description"),
        "splits_metric": [
            {
                "km":            s.get("split"),
                "elapsed_sec":   s.get("elapsed_time"),
                "elevation_diff":s.get("elevation_difference"),
                "avg_heartrate": s.get("average_heartrate"),
                "avg_speed_kph": round(s.get("average_speed", 0) * 3.6, 2),
                "pace_per_km":   str(timedelta(seconds=int(1000 / s["average_speed"])))
                                 if s.get("average_speed") else None,
            }
            for s in activity.get("splits_metric", [])
        ],
        "laps": [
            {
                "lap":           l.get("lap_index"),
                "distance_km":   round(l.get("distance", 0) / 1000, 2),
                "duration_min":  round(l.get("moving_time", 0) / 60, 1),
                "avg_speed_kph": round(l.get("average_speed", 0) * 3.6, 2),
                "avg_heartrate": l.get("average_heartrate"),
                "elevation_m":   l.get("total_elevation_gain"),
            }
            for l in laps
        ],
        "best_efforts": [
            {
                "name":        e.get("name"),
                "distance_m":  e.get("distance"),
                "elapsed_sec": e.get("elapsed_time"),
                "pr_rank":     e.get("pr_rank"),
            }
            for e in activity.get("best_efforts", [])
        ],
    }


@mcp.tool()
def get_heart_rate_zones(activity_id: int) -> dict:
    """Get time spent in each heart rate zone for a specific activity.
    Requires a Strava subscription and HR data recorded during the activity."""
    zones = httpx.get(
        f"{STRAVA_API_BASE}/activities/{activity_id}/zones",
        headers=headers()
    ).json()

    for zone_block in zones:
        if zone_block.get("type") == "heartrate":
            return {
                "heart_rate_zones": [
                    {
                        "zone":     i + 1,
                        "min_bpm":  z.get("min"),
                        "max_bpm":  z.get("max"),
                        "time_sec": z.get("time"),
                        "time_min": round(z.get("time", 0) / 60, 1),
                    }
                    for i, z in enumerate(zone_block.get("distribution_buckets", []))
                ]
            }
    return {"error": "No heart rate zone data available for this activity"}


@mcp.tool()
def get_weekly_summary() -> dict:
    """Compare this week's training vs last week — distance, duration, and activity count.
    Fetches live from Strava so it always reflects the latest data."""
    h   = headers()
    now = datetime.utcnow()

    today       = now.date()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(weeks=1)

    def fetch_week(start_date):
        after  = int(datetime.combine(start_date, datetime.min.time()).timestamp())
        before = after + 7 * 86400
        return httpx.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=h,
            params={"after": after, "before": before, "per_page": 50}
        ).json()

    def summarise(activities):
        by_type = {}
        for a in activities:
            t = a.get("type", "Other")
            if t not in by_type:
                by_type[t] = {"count": 0, "distance_km": 0, "duration_min": 0, "elevation_m": 0}
            by_type[t]["count"]        += 1
            by_type[t]["distance_km"]  += round(a.get("distance", 0) / 1000, 2)
            by_type[t]["duration_min"] += round(a.get("moving_time", 0) / 60, 1)
            by_type[t]["elevation_m"]  += a.get("total_elevation_gain", 0)
        return {
            "total_activities":  len(activities),
            "total_distance_km": round(sum(a.get("distance", 0) for a in activities) / 1000, 2),
            "total_duration_min":round(sum(a.get("moving_time", 0) for a in activities) / 60, 1),
            "by_type":           by_type,
        }

    this = summarise(fetch_week(this_monday))
    last = summarise(fetch_week(last_monday))

    return {
        "this_week": {"starting": str(this_monday), **this},
        "last_week": {"starting": str(last_monday), **last},
        "changes": {
            "distance_km":  round(this["total_distance_km"] - last["total_distance_km"], 2),
            "duration_min": round(this["total_duration_min"] - last["total_duration_min"], 1),
            "activities":   this["total_activities"] - last["total_activities"],
        },
    }


@mcp.tool()
def get_best_efforts(activity_id: int) -> list:
    """Get personal best efforts for common distances (1K, 1 mile, 5K, 10K, half marathon, marathon)
    from a specific run. Use get_recent_activities first to find the activity_id."""
    activity = httpx.get(
        f"{STRAVA_API_BASE}/activities/{activity_id}",
        headers=headers(),
        params={"include_all_efforts": True}
    ).json()
    return [
        {
            "distance":    e.get("name"),
            "time":        str(timedelta(seconds=e.get("elapsed_time", 0))),
            "elapsed_sec": e.get("elapsed_time"),
            "pr_rank":     e.get("pr_rank"),   # 1 = all-time PR, 2 = 2nd best, None = not a PR
            "is_pr":       e.get("pr_rank") == 1,
        }
        for e in activity.get("best_efforts", [])
    ]


@mcp.tool()
def get_relative_effort_trend(num_activities: int = 20) -> dict:
    """Analyse Relative Effort (suffer score) trend across recent activities.
    Relative Effort is a Strava subscriber-only metric — quantifies workout
    difficulty based on time spent in each heart rate zone."""
    activities = httpx.get(
        f"{STRAVA_API_BASE}/athlete/activities",
        headers=headers(),
        params={"per_page": num_activities}
    ).json()

    efforts = [
        {
            "name":            a["name"],
            "date":            a["start_date_local"],
            "type":            a["type"],
            "relative_effort": a.get("suffer_score"),
            "distance_km":     round(a.get("distance", 0) / 1000, 2),
            "duration_min":    round(a.get("moving_time", 0) / 60, 1),
        }
        for a in activities
        if a.get("suffer_score") is not None
    ]

    if not efforts:
        return {"error": "No Relative Effort data found. Make sure you recorded heart rate and have a Strava subscription."}

    scores = [e["relative_effort"] for e in efforts]
    return {
        "activities": efforts,
        "summary": {
            "avg_relative_effort":  round(sum(scores) / len(scores), 1),
            "highest":              max(scores),
            "lowest":               min(scores),
            "total_efforts_found":  len(efforts),
        },
    }


if __name__ == "__main__":
    mcp.run()