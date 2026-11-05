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

## 컨택 후보 스토어 리스트 (stores.py)

모니터가 성공으로 끝나면 `daily-stores` 워크플로가 이어서 돈다. 결과는 `STORES.md`(매일 교체)와 `data/stores/`(날짜별 보관).

- 대상: 가장 최근 알림 품목. 품목당 최대 3곳, 스토어는 리스트 전체에서 한 번만.
- 후보: NAVER API HUB 웹문서 검색으로 "{지역} {품목} 스마트스토어"를 찾아 스마트스토어 주소만 남긴다. (쇼핑 검색 API는 2026-07-31 종료)
- 산지: 노선 지역 이름으로 검색하고, 결과 글에 그 지역과 품목이 모두 있을 때만 선정. 아니면 '품목·지역 확인 필요'.
- 노선: `data/routes.csv`에 있는 시·군만 인정. 고양종합터미널 직행(환승 없음) 기준. 분기마다 확인해 고칠 것.
- 컨택 제외: 구글 시트 컨택목록에 적힌 스토어는 뺀다. '회신 대기'로 14일이 지나면 다시 노출.

필요한 저장소 Secrets: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`(NAVER API HUB Application의 Client ID·Secret), `CONTACTS_CSV_URL`(선택).
