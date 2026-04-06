# QA Hub — Agent Instructions

QA Hub is a FastAPI-based internal dashboard for the Game Studio QA team.
It aggregates data from Jira, Confluence, Google Drive, Slack, and the gs-os-ontology MCP server.

## Architecture

- `app.py` — FastAPI server, in-memory cache, all API endpoints (34 routes)
- `search.py` — Data fetching (Jira, Confluence, Drive, Slack) + local search logic
- `static/hub.html` — Single-page UI, no build step required
- `static/hub.css` — UI stylesheet
- `docs/presentation.html` — Team intro slide deck (served at `/presentation`)
- `.env` — Credentials (never commit)

## Baseline Rules

- Never commit `.env`. Credentials stay local.
- Never hardcode `/Users/` paths — use environment variables.
- `app.py` is large (3000+ lines). When editing, read only the relevant section. Cite `[app.py:line]` in reports.
- All data sources (Jira, Confluence, Drive, Slack) are **read-only**. Never write back to external services.
- When adding API endpoints, follow the existing pattern: FastAPI route decorator → async function → return `JSONResponse`.

## Data Sources

| Source | How accessed | Cached? |
|--------|-------------|---------|
| Jira | REST API v3 (`/rest/api/3/search/jql`) | Yes, 1h |
| Confluence | REST API (`/wiki/rest/api/content`) | Yes, 1h |
| Google Drive | MCP SSE server (`MCP_SSE_URL`) | No (real-time) |
| Slack | Slack API via `search.py` | No (real-time) |
| gs-os-ontology | MCP SSE server (`MCP_SSE_URL`) | Partial |
| Google Sheets | `gws` CLI subprocess | Yes, on startup |

## Key Constraints

- Python 3.9 — use `Optional[X]` / `List[X]` from `typing`, not `X | None` or `list[X]`
- Jira pagination: `nextPageToken` / `isLast` — NOT `startAt` / `total`
- Confluence: use `spaceKey=GM` and `spaceKey=CVS` separately — `/content/search` pagination is broken
- Jira JQL: use `-730d` not `-2y`
- MCP SSE URL default: `http://172.16.50.144:3100` (set via `MCP_SSE_URL` env var)

## Available Skills

- `/restart` — 서버 재시작
- `/cache-refresh` — 캐시 수동 갱신
- `/deploy` — 배포 전 체크리스트 실행

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ATLASSIAN_DOMAIN` | ✅ | Atlassian 도메인 (e.g. bagelcode.atlassian.net) |
| `ATLASSIAN_EMAIL` | ✅ | Atlassian 계정 이메일 |
| `ATLASSIAN_API_TOKEN` | ✅ | Atlassian API 토큰 |
| `JIRA_PROJECT` | ✅ | Jira 프로젝트 키 (e.g. GS) |
| `SLACK_BOT_TOKEN` | ✅ | Slack Bot 토큰 |
| `OPENAI_API_KEY` | ✅ | OpenAI API 키 (Hub AI용) |
| `MCP_SSE_URL` | ✅ | MCP SSE 서버 주소 |
| `REPOB_BIN` | ⬜ | repob 바이너리 경로 (없으면 코드 참조 기능만 비활성화) |
