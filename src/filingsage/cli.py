"""Command-line entrypoints. Thin by design: parse args, wire dependencies,
call library code, print. All logic lives in the library where tests reach it."""

import argparse
from collections.abc import Sequence
from datetime import date

from filingsage.config import get_settings
from filingsage.connectors import EdgarClient, EdgarConnector, FilingRef


def _build_connector() -> EdgarConnector:
    settings = get_settings()
    client = EdgarClient(contact_email=settings.sec_contact_email)
    return EdgarConnector(client, bronze_dir=settings.bronze_dir)


def _group(filings: list[FilingRef]) -> dict[str, list[FilingRef]]:
    by_ticker: dict[str, list[FilingRef]] = {}
    for f in filings:
        by_ticker.setdefault(f.ticker, []).append(f)
    return by_ticker


def cmd_discover(args: argparse.Namespace) -> None:
    connector = _build_connector()
    filings = connector.discover(args.tickers, since=args.since)
    by_ticker = _group(filings)
    for ticker in dict.fromkeys(t.upper() for t in args.tickers):
        rows = by_ticker.get(ticker, [])
        print(f"\n{ticker}: {len(rows)} filings (10-K/10-Q/8-K) in EDGAR's recent window")
        for f in rows[:5]:
            print(f"  {f.filed_at}  {f.form_type:<5} {f.accession_number}  {f.primary_document}")
        if len(rows) > 5:
            print(f"  ... and {len(rows) - 5} more")
    print(f"\nBronze snapshots written under: {get_settings().bronze_dir}")


def cmd_fetch(args: argparse.Namespace) -> None:
    connector = _build_connector()
    filings = connector.discover(args.tickers, since=args.since)
    for ticker, rows in _group(filings).items():
        print(f"\n{ticker}: fetching {min(args.limit, len(rows))} of {len(rows)} filings")
        for ref in rows[: args.limit]:  # newest first, per EDGAR's ordering
            path = connector.fetch_raw(ref)
            size_kb = path.stat().st_size / 1024
            print(f"  {ref.filed_at}  {ref.form_type:<5} -> {path} ({size_kb:.0f} KB)")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m filingsage.cli",
        description="FilingSage ingestion commands (SEC EDGAR).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="List recent 10-K/10-Q/8-K filings per ticker")
    p_discover.add_argument("tickers", nargs="+", help="Ticker symbols, e.g. AAPL MSFT NVDA")
    p_discover.add_argument("--since", type=date.fromisoformat, default=None,
                            help="Only filings on/after this date (YYYY-MM-DD)")
    p_discover.set_defaults(func=cmd_discover)

    p_fetch = sub.add_parser("fetch", help="Discover, then download primary documents to bronze")
    p_fetch.add_argument("tickers", nargs="+", help="Ticker symbols, e.g. AAPL MSFT NVDA")
    p_fetch.add_argument("--since", type=date.fromisoformat, default=None,
                         help="Only filings on/after this date (YYYY-MM-DD)")
    p_fetch.add_argument("--limit", type=int, default=3,
                         help="Most recent filings to fetch per ticker (default 3)")
    p_fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()