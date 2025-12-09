import os, base64
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx

APP_ID = os.getenv("PCO_APP_ID")
APP_SECRET = os.getenv("PCO_SECRET")
def auth_header():
    if not APP_ID or not APP_SECRET:
        raise RuntimeError("Missing PCO_APP_ID or PCO_SECRET environment variables.")
    basic = f"{APP_ID}:{APP_SECRET}".encode("utf-8")
    token = base64.b64encode(basic).decode("utf-8")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
servers = [{"url": PUBLIC_BASE_URL}] if PUBLIC_BASE_URL else []
app = FastAPI(title="Planning Center Connector (MVP)", servers=servers)
origins = (os.getenv("CORS_ORIGINS") or "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in origins],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

DEFAULT_SERVICE_TYPE_ID = os.getenv("DEFAULT_SERVICE_TYPE_ID")
DEFAULT_SERVICE_TYPE_NAME = os.getenv("DEFAULT_SERVICE_TYPE_NAME")

@app.get("/health")
def health(): return {"ok": True}

@app.get("/openapi-chatgpt.json")
def openapi_chatgpt(request: Request):
    spec = app.openapi()
    base_url = str(request.base_url).rstrip("/")
    spec["servers"] = [{"url": base_url}]
    return spec

from urllib.parse import urlencode
from fastapi.responses import RedirectResponse

PCO_CLIENT_ID = os.getenv("PCO_CLIENT_ID")
PCO_CLIENT_SECRET = os.getenv("PCO_CLIENT_SECRET")
PCO_REDIRECT_URI = os.getenv("PCO_REDIRECT_URI")
PCO_SCOPES = os.getenv("PCO_SCOPES", "people services")

AUTH_URL = "https://api.planningcenteronline.com/oauth/authorize"
TOKEN_URL = "https://api.planningcenteronline.com/oauth/token"

TOKEN_STORE = {}
import time, httpx
def tenant_key_from_request(request: Request) -> str: return "default"

async def exchange_code_for_token(code: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": PCO_REDIRECT_URI,
            "client_id": PCO_CLIENT_ID,
            "client_secret": PCO_CLIENT_SECRET,
        }
        r = await client.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()

@app.get("/connect")
def connect_to_planning_center():
    if not (PCO_CLIENT_ID and PCO_REDIRECT_URI):
        raise HTTPException(status_code=500, detail="OAuth not configured on server.")
    params = {
        "client_id": PCO_CLIENT_ID,
        "redirect_uri": PCO_REDIRECT_URI,
        "response_type": "code",
        "scope": PCO_SCOPES,
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}")

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = Query(...), error: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    token_payload = await exchange_code_for_token(code)
    TOKEN_STORE["default"] = {
        "access_token": token_payload["access_token"],
        "refresh_token": token_payload.get("refresh_token"),
        "expires_at": time.time() + int(token_payload.get("expires_in", 3600)),
    }
    return {"connected": True}


@app.get("/pco/people/find")
async def find_person(name: str = Query(...), page_size: int = Query(5, ge=1, le=100)):
    url = "https://api.planningcenteronline.com/people/v2/people"
    params = {"where[name]": name, "include": "emails,phone_numbers", "page[size]": page_size}
    headers = auth_header()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers, params=params)
        if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()
    included = {f"{i.get('type')}:{i.get('id')}": i for i in data.get("included", [])} if data.get("included") else {}
    results = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {}); rel = item.get("relationships", {})
        emails, phones = [], []
        if rel.get("emails", {}).get("data"):
            for ref in rel["emails"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                addr = (inc.get("attributes") or {}).get("address")
                if addr: emails.append(addr)
        if rel.get("phone_numbers", {}).get("data"):
            for ref in rel["phone_numbers"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                num = (inc.get("attributes") or {}).get("number")
                if num: phones.append(num)
        results.append({"id": item.get("id"), "name": attrs.get("name"),
                        "first_name": attrs.get("first_name"), "last_name": attrs.get("last_name"),
                        "emails": emails, "phones": phones})
    return {"count": len(results), "people": results}

async def _fetch_service_types(headers, page_size=50, max_pages=5):
    url = "https://api.planningcenteronline.com/services/v2/service_types"
    params = {"page[size]": min(max(page_size, 1), 100)}
    items = []
    async with httpx.AsyncClient(timeout=25) as client:
        pages = 0
        while url and pages < max_pages:
            r = await client.get(url, headers=headers, params=params if pages == 0 else None)
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            payload = r.json()
            items.extend(payload.get("data", []))
            links = payload.get("links", {}) or {}
            url = links.get("next")
            pages += 1
    return items

def _normalize_service_type(item):
    attrs = item.get("attributes", {}) if item else {}
    return {"id": item.get("id"), "name": attrs.get("name"),
            "folder_name": attrs.get("folder_name"), "sequence": attrs.get("sequence")}

def _best_name_matches(items, query):
    q = (query or "").strip().lower()
    scored = []
    for it in items:
        name = (it.get("attributes", {}) or {}).get("name") or ""
        nlow = name.lower(); score = 0
        if nlow == q: score = 3
        elif nlow.startswith(q): score = 2
        elif q in nlow: score = 1
        if score > 0: scored.append((score, it))
    scored.sort(key=lambda t: (-t[0], ((t[1].get('attributes') or {}).get('sequence') or 99999)))
    return [s[1] for s in scored]

async def _resolve_default_service_type_id(headers) -> Optional[str]:
    if DEFAULT_SERVICE_TYPE_ID:
        return DEFAULT_SERVICE_TYPE_ID
    if DEFAULT_SERVICE_TYPE_NAME:
        items = await _fetch_service_types(headers, page_size=100, max_pages=5)
        matches = _best_name_matches(items, DEFAULT_SERVICE_TYPE_NAME)
        if matches:
            return matches[0].get("id")
    return None

@app.get("/pco/services/default-service-type")
async def get_default_service_type():
    headers = auth_header()
    resolved = await _resolve_default_service_type_id(headers)
    return {"DEFAULT_SERVICE_TYPE_ID": DEFAULT_SERVICE_TYPE_ID,
            "DEFAULT_SERVICE_TYPE_NAME": DEFAULT_SERVICE_TYPE_NAME,
            "resolved_service_type_id": resolved}

@app.get("/pco/services/service-types")
async def list_service_types(page_size: int = Query(50, ge=1, le=100), max_pages: int = Query(5, ge=1, le=20)):
    headers = auth_header()
    items = await _fetch_service_types(headers, page_size=page_size, max_pages=max_pages)
    return {"count": len(items), "service_types": [_normalize_service_type(i) for i in items]}

@app.get("/pco/services/service-types/resolve")
async def resolve_service_type(query: str = Query(...), page_size: int = Query(50, ge=1, le=100),
                               max_pages: int = Query(5, ge=1, le=20)):
    headers = auth_header()
    items = await _fetch_service_types(headers, page_size=page_size, max_pages=max_pages)
    matches = _best_name_matches(items, query)
    out = [_normalize_service_type(m) for m in matches]
    return {"query": query, "matches": out, "count": len(out)}

# Aliases for compatibility
@app.get("/pco/services/types")
async def list_types_alias(page_size: int = Query(50, ge=1, le=100), max_pages: int = Query(5, ge=1, le=20)):
    return await list_service_types(page_size=page_size, max_pages=max_pages)

@app.get("/pco/services/types/resolve")
async def resolve_types_alias(query: str = Query(...), page_size: int = Query(50, ge=1, le=100),
                               max_pages: int = Query(5, ge=1, le=20)):
    return await resolve_service_type(query=query, page_size=page_size, max_pages=max_pages)

@app.get("/pco/services/plans")
async def services_plans(service_type_id: Optional[str] = Query(None),
                         service_type_name: Optional[str] = Query(None),
                         page_size: int = Query(10, ge=1, le=100),
                         include: str = Query("plan_times,needed_positions,team_members"),
                         from_date: Optional[str] = None, to_date: Optional[str] = None):
    headers = auth_header()
    if not service_type_id and service_type_name:
        items = await _fetch_service_types(headers, page_size=100, max_pages=5)
        matches = _best_name_matches(items, service_type_name)
        if not matches: raise HTTPException(status_code=404, detail=f"No service type matched '{service_type_name}'.")
        service_type_id = matches[0].get("id")
    if not service_type_id:
        service_type_id = await _resolve_default_service_type_id(headers)
    if not service_type_id:
        raise HTTPException(status_code=422, detail="Provide service_type_id or service_type_name, or set defaults via env.")

    base = f"https://api.planningcenteronline.com/services/v2/service_types/{service_type_id}/plans"
    params = {"include": include, "page[size]": page_size}
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(base, headers=headers, params=params)
        if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()

    included = {f"{i.get('type')}:{i.get('id')}": i for i in data.get("included", [])} if data.get("included") else {}
    plans_out = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {}); rel = item.get("relationships", {})
        times, needed_positions = [], []
        if rel.get("plan_times", {}).get("data"):
            for ref in rel["plan_times"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                tattrs = (inc.get("attributes") or {})
                times.append({"starts_at": tattrs.get("starts_at"), "ends_at": tattrs.get("ends_at"), "name": tattrs.get("name")})
        if rel.get("needed_positions", {}).get("data"):
            for ref in rel["needed_positions"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                nattrs = (inc.get("attributes") or {})
                needed_positions.append({"team_position_name": nattrs.get("team_position_name"),
                                         "quantity": nattrs.get("quantity"), "assigned_count": nattrs.get("assigned_count")})
        plans_out.append({"id": item.get("id"), "dates": attrs.get("sort_date") or attrs.get("dates"),
                          "title": attrs.get("title"), "series_title": attrs.get("series_title"),
                          "times": times, "needed_positions": needed_positions})
    if from_date or to_date:
        filtered = []
        for p in plans_out:
            d = p.get("dates"); ok = True
            if from_date and d and d < from_date: ok = False
            if to_date and d and d > to_date: ok = False
            if ok: filtered.append(p)
        plans_out = filtered
    return {"count": len(plans_out), "plans": plans_out}

@app.get("/pco/services/plan")
async def services_plan_detail(plan_id: str = Query(...),
                               include: str = Query("plan_times,needed_positions,team_members,team_members.person")):
    base = f"https://api.planningcenteronline.com/services/v2/plans/{plan_id}"
    headers = auth_header(); params = {"include": include}
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(base, headers=headers, params=params)
        if r.status_code != 200: raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
