<!-- Generated: 2026-08-22 | Files scanned: 24 | Token estimate: ~570 -->
# Frontend

No build step, no JS package manager. Each service is a single server-rendered Jinja2
page plus hand-written vanilla JS in an inline `<script>` block. Chart.js is pulled from
the jsDelivr CDN. Each service is also an installable PWA (manifest + service worker).

## Page tree

```
vitalforge-weight/templates/index.html        (440 lines, single page)
  nav: "Weight" (active, only link)
  input-group: numeric weight input + lbs/kg unit-toggle buttons
  submit-btn -> POST /api/weight (sends `source: "pwa"`; no composition inputs in the
                form yet — those fields are API-only so far, populated by non-PWA
                clients like the Bascule Android app)
  recent-list  <- GET /api/weight/recent
  trend-section (canvas#trendChart, Chart.js) <- GET /api/weight/trend
  toast (transient success/error message)

vitalforge-dashboard/templates/index.html      (638 lines, single page)
  nav: "Dashboard" (active, only link)
  header-right: syncInfo <- GET /api/sync/status, Sync button -> POST /api/sync,
                unit-toggle (lbs/kg), range-toggle (7d/30d/90d)
  recs-section  <- GET /api/recommendations (+ Refresh -> ?refresh=true)
  alerts        <- populated from recommendations/rules findings
  top-cards     (Weight, VO2 Max, ... latest-value tiles)
  per-metric chart sections, one canvas each <- GET /api/metrics/{name}?days=
```

## State management

No frontend framework/store — plain DOM manipulation + `fetch()` calls triggered by
button `onclick` handlers and an initial page-load bootstrap script. State lives in a
handful of top-level `let`/`const` bindings per page (e.g. `trendChart`, current unit,
current day-range) and is not persisted client-side beyond the page session.

## Static assets (per service, `static/`)

- `manifest.json` — PWA manifest (name, icons, theme color)
- `icon-192.png` — app icon
- `sw.js` — service worker (~14 lines, minimal cache-first shell)
- served via FastAPI `StaticFiles` mount at `/static`, exempted from the auth middleware

## Server-side templating conventions

- Both `index()` routes inject `default_unit` (`DEFAULT_UNIT` env, default `"lbs"`) and
  `tz` (`TZ` env) into the template context; weight service also injects `dashboard_url`,
  dashboard service injects `weight_url` — used for cross-service nav links when deployed
  behind the optional `nginx/nginx.conf` subdomain split.
- No shared base template / includes between the two services — each `index.html` is
  fully self-contained (own `<style>` block, own inline `<script>`).
