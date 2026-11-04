"""Checks manual history and transparent small-sample forecasting."""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import http.cookiejar
from datetime import date, timedelta
from pathlib import Path

_tmp=tempfile.TemporaryDirectory(prefix='smartspend-history-')
os.environ['SMARTSPEND_DB']=str(Path(_tmp.name)/'history.sqlite3')
from database import setup  # noqa: E402
from server import Handler, ThreadingHTTPServer  # noqa: E402
from ml_model import fit_and_forecast, InsufficientHistory, preceding_month, week_start  # noqa: E402


class HistoryForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2)

    def api(self,path,method='GET',data=None):
        headers={}
        if data is not None:
            data=json.dumps(data).encode();headers['Content-Type']='application/json'
        if getattr(self,'csrf',None):headers['X-CSRF-Token']=self.csrf
        try:
            with self.client.open(urllib.request.Request(self.url+path,method=method,data=data,headers=headers),timeout=10) as r:
                return r.status,json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code,json.loads(e.read())

    def setUp(self):
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        import secrets
        code=secrets.token_hex(5)
        status,r=self.api('/api/auth/register','POST',{'name':'Student Tester','email':f'{code}@example.org','password':'VeryStrongPwd123!'})
        self.assertEqual(status,201,r);self.csrf=r['csrf']

    def test_monthly_history_three_months_and_overlap(self):
        current=date.today().strftime('%Y-%m')
        months=[];m=preceding_month(current)
        for _ in range(3):months.insert(0,m);m=preceding_month(m)
        for i,m in enumerate(months):
            status,r=self.api('/api/history/manual','POST',{'period':'monthly','month':m,'amount':2000+300*i})
            self.assertEqual(status,201,r)
        status,p=self.api('/api/predict','POST',{'period':'monthly'})
        self.assertEqual(status,201,p)
        self.assertEqual(p['period_type'],'monthly')
        self.assertEqual(p['history_periods'],3)
        self.assertEqual(p['prediction_stage'],'early')
        self.assertEqual(p['evaluation'],[])
        self.assertGreater(p['predicted_amount'],0)
        # Updating the same manual summary must replace rather than double count.
        status,u=self.api('/api/history/manual','POST',{'period':'monthly','month':months[1],'amount':3333})
        self.assertEqual(status,200,u);self.assertTrue(u['updated'])
        status,r=self.api('/api/transactions','POST',{'date':months[1]+'-15','type':'expense','amount':500,'category':'Other','description':'Would double count'})
        self.assertEqual(status,409,r)

    def test_weekly_history_six_weeks_and_invalid_overlap(self):
        week=week_start(date.today())-timedelta(days=7)
        weeks=[week-timedelta(weeks=i) for i in reversed(range(6))]
        for i,w in enumerate(weeks):
            status,r=self.api('/api/history/manual','POST',{'period':'weekly','date':w.isoformat(),'amount':500+25*i})
            self.assertEqual(status,201,r)
        status,p=self.api('/api/predict','POST',{'period':'weekly'})
        self.assertEqual(status,201,p)
        self.assertEqual(p['period_type'],'weekly')
        self.assertEqual(p['history_periods'],6)
        self.assertEqual(p['prediction_stage'],'early')
        self.assertEqual(p['evaluation'],[])
        self.assertEqual(len(p['forecast_month']),10)
        # Monthly forecast cannot incorrectly treat weekly summaries as complete months.
        status,e=self.api('/api/predict','POST',{'period':'monthly'})
        self.assertEqual(status,422,e)
        # An aggregate month overlapping a finished weekly period must be rejected.
        status,r=self.api('/api/history/manual','POST',{'period':'monthly','month':weeks[-1].strftime('%Y-%m'),'amount':999})
        self.assertEqual(status,409,r)

    def test_weekly_evaluated_with_fourteen_complete_weeks(self):
        today=date(2026,11,4)
        last=week_start(today)-timedelta(days=7)
        weeks=[last-timedelta(weeks=i) for i in reversed(range(14))]
        rows=[{'date':(w+timedelta(days=3)).isoformat(),'amount_cents':int((1200 + i*17 + (i%3)*11)*100)} for i,w in enumerate(weeks)]
        report=fit_and_forecast(rows,today=today,period='weekly')
        self.assertEqual(report['prediction_stage'],'evaluated')
        self.assertEqual(len(report['evaluation']),3)
        self.assertGreaterEqual(report['test_samples'],2)
        self.assertIn(report['selected_model'],[r['model'] for r in report['evaluation']])

    def test_gap_is_not_counted_as_zero(self):
        today=date(2026,11,4)
        rows=[{'date':'2026-08-15','amount_cents':10000},{'date':'2026-09-15','amount_cents':10000}]
        with self.assertRaises(InsufficientHistory):fit_and_forecast(rows,today=today,period='monthly')


if __name__=='__main__':unittest.main()