# QA Hub

Game Studio QA팀 전용 통합 검색 및 어시스턴트 도구

## 주요 기능

- **Hub AI** — Jira·Confluence 데이터 기반 채팅 어시스턴트. 게임 정보, TC 진행률, 라이브 이슈, 포트폴리오 통계 조회
- **키워드 검색** — Jira·Confluence·Google Drive·Slack 통합 검색
- **대시보드** — 이번 주 버그 요약, QA 타임라인
- **슬롯별 이슈** — 게임별 이슈 추이 및 등급 분석
- **라이브 이슈** — 출시 게임 이슈 현황 (연도/월별)
- **스케줄** — QA 일정 관리 및 TC 진행률 연동

## 기술 스택

- Backend: Python 3.9 / FastAPI / Uvicorn
- 데이터 소스: Jira, Confluence, Google Drive, Slack, gs-os-ontology MCP
- LLM: OpenAI-compatible API
- repob CLI — 사내 games 레포에서 게임 브랜치 및 코드 참조 조회 (Hub AI 컨텍스트 보강)

## 실행 방법

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env에 아래 값 입력
# ATLASSIAN_DOMAIN, ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, JIRA_PROJECT, OPENAI_API_KEY
# OPENAI_API_KEY 미설정 시 Hub AI 채팅 기능만 비활성화되며 나머지 기능은 정상 동작

# 서버 실행
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --log-level warning
```

서버 시작 시 Jira·Confluence 데이터를 자동으로 캐시하며, 이후 1시간마다 자동 갱신됩니다.

## 의존성

### MCP 서버 (gs-os-ontology)
Google Drive 및 게임 온톨로지 조회에 사용. SSE 방식으로 `http://172.16.50.144:3100`에 연결합니다. 사내 네트워크(VPN) 필요.

### repob CLI
Hub AI가 게임 관련 질문에 답할 때 사내 games 레포의 코드를 참조하는 데 사용합니다. 별도 설치 필요 — 설치되지 않은 경우 코드 참조 기능만 비활성화되며 나머지 기능은 정상 동작합니다.

## API

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /hub` | Hub UI |
| `GET /api/status` | 캐시 상태 확인 |
| `POST /api/refresh` | 캐시 수동 갱신 |
| `GET /api/search` | 통합 검색 |
| `POST /api/chat` | Hub AI 채팅 |
| `GET /api/schedule` | QA 스케줄 목록 |
| `GET /api/tc_progress` | TC 진행률 |
| `GET /api/live_issues` | 라이브 이슈 |
| `GET /api/game_links` | 게임 문서 링크 (GDD/MATH/CTD/Sound/연출) |
