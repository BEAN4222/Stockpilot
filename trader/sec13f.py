"""
Latest 13F holdings of an institutional manager (e.g. Berkshire Hathaway),
from SEC EDGAR, with CUSIPs turned into tickers through OpenFIGI.

How the latest portfolio is built:
  1. data.sec.gov/submissions lists the manager's filings. The latest report
     period is the newest one with an original 13F-HR.
  2. Every 13F-HR and 13F-HR/A for that period is read, oldest first:
     an original or a RESTATEMENT replaces the holdings, a NEW HOLDINGS
     amendment adds to them (Berkshire uses these to reveal positions it was
     allowed to keep confidential at first).
  3. Rows are summed per CUSIP (one stock is reported once per sub-manager),
     keeping only shares (SH), never options (putCall rows are dropped).

Parsed filings and ticker lookups are cached in cache/, since neither changes.
SEC asks every client to send a User-Agent with a contact email: set
SEC_USER_AGENT in .env, e.g. "StockTrader you@example.com".
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import requests

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{name}"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_BATCH = 10  # max jobs per request without an API key


@dataclass
class Holding:
    cusip: str
    issuer: str
    title: str
    shares: float
    value: float


@dataclass
class Filing13F:
    cik: str
    report_date: str
    filed_date: str            # date of the newest filing used
    accessions: List[str]
    confidential_omitted: bool
    holdings: List[Holding] = field(default_factory=list)


# ------------------------------------------------------------------ parsing
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_info_table(xml_text: str) -> List[Holding]:
    """Rows of a 13F information table, shares only (no options)."""
    rows = []
    for entry in ET.fromstring(xml_text):
        fields = {_local(e.tag): (e.text or "").strip() for e in entry.iter()}
        if fields.get("putCall") or fields.get("sshPrnamtType") != "SH":
            continue
        rows.append(Holding(
            cusip=fields["cusip"].upper(),
            issuer=fields.get("nameOfIssuer", ""),
            title=fields.get("titleOfClass", ""),
            shares=float(fields.get("sshPrnamt") or 0),
            value=float(fields.get("value") or 0),
        ))
    return rows


def parse_cover(xml_text: str) -> dict:
    """amendment type ("" for an original) and the confidential flag."""
    fields = {_local(e.tag): (e.text or "").strip() for e in ET.fromstring(xml_text).iter()}
    return {
        "amendment_type": fields.get("amendmentType", "").upper(),
        "confidential_omitted": fields.get("isConfidentialOmitted", "").lower() == "true",
    }


def aggregate(rows: List[Holding]) -> List[Holding]:
    """One entry per CUSIP, largest value first."""
    by_cusip: Dict[str, Holding] = {}
    for r in rows:
        if r.cusip in by_cusip:
            by_cusip[r.cusip].shares += r.shares
            by_cusip[r.cusip].value += r.value
        else:
            by_cusip[r.cusip] = Holding(**asdict(r))
    return sorted(by_cusip.values(), key=lambda h: -h.value)


def combine(filings: List[dict]) -> List[Holding]:
    """Apply one period's filings (oldest first, each with "amendment_type"
    and "holdings") and return the resulting holdings."""
    rows: List[Holding] = []
    for f in filings:
        if f["amendment_type"] == "NEW HOLDINGS":
            rows = rows + f["holdings"]
        else:  # original or RESTATEMENT
            rows = list(f["holdings"])
    return aggregate(rows)


# ------------------------------------------------------------------ fetching
class SEC13FClient:
    def __init__(self, cache_dir: str = "cache"):
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if "@" not in user_agent:
            raise ValueError(
                'SEC_USER_AGENT is not set. SEC requires a contact email, e.g. '
                'SEC_USER_AGENT="StockTrader you@example.com" in .env'
            )
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.cache_dir = cache_dir

    def _get(self, url: str) -> requests.Response:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        time.sleep(0.2)  # SEC allows 10 requests/s; stay well below
        return response

    def _read_filing(self, cik: str, accession: str) -> dict:
        """Cover info and holdings of one filing, cached on disk."""
        path = os.path.join(self.cache_dir, "13f", f"{accession}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["holdings"] = [Holding(**h) for h in data["holdings"]]
            return data

        folder = accession.replace("-", "")
        cik_num = str(int(cik))
        index = self._get(ARCHIVE_URL.format(cik=cik_num, accession=folder, name="index.json")).json()
        names = [item["name"] for item in index["directory"]["item"]]
        table_name = next(n for n in names if n.endswith(".xml") and n != "primary_doc.xml")
        cover = parse_cover(self._get(
            ARCHIVE_URL.format(cik=cik_num, accession=folder, name="primary_doc.xml")).text)
        holdings = parse_info_table(self._get(
            ARCHIVE_URL.format(cik=cik_num, accession=folder, name=table_name)).text)

        data = {**cover, "holdings": holdings}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**cover, "holdings": [asdict(h) for h in holdings]}, f)
        return data

    def latest(self, cik: str) -> Filing13F:
        cik = str(cik).zfill(10)
        recent = self._get(SUBMISSIONS_URL.format(cik=cik)).json()["filings"]["recent"]
        filings = [
            {"form": form, "accession": acc, "filed": filed, "period": period}
            for form, acc, filed, period in zip(
                recent["form"], recent["accessionNumber"], recent["filingDate"], recent["reportDate"])
            if form in ("13F-HR", "13F-HR/A")
        ]
        originals = [f for f in filings if f["form"] == "13F-HR"]
        if not originals:
            raise RuntimeError(f"No 13F-HR filings found for CIK {cik}")
        period = max(f["period"] for f in originals)
        same_period = sorted((f for f in filings if f["period"] == period),
                             key=lambda f: (f["filed"], f["accession"]))
        # An amendment filed before the original makes no sense; start at the original.
        first = next(i for i, f in enumerate(same_period) if f["form"] == "13F-HR")
        same_period = same_period[first:]

        read = [self._read_filing(cik, f["accession"]) for f in same_period]
        for r in read[1:]:
            if not r["amendment_type"]:
                r["amendment_type"] = "RESTATEMENT"  # a later 13F-HR replaces the earlier one
        return Filing13F(
            cik=cik,
            report_date=period,
            filed_date=same_period[-1]["filed"],
            accessions=[f["accession"] for f in same_period],
            confidential_omitted=any(r["confidential_omitted"] for r in read),
            holdings=combine(read),
        )


# ------------------------------------------------------------ CUSIP -> ticker
def _figi_ticker(result: dict) -> Optional[str]:
    for item in result.get("data") or []:
        ticker = item.get("ticker")
        if ticker and item.get("marketSector") == "Equity":
            return ticker.replace("/", ".")  # OpenFIGI "LEN/B" is Alpaca "LEN.B"
    return None


def map_cusips(cusips: List[str], cache_dir: str = "cache",
               overrides: Optional[Dict[str, str]] = None) -> Dict[str, Optional[str]]:
    """CUSIP -> ticker (None when OpenFIGI does not know it). `overrides`
    (from trading_config.json) wins over everything."""
    overrides = {k.upper(): v.upper() for k, v in (overrides or {}).items()}
    path = os.path.join(cache_dir, "cusip_tickers.json")
    cache: Dict[str, Optional[str]] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)

    missing = [c for c in cusips if c not in cache and c not in overrides]
    for i in range(0, len(missing), OPENFIGI_BATCH):
        batch = missing[i:i + OPENFIGI_BATCH]
        # Non-US issuers (e.g. Chubb, "H1467J104") have a CINS code, which
        # starts with a letter; OpenFIGI only finds those as ID_CINS.
        jobs = [{"idType": "ID_CINS" if c[:1].isalpha() else "ID_CUSIP", "idValue": c, "exchCode": "US"}
                for c in batch]
        for _ in range(3):
            response = requests.post(OPENFIGI_URL, json=jobs, timeout=30)
            if response.status_code != 429:
                break
            time.sleep(float(response.headers.get("ratelimit-reset", 60)) + 1)
        response.raise_for_status()
        for cusip, result in zip(batch, response.json()):
            # Cache only definite answers, not transient errors.
            if "data" in result or "No identifier found" in result.get("warning", ""):
                cache[cusip] = _figi_ticker(result)

    if missing:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
    return {c: overrides.get(c, cache.get(c)) for c in cusips}
