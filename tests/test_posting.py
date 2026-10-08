import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import posting
import server

URL = 'https://www.linkedin.com/jobs/view/123/'
HTML = '''<h1 class="top-card-layout__title">Engineer</h1>
<a class="topcard__org-name-link">Acme</a><span class="topcard__flavor--bullet">Auckland</span>
<div class="show-more-less-html__markup"><p>Build <b>great</b> things.</p><ul><li>Python</li><li>SQL</li></ul></div>'''


@pytest.mark.parametrize('url', [URL, 'https://nz.linkedin.com/jobs/view/engineer-at-acme-123/?trk=x', 'https://www.linkedin.com/jobs/search/?keywords=python&currentJobId=123'])
def test_canonical_url(url):
    assert posting.posting_url(url) == (URL, '123')


@pytest.mark.parametrize('url', [None, 'javascript:alert(1)', 'https://linkedin.com.evil.com/jobs/view/123', 'https://example.com/jobs/view/123', 'https://linkedin.com/jobs/search', 'https://linkedin.com/jobs/view/abc', 'https://user@linkedin.com/jobs/view/123', 'https://linkedin.com:9000/jobs/view/123'])
def test_invalid_url(url):
    with pytest.raises(HTTPException) as error:
        posting.posting_url(url)
    assert error.value.status_code == 422


def test_structured_and_html_fallback():
    data = {'@graph': [{'@type': 'JobPosting', 'title': 'Senior Engineer', 'identifier': {'value': '123'},
        'hiringOrganization': {'name': 'Acme', 'sameAs': 'https://acme.example'},
        'jobLocation': {'address': {'addressLocality': 'Auckland', 'addressCountry': {'name': 'NZ'}}},
        'baseSalary': {'currency': 'NZD', 'value': {'minValue': 100000, 'maxValue': 120000, 'unitText': 'YEAR'}},
        'jobLocationType': 'TELECOMMUTE', 'datePosted': '2026-10-01'}]}
    result = posting.parse_posting('<script type="application/ld+json">'+json.dumps(data)+'</script>'+HTML, URL, '123')
    details = result['details']
    assert details['jobTitle'] == 'Senior Engineer'
    assert details['companyWebsite'] == 'https://acme.example'
    assert details['location'] == 'Auckland, NZ'
    assert details['salary'] == 'NZD 100000 – 120000 YEAR'
    assert details['workType'] == 'Remote'
    assert details['jobDescription'] == 'Build great things.\nPython\nSQL'
    assert details['publishedAt'] == '2026-10-01'
    assert not result['warnings']


def test_malformed_json_and_partial_html():
    result = posting.parse_posting('<script type="application/ld+json">{bad}</script>'+HTML, URL, '123')
    assert result['details']['company'] == 'Acme'
    assert 'workType' not in result['details']
    assert 'salary' not in result['details']
    assert result['warnings']
    partial = posting.parse_posting('<h1 class="topcard__title">Engineer</h1>', URL, '123')
    assert 'company' not in partial['details']


def test_explicit_header_arrangement_only():
    html = '<div class="top-card-layout"><h1 class="topcard__title">Engineer</h1><span>Auckland (Hybrid)</span></div>'
    assert posting.parse_posting(html, URL, '123')['details']['workType'] == 'Hybrid'
    result = posting.parse_posting(HTML.replace('Build', 'Remote teams build'), URL, '123')
    assert 'workType' not in result['details']


@pytest.mark.parametrize('html', ['<h1>Sign in to LinkedIn</h1>', '<html></html>', '<div class="base-main-card__title">Recommended person</div>'])
def test_login_and_empty_pages(html):
    with pytest.raises(HTTPException) as error:
        posting.parse_posting(html, URL, '123')
    assert error.value.status_code == 502


def test_preview_does_not_save_and_metadata_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DB_PATH', str(tmp_path / 'test.sqlite3'))
    monkeypatch.setattr(posting, 'fetch_posting', AsyncMock(return_value=HTML))
    with TestClient(server.app) as client:
        result = client.post('/api/posting-preview', json={'url': URL})
        assert result.status_code == 200
        assert client.get('/api/applications').json() == []
        item = client.post('/api/applications', json={**result.json()['details'], 'notes': 'My notes'}).json()
        assert item['linkedInJobId'] == '123'
        assert item['jobDescription'] == 'Build great things.\nPython\nSQL'
        assert item['notes'] == 'My notes'
        assert client.get('/api/applications').json()[0] == item


@pytest.mark.parametrize('error,status', [(httpx.ReadTimeout('timeout'), 504), (httpx.ConnectError('network'), 502)])
def test_preview_network_errors(monkeypatch, error, status):
    monkeypatch.setattr(posting, 'fetch_posting', AsyncMock(side_effect=error))
    with pytest.raises(HTTPException) as caught:
        import asyncio
        asyncio.run(posting.preview(URL))
    assert caught.value.status_code == status


@pytest.mark.parametrize('status,expected', [(403, 502), (429, 502), (999, 502), (404, 404), (410, 404), (500, 502)])
def test_upstream_status(monkeypatch, status, expected):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text='error'))
    monkeypatch.setattr(posting.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    import asyncio
    with pytest.raises(HTTPException) as error:
        asyncio.run(posting.fetch_posting(URL))
    assert error.value.status_code == expected


@pytest.mark.parametrize('destination', ['https://evil.example/jobs/view/123/', 'https://www.linkedin.com/login', 'http://www.linkedin.com/jobs/view/123/', 'https://www.linkedin.com:9000/jobs/view/123/', 'https://www.linkedin.com/jobs/view/456/', 'https://www.linkedin.com:bad/jobs/view/123/'])
def test_redirect_rejected_without_fetching_destination(monkeypatch, destination):
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location': destination})
    original = httpx.AsyncClient
    monkeypatch.setattr(posting.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    import asyncio
    with pytest.raises(HTTPException):
        asyncio.run(posting.preview(URL))
    assert calls == [URL]


def test_size_limit_and_safe_redirect(monkeypatch):
    calls = []
    def respond(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={'location': '/jobs/view/engineer-at-acme-123/'})
        return httpx.Response(200, text=HTML)
    original = httpx.AsyncClient
    monkeypatch.setattr(posting.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    import asyncio
    assert asyncio.run(posting.fetch_posting(URL)) == HTML
    monkeypatch.setattr(posting, 'MAX_BYTES', 10)
    with pytest.raises(HTTPException):
        asyncio.run(posting.fetch_posting(URL))


def test_unrelated_structured_posting_and_closed_warning():
    unrelated = '<script type="application/ld+json">' + json.dumps({'@type': 'JobPosting', 'identifier': '1234', 'title': 'Wrong job'}) + '</script>'
    with pytest.raises(HTTPException):
        posting.parse_posting(unrelated + HTML, URL, '123')
    result = posting.parse_posting(HTML + '<span>No longer accepting applications</span>', URL, '123')
    assert any('no longer accepting' in warning for warning in result['warnings'])


def test_overall_deadline(monkeypatch):
    import asyncio
    async def slow(url):
        await asyncio.sleep(1)
    wait_for = asyncio.wait_for
    monkeypatch.setattr(posting, 'fetch_posting', slow)
    monkeypatch.setattr(posting.asyncio, 'wait_for', lambda task, timeout: wait_for(task, timeout=.001))
    with pytest.raises(HTTPException) as error:
        asyncio.run(posting.preview(URL))
    assert error.value.status_code == 504


@pytest.mark.parametrize('website', ['https://www.linkedin.com/company/acme', 'https://[invalid'])
def test_company_profile_is_not_company_website(website):
    html = '<script type="application/ld+json">' + json.dumps({'@type': 'JobPosting', 'title': 'Engineer', 'hiringOrganization': {'name': 'Acme', 'sameAs': website}}) + '</script>'
    assert 'companyWebsite' not in posting.parse_posting(html, URL, '123')['details']
