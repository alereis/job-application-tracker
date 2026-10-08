"""Optional real-browser smoke test. Uses a temporary database and a fake actor."""
import json
import csv
import io
import asyncio
import os
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright, expect

import server
import posting


def main():
    errors = []
    with tempfile.TemporaryDirectory() as directory:
        server.DB_PATH = str(Path(directory) / "test.sqlite3")
        os.environ["APIFY_TOKEN"] = "fake-for-ui-test"

        async def fake_apify(method, path, **kwargs):
            if method == "POST":
                return {"data": {"id": "fake-run", "status": "READY", "defaultDatasetId": "fake-dataset"}}
            if path.startswith("actor-runs/"):
                return {"data": {"status": "SUCCEEDED", "defaultDatasetId": "fake-dataset"}}
            if kwargs["params"]["offset"]:
                return []
            return [{"jobId": str(1000 + i), "jobTitle": f"Engineer {i:02}", "companyName": "Acme",
                     "jobUrl": f"https://www.linkedin.com/jobs/view/{1000+i}", "location": "Auckland",
                     "jobDescription": "Full posting for Python engineer", "workType": "Engineering",
                     "contractType": "Full-time", "salaryInfo": ["NZ$100000", "NZ$120000"]} for i in range(26)]

        server.apify = fake_apify
        async def fake_posting(url):
            if '/999/' in url:
                from fastapi import HTTPException
                raise HTTPException(502, 'LinkedIn blocked this lookup.')
            if '/200/' in url:
                await asyncio.sleep(.5)
            return '<script type="application/ld+json">' + json.dumps({
                '@type': 'JobPosting', 'title': 'Autofilled Engineer',
                'hiringOrganization': {'name': 'Autofill Co'},
                'jobLocation': {'address': {'addressLocality': 'Auckland'}},
                'description': '<p>Full posting</p><ul><li>Python</li></ul>',
                'jobLocationType': 'TELECOMMUTE', 'datePosted': '2026-10-01'
            }) + '</script><a class="apply-button" href="https://example.com/apply">Apply</a>'
        posting.fetch_posting = fake_posting
        instance = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=8001, log_level="warning"))
        thread = threading.Thread(target=instance.run, daemon=True)
        thread.start()
        for _ in range(100):
            if instance.started:
                break
            time.sleep(.05)
        assert instance.started, "Test server did not start"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://localhost:8001")
                expect(page.locator("#database-status")).to_have_text("Saved locally in SQLite")
                page.get_by_role("button", name="Discover Jobs", exact=True).click()
                page.locator("#search-keywords").fill("Python engineer")
                page.locator("#search-locations").fill("Auckland")
                page.get_by_role("button", name="Run Search", exact=True).click()
                expect(page.locator("#run-status")).to_contain_text("SUCCEEDED", timeout=15000)
                expect(page.locator("#discovery-table tr")).to_have_count(25)
                page.locator("[data-preview]").first.click()
                expect(page.locator("#preview-content")).to_contain_text("Full posting for Python engineer")
                page.locator("#close-preview").click()
                page.locator("[data-result-check]").nth(0).check()
                page.locator("[data-result-check]").nth(1).check()
                page.locator("#next-page").click()
                expect(page.locator("#discovery-table tr")).to_have_count(1)
                page.locator("#select-page").check()
                expect(page.locator("#selected-count")).to_have_text("3 selected")
                page.locator("#import-selected").click()
                expect(page.locator("#selected-count")).to_have_text("0 selected")
                expect(page.locator("#discovery-table")).to_contain_text("Already tracked")
                page.locator("#tab-kanban").click()
                expect(page.locator("#count-Wishlist")).to_have_text("3")
                page.reload()
                expect(page.locator("#database-status")).to_have_text("Saved locally in SQLite")
                expect(page.locator("#count-Wishlist")).to_have_text("3")
                page.locator("header button[onclick=\"openNewModal()\"]").click()
                page.locator('#field-company').fill('Typed company')
                page.locator('#field-notes').fill('Personal notes')
                original_date = page.locator('#field-date').input_value()
                page.locator('#quick-url-input').fill('https://nz.linkedin.com/jobs/view/123/?trk=x')
                page.get_by_role('button', name='Autofill', exact=True).click()
                expect(page.locator('#posting-extraction-result')).to_contain_text('Filled')
                expect(page.locator('#field-title')).to_have_value('Autofilled Engineer')
                expect(page.locator('#field-company')).to_have_value('Typed company')
                expect(page.locator('#field-notes')).to_have_value('Personal notes')
                expect(page.locator('#field-description')).to_have_value('Full posting\nPython')
                expect(page.locator('#field-worktype')).to_have_value('Remote')
                expect(page.locator('#field-status')).to_have_value('Applied')
                expect(page.locator('#field-date')).to_have_value(original_date)
                page.locator('#job-form [type=submit]').click()
                page.reload()
                saved = page.request.get('http://localhost:8001/api/applications').json()
                autofilled = next(a for a in saved if a['jobTitle'] == 'Autofilled Engineer')
                assert autofilled['linkedInJobId'] == '123'
                assert autofilled['publishedAt'] == '2026-10-01'
                assert autofilled['applyUrl'] == 'https://example.com/apply'
                assert autofilled['jobDescription'] == 'Full posting\nPython'
                page.request.delete('http://localhost:8001/api/applications/' + autofilled['id'])
                page.reload()
                page.locator('header button[onclick="openNewModal()"]').click()
                page.locator('#quick-url-input').fill('https://linkedin.com/jobs/view/999/')
                page.get_by_role('button', name='Autofill', exact=True).click()
                expect(page.locator('#posting-extraction-result')).to_contain_text('blocked')
                expect(page.get_by_role('button', name='Autofill', exact=True)).to_be_enabled()
                page.locator('#quick-url-input').fill('https://linkedin.com/jobs/view/200/')
                page.get_by_role('button', name='Autofill', exact=True).click()
                page.locator('#quick-url-input').fill('https://linkedin.com/jobs/view/201/')
                page.wait_for_timeout(700)
                expect(page.locator('#field-title')).to_have_value('')
                page.get_by_role('button', name='Autofill', exact=True).click()
                expect(page.locator('#field-title')).to_have_value('Autofilled Engineer')
                page.locator('#quick-url-input').fill('https://linkedin.com/jobs/view/200/')
                page.locator('#field-title').fill('')
                page.get_by_role('button', name='Autofill', exact=True).click()
                page.locator('#field-title').fill('Typed while loading')
                expect(page.locator('#posting-extraction-result')).to_contain_text('Review before saving')
                expect(page.locator('#field-title')).to_have_value('Typed while loading')
                page.locator('#quick-url-input').fill('https://linkedin.com/jobs/view/200/')
                page.get_by_role('button', name='Autofill', exact=True).click()
                page.locator('#app-modal button[onclick="closeModal()"]').first.click()
                page.locator('header button[onclick="openNewModal()"]').click()
                page.wait_for_timeout(700)
                expect(page.locator('#field-title')).to_have_value('')
                page.locator('#posting-text').fill('Company: Text Co\nTitle: Text Engineer\nLocation: Wellington\nRemote\nAbout the job\nBuild software')
                page.get_by_role('button', name='Extract Details', exact=True).click()
                expect(page.locator('#field-title')).to_have_value('Text Engineer')
                expect(page.locator('#field-description')).to_have_value('Company: Text Co\nTitle: Text Engineer\nLocation: Wellington\nRemote\nAbout the job\nBuild software')
                expect(page.locator('#field-notes')).to_have_value('')
                page.locator('#app-modal button[onclick="closeModal()"]').first.click()
                page.locator('header button[onclick="openNewModal()"]').click()
                page.locator("#field-title").fill("Manual role")
                page.locator("#field-company").fill("Manual company")
                page.locator("#field-description").fill("Manual description")
                resume_link = 'https://drive.google.com/file/d/resume/view?note=a,b#submitted'
                cover_link = 'https://docs.google.com/document/d/letter/edit'
                page.locator('#field-resume').fill(resume_link)
                page.locator('#field-coverletter').fill(cover_link)
                page.locator("#job-form [type=submit]").click()
                expect(page.locator("#app-modal")).to_have_class(__import__('re').compile(r".*hidden.*"))
                page.locator("#tab-sheet").click()
                expect(page.locator("#sheet-table-body")).to_contain_text("Manual role")
                with page.expect_download() as download:
                    page.get_by_role("button", name="JSON Backup", exact=True).click()
                backup=json.loads(Path(download.value.path()).read_text())
                assert len(backup["applications"])==4
                assert any(a["jobDescription"]=="Full posting for Python engineer" for a in backup["applications"])
                manual = next(a for a in backup['applications'] if a['jobTitle'] == 'Manual role')
                assert manual['resumeUrl'] == resume_link and manual['coverLetterUrl'] == cover_link
                with page.expect_download() as csv_download:
                    page.locator('button[onclick="exportToCSV()"]').click()
                csv_rows = list(csv.DictReader(io.StringIO(Path(csv_download.value.path()).read_text())))
                csv_manual = next(row for row in csv_rows if row['Job Title'] == 'Manual role')
                assert csv_manual['Resume used'] == resume_link
                assert csv_manual['Cover letter used'] == cover_link
                page.locator("#import-json").set_input_files({"name":"backup.json", "mimeType":"application/json", "buffer":json.dumps(backup).encode()})
                expect(page.locator("#toast-container")).to_contain_text("0 imported, 4 skipped")
                imported_row=page.locator("#sheet-table-body tr").filter(has_text="Engineer 00")
                imported_row.locator('[title="Edit row"]').click()
                expect(page.locator("#field-worktype")).to_have_value("")
                expect(page.locator("#field-description")).to_have_value("Full posting for Python engineer")
                page.locator("#field-notes").fill("Personal follow-up")
                page.locator("#job-form [type=submit]").click()
                expect(page.locator("#app-modal")).to_have_class(__import__('re').compile(r".*hidden.*"))
                imported_row.locator('select').select_option("Applied")
                expect(imported_row.locator('select')).to_have_value("Applied")
                imported_row.locator('[title="View details"]').click()
                expect(page.locator("#detail-content")).to_contain_text("Full posting for Python engineer")
                expect(page.locator("#detail-content")).to_contain_text("Personal follow-up")
                page.locator('button[onclick="closeDetailsModal()"]').click()
                manual_row = page.locator('#sheet-table-body tr').filter(has_text='Manual role')
                manual_row.locator('[title="View details"]').click()
                docs = page.locator('#application-documents')
                expect(docs.get_by_role('link', name='Open resume used')).to_have_attribute('href', resume_link)
                expect(docs.get_by_role('link', name='Open cover letter used')).to_have_attribute('target', '_blank')
                docs.get_by_role('button', name='Edit links').click()
                page.locator('#document-resumeUrl').fill('https://example.com/discarded')
                docs.get_by_role('button', name='Cancel', exact=True).click()
                expect(docs.get_by_role('link', name='Open resume used')).to_have_attribute('href', resume_link)
                docs.get_by_role('button', name='Edit links').click()
                page.locator('#document-resumeUrl').fill('javascript:alert(1)')
                docs.get_by_role('button', name='Save', exact=True).click()
                expect(docs.locator('[data-document-feedback]')).to_contain_text('valid http or https')
                expect(page.locator('#detail-content')).to_contain_text('Manual description')
                page.locator('#document-resumeUrl').fill('https://example.com/submitted.pdf')
                page.locator('#document-coverLetterUrl').fill('')
                docs.get_by_role('button', name='Save', exact=True).click()
                expect(docs.get_by_role('link', name='Open resume used')).to_have_attribute('href', 'https://example.com/submitted.pdf')
                expect(docs).to_contain_text('Not added')
                expect(page.locator('#details-modal')).to_be_visible()
                expect(page.locator('#detail-content')).to_contain_text('Manual description')
                page.set_viewport_size({'width':390, 'height':844})
                docs.get_by_role('button', name='Edit links').click()
                page.screenshot(path='/private/tmp/careerforge-documents-mobile.png', full_page=True)
                assert page.locator('#details-modal').evaluate('(element) => element.scrollWidth <= element.clientWidth')
                docs.get_by_role('button', name='Cancel', exact=True).click()
                page.set_viewport_size({'width':1440, 'height':1000})
                page.locator('button[onclick="closeDetailsModal()"]').click()
                manual_row.locator('[title="Edit row"]').click()
                expect(page.locator('#field-resume')).to_have_value('https://example.com/submitted.pdf')
                expect(page.locator('#field-coverletter')).to_have_value('')
                page.locator('#app-modal button[onclick="closeModal()"]').first.click()
                page.reload()
                page.locator('#tab-sheet').click()
                manual_row.locator('[title="View details"]').click()
                expect(page.locator('#application-documents').get_by_role('link', name='Open resume used')).to_have_attribute('href', 'https://example.com/submitted.pdf')
                page.locator('button[onclick="closeDetailsModal()"]').click()
                page.locator("#sheet-table-body tr").filter(has_text="Manual role").locator('[title="Delete"]').click()
                expect(page.locator("#sheet-table-body")).not_to_contain_text("Manual role")
                page.get_by_role("button", name="Discover Jobs", exact=True).click()
                page.locator("#search-mode").select_option("urls")
                expect(page.locator("#search-url-fields")).to_be_visible()
                expect(page.locator("#search-form-fields")).to_be_hidden()
                expect(page.locator("#tab-sheet")).not_to_have_class(__import__('re').compile(r".*bg-white.*"))
                page.wait_for_timeout(400)  # Let existing tab hover/transition styles settle for visual QA.
                page.screenshot(path="/private/tmp/careerforge-desktop.png")
                page.set_viewport_size({"width":390,"height":844})
                page.screenshot(path="/private/tmp/careerforge-mobile.png", full_page=True)
                legacy = browser.new_page(viewport={'width':1440, 'height':1000})
                legacy.on('pageerror', lambda error: errors.append(str(error)))
                legacy.goto((Path(__file__).resolve().parent.parent / 'job_application_tracker.html').as_uri())
                legacy.locator('header button[onclick="openNewModal()"]').click()
                legacy.locator('#field-title').fill('Standalone documents')
                legacy.locator('#field-company').fill('Legacy Co')
                legacy.locator('#field-resume').fill(resume_link)
                legacy.locator('#field-coverletter').fill(cover_link)
                legacy.locator('#job-form [type=submit]').click()
                legacy.reload()
                legacy.locator('#tab-sheet').click()
                legacy_row = legacy.locator('#sheet-table-body tr').filter(has_text='Standalone documents')
                legacy_row.locator('[title="View details"]').click()
                legacy_docs = legacy.locator('#application-documents')
                expect(legacy_docs.get_by_role('link', name='Open resume used')).to_have_attribute('href', resume_link)
                legacy_docs.get_by_role('button', name='Edit links').click()
                legacy.locator('#document-coverLetterUrl').fill('')
                legacy_docs.get_by_role('button', name='Save', exact=True).click()
                expect(legacy_docs).to_contain_text('Not added')
                legacy.reload()
                legacy.locator('#tab-sheet').click()
                legacy_row.locator('[title="Edit row"]').click()
                expect(legacy.locator('#field-resume')).to_have_value(resume_link)
                expect(legacy.locator('#field-coverletter')).to_have_value('')
                legacy.close()
                assert not errors, errors
                browser.close()
                print("UI smoke passed: document links, inline editing, CSV links, autofill, edit preservation, stale responses, fallback, metadata persistence, search, paging, import, CRUD, JSON roundtrip, and mobile layout")
        finally:
            instance.should_exit=True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
