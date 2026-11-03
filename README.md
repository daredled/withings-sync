# withings-sync

Download your own blood pressure readings through the [Withings Public API](https://developer.withings.com/)
and keep them in a CSV, one row per reading.

- Standard library only, no runtime dependencies.
- OAuth 2.0 with automatic refresh-token rotation. Tokens are stored locally with `0600` permissions.
- Each reading uses the local time of the timezone Withings stored with it.
- Re-running a sync updates the readings it downloads and never duplicates rows.

## 1. Create a Withings app

1. Go to the [Withings Developer Dashboard](https://developer.withings.com/dashboard/) and choose **Europe Cloud**
   (the US Cloud requires a contract).
2. Create a **Public API integration** app with target environment **Production**.
3. Register the callback URL `http://127.0.0.1:3001/callback`. Withings warns that `http`/localhost URLs limit the
   app to 10 users; that is fine for personal use. Do not press *Test*: Withings cannot reach your machine.
4. Put the credentials in a `.env` file. Real environment variables also work.

   ```
   WITHINGS_CLIENT_ID=...
   WITHINGS_CLIENT_SECRET=...
   WITHINGS_REDIRECT_URI=http://127.0.0.1:3001/callback
   ```

## 2. Authorize (once)

```bash
withings-sync auth
```

Open the printed URL and approve access. If your browser cannot reach `127.0.0.1:3001` (for example a Windows
browser with the tool running in WSL), press Ctrl+C and use `withings-sync auth --manual`, then paste the URL
from the address bar, even if the page shows an error.

Tokens are saved to `~/.config/withings-sync/tokens.json`. Change the location with `--tokens-file`.
Refresh tokens last one year and are renewed on every use.

## 3. Sync

```bash
withings-sync sync --days 30 --csv blood_pressure.csv --raw-dir withings_raw/
```

| Option | Default | Meaning |
|---|---|---|
| `--days` | 30 | How many days back to download |
| `--csv` | `blood_pressure.csv` | CSV to create or update |
| `--raw-dir` | – | Also save the raw API response as JSON |

Columns: `date, time, systolic, diastolic, pulse, grpid, timezone` (pressures in mmHg, pulse in bpm).
Readings only appear after the Withings app on your phone has synced them to the cloud.
