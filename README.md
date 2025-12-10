# Planning Center Connector (FastAPI) — Redis Enabled (Railway Ready)

This build persists OAuth tokens in Redis so your sessions survive restarts and scale across instances.

## Deploy (Railway)
1) Push these files to GitHub; deploy on Railway.
2) Add **Redis** to your Railway project (New → Database → Redis) and ensure `REDIS_URL` is present in Variables.
3) Set Variables:
   - PCO_CLIENT_ID, PCO_CLIENT_SECRET
   - PCO_REDIRECT_URI = https://YOUR-APP.up.railway.app/auth/callback
   - PCO_SCOPES = people services
   - PUBLIC_BASE_URL = https://YOUR-APP.up.railway.app
   - CORS_ORIGINS = *
   - SESSION_SECRET_KEY = (long random string)
   - REDIS_URL = (Railway-provided, or manual per above)
   - Optional: DEFAULT_SERVICE_TYPE_ID=1232778 (or DEFAULT_SERVICE_TYPE_NAME)
4) Redeploy → visit `/health` → should show `{"ok": true, "redis": true}`.
5) Run OAuth: `/connect` → approve → `/auth/callback` → you should see `{connected: true}`.
6) Import `https://YOUR-APP.up.railway.app/openapi-chatgpt.json` into GPT Actions (Auth=None).

## Endpoints
- Health: `/health` (includes `"redis": true|false`)
- Spec: `/openapi-chatgpt.json` (HTTPS-only servers)
- OAuth: `/connect`, `/auth/callback`
- People: `GET /pco/people/find?name=...`
- Services:
  - `GET /pco/services/service-types`
  - `GET /pco/services/service-types/resolve?query=...`
  - Aliases: `/pco/services/types`, `/pco/services/types/resolve`
  - `GET /pco/services/plans?service_type_id=...` or `?service_type_name=...`
  - `GET /pco/services/plan?plan_id=...`
