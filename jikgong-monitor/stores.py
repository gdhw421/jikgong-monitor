"""직공 컨택 후보 스토어 리스트 v0

폭락 감지 모니터(monitor.py)가 끝난 뒤 실행한다.
가장 최근 알림 품목마다 네이버 쇼핑에서 스마트스토어를 찾아,
산지가 고양종합터미널 직행 노선이 있는 시·군인 곳을 품목당 최대 3곳 뽑는다.

산출물
  STORES.md                    오늘의 리스트 (매일 교체)
  data/stores/YYYY-MM-DD.md    날짜별 보관본
  data/stores/YYYY-MM-DD.csv   검토한 후보 전체와 판정 사유 (검증용)

입력
  data/log.csv                 모니터 누적 로그. 가장 최근 날짜의 알림 행을 쓴다
  data/routes.csv              고양종합터미널 직행 노선이 있는 시·군
  data/regions.json            산지 추정용 시·군 이름

환경변수
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET   네이버 검색 오픈 API 키
  CONTACTS_CSV_URL (선택)                 컨택목록 구글 시트의 'CSV로 웹에 게시' 주소
"""

import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))
NAVER_API = "https://openapi.naver.com/v1/search/shop.json"


# ---------- 입력 읽기 ----------

def load_alerts():
    """log.csv에서 가장 최근 날짜의 상온 알림 행을 품목(검색어)별로 묶는다."""
    with open(DATA / "log.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None, {}
    run_date = max(r["date"] for r in rows)
    groups = {}
    for r in rows:
        if r["date"] == run_date and r["ambient"] == "True" and r["alert"] == "True":
            groups.setdefault(search_name(r["item_name"], r["kind_name"]), []).append(r)
    return run_date, groups


def search_name(item, kind):
    """검색에 쓸 품목명. '파 / 쪽파(1kg)' → 쪽파, '깐마늘(국산)' → 깐마늘, '감자 / 수미(노지)' → 감자."""
    item = re.sub(r"\(.*?\)", "", item).strip()
    kind = re.sub(r"\(.*?\)", "", kind).strip()
    if kind and kind != item and kind.endswith(item):
        return kind
    return item


def load_routes(require_parcel):
    routes = {}
    with open(DATA / "routes.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if require_parcel and r["소화물"].strip().upper() != "O":
                continue
            routes.setdefault(r["시군"].strip(), []).append(r)
    return routes


def load_regions():
    with open(DATA / "regions.json", encoding="utf-8") as f:
        j = json.load(f)
    names = {}  # 시군 → 시도
    for sido, lst in j["regions"].items():
        for n in lst:
            names.setdefault(n, sido)
    return names, set(j.get("strict", [])), j.get("aliases", {})


def norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def load_contacts(url, recontact_days, today):
    """컨택목록에서 리스트에서 뺄 스토어명을 읽는다. (제외 집합, 경고문) 반환."""
    if not url:
        return set(), "컨택목록 주소(CONTACTS_CSV_URL)가 없어 제외 없이 만들었음."
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
    except Exception as e:  # 시트를 못 읽어도 리스트는 만든다
        return set(), f"컨택목록을 읽지 못해 제외 없이 만들었음 ({type(e).__name__})."
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "스토어명" not in rows[0] or "상태" not in rows[0]:
        return set(), "컨택목록 형식이 달라 제외 없이 만들었음 (첫 줄에 '스토어명', '상태'가 있어야 함)."
    head = rows[0]
    i_name, i_state = head.index("스토어명"), head.index("상태")
    i_date = head.index("연락일") if "연락일" in head else None
    excluded = set()
    for row in rows[1:]:
        name = row[i_name].strip() if len(row) > i_name else ""
        if not name or name.startswith("(예시)"):
            continue
        state = row[i_state].strip() if len(row) > i_state else ""
        if state == "회신 대기" and i_date is not None and len(row) > i_date:
            nums = re.findall(r"\d+", row[i_date])  # 2026-11-05, 2026. 11. 5 모두 허용
            if len(nums) >= 3:
                try:
                    d = datetime(int(nums[0]), int(nums[1]), int(nums[2])).date()
                    if (today - d).days >= recontact_days:
                        continue  # 무응답 기간이 지나 다시 노출
                except ValueError:
                    pass
        excluded.add(norm(name))
    return excluded, None


# ---------- 네이버 쇼핑 검색 ----------

def naver_search(query, cid, secret):
    r = requests.get(
        NAVER_API,
        params={"query": query, "display": 100, "sort": "sim", "exclude": "used:rental:cbshop"},
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def find_region(text, names, strict, aliases):
    """글에서 가장 먼저 나오는 시·군 이름을 찾는다. 없으면 None."""
    best = None  # (위치, 시군)
    taken = [False] * len(text)
    keys = sorted(list(names) + list(aliases), key=len, reverse=True)  # 남양주가 양주보다 먼저
    for key in keys:
        for m in re.finditer(re.escape(key), text):
            s, e = m.span()
            if any(taken[s:e]):
                continue
            if key in strict and not re.match(r"[시군산]", text[e:e + 1] or " "):
                continue
            for i in range(s, e):
                taken[i] = True
            if best is None or s < best[0]:
                best = (s, aliases.get(key, key))
    return best[1] if best else None


def candidates_for(name, scfg, cid, secret, names, strict, aliases):
    """한 품목의 후보 스토어. 스토어당 1행, 검색 순위 순."""
    raw = []
    for q in scfg["queries"]:
        raw += naver_search(q.replace("{품목}", name), cid, secret)

    smart = [it for it in raw if "smartstore.naver.com" in it.get("link", "")]
    pool = smart if (scfg.get("only_smartstore") and smart) else raw
    fallback = bool(scfg.get("only_smartstore")) and bool(raw) and not smart

    seen, out = set(), []
    for rank, it in enumerate(pool):
        mall = (it.get("mallName") or "").strip()
        title = re.sub(r"<.*?>", "", it.get("title", ""))
        if not mall or mall in scfg["exclude_malls"] or norm(mall) in seen:
            continue
        if scfg.get("category2") and it.get("category2") != scfg["category2"]:
            continue
        if any(w in title for w in scfg["exclude_title"]):
            continue
        seen.add(norm(mall))
        region = find_region(title, names, strict, aliases) or find_region(mall, names, strict, aliases)
        out.append({
            "품목": name, "스토어": mall, "상품명": title, "링크": it.get("link", ""),
            "최저가": it.get("lprice", ""), "산지": region or "",
            "시도": names.get(region, "") if region else "",
            "생산자": any(k in mall for k in scfg["producer_keywords"]), "순위": rank,
        })
    out.sort(key=lambda c: (0 if c["생산자"] else 1, c["순위"]))
    return out, len(raw), fallback


# ---------- 판정·출력 ----------

def signal_text(rows, th):
    parts = []
    for r in rows:
        sig = []
        for label, k, t in (("1주", "vs_week_pct", th["drop_vs_week_pct"]),
                            ("1개월", "vs_month_pct", th["drop_vs_month_pct"]),
                            ("평년", "vs_normal_pct", th["below_normal_pct"])):
            if r[k] not in ("", None) and float(r[k]) <= t:
                sig.append(f"{label} {float(r[k]):+.1f}%")
        parts.append(f"{r['kind_name']} {r['rank']} {float(r['price']):,.0f}원 ({', '.join(sig)})")
    return " / ".join(parts)


def price(v):
    return f"{int(v):,}원" if str(v).isdigit() else "–"


def link(c):
    return f"[{c['스토어']}]({c['링크']})"


def main():
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 필요하다.")
    with open(ROOT / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    scfg, th = cfg["stores"], cfg["thresholds"]

    now = datetime.now(KST)
    run_date, groups = load_alerts()
    if run_date is None:
        sys.exit("data/log.csv가 비어 있다. 모니터가 먼저 돌아야 한다.")
    routes = load_routes(scfg.get("require_parcel"))
    names, strict, aliases = load_regions()
    excluded, warn = load_contacts(os.environ.get("CONTACTS_CSV_URL"), scfg["recontact_days"], now.date())

    lines = [
        f"# 직공 컨택 후보 스토어 — {run_date} 도매가 알림 기준",
        "",
        f"만든 시각: {now:%Y-%m-%d %H:%M} (KST). 품목당 최대 {scfg['max_per_item']}곳, "
        "고양종합터미널 직행 노선이 있는 시·군만.",
        "산지는 상품명·스토어명에서 추정한 값이라 연락 전에 상품 페이지의 출하지를 확인할 것.",
    ]
    if warn:
        lines += ["", f"> 주의: {warn}"]
    else:
        lines.append(f"컨택목록 반영: {len(excluded)}곳 제외.")
    lines.append("")

    audit, used = [], set()  # used: 앞 품목에서 이미 올린 스토어 (스토어는 리스트 전체에서 한 번만)
    if not groups:
        lines.append("오늘 알림 품목 없음.")
    for name, rows in groups.items():
        cands, n_raw, fallback = candidates_for(name, scfg, cid, secret, names, strict, aliases)
        picked, unknown, n_noroute, n_contacted = [], [], 0, 0
        for c in cands:
            key = norm(c["스토어"])
            if key in excluded:
                verdict, n_contacted = "컨택 완료 제외", n_contacted + 1
            elif key in used:
                verdict = "다른 품목에 이미 올림"
            elif not c["산지"]:
                verdict = "산지 확인 필요"
                unknown.append(c)
            elif c["산지"] not in routes or (c["산지"] == "광주" and "경기" in c["상품명"]):
                verdict, n_noroute = "노선 없는 지역", n_noroute + 1
            elif len(picked) < scfg["max_per_item"]:
                verdict = "선정"
                picked.append(c)
                used.add(key)
            else:
                verdict = "조건 충족, 3곳 초과"
            audit.append(c | {"판정": verdict})

        lines += [f"## {name}", "", f"알림: {signal_text(rows, th)}", ""]
        if fallback:
            lines += ["> 주의: 스마트스토어 링크를 구분하지 못해 전체 쇼핑몰에서 골랐음.", ""]
        if picked:
            lines += ["| 순서 | 스토어 | 산지 | 출발 터미널 | 소화물 | 최저가 | 상품명 |", "|---|---|---|---|---|---:|---|"]
            for i, c in enumerate(picked, 1):
                rt = routes[c["산지"]]
                lines.append(
                    f"| {i} | {link(c)}{' (생산자)' if c['생산자'] else ''} | {c['시도']} {c['산지']} | "
                    f"{', '.join(r['출발 터미널'] for r in rt)} | {rt[0]['소화물']} | {price(c['최저가'])} | {c['상품명']} |"
                )
        elif not cands:
            lines.append("**없음** — 조건에 맞는 스토어 검색 결과가 없음." if n_raw else "**없음** — 검색 결과가 없음.")
        elif n_contacted and not n_noroute and not unknown:
            lines.append(f"**없음** — 조건에 맞는 스토어 {n_contacted}곳이 모두 컨택 완료.")
        else:
            lines.append(f"**없음** — 직행 노선이 있는 지역의 스토어가 없음 "
                         f"(노선 없는 지역 {n_noroute}곳, 컨택 완료 {n_contacted}곳).")
        if unknown:
            lines += ["", f"산지 확인 필요 {len(unknown)}곳 중 상위: " + ", ".join(link(c) for c in unknown[:3])]
        lines.append("")

    out_dir = DATA / "stores"
    out_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    (ROOT / "STORES.md").write_text(text, encoding="utf-8")
    (out_dir / f"{run_date}.md").write_text(text, encoding="utf-8")
    fields = ["품목", "판정", "스토어", "산지", "시도", "생산자", "최저가", "상품명", "링크"]
    with open(out_dir / f"{run_date}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(audit)
    print(f"{run_date}: 품목 {len(groups)}개, 선정 {sum(1 for a in audit if a['판정'] == '선정')}곳, 후보 {len(audit)}곳")


if __name__ == "__main__":
    main()
