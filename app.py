import os, time, base64, hashlib, secrets, asyncio
from typing import Optional, Dict, Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

# --- BEGIN PATCH: proxy headers safe import ---
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware  # Preferred (Starlette >= 0.14)
except Exception:  # Starlette too old or module missing
    try:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # Fallback
    except Exception:
        class ProxyHeadersMiddleware:  # No-op shim
            def __init__(self, app, **kwargs):
                self.app = app
            async def __call__(self, scope, receive, send):
                await self.app(scope, receive, send)
# --- END PATCH ---
import httpx

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
# Avoid passing None to servers param; let FastAPI set default
if PUBLIC_BASE_URL:
    app = FastAPI(title="Planning Center Connector (OAuth+JSONAPI)", servers=[{"url": PUBLIC_BASE_URL}])
else:
    app = FastAPI(title="Planning Center Connector (OAuth+JSONAPI)")

app.add_middleware(ProxyHeadersMiddleware)
origins = (os.getenv("CORS_ORIGINS") or "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in origins], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "change-me")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax", https_only=True)

PCO_CLIENT_ID = os.getenv("PCO_CLIENT_ID")
PCO_CLIENT_SECRET = os.getenv("PCO_CLIENT_SECRET")
PCO_REDIRECT_URI = os.getenv("PCO_REDIRECT_URI")
PCO_SCOPES = os.getenv("PCO_SCOPES", "people services")

AUTH_URL = "https://api.planningcenteronline.com/oauth/authorize"
TOKEN_URL = "https://api.planningcenteronline.com/oauth/token"

DEFAULT_SERVICE_TYPE_ID = os.getenv("DEFAULT_SERVICE_TYPE_ID")
DEFAULT_SERVICE_TYPE_NAME = os.getenv("DEFAULT_SERVICE_TYPE_NAME")

TOKEN_STORE: Dict[str, Dict[str, Any]] = {}

def tenant_key_from_request(request: Request) -> str:
    return "default"

def jsonapi_headers_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json"}

async def pco_get(url: str, headers: dict, params: Optional[dict] = None, max_retries: int = 3):
    attempt = 0
    async with httpx.AsyncClient(timeout=25) as client:
        while True:
            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 429 or attempt >= max_retries:
                return r
            retry_after = int(r.headers.get("Retry-After", "1"))
            await asyncio.sleep(retry_after)
            attempt += 1

async def exchange_code_for_token(code: str, code_verifier: Optional[str] = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": PCO_REDIRECT_URI,
                "client_id": PCO_CLIENT_ID, "client_secret": PCO_CLIENT_SECRET}
        if code_verifier: form["code_verifier"] = code_verifier
        r = await client.post(TOKEN_URL, data=form)
        if r.status_code == 200: return r.json()
        basic = base64.b64encode(f"{PCO_CLIENT_ID}:{PCO_CLIENT_SECRET}".encode()).decode()
        form2 = {"grant_type": "authorization_code", "code": code, "redirect_uri": PCO_REDIRECT_URI}
        if code_verifier: form2["code_verifier"] = code_verifier
        r2 = await client.post(TOKEN_URL, data=form2, headers={"Authorization": f"Basic {basic}"})
        if r2.status_code == 200: return r2.json()
        return {"error": "token_exchange_failed", "status": r2.status_code, "body": r2.text}

async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": PCO_CLIENT_ID, "client_secret": PCO_CLIENT_SECRET}
        r = await client.post(TOKEN_URL, data=data); r.raise_for_status(); return r.json()

async def get_valid_access_token(tkey: str) -> str:
    entry = TOKEN_STORE.get(tkey)
    if not entry: raise HTTPException(status_code=401, detail="Not connected to Planning Center. Visit /connect.")
    if entry.get("expires_at") and entry["expires_at"] - time.time() < 60 and entry.get("refresh_token"):
        newt = await refresh_access_token(entry["refresh_token"])
        entry["access_token"] = newt["access_token"]
        entry["refresh_token"] = newt.get("refresh_token", entry["refresh_token"])
        entry["expires_at"] = time.time() + int(newt.get("expires_in", 3600))
        TOKEN_STORE[tkey] = entry
    return entry["access_token"]

@app.get("/health")
def health(): return {"ok": True}

@app.get("/openapi-chatgpt.json")
def openapi_chatgpt(request: Request):
    spec = app.openapi()
    base_url = os.getenv("PUBLIC_BASE_URL")
    if not base_url:
        base_url = str(request.base_url).rstrip("/")
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[len("http://"):]
    spec["servers"] = [{"url": base_url}]
    return spec

@app.get("/connect")
def connect_to_planning_center(request: Request):
    if not (PCO_CLIENT_ID and PCO_REDIRECT_URI):
        raise HTTPException(status_code=500, detail="OAuth not configured on server. Check env.")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    request.session["pkce_verifier"] = code_verifier
    params = {"client_id": PCO_CLIENT_ID, "redirect_uri": PCO_REDIRECT_URI, "response_type": "code", "scope": PCO_SCOPES,
              "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256"}
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}")

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = Query(...), state: Optional[str] = None, error: Optional[str] = None):
    if error: raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if request.session.get("oauth_state") != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state. Start over at /connect.")
    code_verifier = request.session.get("pkce_verifier")
    token_payload = await exchange_code_for_token(code, code_verifier=code_verifier)
    if "error" in token_payload:
        raise HTTPException(status_code=token_payload.get("status", 500), detail={"message": token_payload["error"], "upstream": token_payload.get("body")})
    tkey = tenant_key_from_request(request)
    TOKEN_STORE[tkey] = {"access_token": token_payload["access_token"], "refresh_token": token_payload.get("refresh_token"),
                         "expires_at": time.time() + int(token_payload.get("expires_in", 3600))}
    request.session.pop("oauth_state", None); request.session.pop("pkce_verifier", None)
    return {"connected": True, "tenant": tkey, "expires_in": token_payload.get("expires_in"), "has_refresh": bool(token_payload.get("refresh_token"))}

# Helpers for Services
async def _fetch_service_types(headers: dict, page_size: int = 50, max_pages: int = 5):
    url = "https://api.planningcenteronline.com/services/v2/service_types"
    params = {"page[size]": min(max(page_size, 1), 100)}
    items = []
    pages = 0
    while url and pages < max_pages:
        r = await pco_get(url, headers, params if pages == 0 else None)
        if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail=r.text)
        payload = r.json(); items.extend(payload.get("data", []))
        url = (payload.get("links") or {}).get("next"); pages += 1
    return items

def _normalize_service_type(item: dict):
    attrs = item.get("attributes", {}) if item else {}
    return {"id": item.get("id"), "name": attrs.get("name"), "folder_name": attrs.get("folder_name"), "sequence": attrs.get("sequence")}

def _best_name_matches(items, query: Optional[str]):
    q = (query or "").strip().lower(); scored = []
    for it in items:
        name = (it.get("attributes", {}) or {}).get("name") or ""
        nlow = name.lower(); score = 0
        if nlow == q: score = 3
        elif nlow.startswith(q): score = 2
        elif q in nlow: score = 1
        if score > 0: scored.append((score, it))
    scored.sort(key=lambda t: (-t[0], ((t[1].get('attributes') or {}).get('sequence') or 99999)))
    return [s[1] for s in scored]

async def _resolve_default_service_type_id(headers: dict) -> Optional[str]:
    if DEFAULT_SERVICE_TYPE_ID: return DEFAULT_SERVICE_TYPE_ID
    if DEFAULT_SERVICE_TYPE_NAME:
        items = await _fetch_service_types(headers, page_size=100, max_pages=5)
        matches = _best_name_matches(items, DEFAULT_SERVICE_TYPE_NAME)
        if matches: return matches[0].get("id")
    return None

# People
@app.get("/pco/people/find")
async def find_person(request: Request, name: str = Query(..., description="Full or partial name"),
                      page_size: int = Query(5, ge=1, le=100), **fields):
    token = await get_valid_access_token(tenant_key_from_request(request))
    headers = jsonapi_headers_bearer(token)
    params = {"where[name]": name, "include": "emails,phone_numbers", "page[size]": page_size}
    for k, v in fields.items():
        if k.startswith("fields[") and v: params[k] = v
    r = await pco_get("https://api.planningcenteronline.com/people/v2/people", headers, params)
    if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json(); included = {f"{i.get('type')}:{i.get('id')}": i for i in data.get("included", [])} if data.get("included") else {}
    results = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {}); rel = item.get("relationships", {})
        emails, phones = [], []
        if rel.get("emails", {}).get("data"):
            for ref in rel["emails"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}"); addr = (inc.get("attributes") or {}).get("address") if inc else None
                if addr: emails.append(addr)
        if rel.get("phone_numbers", {}).get("data"):
            for ref in rel["phone_numbers"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}"); num = (inc.get("attributes") or {}).get("number") if inc else None
                if num: phones.append(num)
        results.append({"id": item.get("id"), "name": attrs.get("name"),
                        "first_name": attrs.get("first_name"), "last_name": attrs.get("last_name"),
                        "emails": emails, "phones": phones})
    return {"count": len(results), "people": results}

# Services: Service Types
@app.get("/pco/services/service-types")
async def list_service_types(request: Request, page_size: int = Query(50, ge=1, le=100), max_pages: int = Query(5, ge=1, le=20)):
    token = await get_valid_access_token(tenant_key_from_request(request))
    headers = jsonapi_headers_bearer(token)
    items = await _fetch_service_types(headers, page_size=page_size, max_pages=max_pages)
    return {"count": len(items), "service_types": [_normalize_service_type(i) for i in items]}

@app.get("/pco/services/service-types/resolve")
async def resolve_service_type(request: Request, query: str = Query(...), page_size: int = Query(50, ge=1, le=100), max_pages: int = Query(5, ge=1, le=20)):
    token = await get_valid_access_token(tenant_key_from_request(request))
    headers = jsonapi_headers_bearer(token)
    items = await _fetch_service_types(headers, page_size=page_size, max_pages=max_pages)
    matches = _best_name_matches(items, query)
    out = [_normalize_service_type(m) for m in matches]
    return {"query": query, "matches": out, "count": len(out)}

# Aliases
@app.get("/pco/services/types")
async def list_types_alias(request: Request, page_size: int = Query(50, ge=1, le=100), max_pages: int = Query(5, ge=1, le=20)):
    return await list_service_types(request, page_size=page_size, max_pages=max_pages)

@app.get("/pco/services/types/resolve")
async def resolve_types_alias(request: Request, query: str = Query(...), page_size: int = Query(50, ge=1, le=100), max_pages: int = Query(5, ge=1, le=20)):
    return await resolve_service_type(request, query=query, page_size=page_size, max_pages=max_pages)

# Services: Plans & Plan Detail
@app.get("/pco/services/plans")
async def services_plans(request: Request, service_type_id: Optional[str] = Query(None), service_type_name: Optional[str] = Query(None),
                         page_size: int = Query(10, ge=1, le=100), include: str = Query("plan_times,needed_positions,team_members"), **fields):
    token = await get_valid_access_token(tenant_key_from_request(request))
    headers = jsonapi_headers_bearer(token)
    use_id = service_type_id
    if not use_id and service_type_name:
        items = await _fetch_service_types(headers, page_size=100, max_pages=5)
        matches = _best_name_matches(items, service_type_name)
        if not matches: raise HTTPException(status_code=404, detail=f"No service type matched '{service_type_name}'.")
        use_id = matches[0].get("id")
    if not use_id: use_id = await _resolve_default_service_type_id(headers)
    if not use_id: raise HTTPException(status_code=422, detail="Provide service_type_id or service_type_name, or set defaults via env.")
    base = f"https://api.planningcenteronline.com/services/v2/service_types/{use_id}/plans"
    params = {"include": include, "page[size]": page_size}
    for k, v in fields.items():
        if k.startswith("fields[") and v: params[k] = v
    r = await pco_get(base, headers, params)
    if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json(); included = {f"{i.get('type')}:{i.get('id')}": i for i in data.get("included", [])} if data.get("included") else {}
    plans_out = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {}); rel = item.get("relationships", {})
        times, needed_positions = [], []
        if rel.get("plan_times", {}).get("data"):
            for ref in rel["plan_times"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}"); tattrs = (inc.get("attributes") or {}) if inc else {}
                times.append({"starts_at": tattrs.get("starts_at"), "ends_at": tattrs.get("ends_at"), "name": tattrs.get("name")})
        if rel.get("needed_positions", {}).get("data"):
            for ref in rel["needed_positions"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}"); nattrs = (inc.get("attributes") or {}) if inc else {}
                needed_positions.append({"team_position_name": nattrs.get("team_position_name"),
                                         "quantity": nattrs.get("quantity"), "assigned_count": nattrs.get("assigned_count")})
        plans_out.append({"id": item.get("id"), "dates": attrs.get("sort_date") or attrs.get("dates"),
                          "title": attrs.get("title"), "series_title": attrs.get("series_title"),
                          "times": times, "needed_positions": needed_positions})
    return {"count": len(plans_out), "plans": plans_out}

@app.get("/pco/services/plan")
async def services_plan_detail(request: Request, plan_id: str = Query(...), include: str = Query("plan_times,needed_positions,team_members,team_members.person"), **fields):
    token = await get_valid_access_token(tenant_key_from_request(request))
    headers = jsonapi_headers_bearer(token)
    base = f"https://api.planningcenteronline.com/services/v2/plans/{plan_id}"
    params = {"include": include}
    for k, v in fields.items():
        if k.startswith("fields[") and v: params[k] = v
    r = await pco_get(base, headers, params)
    if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()
