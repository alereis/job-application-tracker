# CareerForge

Local job application tracker with LinkedIn job discovery through Apify. The interface and SQLite database run on your desktop; the actor runs in your Apify account. Existing Kanban, Data Sheet, dashboard, CSV export, and manual Google integration remain available.

## Run locally

Requires Python 3.9 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `APIFY_TOKEN` in `.env` to your Apify API token. Never commit this file. You can use the tracker without a token; searches require one. Restart the server after changing configuration.

```sh
uvicorn server:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000. Keep this origin and port consistent. The default database is `data/careerforge.sqlite3`.

## Run with Docker

Requires Docker Desktop running. Create `.env` as above, then:

```sh
docker compose up --build -d
```

Open http://localhost:8000. A named volume retains SQLite data through container rebuilds and restarts. `docker compose down` retains it; `docker compose down -v` deletes it.

## Autofill a LinkedIn posting

In **Add New Application**, paste a LinkedIn job link and click **Autofill**. The local server retrieves the public posting and fills empty fields, including its description. Review the details and save. Existing entries, personal notes, stage, and date are preserved. Numeric links, title-based links, regional LinkedIn domains, and search URLs containing a selected `currentJobId` are supported.

Autofill does not use Apify or incur scraper charges. LinkedIn may block public access, limit requests, or remove a posting. If lookup fails, copy the posting header and description into **Paste job posting text** and click **Extract Details**. Missing salary and work arrangement remain unspecified. Other job sites and the standalone HTML file offer link parsing and copied-text extraction only; use localhost for LinkedIn lookup.

## Discover and import

1. Open **Discover Jobs**. Enter keywords and locations, one per line, or paste LinkedIn `/jobs/search` URLs.
2. Choose filters and a maximum result count (150 minimum). Each keyword/location pair generates a search. URL mode uses filters encoded in the URLs.
3. Click **Run Search**. Follow status, cancel a running actor, or open its Apify Console page. The default spending ceiling is **USD 1 per run**, configurable through `APIFY_MAX_CHARGE_USD`. Charges include actor startup and results; a spending ceiling can end a run before the requested count is reached.
4. Review results and descriptions. Select individual jobs or the available jobs on the current page. Selection carries across pages within that search.
5. Choose the import stage (default **Wishlist**) and click **Import selected**. Existing postings are skipped without overwriting your changes.

Descriptions are stored separately from personal notes. Missing salary, company website, and work arrangement remain unspecified. The actor's `workType` output is a job function, not a remote/hybrid/on-site classification. Company enrichment is optional and slower.

Search history and downloaded results are retained locally. Closing the page does not stop the actor. Restarting the server resumes known active runs. **Resume monitoring / download** retries a known run without starting a new one. If a start request was interrupted before the actor run ID arrived, check Apify Console before starting another search; the app deliberately does not repeat that request automatically. Failed, aborted, or timed-out actors can still have partial results.

The actor ID is configurable with `APIFY_ACTOR_ID`, but the input/output adapter targets `cheap_scraper/linkedin-job-scraper`; unrelated actors require code changes. There is no scheduler or automatic Google sync.

## Migrate existing records and make backups

Open the original HTML file in the browser where your records are stored. In **Data Sheet**, click **JSON Backup**. In the local server app, use **Import JSON backup / legacy data** at the bottom of the page. IDs, personal notes, posting details, and Google sync URL are retained. Imports merge records and skip existing IDs/postings, so repeating an import is safe. Your original browser data is left intact.

If legacy records are accessible under the same browser origin, **Import existing browser records** appears automatically. Browser storage for a file opened from disk cannot be read from localhost.

Use **Export JSON backup** for portable tracker records and Google sync settings. JSON does not include search history or the Apify token. To back up the complete local database including search history while the app is running:

```sh
python3 -c "import sqlite3; source=sqlite3.connect('data/careerforge.sqlite3'); target=sqlite3.connect('careerforge-backup.sqlite3'); source.backup(target); target.close(); source.close()"
```

For Docker, create a consistent database backup inside the container and copy it out:

```sh
docker compose exec careerforge python -c "import sqlite3; source=sqlite3.connect('/app/data/careerforge.sqlite3'); target=sqlite3.connect('/tmp/careerforge-backup.sqlite3'); source.backup(target); target.close(); source.close()"
docker compose cp careerforge:/tmp/careerforge-backup.sqlite3 ./careerforge-backup.sqlite3
```

Restore a full backup with the server stopped by replacing its database and removing stale `-wal`/`-shm` sidecars. For Docker, stop the service, then copy the backup using `docker compose run --rm --no-deps -v "$PWD/careerforge-backup.sqlite3:/backup.sqlite3:ro" careerforge sh -c 'cp /backup.sqlite3 /app/data/careerforge.sqlite3 && rm -f /app/data/careerforge.sqlite3-wal /app/data/careerforge.sqlite3-shm'`, and start the service again. Keep the backup outside the data volume.

## Application document links

Add **Resume used** and **Cover letter used** links in the application form, or open an application's details and select **Edit links** under **Application documents**. Save updates the links without changing other application details. Clear an input to remove a link; Cancel discards edits. Open launches the document in a new tab.

Use separate copies or PDFs of the exact documents submitted for each application. Private Google Drive links are supported; the tracker does not change access permissions or download the files. HTTP/HTTPS links to other document hosts are also accepted. Links are included in JSON backups, CSV exports, and Google Sheets sync; the documents themselves remain separately stored. Older Sheets without the new columns preserve local links for matching application IDs. Redeploy the updated Apps Script to sync the new columns. Docker users rebuild with `docker compose up --build -d`; the existing data volume retains records.

## Google Sheets and Docs

Replace the deployed Apps Script with `google_apps_script.gs` and deploy a new version. The embedded setup guide contains the same script. Updated Sheets include description, LinkedIn job ID, published date, and application URL columns. Older sheets are accepted; posting details missing from older columns are preserved locally for matching application IDs. Pull remains an explicit replacement of tracker records and asks for confirmation, including when the sheet is empty. Push is manual; its existing browser `no-cors` mode cannot verify whether the remote write succeeded.

## Development checks

```sh
pip install pytest
python -m pytest -q
```

Tests use temporary databases and mocked Apify responses. They do not start paid actors. A live smoke test requires your token: run a small search, inspect a posting, import it, reload, and verify the record and description remain. The UI uses its existing external Tailwind, Font Awesome, and Google Fonts resources, so full styling requires internet access.

Optional real-browser smoke test (uses a fake actor and temporary database):

```sh
pip install playwright
playwright install chromium
PYTHONPATH=. python tests/ui_smoke.py
```

This starts a temporary test server on port 8001 and checks search, descriptions, multi-page selection, import, CRUD, reload persistence, JSON migration, and mobile layout. It does not access your real database or Apify account.
