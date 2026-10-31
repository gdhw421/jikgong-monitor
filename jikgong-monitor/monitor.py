"""직공 폭락 감지 모니터 v0

매일 1회 KAMIS(농산물유통정보) 오픈 API에서 도매가를 받아
1주·1개월·평년 대비 하락률을 계산하고, 임계치를 넘는 품목을 알림으로 기록한다.

산출물
  data/raw/YYYY-MM-DD_<카테고리>.json   API 원본 응답 (디버깅·검증용)
  data/log.csv                           누적 로그. 2라운드 학습 데이터 1차분
  data/alerts/YYYY-MM-DD.md              당일 폭락 알림
  STATUS.md                              최신 상태 요약 (저장소 첫 화면용)

환경변수
  KAMIS_CERT_KEY, KAMIS_CERT_ID          KAMIS 발급 키·아이디
  RUN_DATE (선택)                         yyyy-mm-dd. 없으면 어제(KST) — 당일가는 오후 확정
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))
API = "https://www.kamis.or.kr/service/price/xml.do"

# KAMIS dailyPriceByCategoryList 응답의 가격 필드.
# dpr1=당일, dpr2=1일전, dpr3=1주일전, dpr4=2주일전, dpr5=1개월전, dpr6=1년전, dpr7=일평년
FIELD_TODAY, FIELD_WEEK, FIELD_MONTH, FIELD_YEAR, FIELD_NORMAL = "dpr1", "dpr3", "dpr5", "dpr6", "dpr7"


def load_config():
    with open(ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


def to_num(v):
    """'62,421' → 62421.0. '-' 또는 빈값은 None."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "0"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pct(now, base):
    if now is None or base is None or base == 0:
        return None
    return round((now - base) / base * 100, 1)


def fetch_category(cfg, category, run_date, key, cert_id):
    params = {
        "action": "dailyPriceByCategoryList",
        "p_product_cls_code": cfg["product_cls_code"],
        "p_item_category_code": category,
        "p_country_code": cfg["country_code"],
        "p_regday": run_date,
        "p_convert_kg_yn": "N",
        "p_cert_key": key,
        "p_cert_id": cert_id,
        "p_returntype": "json",
    }
    r = requests.get(API, params=params, timeout=30)
    r.raise_for_status()
    body = r.json()

    raw_path = DATA / "raw" / f"{run_date}_{category}.json"
    raw_path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    # 응답 구조: {"condition":[...], "data": {"error_code":"000", "item":[...]}}
    data = body.get("data", {})
    if isinstance(data, list):  # 오류 시 data가 문자열/리스트로 오는 경우가 있다
        print(f"[{category}] 응답 이상: {data}", file=sys.stderr)
        return []
    if str(data.get("error_code", "000")) != "000":
        print(f"[{category}] API 오류 코드 {data.get('error_code')}", file=sys.stderr)
        return []
    items = data.get("item", [])
    return items if isinstance(items, list) else [items]


def is_ambient(name, items):
    if not items:
        return True
    return name.strip() in items


def evaluate(item, cfg):
    name = item.get("item_name", "")
    kind = item.get("kind_name", "")
    today = to_num(item.get(FIELD_TODAY))
    week = to_num(item.get(FIELD_WEEK))
    month = to_num(item.get(FIELD_MONTH))
    year = to_num(item.get(FIELD_YEAR))
    normal = to_num(item.get(FIELD_NORMAL))

    row = {
        "item_name": name,
        "kind_name": kind,
        "rank": item.get("rank", ""),
        "unit": item.get("unit", ""),
        "price": today,
        "vs_week_pct": pct(today, week),
        "vs_month_pct": pct(today, month),
        "vs_year_pct": pct(today, year),
        "vs_normal_pct": pct(today, normal),
    }

    th = cfg["thresholds"]
    signals = []
    if row["vs_month_pct"] is not None and row["vs_month_pct"] <= th["drop_vs_month_pct"]:
        signals.append(f"1개월 대비 {row['vs_month_pct']}%")
    if row["vs_week_pct"] is not None and row["vs_week_pct"] <= th["drop_vs_week_pct"]:
        signals.append(f"1주 대비 {row['vs_week_pct']}%")
    if row["vs_normal_pct"] is not None and row["vs_normal_pct"] <= th["below_normal_pct"]:
        signals.append(f"평년 대비 {row['vs_normal_pct']}%")

    row["signals"] = signals
    row["alert"] = today is not None and len(signals) >= cfg["min_signals"]
    return row


def append_log(run_date, rows):
    path = DATA / "log.csv"
    new = not path.exists()
    fields = ["date", "category", "item_name", "kind_name", "rank", "unit", "price",
              "vs_week_pct", "vs_month_pct", "vs_year_pct", "vs_normal_pct", "ambient", "alert"]
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields} | {"date": run_date})


def fmt(v):
    return "–" if v is None else f"{v:+.1f}%"


def write_alerts(run_date, alerts):
    path = DATA / "alerts" / f"{run_date}.md"
    lines = [f"# 폭락 알림 {run_date}", ""]
    if not alerts:
        lines.append("임계치를 넘은 상온 품목 없음.")
    else:
        lines += ["| 품목 | 품종 | 등급 | 단위 | 도매가 | 1주 | 1개월 | 평년 | 신호 |",
                  "|---|---|---|---|---:|---:|---:|---:|---|"]
        for a in alerts:
            lines.append(
                f"| {a['item_name']} | {a['kind_name']} | {a['rank']} | {a['unit']} | "
                f"{a['price']:,.0f} | {fmt(a['vs_week_pct'])} | {fmt(a['vs_month_pct'])} | "
                f"{fmt(a['vs_normal_pct'])} | {', '.join(a['signals'])} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_status(run_date, rows, alerts, cfg):
    ambient = [r for r in rows if r["ambient"] and r["price"] is not None]
    # 알림 행 먼저 → 1개월 대비 하락률 오름차순 → 1개월 비교값 없는 행(신규출하)은 맨 뒤
    ambient.sort(key=lambda r: (
        0 if r["alert"] else 1,
        1 if r["vs_month_pct"] is None else 0,
        r["vs_month_pct"] if r["vs_month_pct"] is not None else 0,
    ))
    th = cfg["thresholds"]
    lines = [
        "# 직공 폭락 감지 모니터 — 현재 상태",
        "",
        f"마지막 실행: {run_date} (KST). 기준: 서울 도매가(KAMIS).",
        f"판정 기준: 1개월 대비 {th['drop_vs_month_pct']}% / 1주 대비 {th['drop_vs_week_pct']}% / "
        f"평년 대비 {th['below_normal_pct']}% 이하, {cfg['min_signals']}개 이상 해당 시 알림.",
        "",
        f"## 오늘의 알림 ({len(alerts)}건)",
        "",
    ]
    if alerts:
        for a in alerts:
            lines.append(f"- **{a['item_name']} {a['kind_name']}** ({a['rank']}, {a['unit']}) "
                         f"{a['price']:,.0f}원 — {', '.join(a['signals'])}")
    else:
        lines.append("없음")
    lines += ["", "## 상온 유통 후보 품목 — 알림 우선, 1개월 대비 하락 순", "",
              "| 품목 | 품종 | 등급 | 단위 | 도매가 | 1주 | 1개월 | 1년 | 평년 |",
              "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for r in ambient:
        month = "신규출하" if r["vs_month_pct"] is None else fmt(r["vs_month_pct"])
        lines.append(
            f"| {r['item_name']} | {r['kind_name']} | {r['rank']} | {r['unit']} | {r['price']:,.0f} | "
            f"{fmt(r['vs_week_pct'])} | {month} | {fmt(r['vs_year_pct'])} | {fmt(r['vs_normal_pct'])} |"
        )
    lines += ["", f"전체 수집 {len(rows)}개 행, 상온 후보 {len(ambient)}개. 누적 로그: `data/log.csv`"]
    (ROOT / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    key = os.environ.get("KAMIS_CERT_KEY")
    cert_id = os.environ.get("KAMIS_CERT_ID")
    if not key or not cert_id:
        sys.exit("KAMIS_CERT_KEY / KAMIS_CERT_ID 환경변수가 필요하다.")

    cfg = load_config()
    (DATA / "raw").mkdir(parents=True, exist_ok=True)
    (DATA / "alerts").mkdir(parents=True, exist_ok=True)
    # KAMIS 당일 도매가는 오후에 확정되므로 기본 조회일은 어제(KST)
    run_date = os.environ.get("RUN_DATE") or (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"실행일 {run_date}")

    rows = []
    for cat in cfg["categories"]:
        items = fetch_category(cfg, cat, run_date, key, cert_id)
        print(f"[{cat}] {len(items)}개 품목")
        for it in items:
            r = evaluate(it, cfg)
            r["category"] = cat
            r["ambient"] = is_ambient(r["item_name"], cfg["ambient_items"])
            rows.append(r)

    if not rows:
        sys.exit("수집된 데이터가 없다. data/raw/ 의 원본 응답을 확인할 것.")

    alerts = [r for r in rows if r["ambient"] and r["alert"]]
    alerts.sort(key=lambda r: r["vs_month_pct"] if r["vs_month_pct"] is not None else 0)

    append_log(run_date, rows)
    write_alerts(run_date, alerts)
    write_status(run_date, rows, alerts, cfg)
    print(f"알림 {len(alerts)}건: " + ", ".join(a["item_name"] for a in alerts))


if __name__ == "__main__":
    main()
