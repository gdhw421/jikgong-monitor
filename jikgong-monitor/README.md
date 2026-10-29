# jikgong-monitor

농산물 도매가 폭락 감지 모니터 v0. KAMIS(농산물유통정보) 오픈 API로 서울 도매가를 매일 받아 1주·1개월·평년 대비 하락률을 계산하고, 상온 유통 가능 품목 중 임계치를 넘는 것을 알림으로 남긴다. 누적 로그는 이후 선정 모델 학습 데이터로 쓴다.

## 구성

| 파일 | 역할 |
|---|---|
| `monitor.py` | 수집·계산·기록 |
| `config.json` | 대상 카테고리, 상온 품목 키워드, 폭락 임계치 |
| `.github/workflows/monitor.yml` | 매일 07:10 KST 자동 실행, 결과 커밋 |
| `STATUS.md` | 최신 상태 요약 (자동 생성) |
| `data/log.csv` | 누적 로그 (자동 생성) |
| `data/alerts/` | 날짜별 알림 (자동 생성) |
| `data/raw/` | API 원본 응답 (자동 생성) |

## 설치

1. 이 파일들을 저장소에 올린다.
2. 저장소 **Settings → Secrets and variables → Actions** 에 두 개 등록
   - `KAMIS_CERT_KEY` — KAMIS에서 발급받은 인증키
   - `KAMIS_CERT_ID` — KAMIS 아이디
3. **Actions → daily-monitor → Run workflow** 로 수동 1회 실행. 성공하면 `STATUS.md`가 생긴다.
4. 이후 매일 자동 실행.

로컬에서 돌릴 때:

```
pip install -r requirements.txt
KAMIS_CERT_KEY=... KAMIS_CERT_ID=... python monitor.py
```

## 판정 규칙 (v0)

서울 도매가 당일 값을 기준으로 세 가지 하락률을 본다.

- 1개월 전 대비 −20% 이하
- 1주 전 대비 −12% 이하
- 평년 대비 −15% 이하

`config.json`의 `min_signals` 개 이상 해당하면 알림. 임계치는 11월 실험 결과를 보고 조정한다.

## 첫 실행 후 확인할 것

`data/raw/`의 원본 JSON에서 가격 필드가 `dpr1`~`dpr7` 이름인지 확인한다. 다르면 `monitor.py` 상단의 `FIELD_*` 상수만 바꾸면 된다.
