"""Watchlist resolution (build spec v4 §2). No network: a fake yfinance module
is installed in sys.modules, so `import yfinance` inside graph.watchlist gets
the fake. registry_sec holds NVDA and AMD (conftest)."""
import sys
import threading
import time
import types

import pytest

from graph import watchlist

# What Yahoo returns for a listed company (the fields §2 reads).
INFO_NVDA = {
    "quoteType": "EQUITY",
    "longName": "NVIDIA Corporation",
    "shortName": "NVIDIA Corp",
    "sector": "Technology",
    "industry": "Semiconductors",
    "fullExchangeName": "NasdaqGS",
    "exchange": "NMS",
    "country": "United States",
    "currency": "USD",
    "website": "https://www.nvidia.com",
}

INFO_TENCENT = {
    "quoteType": "EQUITY",
    "longName": "Tencent Holdings Limited",
    "sector": "Communication Services",
    "industry": "Internet Content & Information",
    "fullExchangeName": "HKSE",
    "country": "China",
    "currency": "HKD",
    "website": "https://www.tencent.com",
}

# An unknown symbol: Yahoo answers with an all-null dict, not an error.
INFO_NULL = {"quoteType": None, "longName": None, "shortName": None,
             "sector": None, "industry": None, "currency": None}


def fake_yfinance(monkeypatch, infos=None, quotes=None, ticker_error=None,
                  search_error=None, hang=None):
    """Install a fake yfinance module and return the call log."""
    calls = {"tickers": [], "searches": []}

    class Ticker:
        def __init__(self, symbol, **kw):
            calls["tickers"].append(symbol)
            self.symbol = symbol

        @property
        def info(self):
            if hang:
                threading.Event().wait(hang)
            if ticker_error:
                raise ticker_error
            return dict((infos or {}).get(self.symbol, INFO_NULL))

    class Search:
        def __init__(self, query, **kw):
            calls["searches"].append((query, kw))
            if search_error:
                raise search_error
            self.quotes = list(quotes or [])

    module = types.ModuleType("yfinance")
    module.Ticker = Ticker
    module.Search = Search
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return calls


# ------------------------------------------------------------------ resolve


def test_resolve_equity_stores_company_facts(con, monkeypatch):
    fake_yfinance(monkeypatch, infos={"NVDA": INFO_NVDA})

    info = watchlist.resolve(con, " nvda ")
    assert info["ticker"] == "NVDA"
    assert info["resolver"] == "yfinance"
    assert info["name"] == "NVIDIA Corporation"
    assert info["sector"] == "Technology"
    assert info["industry"] == "Semiconductors"
    assert info["exchange"] == "NasdaqGS"      # fullExchangeName wins
    assert info["country"] == "United States"
    assert info["currency"] == "USD"
    assert info["quote_type"] == "EQUITY"
    assert info["website"] == "https://www.nvidia.com"
    assert info["cik"] == 1045810              # joined from registry_sec

    row = watchlist.resolve_and_upsert(con, "nvda", "web")
    assert row["ticker"] == "NVDA"
    assert row["active"] is True
    assert row["name"] == "NVIDIA Corporation"
    assert row["sector"] == "Technology"
    assert row["industry"] == "Semiconductors"
    assert row["exchange"] == "NasdaqGS"
    assert row["country"] == "United States"
    assert row["currency"] == "USD"
    assert row["cik"] == 1045810
    assert row["resolver"] == "yfinance"
    assert row["resolved_at"] is not None
    assert row["added_by"] == "web"


def test_resolve_keeps_non_us_symbol_verbatim(con, monkeypatch):
    calls = fake_yfinance(monkeypatch, infos={"0700.HK": INFO_TENCENT})

    row = watchlist.resolve_and_upsert(con, " 0700.hk ", "web")
    assert calls["tickers"] == ["0700.HK"]     # suffix kept, uppercased
    assert row["ticker"] == "0700.HK"
    assert row["name"] == "Tencent Holdings Limited"
    assert row["exchange"] == "HKSE"
    assert row["currency"] == "HKD"
    assert row["cik"] is None                  # not an SEC registrant
    assert row["resolver"] == "yfinance"


def test_resolve_falls_back_to_sec_registry(con, monkeypatch):
    fake_yfinance(monkeypatch)                 # every symbol -> all-null info

    info = watchlist.resolve(con, "amd")
    assert info["resolver"] == "sec"
    assert info["name"] == "ADVANCED MICRO DEVICES INC"
    assert info["cik"] == 2488
    assert info["sector"] is None
    assert info["exchange"] is None

    row = watchlist.resolve_and_upsert(con, "amd", "cli", sector_override="Semis")
    assert row["sector"] == "Semis"            # the override still applies
    assert row["resolver"] == "sec"
    assert row["cik"] == 2488


def test_resolve_unknown_everywhere_is_none(con, monkeypatch):
    fake_yfinance(monkeypatch)

    assert watchlist.resolve(con, "ZZZZ") is None
    assert watchlist.resolve(con, "  ") is None
    assert watchlist.resolve_and_upsert(con, "ZZZZ", "web") is None
    assert con.execute("select count(*) n from watchlist "
                       "where ticker='ZZZZ'").fetchone()["n"] == 0


def test_resolve_survives_a_yahoo_error(con, monkeypatch):
    fake_yfinance(monkeypatch, ticker_error=RuntimeError("429 Too Many Requests"))

    info = watchlist.resolve(con, "NVDA")      # degrades, never raises
    assert info["resolver"] == "sec"
    assert info["name"] == "NVIDIA CORP"
    assert watchlist.resolve(con, "SAP.DE") is None


def test_resolve_gives_up_on_a_hung_call(con, monkeypatch):
    fake_yfinance(monkeypatch, infos={"NVDA": INFO_NVDA}, hang=5)
    monkeypatch.setattr(watchlist, "INFO_TIMEOUT", 0.1)

    started = time.monotonic()
    info = watchlist.resolve(con, "NVDA")
    assert time.monotonic() - started < 2     # the budget, not the hung call
    assert info["resolver"] == "sec"


# ------------------------------------------------------------------- upsert


def test_upsert_reactivates_and_refreshes(con, monkeypatch):
    fake_yfinance(monkeypatch, infos={"NVDA": INFO_NVDA})

    watchlist.upsert(con, "NVDA", watchlist.resolve(con, "NVDA"), "seed",
                     sector_override="Chips")
    # pause it and age the resolve so the refresh is visible (now() is the
    # transaction clock, so two upserts in one test share a timestamp)
    con.execute("update watchlist set active=false, "
                "resolved_at = resolved_at - interval '1 day' "
                "where ticker='NVDA'")
    first = con.execute("select * from watchlist where ticker='NVDA'").fetchone()

    stale = dict(INFO_NVDA, longName="NVIDIA Corp Renamed", sector=None,
                 industry="Semiconductor Equipment")
    fake_yfinance(monkeypatch, infos={"NVDA": stale})
    row = watchlist.resolve_and_upsert(con, "NVDA", "web")

    assert row["active"] is True                        # paused -> active
    assert row["name"] == "NVIDIA Corp Renamed"         # refreshed
    assert row["industry"] == "Semiconductor Equipment"
    assert row["sector"] == "Chips"                     # kept: no new sector
    assert row["added_at"] == first["added_at"]         # never touched
    assert row["resolved_at"] > first["resolved_at"]


# ------------------------------------------------------------------- search


def test_search_filters_and_shapes_quotes(monkeypatch):
    quotes = [
        {"symbol": "NVDA", "shortname": "NVIDIA Corporation",
         "longname": "NVIDIA Corporation Inc", "exchDisp": "NASDAQ",
         "quoteType": "EQUITY"},
        {"symbol": "^NDX", "shortname": "Nasdaq 100", "exchDisp": "Nasdaq GIDS",
         "quoteType": "INDEX"},
        {"symbol": "NVDL", "longname": "GraniteShares 2x Long NVDA ETF",
         "exchDisp": "NASDAQ", "quoteType": "ETF"},
        {"symbol": "NVDAX", "shortname": "Some Fund", "exchDisp": "NASDAQ",
         "quoteType": "MUTUALFUND"},
    ]
    calls = fake_yfinance(monkeypatch, quotes=quotes)

    out = watchlist.search("nvidia")
    assert calls["searches"][0][0] == "nvidia"
    assert out == [
        {"symbol": "NVDA", "name": "NVIDIA Corporation", "exchange": "NASDAQ",
         "type": "EQUITY"},
        {"symbol": "NVDL", "name": "GraniteShares 2x Long NVDA ETF",
         "exchange": "NASDAQ", "type": "ETF"},
    ]
    assert len(watchlist.search("nvidia", limit=1)) == 1


def test_search_never_raises(monkeypatch):
    fake_yfinance(monkeypatch, search_error=RuntimeError("Yahoo is down"))
    assert watchlist.search("nvidia") == []

    calls = fake_yfinance(monkeypatch, quotes=[])
    assert watchlist.search("   ") == []
    assert calls["searches"] == []            # nothing asked for an empty query


@pytest.mark.parametrize("raw,expected", [
    (" nvda ", "NVDA"), ("brk-b", "BRK-B"), ("7203.t", "7203.T"), (None, ""),
])
def test_clean_ticker(raw, expected):
    assert watchlist.clean_ticker(raw) == expected
