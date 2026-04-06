# 배포 체크리스트

QA Hub를 사내 서버에 배포하기 전 체크리스트를 실행한다.

## 체크리스트 실행

각 항목을 순서대로 확인하고 결과를 보고한다.

### 1. 코드 상태 확인

```bash
cd ~/qa-search
git status
git log --oneline -5
```

- 커밋되지 않은 변경사항 있으면 커밋 먼저
- 민감정보(.env)가 git에 포함되지 않았는지 확인

### 2. .env 필수값 확인

```bash
grep -E "^(ATLASSIAN_DOMAIN|ATLASSIAN_EMAIL|ATLASSIAN_API_TOKEN|OPENAI_API_KEY|MCP_SSE_URL)" ~/qa-search/.env
```

- 모든 값이 채워져 있어야 함
- `your-*` placeholder가 남아있으면 안 됨

### 3. 로컬 경로 하드코딩 확인

```bash
grep -rn "/Users/" ~/qa-search/app.py ~/qa-search/search.py
```

- `_REPOB_BIN`은 `os.getenv`로 감싸져 있어야 함 (완료)
- 그 외 `/Users/` 경로가 있으면 환경변수로 빼야 함

### 4. 의존성 확인

```bash
cat ~/qa-search/requirements.txt
```

- 서버에 설치될 패키지 목록 확인
- `holidays` 패키지 포함 여부 확인

### 5. MCP 서버 연결 확인

```bash
curl -s --max-time 5 http://172.16.50.144:3100/health
```

- `{"status":"ok"}` 응답 있어야 함
- 실패 시 사내 VPN 연결 또는 서버 담당자 확인

### 6. 로컬 서버 최종 동작 확인

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/hub
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/presentation
curl -s http://localhost:8000/api/status | python3 -m json.tool
```

- `/hub`: 200
- `/presentation`: 200
- `api/status`: `ready: true`, 에러 없음

## IT 요청 사항 (배포 서버)

```
1. Python 3.9+, pip 설치
2. gws CLI 설치 + Google OAuth 인증
3. repob 바이너리 배치 후 경로를 REPOB_BIN에 세팅
4. .env 파일 세팅 (아래 값 전달)
5. uvicorn app:app --host 0.0.0.0 --port 8000 상시 실행 (PM2 or systemd)
6. 내부 도메인 연결 (예: qa-hub.bagel.internal)
7. Atlassian 서비스 계정 생성 요청 (gs-qa-hub@bagelcode.com, GS 프로젝트 읽기 권한)
```

## .env 전달 템플릿

```
ATLASSIAN_DOMAIN=bagelcode.atlassian.net
ATLASSIAN_EMAIL=gs-qa-hub@bagelcode.com
ATLASSIAN_API_TOKEN=<서비스 계정 토큰>
JIRA_PROJECT=GS
SLACK_BOT_TOKEN=<슬랙 봇 토큰>
OPENAI_API_KEY=<OpenAI 키>
MCP_SSE_URL=http://172.16.50.144:3100
REPOB_BIN=<서버에서 repob 바이너리 경로>
```
