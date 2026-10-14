#!/usr/bin/env python3
"""Terradron – monitor dronových tendrov (inšpekcie, vnútorné inšpekcie, kanalizácia, mapovanie).

Beží denne na GitHub Actions. Zbiera nové oznámenia z verejných API, filtruje ich
kľúčovými slovami/CPV a výsledok zapíše do data/latest.json a data/digest/<dátum>.json.
Hodnotenie relevancie pre ELIOS 3 / Stereo 3 robí následne Claude.

Zdroje:
  TED (EÚ + EHP, nad limit)           – bez kľúča
  oeffentlichevergabe.de (DE národné) – bez kľúča
  BZP / e-Zamówienia (PL národné)     – bez kľúča
  simap.ch (CH)                       – bez kľúča
  UVOstat (SK národné)                – voliteľne, secret UVOSTAT_API_TOKEN
  Doffin (NO národné)                 – voliteľne, secret DOFFIN_API_KEY

Iba štandardná knižnica Pythonu 3.10+.
"""
from __future__ import annotations

import datetime as dt
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
UA = "TerradronTenderMonitor/1.0 (+https://terradron.sk)"
DEBUG = "--debug" in sys.argv
TODAY = dt.date.today()
SINCE = TODAY - dt.timedelta(days=int(CFG.get("lookback_days", 4)))

ISO3_TO_2 = {"DEU": "DE", "AUT": "AT", "CHE": "CH", "CZE": "CZ", "POL": "PL", "DNK": "DK",
             "NOR": "NO", "SWE": "SE", "FIN": "FI", "SVK": "SK", "SVN": "SI"}

import socket
_orig_gai = socket.getaddrinfo
def _gai_v4(host, *a, **k):  # GitHub runnery nemajú IPv6 → simap.ch padal na Errno 101
    res = _orig_gai(host, *a, **k)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res
socket.getaddrinfo = _gai_v4

SAMPLES = {}  # vzorky názvov na diagnostiku
READ = {}  # počet prečítaných záznamov na zdroj (diagnostika)

_cookies = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookies))


def log(*a):
    print(*a, flush=True)


def http(url, *, method="GET", body=None, headers=None, timeout=90, retries=2, binary=False):
    """Vráti (status, text|bytes). Opakuje pri 429/5xx/sieťovej chybe."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(5 * 3 ** (attempt - 1))
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with _opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (raw if binary else raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            last = (e.code, raw if binary else raw.decode("utf-8", "replace"))
            if e.code in (429, 500, 502, 503, 504, 403) and attempt < retries:
                continue
            return last
        except Exception as e:  # sieť, timeout
            last = (0, str(e))
    return last


# ---------------------------------------------------------------- filtrovanie
def _rx(key):
    return [re.compile(p, re.I | re.U) for p in CFG[key]]


RX = {"dron": _rx("drone_patterns"), "vnútorná inšpekcia/kanalizácia": _rx("indoor_patterns"),
      "mapovanie": _rx("mapping_patterns")}
STRONG = CFG["strong_cpv"]


def classify(text: str, cpvs: list[str]) -> list[str]:
    reasons = []
    for label, pats in RX.items():
        for p in pats:
            m = p.search(text or "")
            if m:
                reasons.append(f"{label}: „{m.group(0)}“")
                break
    for c in cpvs:
        c8 = c[:8]
        if c8 in STRONG:
            reasons.append(f"CPV {c8} – {STRONG[c8]}")
    return reasons


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def pick_lang(v, order=("slk", "ces", "eng", "deu", "pol", "sk", "cs", "en", "de")):
    """TED/simap vracia viacjazyčné polia ako dict alebo list."""
    if isinstance(v, dict):
        for k in order:
            if v.get(k):
                return pick_lang(v[k], order)
        for x in v.values():
            if x:
                return pick_lang(x, order)
        return ""
    if isinstance(v, list):
        return "; ".join(dict.fromkeys(clean(pick_lang(x, order)) for x in v if x))
    return clean(v)


def lang_all(v):
    """Všetky jazykové verzie spojené (na filtrovanie)."""
    if isinstance(v, dict):
        return " ".join(lang_all(x) for x in v.values())
    if isinstance(v, list):
        return " ".join(lang_all(x) for x in v)
    return clean(v)


def mk(source, country, id_, title, buyer, published, deadline, cpvs, link, reasons, extra=None):
    d = {"id": id_, "source": source, "country": country, "title": clean(title)[:400],
         "buyer": clean(buyer)[:200], "published": (published or "")[:10],
         "deadline": (deadline or "")[:10], "cpv": sorted(set(c[:8] for c in cpvs if c))[:8],
         "link": link, "match": reasons}
    if extra:
        d.update(extra)
    return d


# ---------------------------------------------------------------- TED
def src_ted():
    countries = " ".join(CFG["countries_iso3"])
    terms = CFG["ted_terms"]
    chunks = [terms[i:i + 14] for i in range(0, len(terms), 14)]
    clauses = [" OR ".join(f'FT~("{t}")' for t in ch) for ch in chunks]
    clauses.append(f"classification-cpv IN ({' '.join(CFG['ted_cpv'])})")
    fields = ["publication-number", "notice-title", "buyer-name", "buyer-country",
              "publication-date", "deadline-receipt-tender-date-lot",
              "deadline-receipt-request-date-lot", "classification-cpv", "notice-type",
              "description-lot", "title-lot"]
    out = {}
    for cl in clauses:
        q = (f"({cl}) AND buyer-country IN ({countries}) "
             f"AND publication-date>={SINCE:%Y%m%d} SORT BY publication-number DESC")
        token = None
        for _page in range(10):
            body = {"query": q, "fields": fields, "limit": 100, "scope": "ALL",
                    "paginationMode": "ITERATION", "checkQuerySyntax": False,
                    "onlyLatestVersions": False}
            if token:
                body["iterationNextToken"] = token
            st, txt = http("https://api.ted.europa.eu/v3/notices/search", method="POST", body=body)
            if st != 200:
                raise RuntimeError(f"TED HTTP {st}: {str(txt)[:300]} | query: {q[:200]}")
            js = json.loads(txt)
            notices = js.get("notices") or []
            for n in notices:
                pub = n.get("publication-number")
                if not pub:
                    continue
                cpvs = [str(c) for c in (n.get("classification-cpv") or [])]
                text = " ".join(lang_all(n.get(k)) for k in ("notice-title", "title-lot", "description-lot"))
                reasons = classify(text, cpvs) or ["TED fulltext (výraz v texte oznámenia)"]
                dl = n.get("deadline-receipt-tender-date-lot") or n.get("deadline-receipt-request-date-lot") or []
                dl = sorted(str(x)[:10] for x in (dl if isinstance(dl, list) else [dl]))
                cc = pick_lang(n.get("buyer-country"))
                out[f"TED-{pub}"] = mk(
                    "TED", ISO3_TO_2.get(cc.split(";")[0].strip(), cc), f"TED-{pub}",
                    pick_lang(n.get("notice-title"), ("eng", "deu", "slk", "ces", "pol")),
                    pick_lang(n.get("buyer-name")), str(n.get("publication-date", ""))[:10],
                    dl[-1] if dl else "", cpvs, f"https://ted.europa.eu/en/notice/-/detail/{pub}",
                    reasons, {"notice_type": pick_lang(n.get("notice-type"))})
            token = js.get("iterationNextToken")
            if not token or not notices:
                break
            time.sleep(0.5)
        time.sleep(0.5)
    return list(out.values())


# ---------------------------------------------------------------- Nemecko (národné)
def _x(xml, pat):
    m = re.search(pat, xml, re.S)
    if not m:
        return ""
    import html
    return clean(html.unescape(m.group(1)))


def src_de():
    out = {}
    day = TODAY - dt.timedelta(days=1)
    while day >= SINCE:
        st, raw = http(f"https://oeffentlichevergabe.de/api/notice-exports?pubDay={day}&format=eforms.zip",
                       headers={"Accept": "*/*"}, binary=True, timeout=180)
        if st == 200:
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile:
                log(f"  DE {day}: poškodený zip")
                day -= dt.timedelta(days=1)
                continue
            for name in zf.namelist():
                if not name.endswith(".xml"):
                    continue
                xml = zf.read(name).decode("utf-8", "replace")
                READ["DE"] = READ.get("DE", 0) + 1
                t = re.search(r'<cbc:NoticeTypeCode listName="([^"]+)"', xml)
                if not t or t.group(1) not in ("competition", "planning"):
                    continue
                domains = re.findall(r"<cbc:RegulatoryDomain>([^<]+)<", xml)
                if domains and any(re.match(r"^3\d{4}[LR]\d{4}$", d) for d in domains):
                    continue  # EÚ postup – zachytí TED
                SAMPLES.setdefault("DE", []).append(_x(xml, r"<cac:ProcurementProject>.*?<cbc:Name[^>]*>([^<]+)<")[:120]) if len(SAMPLES.get("DE", [])) < 60 else None
                proj = _x(xml, r"<cac:ProcurementProject>(.*?)</cac:ProcurementProject>")
                title = _x(xml, r"<cac:ProcurementProject>.*?<cbc:Name[^>]*>([^<]+)<")
                desc = _x(xml, r"<cac:ProcurementProject>.*?<cbc:Description[^>]*>([^<]+)<")
                cpvs = re.findall(r'<cbc:ItemClassificationCode listName="cpv">(\d{8})<', xml)
                reasons = classify(f"{title} {desc}", cpvs)
                if not reasons:
                    continue
                folder = _x(xml, r"<cbc:ContractFolderID>([^<]+)<")
                buyer = _x(xml, r"<efac:Company>.*?<cac:PartyName>\s*<cbc:Name[^>]*>([^<]+)<")
                city = _x(xml, r"<cbc:CityName>([^<]+)<")
                deadline = _x(xml, r"<cac:TenderSubmissionDeadlinePeriod>.*?<cbc:EndDate>(\d{4}-\d{2}-\d{2})")
                docs = _x(xml, r"<cac:CallForTendersDocumentReference>.*?<cbc:URI>([^<]+)<")
                link = docs if docs.startswith("http") else "https://oeffentlichevergabe.de/ui/de/search"
                out[f"DE-{folder}"] = mk("oeffentlichevergabe.de", "DE", f"DE-{folder}", title,
                                         f"{buyer} ({city})" if city else buyer,
                                         _x(xml, r"<cbc:IssueDate>(\d{4}-\d{2}-\d{2})"), deadline,
                                         cpvs, link, reasons, {"description": desc[:600]})
        elif st in (204, 400, 404):
            log(f"  DE {day}: export nie je k dispozícii (HTTP {st})")
        else:
            raise RuntimeError(f"oeffentlichevergabe.de HTTP {st} pre {day}")
        day -= dt.timedelta(days=1)
        time.sleep(2)
    return list(out.values())


# ---------------------------------------------------------------- Poľsko (BZP)
def src_pl():
    out = {}
    api = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"
    page = 250
    day = TODAY
    while day >= SINCE:
        cursor, to = f"{day}T00:00:00", f"{day}T23:59:59"
        for _ in range(20):
            url = (f"{api}?NoticeType=ContractNotice&PublicationDateFrom={cursor}"
                   f"&PublicationDateTo={to}&PageSize={page}")
            st, txt = http(url, timeout=120, retries=3)
            if st != 200:
                raise RuntimeError(f"BZP HTTP {st} ({day}): {str(txt)[:200]}")
            rows = json.loads(txt or "[]")
            if not isinstance(rows, list):
                raise RuntimeError(f"BZP neočakávaná odpoveď: {str(txt)[:200]}")
            READ["PL"] = READ.get("PL", 0) + len(rows)
            if rows and "PL" not in SAMPLES:
                SAMPLES["PL"] = {"keys": sorted(rows[0].keys()), "titles": [clean(r.get("orderObject"))[:120] for r in rows[:40]]}
            for n in rows:
                cpvs = re.findall(r"(\d{8})-\d", str(n.get("cpvCode") or ""))
                title = clean(n.get("orderObject"))
                reasons = classify(title, cpvs)
                if not reasons:
                    continue
                bzp = clean(n.get("bzpNumber")).replace(" ", "")
                oid = n.get("objectId")
                buyer = clean(n.get("organizationName"))
                if n.get("organizationCity"):
                    buyer += f" ({clean(n.get('organizationCity'))})"
                out[f"PL-{bzp}"] = mk("BZP", "PL", f"PL-{bzp}", title, buyer,
                                      str(n.get("publicationDate", ""))[:10],
                                      str(n.get("submittingOffersDate", ""))[:10], cpvs,
                                      f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{oid}"
                                      if oid else "https://ezamowienia.gov.pl/mo-client-board/bzp/list",
                                      reasons)
            if len(rows) < page:
                break
            nxt = str(rows[-1].get("publicationDate") or "")[:19]
            if not nxt or nxt <= cursor:
                nxt = (dt.datetime.fromisoformat(cursor) + dt.timedelta(seconds=1)).isoformat()[:19]
            cursor = nxt
            time.sleep(3)
        day -= dt.timedelta(days=1)
        time.sleep(3)
    return list(out.values())


# ---------------------------------------------------------------- Švajčiarsko (simap)
SIMAP_SEARCH = ["Drohne", "Drohnen", "drone", "UAV", "Kanalinspektion", "Kanal-TV", "Kanalfernsehen",
                "TV-Aufnahmen", "Befahrung", "Inspektion", "Zustandserfassung", "Photogrammetrie",
                "Laserscanning", "Druckleitung", "Stollen", "inspection"]


def src_ch():
    base = "https://www.simap.ch/api"
    http(f"{base}/cantons/v1?lang=de")  # nastaví session cookie
    out = {}
    queries = [{"search": s} for s in SIMAP_SEARCH] + [{"cpvCodes": c} for c in CFG["ted_cpv"]]
    for q in queries:
        params = {"lang": "de", "newestPublicationFrom": SINCE.isoformat(), **q}
        st, txt = http(f"{base}/publications/v2/project/project-search?" + urllib.parse.urlencode(params))
        if st != 200:
            raise RuntimeError(f"simap HTTP {st}: {str(txt)[:200]}")
        js = json.loads(txt)
        READ["CH"] = READ.get("CH", 0) + len(js.get("projects", []))
        for p in js.get("projects", []):
            title = pick_lang(p.get("title"), ("de", "fr", "it", "en"))
            cpvs = [str(c) for c in (p.get("cpvCodes") or [])] if isinstance(p.get("cpvCodes"), list) else []
            reasons = classify(title, cpvs)
            if not reasons and "cpvCodes" in q:
                reasons = [f"CPV {q['cpvCodes']} – {STRONG.get(q['cpvCodes'], '')}"]
            if not reasons:
                continue  # napr. „Inspektion“ bez súvisu s dronmi/vnútornou inšpekciou
            pid = p.get("id", "")
            out[f"CH-{pid}"] = mk("simap.ch", "CH", f"CH-{pid}", title,
                                  pick_lang(p.get("procOfficeName"), ("de", "fr", "it", "en")),
                                  str(p.get("publicationDate", ""))[:10], "", cpvs,
                                  f"https://www.simap.ch/de/project-detail/{pid}" if pid else "https://www.simap.ch",
                                  reasons, {"project_number": p.get("projectNumber")})
        time.sleep(1)
    return list(out.values())


# ---------------------------------------------------------------- Slovensko (UVOstat)
def src_sk():
    token = os.environ.get("UVOSTAT_API_TOKEN", "").strip()
    if not token:
        return None
    out, offset = {}, 0
    while True:
        params = {"datum_zverejnenia_od": SINCE.isoformat(), "datum_zverejnenia_do": TODAY.isoformat(),
                  "limit": 100, "offset": offset}
        st, txt = http("https://www.uvostat.sk/api/vyhlasene_obstaravania?" + urllib.parse.urlencode(params),
                       headers={"ApiToken": token})
        if st != 200:
            raise RuntimeError(f"UVOstat HTTP {st}: {str(txt)[:200]}")
        js = json.loads(txt)
        data = js.get("data") or []
        READ["SK"] = READ.get("SK", 0) + len(data)
        for n in data:
            cpvs = re.findall(r"(\d{8})", " ".join([str(n.get("hlavne_cpv") or "")] +
                                                   [str(x) for x in (n.get("dodatkove_cpv") or [])]))
            title = clean(n.get("nazov"))
            reasons = classify(f"{title} {clean(n.get('popis'))}", cpvs)
            if not reasons:
                continue
            oid = n.get("order_id") or n.get("id")
            out[f"SK-{oid}"] = mk("UVO (UVOstat)", "SK", f"SK-{oid}", title, n.get("obstaravatel_meno"),
                                  str(n.get("datum_zverejnenia", ""))[:10],
                                  str(n.get("lehota_na_predkladanie_ponuk") or n.get("datum_lehoty") or "")[:10],
                                  cpvs, n.get("order_url") or "https://www.uvo.gov.sk", reasons,
                                  {"phz_eur": n.get("phz")})
        offset += len(data)
        if len(data) < 100 or offset >= int((js.get("summary") or {}).get("total_records") or 0):
            break
        time.sleep(1)
    return list(out.values())


# ---------------------------------------------------------------- Nórsko (Doffin)
def src_no():
    key = os.environ.get("DOFFIN_API_KEY", "").strip()
    if not key:
        return None
    out = {}
    for page in range(1, 6):
        st, txt = http(f"https://api.doffin.no/public/v2/search?status=ACTIVE&numHitsPerPage=100&page={page}"
                       f"&sortBy=PUBLICATION_DATE_DESC", headers={"Ocp-Apim-Subscription-Key": key})
        if st != 200:
            raise RuntimeError(f"Doffin HTTP {st}: {str(txt)[:200]}")
        hits = json.loads(txt).get("hits") or []
        older = False
        for h in hits:
            pub = str(h.get("publicationDate") or h.get("issueDate") or "")[:10]
            if pub and pub < SINCE.isoformat():
                older = True
                continue
            cpvs = re.findall(r"(\d{8})", " ".join(str(c) for c in (h.get("cpvCodes") or [])))
            title = clean(h.get("heading"))
            reasons = classify(f"{title} {clean(h.get('description'))}", cpvs)
            if not reasons:
                continue
            out[f"NO-{h.get('id')}"] = mk("Doffin", "NO", f"NO-{h.get('id')}", title,
                                          ", ".join(b.get("name", "") for b in (h.get("buyer") or [])),
                                          pub, str(h.get("deadline") or "")[:10], cpvs,
                                          f"https://doffin.no/notices/{h.get('id')}", reasons)
        if older or not hits:
            break
        time.sleep(2.5)
    return list(out.values())


# ---------------------------------------------------------------- beh
SOURCES = [("TED", src_ted), ("DE oeffentlichevergabe.de", src_de), ("PL BZP", src_pl),
           ("CH simap", src_ch), ("SK UVOstat", src_sk), ("NO Doffin", src_no)]


def main():
    DATA.mkdir(exist_ok=True)
    (DATA / "digest").mkdir(exist_ok=True)
    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    first_run = not seen

    status, found = {}, []
    for name, fn in SOURCES:
        t0 = time.time()
        try:
            res = fn()
            if res is None:
                status[name] = {"status": "vypnuté (chýba API kľúč)"}
                log(f"{name}: preskočené – chýba kľúč")
                continue
            found += res
            status[name] = {"status": "ok", "matches": len(res), "read": READ.get(name.split()[0]),
                            "seconds": round(time.time() - t0)}
            log(f"{name}: {len(res)} zhôd")
        except Exception as e:
            status[name] = {"status": "chyba", "error": str(e)[:400]}
            log(f"{name}: CHYBA {e}")
            if DEBUG:
                traceback.print_exc()

    new = []
    for f in found:
        if f["id"] in seen:
            continue
        seen[f["id"]] = TODAY.isoformat()
        if f["deadline"] and f["deadline"] < TODAY.isoformat():
            continue
        new.append(f)
    # TED vracia viac verzií toho istého; zoradiť podľa krajiny a dátumu
    new.sort(key=lambda x: (x["country"], x["published"]), reverse=False)

    cutoff = (TODAY - dt.timedelta(days=180)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, indent=0, sort_keys=True))

    digest = {"run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "date": TODAY.isoformat(), "window_from": SINCE.isoformat(), "first_run": first_run,
              "sources": status, "new_count": len(new), "new": new}
    txt = json.dumps(digest, ensure_ascii=False, indent=1)
    (DATA / "latest.json").write_text(txt, encoding="utf-8")
    (DATA / "digest" / f"{TODAY}.json").write_text(txt, encoding="utf-8")
    (DATA / "debug_samples.json").write_text(json.dumps(SAMPLES, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Hotovo: {len(new)} nových, spolu sledovaných {len(seen)}")
    if all(s.get("status") == "chyba" for s in status.values() if s.get("status") != "vypnuté (chýba API kľúč)"):
        sys.exit(1)


if __name__ == "__main__":
    main()
