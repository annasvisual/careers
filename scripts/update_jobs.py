"""Daily read-only collection of public bank career listings."""
import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def phrase(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I))


def matches(job, config):
    title = job['title'].lower()
    location = job.get('location', '').lower()
    description = job.get('description', '').lower()
    if not phrase(location, 'london'):
        if not (config['include_uk_remote'] and re.search(r'remote.*(?:uk|united kingdom)', location)):
            return False
    if any(phrase(title, t) for t in ['lead', 'head', 'director', 'senior', 'principal', 'staff']):
        junior_product = False
    else:
        junior_product = phrase(title, 'junior') and phrase(title, 'product')
    if junior_product:
        return True
    marketing = ('marketing' in title or any(phrase(title, t) for t in ['ppc', 'paid media', 'paid social', 'paid search', 'affiliate']))
    if marketing:
        return (any(phrase(title, t) for t in config['marketing_include'])
                and not any(phrase(title, t) for t in config['marketing_exclude'] + config['excluded_marketing_levels']))
    if any(phrase(title, t) for t in config['finance_exclude'] + ['senior', 'lead', 'head', 'director', 'manager', 'principal']):
        return False
    if not any(phrase(title, t) for t in config['finance_domains']):
        return False
    early = any(phrase(title, t) for t in config['early_career_terms'])
    # Analyst/Associate alone is not sufficient evidence of an entry-level role.
    if not early and re.search(r'\b(?:analyst|associate)\b', title):
        early = bool(re.search(r'entry[- ]level|no (?:prior )?experience|0\s*[-–]\s*[12]\s*years|up to [12] years', description))
    if not early:
        return False
    required = re.findall(r'(?:at least|minimum(?: of)?|requires?|must have)\s*(\d+)\+?\s*years', description)
    required += re.findall(r'(\d+)\+?\s*years?\s*(?:of\s*)?(?:relevant |professional |previous )?experience', description)
    ranges = re.findall(r'(\d+)\s*[-–]\s*\d+\s*years?\s*(?:of\s*)?(?:relevant |professional )?experience', description)
    return not any(int(n) > config['finance_max_required_years'] for n in required + ranges)


def merge(state, results, errors, day, timestamp):
    """Baseline each source independently; retrying a day preserves its new jobs."""
    state.setdefault('initialized_sources', [])
    record = next((d for d in state['days'] if d['date'] == day), None)
    if record is None:
        record = {'date': day, 'jobs': [], 'errors': {}, 'baseline_sources': []}
        state['days'].append(record)
    existing = {j['id'] for j in record['jobs']}
    for source, jobs in results.items():
        baseline = source not in state['initialized_sources']
        if baseline:
            state['initialized_sources'].append(source)
            record['baseline_sources'].append(source)
        for job in jobs:
            key = job['id']
            if key not in state['seen']:
                state['seen'][key] = {'first_seen': day, 'source': source}
                if not baseline and key not in existing and job.get('selected', True):
                    record['jobs'].append(job)
                    existing.add(key)
        record['errors'].pop(source, None)
    record['errors'].update(errors)
    record['checked_at'] = timestamp
    state['last_checked'] = timestamp
    state['days'].sort(key=lambda d: d['date'], reverse=True)
    return state


# These extract rendered public DOM content; no applicant accounts or applications.
EXTRACT = r"""({source}) => {
 const out=[];
 for (const a of document.querySelectorAll('a[href]')) {
   const url=a.href;
   let valid = source==='Lloyds' ? /workday-job\.\d+\.html/.test(url)
     : source==='Nationwide' ? /\/jobs\/[^/]+-\d+/.test(url)
     : source==='Starling' ? /apply\.workable\.com\/.*(?:\/j\/|\/j$)/.test(url)
     : source==='NatWest' ? /\/jobs\/\d+-/.test(url)
     : /\/job\//.test(url);
   if(!valid) continue;
   let node=a;
   const headingTag=source==='Starling'?'h3':'h2';
   for(let i=0;!['Lloyds','NatWest','Triodos'].includes(source) && i<6 && node.parentElement;i++) {
     if(node.querySelector(headingTag)) break;
     node=node.parentElement;
   }
   const heading=node.querySelector(headingTag);
   let title=heading?heading.innerText.trim():a.innerText.trim().split(/Location:|\n/)[0].trim();
   title=title.replace(/^Job details of\s*/,'');
   if(!title) continue;
   out.push({title,url,listing:node.innerText.slice(0,12000)});
 }
 return out;
}"""


async def detail(context, source, item):
    page = await context.new_page()
    try:
        await page.goto(item['url'], wait_until='domcontentloaded', timeout=60000)
        await page.locator('h1').first.wait_for(timeout=25000)
        # Career templates sometimes populate the description after the heading.
        await page.wait_for_function("document.body.innerText.length > 1500", timeout=25000)
        body = await page.locator('body').inner_text()
        title = (await page.locator('h1').first.inner_text()).strip()
        if re.search(r'not found|no longer available|job (?:has been )?closed', title, re.I):
            raise ValueError('Vacancy detail unavailable')
        # Restrict location evidence to the job header, before related jobs/footer.
        header = re.split(r'Job description|What you.ll be doing|About (?:the role|us)|Description', body, maxsplit=1, flags=re.I)[0]
        if source == 'Nationwide':
            m = re.search(r'Location\s*\n([\s\S]*?)\nSalary', header, re.I)
            location = m.group(1) if m else ''
        else:
            location = header
        return {'id': source + ':' + urlparse(item['url']).path.rstrip('/'), 'source': source,
                'title': title or item['title'], 'url': item['url'],
                'location': location[:4000], 'description': body[:45000]}
    finally:
        await page.close()


async def collect(context, source, url, config):
    if source == 'Monzo':
        response = await context.request.get('https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true', timeout=60000)
        if not response.ok:
            raise ValueError(f'Greenhouse HTTP {response.status}')
        payload = await response.json()
        jobs = payload.get('jobs')
        if not isinstance(jobs, list) or not jobs:
            raise ValueError('Empty or invalid Greenhouse response')
        normalized = [{'id':f"Monzo:{j['id']}", 'source':source, 'title':j['title'], 'url':j['absolute_url'],
                       'location':j['location']['name'], 'description':re.sub('<[^>]+>', ' ', j.get('content', ''))} for j in jobs]
        return normalized
    page = await context.new_page()
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        selector = {'Lloyds':'a[href*="workday-job."]', 'Starling':'a[href*="apply.workable.com"]',
                    'Nationwide':'a[href*="/jobs/"]', 'NatWest':'a[href*="/jobs/"]',
                    'Triodos':'a[href*="/job/"]'}[source]
        # A displayed zero is intentionally not treated as successful until validated.
        await page.locator(selector).first.wait_for(state='attached', timeout=45000)
        items = {}
        for number in range(50):
            batch = await page.evaluate(EXTRACT, {'source': source})
            if not batch:
                raise ValueError('No readable vacancy cards')
            for item in batch:
                items[item['url']] = item
            next_link = page.get_by_role('link', name=re.compile(r'^(?:Next page|Navigate to the next page of the results)$', re.I))
            if not await next_link.count() or not await next_link.first.is_visible():
                break
            if await next_link.first.get_attribute('aria-disabled') == 'true':
                break
            old_urls = [j['url'] for j in batch]
            await next_link.first.click()
            await page.wait_for_function("old => {const links=Array.from(document.querySelectorAll('a[href]')).map(a=>a.href);return old.some(u=>!links.includes(u));}", arg=old_urls, timeout=25000)
        else:
            raise ValueError('Pagination limit reached')
        normalized = []
        for item in items.values():
            # Fetch only plausible titles, but retain IDs for every seen listing.
            candidate = {'title':item['title'], 'location':'London', 'description':'entry level'}
            if matches(candidate, config):
                normalized.append(await detail(context, source, item))
            else:
                normalized.append({'id':source+':'+urlparse(item['url']).path.rstrip('/'), 'source':source,
                                   'title':item['title'], 'url':item['url'], 'location':'', 'description':''})
        return normalized
    finally:
        await page.close()


async def run(config, state_path):
    from playwright.async_api import async_playwright
    state = json.loads(state_path.read_text())
    results, errors = {}, {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(locale='en-GB')
        for source, url in config['sources'].items():
            try:
                jobs = await collect(context, source, url, config)
                # Track all IDs, display only selected vacancies.
                for job in jobs:
                    job['selected'] = matches(job, config)
                    job.pop('description', None)
                results[source] = jobs
                print(f'{source}: {len(jobs)} listings, {sum(matches(j, config) for j in jobs)} matching')
            except Exception as exc:
                errors[source] = str(exc)[:300]
                print(f'{source}: CHECK FAILED: {errors[source]}')
        await browser.close()
    now = datetime.now(ZoneInfo(config['timezone']))
    # merge must see every ID; prune nonmatching entries from the presentation only.
    merge(state, results, errors, now.date().isoformat(), now.isoformat())
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(state_path)
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=ROOT/'data/jobs.json')
    args = parser.parse_args()
    asyncio.run(run(json.loads((ROOT/'config.json').read_text()), args.data))
