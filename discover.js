/* CareerForge local server integration. The original HTML still supports legacy export. */
if (location.protocol !== 'file:') {
  let discoveryRun = null, discoveryPage = 1, discoveryItems = [], discoveryTotal = 0;
  let selectedResults = new Set(), discoveryTimer, searchDebounce, discoveryRequest = 0;
  const activeStatuses = ['STARTING', 'READY', 'RUNNING', 'TIMING-OUT', 'ABORTING', 'DOWNLOADING'];
  let backendReady = false, apifyConfigured = false, anyActiveRun = false;
  const E = id => document.getElementById(id);
  const esc = value => escapeHtml(String(value ?? ''));
  const buttonClass = 'px-3 py-2 rounded-xl bg-indigo-600 text-white text-sm font-semibold disabled:opacity-40';
  const inputClass = 'w-full border border-slate-200 rounded-xl px-3 py-2 bg-white text-sm';
  const localDate = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };

  async function api(path, options = {}) {
    const response = await fetch('/api' + path, { ...options, headers: {'Content-Type': 'application/json', ...options.headers} });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'The server rejected this request. Check the input.');
    return body;
  }
  const send = (path, body, method = 'POST') => api(path, {method, body: JSON.stringify(body)});
  function refreshTracker() { renderKanban(); renderSheet(); renderDashboard(); }
  async function reloadTracker() { applications = await api('/applications'); refreshTracker(); }
  function report(error) { showToast(error.message || String(error), 'error'); }
  function requireReady() { if (!backendReady) throw new Error('The local database is not connected. Reload the page to reconnect.'); }

  const originalSwitchView = switchView;
  switchView = function(view) {
    originalSwitchView(view);
    E('view-discover').classList.toggle('hidden', view !== 'discover');
    E('tab-discover').className = 'nav-tab px-3 py-1.5 rounded-lg text-xs sm:text-sm font-semibold ' + (view === 'discover' ? 'bg-white text-indigo-600 shadow-sm' : 'text-slate-600');
    if (view === 'discover' && backendReady) loadRuns().catch(report);
  };

  changeStatus = async function(id, stage) {
    try { requireReady(); await send('/applications/' + encodeURIComponent(id), {status: stage}, 'PATCH'); await reloadTracker(); showToast('Moved application to ' + stage); }
    catch (e) { report(e); refreshTracker(); }
  };
  deleteJob = async function(id) {
    try { requireReady(); await api('/applications/' + encodeURIComponent(id), {method: 'DELETE'}); await reloadTracker(); closeDetailsModal(); showToast('Application removed'); }
    catch (e) { report(e); }
  };
  handleFormSubmit = async function(event) {
    event.preventDefault();
    const fieldMap = {jobTitle:'title', company:'company', status:'status', dateApplied:'date', location:'location', workType:'worktype', salary:'salary', source:'source', url:'url', companyWebsite:'companywebsite', contact:'contact', nextStep:'nextstep', notes:'notes', jobDescription:'description', resumeUrl:'resume', coverLetterUrl:'coverletter'};
    const data = Object.fromEntries(Object.entries(fieldMap).map(([key, id]) => [key, E('field-'+id).value.trim()]));
    Object.assign(data, postingMetadata);
    if (postingMetadata.linkedInJobId && linkedInJobId(data.url) !== postingMetadata.linkedInJobId) {
      for (const key of ['linkedInJobId', 'publishedAt', 'applyUrl']) data[key] = '';
    }
    const id = E('field-id').value;
    const submit = E('job-form').querySelector('[type=submit]');
    try {
      requireReady(); if (submit) submit.disabled = true;
      await send(id ? '/applications/'+encodeURIComponent(id) : '/applications', data, id ? 'PATCH' : 'POST');
      await reloadTracker(); closeModal(); showToast(id ? 'Application updated' : 'Application added');
    } catch (e) { report(e); }
    finally { if (submit) submit.disabled = false; }
  };
  function linkedInJobId(url) {
    try {
      const parsed = new URL(url);
      if (parsed.hostname === 'linkedin.com' || parsed.hostname.endsWith('.linkedin.com')) {
        return parsed.pathname.match(/\/jobs\/view\/(?:[^/]*-)?(\d+)\/?$/)?.[1] || parsed.searchParams.get('currentJobId') || '';
      }
    } catch {}
    return '';
  }
  let postingMetadata = {}, postingGeneration = 0, postingController = null;
  function invalidatePostingLookup() {
    postingGeneration++;
    if (postingController) postingController.abort();
    postingController = null;
    const button = E('posting-autofill');
    if (button) { button.disabled = false; button.textContent = 'Autofill'; }
  }
  const originalEdit = openEditModal, originalNew = openNewModal, originalClose = closeModal;
  openEditModal = id => {
    invalidatePostingLookup(); originalEdit(id);
    const app = applications.find(a => a.id === id);
    postingMetadata = {};
    if (app) {
      E('field-description').value = app.jobDescription || ''; E('field-worktype').value = app.workType || '';
      for (const key of ['linkedInJobId', 'publishedAt', 'applyUrl']) if (app[key]) postingMetadata[key] = app[key];
    }
  };
  openNewModal = () => {
    invalidatePostingLookup(); postingMetadata = {}; originalNew();
    E('field-description').value = ''; E('field-worktype').value = ''; E('field-source').value = '';
    E('field-date').value = localDate();
  };
  closeModal = () => { invalidatePostingLookup(); originalClose(); };

  autoParseUrl = async function() {
    const url = E('quick-url-input').value.trim();
    const feedback = E('posting-extraction-result');
    let parsed;
    try { parsed = new URL(url); if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error(); }
    catch { feedback.textContent = 'Enter a valid http or https posting link.'; return; }
    const linkedIn = parsed.hostname === 'linkedin.com' || parsed.hostname.endsWith('.linkedin.com');
    if (!linkedIn) {
      if (!E('field-url').value.trim()) E('field-url').value = url;
      if (!E('field-source').value) E('field-source').value = /(^|\.)indeed\.com$/.test(parsed.hostname) ? 'Indeed' : /(^|\.)(wellfound\.com|angel\.co)$/.test(parsed.hostname) ? 'Wellfound' : 'Company Website';
      feedback.textContent = 'Link recorded. For this site, paste the posting text and use Extract Details.';
      return;
    }
    invalidatePostingLookup();
    const generation = postingGeneration;
    const controller = new AbortController(); postingController = controller;
    const button = E('posting-autofill'); button.disabled = true; button.textContent = 'Loading…';
    feedback.textContent = 'Looking up the LinkedIn posting…';
    try {
      requireReady();
      const result = await sendPreview(url, controller.signal);
      if (generation !== postingGeneration || E('quick-url-input').value.trim() !== url || E('app-modal').classList.contains('hidden')) return;
      const details = result.details, filled = [];
      const map = {jobTitle:'title', company:'company', location:'location', salary:'salary', workType:'worktype', jobDescription:'description', companyWebsite:'companywebsite', contact:'contact', url:'url', source:'source'};
      for (const [key, suffix] of Object.entries(map)) {
        const field = E('field-' + suffix);
        if (details[key] && !field.value.trim()) { field.value = details[key]; filled.push(key === 'workType' ? 'work arrangement' : key === 'jobDescription' ? 'description' : key === 'jobTitle' ? 'title' : key); }
      }
      // Metadata must describe the posting that will actually be saved.
      const savedJobId = linkedInJobId(E('field-url').value);
      if (savedJobId === details.linkedInJobId) {
        for (const key of ['linkedInJobId', 'publishedAt', 'applyUrl']) if (details[key] && !postingMetadata[key]) postingMetadata[key] = details[key];
      }
      feedback.textContent = (filled.length ? 'Filled ' + filled.join(', ') + '. ' : 'No empty fields could be filled. ') + (result.warnings.length ? result.warnings.join(' ') + ' ' : '') + 'Review before saving.';
    } catch (error) {
      if (generation === postingGeneration && error.name !== 'AbortError') feedback.textContent = error.message + ' You can paste the posting text below and use Extract Details.';
    } finally {
      if (generation === postingGeneration) { postingController = null; button.disabled = false; button.textContent = 'Autofill'; }
    }
  };
  async function sendPreview(url, signal) {
    return api('/posting-preview', {method:'POST', body:JSON.stringify({url}), signal});
  }
  saveApplicationDocumentLinks = async function(id, links) {
    requireReady();
    await send('/applications/' + encodeURIComponent(id), links, 'PATCH');
    await reloadTracker();
  };
  const originalDetails = showDetailsModal;
  showDetailsModal = id => {
    originalDetails(id);
    const app = applications.find(a => a.id === id);
    if (app?.jobDescription) E('detail-content').insertAdjacentHTML('beforeend', `<div><span class="text-xs font-semibold text-slate-400 uppercase">Job description</span><div class="whitespace-pre-wrap bg-slate-50 p-3 rounded-xl mt-2">${esc(app.jobDescription)}</div></div>`);
    if (app?.applyUrl) E('detail-content').insertAdjacentHTML('beforeend', `<a class="text-indigo-600 underline" href="${esc(app.applyUrl)}" target="_blank" rel="noopener noreferrer">Application link</a>`);
  };
  const originalBadge = getWorkBadge;
  getWorkBadge = value => value ? originalBadge(value) : '<span class="text-xs text-slate-400">Unspecified</span>';
  // SQLite is authoritative. Legacy handlers that remain must not write browser records.
  persistData = () => { throw new Error('Use the application API to persist changes'); };

  pullFromGoogleSheet = async function() {
    try {
      requireReady(); if (!googleSyncUrl) { openGoogleSyncModal(); return; }
      const response = await fetch(googleSyncUrl);
      if (!response.ok) throw new Error('Could not fetch Google Sheet');
      const result = await response.json();
      if (result.status !== 'success' || !Array.isArray(result.data)) throw new Error('Unexpected Google Sheet response');
      if (!confirm(`Replace the tracker with ${result.data.length} Google Sheet records? Local posting details for matching IDs will be retained.`)) return;
      await send('/applications/bulk', {applications: result.data, replace: true});
      await reloadTracker(); showToast('Google Sheet pulled into the local database');
    } catch(e) { report(e); }
  };
  const originalSaveSync = saveAndTestSyncConnection;
  saveAndTestSyncConnection = async function() {
    if (!backendReady) { report(new Error('Database is not connected')); return; }
    await originalSaveSync();
    try { await send('/settings', {googleSyncUrl}, 'PUT'); }
    catch(e) { report(e); }
  };

  function installUI() {
    document.querySelector('header nav').insertAdjacentHTML('beforeend', '<button id="tab-discover" onclick="switchView(\'discover\')" class="nav-tab px-3 py-1.5 rounded-lg text-xs sm:text-sm text-slate-600">Discover Jobs</button>');
    document.querySelector('header .h-16').classList.add('flex-wrap', 'h-auto', 'min-h-16', 'gap-3', 'py-3');
    E('field-worktype').insertAdjacentHTML('afterbegin', '<option value="">Unspecified</option>');
    E('field-source').insertAdjacentHTML('afterbegin', '<option value="">Unspecified</option>');
    E('posting-autofill').textContent = 'Autofill';
    E('quick-url-input').addEventListener('input', () => { invalidatePostingLookup(); E('posting-extraction-result').textContent = ''; });
    E('field-notes').parentElement.insertAdjacentHTML('afterend', `<div class="sm:col-span-2"><label for="field-description" class="block text-xs font-semibold mb-1">Job description</label><textarea id="field-description" rows="7" class="${inputClass}"></textarea></div>`);
    document.querySelector('main').insertAdjacentHTML('beforeend', `
      <section id="view-discover" class="hidden space-y-5">
        <div><h2 class="text-xl font-bold">Discover LinkedIn Jobs</h2><p class="text-sm text-slate-500 mt-1">Run a search, review postings, and import selected opportunities.</p></div>
        <p id="apify-config" class="text-sm bg-amber-50 p-3 rounded-xl"></p>
        <form id="discovery-form" class="bg-white rounded-2xl border border-slate-200 p-5 space-y-4">
          <div><label for="search-mode" class="text-sm font-semibold">Search method</label><select id="search-mode" class="${inputClass}"><option value="form">Keywords and locations</option><option value="urls">LinkedIn search URLs</option></select></div>
          <div id="search-form-fields" class="grid sm:grid-cols-2 gap-4">
            <div><label for="search-keywords" class="text-sm font-semibold">Keywords — one search per line</label><textarea id="search-keywords" rows="3" class="${inputClass}" placeholder="Software Engineer"></textarea></div>
            <div><label for="search-locations" class="text-sm font-semibold">Locations — one per line (optional)</label><textarea id="search-locations" rows="3" class="${inputClass}" placeholder="Auckland, New Zealand"></textarea></div>
            <div><label for="search-age" class="text-sm font-semibold">Posted within</label><select id="search-age" class="${inputClass}"><option value="">Any time</option><option value="r86400">24 hours</option><option value="r604800">7 days</option><option value="r2592000">30 days</option></select></div>
            <div><label for="search-work" class="text-sm font-semibold">Work arrangement (multiple allowed)</label><select id="search-work" multiple size="3" class="${inputClass}"><option value="remote">Remote</option><option value="hybrid">Hybrid</option><option value="on-site">On-site</option></select></div>
            <div><label for="search-jobtype" class="text-sm font-semibold">Employment type (multiple allowed)</label><select id="search-jobtype" multiple size="3" class="${inputClass}">${['full-time','part-time','contract','temporary','internship','volunteer'].map(v=>`<option value="${v}">${v}</option>`).join('')}</select></div>
            <div><label for="search-experience" class="text-sm font-semibold">Experience level (multiple allowed)</label><select id="search-experience" multiple size="3" class="${inputClass}">${['internship','entry-level','associate','mid-senior','director','executive'].map(v=>`<option value="${v}">${v}</option>`).join('')}</select></div>
            <p class="sm:col-span-2 text-xs text-slate-500">LinkedIn may interpret these filters through its AI search. Review each posting to verify its suitability.</p>
          </div>
          <div id="search-url-fields" class="hidden"><label for="search-urls" class="text-sm font-semibold">LinkedIn job search URLs — one per line</label><textarea id="search-urls" rows="3" class="${inputClass}" placeholder="https://www.linkedin.com/jobs/search/?keywords=..."></textarea><p class="text-xs text-slate-500 mt-1">Filters come from the pasted URLs.</p></div>
          <div class="flex flex-wrap gap-4 items-end"><div><label for="search-limit" class="block text-sm font-semibold">Maximum results</label><input id="search-limit" type="number" min="150" max="10000" value="150" required class="${inputClass}"></div><label class="text-sm pb-2"><input type="checkbox" id="search-enrich"> Enrich company details (slower)</label><button id="run-search" class="${buttonClass}" type="submit">Run Search</button></div>
          <p class="text-xs text-slate-500">Each run uses your Apify account and may incur charges, including a start fee. The server applies the spending ceiling configured in your .env file.</p>
        </form>
        <div class="bg-white border rounded-2xl p-5 space-y-4">
          <div class="flex flex-wrap gap-3 items-center"><label for="search-history" class="font-semibold text-sm">Search history</label><select id="search-history" class="${inputClass} sm:w-auto"></select><button id="refresh-results" type="button" class="px-3 py-2 bg-slate-100 rounded-xl text-sm">Refresh</button></div>
          <div id="run-status" role="status" class="text-sm text-slate-600"></div>
          <div class="flex flex-wrap gap-3"><input id="results-query" aria-label="Filter results" placeholder="Filter title, company, location, description" class="${inputClass} sm:w-80"><select id="results-sort" aria-label="Sort results" class="${inputClass} sm:w-auto">${['jobTitle','company','location','salary','publishedAt','employmentType'].map(v=>`<option value="${v}">${({jobTitle:'Title',publishedAt:'Posting date',employmentType:'Employment type'})[v]||v}</option>`).join('')}</select><label class="text-sm py-2"><input id="results-desc" type="checkbox"> Descending</label></div>
          <div class="overflow-x-auto"><table class="w-full text-sm text-left"><thead class="bg-slate-50"><tr><th class="p-3"><input id="select-page" type="checkbox" aria-label="Select available jobs on this page"></th>${['Title','Company','Location / mode','Salary','Posted','Employment','Tracker'].map(v=>`<th class="p-3">${v}</th>`).join('')}</tr></thead><tbody id="discovery-table"></tbody></table></div>
          <div class="flex flex-wrap items-center justify-between gap-3"><div class="flex gap-3 items-center"><button id="previous-page" type="button" class="px-3 py-2 bg-slate-100 rounded-xl disabled:opacity-40">Previous</button><span id="page-label" class="text-sm"></span><button id="next-page" type="button" class="px-3 py-2 bg-slate-100 rounded-xl disabled:opacity-40">Next</button></div><div class="flex gap-3 items-center"><span id="selected-count" class="text-sm"></span><select id="import-stage" aria-label="Import stage" class="${inputClass}">${COLUMNS.map(v=>`<option>${v}</option>`).join('')}</select><button id="import-selected" type="button" class="${buttonClass}">Import selected</button></div></div>
        </div>
      </section>
      <section class="mt-6 border-t pt-4 flex flex-wrap gap-3 items-center text-xs text-slate-500"><span id="database-status">Connecting to local database…</span><button id="export-json" type="button" class="underline">Export JSON backup</button><label class="underline cursor-pointer">Import JSON backup / legacy data<input id="import-json" type="file" accept="application/json,.json" class="hidden"></label><button id="migrate-browser" type="button" class="underline hidden">Import existing browser records</button></section>
      <dialog id="posting-preview" class="rounded-2xl p-6 w-full max-w-3xl max-h-[85vh] overflow-auto"><button id="close-preview" class="float-right px-3 py-1 bg-slate-100 rounded-lg">Close</button><div id="preview-content" class="space-y-4"></div></dialog>
    `);
    E('search-mode').onchange = () => { const urls = E('search-mode').value === 'urls'; E('search-url-fields').classList.toggle('hidden', !urls); E('search-form-fields').classList.toggle('hidden', urls); };
    E('discovery-form').onsubmit = startSearch;
    E('search-history').onchange = async () => { discoveryRun = E('search-history').value || null; discoveryPage = 1; selectedResults.clear(); try { await loadDiscovery(); } catch(e) {report(e);} };
    E('refresh-results').onclick = () => loadRuns().catch(report);
    E('results-query').oninput = () => { clearTimeout(searchDebounce); searchDebounce = setTimeout(()=>{ discoveryPage=1; loadDiscovery().catch(report); },250); };
    for (const id of ['results-sort','results-desc']) E(id).onchange = () => { discoveryPage=1; loadDiscovery().catch(report); };
    E('previous-page').onclick = () => { if (discoveryPage>1) {discoveryPage--;loadDiscovery().catch(report);} };
    E('next-page').onclick = () => { discoveryPage++;loadDiscovery().catch(report); };
    E('select-page').onchange = event => { for(const item of discoveryItems.filter(v=>!v.inTracker && v.jobTitle && v.company)) { if(event.target.checked) selectedResults.add(item.id); else selectedResults.delete(item.id); } renderResults(); };
    E('import-selected').onclick = importSelected;
    E('close-preview').onclick = () => E('posting-preview').close();
    E('export-json').onclick = exportJSON;
    E('import-json').onchange = importJSON;
    E('migrate-browser').onclick = migrateBrowser;
  }

  async function startSearch(event) {
    event.preventDefault();
    const lines = id => E(id).value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
    const options = id => Array.from(E(id).selectedOptions).map(v=>v.value);
    E('run-search').disabled = true;
    try {
      requireReady();
      const run = await send('/runs', {mode:E('search-mode').value, keywords:lines('search-keywords'),locations:lines('search-locations'),urls:lines('search-urls'),publishedAt:E('search-age').value,workType:options('search-work'),jobType:options('search-jobtype'),experienceLevel:options('search-experience'),maxItems:Number(E('search-limit').value),enrichCompanyData:E('search-enrich').checked});
      discoveryRun=run.id; discoveryPage=1; selectedResults.clear(); await loadRuns();
    } catch(e) { report(e); E('run-search').disabled=false; }
  }
  async function loadRuns() {
    const runs = await api('/runs');
    if(!discoveryRun && runs.length) discoveryRun = runs[0].id;
    E('search-history').innerHTML = runs.length ? runs.map(r=>`<option value="${esc(r.id)}">${esc(new Date(r.created_at).toLocaleString())} — ${esc(r.status)} (${r.count})</option>`).join('') : '<option value="">No searches yet</option>';
    E('search-history').value = discoveryRun || '';
    anyActiveRun = runs.some(r=>activeStatuses.includes(r.status));
    E('run-search').disabled = !apifyConfigured || anyActiveRun;
    await loadDiscovery();
  }
  async function loadDiscovery() {
    clearTimeout(discoveryTimer);
    const requestNumber = ++discoveryRequest;
    if(!discoveryRun) { discoveryItems=[];discoveryTotal=0; E('run-status').textContent='Run a search to see job postings here.'; renderResults(); return; }
    const runId = discoveryRun;
    const params = new URLSearchParams({page:discoveryPage,pageSize:25,q:E('results-query').value,sort:E('results-sort').value,descending:E('results-desc').checked});
    const [run, result] = await Promise.all([api('/runs/'+runId),api('/runs/'+runId+'/results?'+params)]);
    if(runId !== discoveryRun || requestNumber !== discoveryRequest) return;
    discoveryItems=result.items;discoveryTotal=result.total;
    E('run-status').innerHTML = `<strong>${esc(run.status)}</strong> · ${run.count} results${run.partial?' · Partial results':''} ${run.consoleUrl?`· <a class="underline" href="${esc(run.consoleUrl)}" target="_blank" rel="noopener noreferrer">Open Apify run</a>`:''}${run.error?`<p class="text-rose-700 mt-2">${esc(run.error)}</p>`:''}${activeStatuses.includes(run.status)&&run.remote_id?'<button id="cancel-search" class="ml-3 underline">Cancel run</button>':''}${run.status==='RECOVERY_NEEDED'&&run.remote_id?'<button id="resume-search" class="ml-3 underline">Resume monitoring / download</button>':''}`;
    if(E('cancel-search')) E('cancel-search').onclick = () => runAction('cancel');
    if(E('resume-search')) E('resume-search').onclick = () => runAction('resume');
    for(const item of discoveryItems) if(item.inTracker) selectedResults.delete(item.id);
    renderResults();
    if(activeStatuses.includes(run.status) || anyActiveRun) discoveryTimer = setTimeout(()=>loadRuns().catch(e=>{report(e); discoveryTimer=setTimeout(()=>loadRuns().catch(report),10000);}),5000);
    else E('run-search').disabled = !apifyConfigured || anyActiveRun;
  }
  async function runAction(action) { try { await send('/runs/'+discoveryRun+'/'+action,{}); await loadRuns(); } catch(e) {report(e);} }
  function renderResults() {
    E('discovery-table').innerHTML = discoveryItems.length ? discoveryItems.map(item=>`<tr class="border-t"><td class="p-3"><input data-result-check="${esc(item.id)}" aria-label="Select ${esc(item.jobTitle)}" type="checkbox" ${selectedResults.has(item.id)?'checked':''} ${item.inTracker||!item.jobTitle||!item.company?'disabled':''}></td><td class="p-3"><button data-preview="${esc(item.id)}" class="text-indigo-600 text-left font-semibold">${esc(item.jobTitle||'Missing title')}</button>${item.url?`<a class="block text-xs underline mt-1" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">LinkedIn posting</a>`:''}</td><td class="p-3">${esc(item.company||'Missing company')}</td><td class="p-3">${esc(item.location)}<div class="text-xs text-slate-400">${esc(item.workType||'Unspecified')}</div></td><td class="p-3">${esc(item.salary||'Not listed')}</td><td class="p-3">${esc(item.publishedAt?new Date(item.publishedAt).toLocaleDateString():item.postedTime||'Unknown')}</td><td class="p-3">${esc(item.employmentType||'Not listed')}</td><td class="p-3">${item.inTracker?'Already tracked':'Available'}</td></tr>`).join('') : '<tr><td colspan="8" class="p-6 text-center text-slate-400">No matching results</td></tr>';
    E('page-label').textContent=`Page ${discoveryPage} of ${Math.max(1,Math.ceil(discoveryTotal/25))} · ${discoveryTotal} jobs`;
    E('previous-page').disabled=discoveryPage<=1; E('next-page').disabled=discoveryPage*25>=discoveryTotal;
    E('selected-count').textContent=selectedResults.size+' selected'; E('import-selected').disabled=!selectedResults.size;
    const eligible=discoveryItems.filter(v=>!v.inTracker&&v.jobTitle&&v.company);
    E('select-page').checked=eligible.length>0&&eligible.every(v=>selectedResults.has(v.id));
    E('select-page').indeterminate=eligible.some(v=>selectedResults.has(v.id))&&!E('select-page').checked;
    E('select-page').disabled=!eligible.length;
    document.querySelectorAll('[data-result-check]').forEach(input=>input.onchange=()=>{input.checked?selectedResults.add(input.dataset.resultCheck):selectedResults.delete(input.dataset.resultCheck);renderResults();});
    document.querySelectorAll('[data-preview]').forEach(button=>button.onclick=()=>preview(button.dataset.preview));
  }
  async function preview(id) {
    try {
      const result=await api('/results/'+id), item=result.data;
      E('preview-content').innerHTML=`<h3 class="text-xl font-bold pr-20">${esc(item.jobTitle)}</h3><p>${esc(item.company)} · ${esc(item.location)}</p><p class="text-sm">${esc(item.salary||'Salary not listed')} · ${esc(item.employmentType)} · ${esc(item.workType||'Work arrangement unspecified')}</p><div class="whitespace-pre-wrap text-sm leading-relaxed">${esc(item.jobDescription||'No description returned')}</div><details><summary class="cursor-pointer text-sm text-slate-500">Available posting metadata</summary><pre class="whitespace-pre-wrap break-all text-xs mt-3">${esc(JSON.stringify(result.metadata,null,2))}</pre></details>`;
      E('posting-preview').showModal();
    } catch(e) {report(e);}
  }
  async function importSelected() {
    E('import-selected').disabled=true;
    try {const result=await send('/import',{resultIds:[...selectedResults],stage:E('import-stage').value,dateTracked:localDate()});selectedResults.clear();await reloadTracker();await loadDiscovery();showToast(`${result.imported} imported, ${result.skipped} already tracked`);}
    catch(e) {report(e);renderResults();}
  }
  function exportJSON() {
    try {requireReady();const blob=new Blob([JSON.stringify({version:1,applications,settings:{googleSyncUrl}},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='careerforge-backup-'+localDate()+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    catch(e) {report(e);}
  }
  async function importJSON(event) {
    try {
      requireReady();const file=event.target.files[0];if(!file)return;const content=JSON.parse(await file.text());const records=Array.isArray(content)?content:content.applications;
      if(!Array.isArray(records))throw new Error('Backup must contain an applications array');
      const result=await send('/applications/bulk',{applications:records});
      if(content.settings?.googleSyncUrl) {await send('/settings',{googleSyncUrl:content.settings.googleSyncUrl},'PUT');googleSyncUrl=content.settings.googleSyncUrl;updateSyncStatusUI();}
      await reloadTracker();showToast(`${result.imported} imported, ${result.skipped} skipped`);
    } catch(e) {report(e);}finally {event.target.value='';}
  }
  async function migrateBrowser() {
    try {requireReady();const records=JSON.parse(localStorage.getItem('careerforge_applications')||'[]');const result=await send('/applications/bulk',{applications:records});await reloadTracker();showToast(`${result.imported} browser records imported; ${result.skipped} skipped`);}
    catch(e) {report(e);}
  }
  installUI();
  applications=[];
  document.addEventListener('DOMContentLoaded', async () => {
    try {
      const [config,settings]=await Promise.all([api('/config'),api('/settings')]);
      await reloadTracker();backendReady=true;apifyConfigured=config.apifyConfigured;
      googleSyncUrl=settings.googleSyncUrl||googleSyncUrl||'';updateSyncStatusUI();
      if(!settings.googleSyncUrl&&googleSyncUrl)await send('/settings',{googleSyncUrl},'PUT');
      E('database-status').textContent='Saved locally in SQLite';
      E('apify-config').textContent=config.apifyConfigured?'Apify connected · '+config.actorId:'Set APIFY_TOKEN in .env and restart the server to enable searches.';
      E('run-search').disabled=!config.apifyConfigured;
      if(localStorage.getItem('careerforge_applications'))E('migrate-browser').classList.remove('hidden');
      await loadRuns();
      if(!config.apifyConfigured)E('run-search').disabled=true;
    } catch(e) {E('database-status').textContent='Database unavailable — reload to reconnect'; report(e);}
  });
}
