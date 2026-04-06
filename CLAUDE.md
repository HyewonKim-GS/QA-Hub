# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

```bash
cd ~/qa-search
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --log-level warning &
```

- Use `reload=False` (default). `reload=True` causes issues with the lifespan cache.
- Access at http://localhost:8000
- Check cache status: `curl -s http://localhost:8000/api/status`
- Trigger manual refresh: `curl -s -X POST http://localhost:8000/api/refresh`

## Architecture

The app is a FastAPI server with an in-memory cache. On startup, it fetches all Jira issues and Confluence pages in parallel, caches them in RAM, and all searches run against that local cache. The cache auto-refreshes every 1 hour.

```
app.py          — FastAPI server, cache lifecycle, API endpoints
search.py       — Data fetching (Jira, Confluence, Drive) + local search logic
static/index.html — Single-page UI (dark theme, no build step)
.env            — Credentials (ATLASSIAN_DOMAIN, ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, JIRA_PROJECT)
```

### Cache flow
- `_load_cache()` runs `fetch_all_jira()` and `fetch_all_confluence()` concurrently via `asyncio.gather` + `ThreadPoolExecutor`
- Each source updates `CACHE["jira"]` / `CACHE["confluence"]` as soon as it completes (not after both finish)
- Internal fields prefixed with `_` (e.g. `_search_text`, `_desc_full`) are stripped from API responses by `clean()`

### Jira fetching
- Uses `/rest/api/3/search/jql` (NOT the deprecated `/rest/api/3/search`)
- Paginates via `nextPageToken` / `isLast` (NOT `startAt` / `total`)
- JQL: `project = GS AND updated >= -730d ORDER BY updated DESC`
- Jira description is in Atlassian Document Format (ADF); `_extract_adf_text()` recursively extracts plain text

### Confluence fetching
- Uses `/wiki/rest/api/content?spaceKey=GM` and `?spaceKey=CVS` separately
- **Do NOT use `/wiki/rest/api/content/search`** — its `start` pagination is broken (always returns the same first page regardless of offset)
- Fetches metadata only (no `body.view`) for speed; search is title-based
- `_search_text` field = lowercased title only

### Google Drive search
- Real-time (not cached); called on every search request
- Connects to local MCP SSE server at `http://172.16.50.144:3100` using JSON-RPC over SSE
- Query format must be `name contains 'keyword'` (Google Drive API query syntax)
- Session-per-request: opens SSE → gets session URL → initializes → calls `drive_search` → closes

### gs-os-ontology MCP
- Same MCP SSE server at `http://172.16.50.144:3100` — shared with Google Drive
- `call_mcp_tool(tool_name, arguments)` in `search.py` is the generic caller for any MCP tool
- Used tools:
  - `search_games` — 게임 목록 검색 (서버 시작 시 전체 게임 목록 캐싱에도 사용)
  - `get_game` — 게임명으로 상세 정보(Drive 링크 등) 조회
- Results tagged with `"from_ontology": True` and merged with Drive results (ontology results take priority)

### Local search
- `search_jira_local()`: searches `summary + _desc_full` (title + full description)
- `search_confluence_local()`: searches `_search_text` (title only)
- Both support synonym expansion via `RELATED` dict in `search.py`
- Multi-word queries require ALL words to match (AND logic), each word can match via any synonym

### Synonym map
Add synonyms to `RELATED` dict in `search.py`. Each entry maps a word to its synonyms; add the reverse mapping too. Examples already defined: 잭팟↔jackpot↔jp, 쿠도↔kudo↔kudos, 버그↔bug, 크래시↔crash, etc.

### OpenAI GPT API
- Model: `gpt-4o-mini`
- API key: `OPENAI_API_KEY` in `.env`
- Used in two places:
  - `search.py` — `_gpt_translate()`: 검색어 한↔영 번역 (동의어 사전에 없는 단어 fallback), 결과는 `_TRANS_CACHE`에 메모이제이션
  - `app.py` — `_process_chat()`: 채팅 메시지에서 검색 키워드 추출 및 의도 분석

### Game panel document links (`/api/game_links`)
- Returns TC / GDD / MATH / CTD / Sound / 연출 links for the game side panel
- **GDD/MATH**: real-time Drive folder search — `_search_keywords()` tries tc_prefix → apostrophe-split longest part → `&→and` → raw name
- **Sound**: cached tab map (`sound_tabs`), matched by `tc_prefix`; auto-resolved from `sheet_games` if missing
- **연출**: cached tab map (`direction_tabs`), fuzzy match via `_norm_tab()` (strips apostrophes, hyphens, normalizes `&`/case)
- **CTD**: cached `ctd_game_info` rows, matched by game_id → game_name → tc_code
- Drive folders may contain subfolders — `_drive_search_in_folders()` returns folder URL first (subfolder structure support)
- Full design doc: `docs/game-links.md`
- Folder/sheet constants at top of `app.py` (~line 52): `_SOUND_SHEET_ID`, `_DIRECTION_SHEET_ID`, `_GDD_FOLDER_IDS`, `_MATH_FOLDER_ID`, `_CTD_GAME_INFO_GID`

## Key constraints

- Python 3.9 — use `Optional[X]` / `List[X]` from `typing`, not `X | None` or `list[X]`
- Confluence space keys: `GM` (Game Studio), `CVS` — not space names in CQL
- Jira JQL `-2y` not supported by new search/jql API; use `-730d` instead
