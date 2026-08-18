"""Watchlist resolution: a ticker is all the team types (build spec v4 §2).

Company name, sector, industry, exchange, country and currency come from Yahoo
Finance at add time, so the sources page asks for nothing but the symbol.
Non-US symbols keep their suffix and dash (`0700.HK`, `7203.T`, `BRK-B`).
When Yahoo does not know the symbol — or is slow, rate limited, or broken —
the SEC registrant list answers with the filer's title, and a ticker nobody
lists resolves to None so the caller can say so.

`cik` is always joined from `registry_sec` when the ticker is there: EDGAR
polling still only covers SEC registrants, and other rows are skipped by
edgar.poll exactly as before.

yfinance drags in pandas, numpy and curl_cffi, so it is imported inside
`_yfinance()` rather than at module load, and every call runs in a worker
thread with a budget: a hung Yahoo request must never pin an API worker.
"""
import threading

# quote types we keep; anything else (index, future, currency, mutual fund)
# is not a company we can build a graph around
QUOTE_TYPES = ("EQUITY", "ETF")
INFO_TIMEOUT = 10          # seconds; §2 "10 s budget"
SEARCH_TIMEOUT = 10


def _yfinance():
    """The yfinance module, imported on first use. Tests replace this or
    sys.modules['yfinance'] so nothing touches the network."""
    import yfinance
    return yfinance


def _with_budget(fn, timeout: float):
    """Run fn() in a daemon thread and give up after `timeout` seconds. The
    thread is abandoned rather than joined, so a hung HTTP call costs one
    thread and not the request."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"gave up after {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def clean_ticker(ticker: str) -> str:
    """Uppercase and strip; Yahoo symbols keep their suffix and dash."""
    return (ticker or "").strip().upper()


def info_of(ticker: str, timeout: float | None = None) -> dict:
    """`yfinance.Ticker(t).info`, or {} when Yahoo does not answer in time.
    Unknown symbols come back as an all-null dict, not an error."""
    yf = _yfinance()
    data = _with_budget(lambda: yf.Ticker(ticker).info,
                        INFO_TIMEOUT if timeout is None else timeout)
    return data if isinstance(data, dict) else {}


def resolve(con, ticker: str) -> dict | None:
    """Company facts for a ticker: Yahoo Finance first, the SEC registrant
    list second, None when neither lists it. Never raises for a network,
    rate-limit or parse failure — those degrade to the SEC step."""
    t = clean_ticker(ticker)
    if not t:
        return None
    reg = con.execute(
        "select cik, title from registry_sec where ticker=%s", (t,)).fetchone()

    info = {}
    try:
        info = info_of(t)
    except Exception as e:
        print(f"watchlist: yahoo lookup failed for {t} ({e})")

    quote_type = (info.get("quoteType") or "").upper()
    name = info.get("longName") or info.get("shortName")
    if quote_type in QUOTE_TYPES and name:
        out = {
            "ticker": t,
            "name": name.strip(),
            "sector": info.get("sector") or None,
            "industry": info.get("industry") or None,
            "exchange": info.get("fullExchangeName") or info.get("exchange") or None,
            "country": info.get("country") or None,
            "currency": info.get("currency") or None,
            "quote_type": quote_type,
            "website": info.get("website") or None,
            "resolver": "yfinance",
        }
    elif reg:
        out = {
            "ticker": t,
            "name": reg["title"],
            "sector": None, "industry": None, "exchange": None,
            "country": None, "currency": None, "quote_type": None,
            "website": None,
            "resolver": "sec",
        }
    else:
        return None

    out["cik"] = reg["cik"] if reg else None
    return out


def search(q: str, limit: int = 8) -> list[dict]:
    """Name search for the add box: [{symbol, name, exchange, type}] of
    companies and ETFs. Yahoo failures return an empty list, never an error —
    the box is a convenience, not the add path."""
    query = (q or "").strip()
    if not query:
        return []
    try:
        yf = _yfinance()
        quotes = _with_budget(
            lambda: yf.Search(query, max_results=max(limit, 1) * 2).quotes,
            SEARCH_TIMEOUT)
    except Exception as e:
        print(f"watchlist: yahoo search failed for {query!r} ({e})")
        return []

    out = []
    for quote in quotes or []:
        if not isinstance(quote, dict):
            continue
        quote_type = (quote.get("quoteType") or "").upper()
        symbol = (quote.get("symbol") or "").strip()
        if quote_type not in QUOTE_TYPES or not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": (quote.get("shortname") or quote.get("longname") or "").strip(),
            "exchange": (quote.get("exchDisp") or "").strip(),
            "type": quote_type,
        })
        if len(out) >= limit:
            break
    return out


UPSERT_SQL = """
insert into watchlist (ticker, sector, active, added_by, name, industry,
                       exchange, country, currency, quote_type, website, cik,
                       resolver, resolved_at)
values (%(ticker)s, %(sector)s, true, %(added_by)s, %(name)s, %(industry)s,
        %(exchange)s, %(country)s, %(currency)s, %(quote_type)s, %(website)s,
        %(cik)s, %(resolver)s, now())
on conflict (ticker) do update set
    active      = true,
    sector      = coalesce(excluded.sector, watchlist.sector),
    name        = excluded.name,
    industry    = excluded.industry,
    exchange    = excluded.exchange,
    country     = excluded.country,
    currency    = excluded.currency,
    quote_type  = excluded.quote_type,
    website     = excluded.website,
    cik         = excluded.cik,
    resolver    = excluded.resolver,
    resolved_at = now()
"""


def upsert(con, ticker: str, info: dict | None, added_by: str,
           sector_override: str | None = None) -> None:
    """Write the resolved columns for a ticker. On conflict the row comes
    back to active and the resolved columns refresh; sector only changes when
    the override or the resolver gave one, and `added_at` is never touched.
    No commit (callers commit)."""
    info = info or {}
    sector = (sector_override or "").strip() or info.get("sector") or None
    con.execute(UPSERT_SQL, {
        "ticker": clean_ticker(ticker),
        "sector": sector,
        "added_by": added_by,
        "name": info.get("name"),
        "industry": info.get("industry"),
        "exchange": info.get("exchange"),
        "country": info.get("country"),
        "currency": info.get("currency"),
        "quote_type": info.get("quote_type"),
        "website": info.get("website"),
        "cik": info.get("cik"),
        "resolver": info.get("resolver"),
    })


def resolve_and_upsert(con, ticker: str, added_by: str,
                       sector_override: str | None = None) -> dict | None:
    """Resolve a ticker and store it; the stored row, or None when neither
    Yahoo Finance nor the SEC registry lists the symbol. The one path behind
    POST /api/watchlist and `graph add-ticker`. No commit."""
    t = clean_ticker(ticker)
    info = resolve(con, t)
    if info is None:
        return None
    upsert(con, t, info, added_by, sector_override)
    row = con.execute("select * from watchlist where ticker=%s", (t,)).fetchone()
    return dict(row) if row else None
