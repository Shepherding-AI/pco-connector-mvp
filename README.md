
# Planning Center Connector (MVP)

A tiny FastAPI service that lets ChatGPT fetch **People** and **Services** info from Planning Center (PCO) using a Personal Access Token (PAT).

## What it does
- `GET /pco/people/find?name=Jane` → finds people and returns name + emails + phones.
- `GET /pco/services/plans?service_type_id=...` → lists plans for a service type (with times & needed positions).
- `GET /pco/services/plan?plan_id=...` → a single plan with common includes (useful for "who's scheduled").

> This MVP uses **Basic Auth** with your PCO Personal Access Token (APP ID + SECRET). Keep scopes read-only for testing.

## Quick start (Railway)
1. Create a new project on Railway and deploy this repo.
2. Add environment variables:
   - `PCO_APP_ID` = your Planning Center Personal Access Token App ID
   - `PCO_SECRET` = your Planning Center Personal Access Token Secret
3. (Optional) `CORS_ORIGINS=*` for quick tests.
4. Once deployed, visit:
   - `/health` → `{"ok": true}`
   - `/pco/people/find?name=Smith`
   - `/pco/services/plans?service_type_id=<your_service_type_id>`

## Local run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PCO_APP_ID=xxx
export PCO_SECRET=yyy
uvicorn app:app --reload
```
Open: `http://127.0.0.1:8000/health`

## ChatGPT Action (manual function)
Create a function named **findPerson**:
- Parameters: `{ "type": "object", "properties": { "name": {"type":"string"} }, "required":["name"] }`
- When called, perform `GET https://YOUR-APP-URL/pco/people/find?name={name}` and return the JSON.

Add more functions pointing to the Services endpoints as needed.

## Notes
- This is read-only. Do not include secrets in code; use host environment variables.
- For multi-church use, upgrade to OAuth 2.0 (Authorization Code) later.
- Services date filters vary by account; this MVP returns recent plans and includes common related entities for convenience.
