"""Command line interface: `withings-sync auth` and `withings-sync sync`."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import (BP_COLUMNS, Credentials, WithingsClient, WithingsError,
                     blood_pressure_rows, default_tokens_file)


def _client(args) -> WithingsClient:
    return WithingsClient(Credentials.from_env(args.env_file), args.tokens_file)


def cmd_auth(args) -> None:
    client = _client(args)
    state = client.new_state()
    print("Open this URL in a browser and approve access:\n\n" + client.authorization_url(state) + "\n")
    if args.manual:
        url = input("Paste the full URL you were redirected to (even if the page shows an error): ")
        code = client.code_from_redirect_url(url.strip(), state)
    else:
        print(f"Waiting for the redirect to {client.creds.redirect_uri} ... "
              "(Ctrl+C to cancel; if it never arrives, use `auth --manual`)")
        code = client.wait_for_redirect(state)
    client.exchange_code(code)
    print(f"Authorized. Tokens saved to {client.tokens_file}")


def merge_bp_csv(path: Path, rows: dict[str, dict]) -> None:
    """Upsert blood pressure readings by Withings group id, sorted by date and time."""
    existing: dict[str, dict] = {}
    if path.exists():
        with path.open(newline="") as f:
            existing = {r["grpid"]: r for r in csv.DictReader(f)}
    existing.update(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BP_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(existing.values(), key=lambda r: (r["date"], r["time"], r["grpid"])):
            writer.writerow(row)


def cmd_sync(args) -> None:
    client = _client(args)
    end = datetime.now(timezone.utc)
    groups = client.measure_groups(end - timedelta(days=args.days), end)
    if args.raw_dir:
        args.raw_dir.mkdir(parents=True, exist_ok=True)
        (args.raw_dir / "blood_pressure.json").write_text(json.dumps(groups, indent=2, ensure_ascii=False))
    rows = blood_pressure_rows(groups)
    merge_bp_csv(args.csv, rows)
    print(f"{len(rows)} blood pressure readings updated in {args.csv}.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="withings-sync", description=__doc__)
    p.add_argument("--env-file", type=Path, default=Path(".env"),
                   help="file with WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET / WITHINGS_REDIRECT_URI "
                        "(default: ./.env)")
    p.add_argument("--tokens-file", type=Path, default=default_tokens_file(),
                   help="where OAuth tokens are stored (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="authorize access to your Withings account (run once)")
    a.add_argument("--manual", action="store_true",
                   help="paste the redirect URL instead of running a local callback server")
    a.set_defaults(func=cmd_auth)

    s = sub.add_parser("sync", help="download recent blood pressure readings into a CSV")
    s.add_argument("--days", type=int, default=30, help="how many days back to download (default: 30)")
    s.add_argument("--csv", type=Path, default=Path("blood_pressure.csv"),
                   help="CSV to update, one row per reading (default: %(default)s)")
    s.add_argument("--raw-dir", type=Path, help="also save the raw API response as JSON in this directory")
    s.set_defaults(func=cmd_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except WithingsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
