"""Built-in unittest smoke/integration tests. Run: python -m unittest discover -s tests -v"""
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

# Set an isolated DB BEFORE importing modules that capture SMARTSPEND_DB.
_tmp=tempfile.TemporaryDirectory(prefix='smartspend-test-')
os.environ['SMARTSPEND_DB']=str(Path(_tmp.name)/'test.sqlite3')
from database import setup  # noqa: E402
from demo_data import sample_rows  # noqa: E402
from ml_model import InsufficientHistory, fit_and_forecast  # noqa: E402
from server import Handler, ThreadingHTTPServer, ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402


class SmartSpendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.address=f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def new_client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self,opener,path,method='GET',data=None,csrf=None):
        headers={}
        if data is not None:
            data=json.dumps(data).encode('utf-8');headers['Content-Type']='application/json'
        if csrf:headers['X-CSRF-Token']=csrf
        req=urllib.request.Request(self.address+path,method=method,data=data,headers=headers)
        try:
            with opener.open(req,timeout=10) as r:
                return r.status,json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code,json.loads(exc.read())

    def register(self,email):
        client=self.new_client()
        status,result=self.call(client,'/api/auth/register','POST',
                                {'name':'Example Student','email':email,'password':'TestPassword@123'})
        self.assertEqual(status,201,result)
        return client,result['csrf']

    def test_user_crud_budget_and_roles(self):
        user,csrf=self.register('test_ledger@example.com')
        status,body=self.call(user,'/api/transactions','POST',{'date':'2026-10-05','amount':84.90,
            'description':'Class supplies','category':'Education','type':'expense'},csrf)
        self.assertEqual(status,201,body)
        txid=body['id']
        status,body=self.call(user,'/api/dashboard?month=2026-10')
        self.assertEqual(body['spent'],84.9)
        # An unset budget stays unset; do not fabricate ₱20,000 or remaining funds.
        self.assertIsNone(body['budget'])
        self.assertIsNone(body['remaining'])
        self.assertFalse(body['budget_customized'])
        status,_=self.call(user,'/api/transactions/'+str(txid),'PATCH',
                           {'description':'Updated class supplies','amount':100},csrf)
        self.assertEqual(status,200)
        self.assertEqual(self.call(user,'/api/dashboard?month=2026-10')[1]['spent'],100.0)
        self.assertEqual(self.call(user,'/api/budget','POST',{'month':'2026-10','amount':3000},csrf)[0],200)
        self.assertEqual(self.call(user,'/api/dashboard?month=2026-10')[1]['budget'],3000.0)
        self.assertEqual(self.call(user,'/api/dashboard?month=2026-10')[1]['remaining'],2900.0)
        self.assertIsNone(self.call(user,'/api/dashboard?month=2026-09')[1]['budget'])
        # An intentionally entered zero is still a valid user-chosen budget.
        self.assertEqual(self.call(user,'/api/budget','POST',{'month':'2026-09','amount':0},csrf)[0],200)
        zero_budget=self.call(user,'/api/dashboard?month=2026-09')[1]
        self.assertEqual(zero_budget['budget'],0.0)
        self.assertTrue(zero_budget['budget_customized'])
        self.assertEqual(self.call(user,'/api/admin/users')[0],403)
        self.assertEqual(self.call(user,'/api/transactions/'+str(txid),'DELETE',{},'wrong-token')[0],403)
        self.assertEqual(self.call(user,'/api/transactions/'+str(txid),'DELETE',{},csrf)[0],200)
        self.assertEqual(self.call(user,'/api/dashboard?month=2026-10')[1]['spent'],0.0)

    def test_import_clean_and_export(self):
        user,csrf=self.register('test_import@example.com')
        csvtext='date,description,category,amount,type\n2026-10-12,Lunch,Food,150,expense\n'
        csvtext+='2026-10-12,Lunch,Food,150,expense\nnot-a-date,Invalid,Food,400,expense\n'
        status,body=self.call(user,'/api/datasets/import','POST',{'csv':csvtext,'filename':'test.csv'},csrf)
        self.assertEqual(status,200)
        self.assertEqual((body['imported'],body['duplicates'],body['invalid']),(1,1,1))
        self.assertEqual(self.call(user,'/api/dashboard?month=2026-10')[1]['categories'][0]['name'],'Food & Drinks')
        self.assertEqual(self.call(user,'/api/datasets/info')[1]['transaction_count'],1)
        status,body=self.call(user,'/api/datasets/import','POST',{'csv':'bad,data\n1,2'},csrf)
        self.assertEqual(status,400)

    def test_training_and_evaluation(self):
        today=date.today()
        sample=[r for r in sample_rows(today=today) if r['kind']=='expense']
        result=fit_and_forecast(sample,today=today)
        self.assertEqual(result['history_months'],24)
        self.assertEqual(len(result['evaluation']),3)
        self.assertIn(result['selected_model'],('Ridge Regression','Random Forest'))
        self.assertGreater(result['train_samples'],result['test_samples'])
        self.assertGreater(result['predicted_amount'],0)
        with self.assertRaises(InsufficientHistory):
            fit_and_forecast(sample[:50],today=today)

    def test_admin_reads_database_but_cannot_write_user_records(self):
        self.register('test_admin_see@example.com')
        admin=self.new_client()
        status,login=self.call(admin,'/api/auth/login','POST',
              {'email':ADMIN_EMAIL,'password':ADMIN_PASSWORD})
        self.assertEqual(status,200)
        self.assertEqual(login['role'],'admin')
        users=self.call(admin,'/api/admin/users')[1]['users']
        self.assertTrue(any(u['email']=='test_admin_see@example.com' for u in users))
        self.assertNotIn('password_hash',users[0])
        self.assertEqual(self.call(admin,'/api/transactions')[0],403)
        self.assertEqual(self.call(admin,'/api/admin/users','POST',{},login['csrf'])[0],405)
        with admin.open(self.address+'/api/admin/export?dataset=users') as response:
            exported=response.read().decode('utf-8-sig')
        self.assertIn('test_admin_see@example.com',exported)
        self.assertNotIn('password_hash',exported)


if __name__=='__main__':unittest.main()