"""Bounded public LinkedIn posting previews; never writes tracker data."""
import asyncio
import json
import re
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException

MAX_BYTES = 2 * 1024 * 1024


def posting_url(value):
    if not isinstance(value, str):
        raise HTTPException(422, "Enter a LinkedIn job posting URL")
    p = urlparse(value.strip())
    if (p.scheme not in ("http", "https") or not p.hostname
            or not (p.hostname == "linkedin.com" or p.hostname.endswith(".linkedin.com"))
            or p.username or p.password or p.port not in (None, 80, 443)):
        raise HTTPException(422, "Use a LinkedIn job posting URL")
    match = re.fullmatch(r"/jobs/view/(?:[^/]*-)?(\d+)/?", p.path)
    job_id = match.group(1) if match else ""
    if not job_id and p.path.rstrip("/") == "/jobs/search":
        candidate = parse_qs(p.query).get("currentJobId", [""])[0]
        if re.fullmatch(r"\d+", candidate):
            job_id = candidate
    if not job_id:
        raise HTTPException(422, "Use a /jobs/view link or a search link with a selected job")
    return "https://www.linkedin.com/jobs/view/" + job_id + "/", job_id


def plain_text(value):
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(["br", "p", "div", "li", "h1", "h2", "h3"]):
        if tag.name == "br":
            tag.replace_with("\n")
        else:
            tag.insert_before("\n")
            tag.insert_after("\n")
    return "\n".join(line.strip() for line in soup.get_text().splitlines() if line.strip())


def valid_link(value):
    if not isinstance(value, str):
        return ""
    try:
        p = urlparse(value)
    except ValueError:
        return ""
    return value if p.scheme in ("http", "https") and p.hostname and not p.username and not p.password else ""


def parse_posting(html, url, job_id):
    soup = BeautifulSoup(html, "html.parser")
    details = {"url": url, "linkedInJobId": job_id, "source": "LinkedIn"}

    def nodes(value):
        if isinstance(value, list):
            for item in value:
                yield from nodes(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                yield value
            yield from nodes(value.get("@graph", []))

    postings = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            postings.extend(nodes(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    def explicit_id(item):
        identifier = item.get('identifier', '')
        if isinstance(identifier, dict):
            identifier = identifier.get('value', '')
        if re.fullmatch(r'\d+', str(identifier)):
            return str(identifier)
        try:
            return posting_url(item.get('url', ''))[1]
        except (HTTPException, ValueError):
            return ''
    matching = [item for item in postings if explicit_id(item) == job_id]
    data = matching[0] if matching else postings[0] if len(postings) == 1 and not explicit_id(postings[0]) else {}
    if postings and not data and any(explicit_id(item) for item in postings):
        raise HTTPException(502, 'LinkedIn returned a different posting. Try again or paste the posting text.')
    for source, target in (("title", "jobTitle"), ("description", "jobDescription"), ("datePosted", "publishedAt")):
        if isinstance(data.get(source), str):
            details[target] = plain_text(data[source])
    org = data.get("hiringOrganization") or {}
    if isinstance(org, dict):
        details["company"] = plain_text(org.get("name"))
        website = valid_link(org.get("url")) or valid_link(org.get("sameAs"))
        host = urlparse(website).hostname or ""
        if host != "linkedin.com" and not host.endswith(".linkedin.com"):
            details["companyWebsite"] = website
    locations = data.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    if isinstance(locations, list):
        values = []
        for place in locations:
            address = place.get("address", {}) if isinstance(place, dict) else {}
            if isinstance(address, str):
                values.append(plain_text(address))
            elif isinstance(address, dict):
                parts = [address.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
                parts = [v.get("name", "") if isinstance(v, dict) else v for v in parts]
                values.append(", ".join(dict.fromkeys(str(v) for v in parts if v)))
        details["location"] = " / ".join(dict.fromkeys(v for v in values if v))
    if data.get("jobLocationType") == "TELECOMMUTE":
        details["workType"] = "Remote"
    salary = data.get("baseSalary")
    if isinstance(salary, dict):
        value = salary.get("value", {})
        currency = salary.get("currency", "")
        if isinstance(value, dict):
            amounts = [value.get("minValue"), value.get("maxValue")]
            amounts = [str(v) for v in amounts if isinstance(v, (str, int, float))]
            amount = " – ".join(amounts) or str(value.get("value", ""))
            unit = value.get("unitText", "")
        else:
            amount, unit = str(value or ""), ""
        if amount:
            details["salary"] = " ".join(str(v) for v in (currency, amount, unit) if v)
    elif isinstance(salary, (int, float, str)):
        details["salary"] = " ".join(str(v) for v in (data.get("salaryCurrency", ""), salary) if v)

    selectors = {
        "jobTitle": "h1.top-card-layout__title, h1.topcard__title",
        "company": ".topcard__org-name-link, .topcard__flavor-row .topcard__flavor:first-child",
        "location": ".topcard__flavor--bullet",
        "jobDescription": ".show-more-less-html__markup, .description__text",
        "salary": ".compensation__salary, .salary.compensation__salary",
        "contact": ".message-the-recruiter .base-main-card__title, .hirer-card__hirer-name",
    }
    for key, selector in selectors.items():
        tag = soup.select_one(selector)
        if not details.get(key) and tag:
            details[key] = plain_text(str(tag))
    title_tag = soup.select_one("h1.top-card-layout__title, h1.topcard__title")
    if not details.get("workType") and title_tag:
        header = soup.select_one(".top-card-layout, .topcard")
        if header:
            match = re.search(r"\((Remote|Hybrid|On[- ]?site)\)|\bWorkplace type\s*:\s*(Remote|Hybrid|On[- ]?site)\b", header.get_text(" ", strip=True), re.I)
            if match:
                mode = (match.group(1) or match.group(2)).lower()
                details["workType"] = "Remote" if mode == "remote" else "Hybrid" if mode == "hybrid" else "Onsite"
    if not any(details.get(key) for key in ("jobTitle", "company", "jobDescription")):
        raise HTTPException(502, "LinkedIn did not provide posting details. Paste the posting text and use Extract Details.")
    apply = soup.select_one('a.apply-button[href], a.topcard__apply-link[href]')
    if apply:
        details['applyUrl'] = valid_link(urljoin(url, apply.get('href', '')))
    missing = [label for key, label in (("location", "location"), ("salary", "salary"), ("workType", "work arrangement"), ("jobDescription", "description")) if not details.get(key)]
    warnings = ["Unavailable: " + ", ".join(missing) + "."] if missing else []
    if not details.get("jobTitle") or not details.get("company"):
        warnings.append("Some posting details were unavailable. Review the fields before saving.")
    if 'no longer accepting applications' in soup.get_text(' ', strip=True).lower():
        warnings.append('This posting is no longer accepting applications.')
    return {"details": {k: v for k, v in details.items() if v}, "warnings": warnings}


async def fetch_posting(url):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, headers={"User-Agent": "CareerForge/1.0 (public job preview)", "Accept": "text/html"}) as client:
        for _ in range(4):
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    destination = urljoin(url, response.headers.get("location", ""))
                    try:
                        p = urlparse(destination)
                        port = p.port
                        redirect_id = posting_url(destination)[1]
                    except (ValueError, HTTPException):
                        raise HTTPException(502, 'LinkedIn redirected away from the posting. Paste its text instead.')
                    if (redirect_id != posting_url(url)[1] or p.scheme != "https" or p.hostname not in ("www.linkedin.com", "linkedin.com")
                            or p.username or p.password or port not in (None, 443)
                            or not p.path.startswith("/jobs/view/")):
                        raise HTTPException(502, "LinkedIn requires sign-in or redirected away from the posting. Paste its text instead.")
                    url = destination
                    continue
                if response.status_code in (404, 410):
                    raise HTTPException(404, "This posting is no longer available. Paste its text if you still have it.")
                if response.status_code in (401, 403, 429, 999):
                    raise HTTPException(502, "LinkedIn blocked or limited this lookup. Paste the posting text and use Extract Details.")
                if response.status_code != 200:
                    raise HTTPException(502, "Could not retrieve the LinkedIn posting. Try again or paste its text.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise HTTPException(502, "The posting response was too large. Paste the posting text instead.")
                return bytes(content).decode(response.encoding or "utf-8", errors="replace")
    raise HTTPException(502, "LinkedIn redirected too many times. Paste the posting text instead.")


async def preview(value):
    try:
        url, job_id = posting_url(value)
    except ValueError:
        raise HTTPException(422, "Enter a valid LinkedIn job posting URL")
    try:
        html = await asyncio.wait_for(fetch_posting(url), timeout=15)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, "LinkedIn lookup timed out. Try again or paste the posting text.")
    except httpx.RequestError:
        raise HTTPException(502, "Could not connect to LinkedIn. Try again or paste the posting text.")
    return parse_posting(html, url, job_id)
