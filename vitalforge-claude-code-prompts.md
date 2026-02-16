# VitalForge — Claude Code Prompts

Use these prompts sequentially with Claude Code to build the project in phases. Each phase builds on the previous one.

---

## Phase 0: Project Scaffolding

```
Create a Python project called "vitalforge" with the following structure:

vitalforge/
├── docker-compose.yml
├── .env.example                  # Garmin credentials, Claude API key
├── shared/
│   ├── __init__.py
│   ├── garmin_client.py          # Shared garth authentication wrapper
│   └── database.py               # SQLite connection/setup using aiosqlite
├── vitalforge-weight/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py
│   ├── static/
│   │   ├── manifest.json
│   │   ├── sw.js
│   │   └── icon-192.png          # placeholder
│   └── templates/
│       └── index.html
├── vitalforge-dashboard/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py
│   ├── static/
│   │   ├── manifest.json
│   │   └── sw.js
│   └── templates/
│       └── index.html
├── nginx/
│   └── nginx.conf
└── README.md

Requirements:
- Each service uses FastAPI with uvicorn
- Shared garmin_client.py wraps the `garth` library:
  - Handles initial authentication with email/password from .env
  - Persists tokens to a Docker volume at /app/data/.garth/
  - Auto-refreshes tokens
  - Provides methods that both services will use
- SQLite database stored at /app/data/fitness.db (shared Docker volume)
- docker-compose.yml defines:
  - vitalforge-weight service on port 8085
  - vitalforge-dashboard service on port 8086
  - shared volume for /app/data (tokens + database)
  - .env file for GARMIN_EMAIL, GARMIN_PASSWORD, ANTHROPIC_API_KEY
  - restart: unless-stopped on all services
- nginx.conf with upstream configs for both services (user will customize domain names)
- .env.example with placeholder values
- README.md with setup instructions

Do NOT implement any business logic yet — just the scaffolding, shared auth module, database setup, and Docker configuration. Each app.py should just have a health check endpoint at GET /health.
```

---

## Phase 1: Push Weight Service

```
Implement the vitalforge-weight service in vitalforge/vitalforge-weight/.

API Endpoints:
- POST /api/weight
  - Accepts JSON: { "weight": 185.4, "unit": "lbs" }
  - unit is optional, defaults to "lbs", also accepts "kg"
  - Converts to grams for Garmin API
  - Pushes to Garmin Connect using shared garmin_client
  - Saves to local SQLite database with timestamp
  - Returns: { "success": true, "weight_lbs": 185.4, "timestamp": "..." }

- GET /api/weight/recent
  - Returns last 10 weigh-ins from local database
  - Used by the web UI to show recent entries

- DELETE /api/weight/{id}
  - Deletes a weigh-in from local database (mistakes happen)
  - Note: cannot delete from Garmin, just local record

Web UI (templates/index.html):
- Mobile-first PWA, installable to Android home screen
- Clean, minimal design — dark theme
- Large numeric input field that triggers the phone number keyboard (inputmode="decimal")
- Big submit button
- Below the input: list of last 5 weigh-ins with date and weight
- Success/error feedback via a toast notification, not a page reload
- manifest.json and service worker for PWA "Add to Home Screen" support
- Viewport meta tag for mobile
- No JavaScript framework — vanilla JS with fetch() is fine

The garmin_client.py should implement a push_weight(grams: int, timestamp: datetime) method that uses garth to post the weigh-in to Garmin Connect. Research the correct garth API call for this — it should use the Garmin Connect weight endpoint.
```

---

## Phase 2: Health Dashboard — Data Collection

```
Implement the data collection layer for the vitalforge-dashboard service.

In shared/garmin_client.py, add methods to pull the following from Garmin Connect using garth:
- Daily sleep data (duration, sleep stages, SpO2, respiration, sleep score)
- Resting heart rate (daily)
- HRV status (daily, from overnight measurement)
- Body Battery (daily summary — charged/drained values)
- Stress (daily average)
- VO2 Max (latest estimate)
- Weight history (from Garmin, to supplement local records)
- Body fat % (if available)
- Training load / intensity minutes (weekly)
- Daily step count
- Active calories

Create a data sync module in vitalforge-dashboard/sync.py that:
- Pulls data from Garmin for a specified date range
- Stores everything in SQLite with proper schema (one table per metric type)
- Handles incremental sync — only fetches data we don't already have
- Can do an initial backfill of the last 90 days
- Runs on a schedule (configurable, default every 2 hours)
- Logs sync status and any errors

Database tables should store raw values with dates so we can compute trends at query time.

Add API endpoints:
- POST /api/sync — trigger a manual sync
- GET /api/sync/status — last sync time and result
- GET /api/metrics/{metric_name}?days=30 — returns time series data for any metric
  - metric_name: sleep_duration, sleep_score, resting_hr, hrv, body_battery, stress, vo2max, weight, body_fat, training_load, steps, active_calories
  - Response includes raw values and computed 7-day moving average

Use garth's API endpoints. The key garth calls are typically:
- garth.connectapi(f"/wellness-service/wellness/dailySleepData/{display_name}?date={date}")
- garth.connectapi(f"/usersummary-service/usersummary/daily/{display_name}?calendarDate={date}")  
- And similar patterns for other metrics

Research the actual Garmin Connect API paths that garth can access for each metric.
```

---

## Phase 3: Health Dashboard — Visualization

```
Build the health dashboard web UI in vitalforge-dashboard/templates/index.html.

Design principles:
- Mobile-first PWA, same as vitalforge-weight
- Dark theme, clean and minimal
- Focus on TRENDS, not daily numbers
- Default view is 30 days, toggleable to 7/30/90 days

Layout (single scrollable page):
1. Header with last sync time and manual sync button
2. Time range toggle: 7d / 30d / 90d

3. Top Cards Row (key numbers with trend arrows):
   - Current weight + trend direction
   - Latest VO2 Max
   - Average RHR this week vs last week
   - HRV status (current 7-day avg vs baseline)

4. Sleep Section:
   - Line chart: sleep duration over time with 7-day moving average
   - Sleep score trend line

5. Heart & Recovery Section:
   - Line chart: RHR trend
   - Line chart: HRV trend  
   - Body Battery daily range (high/low) as an area chart

6. Stress Section:
   - Line chart: daily average stress with 7-day moving average

7. Body Composition Section:
   - Line chart: weight trend with 7-day moving average
   - Body fat % trend (if data exists)

8. Activity Section:
   - Bar chart: daily steps
   - Training load / intensity minutes (weekly bars)

Charts:
- Use Chart.js loaded from CDN
- Consistent color scheme across all charts
- Show the moving average as a smooth line overlaid on raw data points
- Responsive — works on phone and desktop
- Minimal axis labels, no clutter

Deviation Alerts:
- At the top of the page, below the time toggle, show alert cards when a metric deviates significantly from the user's personal baseline (e.g., RHR 10% above 30-day average, HRV dropped below baseline for 3+ days, sleep duration below 6hrs for 3+ consecutive days)
- Alert cards should be yellow/orange, dismissable
- Define baseline as the 30-day moving average

Use vanilla JS with fetch() calls to the /api/metrics endpoints. No framework needed.
```

---

## Phase 4: Recommendations Engine

```
Add an AI-powered health recommendations feature to the health dashboard.

Create vitalforge-dashboard/recommendations.py with a hybrid rules + LLM engine:

RULES ENGINE:
Define pattern detection rules that analyze the metric data. Each rule outputs a finding with severity (info/warning/alert) and category. Rules include:

Sleep:
- Sleep duration below 7hrs for 3+ consecutive days → warning
- Sleep duration trending down over 2 weeks → warning
- Sleep score below 70 for 3+ days → warning

Recovery:
- HRV below personal 30-day baseline for 3+ days → warning
- HRV dropped more than 15% week-over-week → alert
- Resting HR elevated 10%+ above 30-day average → warning
- Resting HR trending up over 2 weeks → warning
- Body Battery not recovering above 80 for 3+ days → warning

Stress:
- Daily average stress above 50 for 3+ days → warning
- Stress baseline trending up over 2 weeks → warning

Body Composition:
- Weight plateau (less than 0.5lb change over 3+ weeks with active training) → info
- Weight gain rate exceeding 2lbs/week → warning
- No weight data logged in 7+ days → info reminder

Activity:
- Steps below 7000 daily average for the week → info
- No high-intensity workouts in 7+ days → info
- Training load significantly above recent average → warning (overtraining risk)
- All workouts same intensity (no zone 2 or no high intensity variety) → info
- VO2 Max declining → warning

Correlations:
- Poor sleep + elevated RHR + low HRV → alert: recovery deficit
- High training load + declining HRV + elevated RHR → alert: overtraining risk
- Weight plateau + high training load → info: possible underfueling
- Low steps + sedentary stress pattern → info: increase daily movement

LLM LAYER:
- Take the triggered rules/findings and the relevant metric data
- Send to Claude API (claude-sonnet-4-20250514) with a system prompt that positions it as a knowledgeable fitness and health coach
- The system prompt should include context: "You are analyzing health data from a Garmin Fenix 7X user. Provide specific, actionable recommendations based on the patterns detected. Be direct and practical. Reference specific numbers from their data. Suggest concrete changes to training, sleep habits, nutrition, or lifestyle. Keep recommendations to 3-5 items, prioritized by impact."
- The user message should include: the triggered findings, the relevant metric summaries (7-day and 30-day averages for each metric), and recent trends
- Parse Claude's response and display as recommendation cards

API Endpoints:
- GET /api/recommendations
  - Runs the rules engine against current data
  - Sends findings to Claude API
  - Returns structured recommendations
  - Cache the result for 6 hours (don't re-run on every page load)
  - Include a "refresh" option to force regeneration

- GET /api/recommendations/rules-only
  - Returns just the rules engine output without the LLM call (for debugging or if API is down)

UI Integration:
- Add a "Recommendations" section at the top of the dashboard, below the deviation alerts
- Show 3-5 recommendation cards
- Each card has: a title, the recommendation text, which metrics drove it, and a severity indicator
- "Last updated X hours ago" with a refresh button
- If Claude API is unavailable, fall back to displaying the raw rules engine findings

Environment:
- ANTHROPIC_API_KEY from .env
- Use the anthropic Python SDK
```

---

## Phase 5: Polish & Integration

```
Final polish pass on the vitalforge project:

1. Unified navigation:
   - Add a simple nav bar to both PWAs that links between vitalforge-weight and vitalforge-dashboard
   - Consistent look and feel between both apps

2. Push-weight → Dashboard integration:
   - After a successful weigh-in on vitalforge-weight, show the weight trend mini-chart from the last 30 days
   - This motivates consistent logging

3. Error handling:
   - All API endpoints return proper error JSON with status codes
   - Garmin auth failures trigger a clear "re-authenticate" message
   - Network errors show user-friendly messages in the UI
   - If Garmin API is rate-limited, queue and retry

4. Docker hardening:
   - Health checks on all containers
   - Proper logging to stdout for Docker logs
   - Non-root user in Dockerfiles
   - Pin Python and dependency versions

5. README.md:
   - Clear setup instructions
   - .env configuration guide
   - nginx configuration examples for custom domains
   - Tasker HTTP POST configuration example for vitalforge-weight
   - Screenshots or description of the UI

6. Tasker Integration Docs:
   - Document how to create a Tasker task:
     - Action 1: Input Dialog (title: "Weight", input type: decimal)
     - Action 2: HTTP Request (POST to https://weight.yourdomain/api/weight, body: {"weight": %input})
     - Action 3: Flash notification with response
   - How to add as home screen widget

7. Optional NFC tag setup:
   - Document how to set up an NFC tag trigger for the Tasker weight-push task
```

---

## Running Order

Execute these prompts in Claude Code in order. After each phase:
1. Build and test the Docker containers: `docker-compose up --build`
2. Verify endpoints work: `curl http://localhost:8085/health`
3. Fix any issues before moving to the next phase

Start with Phase 0. Once scaffolding is solid, proceed through each phase sequentially.
