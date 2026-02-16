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
| `VITALFORGE_USER` | No | Login username (default: `admin`) |
| `VITALFORGE_PASS` | No | Login password. If empty, auth is disabled (open access) |
| `VITALFORGE_SECRET` | No | Secret key for signing session cookies |
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
│   ├── auth.py                # Cookie-based session authentication
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
- Weight and body fat %
- Training load
- Steps and active calories

### Recommendations engine

The recommendations feature uses a hybrid approach:

1. **Rules engine** — Detects patterns like consecutive poor sleep, elevated RHR, declining HRV, overtraining risk, and cross-metric correlations
2. **LLM layer** (optional) — Sends findings to Claude API for personalized, actionable recommendations

If no `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` is set, the system falls back to rules engine output only.

### Authentication

Cookie-based session auth with a 30-day expiry. Set `VITALFORGE_PASS` in `.env` to enable. Both services share the same credentials. Leave `VITALFORGE_PASS` empty to disable auth (open access).

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
   - If using auth, add header: `Cookie: vf_session=YOUR_SESSION_COOKIE`
4. Add action: **Alert > Flash**
   - Text: `Weight logged: %input lbs`

### Home screen widget

1. Go to Tasker > Tasks > long-press your weight task
2. Select "Add Shortcut to Home"
3. One tap to log weight from your home screen

### Auth with Tasker

Since VitalForge uses cookie-based auth, the easiest approach:

1. Log in via browser and copy the `vf_session` cookie value from dev tools
2. Add as header in HTTP Request: `Cookie: vf_session=YOUR_COOKIE_VALUE`
3. Session lasts 30 days — log in again when it expires

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
| `POST` | `/api/weight` | Log weight (`{"weight": 185.4, "unit": "lbs"}`) |
| `GET` | `/api/weight/recent` | Last 10 weigh-ins |
| `GET` | `/api/weight/trend` | Last 30 days for trend chart |
| `DELETE` | `/api/weight/{id}` | Delete a weigh-in |

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

Available metrics: `sleep_duration`, `sleep_score`, `resting_hr`, `hrv`, `body_battery`, `body_battery_low`, `stress`, `vo2max`, `weight`, `body_fat`, `training_load`, `steps`, `active_calories`

## License

MIT
