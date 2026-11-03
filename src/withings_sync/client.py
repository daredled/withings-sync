"""OAuth 2.0 and HTTP client for the Withings Public API (standard library only).

Withings is not REST-style: every call is a POST with an `action` field, and errors come back
as HTTP 200 with a non-zero `status` in the JSON body.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
DEFAULT_SCOPES = "user.metrics"
USER_AGENT = "withings-sync/0.1"

DIASTOLIC, SYSTOLIC, PULSE = 9, 10, 11
BP_COLUMNS = ["date", "time", "systolic", "diastolic", "pulse", "grpid", "timezone"]


class WithingsError(RuntimeError):
    """Any failure talking to Withings (HTTP error, API status, bad OAuth state, missing tokens)."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    redirect_uri: str = "http://127.0.0.1:3001/callback"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Credentials:
        """Read WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET / WITHINGS_REDIRECT_URI.

        Real environment variables win over values in `env_file`.
        """
        values = _read_env_file(env_file) if env_file and env_file.exists() else {}
        values.update({k: v for k, v in os.environ.items() if k.startswith("WITHINGS_")})
        missing = [k for k in ("WITHINGS_CLIENT_ID", "WITHINGS_CLIENT_SECRET") if not values.get(k)]
        if missing:
            raise WithingsError(f"Missing {', '.join(missing)} (environment or {env_file})")
        return cls(
            values["WITHINGS_CLIENT_ID"],
            values["WITHINGS_CLIENT_SECRET"],
            values.get("WITHINGS_REDIRECT_URI") or cls.redirect_uri,
        )


def _read_env_file(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def default_tokens_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "withings-sync" / "tokens.json"


def _request_json(url: str, data: dict, headers: dict | None = None, retries: int = 3) -> dict:
    body = urllib.parse.urlencode(data).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or e.code >= 500
            if retryable and attempt < retries:
                time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                continue
            detail = e.read().decode(errors="replace")[:500]
            raise WithingsError(f"HTTP {e.code} from {url}: {detail}") from e
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise WithingsError(f"Cannot reach {url}: {e.reason}") from e
    raise AssertionError("unreachable")


def _call(url: str, data: dict, headers: dict | None = None) -> dict:
    """POST to Withings and return `body`, raising on a non-zero `status`."""
    resp = _request_json(url, data, headers)
    if resp.get("status") != 0:
        raise WithingsError(f"Withings {data.get('action')} failed: status {resp.get('status')} "
                            f"{resp.get('error', '')}".strip())
    return resp["body"]


class WithingsClient:
    def __init__(self, credentials: Credentials, tokens_file: Path | None = None,
                 scopes: str = DEFAULT_SCOPES):
        self.creds = credentials
        self.tokens_file = tokens_file or default_tokens_file()
        self.scopes = scopes

    # ---- OAuth -------------------------------------------------------------

    def authorization_url(self, state: str) -> str:
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.creds.client_id,
            "redirect_uri": self.creds.redirect_uri,
            "scope": self.scopes,
            "state": state,
        })

    @staticmethod
    def new_state() -> str:
        return secrets.token_urlsafe(12)

    def wait_for_redirect(self, state: str) -> str:
        """Serve the redirect URI locally until Withings calls back; return the auth code."""
        redirect = urllib.parse.urlparse(self.creds.redirect_uri)
        result: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result.update({k: v[0] for k, v in q.items()})
                ok = "code" in result and result.get("state") == state
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = "Authorized. You can close this tab." if ok else "Authorization failed."
                self.wfile.write(f"<h2>{msg}</h2>".encode())

            def log_message(self, *args):
                pass

        with http.server.HTTPServer((redirect.hostname, redirect.port or 80), Handler) as srv:
            while "code" not in result and "error" not in result:
                srv.handle_request()
        return self.code_from_redirect_params(result, state)

    @staticmethod
    def code_from_redirect_url(url: str, state: str) -> str:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return WithingsClient.code_from_redirect_params({k: v[0] for k, v in q.items()}, state)

    @staticmethod
    def code_from_redirect_params(params: dict[str, str], state: str) -> str:
        if "error" in params:
            raise WithingsError(f"Withings returned an error: {params}")
        if params.get("state") != state:
            raise WithingsError("OAuth state mismatch; start the authorization again")
        if "code" not in params:
            raise WithingsError("Redirect has no authorization code")
        return params["code"]

    def exchange_code(self, code: str) -> dict:
        tokens = _call(TOKEN_URL, {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.creds.client_id,
            "client_secret": self.creds.client_secret,
            "redirect_uri": self.creds.redirect_uri,
        })
        self._save_tokens(tokens)
        return tokens

    def _save_tokens(self, tokens: dict) -> None:
        tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 10800)) - 60
        self.tokens_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.tokens_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.tokens_file)

    def access_token(self) -> str:
        if not self.tokens_file.exists():
            raise WithingsError(f"No tokens at {self.tokens_file}; run `withings-sync auth` first")
        tokens = json.loads(self.tokens_file.read_text())
        if time.time() < tokens.get("expires_at", 0):
            return tokens["access_token"]
        # Withings rotates refresh tokens: the new pair is written to disk before anything
        # else can fail, so the old one is never needed again.
        new = _call(TOKEN_URL, {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": self.creds.client_id,
            "client_secret": self.creds.client_secret,
        })
        self._save_tokens(new)
        return new["access_token"]

    # ---- API ---------------------------------------------------------------

    def measure_groups(self, start: datetime, end: datetime,
                       meastypes=(SYSTOLIC, DIASTOLIC, PULSE)) -> list[dict]:
        """Every real (non-goal) measurement group between `start` and `end`, following pagination."""
        groups, offset = [], None
        while True:
            data = {
                "action": "getmeas",
                "meastypes": ",".join(map(str, meastypes)),
                "category": 1,
                "startdate": int(start.timestamp()),
                "enddate": int(end.timestamp()),
            }
            if offset is not None:
                data["offset"] = offset
            body = _call(MEASURE_URL, data, {"Authorization": f"Bearer {self.access_token()}"})
            groups += body.get("measuregrps", [])
            if not body.get("more"):
                return groups
            offset = body["offset"]


def blood_pressure_rows(groups: list[dict]) -> dict[str, dict]:
    """One row per measurement group that has systolic and diastolic, keyed by grpid.

    Times are local to the timezone Withings stored with each group.
    """
    rows = {}
    for g in groups:
        values = {m["type"]: m["value"] * 10 ** m["unit"] for m in g.get("measures", [])}
        if SYSTOLIC not in values or DIASTOLIC not in values:
            continue
        tz_name = g.get("timezone") or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = timezone.utc
        when = datetime.fromtimestamp(g["date"], tz)
        rows[str(g["grpid"])] = {
            "date": when.strftime("%Y-%m-%d"),
            "time": when.strftime("%H:%M"),
            "systolic": round(values[SYSTOLIC]),
            "diastolic": round(values[DIASTOLIC]),
            "pulse": round(values[PULSE]) if PULSE in values else "",
            "grpid": str(g["grpid"]),
            "timezone": tz_name,
        }
    return rows
