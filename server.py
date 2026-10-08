"""Single-user CareerForge server. Run with uvicorn server:app."""
import asyncio
import json
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from posting import preview

load_dotenv()
ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", "data/careerforge.sqlite3")
ACTOR = os.getenv("APIFY_ACTOR_ID", "2rJKkhh7vjpX7pvjg")
STAGES = ["Wishlist", "Applied", "Screening", "Interviewing", "Offer", "Rejected"]
FIELDS = ["jobTitle", "company", "status", "dateApplied", "location", "workType", "salary", "source", "url", "companyWebsite", "contact", "nextStep", "notes", "jobDescription", "linkedInJobId", "publishedAt", "applyUrl", "sourceResultId", "resumeUrl", "coverLetterUrl"]
ACTIVE = ("STARTING", "READY", "RUNNING", "TIMING-OUT", "ABORTING", "DOWNLOADING")
tasks = {}


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS applications(id TEXT PRIMARY KEY, identity TEXT, data TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS application_identity ON applications(identity) WHERE identity IS NOT NULL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, remote_id TEXT, dataset_id TEXT, status TEXT NOT NULL,
          created_at TEXT NOT NULL, input TEXT NOT NULL, error TEXT, offset INTEGER NOT NULL DEFAULT 0, remote_status TEXT);
        CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
          identity TEXT, data TEXT NOT NULL, raw TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS result_run ON results(run_id);
        CREATE UNIQUE INDEX IF NOT EXISTS result_identity ON results(run_id, identity) WHERE identity IS NOT NULL;
        """)


def safe_url(value):
    value = str(value or "").strip()
    if value and (urlparse(value).scheme not in ("http", "https") or not urlparse(value).hostname):
        raise HTTPException(422, "Links must be valid http or https URLs")
    return value


def linkedin_id(url):
    parsed = urlparse(str(url or ""))
    if parsed.hostname and (parsed.hostname == "linkedin.com" or parsed.hostname.endswith(".linkedin.com")):
        match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)", parsed.path)
        if match:
            return match.group(1)
    return ""


def identity(item):
    job_id = item.get("linkedInJobId") or linkedin_id(item.get("url"))
    if job_id:
        return "linkedin:" + str(job_id)
    if item.get("url"):
        p = urlparse(item["url"])
        query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                                 if not k.lower().startswith("utm_") and k.lower() not in ("trk", "trackingid", "ref")))
        return "url:" + p.scheme.lower() + "://" + p.netloc.lower() + p.path.rstrip("/") + ("?" + query if query else "")
    if item.get("sourceResultId"):
        return "result:" + item["sourceResultId"]
    return None


def validate_application(item, existing=None):
    if not isinstance(item, dict):
        raise HTTPException(422, "Each application must be an object")
    result = dict(existing or {})
    for key in FIELDS:
        if key in item:
            result[key] = str(item[key] or "")
        elif key not in result:
            result[key] = ""
    result["id"] = str(item.get("id") or result.get("id") or uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", result["id"]):
        raise HTTPException(422, "Application ID contains unsupported characters")
    if not result["jobTitle"].strip() or not result["company"].strip():
        raise HTTPException(422, "Job title and company are required")
    if not result["status"]:
        result["status"] = "Wishlist"
    if result["status"] not in STAGES:
        raise HTTPException(422, "Invalid pipeline stage")
    if result["workType"] not in ("", "Remote", "Hybrid", "Onsite"):
        raise HTTPException(422, "Invalid work arrangement")
    if result["dateApplied"]:
        try:
            result["dateApplied"] = datetime.strptime(result["dateApplied"][:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise HTTPException(422, "Date must use YYYY-MM-DD format")
    for key in ("url", "companyWebsite", "applyUrl"):
        result[key] = safe_url(result[key])
    for key in ('resumeUrl', 'coverLetterUrl'):
        value = str(result[key] or '').strip()
        try:
            safe_url(value)
            parsed = urlparse(value)
            if value and (parsed.username or parsed.password or re.search(r'\s', value)):
                raise ValueError()
            parsed.port
        except (ValueError, HTTPException):
            raise HTTPException(422, 'Resume and cover-letter links must be valid http or https URLs without credentials')
        result[key] = value
    return result


def store_application(c, item):
    c.execute("INSERT INTO applications VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET identity=excluded.identity,data=excluded.data",
              (item["id"], identity(item), json.dumps(item)))


def applications_list(c):
    return [json.loads(r["data"]) for r in c.execute("SELECT data FROM applications ORDER BY rowid DESC")]


def normalize(raw):
    def text(key):
        value = raw.get(key)
        return str(value) if value is not None and not isinstance(value, (dict, list)) else ""
    def link(key):
        try:
            return safe_url(text(key))
        except HTTPException:
            return ""
    salary = raw.get("salaryInfo") or []
    arrangement = text("workplaceType").lower()
    return {"jobTitle": text("jobTitle"), "company": text("companyName"), "location": text("location"),
            "salary": " – ".join(str(v) for v in salary) if isinstance(salary, list) else str(salary),
            "url": link("jobUrl"), "companyWebsite": link("companyWebsite"), "applyUrl": link("applyUrl"),
            "jobDescription": text("jobDescription") or text("descriptionText") or text("description"),
            "linkedInJobId": text("jobId") or linkedin_id(text("jobUrl")),
            "publishedAt": text("publishedAt"), "postedTime": text("postedTime"),
            "employmentType": text("contractType"), "jobFunction": text("workType"),
            "experienceLevel": text("experienceLevel"),
            "workType": {"remote": "Remote", "hybrid": "Hybrid", "on-site": "Onsite", "onsite": "Onsite"}.get(arrangement, ""),
            "contact": " · ".join(filter(None, [text("posterFullName"), link("posterProfileUrl")])),
            "source": "LinkedIn", "notes": "", "nextStep": "", "status": "Wishlist", "dateApplied": ""}


def search_input(payload):
    mode = payload.get("mode", "form")
    limit = payload.get("maxItems", 150)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 150 <= limit <= 10000:
        raise HTTPException(422, "Result limit must be an integer between 150 and 10000")
    result = {"maxItems": limit, "saveOnlyUniqueItems": True, "enrichCompanyData": bool(payload.get("enrichCompanyData", False))}
    def strings(key):
        values = payload.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise HTTPException(422, key + " must be a list of strings")
        return [v.strip() for v in values if v.strip()]
    if mode == "urls":
        urls = strings("urls")
        if not urls:
            raise HTTPException(422, "At least one LinkedIn search URL is required")
        for url in urls:
            safe_url(url)
            p = urlparse(url)
            if not p.hostname or not (p.hostname == "linkedin.com" or p.hostname.endswith(".linkedin.com")) or not p.path.startswith("/jobs/search"):
                raise HTTPException(422, "Use LinkedIn /jobs/search URLs")
        result["startUrls"] = [{"url": v} for v in urls]
    elif mode == "form":
        result["keyword"] = strings("keywords")
        result["locations"] = strings("locations")
        if not result["keyword"]:
            raise HTTPException(422, "At least one keyword is required")
        for key, allowed in {"jobType": ["full-time", "part-time", "contract", "temporary", "internship", "volunteer"],
                             "experienceLevel": ["internship", "entry-level", "associate", "mid-senior", "director", "executive"],
                             "workType": ["on-site", "remote", "hybrid"]}.items():
            result[key] = strings(key)
            if any(v not in allowed for v in result[key]):
                raise HTTPException(422, "Invalid " + key)
        result["publishedAt"] = payload.get("publishedAt", "")
        if result["publishedAt"] not in ("", "r86400", "r604800", "r2592000"):
            raise HTTPException(422, "Invalid posting age")
    else:
        raise HTTPException(422, "Invalid search mode")
    return result


def run_row(run_id):
    with db() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Search not found")
        result = dict(row)
        result["input"] = json.loads(result["input"])
        result["count"] = c.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0]
        result["partial"] = bool(result["count"] and result["status"] != "SUCCEEDED")
        result["consoleUrl"] = "https://console.apify.com/actors/runs/" + result["remote_id"] if result["remote_id"] else ""
        return result


def update_run(run_id, **fields):
    with db() as c:
        c.execute("UPDATE runs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), run_id))


async def apify(method, path, **kwargs):
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        raise RuntimeError("Configure APIFY_TOKEN in .env and restart the server")
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(4):
            try:
                response = await client.request(method, "https://api.apify.com/v2/" + path,
                                                headers={"Authorization": "Bearer " + token}, **kwargs)
                if method == "GET" and (response.status_code == 429 or response.status_code >= 500) and attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if response.is_error:
                    raise RuntimeError("Apify request rejected (HTTP %s). Check credentials, actor access, and account balance." % response.status_code)
                return response.json()
            except httpx.RequestError:
                if method != "GET" or attempt == 3:
                    raise RuntimeError("Could not reach Apify. If starting a run, check Apify Console before retrying; it may already have started.")
                await asyncio.sleep(2 ** attempt)


async def monitor(run_id):
    try:
        row = run_row(run_id)
        if not row["remote_id"]:
            params = {"timeout": 1500}
            ceiling = os.getenv("APIFY_MAX_CHARGE_USD", "1")
            if ceiling:
                params["maxTotalChargeUsd"] = float(ceiling)
            response = await apify("POST", "acts/" + ACTOR + "/runs", params=params, json=row["input"])
            remote = response["data"]
            update_run(run_id, remote_id=remote["id"], dataset_id=remote.get("defaultDatasetId"), status=remote["status"])
        row = run_row(run_id)
        while row["status"] != "DOWNLOADING":
            remote = (await apify("GET", "actor-runs/" + row["remote_id"]))["data"]
            status = remote["status"]
            update_run(run_id, status=status, dataset_id=remote.get("defaultDatasetId"), remote_status=status, error=None)
            if status in ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"):
                update_run(run_id, status="DOWNLOADING")
                break
            await asyncio.sleep(5)
            row = run_row(run_id)
        row = run_row(run_id)
        if row["dataset_id"]:
            while True:
                items = await apify("GET", "datasets/" + row["dataset_id"] + "/items", params={"offset": row["offset"], "limit": 250, "clean": "true"})
                if not items:
                    break
                with db() as c:
                    for raw in items:
                        item = normalize(raw)
                        c.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?,?)",
                                  (str(uuid.uuid4()), run_id, identity(item), json.dumps(item), json.dumps(raw)))
                    c.execute("UPDATE runs SET offset=offset+? WHERE id=?", (len(items), run_id))
                row = run_row(run_id)
        status = row["remote_status"] or "SUCCEEDED"
        update_run(run_id, status=status, error=None if status == "SUCCEEDED" else "Actor ended with " + status + ". Available results are partial.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        update_run(run_id, status="RECOVERY_NEEDED", error=str(exc))
    finally:
        tasks.pop(run_id, None)


def launch(run_id):
    if run_id not in tasks:
        tasks[run_id] = asyncio.create_task(monitor(run_id))


@asynccontextmanager
async def lifespan(app):
    initialize()
    with db() as c:
        rows = c.execute("SELECT id,remote_id FROM runs WHERE status IN ('STARTING','READY','RUNNING','TIMING-OUT','ABORTING','DOWNLOADING')").fetchall()
    for row in rows:
        if row["remote_id"]:
            launch(row["id"])
        else:
            update_run(row["id"], status="RECOVERY_NEEDED", error="Server stopped while starting the run. Check Apify Console before starting another search.")
    yield
    pending = list(tasks.values())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


app = FastAPI(title="CareerForge", lifespan=lifespan)


@app.middleware("http")
async def local_requests(request: Request, call_next):
    host = request.url.hostname
    if host not in ("localhost", "127.0.0.1", "::1", "testserver"):
        return JSONResponse({"detail": "Use localhost to access CareerForge"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
    return await call_next(request)


@app.exception_handler(sqlite3.IntegrityError)
async def duplicate_error(request, exc):
    return JSONResponse({"detail": "This posting is already in the tracker"}, status_code=409)


@app.get("/")
def index():
    return FileResponse(ROOT / "job_application_tracker.html")


@app.get("/discover.js")
def javascript():
    return FileResponse(ROOT / "discover.js", media_type="text/javascript")


@app.get("/api/config")
def config():
    return {"apifyConfigured": bool(os.getenv("APIFY_TOKEN")), "actorId": ACTOR, "stages": STAGES}


@app.get("/api/applications")
def get_applications():
    with db() as c:
        return applications_list(c)


@app.post("/api/posting-preview")
async def posting_preview(payload: dict):
    return await preview(payload.get("url"))


@app.post("/api/applications")
def create_application(payload: dict):
    item = validate_application(payload)
    with db() as c:
        if c.execute("SELECT 1 FROM applications WHERE id=?", (item["id"],)).fetchone():
            raise HTTPException(409, "Application ID already exists")
        store_application(c, item)
    return item


@app.patch("/api/applications/{app_id}")
def edit_application(app_id: str, payload: dict):
    with db() as c:
        row = c.execute("SELECT data FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Application not found")
        item = validate_application({**payload, "id": app_id}, json.loads(row["data"]))
        store_application(c, item)
    return item


@app.delete("/api/applications/{app_id}")
def delete_application(app_id: str):
    with db() as c:
        c.execute("DELETE FROM applications WHERE id=?", (app_id,))
    return {"ok": True}


@app.post("/api/applications/bulk")
def bulk_applications(payload: dict):
    items = payload.get("applications")
    if not isinstance(items, list):
        raise HTTPException(422, "applications must be an array")
    replace = payload.get("replace", False)
    imported = skipped = 0
    with db() as c:
        existing = {a["id"]: a for a in applications_list(c)}
        normalized = [validate_application(a, existing.get(str(a.get("id")))) if isinstance(a, dict) else validate_application(a) for a in items]
        if replace:
            c.execute("DELETE FROM applications")
        for item in normalized:
            match = c.execute("SELECT id FROM applications WHERE id=? OR (identity IS NOT NULL AND identity=?)", (item["id"], identity(item))).fetchone()
            if match:
                skipped += 1
                continue
            store_application(c, item)
            imported += 1
    return {"imported": imported, "skipped": skipped}


@app.get("/api/settings")
def settings():
    with db() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}


@app.put("/api/settings")
def save_settings(payload: dict):
    with db() as c:
        if "googleSyncUrl" in payload:
            value = safe_url(payload["googleSyncUrl"])
            c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("googleSyncUrl", value))
    return {"ok": True}


@app.get("/api/runs")
def runs():
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM runs ORDER BY created_at DESC")]
    return [run_row(r) for r in ids]


@app.post("/api/runs", status_code=202)
async def start_run(payload: dict):
    if not os.getenv("APIFY_TOKEN"):
        raise HTTPException(503, "Configure APIFY_TOKEN in .env and restart the server")
    data = search_input(payload)
    run_id = str(uuid.uuid4())
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM runs WHERE status IN (" + ",".join("?" for _ in ACTIVE) + ")", ACTIVE).fetchone():
            raise HTTPException(409, "A search is already active")
        c.execute("INSERT INTO runs(id,status,created_at,input) VALUES (?,?,?,?)", (run_id, "STARTING", now(), json.dumps(data)))
    launch(run_id)
    return run_row(run_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return run_row(run_id)


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    row = run_row(run_id)
    if not row["remote_id"]:
        raise HTTPException(409, "No run ID was received. Check Apify Console; this search cannot be safely resumed automatically.")
    with db() as c:
        if c.execute("SELECT 1 FROM runs WHERE id<>? AND status IN (" + ",".join("?" for _ in ACTIVE) + ")", (run_id, *ACTIVE)).fetchone():
            raise HTTPException(409, "Another search is active")
    update_run(run_id, status="DOWNLOADING" if row["remote_status"] in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT") else "RUNNING", error=None)
    launch(run_id)
    return run_row(run_id)


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    row = run_row(run_id)
    if not row["remote_id"]:
        raise HTTPException(409, "Wait for the run ID before cancelling")
    if row["status"] not in ACTIVE and row["status"] != "RECOVERY_NEEDED":
        raise HTTPException(409, "This run has already ended")
    try:
        await apify("POST", "actor-runs/" + row["remote_id"] + "/abort")
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    update_run(run_id, status="ABORTING")
    launch(run_id)
    return run_row(run_id)


@app.get("/api/runs/{run_id}/results")
def get_results(run_id: str, q: str = "", sort: str = "jobTitle", descending: bool = False,
                page: int = Query(1, ge=1), pageSize: int = Query(25, ge=1, le=100)):
    run_row(run_id)
    if sort not in ("jobTitle", "company", "location", "salary", "publishedAt", "employmentType"):
        raise HTTPException(422, "Invalid sort column")
    with db() as c:
        tracked = {r[0] for r in c.execute("SELECT identity FROM applications WHERE identity IS NOT NULL")}
        values = [{**json.loads(r["data"]), "id": r["id"], "inTracker": (r["identity"] or "result:" + r["id"]) in tracked} for r in c.execute("SELECT * FROM results WHERE run_id=? ORDER BY rowid", (run_id,))]
    query = q.casefold().strip()
    if query:
        values = [v for v in values if query in " ".join(str(v.get(k, "")) for k in ("jobTitle", "company", "location", "jobDescription")).casefold()]
    values.sort(key=lambda v: str(v.get(sort, "")).casefold(), reverse=descending)
    return {"items": values[(page - 1) * pageSize:page * pageSize], "total": len(values), "page": page}


@app.get("/api/results/{result_id}")
def get_result(result_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Result not found")
        return {"id": row["id"], "data": json.loads(row["data"]), "metadata": json.loads(row["raw"])}


@app.post("/api/import")
def import_results(payload: dict):
    ids = payload.get("resultIds")
    stage = payload.get("stage", "Wishlist")
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or stage not in STAGES:
        raise HTTPException(422, "Provide result IDs and a valid stage")
    imported = skipped = 0
    with db() as c:
        for result_id in dict.fromkeys(ids):
            row = c.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Selected result no longer exists")
            item = json.loads(row["data"])
            item["sourceResultId"] = result_id
            key = identity(item)
            if key and c.execute("SELECT 1 FROM applications WHERE identity=?", (key,)).fetchone():
                skipped += 1
                continue
            item.update(id=str(uuid.uuid4()), status=stage, dateApplied=str(payload.get("dateTracked") or datetime.now().date().isoformat()))
            item = validate_application(item)
            store_application(c, item)
            imported += 1
    return {"imported": imported, "skipped": skipped}
