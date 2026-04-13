#!/usr/bin/env python3
"""
QA Integrated Search Tool
Jira (GS project) + Confluence unified search
"""

import os
import re
import base64
import json
import subprocess
import threading
import time
import queue
from typing import Optional, Dict, List

import click
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

load_dotenv()

console = Console()

# ── Config ─────────────────────────────────────────────────────────────────

ATLASSIAN_DOMAIN = os.getenv("ATLASSIAN_DOMAIN", "bagelcode.atlassian.net")
ATLASSIAN_EMAIL = os.getenv("ATLASSIAN_EMAIL", "")
ATLASSIAN_API_TOKEN = os.getenv("ATLASSIAN_API_TOKEN", "")
JIRA_PROJECT = os.getenv("JIRA_PROJECT", "GS")

JIRA_BASE = f"https://{ATLASSIAN_DOMAIN}/rest/api/3"
CONFLUENCE_BASE = f"https://{ATLASSIAN_DOMAIN}/wiki/rest/api"


def atlassian_headers() -> dict:
    token = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ── Cache: 전체 데이터 수집 ──────────────────────────────────────────────────

def fetch_all_jira() -> List[Dict]:
    """GS 프로젝트 전체 이슈를 nextPageToken 방식으로 수집."""
    jql = f"project = {JIRA_PROJECT} AND updated >= -730d ORDER BY updated DESC"
    PAGE_SIZE = 100
    next_page_token: Optional[str] = None
    all_items: List[Dict] = []
    try:
        while True:
            params: Dict = {
                "jql": jql,
                "maxResults": PAGE_SIZE,
                "fields": "summary,status,assignee,priority,issuetype,updated,created,description,labels,parent,resolutiondate",
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token
            resp = requests.get(
                f"{JIRA_BASE}/search/jql",
                headers=atlassian_headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])
            for issue in issues:
                f = issue["fields"]
                assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")
                updated_raw = f.get("updated", "")
                updated = updated_raw[:10] if updated_raw else "-"
                created_raw = f.get("created", "")
                created = created_raw[:10] if created_raw else "-"
                resolved_raw = f.get("resolutiondate", "")
                resolved = resolved_raw[:10] if resolved_raw else None
                desc_obj = f.get("description")
                desc_full = _extract_adf_text(desc_obj) if desc_obj else ""
                parent_summary = ((f.get("parent") or {}).get("fields") or {}).get("summary", "")
                game = _extract_game_from_parent(parent_summary) if parent_summary else ""
                is_sb = bool(re.search(r"Super Bonus", parent_summary, re.IGNORECASE)) if parent_summary else False
                all_items.append({
                    "key": issue["key"],
                    "summary": f.get("summary", "") or "",
                    "status": (f.get("status") or {}).get("name", "-"),
                    "type": (f.get("issuetype") or {}).get("name", "-"),
                    "priority": (f.get("priority") or {}).get("name", "-"),
                    "assignee": assignee,
                    "updated": updated,
                    "created": created,
                    "resolved": resolved,
                    "game": game,
                    "is_sb": is_sb,
                    "description": desc_full[:120] + "..." if len(desc_full) > 120 else desc_full,
                    "_desc_full": (desc_full + " " + parent_summary).lower(),
                    "url": f"https://{ATLASSIAN_DOMAIN}/browse/{issue['key']}",
                })
            if data.get("isLast", True) or not issues:
                break
            next_page_token = data.get("nextPageToken")
        console.print(f"[dim][Cache] Jira {len(all_items)}건 수집 완료[/dim]")
        return all_items
    except Exception as e:
        console.print(f"[red][Cache][Jira] 오류: {e}[/red]")
        if not all_items:
            raise
        return all_items


# ── Live Issues (GS-Live 컴포넌트, on-demand) ────────────────────────────────

_PRIORITY_SEV: Dict[str, str] = {
    "주요": "c",       # Critical
    "중요": "c",       # Critical
    "medium": "mj",    # Major
    "Medium": "mj",
    "사소": "mn",      # Minor
}
_SEV_LABEL: Dict[str, str] = {"c": "Critical", "mj": "Major", "mn": "Minor"}


def _extract_game_from_parent(parent_summary: str) -> str:
    """상위항목 summary에서 게임명 추출.
    GS-NNN 키, [CODE] 태그, Super Bonus 접미사 제거."""
    s = parent_summary.strip()
    s = re.sub(r"^[A-Z]+-\d+\s*[-–:]\s*", "", s)   # GS-NNN - 제거
    s = re.sub(r"^\[[^\]]+\]\s*", "", s)             # [JINWOO] 제거
    s = re.sub(r"\s+Super Bonus$", "", s, flags=re.IGNORECASE)  # Super Bonus 접미사 제거
    return s.strip()


def fetch_live_issues(year: int, month: Optional[int] = None) -> List[Dict]:
    """GS-Live 컴포넌트 이슈를 Jira에서 on-demand로 가져옴.
    month가 None이면 해당 연도 전체 (1~12월 레이블 OR)."""
    if month:
        labels_jql = f'"gs qa label[labels]" IN ("{year}_PROD_{month}")'
    else:
        labels = ", ".join([f'"{year}_PROD_{m}"' for m in range(1, 13)])
        labels_jql = f'"gs qa label[labels]" IN ({labels})'

    jql = (
        f'project = {JIRA_PROJECT} AND component = "GS-Live" '
        f"AND {labels_jql} ORDER BY created DESC"
    )

    all_issues: List[Dict] = []
    next_page_token: Optional[str] = None
    try:
        while True:
            params: Dict = {
                "jql": jql,
                "maxResults": 100,
                "fields": "summary,status,priority,assignee,created,parent,labels",
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token
            resp = requests.get(
                f"{JIRA_BASE}/search/jql",
                headers=atlassian_headers(),
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            for issue in data.get("issues", []):
                f = issue["fields"]
                prio = (f.get("priority") or {}).get("name", "")
                sev = _PRIORITY_SEV.get(prio, "mn")

                parent_summary = ((f.get("parent") or {}).get("fields") or {}).get("summary", "")
                game = _extract_game_from_parent(parent_summary) if parent_summary else ""

                status_name = (f.get("status") or {}).get("name", "")
                assignee = (f.get("assignee") or {}).get("displayName", "")
                created_raw = f.get("created", "")
                created_date = created_raw[:10] if created_raw else ""
                created_month = int(created_date[5:7]) if len(created_date) >= 7 else 0

                # PROD 레이블에서 월 추출 (e.g. 2026_PROD_3 → 3)
                label_month = None
                for lbl in (f.get("labels") or []):
                    m = re.match(r"^\d{4}_PROD_(\d+)$", lbl)
                    if m:
                        label_month = int(m.group(1))
                        break

                all_issues.append({
                    "key": issue["key"],
                    "summary": f.get("summary", ""),
                    "sev": sev,
                    "sev_label": _SEV_LABEL.get(sev, ""),
                    "status": status_name,
                    "assignee": assignee,
                    "created": created_date,
                    "month": label_month or (month if month else created_month),
                    "game": game,
                    "url": f"https://{ATLASSIAN_DOMAIN}/browse/{issue['key']}",
                })
            if data.get("isLast", True) or not data.get("issues"):
                break
            next_page_token = data.get("nextPageToken")
    except Exception as e:
        console.print(f"[red][LiveIssues] 오류: {e}[/red]")
    return all_issues


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def _fetch_space_pages(space_key: str) -> List[Dict]:
    """단일 스페이스의 전체 페이지를 /wiki/rest/api/content 로 수집 (메타데이터만)."""
    PAGE_SIZE = 50
    start = 0
    items_out: List[Dict] = []
    while True:
        params = {
            "spaceKey": space_key,
            "type": "page",
            "limit": PAGE_SIZE,
            "start": start,
            "expand": "space,version",
        }
        resp = requests.get(
            f"{CONFLUENCE_BASE}/content",
            headers=atlassian_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        for item in results:
            space = (item.get("space") or {}).get("name", "-")
            version_by = ((item.get("version") or {}).get("by") or {}).get("displayName", "-")
            last_mod = ((item.get("version") or {}).get("when") or "")[:10]
            title = item.get("title", "") or ""
            items_out.append({
                "id": item["id"],
                "title": title,
                "space": space,
                "last_modified": last_mod,
                "modified_by": version_by,
                "url": f"https://{ATLASSIAN_DOMAIN}/wiki{item.get('_links', {}).get('webui', '')}",
                "_search_text": title.lower(),
            })
        start += len(results)
        if len(results) < PAGE_SIZE:
            break
    return items_out


def fetch_all_confluence() -> List[Dict]:
    """Game Studio(GM) + CVS 스페이스 전체 페이지 수집."""
    all_items: List[Dict] = []
    errors: List[str] = []
    for space_key in ["GM", "CVS"]:
        try:
            pages = _fetch_space_pages(space_key)
            all_items.extend(pages)
            console.print(f"[dim][Cache] Confluence {space_key} {len(pages)}건[/dim]")
        except Exception as e:
            console.print(f"[red][Cache][Confluence][{space_key}] 오류: {e}[/red]")
            errors.append(str(e))
    if errors and not all_items:
        raise Exception(f"Confluence 연결 실패: {'; '.join(errors)}")
    console.print(f"[dim][Cache] Confluence 총 {len(all_items)}건 수집 완료[/dim]")
    return all_items


def fetch_confluence_page_body(page_id: str) -> str:
    """단일 Confluence 페이지의 본문을 plain text로 반환 (최대 2000자)."""
    try:
        resp = requests.get(
            f"{CONFLUENCE_BASE}/content/{page_id}",
            headers=atlassian_headers(),
            params={"expand": "body.storage"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        body_html = (data.get("body", {}).get("storage", {}).get("value", "")) or ""
        return _strip_html(body_html)[:2000]
    except Exception:
        return ""


# ── Slack channel search (static list) ───────────────────────────────────────

SLACK_WORKSPACE = "bagelcode"

SLACK_CHANNELS: List[Dict] = [
    # ── 활성 채널 ──
    {"name": "tf-gs-barbarian-destroyer-sb",  "id": "C0A6X52U7MK", "archived": False},
    {"name": "tf-gs-fiesta-spicy-friends",    "id": "C0AGW7K7P5X", "archived": False},
    {"name": "tf-gs-aqua-blessing",           "id": "C0AM6F1ABSR", "archived": False},
    {"name": "tf-gs-blazing-triplex",         "id": "C0A9EQZ1HS4", "archived": False},
    {"name": "tf-gs-roar-and-flame-sb",       "id": "C0AE4ETH36F", "archived": False},
    {"name": "tf-gs-the-incredible-herc",     "id": "C09T4KFGM0T", "archived": False},
    {"name": "tf-gs-luck-n-roll-wheels",      "id": "C0A3ZDXJ03X", "archived": False},
    {"name": "tf-gs-skipper-willie-sb",       "id": "C0AA5AG219U", "archived": False},
    {"name": "tf-gs-cash-them-if-you-ham",    "id": "C09AT1R9QE6", "archived": False},
    {"name": "tf-gs-money-tree-bloom",        "id": "C0ACTBYD18D", "archived": False},
    {"name": "tf-gs-big-footunate-wheel",     "id": "C09MG56DSCB", "archived": False},
    {"name": "tf-gs-farmyard-craze",          "id": "C0AGP515N2U", "archived": False},
    {"name": "tf-gs-math",                    "id": "C06EWFYTN5R", "archived": False},
    {"name": "tf-gs-five-gold-pots",          "id": "C09UXGATAF6", "archived": False},
    {"name": "tf-gs-stone-gaze",              "id": "C09PMH8EMRN", "archived": False},
    {"name": "tf-gs-mammoth-mega-rush",       "id": "C099XRMM457", "archived": False},
    {"name": "tf-gs-triple-spin-mania",       "id": "C09F3V2G9AR", "archived": False},
    {"name": "tf-gs-el-oro-de-zorro",         "id": "C09DPFJM4L9", "archived": False},
    {"name": "tf-gs-hypercasual-support",     "id": "C09H43M3H9R", "archived": False},
    {"name": "tf-gs-caishen-quad-cash",       "id": "C0AMSDNBYSK", "archived": False},
    {"name": "tf-cvs-v-layout-revamp",        "id": "C09U37M6XT7", "archived": False},
    {"name": "tf-cvs-asset-ttalkkag",         "id": "C08LRRPMDC7", "archived": False},
    {"name": "tf-brickflow",                  "id": "C0AKYGWV76G", "archived": False},
    {"name": "tf-nano-banana",                "id": "C09D6LWQQP2", "archived": False},
    {"name": "tf-abyss",                      "id": "C08QDLUDQJG", "archived": False},
    {"name": "tf-internal-llm",               "id": "C0ALAQTPC6B", "archived": False},
    {"name": "tf-ai-first",                   "id": "C0A09GMU5RD", "archived": False},
    {"name": "tf-ai-adoption",                "id": "C05382G4B6X", "archived": False},
    {"name": "tf-ai-edu",                     "id": "C06HZMVHD25", "archived": False},
    {"name": "tf-pr-ai-lab",                  "id": "C0A00HDE8N8", "archived": False},
    {"name": "tf-k2-upg-ops",                 "id": "C0AE22Z3YTU", "archived": False},
    {"name": "tf-meta-business",              "id": "C0A1M4D0NUA", "archived": False},
    {"name": "tf-vitag",                      "id": "C0849AHNVNY", "archived": False},
    {"name": "tf-gamejam-2026-march",         "id": "C0AG9V290NP", "archived": False},
    {"name": "tf-gamejam-goat",               "id": "C04LW415K0U", "archived": False},
    {"name": "tf-geneva-jodong",              "id": "C09C511BX3N", "archived": False},
    # ── 보관된 채널 ──
    {"name": "tf-gs-the-rose-of-the-beast",   "id": "C086M3VCLCQ", "archived": True},
    {"name": "tf-gs-rhino-mega-splits",        "id": "C0972MVUEE8", "archived": True},
    {"name": "tf-gs-golden-mango-mummy",       "id": "C098XKHDTDY", "archived": True},
    {"name": "tf-gs-cravy-peng-wins",          "id": "C0770S3AXAM", "archived": True},
    {"name": "tf-gs-sher-lucky-spins",         "id": "C070ZDCBQBX", "archived": True},
    {"name": "tf-gs-barbarian-golden-axe",     "id": "C0673GCAFFE", "archived": True},
    {"name": "tf-gs-sb-tropical-dream",        "id": "C05B416A1BR", "archived": True},
    {"name": "tf-gs-dazzling-jewel-streak",    "id": "C03P2AZA8H3", "archived": True},
    {"name": "tf-gs-kung-food-panda",          "id": "C083VFLS8BS", "archived": True},
    {"name": "tf-gs-lotta-cash-mummy",         "id": "C08EHDYR54Z", "archived": True},
    {"name": "tf-gs-big-bang-bounty",          "id": "C07RSKJAVK6", "archived": True},
    {"name": "tf-gs-cash-hits-blitz",          "id": "C070RTB4M8A", "archived": True},
    {"name": "tf-gs-infinite-fireball-link",   "id": "C06RU7F5M8V", "archived": True},
    {"name": "tf-gs-heat-of-cleopatra",        "id": "C03THHJDT8X", "archived": True},
    {"name": "tf-gs-triple-fish-riches",       "id": "C03LRE9QKFF", "archived": True},
    {"name": "tf-gs-benhur-dash-for-glory",    "id": "C0797SUNHG8", "archived": True},
    {"name": "tf-gs-drum-xi-fa-cai",           "id": "C06CSEWHS07", "archived": True},
    {"name": "tf-gs-spooky-magic-orbs",        "id": "C03GZU6PDRN", "archived": True},
    {"name": "tf-gs-hefty-piggy-bank",         "id": "C03FAMF84AD", "archived": True},
    {"name": "tf-gs-wicked-hot-diamonds",      "id": "C03NPALD1U4", "archived": True},
    {"name": "tf-gs-ethereal-wishes",          "id": "C08CLQRGVJ5", "archived": True},
    {"name": "tf-gs-celestial-express",        "id": "C03AP16J5BK", "archived": True},
    {"name": "tf-gs-kitty-kash-mewlah",        "id": "C08K9UM5VSQ", "archived": True},
    {"name": "tf-gs-fury-truck-bandits",       "id": "C082Y6KNTU0", "archived": True},
    {"name": "tf-gs-beans-grow-wild",          "id": "C05THBAKWSX", "archived": True},
    {"name": "tf-gs-jpj-clone-projects",       "id": "C04095X0HMY", "archived": True},
    {"name": "tf-gs-wild-west-rails",          "id": "C08KEEJ95LK", "archived": True},
    {"name": "tf-gs-triple-fu-bats",           "id": "C03JM8U23V3", "archived": True},
    {"name": "tf-gs-oink-doink-joint",         "id": "C09DMNN0N4R", "archived": True},
    {"name": "tf-gs-supercharge-x-treme",      "id": "C092Y913R2L", "archived": True},
    {"name": "tf-gs-mystic-moon-legacy",       "id": "C08S3NT3GEP", "archived": True},
    {"name": "tf-gs-steam-of-fortune",         "id": "C0785D730G4", "archived": True},
    {"name": "tf-gs-xing-xing-express",        "id": "C06CS46HPLY", "archived": True},
    {"name": "tf-gs-blade-cash-sakura",        "id": "C053LU4EM0E", "archived": True},
    {"name": "tf-gs-caishen-triple-cash",      "id": "C0492MXPXFG", "archived": True},
    {"name": "tf-gs-mystery-of-neverland",     "id": "C09B0SYD0CX", "archived": True},
    {"name": "tf-gs-mega-joy-party",           "id": "C02NRUD7T7Y", "archived": True},
    {"name": "tf-gs-mighty-dragons-yellow",    "id": "C06U6KEN509", "archived": True},
    {"name": "tf-gs-blessings-of-the-goddess", "id": "C06R3DJS0AJ", "archived": True},
    {"name": "tf-gs-wildhit-silverback",       "id": "C04H653QM89", "archived": True},
    {"name": "tf-gs-rich-hits-strike",         "id": "C04H07BCB2L", "archived": True},
    {"name": "tf-gs-the-great-buffalo",        "id": "C06RYMSTKD4", "archived": True},
    {"name": "tf-gs-wizard-of-the-moon",       "id": "C01KP6B3TDW", "archived": True},
    {"name": "tf-gs-winrock-resort-and-casino","id": "C05FXS275CG", "archived": True},
    {"name": "tf-gs-rich-for-the-star",        "id": "C06HPG7FTRA", "archived": True},
    {"name": "tf-gs-lucky-claw-eclipse",       "id": "C05DR4VEE0N", "archived": True},
    {"name": "tf-gs-buffalo-triple-bounty",    "id": "C0826ALG25P", "archived": True},
    {"name": "tf-gs-fu-bats-riches",           "id": "C06QNNWTDQX", "archived": True},
    {"name": "tf-gs-roar-and-flame",           "id": "C08BW3U0420", "archived": True},
    {"name": "tf-gs-mystic-dragons-pagoda",    "id": "C08MSJ5S6G6", "archived": True},
    {"name": "tf-gs-bingo-inferno-deluxe",     "id": "C047S0ELNQ6", "archived": True},
    {"name": "tf-gs-dingo-grand",              "id": "C054405E2UB", "archived": True},
    {"name": "tf-gs-fishing-fever",            "id": "C03MP8TS87P", "archived": True},
    {"name": "tf-gs-djinn-the-win",            "id": "C05KW7926PL", "archived": True},
    {"name": "tf-gs-lock-o-saurus",            "id": "C07GA15DQP8", "archived": True},
    {"name": "tf-gs-guess-or-dare",            "id": "C02NYK9TSRY", "archived": True},
    {"name": "tf-gs-wild-big-horn",            "id": "C05F571R08M", "archived": True},
    {"name": "tf-gs-wild-loot-squad",          "id": "C07EVGG681G", "archived": True},
    {"name": "tf-gs-serpents-treasure-quest",  "id": "C07J4ESR5A9", "archived": True},
    {"name": "tf-gs-goosepoly-go-wild",        "id": "C04TCRY5BSS", "archived": True},
    {"name": "tf-gs-ultimate-money-falls",     "id": "C04HAK88BEG", "archived": True},
    {"name": "tf-gs-wild-hit-samurai",         "id": "C03B05HAH32", "archived": True},
    {"name": "tf-gs-wild-hit-buffalo",         "id": "C02UYEQP5FT", "archived": True},
    {"name": "tf-gs-golden-mango-kingdom",     "id": "C086B1BRLD7", "archived": True},
    {"name": "tf-gs-ocean-spirit-treasures",   "id": "C083MEZBMCG", "archived": True},
    {"name": "tf-gs-jelly-bear-king",          "id": "C05EBV19E3U", "archived": True},
    {"name": "tf-gs-fu-dai-bao-nanza",         "id": "C08NCH1K02V", "archived": True},
    {"name": "tf-gs-dino-duo-wild",            "id": "C07SRJ5R2TY", "archived": True},
    {"name": "tf-gs-az-stack-rise",            "id": "C05TQ9LSYP4", "archived": True},
    {"name": "tf-gs-rich-hits-triple-pop",     "id": "C07DE9PSL14", "archived": True},
    {"name": "tf-gs-wild-tide-of-neptune",     "id": "C069YLLN892", "archived": True},
    {"name": "tf-gs-spot-x-of-the-nile",       "id": "C079B2H88BT", "archived": True},
    {"name": "tf-gs-rich-for-the-star-winter", "id": "C07KH94AR5E", "archived": True},
    {"name": "tf-gs-spot-y-of-the-nile",       "id": "C078YCJD7QS", "archived": True},
    {"name": "tf-gs-jin-long-hu-yu",           "id": "C08AFJT6U2D", "archived": True},
    {"name": "tf-gs-cash-mummy",               "id": "C075ZSZ45GC", "archived": True},
    {"name": "tf-gs-bingo-ahoy",               "id": "C07JJBLLA3W", "archived": True},
    {"name": "tf-gs-splendid-stallion",        "id": "C04MZ6PH9RU", "archived": True},
    {"name": "tf-gs-fortune-spookie",          "id": "C05BB58CXA5", "archived": True},
    {"name": "tf-gs-hothotchilli-sb",          "id": "C068ZA373UM", "archived": True},
    {"name": "tf-gs-wicked-rumble",            "id": "C06HFHTMBHS", "archived": True},
    {"name": "tf-gs-sams-gems",                "id": "C06N1HRPFHB", "archived": True},
    {"name": "tf-gs-the-wishgranter",          "id": "C074F8TC729", "archived": True},
    {"name": "tf-gs-elephant-ascension",       "id": "C04NHUF4M8D", "archived": True},
    {"name": "tf-gs-buffalo-burst",            "id": "C06N0KW3NSH", "archived": True},
    {"name": "tf-gs-dragons-gate",             "id": "C07ET7J8EAV", "archived": True},
    {"name": "tf-gs-video-poker",              "id": "C075WE0E17B", "archived": True},
    {"name": "tf-gs-cowsmic-invaders",         "id": "C087CLVPLSX", "archived": True},
    {"name": "tf-gs-secret-fortunes",          "id": "C066YBDARHS", "archived": True},
    {"name": "tf-gs-pharaoh-bacon",            "id": "C04J5165PD4", "archived": True},
    {"name": "tf-gs-coin-bite",                "id": "C054FGSPGD7", "archived": True},
    {"name": "tf-gs-sun-gods-scriptures",      "id": "C085H1ZJF61", "archived": True},
    {"name": "tf-gs-fire-bat-888",             "id": "C09843F64V8", "archived": True},
    {"name": "tf-gs-race-to-valhalla",         "id": "C093D2V6J9J", "archived": True},
    {"name": "tf-gs-cash-gone-nuts",           "id": "C090C1KESV9", "archived": True},
    {"name": "tf-gs-big-cluckin-winz",         "id": "C08UFL9CKFT", "archived": True},
    {"name": "tf-gs-captain-mango-bingo",      "id": "C08DY95N97Y", "archived": True},
    {"name": "tf-gs-buck-choys-fortune",       "id": "C06D42Y6KDK", "archived": True},
    {"name": "tf-gs-lucky-7-locomotive",       "id": "C07T9SYG78F", "archived": True},
    {"name": "tf-gs-sb-serengeti-sun",         "id": "C0694LRQ0MS", "archived": True},
    {"name": "tf-gs-pots-o-cash",              "id": "C05TEGAH8MT", "archived": True},
    {"name": "tf-gs-mighty-pot-hero",          "id": "C05U6DSK4HE", "archived": True},
    {"name": "tf-gs-oro-del-toro",             "id": "C05KPHMCSP8", "archived": True},
    {"name": "tf-gs-choys-gold-cash",          "id": "C04A7D3P537", "archived": True},
    {"name": "tf-gs-gramps-and-goldie",        "id": "C09D7QKF3M0", "archived": True},
    {"name": "tf-gs-goldmungandr",             "id": "C05KW0Z5DQB", "archived": True},
    {"name": "tf-gs-gorilla-bonanza",          "id": "C053RU21BQC", "archived": True},
    {"name": "tf-gs-goldblins-grotto",         "id": "C05EHPZB4KT", "archived": True},
    {"name": "tf-gs-sovereign-dragon",         "id": "C045J24ALAK", "archived": True},
    {"name": "tf-gs-phoenix-bounty",           "id": "C057XNFV8ES", "archived": True},
    {"name": "tf-gs-savannah-savages",         "id": "C08LUA3DNGY", "archived": True},
    {"name": "tf-gs-radiant-cleopatra",        "id": "C08U3UPU8P4", "archived": True},
    {"name": "tf-gs-moneyki-neko",             "id": "C08K9JS2L7P", "archived": True},
    {"name": "tf-gs-majestic-bay",             "id": "C04K32TC8PJ", "archived": True},
    {"name": "tf-gs-rising-cheshire",          "id": "C04PKHBEB29", "archived": True},
    {"name": "tf-gs-coin-pusher",              "id": "C04RYLULVQD", "archived": True},
    {"name": "tf-gs-samba-paradise",           "id": "C076N2QGXNX", "archived": True},
    {"name": "tf-gs-star-juggler",             "id": "C07UD85DX4G", "archived": True},
    {"name": "tf-gs-lion-rumble",              "id": "C0729UMA683", "archived": True},
    {"name": "tf-gs-888-firecrackers",         "id": "C08MYRQR55K", "archived": True},
    {"name": "tf-gs-boombastic-bingo",         "id": "C0923FJUUHL", "archived": True},
    {"name": "tf-gs-franken-sparks",           "id": "C098VK929C6", "archived": True},
    {"name": "tf-gs-charming-wheels",          "id": "C02NRUBP3E2", "archived": True},
    {"name": "tf-gs-pawsome-kitty",            "id": "C037CQGENKF", "archived": True},
    {"name": "tf-gs-zodiac-taurus",            "id": "C04J2MLG3PY", "archived": True},
    {"name": "tf-gs-lotus-express",            "id": "C0546V96A85", "archived": True},
    {"name": "tf-gs-oink-overflow",            "id": "C08JBDPH006", "archived": True},
    {"name": "tf-gs-mystery-suspect",          "id": "C04C47AC2NN", "archived": True},
    {"name": "tf-gs-taurus-fury",              "id": "C09N8N4P6FP", "archived": True},
    {"name": "tf-gs-immortal-beauty",          "id": "C02NVM74AGM", "archived": True},
    {"name": "tf-gs-grand-hippo",              "id": "C062D51BC4U", "archived": True},
    {"name": "tf-gs-crystal-nudge",            "id": "C08SCDSF2RJ", "archived": True},
    {"name": "tf-gs-pepery-wild",              "id": "C02PB8RT741", "archived": True},
    {"name": "tf-gs-bubble-farm",              "id": "C02PD9XRV50", "archived": True},
    {"name": "tf-gs-golden-madness",           "id": "C096P5XVC5N", "archived": True},
    {"name": "tf-gs-skipper-willie",           "id": "C07N2V2V0NN", "archived": True},
    {"name": "tf-gs-chief-power",              "id": "C07RSG37XGU", "archived": True},
    {"name": "tf-gs-panda-unleashed",          "id": "C08SDL3ECUT", "archived": True},
    {"name": "tf-gs-grand-gator",              "id": "C09CTSL0AKH", "archived": True},
    {"name": "tf-gs-rhino-superbonus",         "id": "C028YPW24HX", "archived": True},
    {"name": "tf-gs-fire-pawtrol",             "id": "C07JD0JCVJR", "archived": True},
    {"name": "tf-gs-mighty-dragons",           "id": "C06S2D3J093", "archived": True},
    {"name": "tf-gs-patriot-stripes",          "id": "C03S6GKBVNG", "archived": True},
    {"name": "tf-gs-goldcano-riches",          "id": "C09194WH2GM", "archived": True},
    {"name": "tf-gs-outback-voyage",           "id": "C048HQXK6J3", "archived": True},
    {"name": "tf-gs-sweetie-wins",             "id": "C02T5LK0PQQ", "archived": True},
    {"name": "tf-gs-purrfect-adventure",       "id": "C05E8FKDJEQ", "archived": True},
    {"name": "tf-gs-barbarian",                "id": "C04NKC6T0QN", "archived": True},
    {"name": "tf-gs-cbnriches",                "id": "C022H4B18Q3", "archived": True},
    {"name": "tf-gs-rpgbattle",                "id": "C09BS6Z0P5X", "archived": True},
    {"name": "tf-gs-bacontrio",                "id": "C05SYSS64QG", "archived": True},
    {"name": "tf-gs-wild-lips",                "id": "C03S44BH1L2", "archived": True},
    {"name": "tf-gs-porky-pots",               "id": "C0911L1LR3P", "archived": True},
    {"name": "tf-gs-mauis-fire",               "id": "C07UE6208PJ", "archived": True},
    {"name": "tf-gs-goldy-king",               "id": "C048HEHH6Q1", "archived": True},
    {"name": "tf-gs-shen-long",                "id": "C04HHSG30FJ", "archived": True},
    {"name": "tf-gs-anigacha",                 "id": "C09B1NZ5ALF", "archived": True},
    {"name": "tf-gs-rhino",                    "id": "C01NNV9NDFG", "archived": True},
    {"name": "tf-gs-owl-loot",                 "id": "C02NYL83943", "archived": True},
    {"name": "tf-gs-buzz-pop",                 "id": "C0944AE2PU3", "archived": True},
    {"name": "tf-gs-plinko",                   "id": "C0562P4JD0U", "archived": True},
    {"name": "tf-gs-cascade",                  "id": "C058CBC8YQP", "archived": True},
    {"name": "tf-gs-wizardwayz",               "id": "C067Y6DR3MG", "archived": True},
    {"name": "tf-gs-conversionslot",           "id": "C011PPRTEFM", "archived": True},
    {"name": "tf-gs-luckycharms",              "id": "C01E4T8S6KZ", "archived": True},
    {"name": "tf-gs-treasureriches",           "id": "C03E1U8PTPE", "archived": True},
    {"name": "tf-gs-triple-riches",            "id": "C034K513CQ5", "archived": True},
    {"name": "tf-gs-piggybanktrio",            "id": "C06RXN6GE7M", "archived": True},
    {"name": "tf-gs-megarespin",               "id": "C03NUMRGXT6", "archived": True},
    {"name": "tf-gs-cashingin",                "id": "C02D0CPK4SF", "archived": True},
    {"name": "tf-gs-grandways",                "id": "C05BC3PD94K", "archived": True},
    {"name": "tf-gs-supernine",                "id": "C01KY79GGS2", "archived": True},
    {"name": "tf-gs-fistofzeus",               "id": "C05FFUURFUG", "archived": True},
    {"name": "tf-gs-cbbingo",                  "id": "C029ALBQ484", "archived": True},
    {"name": "tf-gs-hexagold",                 "id": "C0278BX582G", "archived": True},
    {"name": "tf-gs-penguins",                 "id": "C03V0B5CMFA", "archived": True},
    {"name": "tf-gs-goblin",                   "id": "C01SU6LRM70", "archived": True},
    {"name": "tf-gs-cash-billionaire-party",   "id": "C022SQG95C2", "archived": True},
    {"name": "tf-gs-cash-gold",                "id": "C02T9FSJS0Y", "archived": True},
    {"name": "tf-gs-1001nights",               "id": "C026WQA9VLK", "archived": True},
    {"name": "tf-gs-mermaids",                 "id": "C01TMDWGB5X", "archived": True},
    {"name": "tf-gs-medusa",                   "id": "C046VNQ9EQP", "archived": True},
    {"name": "tf-golden-drums",                "id": "C031JDM0CR3", "archived": True},
]

# 채널 이름에서 검색 텍스트 생성 (하이픈을 공백으로)
for _ch in SLACK_CHANNELS:
    _ch["_search_text"] = _ch["name"].replace("-", " ").replace("_", " ").lower()
    _ch["url"] = f"https://{SLACK_WORKSPACE}.slack.com/archives/{_ch['id']}"


def search_slack_channels(query: str) -> List[Dict]:
    """채널 이름에서 로컬 검색."""
    # 언더스코어·아포스트로피를 공백으로 치환 후 토크나이징 (SB_게임명, Luck'n'Roll 대응)
    normalized = _norm_ampersand(query.strip().lower()).replace("_", " ").replace("'", " ").replace("'", " ")
    tokens = [t for t in normalized.split() if t]
    if not tokens:
        return []
    word_groups = [_expand_word(t) for t in tokens]
    results = []
    for ch in SLACK_CHANNELS:
        text = ch["_search_text"]
        if all(any(syn in text for syn in group) for group in word_groups):
            results.append({k: v for k, v in ch.items() if not k.startswith("_")})
    return results


# ── GS OS REST API + GWS Drive search ────────────────────────────────────────

GS_OS_API_URL = os.environ.get("GS_OS_API_URL", "https://gs-os-dev.backoffice.bagelgames.com")

_gs_os_games: Optional[List[Dict]] = None
_gs_os_games_lock = threading.Lock()

MIME_LABELS = {
    "application/vnd.google-apps.document": "Docs",
    "application/vnd.google-apps.spreadsheet": "Sheets",
    "application/vnd.google-apps.presentation": "Slides",
    "application/vnd.google-apps.folder": "폴더",
    "application/pdf": "PDF",
    "image/jpeg": "이미지",
    "image/png": "이미지",
}


def reset_mcp_session():
    """게임 목록 캐시 리셋. 다음 call_mcp_tool 호출 시 REST API에서 재조회."""
    global _gs_os_games
    with _gs_os_games_lock:
        _gs_os_games = None


def _load_gs_os_games() -> List[Dict]:
    """GS OS REST API에서 전체 게임 목록을 조회하고 캐시에 저장."""
    global _gs_os_games
    resp = requests.get(f"{GS_OS_API_URL}/api/games", timeout=15)
    resp.raise_for_status()
    games = resp.json().get("games", [])
    _gs_os_games = games
    return games


def _get_gs_os_games() -> List[Dict]:
    """캐시된 게임 목록 반환. 없으면 REST API에서 조회."""
    global _gs_os_games
    if _gs_os_games is None:
        with _gs_os_games_lock:
            if _gs_os_games is None:
                _load_gs_os_games()
    return _gs_os_games or []


_GS_OS_CLI_ENV = {
    **os.environ,
    "GS_OS_SERVER_URL": os.environ.get("GS_OS_API_URL", "https://gs-os-dev.backoffice.bagelgames.com"),
    "NODE_TLS_REJECT_UNAUTHORIZED": "0",
}


def _run_gs_os(*args: str, timeout: int = 15) -> Optional[str]:
    """gs-os CLI 실행 후 stdout JSON 반환. 실패 시 None."""
    try:
        r = subprocess.run(
            ["gs-os", *args],
            capture_output=True, text=True, timeout=timeout,
            env=_GS_OS_CLI_ENV,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return r.stdout.strip()
    except Exception as e:
        console.print(f"[red][gs-os {args[0]}] 오류: {e}[/red]")
        return None


def call_mcp_tool(tool_name: str, arguments: dict) -> Optional[str]:
    """GS OS 도구 호출. 쿼리 있는 search_games는 CLI, 나머지는 REST 캐시 또는 CLI."""
    try:
        if tool_name == "search_games":
            query = arguments.get("query", "").strip()
            if query:
                # 자연어 쿼리 → gs-os search (태그 기반 매칭)
                raw = _run_gs_os("search", query)
                if raw:
                    data = json.loads(raw)
                    games = data.get("data", [])
                    return json.dumps({"results": games})
                # CLI 검색 실패(한국어 등) → 빈 결과 (호출측에서 캐시 목록과 병합)
                return json.dumps({"results": []})
            # 쿼리 없으면 전체 목록 반환
            games = _get_gs_os_games()
            return json.dumps({"results": games})

        elif tool_name == "get_game":
            game_name = arguments.get("game_name", "").lower().strip()
            games = _get_gs_os_games()
            # 이름으로 game_id 조회 후 CLI로 상세 정보 가져오기
            for g in games:
                if g.get("game_name", "").lower() == game_name or \
                   g.get("game_code", "").lower() == game_name:
                    game_id = g.get("game_id")
                    if game_id:
                        raw = _run_gs_os("get", str(game_id))
                        if raw:
                            data = json.loads(raw)
                            return json.dumps(data.get("data", g))
                    return json.dumps(g)
            return None

        elif tool_name == "similar_games":
            game_id = arguments.get("game_id")
            top_n = arguments.get("top_n", 5)
            if game_id:
                return _run_gs_os("similar", str(game_id), "--limit", str(top_n))
            return None

        elif tool_name == "resolve_query":
            query = arguments.get("query", "")
            if query:
                return _run_gs_os("search", query)
            return None

        elif tool_name == "portfolio_stats":
            return _run_gs_os("stats")

        elif tool_name == "get_dictionary":
            tag = arguments.get("tag_name", "")
            if tag:
                return _run_gs_os("dict", tag)
            return _run_gs_os("dict")

        else:
            return None

    except Exception as e:
        console.print(f"[red][GS OS:{tool_name}] 오류: {e}[/red]")
        return None


def drive_search_mcp(query: str, page_size: int = 20) -> List[Dict]:
    """GWS CLI를 통해 Google Drive 파일 제목 검색."""
    results: List[Dict] = []
    seen: set = set()

    def _do_search(q: str) -> List[Dict]:
        params = json.dumps({
            "q": q,
            "supportsAllDrives": "true",
            "corpora": "allDrives",
            "pageSize": page_size,
            "orderBy": "name",
        })
        try:
            r = subprocess.run(
                ["gws", "drive", "files", "list", "--params", params, "--format", "json"],
                capture_output=True, text=True, timeout=15,
            )
            return json.loads(r.stdout).get("files", [])
        except Exception:
            return []

    def _append(files: List[Dict]):
        for f in files:
            file_id = f.get("id", "")
            if file_id and file_id not in seen:
                mime = f.get("mimeType", "")
                label = MIME_LABELS.get(mime, mime.split("/")[-1])
                results.append({
                    "id": file_id,
                    "title": f.get("name", ""),
                    "mime_label": label,
                    "url": f"https://drive.google.com/open?id={file_id}",
                })
                seen.add(file_id)

    safe = query.replace("'", "").replace("\u2019", "").replace("\u2018", "")
    # 1차: 파일명 검색 (공백 그대로)
    _append(_do_search(f"name contains '{safe}'"))
    # 2차: 공백 → 언더스코어
    underscore = safe.replace(" ", "_")
    if underscore != safe:
        _append(_do_search(f"name contains '{underscore}'"))
    # 3차: and → & 변환
    ampersand = safe.replace(" and ", " & ")
    if ampersand != safe:
        _append(_do_search(f"name contains '{ampersand}'"))
    # 4차: SB_ 접두사
    if query.upper().startswith("SB_"):
        base = query[3:].replace("'", "").replace("\u2019", "").replace("\u2018", "")
        base_amp = base.replace(" and ", " & ")
        _append(_do_search(f"name contains '{base_amp}_SB'"))
    # 5차: 아포스트로피 포함 쿼리 → 가장 긴 파트
    if "'" in query or "\u2019" in query or "\u2018" in query:
        parts = query.replace("\u2019", "'").replace("\u2018", "'").split("'")
        longest = max(parts, key=len).strip()
        if longest and longest != query.strip():
            safe_longest = longest.replace("'", "").replace("\u2019", "").replace("\u2018", "")
            _append(_do_search(f"name contains '{safe_longest}'"))

    return results


# ── 연관 검색: 동의어 맵 ───────────────────────────────────────────────────────
# 각 단어와 동의어를 등록. 검색 시 원본 단어 대신 동의어 중 하나라도 텍스트에 있으면 매칭.

RELATED: Dict[str, List[str]] = {
    "잭팟":       ["jackpot", "jp"],
    "jackpot":    ["잭팟", "jp"],
    "jp":         ["잭팟", "jackpot"],
    "버그":       ["bug"],
    "bug":        ["버그"],
    "크래시":     ["crash"],
    "crash":      ["크래시", "크래쉬"],
    "정산":       ["payout", "settlement"],
    "payout":     ["정산"],
    "결제":       ["payment", "iap"],
    "payment":    ["결제", "iap"],
    "로그인":     ["login"],
    "login":      ["로그인"],
    "보너스":     ["bonus"],
    "bonus":      ["보너스"],
    "스핀":       ["spin"],
    "spin":       ["스핀"],
    "쿠도":       ["kudo", "kudos"],
    "kudo":       ["쿠도", "kudos"],
    "kudos":      ["쿠도", "kudo"],
    "프리스핀":   ["free spin", "freespin"],
    "멀티플라이어": ["multiplier"],
    "multiplier": ["멀티플라이어"],
    "성능":       ["performance", "fps", "lag"],
    "performance": ["성능", "fps"],
    "네트워크":   ["network"],
    "network":    ["네트워크"],
}


def _norm_ampersand(s: str) -> str:
    """& ↔ and 정규화: 검색어·검색대상 모두에 적용."""
    return s.replace(" & ", " and ").replace("&", "and")


# GPT 번역 캐시 (세션 내 중복 호출 방지)
_TRANS_CACHE: Dict[str, Optional[str]] = {}


def _gpt_translate(word: str) -> Optional[str]:
    """GPT로 단어 하나를 영어↔한국어 번역 (슬롯 게임/QA 도메인)."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    if word in _TRANS_CACHE:
        return _TRANS_CACHE[word]
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"슬롯 게임 QA 용어야. '{word}'의 영어↔한국어 번역어를 "
                    f"딱 한 단어(또는 짧은 구)만 반환해. "
                    f"번역어가 없거나 고유명사면 null 반환. "
                    f"다른 설명 없이 번역어만 반환."
                )
            }],
            max_tokens=15,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip().strip('"\'').lower()
        result = None if raw in ("null", "none", "", word.lower()) else raw
        _TRANS_CACHE[word] = result
        # 역방향 캐시도 저장
        if result:
            _TRANS_CACHE[result] = word
        return result
    except Exception:
        _TRANS_CACHE[word] = None
        return None


def _expand_word(word: str) -> List[str]:
    """단어 하나를 동의어 + GPT 번역어 포함 리스트로 확장."""
    key = word.lower()
    synonyms = [key] + [s.lower() for s in RELATED.get(key, [])]
    # RELATED에 없는 단어만 GPT로 번역 시도
    if key not in RELATED:
        translation = _gpt_translate(key)
        if translation and translation not in synonyms:
            synonyms.append(translation)
    return synonyms


# ── Cache: 로컬 검색 ─────────────────────────────────────────────────────────

def search_jira_local(items: List[Dict], query: str) -> List[Dict]:
    """캐시에서 검색. 각 단어는 동의어 중 하나라도 텍스트에 포함되면 매칭."""
    tokens = _norm_ampersand(query.strip().lower()).split()
    if not tokens:
        return []
    word_groups = [_expand_word(t) for t in tokens]
    results = []
    for r in items:
        text = _norm_ampersand(r.get("key","").lower() + " " + r["summary"].lower() + " " + r["_desc_full"])
        if all(any(syn in text for syn in group) for group in word_groups):
            results.append(r)
    return results


def search_confluence_local(items: List[Dict], query: str) -> List[Dict]:
    """캐시에서 검색. 각 단어는 동의어 중 하나라도 텍스트에 포함되면 매칭."""
    tokens = _norm_ampersand(query.strip().lower()).split()
    if not tokens:
        return []
    word_groups = [_expand_word(t) for t in tokens]
    return [
        r for r in items
        if all(any(syn in _norm_ampersand(r["_search_text"]) for syn in group) for group in word_groups)
    ]


# ── Jira ────────────────────────────────────────────────────────────────────

def search_jira(query: str) -> List[Dict]:
    """Search Jira issues (all pages) — summary + description only."""
    q = query.strip().replace('"', '\\"')
    jql = f'project = {JIRA_PROJECT} AND (summary ~ "{q}" OR description ~ "{q}") ORDER BY updated DESC'
    PAGE_SIZE = 100
    start_at = 0
    all_results: List[Dict] = []
    try:
        while True:
            params = {
                "jql": jql,
                "maxResults": PAGE_SIZE,
                "startAt": start_at,
                "fields": "summary,status,assignee,priority,issuetype,updated,description,labels",
            }
            resp = requests.get(
                f"{JIRA_BASE}/search/jql",
                headers=atlassian_headers(),
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])
            total = data.get("total", 0)
            for issue in issues:
                f = issue["fields"]
                assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")
                updated_raw = f.get("updated", "")
                updated = updated_raw[:10] if updated_raw else "-"
                desc_obj = f.get("description")
                desc = _extract_adf_text(desc_obj) if desc_obj else ""
                all_results.append({
                    "key": issue["key"],
                    "summary": f.get("summary", ""),
                    "status": (f.get("status") or {}).get("name", "-"),
                    "type": (f.get("issuetype") or {}).get("name", "-"),
                    "priority": (f.get("priority") or {}).get("name", "-"),
                    "assignee": assignee,
                    "updated": updated,
                    "description": desc[:120] + "..." if len(desc) > 120 else desc,
                    "url": f"https://{ATLASSIAN_DOMAIN}/browse/{issue['key']}",
                })
            start_at += len(issues)
            if start_at >= total or not issues:
                break
        return all_results
    except requests.HTTPError as e:
        console.print(f"[red][Jira] HTTP error: {e.response.status_code} {e.response.text[:200]}[/red]")
        return all_results
    except Exception as e:
        console.print(f"[red][Jira] Error: {e}[/red]")
        return all_results


def _extract_adf_text(node: Optional[Dict]) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not node:
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    parts = []
    for child in node.get("content", []):
        parts.append(_extract_adf_text(child))
    return " ".join(p for p in parts if p)


# ── Confluence ───────────────────────────────────────────────────────────────

def search_confluence(query: str) -> List[Dict]:
    """Search Confluence pages (all pages) using CQL text ~."""
    q = query.strip().replace('"', '\\"')
    cql = f'type = page AND space in ("GM","CVS") AND text ~ "{q}" ORDER BY lastModified DESC'
    PAGE_SIZE = 50
    start = 0
    all_results: List[Dict] = []
    try:
        while True:
            params = {
                "cql": cql,
                "limit": PAGE_SIZE,
                "start": start,
                "expand": "space,version",
            }
            resp = requests.get(
                f"{CONFLUENCE_BASE}/content/search",
                headers=atlassian_headers(),
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("results", [])
            for item in items:
                space = (item.get("space") or {}).get("name", "-")
                version_by = ((item.get("version") or {}).get("by") or {}).get("displayName", "-")
                last_mod = ((item.get("version") or {}).get("when") or "")[:10]
                all_results.append({
                    "id": item["id"],
                    "title": item.get("title", ""),
                    "space": space,
                    "last_modified": last_mod,
                    "modified_by": version_by,
                    "url": f"https://{ATLASSIAN_DOMAIN}/wiki{item.get('_links', {}).get('webui', '')}",
                })
            start += len(items)
            if len(items) < PAGE_SIZE:
                break
        return all_results
    except requests.HTTPError as e:
        console.print(f"[red][Confluence] HTTP error: {e.response.status_code} {e.response.text[:200]}[/red]")
        return all_results
    except Exception as e:
        console.print(f"[red][Confluence] Error: {e}[/red]")
        return all_results


# ── Display ──────────────────────────────────────────────────────────────────

def display_jira(results: List[Dict]) -> None:
    if not results:
        console.print("[yellow]  결과 없음[/yellow]")
        return
    table = Table(box=box.SIMPLE_HEAD, show_lines=False, expand=True)
    table.add_column("Key", style="cyan bold", no_wrap=True, width=12)
    table.add_column("Type", width=10)
    table.add_column("Summary", style="white")
    table.add_column("Status", width=14)
    table.add_column("Priority", width=10)
    table.add_column("Assignee", width=18)
    table.add_column("Updated", width=10)
    for r in results:
        status_color = {
            "Done": "green", "In Progress": "yellow", "To Do": "blue",
            "In Review": "magenta", "Blocked": "red",
        }.get(r["status"], "white")
        table.add_row(
            r["key"],
            r["type"],
            r["summary"],
            f"[{status_color}]{r['status']}[/{status_color}]",
            r["priority"],
            r["assignee"],
            r["updated"],
        )
    console.print(table)
    for r in results:
        if r["description"]:
            console.print(f"  [dim]{r['key']}[/dim] {r['description']}")
    console.print()


def display_confluence(results: List[Dict]) -> None:
    if not results:
        console.print("[yellow]  결과 없음[/yellow]")
        return
    table = Table(box=box.SIMPLE_HEAD, show_lines=False, expand=True)
    table.add_column("Title", style="cyan")
    table.add_column("Space", width=20)
    table.add_column("Last Modified", width=12)
    table.add_column("Modified By", width=20)
    table.add_column("URL", style="dim")
    for r in results:
        table.add_row(r["title"], r["space"], r["last_modified"], r["modified_by"], r["url"])
    console.print(table)
    console.print()


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """QA 통합 검색 툴 — Jira (GS) · Confluence"""


@cli.command()
@click.argument("query")
@click.option("--jira/--no-jira", default=True, help="Jira 검색 포함 여부")
@click.option("--confluence/--no-confluence", default=True, help="Confluence 검색 포함 여부")
def search(query: str, jira: bool, confluence: bool):
    """Jira + Confluence 통합 검색 (전체 결과)."""
    _check_env()
    console.print()
    console.print(Panel(
        f"[bold white]검색어:[/bold white] [yellow]{query}[/yellow]",
        title="[bold cyan]QA 통합 검색[/bold cyan]",
        border_style="cyan",
    ))

    if jira:
        console.rule(f"[bold cyan]Jira  [{JIRA_PROJECT}]")
        console.print()
        display_jira(search_jira(query))

    if confluence:
        console.rule("[bold cyan]Confluence")
        console.print()
        display_confluence(search_confluence(query))


@cli.command()
@click.argument("query")
def jira(query: str):
    """Jira GS 프로젝트 전체 검색."""
    _check_env()
    console.rule(f"[bold cyan]Jira [{JIRA_PROJECT}] — {query}")
    console.print()
    display_jira(search_jira(query))


@cli.command()
@click.argument("query")
def confluence(query: str):
    """Confluence 전체 검색."""
    _check_env()
    console.rule(f"[bold cyan]Confluence — {query}")
    console.print()
    display_confluence(search_confluence(query))


@cli.command()
@click.argument("issue_key")
def issue(issue_key: str):
    """Jira 이슈 상세 조회 (예: GS-123)."""
    _check_env()
    try:
        resp = requests.get(
            f"{JIRA_BASE}/issue/{issue_key}",
            headers=atlassian_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        f = data["fields"]
        desc = _extract_adf_text(f.get("description"))
        console.print()
        console.print(Panel(
            f"[bold]{f.get('summary')}[/bold]\n\n"
            f"[dim]상태:[/dim] {(f.get('status') or {}).get('name', '-')}  "
            f"[dim]타입:[/dim] {(f.get('issuetype') or {}).get('name', '-')}  "
            f"[dim]우선순위:[/dim] {(f.get('priority') or {}).get('name', '-')}\n"
            f"[dim]담당자:[/dim] {(f.get('assignee') or {}).get('displayName', 'Unassigned')}  "
            f"[dim]보고자:[/dim] {(f.get('reporter') or {}).get('displayName', '-')}\n\n"
            f"{desc[:500]}{'...' if len(desc) > 500 else ''}\n\n"
            f"[dim]{f'https://{ATLASSIAN_DOMAIN}/browse/{issue_key}'}[/dim]",
            title=f"[bold cyan]{issue_key}[/bold cyan]",
            border_style="cyan",
        ))
    except requests.HTTPError as e:
        console.print(f"[red]HTTP error {e.response.status_code}: {e.response.text[:200]}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _check_env() -> None:
    missing = []
    if not ATLASSIAN_API_TOKEN:
        missing.append("ATLASSIAN_API_TOKEN")
    if not ATLASSIAN_EMAIL:
        missing.append("ATLASSIAN_EMAIL")
    if missing:
        console.print(f"[yellow]경고: 다음 환경변수가 설정되지 않았습니다: {', '.join(missing)}[/yellow]")
        console.print("[dim].env 파일을 확인하세요 (.env.example 참고)[/dim]")


if __name__ == "__main__":
    cli()
