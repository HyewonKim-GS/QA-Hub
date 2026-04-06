#!/usr/bin/env python3
"""
QA Search Web UI — FastAPI backend (with local cache)
"""

import asyncio
import json
import re
import subprocess
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import holidays as holidays_lib

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from search import (
    fetch_all_jira,
    fetch_all_confluence,
    fetch_confluence_page_body,
    search_jira_local,
    search_confluence_local,
    drive_search_mcp,
    search_slack_channels,
    fetch_live_issues,
    call_mcp_tool,
)

# ── Cache (L38~246) ───────────────────────────────────────────────────────────
# CACHE 딕셔너리, Sheets 초기 로드 (_load_contents_sheet, _load_sheet_tab_map,
# _load_ctd_game_info), _load_cache(), 1시간 자동갱신 루프

CACHE: dict = {
    "jira": [],
    "confluence": [],
    "game_code_map": {},    # game_code (lowercase) -> game_name
    "game_list": [],        # 전체 게임 목록 (search_games 전체 결과)
    "sheet_games": {},      # game_code.lower()/game_name.lower() -> {game_id, game_name, game_code, game_type}
    "last_updated": None,   # datetime | None
    "loading": False,
    "sound_tabs": {},       # tab_title_lower -> gid  (사운드 시트 탭 맵)
    "direction_tabs": {},   # tab_title_lower -> gid  (연출 리스트 탭 맵)
    "ctd_game_info": [],    # [{row_num, game_id_str, game_name, game_title}]
}

_CONTENTS_SHEET_ID  = "1qDCoxHalm1ohVW6FCIqfJ55w29bPnE8_FRZDEdPnpIE"
_SOUND_SHEET_ID     = "110ROCiEItteR_A-9yanFmbv-VNksDEGN3-tBJQ3y33w"
_DIRECTION_SHEET_ID = "1tfnyPFAtrjiaZCBrpFW9tOUpyqi0LMr17dPgfeTlm38"
_CTD_GAME_INFO_GID  = 491056088
_GDD_FOLDER_IDS     = ["1cGFkhVp9gTVYge6PRAJTwrFepz47HyJH",   # Dnipro GDD
                        "1u6mG6JNl4OP-_AdqzkRfvTF8_0e6B8R6"]   # V3 Seoul Game Design Doc
_MATH_FOLDER_ID     = "1M9_XL6YxhBeCnt0ZxzzyHd6cZ2hGXKZ5"     # v3 Math Models


def _load_contents_sheet() -> dict:
    """Game ID 시트에서 게임 목록 로드.
    반환: game_code.lower() / game_name.lower() -> {game_id, game_name, game_code, game_type}
    MCP에 없는 게임의 fallback 용.
    """
    try:
        result = subprocess.run(
            ["gws", "sheets", "+read",
             "--spreadsheet", _CONTENTS_SHEET_ID,
             "--range", "Game ID!B2:F500",
             "--format", "json"],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        rows = data.get("values", [])
        sheet_map = {}
        for row in rows[1:]:  # 첫 행은 헤더
            if len(row) < 4:
                continue
            game_id_str = row[0].strip() if row[0] else ""
            game_name = row[2].strip() if len(row) > 2 and row[2] else ""
            game_code = row[3].strip() if len(row) > 3 and row[3] else ""
            game_type = row[4].strip() if len(row) > 4 and row[4] else "Slot"
            if not game_id_str or not game_name:
                continue
            try:
                game_id_int = int(game_id_str)
            except ValueError:
                continue
            entry = {
                "game_id": game_id_int,
                "game_name": game_name,
                "game_code": game_code,
                "game_type": game_type,
            }
            if game_code:
                sheet_map[game_code.lower()] = entry
            sheet_map[game_name.lower()] = entry
        print(f"[ContentsSheet] 게임 시트 로드 완료 — {len(set(e['game_name'] for e in sheet_map.values()))}개")
        return sheet_map
    except Exception as e:
        print(f"[ContentsSheet] 로드 실패: {e}")
        return {}


def _load_sheet_tab_map(spreadsheet_id: str) -> dict:
    """스프레드시트의 모든 탭 이름과 GID를 반환: {tab_title_lower: gid}"""
    try:
        result = subprocess.run(
            ["gws", "sheets", "spreadsheets", "get",
             "--params", json.dumps({"spreadsheetId": spreadsheet_id,
                                     "fields": "sheets.properties(sheetId,title)"}),
             "--format", "json"],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout)
        return {
            s["properties"]["title"].lower(): s["properties"]["sheetId"]
            for s in data.get("sheets", [])
        }
    except Exception as e:
        print(f"[SheetTabMap] {spreadsheet_id} 로드 실패: {e}")
        return {}


def _load_ctd_game_info() -> List[dict]:
    """CTD 시트 'Game Info' 탭 B:D 열 로드. [{row_num, game_id_str, game_name, game_title}]"""
    try:
        result = subprocess.run(
            ["gws", "sheets", "+read",
             "--spreadsheet", _CONTENTS_SHEET_ID,
             "--range", "Game Info!B2:D2000",
             "--format", "json"],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        rows = data.get("values", [])
        items = []
        for i, row in enumerate(rows):
            game_id_str = row[0].strip() if len(row) > 0 and row[0] else ""
            game_name   = row[1].strip() if len(row) > 1 and row[1] else ""
            game_title  = row[2].strip() if len(row) > 2 and row[2] else ""
            if not game_name:
                continue
            items.append({
                "row_num":     i + 2,   # 시트 행 번호 (헤더=1, 첫 데이터=2)
                "game_id_str": game_id_str,
                "game_name":   game_name,
                "game_title":  game_title,  # e.g. "SS/JGQ"
            })
        print(f"[CTDGameInfo] 로드 완료 — {len(items)}행")
        return items
    except Exception as e:
        print(f"[CTDGameInfo] 로드 실패: {e}")
        return []


CACHE_TTL_SECONDS = 3600   # 1시간
_executor = ThreadPoolExecutor(max_workers=10)  # 병렬 요청 처리량 개선


async def _load_cache() -> None:
    """Jira + Confluence를 병렬로 수집, 각각 완료되는 즉시 캐시에 반영."""
    if CACHE["loading"]:
        return
    CACHE["loading"] = True
    loop = asyncio.get_event_loop()

    async def _fetch_jira():
        data = await loop.run_in_executor(_executor, fetch_all_jira)
        CACHE["jira"] = data

    async def _fetch_confluence():
        data = await loop.run_in_executor(_executor, fetch_all_confluence)
        CACHE["confluence"] = data

    async def _fetch_game_codes():
        def _load():
            sheet_map = _load_contents_sheet()
            raw = call_mcp_tool("search_games", {"query": "", "tags": []})
            if not raw:
                # MCP 실패 시 시트 데이터만으로 구성
                seen = {}
                for entry in sheet_map.values():
                    seen[entry["game_name"]] = entry
                code_map = {
                    entry["game_code"].lower(): entry["game_name"]
                    for entry in seen.values()
                    if entry.get("game_code")
                }
                games = list(seen.values())
                return code_map, games, sheet_map
            import json as _json
            games = _json.loads(raw).get("results", [])
            code_map = {g["game_code"].lower(): g["game_name"] for g in games if g.get("game_code") and g.get("game_name")}
            # 시트에 있지만 MCP에 없는 코드 보충
            for key, entry in sheet_map.items():
                if entry.get("game_code") and entry["game_code"].lower() == key:
                    if key not in code_map:
                        code_map[key] = entry["game_name"]
            return code_map, games, sheet_map
        code_map, games, sheet_map = await loop.run_in_executor(_executor, _load)
        CACHE["game_code_map"] = code_map
        CACHE["game_list"] = games
        CACHE["sheet_games"] = sheet_map
        print(f"[Cache] 게임 코드 맵 로드 완료 — MCP+시트 합계 {len(code_map)}개")

    async def _fetch_doc_tabs():
        def _load():
            sound_tabs     = _load_sheet_tab_map(_SOUND_SHEET_ID)
            direction_tabs = _load_sheet_tab_map(_DIRECTION_SHEET_ID)
            ctd_info       = _load_ctd_game_info()
            return sound_tabs, direction_tabs, ctd_info
        sound_tabs, direction_tabs, ctd_info = await loop.run_in_executor(_executor, _load)
        CACHE["sound_tabs"]     = sound_tabs
        CACHE["direction_tabs"] = direction_tabs
        CACHE["ctd_game_info"]  = ctd_info
        print(f"[Cache] 문서 탭 캐시 완료 — Sound {len(sound_tabs)}탭 / 연출 {len(direction_tabs)}탭 / CTD {len(ctd_info)}행")

    try:
        await asyncio.gather(_fetch_jira(), _fetch_confluence(), _fetch_game_codes(), _fetch_doc_tabs())
        CACHE["last_updated"] = datetime.now()
        print(f"[Cache] 갱신 완료 — Jira {len(CACHE['jira'])}건 / Confluence {len(CACHE['confluence'])}건")
    except Exception as e:
        print(f"[Cache] 갱신 실패: {e}")
    finally:
        CACHE["loading"] = False


async def _auto_refresh_loop() -> None:
    """1시간마다 캐시 자동 갱신."""
    while True:
        await asyncio.sleep(CACHE_TTL_SECONDS)
        print(f"[Cache] 자동 갱신 시작 ({datetime.now().strftime('%H:%M:%S')})")
        await _load_cache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 시작 시 캐시 로드 + 자동 갱신 루프 시작
    asyncio.create_task(_load_cache())
    asyncio.create_task(_auto_refresh_loop())
    yield


# ── App (L249~585) ────────────────────────────────────────────────────────────
# FastAPI 앱 생성 + lifespan + 기본 API
# GET  /  /hub  (HTML 서빙)
# GET  /api/status  /api/recent_bugs  /api/refresh
# GET  /api/search  /api/weekly_bugs  /api/live_issues  /api/tc_progress

app = FastAPI(title="QA Search", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/hub", response_class=HTMLResponse)
async def hub():
    return HTMLResponse((STATIC_DIR / "hub_v8.html").read_text(encoding="utf-8"))


@app.get("/presentation", response_class=HTMLResponse)
async def presentation():
    return HTMLResponse((STATIC_DIR / "presentation.html").read_text(encoding="utf-8"))


@app.get("/api/status")
async def api_status():
    return JSONResponse({
        "loading": CACHE["loading"],
        "jira_count": len(CACHE["jira"]),
        "confluence_count": len(CACHE["confluence"]),
        "game_count": len(CACHE["game_code_map"]),
        "ready": CACHE["last_updated"] is not None,
        "last_updated": (
            CACHE["last_updated"].strftime("%Y-%m-%d %H:%M:%S")
            if CACHE["last_updated"] else None
        ),
    })


@app.get("/api/recent_bugs")
async def api_recent_bugs():
    """최근 3일 내 등록된 Critical(주요) Bug 이슈 반환."""
    cutoff = (date.today() - timedelta(days=3)).isoformat()
    bugs = [
        {k: v for k, v in issue.items() if not k.startswith("_")}
        for issue in CACHE["jira"]
        if issue.get("priority") == "주요"
        and issue.get("type") == "Bug"
        and (issue.get("created") or "") >= cutoff
    ]
    return JSONResponse(bugs)


@app.post("/api/refresh")
async def api_refresh():
    """수동 캐시 갱신."""
    asyncio.create_task(_load_cache())
    return JSONResponse({"message": "캐시 갱신을 시작했습니다."})


@app.get("/api/search")
async def api_search(q: str, sources: str = "all"):
    if not q.strip():
        return JSONResponse({"jira": [], "confluence": [], "drive": []})

    query = q.strip()
    loop = asyncio.get_event_loop()

    jira_results = search_jira_local(CACHE["jira"], query)

    # sources=jira : Jira만 반환 (대시보드 이슈카운트용, Drive/MCP 없음)
    # sources=fast : Jira+Confluence+Slack 반환 (패널 1단계용, Drive/MCP 없음)
    if sources in ("jira", "fast"):
        def clean(item: dict) -> dict:
            return {k: v for k, v in item.items() if not k.startswith("_")}
        conf = search_confluence_local(CACHE["confluence"], query) if sources == "fast" else []
        slack = search_slack_channels(query) if sources == "fast" else []
        return JSONResponse({
            "jira": [clean(r) for r in jira_results],
            "confluence": [clean(r) for r in conf],
            "slack": slack,
            "drive": [],
        })

    conf_results = search_confluence_local(CACHE["confluence"], query)
    slack_results = search_slack_channels(query)
    # Drive 검색 + 온톨로지 병렬 실행
    def _ontology_drive_sources(q: str) -> List[dict]:
        sources = []
        seen_ids: set = set()

        def _add_game_sources(game_name: str):
            try:
                raw = call_mcp_tool("get_game", {"game_name": game_name})
                if not raw:
                    return
                game_data = json.loads(raw)
                if not game_data or game_data.get("error"):
                    return
                srcs = game_data.get("sources", {})
                qa = srcs.get("qa")
                if qa and qa.get("drive_id") and qa["drive_id"] not in seen_ids:
                    seen_ids.add(qa["drive_id"])
                    sources.append({
                        "id": qa["drive_id"],
                        "title": qa.get("doc_name", "QA 문서"),
                        "mime_label": "QA 시트",
                        "url": f"https://docs.google.com/spreadsheets/d/{qa['drive_id']}",
                        "from_ontology": True,
                    })
                design = srcs.get("design_doc")
                if design and design.get("drive_id") and design["drive_id"] not in seen_ids:
                    seen_ids.add(design["drive_id"])
                    folder_name = design.get("folder_name") or game_data.get("game_name") or "기획 문서"
                    sources.append({
                        "id": design["drive_id"],
                        "title": folder_name,
                        "mime_label": "기획 폴더",
                        "url": f"https://drive.google.com/drive/folders/{design['drive_id']}",
                        "from_ontology": True,
                    })
            except Exception:
                pass

        def _norm(s: str) -> str:
            """& ↔ and, _ → 공백, 아포스트로피 → 공백 정규화 (SB_게임명, Luck'n'Roll 대응)."""
            return s.replace(" & ", " and ").replace("&", "and").replace("_", " ").replace("'", " ").replace("'", " ")

        token = _norm(q.strip().lower())

        # 1. 정확한 game_code 매칭
        exact_code = CACHE["game_code_map"].get(token)
        if exact_code:
            _add_game_sources(exact_code)
            return sources  # 코드 매칭이면 하나만 반환

        # 2. game_code_map에서 쿼리가 게임명에 포함되는 게임 부분 매칭 (최대 4개)
        tokens = token.split()
        matched_names = [
            name for name in CACHE["game_code_map"].values()
            if all(t in _norm(name.lower()) for t in tokens)
        ]
        # 게임명 앞부분에 쿼리가 매칭되는 게임 우선 정렬
        matched_names.sort(key=lambda n: (0 if _norm(n.lower()).startswith(token) else 1, n))
        for name in matched_names[:4]:
            _add_game_sources(name)

        return sources

    drive_results, ontology_drive = await asyncio.gather(
        loop.run_in_executor(_executor, drive_search_mcp, query),
        loop.run_in_executor(_executor, _ontology_drive_sources, query),
    )
    # 온톨로지 결과가 있으면 맨 앞에, Drive 검색 결과에서 중복 ID 제거
    if ontology_drive:
        ontology_ids = {s["id"] for s in ontology_drive}
        drive_results = ontology_drive + [r for r in drive_results if r.get("id") not in ontology_ids]

    def clean(item: dict) -> dict:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    return JSONResponse({
        "jira": [clean(r) for r in jira_results],
        "confluence": [clean(r) for r in conf_results],
        "slack": slack_results,
        "drive": drive_results,
    })


@app.get("/api/weekly_bugs")
async def api_weekly_bugs():
    """이번 주(월요일 00:00 ~ 현재) 등록된 버그 티켓 반환. 최대 5건, 최신순."""
    from datetime import date, timedelta
    today = date.today()
    days_since_monday = today.weekday()  # 월=0, 일=6
    monday = today - timedelta(days=days_since_monday)
    monday_str = monday.isoformat()

    _PRIORITY_SEV_MAP = {
        "주요": ("c", "Critical"),
        "Highest": ("c", "Critical"),
        "highest": ("c", "Critical"),
        "High": ("mj", "Major"),
        "high": ("mj", "Major"),
        "medium": ("mj", "Major"),
        "Medium": ("mj", "Major"),
        "사소": ("mn", "Minor"),
        "Low": ("mn", "Minor"),
        "low": ("mn", "Minor"),
        "Lowest": ("mn", "Minor"),
        "lowest": ("mn", "Minor"),
    }
    BUG_TYPES = {"버그", "Bug", "bug"}

    issues = CACHE.get("jira", [])
    results = []
    for item in issues:
        if item.get("type") not in BUG_TYPES:
            continue
        created = item.get("created", "")
        if not created or created < monday_str:
            continue
        priority = item.get("priority", "-")
        sev, sev_label = _PRIORITY_SEV_MAP.get(priority, ("mn", "Minor"))
        results.append({
            "key": item["key"],
            "summary": item["summary"],
            "game": item.get("game", ""),
            "is_sb": item.get("is_sb", False),
            "status": item.get("status", "-"),
            "assignee": item.get("assignee", "-"),
            "created": created,
            "priority": priority,
            "sev": sev,
            "sev_label": sev_label,
            "url": item["url"],
        })

    results.sort(key=lambda x: x["created"], reverse=True)
    total = len(results)
    open_count = sum(1 for r in results if r["status"] not in ("완료", "Done", "해결됨", "Resolved", "Closed"))
    resolved_count = total - open_count
    return JSONResponse({
        "issues": results[:5],
        "total": total,
        "open": open_count,
        "resolved": resolved_count,
        "week_start": monday_str,
    })


@app.get("/api/live_issues")
async def api_live_issues(year: int, month: Optional[int] = None):
    """GS-Live 이슈 반환. month 생략 시 연도 전체."""
    _li_key = (year, month)
    if _li_key in _LI_CACHE:
        _li_cached, _li_ts = _LI_CACHE[_li_key]
        if (datetime.now() - _li_ts).total_seconds() < _LI_CACHE_TTL:
            return JSONResponse(_li_cached)

    loop = asyncio.get_event_loop()
    issues = await loop.run_in_executor(
        _executor, lambda: fetch_live_issues(year, month)
    )
    counts = {"c": 0, "mj": 0, "mn": 0}
    month_data = {str(m): {"c": 0, "mj": 0, "mn": 0} for m in range(1, 13)}
    for i in issues:
        sev = i["sev"]
        counts[sev] = counts.get(sev, 0) + 1
        mo = str(i.get("month", 0))
        if mo in month_data:
            month_data[mo][sev] += 1
    result = {
        "issues": issues,
        "total": len(issues),
        "critical": counts["c"],
        "major": counts["mj"],
        "minor": counts["mn"],
        "month_data": month_data,
    }
    _LI_CACHE[_li_key] = (result, datetime.now())
    return JSONResponse(result)


_LI_CACHE: dict = {}   # (year, month) -> (result, timestamp)
_LI_CACHE_TTL = 3600   # 1시간

_TC_CACHE: dict = {}   # (sheet_id, game_type) -> (result, timestamp)
_TC_CACHE_TTL = 300    # 5분

_GL_CACHE: dict = {}   # (name, tc_prefix, game_id, is_sb) -> (result, timestamp)
_GL_CACHE_TTL = 600    # 10분


def _read_tc_sheet(sheet_id: str, game_type: str = "") -> Optional[dict]:
    """gws CLI로 QA 시트에서 TC 진행률 읽기.
    game_type='sb' 이면 Overall 시트 row5(Super Bonus)만 반환.
    결과는 5분간 메모리 캐시.
    """
    cache_key = (sheet_id, game_type)
    if cache_key in _TC_CACHE:
        cached_result, cached_ts = _TC_CACHE[cache_key]
        if (datetime.now() - cached_ts).total_seconds() < _TC_CACHE_TTL:
            return cached_result

    try:
        result = subprocess.run(
            ["gws", "sheets", "+read",
             "--spreadsheet", sheet_id,
             "--range", "Overall!C33:L42",
             "--format", "json"],
            capture_output=True, text=True, timeout=20
        )
        stdout = result.stdout
        json_start = stdout.find("{")
        if json_start < 0:
            return None
        try:
            data = json.loads(stdout[json_start:])
        except json.JSONDecodeError as e:
            print(f"[TC] JSON 파싱 오류: {e}")
            return None
        values = data.get("values", [])

        total_pct = values[1][2] if len(values) > 1 and len(values[1]) > 2 else "0%"

        def parse_row(row):
            if not row or len(row) < 9:
                return None
            def to_int(s):
                try:
                    return int(s)
                except Exception:
                    return 0
            return {
                "pass": to_int(row[2]),
                "fail": to_int(row[3]),
                "no_run": to_int(row[4]),
                "na": to_int(row[5]),
                "block": to_int(row[6]),
                "total": to_int(row[7]),
                "progress": row[8] if len(row) > 8 else "0%",
            }

        if game_type == "sb":
            sb_row = parse_row(values[5]) if len(values) > 5 else None
            result = {
                "total_progress": total_pct,
                "super_bonus": sb_row,
            }
            _TC_CACHE[cache_key] = (result, datetime.now())
            return result

        result = {
            "total_progress": total_pct,
            "basic": parse_row(values[5]) if len(values) > 5 else None,
            "content": parse_row(values[6]) if len(values) > 6 else None,
        }
        _TC_CACHE[cache_key] = (result, datetime.now())
        return result
    except Exception as e:
        print(f"[TC] 시트 읽기 실패 ({sheet_id}): {e}")
        return None


@app.get("/api/tc_progress")
async def api_tc_progress(sheet_id: str, game_type: str = ""):
    """QA 시트에서 TC 진행률 반환. game_type=sb 이면 Super Bonus 단일 행만 반환."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _read_tc_sheet, sheet_id, game_type)
    if result is None:
        return JSONResponse({"error": "읽기 실패"}, status_code=500)
    return JSONResponse(result)


# ── Schedule / Game Links (L590~1185) ─────────────────────────────────────────
# [스케줄 헬퍼] _schedule_path, _compute_status, _fetch_game_titles
# [게임 문서]   GET /api/game_titles  /api/game_lookup  /api/game_links  /api/game_studio
#               _drive_search_sheet, _autofill_sheet_id
# [스케줄 CRUD] GET/POST/PUT/DELETE /api/schedule

SLOT_SHEET_ID = "1ENqN1xSqOvaid38Wpld0Ma91em-L5EgPNZOUQTTWJGQ"

_MONTH_MAP = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5,
              "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10,
              "Nov": 11, "Dec": 12}


def _schedule_path() -> Path:
    return Path(__file__).parent / "schedule.json"


def _events_path() -> Path:
    return Path(__file__).parent / "events.json"


def _memos_path() -> Path:
    return Path(__file__).parent / "memos.json"


def _read_memos() -> List[dict]:
    p = _memos_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_memos(memos: List[dict]) -> None:
    _memos_path().write_text(json.dumps(memos, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_events() -> List[dict]:
    p = _events_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_events(data: List[dict]) -> None:
    _events_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_schedule() -> List[dict]:
    p = _schedule_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_schedule(data: List[dict]) -> None:
    _schedule_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _compute_status(entry: dict, today: date) -> str:
    """stored status + 날짜 기반으로 표시 상태 계산."""
    stored = entry.get("status", "active")
    if stored in ("hold", "done", "extended"):
        return stored
    try:
        qa_start = date.fromisoformat(entry["qa_start"])
        qa_end = date.fromisoformat(entry["qa_end"])
    except (KeyError, ValueError):
        return "active"
    tc_end_str = entry.get("tc_end", "")
    tc_end = date.fromisoformat(tc_end_str) if tc_end_str else None

    if today < qa_start:
        return "pending"
    if today > qa_end:
        return "needs_action"
    if not tc_end or today <= tc_end:
        return "tc"
    return "testing"


_GAME_TITLES_CACHE: dict = {"data": None, "ts": None}
_GAME_TITLES_TTL = 600  # 10분


def _fetch_game_titles() -> List[dict]:
    """PM 슬롯 시트 D열(TITLE)에서 New Game/Super Bonus 목록을 월별로 반환.
    결과는 10분간 메모리 캐시.
    """
    if _GAME_TITLES_CACHE["data"] is not None and _GAME_TITLES_CACHE["ts"] is not None:
        if (datetime.now() - _GAME_TITLES_CACHE["ts"]).total_seconds() < _GAME_TITLES_TTL:
            return _GAME_TITLES_CACHE["data"]

    try:
        result = subprocess.run(
            ["gws", "sheets", "+read",
             "--spreadsheet", SLOT_SHEET_ID,
             "--range", f"{datetime.now().year} Schedule!A3:BW100",
             "--format", "json"],
            capture_output=True, text=True, timeout=20
        )
        stdout = result.stdout
        json_start = stdout.find("{")
        if json_start < 0:
            return []
        try:
            data = json.loads(stdout[json_start:])
        except json.JSONDecodeError as e:
            print(f"[GameTitles] JSON 파싱 오류: {e}")
            return []
        rows = data.get("values", [])
        if not rows:
            return []

        # 행 0 = 헤더(APP/TYPE/...), 행 1부터 데이터
        # BF열 = 인덱스 57. 행 0의 인덱스 57부터가 월/일 헤더
        header_row = rows[0]  # APP, TYPE, #, TITLE, ...
        # 월/일 헤더는 rows 자체가 "{year} Schedule!A3:BW100" 기준으로
        # 첫 행이 헤더(APP…), 이후가 데이터 — 단 월/일 정보는 별도로 읽어야 함
        # 이미 파악한 구조: BF+(0~17) = Jan/5 ~ May/4
        month_labels = ["Jan","","","","Feb","","","","Mar","","","","","Apr","","","","May"]
        day_labels =   ["5","12","19","26","2","9","16","23","2","9","16","23","30","3","13","20","27","4"]
        year = datetime.now().year
        date_list: List[Optional[date]] = []
        cur_month = "Jan"
        for i, day in enumerate(day_labels):
            if i < len(month_labels) and month_labels[i]:
                cur_month = month_labels[i]
            try:
                date_list.append(date(year, _MONTH_MAP[cur_month], int(day)))
            except Exception:
                date_list.append(None)

        results = []
        for row in rows[1:]:  # 헤더 행 제외
            type_ = row[1].strip() if len(row) > 1 else ""
            if type_ not in ("New Game", "Super Bonus"):
                continue
            title = row[3].strip() if len(row) > 3 else ""
            state = row[16].strip() if len(row) > 16 else ""
            if not title or state in ("DONE", "HOLD"):
                continue

            # BF+ 셀에서 첫 텍스트 = 시작 월
            sched_cells = row[57:] if len(row) > 57 else []
            start_month = ""
            assignee_hint = ""
            for j, cell in enumerate(sched_cells):
                if cell.strip() and j < len(date_list) and date_list[j]:
                    assignee_hint = cell.strip()
                    d = date_list[j]
                    start_month = d.strftime("%b") if d else ""
                    break

            results.append({
                "title": title,
                "type": type_,
                "month": start_month,
                "assignee_hint": assignee_hint,
            })
        _GAME_TITLES_CACHE["data"] = results
        _GAME_TITLES_CACHE["ts"] = datetime.now()
        return results
    except Exception as e:
        print(f"[GameTitles] 시트 읽기 실패: {e}")
        return []


@app.get("/api/game_titles")
async def api_game_titles():
    """PM 슬롯 시트에서 게임 목록 반환 (드롭다운용)."""
    loop = asyncio.get_event_loop()
    titles = await loop.run_in_executor(_executor, _fetch_game_titles)
    return JSONResponse(titles)


@app.get("/api/game_lookup")
async def api_game_lookup(name: str):
    """게임명으로 game_id, game_code 조회 (contents sheet 기반).
    패널에서 GAMES 객체에 없는 게임의 ID/코드를 동적으로 채울 때 사용.
    """
    entry = CACHE.get("sheet_games", {}).get(name.strip().lower())
    if not entry:
        return JSONResponse({"error": "not found"}, status_code=404)
    tc_prefix = ""
    if entry.get("game_code") and "/" in entry["game_code"]:
        tc_prefix = entry["game_code"].split("/")[-1].lower()
    return JSONResponse({
        "game_id": entry["game_id"],
        "game_code": entry["game_code"],
        "tc_prefix": tc_prefix,
        "game_name": entry["game_name"],
        "game_type": entry["game_type"],
    })


@app.get("/api/game_links")
async def api_game_links(name: str, tc_prefix: str = "", game_id: str = "", is_sb: str = "", fast: str = ""):
    """게임 패널용 문서 링크 반환 (GDD, MATH, Sound, 연출, CTD).
    fast=1 이면 캐시 기반 항목(CTD, SOUND, VFX)만 즉시 반환 — GDD/MATH Drive 검색 생략.
    """

    def _norm_tab(s: str) -> str:
        """탭/파일명 정규화: 소문자, 아포스트로피·하이픈·언더스코어 제거, 공백 단일화."""
        s = s.lower()
        s = s.replace("\u2019", "").replace("\u2018", "").replace("'", "")
        s = s.replace("-", " ").replace("_", " ")
        s = s.replace(" & ", " and ").replace("&", "and")
        return " ".join(s.split())

    def _search_keywords(name_str: str, prefix: str) -> List[str]:
        """Drive 검색에 시도할 키워드 목록 (우선순위 순, 중복 제거)."""
        seen: List[str] = []
        def _add(k: str) -> None:
            k = k.strip()
            if k and k not in seen:
                seen.append(k)

        if prefix:
            _add(prefix.upper())
        # 아포스트로피 분리 → 가장 긴 파트 (Luck'n'Roll → Roll Wheels)
        normalized = name_str.replace("\u2019", "'").replace("\u2018", "'")
        parts = normalized.split("'")
        longest = max(parts, key=len).strip()
        if longest and longest.lower() != name_str.lower():
            _add(longest)
        # & → and
        _add(name_str.replace(" & ", " and ").replace("&", "and"))
        # 원본
        _add(name_str)
        return seen

    sb = bool(is_sb and is_sb != "0")

    # 10분 캐시 (같은 게임 재방문 시 즉시 응답)
    _gl_key = (name, tc_prefix, game_id, is_sb)
    if _gl_key in _GL_CACHE:
        _gl_cached, _gl_ts = _GL_CACHE[_gl_key]
        if (datetime.now() - _gl_ts).total_seconds() < _GL_CACHE_TTL:
            return JSONResponse(_gl_cached)

    def _list_in_folder(folder_id: str, keyword: str) -> List[dict]:
        """폴더 내에서 keyword를 포함하는 파일/폴더 목록 반환."""
        safe = keyword.replace("'", "").replace("\u2019", "").replace("\u2018", "").strip()
        if not safe:
            return []
        q = f"name contains '{safe}' and '{folder_id}' in parents and trashed=false"
        params = json.dumps({
            "q": q,
            "fields": "files(id,name,mimeType)",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "corpora": "allDrives",
            "pageSize": 10,
            "orderBy": "name",
        })
        try:
            r = subprocess.run(
                ["gws", "drive", "files", "list", "--params", params, "--format", "json"],
                capture_output=True, text=True, timeout=10
            )
            return json.loads(r.stdout).get("files", [])
        except Exception:
            return []

    def _drive_search_in_folders(keyword: str, folder_ids: List[str]) -> Optional[str]:
        """지정 폴더 내에서 keyword로 파일/폴더 검색.
        - 폴더 hit 시:
          - SB 게임이면 폴더 안에서 'SB_' 파일 우선 검색, 없으면 폴더 URL
          - 일반 게임이면 폴더 URL 반환 (서브폴더 구조 대응)
        - 파일 hit 시 파일 URL 반환.
        """
        safe = keyword.replace("'", "").replace("\u2019", "").replace("\u2018", "").strip()
        if not safe:
            return None
        folder_mime = "application/vnd.google-apps.folder"
        for folder_id in folder_ids:
            files = _list_in_folder(folder_id, safe)
            if not files:
                continue
            # SB 게임: 서브폴더 안에 SB_ 파일이 있는지 확인 후 폴더 URL 반환
            if sb:
                for f in files:
                    if f.get("mimeType") == folder_mime:
                        sb_files = _list_in_folder(f["id"], "SB_")
                        if sb_files:
                            # SB_ 파일이 있는 서브폴더 URL 반환 (viewer 방지)
                            return f"https://drive.google.com/open?id={f['id']}"
                # SB_ 탐색 실패 시 폴더 URL fallback
                for f in files:
                    if f.get("mimeType") == folder_mime:
                        return f"https://drive.google.com/open?id={f['id']}"
            else:
                # 일반: 폴더 hit → 폴더 URL
                for f in files:
                    if f.get("mimeType") == folder_mime:
                        return f"https://drive.google.com/open?id={f['id']}"
            # 폴더 없으면 파일
            non_folders = [f for f in files if f.get("mimeType") != folder_mime]
            if non_folders:
                return f"https://drive.google.com/open?id={non_folders[0]['id']}"
        return None

    result: dict = {"gdd": None, "math": None, "sound": None, "direction": None, "ctd": None}
    name_lower = name.lower().strip()

    # tc_prefix가 없으면 sheet_games 캐시에서 자동 보완
    resolved_prefix = tc_prefix.strip()
    if not resolved_prefix:
        sheet_entry = (
            CACHE.get("sheet_games", {}).get(name_lower)
            or CACHE.get("sheet_games", {}).get(_norm_tab(name))
        )
        if sheet_entry and sheet_entry.get("game_code"):
            code = sheet_entry["game_code"]
            resolved_prefix = code.split("/")[-1].lower() if "/" in code else code.lower()

    loop = asyncio.get_event_loop()

    async def _find_gdd():
        for kw in _search_keywords(name, resolved_prefix):
            url = await loop.run_in_executor(
                _executor, lambda k=kw: _drive_search_in_folders(k, _GDD_FOLDER_IDS)
            )
            if url:
                result["gdd"] = url
                return

    async def _find_math():
        for kw in _search_keywords(name, resolved_prefix):
            url = await loop.run_in_executor(
                _executor, lambda k=kw: _drive_search_in_folders(k, [_MATH_FOLDER_ID])
            )
            if url:
                result["math"] = url
                return

    # ── Sound: tc_prefix로 탭 매칭 ────────────────────────────────────────
    sound_tabs = CACHE.get("sound_tabs", {})
    if resolved_prefix:
        sound_gid = sound_tabs.get(resolved_prefix.lower())
        if sound_gid is not None:
            result["sound"] = (
                f"https://docs.google.com/spreadsheets/d/{_SOUND_SHEET_ID}"
                f"/edit#gid={sound_gid}"
            )

    # ── 연출: 게임명으로 탭 매칭 (정규화 기반 퍼지 매칭) ────────────────────
    direction_tabs = CACHE.get("direction_tabs", {})
    norm_name = _norm_tab(name)
    direction_gid: Optional[int] = None
    best_score = -1
    for tab_name, gid in direction_tabs.items():
        norm_tab = _norm_tab(tab_name)
        if norm_tab == norm_name:          # 완전 일치
            direction_gid = gid
            break
        # 포함 관계 — 더 긴 매칭 우선
        if norm_name in norm_tab or norm_tab in norm_name:
            score = min(len(norm_name), len(norm_tab))
            if score > best_score:
                best_score = score
                direction_gid = gid
    if direction_gid is not None:
        result["direction"] = (
            f"https://docs.google.com/spreadsheets/d/{_DIRECTION_SHEET_ID}"
            f"/edit#gid={direction_gid}"
        )

    # CTD Game Info: game_id > game_name > tc_code 순으로 매칭
    ctd_info = CACHE.get("ctd_game_info", [])
    game_id_int: Optional[int] = None
    if game_id:
        try:
            game_id_int = int(game_id)
        except ValueError:
            pass
    tc_code = resolved_prefix.lower() if resolved_prefix else ""
    ctd_row: Optional[dict] = None
    for row in ctd_info:
        matched = False
        if game_id_int is not None and row["game_id_str"].isdigit():
            if int(row["game_id_str"]) == game_id_int:
                matched = True
        if not matched and row["game_name"].lower().strip() == name_lower:
            matched = True
        if not matched and tc_code and tc_code in row["game_title"].lower():
            matched = True
        if matched:
            ctd_row = row
            break
    if ctd_row:
        result["ctd"] = (
            f"https://docs.google.com/spreadsheets/d/{_CONTENTS_SHEET_ID}"
            f"/edit#gid={_CTD_GAME_INFO_GID}&range=B{ctd_row['row_num']}"
        )
        # game_title에서 스튜디오 추출 (e.g. "SS/JGQ" → "SS", "DS/..." → "DS")
        gt = ctd_row.get("game_title", "")
        studio = gt.split("/")[0].strip().upper() if "/" in gt else gt.strip().upper()
        if studio in ("SS", "DS"):
            result["studio"] = studio

    # fast=1 이면 GDD/MATH Drive 검색 생략하고 즉시 반환
    if fast == "1":
        return JSONResponse(result)

    # GDD/MATH 병렬 실행
    await asyncio.gather(_find_gdd(), _find_math())

    _GL_CACHE[_gl_key] = (result, datetime.now())
    return JSONResponse(result)


@app.get("/api/game_studio")
async def api_game_studio(name: str = "", tc_prefix: str = "", game_id: str = ""):
    """CTD 캐시에서 스튜디오(SS/DS) 빠르게 반환 (Drive 검색 없음)."""
    ctd_info = CACHE.get("ctd_game_info", [])
    name_lower = name.lower().strip()
    game_id_int: Optional[int] = None
    if game_id:
        try:
            game_id_int = int(game_id)
        except ValueError:
            pass
    tc_code = tc_prefix.lower() if tc_prefix else ""
    for row in ctd_info:
        matched = False
        if game_id_int is not None and row["game_id_str"].isdigit():
            if int(row["game_id_str"]) == game_id_int:
                matched = True
        if not matched and row["game_name"].lower().strip() == name_lower:
            matched = True
        if not matched and tc_code and tc_code in row["game_title"].lower():
            matched = True
        if matched:
            gt = row.get("game_title", "")
            studio = gt.split("/")[0].strip().upper() if "/" in gt else gt.strip().upper()
            if studio in ("SS", "DS"):
                return JSONResponse({"studio": studio})
            break
    return JSONResponse({})


def _drive_search_sheet(keywords: List[str]) -> Optional[str]:
    """GWS CLI로 공유 드라이브에서 스프레드시트 검색, 첫 번째 결과 ID 반환."""
    q_parts = [f"name contains '{kw}'" for kw in keywords]
    q_parts.append("mimeType='application/vnd.google-apps.spreadsheet'")
    q = " and ".join(q_parts)
    params = json.dumps({
        "q": q,
        "fields": "files(id,name)",
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
        "corpora": "allDrives",
    })
    try:
        result = subprocess.run(
            ["gws", "drive", "files", "list", "--params", params, "--format", "json"],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout[result.stdout.find("{"):])
        files = data.get("files", [])
        return files[0]["id"] if files else None
    except Exception:
        return None


def _autofill_sheet_id(entry: dict) -> None:
    """qa_sheet_id가 비어 있으면 자동 조회해서 채운다.
    SB 게임: GWS Drive 검색 (공유 드라이브 포함)
    일반 게임: get_game MCP → 실패 시 GWS Drive 검색 fallback
    """
    if entry.get("qa_sheet_id"):
        return
    game_name = entry.get("game_name", "").replace("SB_", "").strip()
    if not game_name:
        return
    is_sb = entry.get("type") == "Super Bonus"

    if is_sb:
        # SB 게임: Drive에서 게임명 키워드 + SB로 검색
        words = [w for w in re.split(r"[^a-zA-Z0-9가-힣]", game_name) if len(w) > 3][:2]
        sheet_id = _drive_search_sheet(words + ["SB"])
        if sheet_id:
            entry["qa_sheet_id"] = sheet_id
        return

    # 일반 게임: MCP get_game 우선
    try:
        raw = call_mcp_tool("get_game", {"game_name": game_name})
        if raw:
            data = json.loads(raw)
            if data and not data.get("error"):
                qa = data.get("sources", {}).get("qa")
                if qa and qa.get("drive_id"):
                    entry["qa_sheet_id"] = qa["drive_id"]
                    return
    except Exception:
        pass

    # fallback: Drive 검색
    words = [w for w in re.split(r"[^a-zA-Z0-9가-힣]", game_name) if len(w) > 3][:2]
    sheet_id = _drive_search_sheet(words)
    if sheet_id:
        entry["qa_sheet_id"] = sheet_id


@app.get("/api/schedule")
async def api_schedule_get():
    """QA 일정 목록 반환 (computed_status 포함, qa_sheet_id 자동 조회)."""
    entries = _read_schedule()
    today = date.today()
    loop = asyncio.get_event_loop()
    missing = [e for e in entries if not e.get("qa_sheet_id")]
    if missing:
        await asyncio.gather(*[
            loop.run_in_executor(_executor, _autofill_sheet_id, e)
            for e in missing
        ])
    for e in entries:
        e["computed_status"] = _compute_status(e, today)
    return JSONResponse(entries)


class ScheduleEntry(BaseModel):
    game_name: str
    type: str
    assignee: str
    qa_start: str
    qa_end: str
    tc_end: Optional[str] = ""
    status: str = "active"
    memo: Optional[str] = ""


@app.post("/api/schedule")
async def api_schedule_post(body: ScheduleEntry):
    """QA 일정 신규 등록."""
    entries = _read_schedule()
    new_entry = {
        "id": str(uuid.uuid4()),
        "game_name": body.game_name,
        "type": body.type,
        "assignee": body.assignee,
        "qa_start": body.qa_start,
        "qa_end": body.qa_end,
        "tc_end": body.tc_end or "",
        "status": body.status,
        "memo": body.memo or "",
        "created_at": date.today().isoformat(),
    }
    entries.append(new_entry)
    _write_schedule(entries)
    new_entry["computed_status"] = _compute_status(new_entry, date.today())
    return JSONResponse(new_entry)


class ScheduleUpdate(BaseModel):
    game_name: Optional[str] = None
    type: Optional[str] = None
    assignee: Optional[str] = None
    qa_start: Optional[str] = None
    qa_end: Optional[str] = None
    tc_end: Optional[str] = None
    status: Optional[str] = None
    memo: Optional[str] = None


@app.put("/api/schedule/{entry_id}")
async def api_schedule_put(entry_id: str, body: ScheduleUpdate):
    """QA 일정 수정 (부분 업데이트)."""
    entries = _read_schedule()
    for e in entries:
        if e.get("id") == entry_id:
            for field in ("game_name", "type", "assignee", "qa_start", "qa_end", "tc_end", "status", "memo"):
                val = getattr(body, field)
                if val is not None:
                    e[field] = val
            _write_schedule(entries)
            e["computed_status"] = _compute_status(e, date.today())
            return JSONResponse(e)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/api/schedule/{entry_id}")
async def api_schedule_delete(entry_id: str):
    """QA 일정 삭제."""
    entries = _read_schedule()
    new_entries = [e for e in entries if e.get("id") != entry_id]
    if len(new_entries) == len(entries):
        return JSONResponse({"error": "not found"}, status_code=404)
    _write_schedule(new_entries)
    return JSONResponse({"ok": True})


# ── Events (L1189~1250) ───────────────────────────────────────────────────────
# 사내 행사 CRUD — GET/POST/PUT/DELETE /api/events

class EventEntry(BaseModel):
    title: str
    start: str
    end: str
    color: Optional[str] = "#7c3aed"


class EventUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    color: Optional[str] = None


@app.get("/api/events")
async def api_events_get():
    return JSONResponse(_read_events())


@app.post("/api/events")
async def api_events_post(body: EventEntry):
    events = _read_events()
    new_event = {
        "id": str(uuid.uuid4()),
        "title": body.title,
        "start": body.start,
        "end": body.end,
        "color": body.color or "#7c3aed",
        "created_at": date.today().isoformat(),
    }
    events.append(new_event)
    _write_events(events)
    return JSONResponse(new_event)


@app.put("/api/events/{event_id}")
async def api_events_put(event_id: str, body: EventUpdate):
    events = _read_events()
    for e in events:
        if e.get("id") == event_id:
            for field in ("title", "start", "end", "color"):
                val = getattr(body, field)
                if val is not None:
                    e[field] = val
            _write_events(events)
            return JSONResponse(e)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/api/events/{event_id}")
async def api_events_delete(event_id: str):
    events = _read_events()
    new_events = [e for e in events if e.get("id") != event_id]
    if len(new_events) == len(events):
        return JSONResponse({"error": "not found"}, status_code=404)
    _write_events(new_events)
    return JSONResponse({"ok": True})


# ── Chart Memos (L1251~1285) ──────────────────────────────────────────────────
# 슬롯별 날짜 메모 CRUD — GET/POST/DELETE /api/memos

@app.get("/api/memos")
async def api_memos_get(game: str = ""):
    memos = _read_memos()
    if game:
        memos = [m for m in memos if m.get("game") == game]
    return JSONResponse(memos)


class MemoEntry(BaseModel):
    game: str
    date: str   # YYYY-MM-DD
    text: str


@app.post("/api/memos")
async def api_memos_post(body: MemoEntry):
    memos = _read_memos()
    # upsert: 같은 game + date 이면 덮어쓰기
    memos = [m for m in memos if not (m.get("game") == body.game and m.get("date") == body.date)]
    if body.text.strip():
        memos.append({"game": body.game, "date": body.date, "text": body.text.strip()})
    _write_memos(memos)
    return JSONResponse({"ok": True})


@app.delete("/api/memos")
async def api_memos_delete(game: str, date: str):
    memos = _read_memos()
    new_memos = [m for m in memos if not (m.get("game") == game and m.get("date") == date)]
    _write_memos(new_memos)
    return JSONResponse({"ok": True})


# ── Korean Holidays (L1286~1320) ──────────────────────────────────────────────
# 공휴일 조회 — GET /api/holidays  (공공데이터포털 API 연동)

_HOLIDAY_KO = {
    "New Year's Day": "새해",
    "The day preceding Korean New Year": "설날 전날",
    "Korean New Year": "설날",
    "The second day of Korean New Year": "설날 다음날",
    "Independence Movement Day": "삼일절",
    "Alternative holiday for Independence Movement Day": "삼일절 대체공휴일",
    "Children's Day": "어린이날",
    "Alternative holiday for Children's Day": "어린이날 대체공휴일",
    "Buddha's Birthday": "부처님오신날",
    "Alternative holiday for Buddha's Birthday": "부처님오신날 대체공휴일",
    "Memorial Day": "현충일",
    "Liberation Day": "광복절",
    "Alternative holiday for Liberation Day": "광복절 대체공휴일",
    "The day preceding Chuseok": "추석 전날",
    "Chuseok": "추석",
    "The second day of Chuseok": "추석 다음날",
    "National Foundation Day": "개천절",
    "Alternative holiday for National Foundation Day": "개천절 대체공휴일",
    "Hangul Day": "한글날",
    "Alternative holiday for Hangul Day": "한글날 대체공휴일",
    "Christmas Day": "크리스마스",
    "Local Election Day": "지방선거일",
}

@app.get("/api/holidays")
async def api_holidays(year: int):
    kr = holidays_lib.Korea(years=year)
    result = {d.isoformat(): _HOLIDAY_KO.get(name, name) for d, name in sorted(kr.items())}
    return JSONResponse(result)


# ── Chat / GPT (L1321~END) ────────────────────────────────────────────────────
# [헬퍼]   _fmt_jira_sources, _fmt_conf_sources, _extract_search_keywords
#           _is_stats_query, _is_dict_query, _fuzzy_find_game
#           _is_game_list_query, _extract_tags_from_message, _build_game_list_context
# [태그맵] 한글→온톨로지 태그 매핑 딕셔너리 (L1461~)
# [GPT]    _classify_intent_gpt, _process_chat (의도분석 + 검색 + 응답생성)
# [API]    POST /api/chat  POST /api/chat/stream

CHAT_SESSIONS: dict = {}  # session_id -> List[{"role": "user"|"assistant", "content": str}]

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None



def _fmt_jira_sources(issues: List[dict]) -> List[dict]:
    return [{"type": "jira", "key": i.get("key", ""), "title": i.get("summary", ""), "url": i.get("url", ""), "status": i.get("status", "")} for i in issues[:5]]


def _fmt_conf_sources(pages: List[dict]) -> List[dict]:
    return [{"type": "confluence", "title": p.get("title", ""), "url": p.get("url", "")} for p in pages[:5]]


# 한국어 조사/어미 패턴 (토큰 끝에서 제거)
_KO_PARTICLES = re.compile(
    r"(가|이|은|는|을|를|와|과|에서|에게|한테|에|의|으로|로|도|만|까지|부터"
    r"|이야|야|이에요|에요|이죠|죠|이지|지|이냐|냐|인가|인가요|인지"
    r"|있지|없지|있냐|있나|있나요|있어|없어|뭐있지|이뭐|뭔지)$"
)
_CHAT_STOP_WORDS = {
    "알려줘", "알려주세요", "뭐야", "뭐에요", "뭐있지", "어때", "어때요",
    "해줘", "해주세요", "보여줘", "보여주세요", "찾아줘", "찾아주세요",
    "궁금해", "궁금한데", "어떤", "관련", "혹시", "좀", "한번", "최근",
    "있어요", "없어요", "알고싶어", "뭐가", "무슨", "어디", "언제", "왜",
    "설명해줘", "설명해주세요", "설명", "소개해줘", "소개해주세요", "소개",
    "알아봐줘", "분석해줘", "정리해줘", "요약해줘", "대해", "대한",
    "알고싶은데", "궁금한게", "뭔지", "뭐야", "뭔가요",
}

def _extract_search_keywords(message: str) -> str:
    """채팅 메시지에서 검색용 핵심 키워드만 추출."""
    tokens = message.strip().lower().split()
    keywords = []
    for t in tokens:
        t = t.rstrip("?!？！~")
        if not t:
            continue
        if t in _CHAT_STOP_WORDS:
            continue
        # 한국어 조사/어미 제거
        t = _KO_PARTICLES.sub("", t)
        if not t:
            continue
        # 1글자 단독 한글 제외
        if len(t) == 1 and re.match(r"^[가-힣]$", t):
            continue
        keywords.append(t)
    return " ".join(keywords) if keywords else message.strip()


# 게임 목록/포트폴리오 쿼리 감지용 키워드
_GAME_LIST_TRIGGERS = {
    "목록", "리스트", "어떤게임", "무슨게임", "전체", "모든", "포트폴리오",
    "개발된", "개발중인", "개발중", "출시된", "라이브", "서비스중", "서비스중인",
    "종료된", "종료", "운영중", "운영중인", "신규", "최신", "신작",
    "슬롯게임", "테이블게임", "포커게임", "아케이드", "인스턴트",
}

# 포트폴리오 통계 쿼리 감지
_STATS_TRIGGERS = {
    "통계", "현황", "몇개", "몇 개", "얼마나", "비율", "분포",
    "집계", "요약", "overview", "stats", "summary",
}


def _is_stats_query(message: str) -> bool:
    """게임 포트폴리오 통계를 묻는 쿼리인지 판단."""
    msg_no_space = message.replace(" ", "").lower()
    for trigger in _STATS_TRIGGERS:
        if trigger in msg_no_space:
            return True
    if any(w in message for w in ["게임이 몇", "게임 몇", "총 몇", "총 게임"]):
        return True
    return False


# 용어 사전 쿼리 감지
_DICT_TRIGGERS = {"뜻", "의미", "정의", "무슨뜻", "뭐야", "뭔가요", "뭔지", "용어", "뭐죠", "설명해"}


def _is_dict_query(message: str, keyword_list: List[str]) -> bool:
    """특정 용어의 뜻/정의를 묻는 쿼리인지 판단."""
    msg_no_space = message.replace(" ", "")
    for trigger in _DICT_TRIGGERS:
        if trigger in msg_no_space:
            return True
    return False


def _fuzzy_find_game(query_words: List[str]) -> Optional[str]:
    """CACHE["game_list"]에서 query_words를 게임명/코드에 서브스트링 매칭해 최적 게임명 반환."""
    if not query_words or not CACHE.get("game_list"):
        return None
    # 불용어 제거
    stop = {"game", "the", "a", "an", "of", "and", "games"}
    words = [w.lower() for w in query_words
             if w.lower() not in stop and len(w) > 1]
    if not words:
        return None
    best_name = None
    best_score = 0.0
    for g in CACHE["game_list"]:
        name = (g.get("game_name") or "").lower()
        code = (g.get("game_code") or "").lower()
        score = 0.0
        for w in words:
            # 게임명 시작 매칭: 가장 높은 가중치 (예: "money"→"moneyki neko")
            if name.startswith(w) or code == w:
                score += 3.0
            # 단어 경계 매칭 (예: "neko"→"moneyki neko")
            elif f" {w}" in f" {name} ":
                score += 2.0
            # 코드 포함 (예: "mkn" in code)
            elif w in code:
                score += 2.0
            # 내부 서브스트링: 낮은 가중치
            elif w in name:
                score += 0.5
        if score > best_score:
            best_score = score
            best_name = g.get("game_name")
    return best_name if best_score >= 2.0 else None


def _is_game_list_query(message: str, keyword_list: List[str]) -> bool:
    """특정 게임 한 개가 아닌, 게임 목록/포트폴리오를 묻는 쿼리인지 판단."""
    msg_no_space = message.replace(" ", "")
    # 트리거 단어가 있으면 True
    for trigger in _GAME_LIST_TRIGGERS:
        if trigger in msg_no_space:
            return True
    # "게임" + 수량/기간 표현
    if "게임" in message and any(w in message for w in ["몇개", "몇 개", "개월", "년", "분기", "반기"]):
        return True
    return False


# ── 한글 → 온톨로지 태그 매핑 ──────────────────────────────────────
_KO_TAG_MAP: Dict[str, str] = {
    "팟": "pot_game", "팟게임": "pot_game",
    "프리스핀": "free_spin", "프리 스핀": "free_spin",
    "리스핀": "respin", "리 스핀": "respin",
    "홀드앤스핀": "hold_and_spin", "홀드 앤 스핀": "hold_and_spin",
    "보너스바이": "bonus_buy", "보너스 바이": "bonus_buy",
    "잭팟": "jackpot", "잭팟게임": "jackpot",
    "멀티플라이어": "multiplier",
    "스캐터": "scatter_pay",
    "웨이": "way_pay", "웨이즈": "way_pay",
    "라인페이": "line_pay", "라인 페이": "line_pay",
    "캐스케이드": "cascade",
    "익스팬딩": "expanding", "익스팬딩 그리드": "expanding_grid",
    "너지": "nudge",
    "미스터리": "mystery_symbol",
    "캐시온릴": "cash_on_reels", "캐시 온 릴": "cash_on_reels",
    "프로그레션": "progression",
    "멀티레벨": "multi_level",
    "리트리거": "retriggerable",
    "다이나믹": "dynamic_row_unlock",
    "크래시": "crash", "크래시게임": "crash",
    "케노": "keno",
    "포커": "video_poker",
    "인스턴트": "instant_win",
}

def _extract_tags_from_message(message: str) -> List[str]:
    """메시지에서 온톨로지 태그 추출 (한글/영어 모두 지원)."""
    tags: List[str] = []
    msg_lower = message.lower().replace(" ", "")
    # 한글 매핑
    for ko, tag in _KO_TAG_MAP.items():
        if ko.replace(" ", "") in msg_lower and tag not in tags:
            tags.append(tag)
    # 영어 태그 직접 언급
    known_tags = [
        "pot_game", "free_spin", "respin", "hold_and_spin", "bonus_buy",
        "jackpot", "multiplier", "scatter_pay", "way_pay", "line_pay",
        "cascade", "expanding", "expanding_grid", "nudge", "mystery_symbol",
        "cash_on_reels", "progression", "retriggerable", "dynamic_row_unlock",
    ]
    msg_raw = message.lower()
    for tag in known_tags:
        if tag in msg_raw and tag not in tags:
            tags.append(tag)
    return tags


def _build_game_list_context(query: str, tags: Optional[List[str]] = None, status_filter: Optional[str] = None) -> str:
    """game_list 캐시 + search_games 동적 호출 결과를 합쳐 GPT용 컨텍스트 문자열 반환.
    tags: 온톨로지 태그 필터 (있으면 해당 태그 보유 게임만 반환)
    status_filter: 상태 필터 (예: 'released', 'in_dev')
    """
    # 동적 검색: 추출된 키워드로 search_games 호출
    dynamic_games: List[dict] = []
    try:
        raw = call_mcp_tool("search_games", {"query": query, "tags": tags or []})
        if raw:
            dynamic_games = json.loads(raw).get("results", [])
    except Exception:
        pass

    # 캐시 목록과 동적 결과 병합 (동적 결과 우선, 중복 제거)
    seen_ids: set = set()
    merged: List[dict] = []
    for g in dynamic_games + CACHE["game_list"]:
        gid = g.get("game_id") or g.get("game_name", "")
        if gid and gid not in seen_ids:
            seen_ids.add(gid)
            merged.append(g)

    # 태그 필터 적용 (캐시 게임에도 적용)
    if tags:
        def _has_tags(g: dict) -> bool:
            raw_tags = g.get("tags", [])
            if not raw_tags:
                return False
            if isinstance(raw_tags[0], dict):
                game_tags = {t["tag"] for t in raw_tags}
            else:
                game_tags = {str(t) for t in raw_tags}
            return any(tag in game_tags for tag in tags)
        merged = [g for g in merged if _has_tags(g)]

    # 상태 필터 적용
    if status_filter:
        merged = [g for g in merged if (g.get("status") or "").lower() == status_filter.lower()]

    if not merged:
        return ""

    lines = []
    for g in merged:
        name = g.get("game_name", "")
        code = g.get("game_code", "")
        status = g.get("status", "")
        game_type = g.get("game_type", "")
        raw_tags = g.get("tags", [])
        if raw_tags and isinstance(raw_tags[0], dict):
            tag_names = [t["tag"] for t in raw_tags if t.get("confidence") == "high"]
        else:
            tag_names = [str(t) for t in raw_tags]
        launched = g.get("launched_at") or g.get("launch_date") or g.get("created_at") or ""
        parts = [name]
        if code:
            parts.append(f"코드:{code}")
        if game_type:
            parts.append(f"유형:{game_type}")
        if status:
            parts.append(f"상태:{status}")
        if launched:
            parts.append(f"출시:{launched}")
        if tag_names:
            parts.append(f"태그:{','.join(tag_names[:5])}")
        lines.append(" | ".join(parts))

    tag_desc = f" (태그 필터: {', '.join(tags)})" if tags else ""
    status_desc = f" (상태: {status_filter})" if status_filter else ""
    return f"## 게임 목록 (온톨로지){tag_desc}{status_desc}\n" + "\n".join(lines)


def _classify_intent_gpt(message: str) -> dict:
    """GPT로 메시지 의도/게임명/키워드 추출. 빠른 단일 호출."""
    import os
    from openai import OpenAI
    # 게임 목록 컨텍스트 (최대 400개, name|code 형식)
    game_list_ctx = ""
    if CACHE.get("game_list"):
        names = [f"{g.get('game_name','')}|{g.get('game_code','')}"
                 for g in CACHE["game_list"] if g.get("game_name")]
        game_list_ctx = "\n게임 목록 (정확한 영문 게임명|코드):\n" + ", ".join(names[:400])
    try:
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": (
                f'메시지: "{message}"\n\n'
                'JSON으로만 응답 (추가 텍스트 없이):\n'
                '{"intent":"game_lookup|comparison|stats|list|tag_filter|dict|general",'
                '"game_names":[],"keywords":[],"tags":[],"status_filter":""}\n\n'
                'intent 기준:\n'
                '- game_lookup: 특정 게임 1개 조회\n'
                '- comparison: 두 게임 비교\n'
                '- stats: 통계/개수/현황/얼마나\n'
                '- list: 전체 게임 목록 (특정 조건 없는 경우)\n'
                '- tag_filter: 태그/장르/기능 기반 게임 목록 ("팟 게임", "free spin 있는 게임", "잭팟 게임들" 등)\n'
                '- dict: 용어 뜻/정의/의미\n'
                '- general: 기타 (버그, Jira, 문서 등)\n\n'
                'game_names: 언급된 게임명 배열 (최대 2개). '
                '아래 게임 목록에서 가장 유사한 정확한 영문 게임명을 찾아 반환. '
                '목록에 없으면 번역해서 반환.\n'
                'keywords: 핵심 검색 키워드 (최대 5개, 조사 제거)\n'
                'tags: tag_filter intent일 때 관련 온톨로지 태그 배열 (예: ["pot_game"], ["free_spin","bonus_buy"]). '
                '가능한 태그: pot_game, free_spin, respin, hold_and_spin, bonus_buy, jackpot, multiplier, '
                'scatter_pay, way_pay, line_pay, cascade, expanding_grid, nudge, mystery_symbol, '
                'cash_on_reels, progression, retriggerable, dynamic_row_unlock\n'
                'status_filter: 상태 필터 (released/in_dev/in_qa 중 하나, 없으면 빈 문자열)'
                + game_list_ctx
            )}],
            max_tokens=200,
            temperature=0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return {"intent": "general", "game_names": [], "keywords": []}


def _build_comparison_ctx(game1_name: str, game2_name: str) -> tuple:
    """두 게임을 병렬 조회해 비교 컨텍스트와 소스 반환. (ctx_str, sources)"""
    def _fetch(name: str) -> Optional[dict]:
        raw = call_mcp_tool("get_game", {"game_name": name})
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return d if d.get("game_name") else None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as _p:
        f1, f2 = _p.submit(_fetch, game1_name), _p.submit(_fetch, game2_name)
        try:
            g1 = f1.result(timeout=10)
        except Exception:
            g1 = None
        try:
            g2 = f2.result(timeout=10)
        except Exception:
            g2 = None

    if not g1 and not g2:
        return "", []

    sources = []

    def _fmt_game(g: dict) -> str:
        lines = []
        name = g.get("game_name", "")
        gid = g.get("game_id", "")
        code = g.get("game_code", "")
        lines.append(f"게임명: {name} (ID: {gid}, 코드: {code})")
        if g.get("game_type"):
            lines.append(f"유형: {g['game_type']}")
        if g.get("status"):
            lines.append(f"상태: {g['status']}")
        gen = (g.get("client_meta") or {}).get("generation", "")
        if gen:
            lines.append(f"세대: {gen}")
        tags = [t["tag"] for t in g.get("tags", []) if isinstance(t, dict) and t.get("confidence") == "high"]
        if tags:
            lines.append(f"태그: {', '.join(tags[:10])}")
        qa = (g.get("sources", {}).get("qa") or {})
        if qa.get("drive_id"):
            url = f"https://docs.google.com/spreadsheets/d/{qa['drive_id']}"
            lines.append(f"QA 문서: {qa.get('doc_name', 'QA')} ({url})")
            sources.append({"type": "ontology", "title": f"{name} QA", "url": url})
        design = (g.get("sources", {}).get("design_doc") or {})
        if design.get("drive_id"):
            url = f"https://drive.google.com/drive/folders/{design['drive_id']}"
            lines.append(f"기획 문서: {design.get('folder_name', name)} ({url})")
            sources.append({"type": "ontology", "title": f"{name} 기획", "url": url})
        return "\n".join(lines)

    parts = []
    if g1:
        parts.append(f"### {g1.get('game_name', game1_name)}\n{_fmt_game(g1)}")
    else:
        parts.append(f"### {game1_name}\n정보 없음")
    if g2:
        parts.append(f"### {g2.get('game_name', game2_name)}\n{_fmt_game(g2)}")
    else:
        parts.append(f"### {game2_name}\n정보 없음")

    ctx = "## 게임 비교 (온톨로지)\n" + "\n\n".join(parts)
    return ctx, sources


def _process_chat(message: str, history: List[dict], _return_prepared: bool = False) -> tuple:
    """채팅 메시지 처리. (answer, sources, intent) 반환.
    _return_prepared=True이면 GPT 호출 없이 (messages, sources, intent) 반환.
    """
    import os
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    # ── 0. GPT 의도 분류 백그라운드 시작 (데이터 수집과 병렬) ──
    _intent_pool = ThreadPoolExecutor(max_workers=1)
    _intent_future = _intent_pool.submit(_classify_intent_gpt, message)

    # ── 1. 키워드 추출 (현재 메시지 + 이전 대화 맥락 보완) ──
    keyword_str = _extract_search_keywords(message)
    keyword_list = keyword_str.split()

    # 이전 대화에서 언급된 게임명 추출
    # 1) assistant 답변에서 "게임명:" 패턴 우선
    # 2) 없으면 이전 user 메시지 키워드로 fallback
    _last_game_name = None
    for h in reversed(history[:-1]):
        if h.get("role") == "assistant":
            m = re.search(r"게임명\s*:\s*([^\n(,]+)", h["content"])
            if m:
                _last_game_name = m.group(1).strip()
                break
    if not _last_game_name:
        for h in reversed(history[:-1]):
            if h.get("role") == "user":
                prev_kw = _extract_search_keywords(h["content"])
                if prev_kw and prev_kw != keyword_str:
                    _last_game_name = prev_kw
                    break

    # 현재 메시지 키워드가 적으면 이전 맥락으로 보완
    if len(keyword_list) < 2 and _last_game_name:
        extra = [k for k in _last_game_name.split() if k not in keyword_list]
        keyword_list = extra[:3] + keyword_list
        keyword_str = " ".join(keyword_list)

    def _union_search_jira(kws: List[str]) -> List[dict]:
        seen = set()
        results = []
        for kw in kws:
            for item in search_jira_local(CACHE["jira"], kw):
                key = item.get("key", "")
                if key not in seen:
                    seen.add(key)
                    results.append(item)
        return results

    def _union_search_conf(kws: List[str]) -> List[dict]:
        seen = set()
        results = []
        for kw in kws:
            for item in search_confluence_local(CACHE["confluence"], kw):
                url = item.get("url", item.get("title", ""))
                if url not in seen:
                    seen.add(url)
                    results.append(item)
        return results

    # ── 2. gs-os 온톨로지에서 게임 정보 조회 (먼저 실행해서 키워드 보완) ──
    game_ctx = ""
    game_raw = None

    def _resolve_game_name(candidate: str) -> Optional[str]:
        """게임 코드나 약어를 게임명으로 변환. 못 찾으면 None."""
        code = candidate.lower()
        # game_code_map에서 코드로 직접 매칭
        if code in CACHE["game_code_map"]:
            return CACHE["game_code_map"][code]
        return None

    def _try_get_game(name: str) -> Optional[str]:
        raw = call_mcp_tool("get_game", {"game_name": name})
        if raw:
            try:
                d = json.loads(raw)
                if d and not d.get("error") and d.get("game_name"):
                    return raw
            except Exception:
                pass
        # MCP 실패 시 contents sheet fallback
        entry = CACHE.get("sheet_games", {}).get(name.lower())
        if entry:
            return json.dumps({
                "game_name": entry["game_name"],
                "game_id": str(entry["game_id"]),
                "game_code": entry["game_code"],
                "game_type": entry["game_type"],
            })
        return None

    try:
        tokens = keyword_list
        # 각 토큰에 대해 game_code 매칭 먼저 시도 (단일 토큰)
        for tok in tokens:
            resolved = _resolve_game_name(tok)
            if resolved:
                game_raw = _try_get_game(resolved)
                if game_raw:
                    break

        # game_code로 못 찾으면 전체 → 점점 줄여가며 game_name으로 시도 (2토큰 이상)
        if not game_raw:
            for size in range(len(tokens), 1, -1):
                for start in range(len(tokens) - size + 1):
                    candidate = " ".join(tokens[start:start + size])
                    game_raw = _try_get_game(candidate)
                    if game_raw:
                        break
                if game_raw:
                    break

        # 단일 토큰도 game_name으로 시도 (코드 매칭 실패 후)
        if not game_raw:
            for tok in tokens:
                game_raw = _try_get_game(tok)
                if game_raw:
                    break

        # 현재 메시지에서 못 찾았으면 이전 대화 게임으로 재시도
        if not game_raw and _last_game_name:
            resolved = _resolve_game_name(_last_game_name)
            game_raw = _try_get_game(resolved or _last_game_name)
            if game_raw:
                # 게임이 컨텍스트에서 복원됐으면 검색 키워드도 보완
                base_name = resolved or _last_game_name
                game_tokens = [t for t in base_name.lower().split() if t not in keyword_list]
                keyword_list = game_tokens + keyword_list
                keyword_str = " ".join(keyword_list)
    except Exception as _e:
        print(f"[Chat] 게임 검색 오류: {_e}")

    # ── GPT 의도 분류 결과 수집 ──
    try:
        _intent_data = _intent_future.result(timeout=4)
    except Exception:
        _intent_data = {"intent": "general", "game_names": [], "keywords": []}
    finally:
        _intent_pool.shutdown(wait=False)

    _intent_type = _intent_data.get("intent", "general")
    _gpt_game_names = _intent_data.get("game_names", []) or []
    _gpt_keywords = _intent_data.get("keywords", []) or []
    _gpt_tags = _intent_data.get("tags", []) or []
    _gpt_status_filter = _intent_data.get("status_filter", "") or ""

    # GPT 키워드로 보완
    if _gpt_keywords:
        kw_lower_set = {k.lower() for k in keyword_list}
        for kw in _gpt_keywords:
            if kw.lower() not in kw_lower_set:
                keyword_list.append(kw)
        keyword_str = " ".join(keyword_list)

    # GPT가 뽑은 게임명으로 재시도 (아직 못 찾은 경우)
    if not game_raw and _gpt_game_names:
        for _gname in _gpt_game_names:
            game_raw = _try_get_game(_gname)
            if game_raw:
                break

    # 캐시 퍼지 폴백: GPT 번역도 실패한 경우 game_list에서 서브스트링 매칭
    if not game_raw and _intent_type in ("game_lookup", "comparison"):
        # GPT 번역 단어 + 원본 키워드 합쳐서 검색
        _fuzzy_words = []
        for _gn in _gpt_game_names:
            _fuzzy_words.extend(_gn.split())
        _fuzzy_words.extend(keyword_list)
        try:
            _found_name = _fuzzy_find_game(_fuzzy_words)
            if _found_name:
                game_raw = _try_get_game(_found_name)
                if game_raw:
                    print(f"[Chat] 퍼지 폴백 성공: {_fuzzy_words} → '{_found_name}'", flush=True)
        except Exception as e:
            print(f"[Chat] 퍼지 폴백 실패: {e}", flush=True)

    # ── 게임 비교 처리 ──
    comparison_ctx = ""
    comparison_sources: List[dict] = []
    is_comparison = (_intent_type == "comparison" and len(_gpt_game_names) >= 2)
    if is_comparison:
        try:
            comparison_ctx, comparison_sources = _build_comparison_ctx(_gpt_game_names[0], _gpt_game_names[1])
        except Exception:
            is_comparison = False

    # 태그 필터: GPT 분류 결과 + 메시지에서 직접 추출 (중복 제거)
    _extracted_tags = _extract_tags_from_message(message)
    _filter_tags: List[str] = list({*_gpt_tags, *_extracted_tags})
    _filter_status = _gpt_status_filter or ""

    # 게임 목록/통계/사전 쿼리 여부 판단 (GPT 의도 우선, 규칙 보조)
    # 이슈/버그 관련 질문이면 태그 필터 비활성화 (잭팟 버그 패턴 → 키워드 검색으로 처리)
    _is_issue_context = any(w in message for w in ["버그", "bug", "이슈", "issue", "패턴", "pattern", "오류", "에러", "결함", "크래시", "crash"])
    is_tag_filter = not game_raw and not is_comparison and not _is_issue_context and (
        _intent_type == "tag_filter" or bool(_filter_tags)
    )
    is_list_query = not game_raw and not is_comparison and not is_tag_filter and (
        _intent_type == "list" or _is_game_list_query(message, keyword_list)
    )
    # 상태 필터만 있는 경우도 list로 처리
    if not is_list_query and not is_tag_filter and not game_raw and _filter_status:
        is_list_query = True
    is_stats = (_intent_type == "stats" or _is_stats_query(message))
    is_dict = (_intent_type == "dict" or _is_dict_query(message, keyword_list))
    print(f"[Chat] intent={_intent_type}, game_raw={'있음' if game_raw else '없음'}, comparison={is_comparison}, tag_filter={is_tag_filter}(tags={_filter_tags}), list={is_list_query}, stats={is_stats}, dict={is_dict}", flush=True)

    # ── 3. Jira / Confluence / Slack 검색 ──
    # 게임이 찾아진 경우 게임명으로만 검색 (노이즈 방지)
    # 목록 쿼리인 경우 키워드 검색 스킵 (노이즈 방지)
    if game_raw:
        try:
            _found_game_name = json.loads(game_raw).get("game_name", "")
        except Exception:
            _found_game_name = ""
        search_kws = _found_game_name.lower().split() if _found_game_name else keyword_list
    elif is_list_query:
        search_kws = []
    elif is_tag_filter:
        # 태그 필터 목록에서 게임명 추출해서 Jira 검색 키워드로 사용
        # (게임 목록이 아직 빌드되기 전이므로 캐시에서 직접 추출)
        _tag_game_names: List[str] = []
        for _g in CACHE.get("game_list", []):
            _raw_tags = _g.get("tags", [])
            if _raw_tags and isinstance(_raw_tags[0], dict):
                _game_tags = {t["tag"] for t in _raw_tags}
            else:
                _game_tags = {str(t) for t in _raw_tags}
            if any(tag in _game_tags for tag in _filter_tags):
                _gname = _g.get("game_name", "")
                if _gname:
                    _tag_game_names.append(_gname)
        # 게임명 키워드로 Jira 검색 (최대 10개 게임명의 첫 단어만)
        search_kws = list({n.split()[0].lower() for n in _tag_game_names[:10] if n.split()})
    else:
        search_kws = keyword_list

    # 게임이 찾아진 경우: game_name 전체를 AND 검색 (단어별 OR union은 노이즈 과다)
    if game_raw and search_kws:
        game_query = " ".join(search_kws)
        jira = search_jira_local(CACHE["jira"], game_query)
        conf = search_confluence_local(CACHE["confluence"], game_query)
    else:
        jira = _union_search_jira(search_kws) if search_kws else []
        conf = _union_search_conf(search_kws) if search_kws else []
    print(f"[Chat] search_kws={search_kws}, Jira={len(jira)}건, Conf={len(conf)}건")
    try:
        slack = search_slack_channels(" ".join(search_kws)) if search_kws else []
    except Exception:
        slack = []

    sources: List[dict] = _fmt_jira_sources(jira) + _fmt_conf_sources(conf)

    try:
        if game_raw:
            game_data = json.loads(game_raw)
            if game_data and not game_data.get("error"):
                lines = []
                name = game_data.get("game_name", "")
                game_id = game_data.get("game_id", "")
                game_code = game_data.get("game_code", "")
                if name:
                    id_str = f" (ID: {game_id}, 코드: {game_code})" if game_id else ""
                    lines.append(f"게임명: {name}{id_str}")
                game_type = game_data.get("game_type", "")
                if game_type:
                    lines.append(f"유형: {game_type}")
                status = game_data.get("status", "")
                if status:
                    lines.append(f"상태: {status}")
                gen = (game_data.get("client_meta") or {}).get("generation", "")
                if gen:
                    lines.append(f"세대: {gen}")
                raw_tags = game_data.get("tags", [])
                tag_names = [t["tag"] for t in raw_tags if isinstance(t, dict) and t.get("confidence") == "high"]
                if tag_names:
                    lines.append(f"태그: {', '.join(tag_names)}")
                sources_data = game_data.get("sources", {})
                qa_info = sources_data.get("qa")
                if qa_info and qa_info.get("drive_id"):
                    doc_name = qa_info.get("doc_name", "QA 문서")
                    drive_url = f"https://docs.google.com/spreadsheets/d/{qa_info['drive_id']}"
                    lines.append(f"QA 문서: {doc_name} ({drive_url})")
                    sources.insert(0, {"type": "ontology", "title": doc_name, "url": drive_url})
                design_info = sources_data.get("design_doc")
                if design_info and design_info.get("drive_id"):
                    folder_name = design_info.get("folder_name") or name or "기획 문서"
                    drive_url = f"https://drive.google.com/drive/folders/{design_info['drive_id']}"
                    lines.append(f"기획 문서: {folder_name} ({drive_url})")
                    sources.insert(1, {"type": "ontology", "title": folder_name, "url": drive_url})
                # 스튜디오 (SS/DS) - CTD 캐시에서 빠르게 조회
                try:
                    _ctd_info = CACHE.get("ctd_game_info", [])
                    _gname_lower = name.lower().strip()
                    _tc_code = game_code.lower() if game_code else ""
                    for _row in _ctd_info:
                        _matched = False
                        if game_id and _row.get("game_id_str", "").isdigit():
                            if int(_row["game_id_str"]) == int(game_id):
                                _matched = True
                        if not _matched and _row.get("game_name", "").lower().strip() == _gname_lower:
                            _matched = True
                        if not _matched and _tc_code and _tc_code in _row.get("game_title", "").lower():
                            _matched = True
                        if _matched:
                            _gt = _row.get("game_title", "")
                            _studio = _gt.split("/")[0].strip().upper() if "/" in _gt else _gt.strip().upper()
                            if _studio in ("SS", "DS"):
                                lines.append(f"스튜디오: {_studio}")
                            break
                except Exception:
                    pass
                # 유사 게임 조회
                if game_id:
                    try:
                        sim_raw = call_mcp_tool("similar_games", {"game_id": int(game_id), "top_n": 5})
                        if sim_raw:
                            sim_data = json.loads(sim_raw)
                            sim_list = sim_data.get("results") or sim_data.get("games") or sim_data if isinstance(sim_data, list) else []
                            sim_names = [g.get("game_name", "") for g in sim_list if g.get("game_name")]
                            if sim_names:
                                lines.append(f"유사 게임: {', '.join(sim_names)}")
                    except Exception:
                        pass
                if lines:
                    game_ctx = "## 게임 정보 (온톨로지)\n" + "\n".join(lines)
    except Exception:
        pass

    # 게임 목록 쿼리: 온톨로지 전체 목록을 GPT context에 주입
    game_list_ctx = ""
    if is_tag_filter:
        try:
            game_list_ctx = _build_game_list_context(keyword_str, tags=_filter_tags, status_filter=_filter_status or None)
            print(f"[Chat] 태그 필터 context 생성: tags={_filter_tags}, status={_filter_status}")
        except Exception as _e:
            print(f"[Chat] 태그 필터 context 오류: {_e}")
    elif is_list_query:
        try:
            game_list_ctx = _build_game_list_context(keyword_str, status_filter=_filter_status or None)
            print(f"[Chat] 게임 목록 context 생성: {len(CACHE['game_list'])}개 기반")
        except Exception as _e:
            print(f"[Chat] 게임 목록 context 오류: {_e}")

    # ── 4. 추가 컨텍스트 병렬 조회: TC, 라이브 이슈, Confluence 본문, 코드 검색 ──
    def _get_tc_ctx() -> str:
        if not game_raw:
            return ""
        try:
            gd = json.loads(game_raw)
            qa_did = (gd.get("sources", {}).get("qa") or {}).get("drive_id")
            if not qa_did:
                return ""
            tc = _read_tc_sheet(qa_did)
            if not tc:
                return ""
            lines = [f"TC 전체 진행률: {tc.get('total_progress', '-')}"]
            if tc.get("basic"):
                b = tc["basic"]
                lines.append(f"기본 TC: Pass {b['pass']} / Fail {b['fail']} / No Run {b['no_run']} / 진행률 {b['progress']}")
            if tc.get("content"):
                c = tc["content"]
                lines.append(f"컨텐츠 TC: Pass {c['pass']} / Fail {c['fail']} / No Run {c['no_run']} / 진행률 {c['progress']}")
            return "## TC 진행률\n" + "\n".join(lines)
        except Exception:
            return ""

    def _get_live_ctx() -> str:
        if not game_raw:
            return ""
        try:
            gd = json.loads(game_raw)
            gname = gd.get("game_name", "")
            if not gname:
                return ""
            year = datetime.now().year
            live_issues = fetch_live_issues(year)
            gname_lower = gname.lower()
            game_live = [i for i in live_issues if gname_lower in (i.get("game") or "").lower()]
            if not game_live:
                return ""
            sev_counts: dict = {"c": 0, "mj": 0, "mn": 0}
            for i in game_live:
                s = i["sev"]
                sev_counts[s] = sev_counts.get(s, 0) + 1
            lines = [f"총 {len(game_live)}건 (Critical: {sev_counts['c']}, Major: {sev_counts['mj']}, Minor: {sev_counts['mn']})"]
            for i in game_live[:5]:
                lines.append(f"- [{i['key']}] {i['summary']} ({i['sev_label']}, {i['status']})")
            return f"## 라이브 이슈 ({year}년)\n" + "\n".join(lines)
        except Exception:
            return ""

    def _get_conf_body_ctx() -> str:
        if not conf:
            return ""
        parts = []
        for page in conf[:3]:
            body = fetch_confluence_page_body(page["id"])
            if body:
                parts.append(f"### {page.get('title', '')}\n{body[:600]}")
        return ("## Confluence 본문\n" + "\n\n".join(parts)) if parts else ""

    def _get_repob_ctx() -> str:
        if not game_raw:
            return ""
        try:
            gd = json.loads(game_raw)
            gname = (gd.get("game_name") or "").strip()
            slug = gname.replace(" ", "-").lower()  # e.g. "blazing-triplex"
            if not slug:
                return ""
            repob_bin = "/Users/kimhyewon/.claude/plugins/marketplaces/bagel-marketplace/plugins/repob/skills/repob/bin/repob"
            if not Path(repob_bin).exists():
                return ""
            # 1. games 레포에서 게임 브랜치 찾기
            search_result = subprocess.run(
                [repob_bin, "search", slug, "--pretty"],
                capture_output=True, text=True, timeout=5,
            )
            target_branch = None
            if search_result.returncode == 0 and search_result.stdout.strip():
                sdata = json.loads(search_result.stdout)
                for sug in sdata.get("suggestions", []):
                    if sug.get("project_name") == "games" and sug.get("type") == "branch":
                        target_branch = sug.get("value")
                        break
            if not target_branch:
                return ""
            # 2. 해당 브랜치에서 grep
            grep_result = subprocess.run(
                [repob_bin, "grep", "games", target_branch, slug, "--pretty"],
                capture_output=True, text=True, timeout=6,
            )
            if grep_result.returncode != 0 or not grep_result.stdout.strip():
                return ""
            data = json.loads(grep_result.stdout)
            matches = data.get("matches") or []
            if not matches:
                return ""
            match_lines = []
            for m in matches[:5]:
                path = m.get("file") or ""
                lineno = m.get("line") or ""
                content = (m.get("text") or "").strip()[:80]
                match_lines.append(f"- `{path}` L{lineno} — {content}")
            branch_short = target_branch.split("/")[-1]
            return (f"## 코드 참조 (games/{branch_short})\n" + "\n".join(match_lines)) if match_lines else ""
        except Exception:
            return ""

    def _get_resolve_query_ctx() -> str:
        try:
            raw = call_mcp_tool("resolve_query", {"query": keyword_str})
            if not raw:
                return ""
            data = json.loads(raw)
            tags = data.get("tags") or data.get("results") or data if isinstance(data, list) else []
            if not tags:
                return ""
            tag_strs = []
            for t in tags[:8]:
                if isinstance(t, dict):
                    tag_strs.append(t.get("tag") or t.get("name") or str(t))
                elif isinstance(t, str):
                    tag_strs.append(t)
            return ("## 관련 태그 (온톨로지)\n" + ", ".join(tag_strs)) if tag_strs else ""
        except Exception:
            return ""

    def _get_portfolio_stats_ctx() -> str:
        if not is_stats:
            return ""
        lines = []
        try:
            for group_by in ("game_type", "generation"):
                raw = call_mcp_tool("portfolio_stats", {"group_by": group_by})
                if not raw:
                    continue
                data = json.loads(raw)
                dist = data.get("distribution") or []
                if not dist:
                    continue
                if not lines:
                    lines.append(f"전체 게임 수: {data.get('total_games', '?')}")
                    lines.append("※ 라이브/서비스 상태 정보는 이 API에서 제공되지 않습니다.")
                label = "유형별" if group_by == "game_type" else "세대별"
                parts = [f"{d['key']} {d['count']}개({d['percentage']}%)" for d in dist[:5]]
                lines.append(f"{label}: " + ", ".join(parts))
        except Exception:
            pass
        return ("## 포트폴리오 통계 (온톨로지)\n" + "\n".join(lines)) if lines else ""

    def _get_dictionary_ctx() -> str:
        if not is_dict:
            return ""
        try:
            raw = call_mcp_tool("get_dictionary", {})
            if not raw:
                return ""
            data = json.loads(raw)
            entries = data if isinstance(data, list) else data.get("entries") or data.get("results") or []
            if not entries:
                return ""
            kws_lower = [k.lower() for k in keyword_list]
            matched = [
                e for e in entries
                if any(
                    kw in (e.get("term") or e.get("name") or e.get("word") or "").lower()
                    or (e.get("term") or e.get("name") or e.get("word") or "").lower() in kw
                    for kw in kws_lower if len(kw) > 1
                )
            ]
            if not matched:
                matched = entries[:5]
            lines = []
            for e in matched[:8]:
                term = e.get("term") or e.get("name") or e.get("word") or ""
                defi = e.get("definition") or e.get("description") or e.get("desc") or e.get("meaning") or ""
                if term and defi:
                    lines.append(f"- **{term}**: {defi}")
            return ("## 용어 사전 (온톨로지)\n" + "\n".join(lines)) if lines else ""
        except Exception:
            return ""

    _SCHED_KWS = ["일정", "스케줄", "schedule", "qa 기간", "qa기간", "언제", "담당게임", "담당 게임",
                  "마감", "qa 시작", "다음 qa", "출시 일정", "릴리즈"]
    _EVENT_KWS = ["이벤트", "event", "행사", "휴가", "공휴일", "휴일"]
    _is_schedule_query = any(kw in message for kw in _SCHED_KWS)
    _is_event_query = any(kw in message for kw in _EVENT_KWS)
    _is_bug_query = any(w in message for w in ["버그", "bug", "Bug"])

    def _get_schedule_ctx() -> str:
        if not _is_schedule_query:
            return ""
        try:
            entries = _read_schedule()
            if not entries:
                return ""
            from datetime import date as _date
            _today_str = _date.today().isoformat()
            lines = []
            for e in sorted(entries, key=lambda x: x.get("qa_start", ""))[:15]:
                gname = e.get("game_name", "")
                assignee = e.get("assignee", "")
                qa_start = e.get("qa_start", "")
                qa_end = e.get("qa_end", "")
                status = e.get("computed_status") or e.get("status", "")
                memo = e.get("memo", "")
                line = f"- {gname} ({assignee}) QA기간: {qa_start}~{qa_end} [{status}]"
                if memo:
                    line += f" 메모: {memo}"
                lines.append(line)
            return ("## QA 스케줄\n" + "\n".join(lines)) if lines else ""
        except Exception:
            return ""

    def _get_events_ctx() -> str:
        if not _is_event_query:
            return ""
        try:
            events = _read_events()
            if not events:
                return ""
            lines = []
            for ev in sorted(events, key=lambda x: x.get("date", ""))[:10]:
                title = ev.get("title", "")
                ev_date = ev.get("date", "")
                memo = ev.get("memo", "")
                line = f"- {ev_date} {title}"
                if memo:
                    line += f" ({memo})"
                lines.append(line)
            return ("## 이벤트/행사\n" + "\n".join(lines)) if lines else ""
        except Exception:
            return ""

    def _get_memos_ctx() -> str:
        if not game_raw:
            return ""
        try:
            gd = json.loads(game_raw)
            gname = gd.get("game_name", "")
            if not gname:
                return ""
            all_memos = _read_memos()
            game_memos = [m for m in all_memos if gname.lower() in (m.get("game", "") or "").lower()]
            if not game_memos:
                return ""
            lines = []
            for m in game_memos[-5:]:  # 최근 5개
                memo_date = m.get("date", "")
                memo_text = m.get("memo", "") or m.get("text", "") or m.get("content", "")
                if memo_text:
                    lines.append(f"- [{memo_date}] {memo_text}")
            return ("## 게임 메모\n" + "\n".join(lines)) if lines else ""
        except Exception:
            return ""

    def _get_game_links_ctx() -> str:
        if not game_raw:
            return ""
        try:
            gd = json.loads(game_raw)
            gname = gd.get("game_name", "")
            tc_prefix = (gd.get("game_code") or "").upper()
            game_id_str = str(gd.get("game_id", ""))
            is_sb_val = "1" if gd.get("game_type", "").lower() == "super bonus" else "0"
            _gl_key = (gname, tc_prefix, game_id_str, is_sb_val)
            # 캐시 히트 시만 사용 (Drive 검색은 느려서 채팅에서 직접 호출 안 함)
            if _gl_key not in _GL_CACHE:
                return ""
            cached_links, _ = _GL_CACHE[_gl_key]
            lines = []
            if cached_links.get("gdd"):
                lines.append(f"GDD: {cached_links['gdd']}")
            if cached_links.get("math"):
                lines.append(f"Math Doc: {cached_links['math']}")
            if cached_links.get("sound"):
                lines.append(f"Sound Sheet: {cached_links['sound']}")
            if cached_links.get("direction"):
                lines.append(f"연출 Sheet: {cached_links['direction']}")
            if cached_links.get("ctd"):
                lines.append(f"CTD: {cached_links['ctd']}")
            return ("## 게임 문서 링크\n" + "\n".join(lines)) if lines else ""
        except Exception:
            return ""

    tc_ctx = ""
    live_ctx = ""
    conf_body_ctx = ""
    repob_ctx = ""
    resolve_ctx = ""
    stats_ctx = ""
    dict_ctx = ""
    schedule_ctx = ""
    events_ctx = ""
    memos_ctx = ""
    game_links_ctx = ""
    try:
        with ThreadPoolExecutor(max_workers=11) as _extra_pool:
            _tc_f = _extra_pool.submit(_get_tc_ctx)
            _live_f = _extra_pool.submit(_get_live_ctx)
            _conf_f = _extra_pool.submit(_get_conf_body_ctx)
            _repob_f = _extra_pool.submit(_get_repob_ctx)
            _resolve_f = _extra_pool.submit(_get_resolve_query_ctx)
            _stats_f = _extra_pool.submit(_get_portfolio_stats_ctx)
            _dict_f = _extra_pool.submit(_get_dictionary_ctx)
            _sched_f = _extra_pool.submit(_get_schedule_ctx)
            _events_f = _extra_pool.submit(_get_events_ctx)
            _memos_f = _extra_pool.submit(_get_memos_ctx)
            _glinks_f = _extra_pool.submit(_get_game_links_ctx)
            try:
                tc_ctx = _tc_f.result(timeout=10)
            except Exception:
                pass
            try:
                live_ctx = _live_f.result(timeout=15)
            except Exception:
                pass
            try:
                conf_body_ctx = _conf_f.result(timeout=12)
            except Exception:
                pass
            try:
                repob_ctx = _repob_f.result(timeout=12)
            except Exception:
                pass
            try:
                resolve_ctx = _resolve_f.result(timeout=8)
            except Exception:
                pass
            try:
                stats_ctx = _stats_f.result(timeout=8)
            except Exception:
                pass
            try:
                dict_ctx = _dict_f.result(timeout=8)
            except Exception:
                pass
            try:
                schedule_ctx = _sched_f.result(timeout=5)
            except Exception:
                pass
            try:
                events_ctx = _events_f.result(timeout=5)
            except Exception:
                pass
            try:
                memos_ctx = _memos_f.result(timeout=5)
            except Exception:
                pass
            try:
                game_links_ctx = _glinks_f.result(timeout=3)
            except Exception:
                pass
    except Exception:
        pass

    # ── 4.5 날짜 기반 이슈 필터 (이번 주/오늘/최근) ──
    _date_jira: List[dict] = []
    _date_label = ""
    _msg_lower = message.lower().replace(" ", "")
    try:
        from datetime import date, timedelta
        _today = date.today()
        if "이번주" in _msg_lower or "이번 주" in message or "thisweek" in _msg_lower:
            _start = _today - timedelta(days=_today.weekday())
            _date_label = f"이번 주 ({_start.strftime('%m/%d')}~)"
            _date_jira_all = [i for i in CACHE["jira"] if i.get("created", "") >= _start.isoformat()]
            # 버그 키워드 있으면 Bug 타입만, 없으면 전체
            if _is_bug_query:
                _date_jira = [i for i in _date_jira_all if i.get("type") == "Bug"]
                _date_label += " (버그)"
            else:
                _date_jira = _date_jira_all
        elif "오늘" in message or "today" in _msg_lower:
            _date_label = f"오늘 ({_today.strftime('%m/%d')})"
            _date_jira = [i for i in CACHE["jira"] if i.get("created", "") == _today.isoformat()]
        elif "어제" in message or "yesterday" in _msg_lower:
            _yd = _today - timedelta(days=1)
            _date_label = f"어제 ({_yd.strftime('%m/%d')})"
            _date_jira = [i for i in CACHE["jira"] if i.get("created", "") == _yd.isoformat()]
        elif "최근 3일" in message or "최근3일" in _msg_lower:
            _start = _today - timedelta(days=3)
            _date_label = "최근 3일"
            _date_jira = [i for i in CACHE["jira"] if i.get("created", "") >= _start.isoformat()]
    except Exception:
        pass

    # ── 4.6 우선순위/상태 기반 이슈 필터 (오픈된 Critical/Major 전체 조회) ──
    _prio_jira: List[dict] = []
    _prio_label = ""
    _CLOSED_STATUSES = {"완료", "Done", "해결됨", "Resolved", "Closed"}
    _CRITICAL_PRIORITIES = {"주요", "Highest", "highest", "Critical", "critical"}
    _MAJOR_PRIORITIES = {"Medium", "medium"}
    try:
        _is_open_query = any(w in message for w in ["오픈", "미해결", "열린", "open", "unresolved", "현재 이슈", "현재이슈"])
        _is_critical_query = any(w in message for w in ["크리티컬", "critical", "Critical", "주요"])
        _is_major_query = any(w in message for w in ["메이저", "major", "Major"])
        if not game_raw and (_is_open_query or _is_critical_query or _is_major_query):
            if _is_critical_query:
                _prio_filter = _CRITICAL_PRIORITIES
                _prio_label = "오픈된 Critical 이슈"
            elif _is_major_query:
                _prio_filter = _MAJOR_PRIORITIES
                _prio_label = "오픈된 Major 이슈"
            else:
                _prio_filter = _CRITICAL_PRIORITIES | _MAJOR_PRIORITIES
                _prio_label = "오픈된 Critical/Major 이슈"
            _prio_jira = [
                i for i in CACHE["jira"]
                if i.get("priority") in _prio_filter
                and i.get("status") not in _CLOSED_STATUSES
                and i.get("type") == "Bug"
            ]
            _prio_jira = sorted(_prio_jira, key=lambda i: i.get("updated", ""), reverse=True)
    except Exception:
        pass

    # ── 5. 검색 결과를 컨텍스트 텍스트로 변환 ──
    ctx_parts = []
    if game_list_ctx:
        ctx_parts.append(game_list_ctx)
    if game_ctx:
        ctx_parts.append(game_ctx)
    if tc_ctx:
        ctx_parts.append(tc_ctx)
    if live_ctx:
        ctx_parts.append(live_ctx)

    # 날짜 필터 결과 주입 (이번 주/오늘/어제 등)
    if _date_jira:
        _date_sorted = sorted(_date_jira, key=lambda i: i.get("created", ""), reverse=True)
        _date_lines = []
        for i in _date_sorted[:30]:
            _date_lines.append(
                f"[{i.get('key','')}] {i.get('summary','')} "
                f"(등록일: {i.get('created','')}, 상태: {i.get('status','')}, "
                f"우선순위: {i.get('priority','')}, 유형: {i.get('type','')})"
            )
        ctx_parts.append(f"## {_date_label} 등록된 Jira 이슈 ({len(_date_jira)}건)\n" + "\n".join(_date_lines))
        sources.extend(_fmt_jira_sources(_date_sorted[:10]))

    if _prio_jira:
        _prio_lines = []
        for i in _prio_jira[:20]:
            _prio_lines.append(
                f"[{i.get('key','')}] {i.get('summary','')} "
                f"(상태: {i.get('status','')}, 우선순위: {i.get('priority','')}, "
                f"담당자: {i.get('assignee','')}, 업데이트: {i.get('updated','')})"
            )
        ctx_parts.append(f"## {_prio_label} ({len(_prio_jira)}건)\n" + "\n".join(_prio_lines))
        sources.extend(_fmt_jira_sources(_prio_jira[:10]))

    if jira:
        # 이슈 관련 질문이면 Bug 타입 우선 정렬
        _is_issue_query = any(w in message for w in ["이슈", "버그", "bug", "issue", "결함", "오류", "에러", "문제"])
        if _is_issue_query:
            jira_sorted = sorted(jira, key=lambda i: 0 if i.get("type", "").lower() == "bug" else 1)
        else:
            jira_sorted = jira
        jira_lines = []
        for i in jira_sorted[:10]:
            jira_lines.append(
                f"[{i.get('key','')}] {i.get('summary','')} "
                f"(상태: {i.get('status','')}, 우선순위: {i.get('priority','')}, "
                f"담당자: {i.get('assignee','')}, 유형: {i.get('type','')})\n"
                f"설명: {i.get('_desc_full','')[:600]}"
            )
        ctx_parts.append("## Jira 이슈\n" + "\n\n".join(jira_lines))

    if conf:
        conf_lines = [f"- {p.get('title','')} ({p.get('url','')})" for p in conf[:8]]
        ctx_parts.append("## Confluence 문서\n" + "\n".join(conf_lines))
    if conf_body_ctx:
        ctx_parts.append(conf_body_ctx)

    if slack:
        slack_lines = [f"- #{s.get('name','')} — {s.get('topic','')}" for s in slack[:5]]
        ctx_parts.append("## Slack 채널\n" + "\n".join(slack_lines))

    if repob_ctx:
        ctx_parts.append(repob_ctx)

    if resolve_ctx:
        ctx_parts.append(resolve_ctx)

    if stats_ctx:
        ctx_parts.append(stats_ctx)

    if dict_ctx:
        ctx_parts.append(dict_ctx)

    if schedule_ctx:
        ctx_parts.append(schedule_ctx)

    if events_ctx:
        ctx_parts.append(events_ctx)

    if memos_ctx:
        ctx_parts.append(memos_ctx)

    if game_links_ctx:
        ctx_parts.append(game_links_ctx)

    if comparison_ctx:
        ctx_parts.append(comparison_ctx)
        sources = comparison_sources + sources

    context = "\n\n".join(ctx_parts) if ctx_parts else "관련 데이터를 찾지 못했습니다."

    # ── 6. 시스템 프롬프트 + 메시지 구성 ──
    extra_hints = ""
    if is_list_query:
        extra_hints += (
            "게임 목록 데이터가 제공된 경우, 사용자의 조건(상태·유형·기간·태그 등)에 맞는 게임을 목록 형식으로 정리해 답변하세요. "
            "출시일 정보가 없으면 솔직하게 밝히세요. "
        )
    if is_comparison:
        extra_hints += (
            "두 게임의 정보를 비교 표 형식(| 항목 | 게임A | 게임B |)으로 정리하세요. "
            "태그·유형·세대·문서 링크를 포함하세요. "
        )

    system_prompt = (
        "## 역할\n"
        "당신은 게임 스튜디오 QA 팀 전용 AI 어시스턴트 **Hub AI**입니다.\n"
        "슬롯 머신을 포함한 카지노 게임 QA에 특화되어 있으며, "
        "사내 여러 데이터 소스를 실시간으로 통합해 QA 엔지니어가 필요한 정보를 빠르게 찾도록 돕습니다.\n\n"

        "## 보유 데이터 소스 및 활용법\n"
        "아래 소스에서 수집한 데이터가 [검색된 데이터] 섹션에 제공됩니다. 각 소스의 특성을 이해하고 적절히 활용하세요.\n\n"

        "**1. gs-os 온톨로지 (게임 메타데이터)**\n"
        "- 사내 모든 게임의 공식 정보 저장소입니다.\n"
        "- 게임명, 게임ID, 게임코드, 유형(SLOT_MACHINE/KENO/VIDEO_POKER 등), 상태(released 등), 세대(nodecanvas_pure/slotmaker_cs 등)를 포함합니다.\n"
        "- 태그: 게임 특성을 나타내는 레이블 (free_spin, bonus_buy, jackpot, scatter_pay 등). confidence=high인 태그만 신뢰하세요.\n"
        "- 소스 링크: QA 문서(Google Sheets), 기획 문서(Drive 폴더)가 등록된 경우 URL이 제공됩니다.\n"
        "- 유사 게임: 태그 기반으로 유사한 게임 목록이 제공될 수 있습니다.\n"
        "- 포트폴리오 통계: 전체 게임 수, 유형별·세대별 분포를 제공합니다. 단, 라이브/개발 상태별 집계는 이 소스에서 지원하지 않습니다.\n"
        "- 용어 사전: 게임 도메인 용어의 정의를 포함합니다.\n\n"

        "**2. Jira (이슈 트래커)**\n"
        "- 프로젝트 GS의 버그·태스크·개선요청 이슈입니다.\n"
        "- 이슈 키(GS-XXXXX), 요약, 상태(To Do/In Progress/Done 등), 우선순위(Critical/Major/Minor 등), 담당자, 유형, 설명을 포함합니다.\n"
        "- 최근 730일(약 2년)치 이슈가 캐싱되어 있습니다.\n"
        "- 버그 조회 시 이슈 키를 클릭 가능한 링크로 표시하지 말고 텍스트 키([GS-12345])로만 표기하세요 (URL이 데이터에 없습니다).\n\n"

        "**3. Confluence (내부 문서)**\n"
        "- GM 스페이스(Game Studio)와 CVS 스페이스의 문서입니다.\n"
        "- 게임 기획서, QA 가이드, 프로세스 문서, 릴리즈 노트 등이 포함됩니다.\n"
        "- 제목 기반 검색 결과이므로, 제목과 URL을 그대로 안내하세요. 본문 내용이 제공된 경우에만 인용하세요.\n\n"

        "**4. Slack 채널**\n"
        "- 관련 Slack 채널 이름과 토픽이 제공됩니다.\n"
        "- 채널명(#채널명)과 토픽을 안내하세요.\n\n"

        "**5. TC 진행률 (테스트 케이스)**\n"
        "- 게임별 QA 스프레드시트에서 실시간 집계된 TC 현황입니다.\n"
        "- 기본 TC와 컨텐츠 TC로 구분됩니다.\n"
        "- Pass / Fail / No Run 수치와 전체 진행률(%)을 항상 함께 표시하세요.\n\n"

        "**6. 라이브 이슈**\n"
        "- 출시 후 발생한 이슈를 연도별로 집계한 데이터입니다.\n"
        "- Critical(C) / Major(MJ) / Minor(MN) 심각도로 분류됩니다.\n"
        "- 게임별 이슈 건수와 최근 이슈 목록을 보여주세요.\n\n"

        "**7. 코드 검색 (repob)**\n"
        "- 사내 games 레포지토리에서 게임 코드 관련 파일/라인을 검색한 결과입니다.\n"
        "- 파일 경로, 라인 번호, 코드 스니펫을 그대로 안내하세요.\n\n"

        "## 응답 형식 규칙\n"
        + extra_hints
        + "- **게임 조회**: 게임 정보를 마크다운 테이블로 표시하세요. 예시:\n"
        "  | 항목 | 값 |\n"
        "  |------|----|\n"
        "  | 게임명 | Moneyki Neko |\n"
        "  | 코드 / ID | mkn / 327 |\n"
        "  | 유형 | SLOT_MACHINE |\n"
        "  | 상태 | released |\n"
        "  | 세대 | slotmaker_cs |\n"
        "  | 태그 | free_spin, bonus_buy, jackpot_type:rich_hits |\n"
        "  | QA 문서 | [링크](url) |\n"
        "  | 기획 문서 | [링크](url) |\n"
        "  태그는 high confidence만 포함하고, 문서 링크가 없으면 해당 행은 생략하세요.\n"
        "- **태그 필터 목록**: 특정 태그/장르/기능으로 필터링한 게임 목록 요청 시, "
        "조건에 맞는 게임을 번호 목록으로 정리하고 각 게임의 코드와 상태를 함께 표시하세요. "
        "예: '1. Moneyki Neko (mkn, released)'. 조건에 해당하는 게임이 없으면 솔직하게 알려주세요.\n"
        "- **유사 게임**: 태그 기반 유사 게임이 있으면 목록으로 안내하세요.\n"
        "- **TC 진행률**: Pass/Fail/No Run 수치와 진행률(%)을 기본 TC / 컨텐츠 TC로 구분해 표시하세요.\n"
        "- **라이브 이슈**: Critical/Major/Minor 건수 요약 후 주요 이슈 목록을 보여주세요.\n"
        "- **통계**: 표 형식(마크다운 테이블)으로 정리하세요.\n"
        "- **용어**: 온톨로지 사전 정의가 있으면 인용하고, 없으면 도메인 지식 기반으로 설명하되 출처를 명시하세요.\n"
        "- **링크**: [검색된 데이터]에 명시된 URL만 사용하세요. URL이 없으면 절대 만들거나 추측하지 마세요.\n"
        "- **Jira 이슈 키**: [GS-12345] 형식으로 텍스트 표기하세요 (하이퍼링크 금지, URL 없음).\n"
        "- **이슈 목록**: 이슈/버그 관련 질문 시 유형(type)이 Bug인 것을 우선 표시하세요. Bug가 아닌 Task/Story 등은 별도로 구분하거나 생략하세요.\n"
        "- **태그 필터 + 이슈**: 특정 장르/태그 게임의 이슈를 물으면, 해당 게임들에서 발생한 Bug 티켓을 우선 정리해 보여주세요.\n"
        "- **데이터 없음**: 해당 소스에 데이터가 없으면 '데이터가 없다'고 솔직하게 말하고, 대신 찾을 수 있는 방법을 안내하세요.\n"
        "- **이전 대화 맥락**: 이전에 언급된 게임명·이슈·주제를 반드시 기억해 연속 질문에 자연스럽게 답하세요.\n"
        "- **언어**: 항상 한국어로 답변하세요. 기술 용어(game_type, status 값 등)는 영어 원문 그대로 표기해도 됩니다.\n\n"

        f"[검색된 데이터]\n{context}"
    )

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})

    intent = _intent_type if _intent_type != "general" else "gpt"

    # _return_prepared=True이면 GPT 호출 없이 준비된 데이터 반환 (스트리밍용)
    if _return_prepared:
        return messages, sources, intent

    # ── 7. GPT 호출 ──
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=2000,
            temperature=0.3,
        )
        answer = resp.choices[0].message.content.strip()
    except Exception as e:
        answer = f"GPT 호출 중 오류가 발생했어요: {e}"

    return answer, sources, intent


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    if session_id not in CHAT_SESSIONS:
        CHAT_SESSIONS[session_id] = []

    history = CHAT_SESSIONS[session_id]
    message = req.message.strip()
    if not message:
        return JSONResponse({"error": "메시지가 비어 있습니다."}, status_code=400)

    history.append({"role": "user", "content": message})

    loop = asyncio.get_event_loop()
    try:
        answer, sources, intent = await loop.run_in_executor(
            _executor, _process_chat, message, list(history)
        )
    except Exception as e:
        import traceback
        print(f"[Chat] 치명적 오류:\n{traceback.format_exc()}")
        return JSONResponse({"session_id": session_id, "answer": f"서버 오류가 발생했어요: {e}", "sources": [], "intent": "error"})

    history.append({"role": "assistant", "content": answer})
    if len(history) > 30:
        CHAT_SESSIONS[session_id] = history[-30:]

    return JSONResponse({
        "session_id": session_id,
        "answer": answer,
        "sources": sources,
        "intent": intent,
    })


@app.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest):
    """GPT 응답을 SSE로 스트리밍하는 채팅 엔드포인트."""
    import os
    session_id = req.session_id or str(uuid.uuid4())
    if session_id not in CHAT_SESSIONS:
        CHAT_SESSIONS[session_id] = []

    history = CHAT_SESSIONS[session_id]
    message = req.message.strip()
    if not message:
        async def _err():
            yield f"data: {json.dumps({'type': 'error', 'content': '메시지가 비어 있습니다.'})}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    history.append({"role": "user", "content": message})

    loop = asyncio.get_event_loop()

    async def generate():
        # 1. 데이터 수집 (스레드에서)
        try:
            messages, sources, intent = await loop.run_in_executor(
                _executor,
                lambda: _process_chat(message, list(history), _return_prepared=True)
            )
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'데이터 수집 오류: {e}'})}\n\n"
            return

        # 2. GPT 스트리밍 호출
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        full_answer = ""
        try:
            stream = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=2000,
                temperature=0.3,
                stream=True,
            )
            for chunk in stream:
                token = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                if token:
                    full_answer += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token}, ensure_ascii=False)}\n\n"
        except Exception as e:
            err_msg = f"GPT 호출 오류: {e}"
            yield f"data: {json.dumps({'type': 'token', 'content': err_msg})}\n\n"
            full_answer = err_msg

        # 3. 완료 이벤트 (소스 + 세션 포함)
        yield f"data: {json.dumps({'type': 'done', 'sources': sources, 'intent': intent, 'session_id': session_id}, ensure_ascii=False)}\n\n"

        # 4. 히스토리 업데이트
        history.append({"role": "assistant", "content": full_answer})
        if len(history) > 30:
            CHAT_SESSIONS[session_id] = history[-30:]

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
