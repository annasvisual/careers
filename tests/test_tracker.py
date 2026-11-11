import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('tracker',ROOT/'scripts/update_jobs.py')
t=importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
C=json.loads((ROOT/'config.json').read_text())

def job(title, location='London', description=''):
    return {'id':title,'source':'Monzo','title':title,'location':location,'description':description,'url':'https://example.com/job'}

class Filters(unittest.TestCase):
    def test_marketing(self):
        for title in ['Performance Marketing Manager','Paid Media Executive','Affiliate Manager','Product Marketing Associate','Web Marketing Executive']:
            self.assertTrue(t.matches(job(title),C),title)
        for title in ['Performance Marketing Lead','Head of Product Marketing','SEO Marketing Manager','CRM Marketing Executive','PR Manager','Product Manager','Web Developer','Business Development Associate']:
            self.assertFalse(t.matches(job(title),C),title)
    def test_junior(self):
        self.assertTrue(t.matches(job('Junior Product Designer'),C))
        self.assertTrue(t.matches(job('Finance Graduate'),C))
        self.assertFalse(t.matches(job('Finance Analyst'),C))
        self.assertFalse(t.matches(job('Finance Analyst',description='This is an entry-level position'),C))
        self.assertTrue(t.matches(job('Junior Finance Analyst'),C))
        self.assertFalse(t.matches(job('Junior Software Engineer - Finance'),C))
    def test_no_city_filter(self):
        self.assertTrue(t.matches(job('Paid Media Executive','Swindon / London'),C))
        self.assertTrue(t.matches(job('Paid Media Executive','Swindon'),C))
        self.assertTrue(t.matches(job('Paid Media Executive',''),C))

class History(unittest.TestCase):
    def test_baseline_retry_and_repost(self):
        s={'seen':{},'days':[]}
        a,b=job('Paid Media Executive'),job('Junior Product Designer')
        t.merge(s,{'Monzo':[a]},{'Triodos':'unavailable'},'2026-11-10','now')
        self.assertEqual(s['days'][0]['jobs'],[])
        t.merge(s,{'Monzo':[a,b]}, {},'2026-11-11','now')
        self.assertEqual(len(s['days'][0]['jobs']),1)
        t.merge(s,{}, {'Monzo':'timeout'},'2026-11-11','now')
        self.assertEqual(len(s['days'][0]['jobs']),1)
        t.merge(s,{'Monzo':[a,b]}, {},'2026-11-11','now')
        self.assertNotIn('Monzo',s['days'][0]['errors'])
        self.assertEqual(len(s['days'][0]['jobs']),1)
        t.merge(s,{'Monzo':[b]}, {},'2026-11-12','now')
        self.assertEqual(s['days'][0]['jobs'],[])
    def test_nonmatching_ids_still_remembered(self):
        s={'seen':{},'days':[],'initialized_sources':['Monzo']}
        a=job('Web Developer');a['selected']=False
        t.merge(s,{'Monzo':[a]}, {},'2026-11-10','now')
        self.assertIn(a['id'],s['seen'])
        self.assertEqual(s['days'][0]['jobs'],[])

if __name__=='__main__': unittest.main()
