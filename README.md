# VitalForge

Personal health metrics platform powered by Garmin Connect.

**Built to solve one problem:** stepping on a scale and getting that weight into Garmin Connect should be as fast as tapping your phone on an NFC tag.

### The workflow

1. Step on your scale, read your weight
2. Tap your phone on an NFC sticker attached to the scale
3. The VitalForge PWA opens instantly — type the number, hit Log
4. Weight is pushed to Garmin Connect and saved locally in under a second

No opening apps, no navigating menus, no waiting for Bluetooth sync. Just weigh, tap, done.

From there, VitalForge grew into a full health dashboard that pulls all your Garmin data (sleep, HRV, resting HR, stress, body battery, VO2 max, training load) and surfaces trends and AI-powered recommendations.

### Two services

- **vitalforge-weight** (port 8085) — Mobile-first PWA for quick weight logging to Garmin Connect
- **vitalforge-dashboard** (port 8086) — Health metrics dashboard with trends and AI-powered recommendations

## Quick Start (pre-built images)

No building required. Pull and run the latest images:

```bash
curl -O https://raw.githubusercontent.com/bearyjd/vitalforge/main/docker-compose.prod.yml
curl -O https://raw.githubusercontent.com/bearyjd/vitalforge/main/.env.example
cp .env.example .env
# Edit .env with your Garmin credentials and auth settings
docker compose -f docker-compose.prod.yml up -d
```

Images are published to both registries on every push:

| Registry | Weight | Dashboard |
|---|---|---|
| **Docker Hub** | `bearyj/vitalforge-weight` | `bearyj/vitalforge-dashboard` |
| **GHCR** | `ghcr.io/bearyjd/vitalforge-weight` | `ghcr.io/bearyjd/vitalforge-dashboard` |

Or pull individually:

```bash
docker pull bearyj/vitalforge-weight:latest
docker pull bearyj/vitalforge-dashboard:latest
```

## Setup (build from source)

### 1. Clone and configure

```bash
git clone https://github.com/bearyjd/vitalforge.git
cd vitalforge
cp .env.example .env
```

Edit `.env` with your credentials:

```
GARMIN_EMAIL=your_garmin_email@example.com
GARMIN_PASSWORD=your_garmin_password
ANTHROPIC_API_KEY=sk-ant-your-api-key-here
VITALFORGE_USER=admin
VITALFORGE_PASS=your-password-here
VITALFORGE_SECRET=your-random-secret-here
```

### 2. Build and run

```bash
docker compose up --build
```

### 3. Verify

```bash
curl http://localhost:8085/health
curl http://localhost:8086/health
```

Visit `http://localhost:8085` for weight logging and `http://localhost:8086` for the dashboard.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GARMIN_EMAIL` | Yes | Your Garmin Connect email |
| `GARMIN_PASSWORD` | Yes | Your Garmin Connect password |
| `ANTHROPIC_API_KEY` | No | Claude API key for AI recommendations (rules engine works without it) |
| `ANTHROPIC_BASE_URL` | No | Custom API base URL (e.g. `http://localhost:4000` for LiteLLM proxy) |
| `VITALFORGE_USER` | No | One-time bootstrap username (default: `admin`) — seeds the first admin account on first boot if no users exist yet; not read for ongoing auth after that (manage accounts from `/auth/admin/users` instead) |
| `VITALFORGE_PASS` | No | One-time bootstrap password for the above. If empty and no users exist yet, auth is disabled (open access) |
| `VITALFORGE_SECRET` | No | Secret key for signing session cookies. If unset or left as the placeholder default, a random one is generated per process at startup and a warning is logged — sessions won't survive a restart, and if you run both services, they won't share sign-on until this is set explicitly |
| `VITALFORGE_API_TOKEN` | No | Upgrade compatibility only: imported once as a named token owned by the first admin. Leave empty on new installs and create per-user tokens at `/auth/account` |
| `WEIGHT_URL` | No | Public URL for weight service (e.g. `https://weight.yourdomain.com`) |
| `DASHBOARD_URL` | No | Public URL for dashboard service (e.g. `https://health.yourdomain.com`) |
| `DEFAULT_UNIT` | No | Default weight unit: `lbs` or `kg` (default: `lbs`) |
| `TZ` | No | IANA timezone for timestamps (e.g. `America/New_York`). Omit for browser default |

Generate a random secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Architecture

```
vitalforge/
├── shared/                    # Shared Python modules
│   ├── auth.py                # Cookie-session + bearer-token authentication
│   ├── database.py            # SQLite connection and schema setup
│   └── garmin_client.py       # Garmin Connect API wrapper (garminconnect)
├── vitalforge-weight/         # Weight logging PWA service
│   ├── app.py                 # FastAPI app — weight CRUD + Garmin push
│   ├── templates/index.html   # Mobile-first weight entry UI
│   └── static/                # PWA manifest, service worker, icons
├── vitalforge-dashboard/      # Health dashboard service
│   ├── app.py                 # FastAPI app — metrics API + sync
│   ├── sync.py                # Garmin data sync (scheduled + manual)
│   ├── recommendations.py     # Hybrid rules + LLM recommendation engine
│   ├── templates/index.html   # Dashboard UI with Chart.js visualizations
│   └── static/                # PWA manifest, service worker
├── nginx/                     # Reverse proxy config for custom domains
├── docker-compose.yml         # Development — builds from source
├── docker-compose.prod.yml    # Production — pulls from GHCR
└── .github/workflows/         # CI/CD — builds and pushes Docker images
```

- **Data volume** — SQLite database and Garmin auth tokens persist in a Docker volume at `/app/data`
- **Docker health checks** — Both containers report health via `/health` endpoint
- **Non-root containers** — Entrypoint fixes volume permissions, then drops to dedicated `vitalforge` user
- **CI/CD** — GitHub Actions builds and pushes images to GHCR on every push to `main`

### Data sync

The dashboard automatically syncs data from Garmin Connect every 2 hours. You can also trigger a manual sync from the dashboard UI. Synced metrics:

- Sleep duration and sleep score
- Resting heart rate and HRV
- Body Battery (daily high/low)
- Stress levels
- VO2 Max
- Weight, body fat %, body water %, bone mass, and muscle mass
- Training load
- Steps and active calories

All of the above are synced, stored, and queryable via `/api/metrics/{name}` (see
[API Reference](#api-reference)). The dashboard UI currently charts weight and body fat
only — body water, bone mass, and muscle mass are not yet rendered as charts, though the
data is there for anyone querying the API directly.

### Recommendations engine

The recommendations feature uses a hybrid approach:

1. **Rules engine** — Detects patterns like consecutive poor sleep, elevated RHR, declining HRV, overtraining risk, and cross-metric correlations
2. **LLM layer** (optional) — Sends findings to Claude API for personalized, actionable recommendations

If no `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` is set, the system falls back to rules engine output only.

### Authentication

VitalForge has real user accounts (a `users` table, shared by both services) instead of one
password for everyone. Two roles:

- **admin** — everything a `user` can do, plus creating/editing/deleting any account from
  `/auth/admin/users`.
- **user** — logs weigh-ins, views the dashboard, manages their own password from
  `/auth/account`.

**First boot.** If no users exist yet, `VITALFORGE_USER`/`VITALFORGE_PASS` seed one admin
account automatically — after that, those two env vars are no longer read for ongoing auth,
only for this one-time bootstrap. An empty `users` table (both env vars unset) means auth is
disabled entirely (open access), matching local-dev convenience; the moment any account
exists, auth is always on for both services.

Two credential types, both optional and checked independently — a request with either a
valid cookie or a valid bearer token is authenticated, and a wrong or missing one never
blocks the other:

- **Cookie-based session auth** for browsers, 30-day expiry. A signed cookie only proves
  *who* you are — your role is re-read from the database on every request, so demoting or
  deleting an account takes effect on its very next request, not after the cookie expires.
- **Bearer-token auth** for unattended/machine clients (Tasker, scripts, the Bascule Android
  client). Each account creates named tokens from `/auth/account`; the raw value is shown
  only once and requests present it as `Authorization: Bearer <token>`. Only a SHA-256 hash
  is stored. Tokens inherit their owner's current role and can be revoked independently.
  Existing `VITALFORGE_API_TOKEN` values are imported once for upgrade compatibility as a
  token owned by the first admin.

**Revoking a credential.** Rotating one does **not** revoke the other:

- Rotate `VITALFORGE_SECRET` to invalidate every outstanding session cookie at once.
- Revoke an individual bearer token from `/auth/account`, or from the administrator token
  list. Tokens do not expire automatically.
- Delete a user's account from `/auth/admin/users` to revoke their sessions and all of their
  tokens outright — takes effect immediately, not just on their next login.

If a token is leaked, revoke that token. If the session signing secret is leaked, rotate
`VITALFORGE_SECRET` to invalidate all cookies.

## Deployment

### Docker images

Images are automatically built and pushed to **Docker Hub** and **GHCR** on every push to `main`:

- `bearyj/vitalforge-weight:latest` / `ghcr.io/bearyjd/vitalforge-weight:latest`
- `bearyj/vitalforge-dashboard:latest` / `ghcr.io/bearyjd/vitalforge-dashboard:latest`

Tagged releases (`v1.0.0`) also produce versioned image tags.

### Deploy to a server

```bash
# On your server
mkdir vitalforge && cd vitalforge
curl -O https://raw.githubusercontent.com/bearyjd/vitalforge/main/docker-compose.prod.yml
curl -O https://raw.githubusercontent.com/bearyjd/vitalforge/main/.env.example
cp .env.example .env
# Edit .env with your credentials
docker compose -f docker-compose.prod.yml up -d
```

### Update to latest

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

## Upgrading

Some releases (starting with the `001-person-id-rebuild` schema migration) change the
database schema in a way that is not safely readable by an older image. For these:

1. **Stop both services before upgrading**: `docker compose down`, not a rolling restart —
   an old container must never run against the new schema mid-upgrade.
2. Pull/build the new images, then `docker compose up`.
3. The new image takes an automatic pre-migration snapshot (`fitness.pre-001-person-id.db`,
   next to `fitness.db` in the `vitalforge-data` volume) before it changes anything, verified
   with a SQLite integrity check. To name the primary person yourself, set
   `VITALFORGE_PRIMARY_PERSON` in `.env` **before** this upgrade — it is read once, during the
   one-shot migration. Without it the name defaults to the first admin's username, slugified,
   on an upgrade of an existing deployment, and to `primary` on a brand-new install (where no
   admin account exists yet at the moment the migration runs).
4. If the migration fails (most commonly: insufficient free space for the snapshot), the
   container will restart-loop with an error naming the cause and the fix. Free up space and
   restart, or — after taking your own volume-level backup — set
   `VITALFORGE_SKIP_MIGRATION_SNAPSHOT=1` to proceed without the automatic snapshot.
5. **Rollback**: stop both services, replace `fitness.db` with the pre-migration snapshot
   (removing any `-wal`/`-shm` sidecar files), redeploy the previous images.
6. Once the upgrade is verified good and at least 7 days have passed, delete
   `fitness.pre-001-person-id.db` — it is a full second copy of your health data and is not
   cleaned up automatically.

## Nginx (optional)

Copy `nginx/nginx.conf` to your nginx configuration and update the `server_name` values:

```
server_name weight.yourdomain.com;
server_name dashboard.yourdomain.com;
```

The nav bar in each service automatically detects whether you're behind nginx (subdomain routing) or using direct ports.

For SSL, add Let's Encrypt with certbot:

```bash
sudo certbot --nginx -d weight.yourdomain.com -d dashboard.yourdomain.com
```

## PWA Installation

Both services are installable as Progressive Web Apps:

- **Desktop**: Visit `http://localhost:8085` in Chrome, click the install icon in the address bar
- **Android (local)**: Connect phone via USB, use Chrome DevTools port forwarding, visit `localhost:8085` on phone
- **Android (production)**: Visit `https://weight.yourdomain.com`, tap "Add to Home Screen"
- **Quick access**: Use ngrok (`ngrok http 8085`) for a temporary HTTPS URL to install from

## Tasker Integration (Android)

Log weight from your Android phone using Tasker without opening the browser.

### Quick weight-log task

1. **Create a new Task** in Tasker
2. Add action: **Input > Input Dialog**
   - Title: `Weight`
   - Input Type: `Decimal`
3. Add action: **Net > HTTP Request**
   - Method: `POST`
   - URL: `https://weight.yourdomain.com/api/weight`
   - Headers: `Content-Type: application/json`
   - Body: `{"weight": %input, "unit": "lbs"}`
   - If using auth, add header: `Authorization: Bearer YOUR_API_TOKEN`
4. Add action: **Alert > Flash**
   - Text: `Weight logged: %input lbs`

### Home screen widget

1. Go to Tasker > Tasks > long-press your weight task
2. Select "Add Shortcut to Home"
3. One tap to log weight from your home screen

### Auth with Tasker

Tasker is an unattended client, so use bearer-token auth rather than a browser session cookie:

1. Sign in as the account Tasker should use and create a named token at `/auth/account`
2. Add as header in HTTP Request: `Authorization: Bearer YOUR_API_TOKEN`
3. The token has no expiry — revoke it from `/auth/account` when it is no longer needed

## NFC Tag Integration

Pair an NFC tag with your scale for a tap-to-log workflow:

1. **Get an NFC tag** — NTAG213 stickers work well, attach one to or near your scale
2. **Install Tasker + AutoNFC plugin** (or use Tasker's built-in NFC support)
3. **Write the tag** — Use any NFC writer app to write a unique identifier
4. **Create a Tasker Profile:**
   - Trigger: **Event > Net > NFC Tag** (select your tag)
   - Link to the weight-logging task above
5. **Workflow:** Step on scale, read weight, tap phone to NFC tag, enter weight, done

### Alternative: NFC Tools app

If you don't use Tasker, the free "NFC Tools" app can open a URL on tap:

1. Write a URL record to the tag: `https://weight.yourdomain.com`
2. Tapping the tag opens the weight PWA directly

## API Reference

### Weight Service (port 8085)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/` | Weight entry UI |
| `POST` | `/api/weight` | Log weight and optional body composition (see below) |
| `GET` | `/api/weight/recent` | Last 10 weigh-ins |
| `GET` | `/api/weight/trend` | Last 30 days for trend chart |
| `DELETE` | `/api/weight/{id}` | Delete a weigh-in |

**Body composition.** All composition fields are optional and independent of each other:

```json
{
  "weight": 185.4,
  "unit": "lbs",
  "body_fat_pct": 19.9,
  "body_water_pct": 54.4,
  "muscle_pct": 40.7,
  "bone_mass_kg": 3.72,
  "source": "pwa"
}
```

| Field | Required | Range | Notes |
|---|---|---|---|
| `weight` | Yes | 2–500 kg after unit conversion | |
| `unit` | No | `lbs` or `kg` | defaults to `lbs` |
| `body_fat_pct` | No | 3–75 | |
| `body_water_pct` | No | 30–80 | |
| `muscle_pct` | No | 10–90 | converted to a mass before being pushed to Garmin |
| `bone_mass_kg` | No | 0.5–10 | |
| `source` | No | `pwa`, `bascule`, `bridge`, or `tasker` | free-text tag, not tied to a specific credential |

Every field pushed to Garmin is best-effort — the local write always succeeds
independently of Garmin's response. `synced_to_garmin` reports whether the Garmin push
worked; `garmin_error` is present when it didn't.

**Deduplication.** A POST within 60 seconds and 50g of an existing entry is treated as
the same weigh-in rather than a new one:

- No new data (including `source`) → the existing row is returned unchanged
  (`"deduplicated": true`).
- New composition fields or `source` the existing row doesn't have yet → those fields
  are added to the existing row and (for composition fields) re-pushed to Garmin
  (`"deduplicated": true, "enriched": true`).
- A field present on both sides with a different value (composition fields or `source`)
  → the original value is kept, and the response returns both `"conflict": true` and
  `"conflict_fields"` (an array naming every field that conflicted, e.g.
  `["body_fat_pct", "source"]`) so a client doesn't need server-log access to see what
  was rejected.

### Dashboard Service (port 8086)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/` | Dashboard UI |
| `POST` | `/api/sync?days=7` | Trigger manual Garmin sync |
| `GET` | `/api/sync/status` | Last sync time and status |
| `GET` | `/api/metrics/{name}?days=30` | Time series data with 7-day moving average |
| `GET` | `/api/recommendations` | AI-powered health recommendations |
| `GET` | `/api/recommendations/rules-only` | Rules engine output without LLM |
| `GET` | `/api/correlations?metrics=a,b,c&days=30&lag=0&min_pairs=5` | Ad-hoc cross-metric correlation matrix |
| `GET` | `/api/export?metric=all\|{name}&days=30&format=csv\|json` | Download metric data as a CSV or JSON file |

Available metrics: `sleep_duration`, `sleep_score`, `resting_hr`, `hrv`, `body_battery`, `body_battery_low`, `stress`, `vo2max`, `weight`, `body_fat`, `body_water`, `bone_mass`, `muscle_mass`, `training_load`, `steps`, `active_calories`

`weight`, `bone_mass`, and `muscle_mass` are in **grams** (matching the `_kg`-suffixed
request fields' storage convention, confirmed against a real Garmin read-back — see
`docs/prp/00-design.md` §3.5/§4.3 and `docs/prp/03-live-validation.md`); `body_fat` and
`body_water` are plain percentages, same as the request fields they come from.

**Correlations.** `/api/correlations` returns a row-major NxN Pearson correlation matrix
(`cells[i][j] = {"r": float|null, "n": int}`) across any subset of the metrics above —
`weight_log` (the raw, timestamp-keyed weigh-in log) is deliberately not queryable here since
every other metric is date-keyed and the alignment is a plain date inner-join. `lag` shifts
each row metric forward `lag` calendar days before joining against each column metric, so a
positive lag tests whether a change in the row metric predicts the column metric `lag` days
later — this makes the matrix asymmetric by design when `lag != 0`. `r` is `null` (never
`NaN`) whenever fewer than `min_pairs` aligned points exist or either series has zero
variance; `n` always reports the actual aligned pair count regardless. The dashboard UI's
Correlations section renders this as a heatmap with a click-to-drill-down scatter plot.

## License

MIT
