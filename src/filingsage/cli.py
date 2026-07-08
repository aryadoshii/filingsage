"""Command-line entrypoints. Thin by design: parse args, wire dependencies,
call library code, print. All logic lives in the library where tests reach it."""

import argparse
from collections.abc import Sequence
from datetime import date

from filingsage.config import get_settings
from filingsage.connectors import EdgarClient, EdgarConnector, FilingRef


def discover(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m filingsage.cli",
        description="Discover recent 10-K/10-Q/8-K filings for tickers via SEC EDGAR.",
    )
    parser.add_argument("tickers", nargs="+", help="Ticker symbols, e.g. AAPL MSFT NVDA")
    parser.add_argument("--since", type=date.fromisoformat, default=None,
                        help="Only filings on/after this date (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    settings = get_settings()
    client = EdgarClient(contact_email=settings.sec_contact_email)
    connector = EdgarConnector(client, bronze_dir=settings.bronze_dir)
    filings = connector.discover(args.tickers, since=args.since)

    by_ticker: dict[str, list[FilingRef]] = {}
    for f in filings:
        by_ticker.setdefault(f.ticker, []).append(f)

    for ticker in dict.fromkeys(t.upper() for t in args.tickers):
        rows = by_ticker.get(ticker, [])
        print(f"\n{ticker}: {len(rows)} filings (10-K/10-Q/8-K) in EDGAR's recent window")
        for f in rows[:5]:
            print(f"  {f.filed_at}  {f.form_type:<5} {f.accession_number}  {f.primary_document}")
        if len(rows) > 5:
            print(f"  ... and {len(rows) - 5} more")

    print(f"\nBronze snapshots written under: {settings.bronze_dir}")


if __name__ == "__main__":
    discover()