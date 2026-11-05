"""직공 컨택 후보 업체 리스트 v1

정해 둔 품목(config.json의 stores.fixed_items, 현재 밤고구마)을 네이버 웹문서 검색
(NAVER API HUB)으로 찾아, 스마트스토어 업체를 품목당 최대 5곳 뽑는다.
고양종합터미널 직행 노선이 있는 시·군의 업체를 먼저 올리고,
노선 밖이거나 산지를 알 수 없는 업체는 참고용으로 따로 보여준다.
items_source를 "alerts"로 바꾸면 폭락 알림 품목을 대상으로 한다.

산출물
  STORES.md                    오늘의 리스트 (매일 교체)
  data/stores/YYYY-MM-DD.md    날짜별 보관본 (만든 날짜)
  data/stores/YYYY-MM-DD.csv   검토한 후보 전체와 판정 사유 (검증용)

입력
  data/routes.csv              고양종합터미널 직행 노선이 있는 시·군
  data/regions.json            산지 추정용 시·군 이름과 별칭
  data/log.csv                 (alerts 모드만) 모니터 누적 로그

환경변수
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET   NAVER API HUB Application의 Client ID / Secret
  CONTACTS_CSV_URL (선택)                 컨택목록 구글 시트의 'CSV로 웹에 게시' 주소
"""

import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))
NAVER_API = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
STORE_HOSTS = ("smartstore.naver.com", "m.smartstore.naver.com", "brand.naver.com", "m.brand.naver.com")
NOT_STORE = {"main", "search", "category", "products", "i", "inflow", "shoppingstory"}


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


def load_routes(require_parcel, require_confirmed=False):
    routes = {}
    with open(DATA / "routes.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if require_confirmed and r.get("도착방향 확인", "").strip().upper() != "O":
                continue
            if require_parcel and r["소화물"].strip().upper() != "O":
                continue
            routes.setdefault(r["시군"].strip(), []).append(r)
    return routes


def load_regions():
    """(시군→시도, strict 집합, 별칭→시군)."""
    with open(DATA / "regions.json", encoding="utf-8") as f:
        j = json.load(f)
    names = {}
    for sido, lst in j["regions"].items():
        for n in lst:
            names.setdefault(n, sido)
    return names, set(j.get("strict", [])), j.get("aliases", {})


def find_region(text, names, strict, aliases):
    """글에서 가장 먼저 나오는 시·군 이름. 없으면 None."""
    best = None
    taken = [False] * len(text)
    for key in sorted(list(names) + list(aliases), key=len, reverse=True):  # 남양주가 양주보다 먼저
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


# ---------- 네이버 웹문서 검색 ----------

def naver_search(query, cid, secret, display):
    r = requests.get(
        NAVER_API,
        params={"query": query, "display": display},
        headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret},
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"네이버 검색 오류 {r.status_code}: {r.text[:300]}")
    time.sleep(0.1)
    return r.json().get("items", [])


def clean(s):
    s = re.sub(r"<.*?>", "", s or "")
    return s.replace("&amp;", "&").replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").strip()


def store_of(link, title):
    """스마트스토어 주소면 (스토어 아이디, 스토어명, 스토어 홈 주소). 아니면 None."""
    u = urlparse(link)
    if u.netloc.lower() not in STORE_HOSTS:
        return None
    parts = [p for p in u.path.split("/") if p]
    if not parts or parts[0].lower() in NOT_STORE:
        return None
    slug = parts[0]
    # 스마트스토어 페이지 제목은 보통 '상품명 : 스토어명' 또는 '스토어명'
    name = title.rsplit(" : ", 1)[-1].strip() if " : " in title else (title if len(parts) == 1 else "")
    host = "brand.naver.com" if "brand" in u.netloc else "smartstore.naver.com"
    return slug.lower(), (name or slug)[:30], f"https://{host}/{slug}"


def search_stores(queries, match, scfg, cid, secret, regions):
    """검색어들을 돌려 스마트스토어 후보를 모은다. 스토어당 1행. (후보, 전체 결과 수) 반환."""
    names, strict, aliases = regions
    raw = []
    for q in queries:
        raw += naver_search(q, cid, secret, scfg["display"])
    seen, out = set(), []
    for rank, it in enumerate(raw):
        title, desc = clean(it.get("title")), clean(it.get("description"))
        st = store_of(it.get("link", ""), title)
        if not st or st[0] in seen:
            continue
        seen.add(st[0])
        text = title + " " + desc
        region = find_region(text, names, strict, aliases)
        if region == "광주" and "경기" in text:
            region = None
        out.append({
            "스토어": st[1], "아이디": st[0], "스토어 주소": st[2], "페이지 제목": title, "소개": desc[:80],
            "링크": it.get("link", ""), "산지": region or "", "시도": names.get(region, "") if region else "",
            "품목 일치": any(w in text for w in match), "정확 일치": match[0] in text,
            "제외어": next((w for w in scfg["exclude_words"] if w in title + " " + st[1]), ""),
            "생산자": any(k in st[1] + " " + desc for k in scfg["producer_keywords"]), "순위": rank,
        })
    out.sort(key=lambda c: (0 if c["생산자"] else 1, 0 if c["정확 일치"] else 1, c["순위"]))
    return out, len(raw)


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


def link(c):
    return f"[{c['스토어']}]({c['스토어 주소']})"


def table(picked, routes):
    lines = ["| 순서 | 업체 | 산지(추정) | 출발 터미널 | 소화물 | 소개 |", "|---|---|---|---|---|---|"]
    for i, c in enumerate(picked, 1):
        rt = routes.get(c["산지"])
        lines.append(
            f"| {i} | {link(c)}{' (생산자)' if c['생산자'] else ''} | {(c['시도'] + ' ' + c['산지']).strip() or '미상'} | "
            f"{', '.join(r['출발 터미널'] for r in rt) if rt else ('노선 없음' if c['산지'] else '확인 필요')} | {rt[0]['소화물'] if rt else '–'} | "
            f"{c['소개'].replace('|', ' ')} |"
        )
    return lines


def main():
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 필요하다.")
    with open(ROOT / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    scfg, th = cfg["stores"], cfg["thresholds"]
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    # 대상 품목: 고정 품목 또는 폭락 알림 품목. {품목명: (일치 단어들, 알림 행들)}
    if scfg.get("items_source") == "alerts":
        run_date, groups = load_alerts()
        if run_date is None:
            sys.exit("data/log.csv가 비어 있다. 모니터가 먼저 돌아야 한다.")
        items = {name: ([name], rows) for name, rows in groups.items()}
        basis = f"{run_date} 도매가 알림 품목"
    else:
        items = {it["name"]: ([it["name"]] + it.get("match", []), None) for it in scfg["fixed_items"]}
        basis = "고정 품목 " + ", ".join(items)

    routes = load_routes(scfg.get("require_parcel"), scfg.get("require_confirmed"))
    regions = load_regions()
    excluded, warn = load_contacts(os.environ.get("CONTACTS_CSV_URL"), scfg["recontact_days"], now.date())
    n_max = scfg["max_per_item"]

    lines = [
        f"# 직공 컨택 후보 업체 — {today}",
        "",
        f"대상: {basis}. 만든 시각: {now:%Y-%m-%d %H:%M} (KST). 품목당 최대 {n_max}곳.",
        f"고양종합터미널 직행 노선 지역: {', '.join(routes) or '없음'}.",
        "산지는 검색 결과 글에서 추정한 값이라 연락 전에 상품 페이지의 출하지를 확인할 것.",
    ]
    lines += ["", f"> 주의: {warn}"] if warn else [f"컨택목록 반영: {len(excluded)}곳 제외."]
    lines.append("")

    audit, used, n_raw_all, n_store_all = [], set(), 0, 0
    if not items:
        lines.append("대상 품목 없음.")
    for name, (match, rows) in items.items():
        queries = [q.replace("{품목}", name).replace("{지역}", r) for r in routes for q in scfg["region_queries"]]
        queries += [q.replace("{품목}", name) for q in scfg["general_queries"]]
        cands, n_raw = search_stores(queries, match, scfg, cid, secret, regions)
        n_raw_all += n_raw
        n_store_all += len(cands)
        picked, others, n_contacted = [], [], 0
        for c in cands:
            if norm(c["스토어"]) in excluded or c["아이디"] in excluded:
                verdict, n_contacted = "컨택 완료 제외", n_contacted + 1
            elif c["아이디"] in used:
                verdict = "이미 올림"
            elif c["제외어"]:
                verdict = f"제외어({c['제외어']})"
            elif not c["품목 일치"]:
                verdict = "품목 불일치"
            elif c["산지"] in routes:
                if len(picked) < n_max:
                    verdict = "선정"
                    picked.append(c)
                    used.add(c["아이디"])
                else:
                    verdict = f"조건 충족, {n_max}곳 초과"
            else:
                verdict = "노선 없는 지역" if c["산지"] else "산지 미상"
                others.append(c)
            audit.append({"품목": name} | c | {"판정": verdict})

        lines += [f"## {name}", ""]
        if rows:
            lines += [f"알림: {signal_text(rows, th)}", ""]
        lines += [f"### 노선 조건 충족 ({len(picked)}곳)", ""]
        if picked:
            lines += table(picked, routes)
        elif not cands:
            lines.append("**없음** — 스마트스토어 업체가 검색되지 않음.")
        elif n_contacted and not others:
            lines.append(f"**없음** — 조건에 맞는 업체 {n_contacted}곳이 모두 컨택 완료.")
        else:
            lines.append("**없음** — 직행 노선이 있는 지역의 업체가 없음.")
        rest = others[: max(0, n_max - len(picked))]
        if rest:
            lines += ["", f"### 참고: 노선 밖 또는 산지 미상 ({len(rest)}곳, 전체 {len(others)}곳 중)", ""]
            lines += table(rest, routes)
        lines.append("")

    if items and n_raw_all and not n_store_all:
        lines += ["> 주의: 웹문서 검색 결과에 스마트스토어 주소가 한 건도 없음. 검색어(config.json)를 바꿔 볼 것.", ""]

    out_dir = DATA / "stores"
    out_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    (ROOT / "STORES.md").write_text(text, encoding="utf-8")
    (out_dir / f"{today}.md").write_text(text, encoding="utf-8")
    fields = ["품목", "판정", "스토어", "산지", "시도", "생산자", "품목 일치", "정확 일치", "페이지 제목", "소개", "스토어 주소", "링크"]
    with open(out_dir / f"{today}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(audit)
    print(f"{today}: 품목 {len(items)}개, 검색 결과 {n_raw_all}건, 스마트스토어 {n_store_all}곳, "
          f"선정 {sum(1 for a in audit if a['판정'] == '선정')}곳")


if __name__ == "__main__":
    main()
