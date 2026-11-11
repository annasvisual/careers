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
    if any(phrase(title, t) for t in ['lead', 'head', 'director', 'senior', 'principal', 'staff']):
        junior_product = False
    else:
        junior_product = phrase(title, 'junior') and phrase(title, 'product')
    if junior_product or (phrase(title, 'ux designer') and not any(phrase(title, t) for t in ['lead', 'head', 'director', 'senior', 'principal', 'staff'])):
        return True
    marketing = ('marketing' in title or any(phrase(title, t) for t in ['ppc', 'paid media', 'paid social', 'paid search', 'affiliate']))
    if marketing:
        return (any(phrase(title, t) for t in config['marketing_include'])
                and not any(phrase(title, t) for t in config['marketing_exclude'] + config['excluded_marketing_levels']))
    if any(phrase(title, t) for t in config['finance_exclude'] + ['senior', 'lead', 'head', 'director', 'manager', 'principal']):
        return False
    if not any(phrase(title, t) for t in config['finance_domains']):
        return False
    return any(phrase(title, t) for t in config['early_career_terms'])


def merge(state, results, errors, day, timestamp):
    """Baseline each source independently; retrying a day preserves its new jobs."""
    state.setdefault('initialized_sources', [])
    current = state.setdefault('current', {})
    record = next((d for d in state['days'] if d['date'] == day), None)
    if record is None:
        record = {'date': day, 'jobs': [], 'errors': {}, 'baseline_sources': []}
        state['days'].append(record)
    existing = {j['id'] for j in record['jobs']}
    for source, jobs in results.items():
        current[source] = {
            'checked_at': timestamp,
            'jobs': list({job['id']: {'source': source, 'title': job['title']}
                          for job in jobs if job.get('selected', True)}.values()),
        }
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
     : source==='Starling' ? new URL(url).hostname === "apply.workable.com" && /^\/(?:[^/]+\/)?j\/[^/]+\/?$/.test(new URL(url).pathname)
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


async def collect(context, source, url, config):
    if source == 'Monzo':
        response = await context.request.get('https://boards-api.greenhouse.io/v1/boards/monzo/jobs', timeout=60000)
        if not response.ok:
            raise ValueError(f'Greenhouse HTTP {response.status}')
        payload = await response.json()
        jobs = payload.get('jobs')
        if not isinstance(jobs, list) or not jobs:
            raise ValueError('Empty or invalid Greenhouse response')
        normalized = [{'id':f"Monzo:{j['id']}", 'source':source, 'title':j['title'], 'url':j['absolute_url']} for j in jobs]
        return normalized
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = await context.new_page()
    response = None
    try:
        response = await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        if response and response.status >= 400:
            raise ValueError(f'Career listing returned HTTP {response.status}')
        if source in {'Nationwide', 'Lloyds'}:
            consent = page.get_by_role('button', name={
                'Nationwide': 'Allow essential cookies only',
                'Lloyds': 'Reject all',
            }[source], exact=True)
            try:
                await consent.first.click(timeout=3500)
            except PlaywrightTimeoutError:
                pass
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
        return [{'id':source+':'+urlparse(item['url']).path.rstrip('/'),
                 'source':source, 'title':item['title'], 'url':item['url']}
                for item in items.values()]
    except Exception:
        try:
            diagnostic = await page.evaluate(r'''() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const body = document.body?.innerText || '';
                return {
                    title: document.title.slice(0, 150),
                    totalLinks: links.length,
                    vacancyLikeLinks: links.filter(a => /\/(jobs?|j)\/|workday-job\./i.test(a.href)).length,
                    emptyMessage: /no jobs found|no vacancies|no results found/i.test(body),
                    accessMessage: /access denied|verify you are human|request blocked/i.test(body),
                };
            }''')
            diagnostic['httpStatus'] = response.status if response else None
            diagnostic['finalPath'] = urlparse(page.url).path
            print(f'{source}: LISTING DIAGNOSTICS {json.dumps(diagnostic, ensure_ascii=True)}', flush=True)
        except Exception:
            print(f'{source}: listing diagnostics unavailable', flush=True)
        raise
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
