
import os
import base64
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
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json"
    }

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
servers = [{"url": PUBLIC_BASE_URL}] if PUBLIC_BASE_URL else []
app = FastAPI(title="Planning Center Connector (MVP)", servers=servers)

# CORS (handy for quick tests)
origins = (os.getenv("CORS_ORIGINS") or "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/pco/people/find")
async def find_person(name: str = Query(..., description="Full or partial name"),
                      page_size: int = Query(5, ge=1, le=100)):
    url = "https://api.planningcenteronline.com/people/v2/people"
    params = {
        "where[name]": name,
        "include": "emails,phone_numbers",
        "page[size]": page_size
    }
    headers = auth_header()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers, params=params)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()

    included = {f"{i.get('type')}:{i.get('id')}": i for i in data.get("included", [])} if data.get("included") else {}
    results = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {})
        rel = item.get("relationships", {})

        emails: List[str] = []
        if rel.get("emails", {}).get("data"):
            for ref in rel["emails"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                addr = (inc.get("attributes") or {}).get("address")
                if addr:
                    emails.append(addr)

        phones: List[str] = []
        if rel.get("phone_numbers", {}).get("data"):
            for ref in rel["phone_numbers"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                num = (inc.get("attributes") or {}).get("number")
                if num:
                    phones.append(num)

        results.append({
            "id": item.get("id"),
            "name": attrs.get("name"),
            "first_name": attrs.get("first_name"),
            "last_name": attrs.get("last_name"),
            "emails": emails,
            "phones": phones
        })

    return {"count": len(results), "people": results}

@app.get("/pco/services/plans")
async def services_plans(
    service_type_id: str = Query(..., description="PCO Services service_type id"),
    page_size: int = Query(10, ge=1, le=100),
    include: str = Query("plan_times,needed_positions,team_members", description="Comma-separated includes"),
    from_date: Optional[str] = Query(None, description="Optional ISO date (YYYY-MM-DD) to filter after"),
    to_date: Optional[str] = Query(None, description="Optional ISO date (YYYY-MM-DD) to filter before"),
):
    base = f"https://api.planningcenteronline.com/services/v2/service_types/{service_type_id}/plans"
    params = {"include": include, "page[size]": page_size}
    headers = auth_header()
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(base, headers=headers, params=params)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()

    included = {f"{i.get('type')}:{i.get('id')}": i for i in data.get("included", [])} if data.get("included") else {}
    plans_out = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {})
        rel = item.get("relationships", {})
        times = []
        if rel.get("plan_times", {}).get("data"):
            for ref in rel["plan_times"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                tattrs = (inc.get("attributes") or {})
                times.append({
                    "starts_at": tattrs.get("starts_at"),
                    "ends_at": tattrs.get("ends_at"),
                    "name": tattrs.get("name"),
                })

        needed_positions = []
        if rel.get("needed_positions", {}).get("data"):
            for ref in rel["needed_positions"]["data"]:
                inc = included.get(f"{ref.get('type')}:{ref.get('id')}", {})
                nattrs = (inc.get("attributes") or {})
                needed_positions.append({
                    "team_position_name": nattrs.get("team_position_name"),
                    "quantity": nattrs.get("quantity"),
                    "assigned_count": nattrs.get("assigned_count"),
                })

        plans_out.append({
            "id": item.get("id"),
            "dates": attrs.get("sort_date") or attrs.get("dates"),
            "title": attrs.get("title"),
            "series_title": attrs.get("series_title"),
            "times": times,
            "needed_positions": needed_positions,
        })

    if from_date or to_date:
        filtered = []
        for p in plans_out:
            d = p.get("dates")
            ok = True
            if from_date and d and d < from_date:
                ok = False
            if to_date and d and d > to_date:
                ok = False
            if ok:
                filtered.append(p)
        plans_out = filtered

    return {"count": len(plans_out), "plans": plans_out}

@app.get("/pco/services/plan")
async def services_plan_detail(
    plan_id: str = Query(..., description="PCO Services plan id"),
    include: str = Query("plan_times,needed_positions,team_members,team_members.person", description="Includes for detail"),
):
    base = f"https://api.planningcenteronline.com/services/v2/plans/{plan_id}"
    params = { "include": include }
    headers = auth_header()
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(base, headers=headers, params=params)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()
    return data


@app.get("/openapi-chatgpt.json")
def openapi_chatgpt(request: Request):
    spec = app.openapi()
    base_url = str(request.base_url).rstrip("/")
    spec["servers"] = [{"url": base_url}]
    return spec
