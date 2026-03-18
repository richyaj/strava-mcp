# Strava MCP Server

MCP server for syncing Strava activities and querying training data from a local SQLite database.

## What this project does

- Fetches activities from Strava via OAuth refresh flow
- Stores activities in a local SQLite file (`strava.db`)
- Exposes MCP tools for:
	- sync (`sync_activities`)
	- DB stats and filtering
	- yearly/monthly summaries
	- live Strava lookups

## Requirements

- Python 3.14+
- Strava API app credentials

## Configuration

Create a local `.env` file in this folder with:

- `STRAVA_CLIENT_ID`
- `STRAVA_CLIENT_SECRET`
- `STRAVA_REFRESH_TOKEN`

Example:

```env
STRAVA_CLIENT_ID=...
STRAVA_CLIENT_SECRET=...
STRAVA_REFRESH_TOKEN=...
```

## Run

From this directory:

```bash
uv run server.py
```

## Data storage

- Local DB path: `strava.db`
- Table created: `activities`
- Typical data stored: activity metadata and metrics (distance, duration, heartrate, calories, etc.)

## Security notes

- Never commit `.env` or database files.
- `.gitignore` is configured to ignore:
	- `.env`
	- `*.db`
- If credentials were ever exposed, rotate them immediately in Strava settings.
- If a secret file is accidentally committed, remove it from git history before continuing work.

