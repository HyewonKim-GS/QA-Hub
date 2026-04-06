# QA Hub 서버 재시작

포트 8000에서 실행 중인 서버를 종료하고 재시작한다.

## 실행 순서

1. 포트 8000 프로세스 확인
2. 기존 프로세스 종료
3. 서버 재시작
4. 헬스체크 (최대 10초 대기)
5. 결과 보고

## 커맨드

```bash
# 기존 프로세스 종료
kill $(lsof -ti :8000) 2>/dev/null && echo "기존 프로세스 종료" || echo "실행 중인 프로세스 없음"

# 재시작
cd ~/qa-search && .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --log-level warning &

# 헬스체크
sleep 3 && curl -s -o /dev/null -w "상태: %{http_code}" http://localhost:8000/api/status
```

## 주의

- `reload=True` 사용 금지 — lifespan 캐시와 충돌
- 재시작 후 캐시 로드까지 약 30초~1분 소요 (Jira 6000+건 수집)
- 캐시 상태 확인: `curl -s http://localhost:8000/api/status`
