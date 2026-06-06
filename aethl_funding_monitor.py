#!/usr/bin/env python3
"""
Aethl funding-opportunity monitor.

Pulls live opportunities from verified sources, scores them for relevance to
Aethl's tech, dedupes, and (optionally) pushes new high-relevance items to a
monday.com board.

SOURCES (verified live):
  - Grants.gov Search2     : OPEN, no key            -> US federal grants/BAAs
  - EU Funding & Tenders   : OPEN, public apiKey=SEDIA -> Horizon Europe / EIC calls
  - TED v3                 : OPEN, no key            -> EU public procurement (CPV)
  - SAM.gov Opportunities  : needs SAM_API_KEY env   -> US federal contracts/solicitations
  - monday.com GraphQL     : needs MONDAY_TOKEN env  -> destination board

CREDENTIALS come from environment variables (never hardcode):
  export SAM_API_KEY=...        # optional; SAM.gov source skipped if absent
  export MONDAY_TOKEN=...        # optional; dry-run (CSV/JSON) if absent
  export MONDAY_BOARD_ID=...     # required to push to monday.com

USAGE:
  python3 aethl_funding_monitor.py            # dry-run: writes opportunities.csv / .json
  MONDAY_TOKEN=xxx MONDAY_BOARD_ID=123 python3 aethl_funding_monitor.py --push
"""

import os
import sys
import re
import csv
import json
import time
import argparse
import datetime as dt
from urllib import request, parse, error

# ---------------------------------------------------------------------------
# Aethl relevance config  (edit these to tune what gets surfaced)
# ---------------------------------------------------------------------------

# Keyword tiers drive the relevance score (1-5). Strong terms = core thesis.
STRONG_TERMS = [
    "wound", "diabetic foot", "foot ulcer", "pressure ulcer", "chronic wound",
    "burn", "tissue oxygen", "hypoxia", "hyperbaric", "hydrogel", "wound dressing",
    "oxygen therapy", "regenerative", "trauma wound", "combat casualty",
]
MEDIUM_TERMS = [
    "biomaterial", "drug delivery", "nitric oxide", "dermatology", "skin repair",
    "antimicrobial", "medical device", "tissue repair", "epithelial", "granulation",
    "prolonged field care", "austere", "veterinary wound",
]

# Search keywords sent to keyword-based APIs (Grants.gov, SEDIA, SAM.gov).
# Broadened to cover every Aethl application + pipeline for higher volume.
SEARCH_KEYWORDS = [
    "wound", "ulcer", "burn", "diabetic foot", "pressure injury", "wound dressing",
    "tissue oxygen", "hyperbaric", "hydrogel", "biomaterial", "regenerative medicine",
    "skin graft", "wound infection", "antimicrobial", "trauma care", "combat casualty",
    "nitric oxide", "drug delivery", "dermatology", "veterinary wound", "amputation",
    "scar", "advanced wound care", "surgical site",
    "oral health", "gum disease", "periodontal", "skincare", "anti-aging", "cosmetic",
]

# CPV codes for TED (EU procurement classification)
TED_CPV = ["33141100", "33141110", "33000000", "33600000", "33140000"]

# Aethl application buckets (mirrors IBEX "Application" column). Order matters:
# first matching bucket wins.
APPLICATIONS = [
    ("Diabetic Foot Ulcers", ["diabetic foot", "foot ulcer", "diabetic ulcer", "dfu"]),
    ("Traumatic Wound and Burn Injuries", ["burn", "trauma", "combat", "blast", "casualty", "austere"]),
    ("Chronic Wound Care", ["chronic wound", "pressure ulcer", "non-healing", "ulcer", "wound dressing"]),
    ("Surgery Recovery", ["surgical", "surgery", "post-operative", "incision"]),
    ("Oral Health", ["oral", "gum", "periodontal", "dental"]),
    ("Skincare", ["skin", "dermatolog", "anti-aging", "cosmetic", "eczema"]),
    ("Veterinary", ["veterinary", "canine", "animal"]),
    ("Platform / Other", ["hydrogel", "biomaterial", "oxygen", "nitric oxide", "regenerative", "wound"]),
]

# Only surface items at or above this relevance (1-5). Lowered for volume;
# tune up once the LLM scoring layer removes false positives.
MIN_RELEVANCE = 2.0

# Procurement junk to drop (SAM.gov matches "wound"/"burn" in gaskets, burners...)
NOISE_TERMS = [
    "gasket", "spiral wound", "burner", "boiler", "scrubber", "burn module",
    "burning man", "fuel oil", " dvd", "cd burner", "autotour", "floor burnish",
    "burnish", "pump station", "janitor", "mowing", "communication services",
    "grove replace", "fire suppression", "engine assembly", "hvac", "duct ",
    "generator", "lawn", "snow removal", "elevator", "roofing",
]
# A SAM.gov hit must contain one of these to count as a real medical opportunity
MED_CONTEXT = [
    "wound", "ulcer", "burn injur", "tissue", "dressing", "healing", "therapeutic",
    "medical research", "clinical", "regenerative", "biomaterial", "oxygen therapy",
    "skin", "dermat", "graft", "diabetic", "trauma", "antimicrobial", "biomedical",
]

# Per-award USD -> Award Size score (1-5). Tunable to Aethl's preference.
AWARD_BANDS = [(100_000, 2), (300_000, 3), (1_000_000, 4), (float("inf"), 5)]

# Enrich Grants.gov hits with detail-page fields (funding, #awards, cost share,
# eligibility). Costs one extra API call per opportunity; capped for politeness.
ENRICH = True
ENRICH_CAP = 60

# LLM extraction layer (fills funding/#awards/eligibility/soft-scores from
# announcement text, and judges true relevance).
# Uses Google Gemini when GEMINI_API_KEY is set (free tier); else Anthropic.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
USE_LLM = bool(GEMINI_API_KEY or os.environ.get("ANTHROPIC_API_KEY"))
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
LLM_CAP = 50


def award_size_score(per_award):
    if not per_award:
        return None
    for ceiling, sc in AWARD_BANDS:
        if per_award <= ceiling:
            return sc
    return 3

USER_AGENT = "AethlFundingMonitor/1.0"


# ---------------------------------------------------------------------------
# Small HTTP helpers (stdlib only, no pip deps)
# ---------------------------------------------------------------------------

def _post_json(url, payload, timeout=40):
    data = json.dumps(payload).encode()
    req = request.Request(url, data=data, method="POST",
                          headers={"Content-Type": "application/json",
                                   "User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post_form(url, fields, timeout=40):
    data = parse.urlencode(fields).encode()
    req = request.Request(url, data=data, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                   "User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_json(url, timeout=40):
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------------------
# Common schema
# ---------------------------------------------------------------------------

def make_opp(source, opp_id, title, funder, country, vehicle, channel,
             deadline, link, summary=""):
    return {
        "source": source, "opp_id": str(opp_id), "title": (title or "").strip(),
        "funder": funder, "country": country, "vehicle": vehicle,
        "eligibility_channel": channel, "deadline": deadline or "",
        "link": link or "", "summary": (summary or "").strip(),
        "number": "", "watchlist": False, "application": "",
        # enrichment fields (filled by detail calls where available)
        "total_funding": None, "num_awards": None, "per_award": None,
        "cost_share_required": None, "eligibility_desc": "", "instrument": "",
        "posted_date": "",
        # scoring + lifecycle
        "relevance": 0.0, "tech_match": 0, "status": "ACTIVE",
    }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def fetch_grants_gov(rows=50):
    out = []
    seen = set()
    for kw in SEARCH_KEYWORDS:
        try:
            d = _post_json("https://api.grants.gov/v1/api/search2",
                           {"keyword": kw, "rows": rows,
                            "oppStatuses": "forecasted|posted"})
            for h in d.get("data", {}).get("oppHits", []):
                if h["id"] in seen:
                    continue
                seen.add(h["id"])
                op = make_opp(
                    "Grants.gov", h["id"], h.get("title"),
                    h.get("agency"), "United States", "grant/BAA",
                    "US-direct", h.get("closeDate"),
                    f"https://www.grants.gov/search-results-detail/{h['id']}",
                )
                op["number"] = h.get("number", "")
                out.append(op)
        except Exception as e:
            print(f"  [Grants.gov] '{kw}' failed: {e}", file=sys.stderr)
    return out


def fetch_sedia(page_size=30):
    out = []
    seen = set()
    for kw in ["wound healing", "tissue oxygenation", "chronic wound", "diabetic ulcer"]:
        try:
            url = ("https://api.tech.ec.europa.eu/search-api/prod/rest/search"
                   f"?apiKey=SEDIA&text={parse.quote(kw)}&pageSize={page_size}")
            d = _post_form(url, {
                "query": json.dumps({"bool": {"must": [{"text": kw}]}}),
                "languages": json.dumps(["en"]),
            })
            for r in d.get("results", []):
                meta = {k: (v[0] if isinstance(v, list) and v else v)
                        for k, v in r.get("metadata", {}).items()}
                ref = meta.get("REFERENCE") or r.get("reference")
                if ref in seen:
                    continue
                seen.add(ref)
                status = (meta.get("status") or "").lower()
                # focus on open/forthcoming calls, skip closed
                if status and status not in ("open", "forthcoming"):
                    continue
                out.append(make_opp(
                    "EU Funding & Tenders", ref,
                    meta.get("title") or r.get("title") or r.get("summary"),
                    "European Commission (Horizon/EIC)", "EU", "grant/call",
                    "EU-consortium", meta.get("es_SortDate") or meta.get("startDate"),
                    meta.get("url") or r.get("url"), r.get("summary", ""),
                ))
        except Exception as e:
            print(f"  [SEDIA] '{kw}' failed: {e}", file=sys.stderr)
    return out


def fetch_ted(limit=25):
    out = []
    cpv = " ".join(TED_CPV)
    q = f"classification-cpv IN ({cpv}) SORT BY publication-date DESC"
    try:
        d = _post_json("https://api.ted.europa.eu/v3/notices/search", {
            "query": q,
            "fields": ["publication-number", "notice-title", "publication-date",
                       "deadline-receipt-tender-date-lot"],
            "limit": limit, "scope": "ALL",
        })
        for n in d.get("notices", []):
            num = n.get("publication-number")
            title = n.get("notice-title")
            if isinstance(title, dict):  # multilingual -> prefer English
                title = title.get("eng") or title.get("ENG") or next(iter(title.values()), "")
            if isinstance(title, list):
                title = title[0]
            dl = n.get("deadline-receipt-tender-date-lot")
            if isinstance(dl, list) and dl:
                dl = dl[0]
            link = (n.get("links", {}).get("html", {}) or {}).get("ENG") \
                or f"https://ted.europa.eu/en/notice/-/detail/{num}"
            out.append(make_opp(
                "TED", num, title, "EU contracting authority", "EU",
                "tender/contract", "EU-tender", dl, link,
            ))
    except Exception as e:
        print(f"  [TED] failed: {e}", file=sys.stderr)
    return out


def enrich_grants_gov(opps):
    """Fill funding/#awards/cost-share/eligibility from the detail endpoint."""
    if not ENRICH:
        return
    n = 0
    for o in opps:
        if o["source"] != "Grants.gov" or n >= ENRICH_CAP:
            continue
        try:
            d = _post_json("https://api.grants.gov/v1/api/fetchOpportunity",
                           {"opportunityId": int(o["opp_id"])}).get("data", {})
            syn = d.get("synopsis", {}) or {}
            funding = syn.get("estimatedFunding")
            naward = syn.get("numberOfAwards")
            ceil = syn.get("awardCeiling")
            o["total_funding"] = int(funding) if funding else None
            o["num_awards"] = int(naward) if naward else None
            per = None
            if ceil:
                per = int(ceil)
            elif funding and naward:
                per = int(int(funding) / int(naward))
            elif funding:
                per = int(funding)
            o["per_award"] = per
            o["cost_share_required"] = bool(syn.get("costSharing"))
            elig = syn.get("applicantEligibilityDesc") or ""
            types = syn.get("eligibility") or syn.get("applicantTypes") or []
            if isinstance(types, list) and types:
                elig = (elig + " " + " ".join(t.get("description", "") for t in types
                        if isinstance(t, dict))).strip()
            o["eligibility_desc"] = elig[:300]
            o["instrument"] = (syn.get("fundingInstrumentDescription") or "").strip()
            o["posted_date"] = syn.get("postingDate", "")
            desc = syn.get("synopsisDesc") or ""
            if desc:
                o["summary"] = re.sub(r"<[^>]+>", " ", desc)[:4000]
            n += 1
            time.sleep(0.15)
        except Exception as e:
            print(f"  [enrich] {o['opp_id']} failed: {e}", file=sys.stderr)


def recompute_overall(o):
    """Recompute Award Size / Cost Share scores from any updated data, then Overall."""
    if o.get("per_award"):
        o["s_award"] = award_size_score(o["per_award"]) or o.get("s_award", 3)
    if o.get("cost_share_required") is not None:
        o["s_cost"] = 5 if not o["cost_share_required"] else 3
    o["overall"] = round((WEIGHTS["award"] * o.get("s_award", 3)
                          + WEIGHTS["elig"] * o.get("s_elig", 3)
                          + WEIGHTS["cost"] * o.get("s_cost", 3)
                          + WEIGHTS["indirect"] * o.get("s_indirect", 3)
                          + WEIGHTS["difficulty"] * o.get("s_difficulty", 3)
                          + WEIGHTS["tech"] * o.get("tech_match", 3)) / 10.0, 2)
    o["relevance"] = float(o.get("tech_match", 3))


def _llm_json(prompt):
    """Send a prompt to Gemini (preferred, free tier) or Anthropic; return raw text."""
    gkey = os.environ.get("GEMINI_API_KEY")
    if gkey:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMINI_MODEL}:generateContent?key={gkey}")
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 500, "temperature": 0,
                                     "responseMimeType": "application/json"}}
        req = request.Request(url, data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json",
                                       "User-Agent": USER_AGENT})
        resp = json.loads(request.urlopen(req, timeout=45).read().decode())
        return "".join(p.get("text", "")
                       for p in resp["candidates"][0]["content"]["parts"])
    akey = os.environ.get("ANTHROPIC_API_KEY")
    if akey:
        body = {"model": LLM_MODEL, "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]}
        req = request.Request("https://api.anthropic.com/v1/messages",
                              data=json.dumps(body).encode(),
                              headers={"x-api-key": akey, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json",
                                       "User-Agent": USER_AGENT})
        resp = json.loads(request.urlopen(req, timeout=45).read().decode())
        return "".join(b.get("text", "") for b in resp.get("content", [])
                       if b.get("type") == "text")
    return None


def llm_enrich(opps):
    """Use Claude to extract funding/#awards/eligibility/soft-scores from text
    and judge true relevance. Skips silently if no ANTHROPIC_API_KEY."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("  [LLM] no GEMINI_API_KEY/ANTHROPIC_API_KEY -> skipping LLM layer",
              file=sys.stderr)
        return
    provider = "Gemini" if os.environ.get("GEMINI_API_KEY") else "Anthropic"
    n = 0
    for o in opps:
        if n >= LLM_CAP:
            break
        text = (f"Title: {o['title']}\nFunder: {o['funder']}\nVehicle: {o['vehicle']}\n"
                f"Known funding: {o.get('total_funding')}\n"
                f"Description: {(o.get('summary') or '')[:4000]}")
        prompt = (
            "You screen funding opportunities for Aethl Bio, a company developing "
            "oxygen-releasing hydrogel wound dressings and therapeutic-gas biomaterials "
            "(wound healing, diabetic foot ulcers, burns, tissue oxygenation, regenerative "
            "medicine, antimicrobial wound care).\n\n"
            "From the opportunity below, return STRICT JSON only (no prose, no code fence) "
            "with these keys:\n"
            '{"relevant": bool (true only if a real R&D or partnership funding opportunity '
            "Aethl could plausibly pursue; false for procurement of goods/facilities, or "
            'unrelated topics), "total_funding": int USD or null, "num_awards": int or null, '
            '"per_award": int USD or null, "cost_share_required": bool or null, '
            '"eligibility_summary": short string, "tech_match": int 1-5 (fit to Aethl tech), '
            '"indirect_rate_score": int 1-5 (5=full indirect allowed, 1=none), '
            '"proposal_difficulty_score": int 1-5 (5=easy/short, 1=very hard)}\n\n' + text)
        try:
            txt = (_llm_json(prompt) or "").strip()
            txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
            data = json.loads(txt)
        except Exception as e:
            print(f"  [LLM] '{o['title'][:30]}' failed: {e}", file=sys.stderr)
            continue
        o["llm_relevant"] = bool(data.get("relevant", True))
        for f in ("total_funding", "num_awards", "per_award"):
            if not o.get(f) and data.get(f) is not None:
                o[f] = data[f]
        if o.get("cost_share_required") is None and data.get("cost_share_required") is not None:
            o["cost_share_required"] = data["cost_share_required"]
        if data.get("eligibility_summary"):
            o["eligibility_desc"] = str(data["eligibility_summary"])[:300]
        if data.get("tech_match"):
            o["tech_match"] = int(data["tech_match"])
        if data.get("indirect_rate_score"):
            o["s_indirect"] = int(data["indirect_rate_score"])
        if data.get("proposal_difficulty_score"):
            o["s_difficulty"] = int(data["proposal_difficulty_score"])
        recompute_overall(o)
        n += 1
        time.sleep(0.2)
    print(f"  [LLM] enriched {n} opportunities via {provider}", file=sys.stderr)


def fetch_watchlist(path="watchlist.json"):
    """Seed the exact opportunities we always want tracked (e.g. the IBEX table).
    Grants.gov 'auto' entries are resolved to a live id so they get enriched;
    others are carried as tracked entries (manual data refreshed where possible)."""
    try:
        entries = json.load(open(path))
    except Exception:
        return []
    out = []
    for e in entries:
        op = make_opp(e.get("source", "watchlist"), e.get("number"),
                      e.get("title", ""), e.get("funder", ""),
                      e.get("country", "United States"), e.get("vehicle", "grant"),
                      e.get("eligibility_channel", "US-direct"),
                      e.get("deadline", ""), e.get("link", ""))
        op["number"] = e.get("number", "")
        op["watchlist"] = True
        op["application"] = e.get("application", "")
        for f in ("total_funding", "num_awards", "cost_share_required",
                  "eligibility_desc"):
            if e.get(f) is not None:
                op[f] = e[f]
        # resolve Grants.gov auto entries to a live numeric id for enrichment
        if e.get("auto") and e.get("source") == "Grants.gov":
            try:
                d = _post_json("https://api.grants.gov/v1/api/search2",
                               {"keyword": e["number"], "rows": 5,
                                "oppStatuses": "forecasted|posted|closed"})
                norm = e["number"].replace("-", "").upper()
                for h in d.get("data", {}).get("oppHits", []):
                    if h.get("number", "").replace("-", "").upper() == norm:
                        op["opp_id"] = h["id"]
                        op["link"] = f"https://www.grants.gov/search-results-detail/{h['id']}"
                        op["deadline"] = h.get("closeDate", op["deadline"])
                        break
            except Exception as ex:
                print(f"  [watchlist] {e['number']} resolve failed: {ex}", file=sys.stderr)
        out.append(op)
    return out


def fetch_sam_gov(limit=50):
    key = os.environ.get("SAM_API_KEY")
    if not key:
        print("  [SAM.gov] SAM_API_KEY not set -> skipping", file=sys.stderr)
        return []
    out = []
    today = dt.date.today()
    frm = (today - dt.timedelta(days=60)).strftime("%m/%d/%Y")
    to = today.strftime("%m/%d/%Y")
    # specific multi-word medical R&D phrases (bare "burn"/"wound" pull procurement junk)
    for kw in ["wound care", "wound healing", "wound dressing", "burn injury",
               "diabetic foot", "tissue oxygenation", "chronic wound"]:
        try:
            url = ("https://api.sam.gov/opportunities/v2/search?"
                   + parse.urlencode({"api_key": key, "limit": limit,
                                      "postedFrom": frm, "postedTo": to,
                                      "title": kw}))
            d = _get_json(url)
            for o in d.get("opportunitiesData", []):
                op = make_opp(
                    "SAM.gov", o.get("noticeId"), o.get("title"),
                    o.get("fullParentPathName", "US Federal"), "United States",
                    o.get("type", "contract/solicitation"), "US-direct",
                    o.get("responseDeadLine"), o.get("uiLink"),
                )
                op["number"] = o.get("solicitationNumber", "")
                award = (o.get("award") or {})
                if award.get("amount"):
                    try:
                        op["total_funding"] = int(float(award["amount"]))
                    except Exception:
                        pass
                out.append(op)
            time.sleep(0.5)  # be polite to the rate limit
        except Exception as e:
            print(f"  [SAM.gov] '{kw}' failed: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Scoring  (mirrors IBEX: 6 axes 1-5, weighted Overall)
# Weights (verified against IBEX sheet): AwardSize 2, Eligibility 1,
# CostShare 1, IndirectRates 1, ProposalDifficulty 2, TechMatch 3 (sum 10).
#
# Tech Match is data-driven (keywords / LLM). The other five axes are scored
# from DEFAULT ASSUMPTIONS below until Aethl's real parameters are supplied
# (award-size bands, eligibility profile, cost-share capacity, NICRA rate).
# These are clearly-flagged placeholders, NOT final scores.
# ---------------------------------------------------------------------------

WEIGHTS = {"award": 2, "elig": 1, "cost": 1, "indirect": 1, "difficulty": 2, "tech": 3}

# Default per-channel assumptions (override once Aethl parameters are known)
CHANNEL_DEFAULTS = {
    #                 elig cost indirect difficulty
    "US-direct":      (5,   5,   4,       4),   # small-biz eligible, no match, indirect ok
    "EU-consortium":  (2,   3,   4,       2),   # needs EU partner, complex
    "EU-tender":      (2,   4,   3,       3),   # procurement, needs local presence
    "open-innovation":(4,   4,   3,       4),   # corporate CRA, open globally, simpler
}
VEHICLE_AWARD_DEFAULT = {  # rough award-size proxy until amounts are enriched
    "grant/BAA": 4, "contract/solicitation": 4, "grant/call": 4,
    "tender/contract": 3, "grant": 3,
}


def classify_application(text):
    t = text.lower()
    for name, kws in APPLICATIONS:
        if any(k in t for k in kws):
            return name
    return "Platform / Other"


def score(opp):
    text = f"{opp['title']} {opp['summary']}".lower()
    strong = sum(1 for t in STRONG_TERMS if t in text)
    medium = sum(1 for t in MEDIUM_TERMS if t in text)
    if strong >= 2:
        tm = 5
    elif strong == 1 and medium >= 1:
        tm = 4
    elif strong == 1 or medium >= 2:
        tm = 3
    elif medium == 1:
        tm = 2
    else:
        tm = 1

    elig, cost, indirect, difficulty = CHANNEL_DEFAULTS.get(
        opp["eligibility_channel"], (3, 3, 3, 3))
    award = VEHICLE_AWARD_DEFAULT.get(opp["vehicle"], 3)
    # STTR needs a university partner -> eligibility penalty
    if "sttr" in text:
        elig = min(elig, 3)

    # --- override placeholders with REAL enriched data when present ---
    if not opp.get("per_award") and opp.get("total_funding"):
        na = opp.get("num_awards") or 1
        opp["per_award"] = int(opp["total_funding"] / na)
    if opp.get("per_award"):
        award = award_size_score(opp["per_award"]) or award
    if opp.get("cost_share_required") is not None:
        cost = 5 if not opp["cost_share_required"] else 3
    ed = (opp.get("eligibility_desc") or "").lower()
    if ed:
        if "unrestricted" in ed or "small business" in ed or "for-profit" in ed \
                or "any type of entity" in ed:
            elig = max(elig, 5)
        elif "institution of higher" in ed or "nonprofit" in ed and "for-profit" not in ed:
            elig = min(elig, 3)

    opp["application"] = opp.get("application") or classify_application(text)
    opp["s_award"] = award
    opp["s_elig"] = elig
    opp["s_cost"] = cost
    opp["s_indirect"] = indirect
    opp["s_difficulty"] = difficulty
    opp["tech_match"] = tm
    overall = (WEIGHTS["award"] * award + WEIGHTS["elig"] * elig
               + WEIGHTS["cost"] * cost + WEIGHTS["indirect"] * indirect
               + WEIGHTS["difficulty"] * difficulty + WEIGHTS["tech"] * tm) / 10.0
    opp["overall"] = round(overall, 2)
    # relevance (triage signal) stays tech-match driven
    opp["relevance"] = float(tm)
    return opp


# ---------------------------------------------------------------------------
# monday.com push
# ---------------------------------------------------------------------------

def push_to_monday(opps):
    token = os.environ.get("MONDAY_TOKEN")
    board = os.environ.get("MONDAY_BOARD_ID")
    if not (token and board):
        print("  [monday] MONDAY_TOKEN / MONDAY_BOARD_ID not set -> skipping push",
              file=sys.stderr)
        return 0
    pushed = 0
    for o in opps:
        # NOTE: replace column IDs below with your board's actual column IDs.
        col_vals = json.dumps({
            "text_source": o["source"],
            "text_funder": o["funder"],
            "text_country": o["country"],
            "text_vehicle": o["vehicle"],
            "status_channel": {"label": o["eligibility_channel"]},
            "date_deadline": {"date": (o["deadline"] or "")[:10]},
            "numbers_score": o["relevance"],
            "link_url": {"url": o["link"], "text": "Opportunity"},
        }).replace('"', '\\"')
        mutation = (
            f'mutation {{ create_item (board_id: {board}, '
            f'item_name: "{o["title"][:200].replace(chr(34), chr(39))}", '
            f'column_values: "{col_vals}") {{ id }} }}'
        )
        try:
            _post = request.Request(
                "https://api.monday.com/v2",
                data=json.dumps({"query": mutation}).encode(),
                headers={"Authorization": token, "Content-Type": "application/json",
                         "User-Agent": USER_AGENT})
            with request.urlopen(_post, timeout=30) as r:
                resp = json.loads(r.read().decode())
            if resp.get("data", {}).get("create_item"):
                pushed += 1
            else:
                print(f"  [monday] error: {resp}", file=sys.stderr)
            time.sleep(0.3)
        except Exception as e:
            print(f"  [monday] push failed: {e}", file=sys.stderr)
    return pushed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="push to monday.com")
    ap.add_argument("--min", type=float, default=MIN_RELEVANCE)
    args = ap.parse_args()

    print("Fetching sources...", file=sys.stderr)
    opps = []
    opps += fetch_grants_gov()
    opps += fetch_sedia()
    opps += fetch_ted()
    opps += fetch_sam_gov()
    watch = fetch_watchlist()

    # Merge watchlist + live, deduping by normalized opportunity number.
    # Prefer the live/enriched record when both exist; otherwise keep watchlist.
    import re as _re
    def norm(n):
        return _re.sub(r"[^A-Za-z0-9]", "", str(n or "")).upper()
    by_num = {}
    leftovers = []
    for o in (opps + watch):
        key = norm(o.get("number"))
        if not key:
            leftovers.append(o); continue
        if key in by_num:
            existing = by_num[key]
            # prefer the non-watchlist (live, enriched) record; carry watchlist meta
            if existing.get("watchlist") and not o.get("watchlist"):
                o["watchlist"] = True
                for f in ("application", "funder"):
                    if not o.get(f):
                        o[f] = existing.get(f)
                by_num[key] = o
            elif not existing.get("watchlist") and o.get("watchlist"):
                existing["watchlist"] = True   # live kept, but mark it tracked
                for f in ("application", "funder"):
                    if not existing.get(f):
                        existing[f] = o.get(f)
        else:
            by_num[key] = o
    merged = list(by_num.values())

    # dedupe only the number-less leftovers (EU/TED) by (source, opp_id)
    uniq = {}
    for o in leftovers:
        uniq[(o["source"], o["opp_id"])] = o
    opps = merged + list(uniq.values())

    # enrich Grants.gov hits with detail-page fields (funding, #awards, etc.)
    print("Enriching with detail data...", file=sys.stderr)
    enrich_grants_gov(opps)

    # score + filter (drop procurement junk; require medical context for SAM)
    for o in opps:
        score(o)

    def keep(o):
        if o.get("watchlist"):
            return True
        t = f"{o['title']} {o.get('summary','')}".lower()
        if any(n in t for n in NOISE_TERMS):
            return False
        if o["source"] == "SAM.gov" and not any(c in t for c in MED_CONTEXT):
            return False
        return o["relevance"] >= args.min

    opps = [o for o in opps if keep(o)]

    # LLM layer: extract funding/#awards/eligibility/soft-scores + judge relevance
    if USE_LLM:
        print("Running LLM extraction...", file=sys.stderr)
        llm_enrich(opps)
        opps = [o for o in opps if o.get("watchlist") or o.get("llm_relevant", True)]

    # ---- lifecycle diff: NEW (added since last run) / REMOVED (gone) ----
    STATE = "opportunities_state.json"
    today = dt.date.today().isoformat()
    try:
        prior = json.load(open(STATE))
    except Exception:
        prior = {}
    current_keys = {f"{o['source']}|{o['opp_id']}" for o in opps}
    new_count = 0
    for o in opps:
        key = f"{o['source']}|{o['opp_id']}"
        if key not in prior:
            o["status"] = "NEW"
            new_count += 1
        else:
            o["status"] = "ACTIVE"
    # things that dropped out since last run
    removed = []
    for key, rec in prior.items():
        if key not in current_keys:
            rec["status"] = "REMOVED"
            rec["removed_on"] = today
            removed.append(rec)
    # persist state (active set + first_seen)
    state_out = {}
    for o in opps:
        key = f"{o['source']}|{o['opp_id']}"
        state_out[key] = {"title": o["title"], "source": o["source"],
                          "opp_id": o["opp_id"],
                          "first_seen": prior.get(key, {}).get("first_seen", today),
                          "last_seen": today, "status": "ACTIVE"}
    json.dump(state_out, open(STATE, "w"), indent=2)

    opps.sort(key=lambda x: (0 if x["status"] == "NEW" else 1,
                             -x["overall"], x["source"]))

    # write outputs (active + a separate removed log)
    json.dump(opps, open("opportunities.json", "w"), indent=2)
    if opps:
        with open("opportunities.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(opps[0].keys()))
            w.writeheader()
            w.writerows(opps)
    if removed:
        json.dump(removed, open("opportunities_removed.json", "w"), indent=2)

    print(f"\n{len(opps)} active opportunities  |  {new_count} NEW  |  "
          f"{len(removed)} removed since last run\n")
    for o in opps[:25]:
        tag = "NEW" if o["status"] == "NEW" else "   "
        fund = f"${o['total_funding']:,}" if o.get("total_funding") else "—"
        print(f"  {tag} [{o['overall']:.2f}] {o['source']:<20} {fund:>10}  "
              f"{o['title'][:55]}")
    if removed:
        print("\n  REMOVED since last run:")
        for r in removed:
            print(f"      {r['source']:<20} {r['title'][:60]}")

    if args.push:
        n = push_to_monday(opps)
        print(f"\nPushed {n} items to monday.com.")


if __name__ == "__main__":
    main()
