"""Manual category breakdowns must never duplicate month/week totals."""
import http.cookiejar
import json
import os
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

_tmp=tempfile.TemporaryDirectory(prefix='smartspend-breakdown-')
os.environ['SMARTSPEND_DB']=str(Path(_tmp.name)/'breakdown.sqlite3')
from database import setup  # noqa: E402
from ml_model import preceding_month, week_start  # noqa: E402
from server import Handler, ThreadingHTTPServer  # noqa: E402


class CategoryBreakdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.address=f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=3)

    def setUp(self):
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status,user=self.api('/api/auth/register','POST',{'name':'Breakdown Tester',
                            'email':secrets.token_hex(8)+'@example.com', 'password':'SafePassword123!'})
        self.assertEqual(status,201,user)
        self.csrf=user['csrf']

    def api(self,path,method='GET',data=None):
        headers={}
        if data is not None:
            data=json.dumps(data).encode()
            headers['Content-Type']='application/json'
        if getattr(self,'csrf',None): headers['X-CSRF-Token']=self.csrf
        req=urllib.request.Request(self.address+path,method=method,data=data,headers=headers)
        try:
            with self.client.open(req,timeout=10) as response:
                return response.status,json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code,json.loads(e.read())

    def last_month(self):
        return preceding_month(date.today().strftime('%Y-%m'))

    def test_breakdown_categories_total_and_replacement(self):
        month=self.last_month()
        payload={'period':'monthly','month':month,'mode':'breakdown','categories':[
            {'category':'Food & Drinks','amount':'1234.50'},
            {'category':'Transportation','amount':'320.25'},
            {'category':'Bills & Utilities','amount':'2000'}]}
        status,r=self.api('/api/history/manual','POST',payload)
        self.assertEqual(status,201,r)
        self.assertEqual(r['total_amount'],3554.75)
        self.assertEqual(r['entry_count'],3)
        status,dash=self.api('/api/dashboard?month='+month)
        self.assertEqual(status,200)
        self.assertEqual(dash['spent'],3554.75)
        self.assertEqual({x['name']:x['amount'] for x in dash['categories']},
                         {'Food & Drinks':1234.5,'Transportation':320.25,'Bills & Utilities':2000.0})
        status,ledger=self.api('/api/transactions?month='+month)
        self.assertEqual(status,200)
        self.assertEqual(ledger['total'],3)
        self.assertEqual({x['source'] for x in ledger['transactions']},{'monthly_breakdown'})
        status,edited=self.api('/api/history/manual?id='+str(ledger['transactions'][0]['id']))
        self.assertEqual(status,200)
        self.assertEqual(edited['mode'],'breakdown')
        self.assertEqual(edited['month'],month)
        self.assertEqual(len(edited['categories']),3)
        # Re-saving same month's breakdown must replace old categories, not add again.
        status,r=self.api('/api/history/manual','POST',{'period':'monthly','month':month,
                      'mode':'breakdown','categories':[{'category':'Shopping','amount':'200'}]})
        self.assertEqual(status,200,r)
        self.assertTrue(r['updated'])
        self.assertEqual(self.api('/api/dashboard?month='+month)[1]['spent'],200)
        self.assertEqual(self.api('/api/transactions?month='+month)[1]['total'],1)
        # Can switch to one total and back without double counting.
        status,r=self.api('/api/history/manual','POST',{'period':'monthly','month':month,
                                                       'mode':'total','amount':'4500.33'})
        self.assertEqual(status,200,r)
        self.assertEqual(r['mode'],'total')
        self.assertEqual(self.api('/api/dashboard?month='+month)[1]['spent'],4500.33)
        self.assertEqual(self.api('/api/transactions?month='+month)[1]['total'],1)

    def test_breakdown_blocks_extra_expense_and_delete_removes_whole_group(self):
        month=self.last_month()
        p={'period':'monthly','month':month,'mode':'breakdown','categories':[
            {'category':'Food & Drinks','amount':100},{'category':'Other','amount':200}]}
        self.assertEqual(self.api('/api/history/manual','POST',p)[0],201)
        status,_=self.api('/api/transactions','POST',{'date':month+'-15','type':'expense',
                     'amount':1,'category':'Other','description':'Would double count'})
        self.assertEqual(status,409)
        rows=self.api('/api/transactions?month='+month)[1]['transactions']
        self.assertEqual(len(rows),2)
        self.assertEqual(self.api('/api/transactions/'+str(rows[0]['id']),'DELETE',{})[0],200)
        self.assertEqual(self.api('/api/transactions?month='+month)[1]['total'],0)
        self.assertEqual(self.api('/api/dashboard?month='+month)[1]['spent'],0)
        # Daily entries are permitted again after deleting the whole snapshot.
        self.assertEqual(self.api('/api/transactions','POST',{'date':month+'-15','type':'expense',
                              'amount':1,'category':'Other','description':'Allowed now'})[0],201)

    def test_breakdown_invalid_payload_does_not_erase_saved_data(self):
        month=self.last_month()
        self.assertEqual(self.api('/api/history/manual','POST',{'month':month,'mode':'total',
                                                           'amount':500})[0],201)
        bad=[
            [{'category':'Food & Drinks','amount':100}, {'category':'Food & Drinks','amount':50}],
            [{'category':'Unknown','amount':100}],
            [{'category':'Food & Drinks','amount':-3}],
        ]
        for rows in bad:
            status,_=self.api('/api/history/manual','POST',{'month':month,'mode':'breakdown',
                                                           'categories':rows})
            self.assertEqual(status,400)
            self.assertEqual(self.api('/api/dashboard?month='+month)[1]['spent'],500)
        status,_=self.api('/api/history/manual','POST',{'month':month,'mode':'breakdown','categories':[]})
        self.assertEqual(status,400)

    def test_weekly_breakdown_forecast_and_monthly_exclusion(self):
        week=week_start(date.today())-timedelta(days=7)
        weeks=[week-timedelta(weeks=i) for i in reversed(range(6))]
        for i,w in enumerate(weeks):
            status,r=self.api('/api/history/manual','POST',{'period':'weekly',
                'date':w.isoformat(),'mode':'breakdown','categories':[
                    {'category':'Food & Drinks','amount':300+i*10},
                    {'category':'Transportation','amount':150}]})
            self.assertEqual(status,201,r)
        status,p=self.api('/api/predict','POST',{'period':'weekly'})
        self.assertEqual(status,201,p)
        self.assertEqual(p['history_periods'],6)
        self.assertEqual(p['prediction_stage'],'early')
        status,_=self.api('/api/predict','POST',{'period':'monthly'})
        self.assertEqual(status,422)


if __name__ == '__main__':unittest.main()