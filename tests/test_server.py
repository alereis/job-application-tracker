import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    with TestClient(server.app) as client:
        yield client


def application(**changes):
    return {"id": "existing", "jobTitle": "Engineer", "company": "Acme", "status": "Wishlist", **changes}


def seed_result(raw=None, run_id="run", result_id="result", status="SUCCEEDED"):
    raw = raw or {"jobId": "123", "jobTitle": "Engineer", "companyName": "Acme", "jobUrl": "https://www.linkedin.com/jobs/view/123", "jobDescription": "Full posting"}
    item = server.normalize(raw)
    with server.db() as c:
        c.execute("INSERT OR IGNORE INTO runs(id,status,created_at,input) VALUES (?,?,?,?)", (run_id, status, server.now(), '{}'))
        c.execute("INSERT INTO results VALUES (?,?,?,?,?)", (result_id, run_id, server.identity(item), json.dumps(item), json.dumps(raw)))
    return item


def test_crud_and_restart_persistence(client):
    assert client.get("/api/applications").json() == []
    assert client.post("/api/applications", json=application()).status_code == 200
    assert client.patch("/api/applications/existing", json={"status": "Applied", "notes": "Personal"}).json()["notes"] == "Personal"
    server.initialize()
    assert client.get("/api/applications").json()[0]["status"] == "Applied"
    assert client.delete("/api/applications/existing").status_code == 200
    assert client.get("/api/applications").json() == []


def test_normalization_and_job_function():
    item = server.normalize({"jobTitle": "Engineer", "workType": "Engineering", "salaryInfo": ["NZ$100k", "NZ$120k"], "jobDescription": "Plain text", "jobUrl": "javascript:alert(1)"})
    assert item["workType"] == ""
    assert item["jobFunction"] == "Engineering"
    assert item["jobDescription"] == "Plain text"
    assert item["salary"] == "NZ$100k – NZ$120k"
    assert item["url"] == ""
    assert server.normalize({})["salary"] == ""


def test_url_identity():
    assert server.identity({"url": "https://nz.linkedin.com/jobs/view/engineer-at-acme-123/?trk=x"}) == "linkedin:123"
    assert server.identity({"url": "https://www.linkedin.com/jobs/view/123?trk=y"}) == "linkedin:123"
    assert server.identity({"url":"https://example.com/jobs?id=1&utm_source=x"}) == "url:https://example.com/jobs?id=1"
    assert server.identity({"url":"https://example.com/jobs?id=1"}) != server.identity({"url":"https://example.com/jobs?id=2"})


def test_selected_import_and_duplicate_skip(client):
    seed_result()
    response = client.post("/api/import", json={"resultIds": ["result"], "dateTracked": "2026-10-04"})
    assert response.json() == {"imported": 1, "skipped": 0}
    app = client.get("/api/applications").json()[0]
    assert app["status"] == "Wishlist" and app["dateApplied"] == "2026-10-04"
    assert app["jobDescription"] == "Full posting" and app["notes"] == ""
    client.patch("/api/applications/" + app["id"], json={"notes": "Keep my notes"})
    assert client.post("/api/import", json={"resultIds": ["result"]}).json() == {"imported": 0, "skipped": 1}
    assert client.get("/api/applications").json()[0]["notes"] == "Keep my notes"
    assert client.get("/api/runs/run/results").json()["items"][0]["inTracker"]


def test_import_batch_is_atomic(client):
    seed_result()
    assert client.post("/api/import", json={"resultIds": ["result", "missing"]}).status_code == 404
    assert client.get("/api/applications").json() == []


def test_legacy_migration_repeat_and_old_sheet_preservation(client):
    app = application(jobDescription="Original description", linkedInJobId="123", applyUrl="https://example.com/apply")
    first = client.post("/api/applications/bulk", json={"applications": [app]})
    assert first.json()["imported"] == 1
    assert client.post("/api/applications/bulk", json={"applications": [app]}).json()["skipped"] == 1
    older = application(notes="Sheet notes", status="Applied")
    assert client.post("/api/applications/bulk", json={"applications": [older], "replace": True}).status_code == 200
    restored = client.get("/api/applications").json()[0]
    assert restored["jobDescription"] == "Original description" and restored["linkedInJobId"] == "123"
    assert restored["notes"] == "Sheet notes"


def test_invalid_replacement_preserves_records(client):
    client.post("/api/applications", json=application())
    assert client.post("/api/applications/bulk", json={"replace": True, "applications": [{"company": "Bad"}]}).status_code == 422
    assert len(client.get("/api/applications").json()) == 1


@pytest.mark.parametrize("payload", [{"url":"javascript:alert(1)"}, {"status":"Invalid"}, {"workType":"Engineering"}, {"id":"x');alert(1)//"}, {"dateApplied":"<img src=x>"}])
def test_application_validation(client, payload):
    assert client.post("/api/applications", json=application(**payload)).status_code == 422


def test_search_modes_and_validation():
    value = server.search_input({"keywords":[" Engineer "], "locations":["Auckland"], "workType":["remote"], "publishedAt":"r604800"})
    assert value["keyword"] == ["Engineer"] and value["saveOnlyUniqueItems"] is True
    value = server.search_input({"mode":"urls", "urls":["https://www.linkedin.com/jobs/search/?keywords=test"]})
    assert value["startUrls"][0]["url"].startswith("https://") and "workType" not in value
    for bad in ({"keywords":["x"],"maxItems":10}, {"keywords":["x"],"workType":["Engineering"]}, {"mode":"urls","urls":["https://linkedin.com.evil.com/jobs/search/"]}):
        with pytest.raises(HTTPException):
            server.search_input(bad)


def test_paginated_filtered_results(client):
    for number in range(30):
        seed_result({"jobId":str(number), "jobTitle":f"Role {number:02}", "companyName":"Acme", "jobDescription":"Python"},result_id=f"r{number}")
    assert len(client.get("/api/runs/run/results").json()["items"]) == 25
    assert len(client.get("/api/runs/run/results?page=2").json()["items"]) == 5
    assert client.get("/api/runs/run/results?q=Python").json()["total"] == 30
    assert client.get("/api/runs/run/results?descending=true").json()["items"][0]["jobTitle"] == "Role 29"
    assert client.get("/api/runs/run/results?sort=invalid").status_code == 422


def test_local_security_and_config(client):
    assert client.post("/api/applications", json=application(), headers={"Origin":"https://evil.example"}).status_code == 403
    assert client.get("/api/config", headers={"Host":"evil.example"}).status_code == 403
    assert client.get("/api/config").json()["apifyConfigured"] is False
    assert client.post("/api/runs", json={"keywords":["Engineer"]}).status_code == 503
    assert client.get("/").status_code == 200
    assert client.get("/discover.js").status_code == 200


def test_single_active_run(client, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test")
    monkeypatch.setattr(server,"launch",lambda run_id: None)
    assert client.post("/api/runs", json={"keywords":["Engineer"]}).status_code == 202
    assert client.post("/api/runs", json={"keywords":["Engineer"]}).status_code == 409


def test_import_without_job_id_or_url_is_idempotent(client):
    seed_result({"jobTitle":"Engineer", "companyName":"Acme"})
    assert client.post("/api/import", json={"resultIds":["result"]}).json()["imported"]==1
    assert client.post("/api/import", json={"resultIds":["result"]}).json()["skipped"]==1
    assert client.get("/api/runs/run/results").json()["items"][0]["inTracker"]


def test_cancel_known_run_and_resume(client, monkeypatch):
    seed_result(status="RUNNING")
    server.update_run("run",remote_id="remote",dataset_id="dataset")
    fake=AsyncMock(return_value={"data":{"status":"ABORTING"}})
    monkeypatch.setattr(server,"apify",fake)
    monkeypatch.setattr(server,"launch",lambda run_id: None)
    assert client.post("/api/runs/run/cancel").json()["status"]=="ABORTING"
    assert fake.call_args.args==("POST","actor-runs/remote/abort")
    server.update_run("run",status="RECOVERY_NEEDED",remote_status="SUCCEEDED")
    assert client.post("/api/runs/run/resume").json()["status"]=="DOWNLOADING"


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"])
def test_actor_lifecycle_and_partial_results(client, monkeypatch, status):
    calls=[]
    async def fake(method,path,**kwargs):
        calls.append((method,path,kwargs))
        if method=="POST":return {"data":{"id":"remote", "defaultDatasetId":"dataset", "status":"READY"}}
        if path.startswith("actor-runs/"):return {"data":{"status":status, "defaultDatasetId":"dataset"}}
        if kwargs["params"]["offset"]==0:return [{"jobId":"123", "jobTitle":"Engineer", "companyName":"Acme", "jobDescription":"Full"}]
        return []
    monkeypatch.setattr(server,"apify",fake)
    with server.db() as c:
        c.execute("INSERT INTO runs(id,status,created_at,input) VALUES ('run','STARTING',?,'{}')", (server.now(),))
    asyncio.run(server.monitor("run"))
    row=server.run_row("run")
    assert row["status"]==status and row["count"]==1
    assert row["partial"] is (status!="SUCCEEDED")
    assert sum(method=="POST" for method,_,_ in calls)==1


def test_download_recovery_never_starts_actor(client, monkeypatch):
    with server.db() as c:
        c.execute("INSERT INTO runs(id,remote_id,dataset_id,status,created_at,input,offset,remote_status) VALUES ('run','remote','dataset','DOWNLOADING',?,'{}',250,'SUCCEEDED')",(server.now(),))
    fake=AsyncMock(return_value=[])
    monkeypatch.setattr(server,"apify",fake)
    asyncio.run(server.monitor("run"))
    assert server.run_row("run")["status"]=="SUCCEEDED"
    assert fake.call_args.args[0]=="GET" and fake.call_args.kwargs["params"]["offset"]==250


def test_startup_recovers_known_run_and_flags_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(server,"DB_PATH",str(tmp_path/"restart.sqlite3"))
    server.initialize()
    with server.db() as c:
        c.execute("INSERT INTO runs(id,remote_id,status,created_at,input) VALUES ('known','remote','RUNNING',?,'{}')",(server.now(),))
        c.execute("INSERT INTO runs(id,status,created_at,input) VALUES ('unknown','STARTING',?,'{}')",(server.now(),))
    launched=[]
    monkeypatch.setattr(server,"launch",launched.append)
    with TestClient(server.app):
        assert launched==["known"]
        assert server.run_row("unknown")["status"]=="RECOVERY_NEEDED"


def test_interrupted_start_is_not_repeated(client, monkeypatch):
    fake=AsyncMock(side_effect=RuntimeError("Connection lost while starting"))
    monkeypatch.setattr(server,"apify",fake)
    with server.db() as c:
        c.execute("INSERT INTO runs(id,status,created_at,input) VALUES ('run','STARTING',?,'{}')",(server.now(),))
    asyncio.run(server.monitor("run"))
    assert fake.call_count==1 and server.run_row("run")["status"]=="RECOVERY_NEEDED"
    assert client.post("/api/runs/run/resume").status_code==409


def test_bridge_copies_match():
    html=Path("job_application_tracker.html").read_text()
    embedded=html.split("const APPS_SCRIPT_SOURCE = `",1)[1].split("`;",1)[0]
    # Existing embedded script escapes backslashes for the JS template literal.
    assert embedded.replace('\\\\','\\').strip()==Path("google_apps_script.gs").read_text().strip()


DOCUMENT_LINKS = {'resumeUrl': 'https://drive.google.com/file/d/resume/view', 'coverLetterUrl': 'https://docs.google.com/document/d/letter/edit'}


def test_document_links_patch_preserves_application(client):
    original = client.post('/api/applications', json=application(notes='Personal', jobDescription='Posting', linkedInJobId='123')).json()
    updated = client.patch('/api/applications/existing', json=DOCUMENT_LINKS).json()
    for key, value in original.items():
        if key not in DOCUMENT_LINKS:
            assert updated[key] == value
    assert all(updated[key] == value for key, value in DOCUMENT_LINKS.items())
    removed = client.patch('/api/applications/existing', json={'resumeUrl': ''}).json()
    assert removed['resumeUrl'] == ''
    assert removed['coverLetterUrl'] == DOCUMENT_LINKS['coverLetterUrl']
    assert client.get('/api/applications').json()[0] == removed


@pytest.mark.parametrize('link', ['javascript:alert(1)', 'file:///tmp/resume.pdf', 'ftp://example.com/file', 'https://[invalid', 'https://user:password@example.com', 'https://example.com:bad/file', 'https://bad host/file'])
@pytest.mark.parametrize('field', ['resumeUrl', 'coverLetterUrl'])
def test_invalid_document_link_is_atomic(client, link, field):
    saved = client.post('/api/applications', json=application(**DOCUMENT_LINKS)).json()
    assert client.patch('/api/applications/existing', json={field: link, 'notes': 'Must not save'}).status_code == 422
    assert client.get('/api/applications').json()[0] == saved


def test_document_links_old_sheet_and_explicit_clear(client):
    client.post('/api/applications', json=application(**DOCUMENT_LINKS))
    old_sheet = application(notes='Sheet update')
    assert client.post('/api/applications/bulk', json={'replace': True, 'applications': [old_sheet]}).status_code == 200
    restored = client.get('/api/applications').json()[0]
    assert all(restored[key] == value for key, value in DOCUMENT_LINKS.items())
    new_sheet = {**old_sheet, 'resumeUrl': '', 'coverLetterUrl': ''}
    client.post('/api/applications/bulk', json={'replace': True, 'applications': [new_sheet]})
    assert client.get('/api/applications').json()[0]['resumeUrl'] == ''
    assert client.get('/api/applications').json()[0]['coverLetterUrl'] == ''


def test_document_links_json_import_and_legacy_defaults(client):
    payload = {'applications': [application(**DOCUMENT_LINKS)]}
    assert client.post('/api/applications/bulk', json=payload).json()['imported'] == 1
    exported = client.get('/api/applications').json()
    client.delete('/api/applications/existing')
    client.post('/api/applications/bulk', json={'applications': exported})
    assert client.get('/api/applications').json() == exported
    legacy = client.post('/api/applications', json=application(id='legacy')).json()
    assert legacy['resumeUrl'] == legacy['coverLetterUrl'] == ''


def test_embedded_apps_script_matches_downloadable():
    html = Path('job_application_tracker.html').read_text()
    embedded = html.split('const APPS_SCRIPT_SOURCE = `', 1)[1].split('`;', 1)[0]
    # JavaScript template literals unescape the doubled backslashes in script strings.
    embedded = embedded.replace(chr(92) * 2, chr(92))
    assert embedded.strip() == Path('google_apps_script.gs').read_text().strip()
    assert '"resumeUrl", "coverLetterUrl"' in embedded
