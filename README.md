---
title: FoodCheck - AI Food Safety Analyzer
emoji: 🍽️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# FoodCheck: AI Food Safety & Corporate Transparency Checker 🇮🇳

An AI-powered packaged food analyzer built for Indian consumers using Google Gemini AI, Open Food Facts, and real-time news cross-referencing. Includes private diet planning with Meta-Llama-3.1 on Groq Cloud, role-based authentication, and a complete administrative control panel.

---

## 🌟 Key Features

- 📷 **Real-time Packaging OCR** via Google Gemini Multimodal Vision.
- 📊 **Nutrition & Additive Safety Analysis** adhering to FSSAI standards.
- ⚠️ **Health Flags & Red Alerts** (high sugar, palm oil, hydrogenated fats, INS chemical additives).
- 🌿 **Healthier Alternatives** available in the Indian market.
- 📰 **Corporate Transparency News Feed** tracking safety recalls, FSSAI warnings, and food controversies.
- 🥗 **AI Diet Planner** (Meta-Llama-3.1-8B-Instruct via Groq Cloud) — personalized daily meal plans with macros and hydration schedules.
- 🔥 **Real-Time Calorie Tracker** — daily consumed vs target calorie meter with macronutrient splits.
- 🛡️ **Role-Based Authentication** — strict user vs admin role separation.
- 🔐 **Secure Developer-Key Admin Panel** (`/admin/*`) — full telemetry, 30-day Chart.js metrics, user controls, active session revocation, and CSV exports.
- 🎨 **Multi-Theme Support** — seamless Light, Dark, and Cyberpunk Tech themes across the entire app and admin console.

---

## 🛠️ Tech Stack

- **Backend:** Python 3.11+ / Flask 3.1 / Gunicorn
- **Security & Auth:** Flask-Login, Flask-WTF (CSRF), Flask-Limiter, Werkzeug hashing, pyotp (2FA)
- **Database ORM & Migrations:** Flask-SQLAlchemy, Flask-Migrate (Alembic)
- **AI Models:**
  - Google Gemini (multi-model fallback pool for OCR & ingredient safety)
  - Meta-Llama-3.1-8B-Instruct on Groq Cloud (ultra-fast personalized diet planning)
- **Database:** SQLite default (`instance/foodcheck.db`) with automatic PostgreSQL override via `DATABASE_URL` / `AIVEN_DATABASE_URL`
- **Frontend:** Vanilla JS, CSS Custom Properties, Chart.js CDN, responsive layouts

---

## 🔒 Security Architecture & Roles

1. **Role Separation:**
   - **User:** Public registration at `/register` and login at `/login`. Allowed to run food analyses, save personal diet profiles, and log calories.
   - **Admin:** Distinct console at `/admin/login`. Non-admins or anonymous visitors requesting `/admin/*` receive an immediate **404 Not Found** to obscure the panel's existence.
   - **Immutable Roles:** User roles can never be passed from forms or request bodies; they are strictly server-enforced.
2. **Developer Invite Key System:**
   - Public registration can never create an administrator account.
   - Admin registration lives at `/admin/register` and requires a single-use, expirable Admin Invite Key generated exclusively via CLI.
   - Keys are hashed with SHA-256 and compared using constant-time `hmac.compare_digest`.
3. **Account Protection:**
   - Accounts lock for 15 minutes after 5 consecutive failed logins.
   - Password policy: minimum 8 characters, at least one letter, and at least one number.
   - HTTPOnly, SameSite=Lax, Secure session cookies.
   - 30-minute inactivity timeout for admin sessions.
   - CSRF protection across all forms and AJAX requests.

---

## 💻 CLI Commands (Developer Key & Admin Management)

Run these commands in your project environment:

### 1. Generate a Single-Use Admin Invite Key
```bash
flask create-admin-key --expires-hours 24
```
*Prints a cryptographically random key once (`secrets.token_urlsafe(32)`). Give this key to the administrator to register at `/admin/register`.*

### 2. List All Admin Invite Keys
```bash
flask list-admin-keys
```
*Displays key IDs, active/used/expired/revoked statuses, creation/expiration dates, and claiming administrators.*

### 3. Revoke an Invite Key
```bash
flask revoke-admin-key <KEY_ID>
```

### 4. Bootstrap Initial Admin Directly (CLI)
```bash
flask create-first-admin
```
*Prompts for username, email, full name, and password to create an administrator account directly without a web key.*

### 5. Purge Old Activity & Security Logs
```bash
flask purge-old-logs --days 90
```
*Deletes activity logs, login events, and error logs older than 90 days.*

---

## 🗄️ Database & Ephemeral Disk Notice (Render / Free Hosting)

> [!IMPORTANT]
> **Ephemeral Disk Notice for Free Cloud Hosting (Render, Railway, Hugging Face):**
> By default, FoodCheck operates on a local SQLite database (`instance/foodcheck.db`). Free-tier container hosts use **ephemeral disks**, meaning any files written to local disk (including SQLite databases) are wiped whenever the service restarts, redeploys, or spins down due to inactivity.

### Using Persistent Cloud PostgreSQL (Free Tier)
To ensure users, admin keys, scans, and diet records persist permanently across deploys:
1. Spin up a free cloud PostgreSQL database on [Aiven](https://aiven.io), [Neon](https://neon.tech), or [Supabase](https://supabase.com).
2. Set the `DATABASE_URL` or `AIVEN_DATABASE_URL` environment variable:
   ```env
   DATABASE_URL=postgresql://avnadmin:PASSWORD@HOST.aivencloud.com:PORT/defaultdb?sslmode=require
   ```
3. FoodCheck automatically detects the URL, configures the connection pool, and executes database migrations via Alembic.

---

## ⚙️ Environment Variables

Configure these variables in your `.env` file or cloud dashboard:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Recommended | Random dev key | Flask session encryption key. |
| `GEMINI_API_KEY` | Yes | — | Google AI Studio API key for multimodal vision & health scoring. |
| `GROQ_API_KEY` | Optional | — | Groq Cloud API key for Meta-Llama-3.1 diet generation. |
| `GROQ_MODEL` | Optional | `llama-3.1-8b-instant` | Groq model identifier. |
| `DATABASE_URL` | Optional | SQLite | Connection string for cloud PostgreSQL. |
| `AIVEN_DATABASE_URL` | Optional | — | Alternative PostgreSQL connection string. |
| `ADMIN_2FA_ENABLED` | Optional | `false` | Set to `true` to require TOTP 2FA for admin logins. |
| `GNEWS_API_KEY` | Optional | — | GNews API key for supplementary brand news. |
| `PORT` | Optional | `5000` | Port for the web server. |

---

## 🧪 Running Automated Tests

```bash
# Run all test suites (Authentication, Isolation, Admin Controls)
python -m unittest discover tests
```