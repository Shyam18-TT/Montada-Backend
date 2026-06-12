# Montada — Django Backend

Fintech trading platform connecting traders with analysts. Provides REST APIs and WebSocket channels consumed by a Flutter mobile app (in a separate repository).

---

## Project Layout

```
Montada/                  ← Django project root (run all commands from here)
├── Montada/              ← Project config (settings.py, urls.py, asgi.py, wsgi.py)
├── Mainapp/              ← Users, auth, OTP, notifications, device tokens
├── Subscriptions/        ← Billing: in-app market data + per-analyst content plans
├── Signals/              ← Trading signals, price alerts, MT5 price monitoring
├── Followers/            ← Social graph: follow/block/mute, analyst reviews
├── Dashboard/            ← Analyst stats, polls, analytics
├── MontadaAdmin/         ← Admin-only endpoints (70+)
├── News/                 ← Articles, economic calendar, live news aggregation
├── chat/                 ← Real-time 1-1 messaging via WebSocket
├── bases/                ← Shared base classes/utilities
├── credentials/          ← Firebase service account JSON
├── logs/                 ← App + PM2 logs (git-ignored)
├── media/                ← User uploads (git-ignored)
├── firebase.py           ← FCM push notification helpers
├── ecosystem.config.cjs  ← PM2 multi-process configuration
├── requirements.txt
└── manage.py
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Framework | Django 5.0.6 + Django REST Framework 3.15.2 |
| WebSocket | Django Channels 4.0.0 + Daphne 4.0.0 (ASGI) |
| Primary DB | MS SQL Server — `MontadaApp` (localhost:1433) |
| Secondary DB | MS SQL Server — `METATRADER5` on `213.175.205.19:1433` (read-only MT5 broker data) |
| Cache / Channel Layer | Redis at `127.0.0.1:6379` |
| Authentication | JWT via `djangorestframework-simplejwt` (Bearer token, 365-day lifetime) |
| Push Notifications | Firebase Cloud Messaging (FCM) via `firebase-admin` |
| Payments | Stripe (payment_intent_id tracked on Subscription model) |
| Process Manager | PM2 (`ecosystem.config.cjs`) — multiple Daphne ASGI workers |
| Email | Gmail SMTP (`montadaapp129@gmail.com`, port 587 TLS) |

---

## Running the Dev Server

```bash
cd Montada

# Activate virtual environment (adjust path as needed)
..\env\Scripts\activate         # Windows
source ../env/bin/activate      # Unix/macOS

# Apply migrations
python manage.py migrate

# Run development server (HTTP + WebSocket via Daphne)
python manage.py runserver

# Or run via Daphne directly
python -m daphne -b 0.0.0.0 -p 8000 Montada.asgi:application
```

**Prerequisites:** Redis must be running on `127.0.0.1:6379` and MS SQL Server on `127.0.0.1:1433`.

### PM2 (Production/Staging)

```bash
# From Montada/ directory
pm2 start ecosystem.config.cjs   # Start all workers
pm2 logs                          # Stream logs
pm2 restart all                   # Restart workers

# Env overrides
WEB_CONCURRENCY=4 PORT=8000 pm2 start ecosystem.config.cjs
```

---

## Database

Two MSSQL databases configured in `settings.py`:

- **`default`** → `MontadaApp` (localhost) — all business logic
- **`mt5clients`** → `METATRADER5` (remote broker at `213.175.205.19`) — read-only live price data for signal monitoring

**MSSQL syntax notes:** Use `TOP N` not `LIMIT N`. No `RETURNING` clause — use `save()` then access the PK. The `mssql-django` backend handles most queryset translation automatically.

```bash
python manage.py migrate                          # default DB
python manage.py migrate --database=mt5clients    # secondary DB (rarely needed)
```

---

## Authentication

JWT Bearer tokens. All endpoints require `Authorization: Bearer <access_token>` except registration, login, OTP verification, and password reset.

- **Access token lifetime:** 365 days
- **Refresh token lifetime:** 365 days, rotated on use (old token blacklisted)
- **User ID field:** `user_id` claim (UUID)

OTP flows (6-digit codes, sent via email):
- Email verification on registration
- Password reset
- Account deletion confirmation

---

## API URL Prefixes

| Prefix | App |
|--------|-----|
| `/api/auth/` | Mainapp |
| `/api/subscriptions/` | Subscriptions |
| `/api/signals/` | Signals |
| `/api/followers/` | Followers |
| `/api/dashboard/` | Dashboard |
| `/api/admin/` | MontadaAdmin |
| `/api/news/` | News |
| `/api/chat/` | chat |
| `/admin/` | Django built-in admin |

---

## WebSocket Endpoints

All WS connections require a valid JWT passed as a query param or during handshake.

| Path | Consumer | Purpose |
|------|----------|---------|
| `/ws/chat/<conversation_id>/` | `ChatConsumer` | Real-time 1-1 messaging |
| `/ws/chat/notifications/` | `ChatNotificationConsumer` | New message alerts |
| `/ws/news/live/` | `LiveNewsConsumer` | Live news stream (Benzinga) |
| `/ws/dashboard/notifications/` | `NotificationConsumer` | In-app notification push |
| `/ws/signals/market-data/` | `MarketDataConsumer` | Live price + signal status updates |

Routing is registered in `Montada/asgi.py`, pulling from each app's `routing.py`.

---

## Background Management Commands

Run from the `Montada/` directory with the virtualenv active:

```bash
# Monitor signals vs live MT5 prices; close signals at TP/SL and notify users
python manage.py run_price_alerts
python manage.py run_price_alerts --use-mt5-manager   # via MT5 Manager API
python manage.py run_price_alerts --use-mt5-db         # via mt5clients DB

# Send FCM/in-app reminders for upcoming economic calendar events
python manage.py run_economic_calendar_reminders
python manage.py run_economic_calendar_reminders --background

# Fetch economic calendar events from Tradays
python manage.py fetch_economic_calendar

# Poll for signal status changes and notify followers
python manage.py poll_signal_change_notifications

# Stream live market data to WebSocket clients
python manage.py run_market_data_stream

# Stream Benzinga live news to WebSocket clients
python manage.py run_fxstreet_news_stream
```

---

## User Model

Custom model at `Mainapp.User` (UUID primary key, extends `AbstractUser`).

**Key fields:**
- `user_type` — `'trader'` or `'analyst'`
- `is_verified` — email verified via OTP
- `is_subscribed` — has active market data subscription
- `free_trial_eligible` — can receive first free trial
- `admin_granted_in_app_access` / `admin_in_app_access_expires_at` — admin bypass
- `is_soft_deleted` / `soft_deleted_at` — soft delete (no hard deletes)
- Analyst-only: `experience`, `company`, `contact_details`, `social_links`
- Notification prefs: `news_notify_ar`, `news_notify_en`, `news_notify_zh`

---

## Subscription System

Two separate subscription tiers:

1. **In-App Subscription** (`Subscriptions.Subscription`) — Grants access to market news and live data. Billed via Stripe. Has free trial support.
2. **Analyst Content Plans** (`Subscriptions.AnalystContentPlan`) — Per-analyst offerings (articles, signals, or both). Traders pay analysts directly.

**Feature flags in `settings.py`:**
- `MARKET_NEWS_AND_DATA_FREE_ACCESS` — when `True`, skips paywall for market data (default: `True`)
- `ANALYST_SUBSCRIPTION_FREE_ACCESS` — when `True`, skips paywall for analyst content (default: `True`)

Override via environment variable: `MARKET_NEWS_AND_DATA_FREE_ACCESS=false`.

---

## Push Notifications (FCM)

Helper module: `firebase.py`

```python
from firebase import send_push_to_tokens, send_push_to_users

send_push_to_tokens(tokens, title, body, data={}, image_url=None)
send_push_to_users(user_queryset, title, body, data={}, image_url=None)
```

- Device tokens stored in `Mainapp.DeviceToken` (deduplicated by `device_id`)
- Multicast chunk size: 500 (FCM limit)
- Supports Android (`AndroidConfig`) and iOS (`APNsConfig`)
- FCM credentials: `credentials/montada-86ba6-firebase-adminsdk-fbsvc-8df57cd800.json`

---

## Third-Party API Keys (in `settings.py`)

| Setting | Service | Purpose |
|---------|---------|---------|
| `MARKETAUX_API_TOKEN` | Marketaux | Market/finance news |
| `FOREXNEWS_API_TOKEN` | ForexNewsAPI | Forex events + trending headlines |
| `EODHD_API_TOKEN` | EODHD | Category financial news |
| `BENZINGA_API_TOKEN` | Benzinga | Live news REST + WebSocket stream |
| `MT5_MANAGER_SERVER/LOGIN/PASSWORD` | MT5 Manager API | Optional; live price source for signal monitoring |

All API tokens can be overridden via environment variables (see `settings.py`).

---

## Logging

Logs written to `Montada/logs/` (auto-created):

| File | Content |
|------|---------|
| `django_db.log` | DB query warnings |
| `exception.log` | Request/server errors |
| `pm2-montada-<N>-out.log` | PM2 stdout per worker |
| `pm2-montada-<N>-error.log` | PM2 stderr per worker |

---

## Common Patterns

**Soft deletes** — never hard-delete Users, Signals, or Messages. Use `is_soft_deleted=True` / `deleted_at=now()` and filter active records with `.filter(deleted_at__isnull=True)` or `.filter(is_soft_deleted=False)`.

**Pagination** — default page size is 20 (`PAGE_SIZE` in `settings.py`). All list views use `PageNumberPagination`.

**Permissions** — default is `IsAuthenticated`. Admin endpoints enforce custom `IsAdminUser` or `IsMontadaAdmin` permission classes. Analyst-only endpoints check `request.user.user_type == 'analyst'`.

**Media files** — uploaded to `MEDIA_ROOT` (`Montada/media/`). Absolute URLs use `PUBLIC_MEDIA_BASE_URL = 'https://api.themontada.com'`. In development, media is served via Django's static file server when `DEBUG=True`.

**Database routing** — `mt5clients` DB is accessed by specifying `.using('mt5clients')` on querysets or via the `DATABASE_ROUTERS` setting if configured.

---

## Environment Variables

These override hardcoded defaults in `settings.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MARKET_NEWS_AND_DATA_FREE_ACCESS` | `true` | Disable market data paywall |
| `BENZINGA_API_TOKEN` | (hardcoded) | Benzinga API key |
| `BENZINGA_NEWS_URL` | (hardcoded) | Benzinga REST URL |
| `BENZINGA_NEWS_STREAM_URL` | (hardcoded) | Benzinga WebSocket URL |
| `BENZINGA_NEWS_DEFAULT_PAGE_SIZE` | `50` | News page size |
| `EODHD_API_TOKEN` | (hardcoded) | EODHD API key |
| `EODHD_NEWS_URL` | (hardcoded) | EODHD endpoint |
| `MT5_MANAGER_SERVER` | `207.97.203.117:443` | MT5 Manager server |
| `MT5_MANAGER_LOGIN` | `5022` | MT5 Manager login |
| `MT5_MANAGER_PASSWORD` | (hardcoded) | MT5 Manager password |
| `WEB_CONCURRENCY` | `2` | PM2 worker count |
| `PORT` | `8000` | Base port for workers |
| `BIND_HOST` | `0.0.0.0` | Daphne bind host |
| `PYTHON_BIN` | `python`/`python3` | Python binary for PM2 |
