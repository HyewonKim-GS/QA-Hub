# 게임 패널 문서 링크 (`/api/game_links`)

게임 카드를 클릭하면 열리는 사이드 패널 상단에 TC / GDD / MATH / CTD / 사운드 / 연출 6개 링크를 표시한다.

---

## 소스 목록

| 링크 | 소스 | 매칭 기준 |
|------|------|-----------|
| TC | `qa_sheet_id` (스케줄 데이터) | 프론트에서 직접 전달 |
| GDD | Drive 폴더 실시간 검색 | tc_prefix → 아포스트로피 분리 최장 파트 → & 치환 → 게임명 |
| MATH | Drive 폴더 실시간 검색 | 동일 |
| CTD | CTD 시트 `Game Info` 탭 캐시 | game_id → game_name → tc_code |
| 사운드 | 사운드 시트 탭 캐시 | `tc_prefix` (3~4자리 코드) |
| 연출 | 연출 리스트 시트 탭 캐시 | 게임명 정규화 퍼지 매칭 |

---

## 시트 / 폴더 상수 (`app.py`)

```python
_SOUND_SHEET_ID     = "110ROCiEItteR_A-9yanFmbv-VNksDEGN3-tBJQ3y33w"
_DIRECTION_SHEET_ID = "1tfnyPFAtrjiaZCBrpFW9tOUpyqi0LMr17dPgfeTlm38"
_CONTENTS_SHEET_ID  = "1qDCoxHalm1ohVW6FCIqfJ55w29bPnE8_FRZDEdPnpIE"  # CTD (Game Info 탭)
_CTD_GAME_INFO_GID  = 491056088
_GDD_FOLDER_IDS     = [
    "1cGFkhVp9gTVYge6PRAJTwrFepz47HyJH",   # Dnipro GDD
    "1u6mG6JNl4OP-_AdqzkRfvTF8_0e6B8R6",   # V3 Seoul Game Design Doc
]
_MATH_FOLDER_ID     = "1M9_XL6YxhBeCnt0ZxzzyHd6cZ2hGXKZ5"   # v3 Math Models
```

폴더/시트가 바뀌면 이 상수만 수정하면 된다.

---

## 캐시 구조

서버 시작 시 `_fetch_doc_tabs()` 에서 아래 3개를 채운다.

```
CACHE["sound_tabs"]     = {탭명.lower(): gid, ...}   # 102개 탭
CACHE["direction_tabs"] = {탭명.lower(): gid, ...}   # 72개 탭
CACHE["ctd_game_info"]  = [                           # 540여 행
    {"row_num": 2, "game_id_str": "1", "game_name": "Jungle Quest", "game_title": "SS/JGQ"},
    ...
]
```

탭이 추가되면 서버 재시작 또는 `POST /api/refresh` 로 반영된다.

---

## 매칭 로직 상세

### `_norm_tab(s)` — 탭/게임명 정규화

```
소문자 → 아포스트로피 제거 → 하이픈·언더스코어 → 공백 → & → and → 공백 단일화
예) "SuperCharge X-treme" → "supercharge x treme"
예) "Big Cluckin' Winz"   → "big cluckin winz"
예) "Roar & Flame"        → "roar and flame"
```

### `_search_keywords(name, prefix)` — Drive 검색 키워드 우선순위

1. `prefix.upper()` → e.g. `SJG`
2. 아포스트로피 분리 최장 파트 → e.g. `Luck'n'Roll` → `Roll Wheels`
3. `& → and` 치환 → e.g. `Roar and Flame`
4. 원본 게임명

### `_drive_search_in_folders(keyword, folder_ids)` — Drive 검색

- `trashed=false` 조건 포함
- 폴더 hit 시:
  - **SB 게임** (`is_sb=1`): 폴더 내부에서 `SB_` 파일 확인 후 **그 폴더 URL** 반환 (파일 직접 링크 시 Drive viewer로 강제 진입하는 문제 방지). 없으면 부모 폴더 URL fallback
  - **일반 게임**: 폴더 URL 반환 (서브폴더 구조 대응)
- 폴더 없으면 파일 URL 반환

### Sound 탭 매칭

`tc_prefix`가 없으면 `sheet_games` 캐시에서 `game_code`를 자동으로 가져온다.
→ GAMES 상수에 없는 게임도 동작함.

### 연출 탭 매칭

1. `_norm_tab(게임명)` 완전 일치 우선
2. 포함 관계: 더 길게 겹치는 탭 우선 (오탐 방지)

### CTD 매칭 우선순위

1. `game_id` 숫자 일치 (가장 정확)
2. `game_name` 소문자 일치
3. `tc_code`가 `game_title`(e.g. `SS/SJG`) 안에 포함

---

## API

```
GET /api/game_links?name=Star+Juggler&tc_prefix=sjg&game_id=306
GET /api/game_links?name=Roar+%26+Flame&tc_prefix=rnf&game_id=321&is_sb=1
```

`is_sb=1` 전달 시 GDD 폴더 내부에서 `SB_` 파일 우선 탐색.

응답:
```json
{
  "gdd":       "https://drive.google.com/open?id=...",
  "math":      "https://drive.google.com/open?id=...",
  "sound":     "https://docs.google.com/spreadsheets/d/.../edit#gid=...",
  "direction": "https://docs.google.com/spreadsheets/d/.../edit#gid=...",
  "ctd":       "https://docs.google.com/spreadsheets/d/.../edit#gid=...&range=B305"
}
```

`tc_prefix` / `game_id` 는 선택 파라미터. 없으면 서버가 `sheet_games` 캐시에서 자동 보완.
링크를 못 찾으면 해당 키는 `null`.

---

## 프론트엔드 연동 (`hub_v8.html`)

`openPanel()` 내에서 `_glParams` 구성 후 `/api/search` 와 병렬로 호출:

```js
const _glParams = new URLSearchParams({name: baseName});
if (g.tc_prefix) _glParams.set('tc_prefix', g.tc_prefix);
if (g.game_id)   _glParams.set('game_id', String(g.game_id));
// ...
fetch('/api/game_links?' + _glParams)
```

`_renderPanelBody(g, data, links)` 의 `_docLinks` 배열로 UI 렌더링:

```js
const _docLinks = [
  {key:'tc',        label:'TC',    icon:'📋'},
  {key:'gdd',       label:'GDD',   icon:'📖'},
  {key:'math',      label:'MATH',  icon:'🔢'},
  {key:'ctd',       label:'CTD',   icon:'📊'},
  {key:'sound',     label:'사운드', icon:'🔊'},
  {key:'direction', label:'연출',   icon:'🎬'},
];
```

링크 있으면 클릭 가능한 버튼, 없으면 회색 비활성 버튼으로 표시.

---

## 변경 시 체크리스트

- [ ] 폴더/시트 ID 바뀜 → `app.py` 상수 + 이 문서 업데이트
- [ ] 새 문서 종류 추가 → `api_game_links` result dict + `_docLinks` 배열 + 이 문서
- [ ] 매칭 안 되는 게임 → 실제 탭명/파일명 확인 후 `_norm_tab` 또는 `_search_keywords` 보완
- [ ] GDD 서브폴더 구조 변경 → `_drive_search_in_folders` 폴더 우선 로직 확인

---

## 테스트

```bash
# 기본 테스트
curl "http://localhost:8000/api/game_links?name=Star+Juggler&tc_prefix=sjg&game_id=306"

# prefix 없이 (자동 보완 확인)
curl "http://localhost:8000/api/game_links?name=Supercharge+X-treme"

# 아포스트로피 게임명
curl "http://localhost:8000/api/game_links?name=Luck%27n%27Roll+Wheels"

# & 포함 게임명
curl "http://localhost:8000/api/game_links?name=Roar+%26+Flame&tc_prefix=rnf&game_id=321"
```
