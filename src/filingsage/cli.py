"""Command-line entrypoints. Thin by design: parse args, wire dependencies,
call library code, print. All logic lives in the library where tests reach it."""

import argparse
from collections.abc import Sequence
from datetime import date

from filingsage.config import get_settings
from filingsage.connectors import EdgarClient, EdgarConnector, FilingRef
from filingsage.gold.retrieval import search
from filingsage.parsing.silver import ParseQuarantineError, parse_to_silver
from filingsage.worker.recovery import (
    RECOVERY_BATCH_DELAY_SECONDS,
    RECOVERY_BATCH_SIZE,
    recover_stale_filings,
)
from filingsage.worker.tasks import ingest_watchlist


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


def cmd_parse(args: argparse.Namespace) -> None:
    settings = get_settings()
    connector = _build_connector()
    filings = connector.discover(args.tickers, since=args.since)
    for ticker, rows in _group(filings).items():
        print(f"\n{ticker}: parsing {min(args.limit, len(rows))} of {len(rows)} filings")
        for ref in rows[: args.limit]:
            bronze_path = connector.fetch_raw(ref)
            try:
                result = parse_to_silver(bronze_path, ref, settings.data_dir / "silver")
            except ParseQuarantineError as exc:
                print(f"  {ref.filed_at}  {ref.form_type:<5} QUARANTINED — {exc}")
                continue
            note = f" ({result.duplicate_count} dup dropped)" if result.duplicate_count else ""
            print(
                f"  {ref.filed_at}  {ref.form_type:<5} -> {result.silver_path.name} "
                f"({result.section_count} sections{note})"
            )


def cmd_ingest(args: argparse.Namespace) -> None:
    result = ingest_watchlist.delay(args.tickers, args.limit)
    tickers = ", ".join(t.upper() for t in args.tickers)
    print(f"Enqueued ingest_watchlist task {result.id} for {tickers}")
    print("The worker consumes it asynchronously — follow progress with:")
    print("  docker compose logs -f worker")


def cmd_search(args: argparse.Namespace) -> None:
    results = search(
        args.query,
        ticker=args.ticker,
        form_type=args.form_type,
        since=args.since,
        limit=args.limit,
    )
    if not results:
        print("No results.")
        return
    for i, r in enumerate(results, start=1):
        snippet = r.text[:160].replace("\n", " ")
        if len(r.text) > 160:
            snippet += "..."
        print(
            f"{i:>2}. [{r.fusion_score:.4f}] {r.ticker} {r.form_type} {r.filed_at} "
            f"{r.section} ({r.accession_number})"
        )
        print(f"    {snippet}")


def cmd_recover_stale(args: argparse.Namespace) -> None:
    plan = recover_stale_filings(
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        batch_delay_seconds=args.batch_delay,
    )
    verb = "Would reset" if args.dry_run else "Reset"
    print(f"{len(plan.intact)} filing(s) intact on disk — left untouched.")
    print(f"{verb} {len(plan.reset)} filing(s) to DISCOVERED (bronze missing).")
    if args.dry_run:
        if plan.reset:
            print("\nRe-run without --dry-run to reset these and re-enqueue fetch_filing:")
            for accession_no in plan.reset[:20]:
                print(f"  {accession_no}")
            if len(plan.reset) > 20:
                print(f"  ... and {len(plan.reset) - 20} more")
    elif plan.reset:
        print(
            f"\nRe-enqueued fetch_filing for all {len(plan.reset)} in batches of "
            f"{args.batch_size}, {args.batch_delay:.0f}s apart — this will take "
            f"roughly {(len(plan.reset) / args.batch_size) * args.batch_delay / 60:.0f} "
            "minutes to finish enqueueing."
        )


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

    p_parse = sub.add_parser("parse", help="Discover, fetch, and parse into sectioned silver Parquet")
    p_parse.add_argument("tickers", nargs="+", help="Ticker symbols, e.g. AAPL MSFT NVDA")
    p_parse.add_argument("--since", type=date.fromisoformat, default=None,
                         help="Only filings on/after this date (YYYY-MM-DD)")
    p_parse.add_argument("--limit", type=int, default=3,
                         help="Most recent filings to parse per ticker (default 3)")
    p_parse.set_defaults(func=cmd_parse)

    p_ingest = sub.add_parser(
        "ingest", help="Enqueue discover->fetch->parse for a watchlist (worker-side pipeline)"
    )
    p_ingest.add_argument("tickers", nargs="+", help="Ticker symbols, e.g. AAPL MSFT NVDA")
    p_ingest.add_argument("--limit", type=int, default=None,
                          help="Most recent filings to ingest per ticker (default: all discovered)")
    p_ingest.set_defaults(func=cmd_ingest)

    p_search = sub.add_parser(
        "search", help="Hybrid dense+sparse search over embedded filing chunks (spec §6)"
    )
    p_search.add_argument("query", help="Free-text query")
    p_search.add_argument("--ticker", default=None, help="Restrict to one ticker, e.g. GOOGL")
    p_search.add_argument("--form-type", default=None, dest="form_type",
                          help="Restrict to one form type, e.g. 10-K")
    p_search.add_argument("--since", type=date.fromisoformat, default=None,
                          help="Only filings on/after this date (YYYY-MM-DD)")
    p_search.add_argument("--limit", type=int, default=10,
                          help="Max results to print (default 10; spec default is 40)")
    p_search.set_defaults(func=cmd_search)

    p_recover = sub.add_parser(
        "recover-stale",
        help=(
            "Reset filings whose bronze/silver were lost on disk (e.g. a "
            "destroyed/recreated volume) back to DISCOVERED and re-enqueue "
            "fetch_filing — see README → Technical Decisions #27"
        ),
    )
    p_recover.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be reset/re-enqueued without changing anything",
    )
    p_recover.add_argument(
        "--batch-size", type=int, default=RECOVERY_BATCH_SIZE,
        help=f"fetch_filing tasks to enqueue per batch (default {RECOVERY_BATCH_SIZE})",
    )
    p_recover.add_argument(
        "--batch-delay", type=float, default=RECOVERY_BATCH_DELAY_SECONDS,
        help=f"Seconds to wait between batches (default {RECOVERY_BATCH_DELAY_SECONDS:.0f})",
    )
    p_recover.set_defaults(func=cmd_recover_stale)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()