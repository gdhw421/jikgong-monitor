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
from urllib.parse import quote, urlparse

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))
NAVER_API = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
NAVER_LOCAL_API = "https://naverapihub.apigw.ntruss.com/search/v1/local"
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
            "구분": "스마트스토어", "스토어": st[1], "아이디": st[0], "스토어 주소": st[2], "페이지 제목": title, "소개": desc[:80],
            "링크": it.get("link", ""), "산지": region or "", "시도": names.get(region, "") if region else "",
            "품목 일치": any(w in text for w in match), "정확 일치": match[0] in text,
            "제외어": next((w for w in scfg["exclude_words"] if w in title + " " + st[1]), ""),
            "생산자": any(k in st[1] + " " + desc for k in scfg["producer_keywords"]), "순위": rank,
        })
    out.sort(key=lambda c: (0 if c["생산자"] else 1, 0 if c["정확 일치"] else 1, c["순위"]))
    return out, len(raw)


def naver_local(query, cid, secret, sort):
    """네이버 지역 검색(지도 등록 업체). 한 번에 최대 5건."""
    r = requests.get(
        NAVER_LOCAL_API,
        params={"query": query, "display": 5, "sort": sort},
        headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret},
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"네이버 지역 검색 오류 {r.status_code}: {r.text[:300]} "
                 "(HUB Application에 '지역' API가 선택돼 있는지 확인)")
    time.sleep(0.1)
    return r.json().get("items", [])


def search_local(name, match, region, sido, scfg, cid, secret):
    """한 품목·한 지역의 지도 등록 농가 후보. 업체당 1행. (후보, 전체 결과 수) 반환."""
    raw = []
    for q in scfg["local_queries"]:
        for sort in scfg["local_sorts"]:
            raw += naver_local(q.replace("{품목}", name).replace("{지역}", region), cid, secret, sort)
    seen, out = set(), []
    for rank, it in enumerate(raw):
        title, cat = clean(it.get("title")), clean(it.get("category"))
        addr = clean(it.get("roadAddress")) or clean(it.get("address"))
        key = norm(title) + "|" + norm(addr)
        if not title or key in seen:
            continue
        seen.add(key)
        text = title + " " + cat + " " + clean(it.get("description"))
        home = (it.get("link") or "").strip()
        out.append({
            "구분": "지도 등록", "스토어": title[:30], "아이디": "local:" + key,
            "스토어 주소": "https://map.naver.com/p/search/" + quote(f"{region} {title}"),
            "페이지 제목": title, "링크": home,
            "소개": " · ".join(x for x in (addr, cat) if x),
            "산지": region if region in addr else "", "시도": sido if region in addr else "",
            "품목 일치": any(w in text for w in match), "정확 일치": match[0] in title,
            "제외어": next((w for w in scfg["local_exclude_category"] if w in cat), "")
                      or next((w for w in scfg["exclude_words"] if w in title), ""),
            "생산자": any(k in title + " " + cat for k in scfg["local_farm_words"]), "순위": rank,
        })
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


def online(c):
    """온라인 판매·안내 채널 주소. 스마트스토어는 스토어 주소, 지도 등록 업체는 등록된 홈페이지."""
    return c["스토어 주소"] if c["구분"] == "스마트스토어" else c["링크"]


def table(picked, routes):
    lines = ["| 순서 | 업체 | 구분 | 산지(추정) | 출발 터미널 | 소화물 | 온라인 채널 | 소개 |", "|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(picked, 1):
        rt = routes.get(c["산지"])
        lines.append(
            f"| {i} | {link(c)}{' (생산자)' if c['생산자'] else ''} | {c['구분']} | {(c['시도'] + ' ' + c['산지']).strip() or '미상'} | "
            f"{', '.join(r['출발 터미널'] for r in rt) if rt else '확인 필요'} | {rt[0]['소화물'] if rt else '–'} | {'[열기](' + online(c) + ')' if online(c) else '–'} | "
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
        items = {name: ([name], rows, None) for name, rows in groups.items()}
        basis = f"{run_date} 도매가 알림 품목"
    else:
        items = {it["name"]: ([it["name"]] + it.get("match", []), None, it.get("regions")) for it in scfg["fixed_items"]}
        basis = "고정 품목 " + ", ".join(
            (" ".join(it.get("regions", [])) + " " + it["name"]).strip() for it in scfg["fixed_items"])

    routes = load_routes(scfg.get("require_parcel"), scfg.get("require_confirmed"))
    regions = load_regions()
    excluded, warn = load_contacts(os.environ.get("CONTACTS_CSV_URL"), scfg["recontact_days"], now.date())
    n_max = scfg["max_per_item"]

    lines = [
        f"# 직공 컨택 후보 업체 — {today}",
        "",
        f"대상: {basis}. 만든 시각: {now:%Y-%m-%d %H:%M} (KST). 품목당 최대 {n_max}곳.",
        f"고양종합터미널 직행 노선 지역: {', '.join(routes) or '없음'}.",
        "스마트스토어의 산지는 소개글에서 추정한 값이고, 지도 등록 업체는 주소 기준이다. 품목을 실제로 파는지는 연락 전에 확인할 것.",
        "지도 등록 업체의 이름을 누르면 네이버 지도 검색으로 가고, 전화번호는 거기서 확인한다.",
    ]
    lines += ["", f"> 주의: {warn}"] if warn else [f"컨택목록 반영: {len(excluded)}곳 제외."]
    lines.append("")

    audit, used, n_raw_all, n_store_all = [], set(), 0, 0
    if not items:
        lines.append("대상 품목 없음.")
    for name, (match, rows, only) in items.items():
        # 품목에 regions가 정해져 있으면 그 지역만 검색하고, 지역 없는 검색은 하지 않는다
        search_regions = [r for r in routes if r in only] if only else list(routes)
        queries = [q.replace("{품목}", name).replace("{지역}", r) for r in search_regions for q in scfg["region_queries"]]
        if not only:
            queries += [q.replace("{품목}", name) for q in scfg["general_queries"]]
        cands, n_raw = search_stores(queries, match, scfg, cid, secret, regions)
        n_raw_all += n_raw
        n_store_all += len(cands)
        if scfg.get("local_search"):
            for r in search_regions:
                lc, n = search_local(name, match, r, routes[r][0]["시도"], scfg, cid, secret)
                cands += lc
                n_raw_all += n
        eligible, others, n_contacted = [], [], 0
        for c in cands:
            local = c["구분"] == "지도 등록"
            if norm(c["스토어"]) in excluded or c["아이디"] in excluded:
                c["판정"], n_contacted = "컨택 완료 제외", n_contacted + 1
            elif c["아이디"] in used:
                c["판정"] = "이미 올림"
            elif c["제외어"]:
                c["판정"] = f"제외어({c['제외어']})"
            elif local and not c["생산자"]:
                c["판정"] = "농가 아님(분류)"
            elif local and c["산지"] not in search_regions:
                c["판정"] = "대상 밖 지역"
            elif local and not c["품목 일치"]:
                c["판정"] = "품목 확인 필요"  # 대상 지역 농가지만 이름·분류에 품목이 없음
                others.append(c)
            elif not c["품목 일치"]:
                c["판정"] = "품목 불일치"
            elif c["산지"] in search_regions:
                eligible.append(c)
            else:
                c["판정"] = "대상 밖 지역" if c["산지"] else "산지 미상"
                others.append(c)
        # 생산자 → 온라인 채널 있는 곳 → 스마트스토어 → 검색 순위 순으로 n_max곳
        order = lambda c: (0 if c["생산자"] else 1, 0 if online(c) else 1,
                           0 if c["구분"] == "스마트스토어" else 1, c["순위"])
        eligible.sort(key=order)
        others.sort(key=lambda c: (0 if c["판정"] == "품목 확인 필요" else 1,) + order(c))
        picked = eligible[:n_max]
        for c in eligible:
            c["판정"] = "선정" if c in picked else f"조건 충족, {n_max}곳 초과"
        used.update(c["아이디"] for c in picked)
        audit += [{"품목": name} | c for c in cands]

        lines += [f"## {(' '.join(only) + ' ') if only else ''}{name}", ""]
        if rows:
            lines += [f"알림: {signal_text(rows, th)}", ""]
        lines += [f"### 노선 조건 충족 ({len(picked)}곳)", ""]
        if picked:
            lines += table(picked, routes)
        elif not cands:
            lines.append("**없음** — 업체가 검색되지 않음.")
        elif n_contacted and not others:
            lines.append(f"**없음** — 조건에 맞는 업체 {n_contacted}곳이 모두 컨택 완료.")
        else:
            lines.append("**없음** — 대상 지역의 업체가 없음.")
        rest = others[:n_max]
        if rest:
            lines += ["", f"### 참고: 품목 확인 필요 또는 대상 밖 ({len(rest)}곳, 전체 {len(others)}곳 중)", "",
                      "대상 지역 농가지만 이름·분류에 품목이 없거나, 산지가 대상 밖이거나 알 수 없는 곳.", ""]
            lines += table(rest, routes)
        lines.append("")

    if items and n_raw_all and not n_store_all:
        lines += ["> 주의: 웹문서 검색 결과에 스마트스토어 주소가 한 건도 없음. 검색어(config.json)를 바꿔 볼 것.", ""]

    out_dir = DATA / "stores"
    out_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    (ROOT / "STORES.md").write_text(text, encoding="utf-8")
    (out_dir / f"{today}.md").write_text(text, encoding="utf-8")
    fields = ["품목", "판정", "구분", "스토어", "산지", "시도", "생산자", "품목 일치", "정확 일치", "페이지 제목", "소개", "스토어 주소", "링크"]
    with open(out_dir / f"{today}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(audit)
    print(f"{today}: 품목 {len(items)}개, 검색 결과 {n_raw_all}건, 스마트스토어 {n_store_all}곳, 전체 후보 {len(audit)}곳, "
          f"선정 {sum(1 for a in audit if a['판정'] == '선정')}곳")


if __name__ == "__main__":
    main()
