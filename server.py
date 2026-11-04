#!/usr/bin/env python3
"""Smart Spend: no Flask required; standard-library web server + scikit-learn model."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from database import get_db, setup
from demo_data import CATEGORIES, sample_rows
from ml_model import InsufficientHistory, fit_and_forecast, preceding_month, following_month, week_start

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'
# Classroom/demo default. Change these via env vars BEFORE public deployment.
ADMIN_EMAIL = os.environ.get('SMARTSPEND_ADMIN_EMAIL', 'admin@smartspend.local').lower()
ADMIN_PASSWORD = os.environ.get('SMARTSPEND_ADMIN_PASSWORD', 'SmartSpend@2026')
MAX_BODY = 3 * 1024 * 1024
COOKIE_NAME = 'smartspend_session'


def local_today():
    return datetime.now(ZoneInfo(os.environ.get('SMARTSPEND_TZ','Asia/Manila'))).date()


class ApiError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def utc_now():
    return datetime.now(timezone.utc)


def iso_month(s):
    if not isinstance(s,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',s):
        raise ApiError(400,'Month must use YYYY-MM.')
    return s


def parse_money(value):
    try:
        v = float(str(value).replace(',','').strip())
    except (TypeError,ValueError):
        raise ApiError(400,'Amount must be a valid number.')
    if not 0 < v <= 10000000 or not (v < float('inf')):
        raise ApiError(400,'Amount must be above zero and no greater than ₱10,000,000.')
    return int(round(v * 100))


def validate_date(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(value)):
        raise ApiError(400,'Date must use YYYY-MM-DD.')
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        raise ApiError(400,'Date must use YYYY-MM-DD.')
    if d.year < 2000 or d > local_today():
        raise ApiError(400,'Date cannot be in the future or before 2000.')
    return d.isoformat()


def normalize_category(s, kind='expense'):
    if kind == 'income':
        return 'Income'
    aliases = {
        'food': 'Food & Drinks', 'food & drinks':'Food & Drinks', 'groceries':'Food & Drinks',
        'transport':'Transportation','transportation':'Transportation','travel':'Transportation',
        'shopping':'Shopping', 'bills':'Bills & Utilities','utilities':'Bills & Utilities',
        'bills & utilities':'Bills & Utilities', 'health':'Health', 'medical':'Health',
        'entertainment':'Entertainment','education':'Education','other':'Other',
    }
    return aliases.get(str(s or '').strip().lower(), 'Other')


def validate_transaction(payload, source='manual'):
    if not isinstance(payload,dict):
        raise ApiError(400,'Invalid transaction.')
    kind = str(payload.get('type',payload.get('kind','expense'))).strip().lower()
    if kind not in ('expense','income'):
        raise ApiError(400,'Transaction type must be income or expense.')
    desc = str(payload.get('description','')).strip()
    if not 1 <= len(desc) <= 100:
        raise ApiError(400,'Description is required (up to 100 characters).')
    return {
        'date': validate_date(payload.get('date','')),
        'description': desc,
        'category': normalize_category(payload.get('category','Other'),kind),
        'kind': kind,
        'amount_cents': parse_money(payload.get('amount')),
        'source': source,
    }


def manual_period_bounds(payload):
    """Store a single clearly marked total as a transaction; disallow overlaps."""
    period = str(payload.get('period', 'monthly')).lower()
    if period == 'monthly':
        month = iso_month(payload.get('month', ''))
        try:start = date.fromisoformat(month + '-01')
        except ValueError:raise ApiError(400,'Enter a valid calendar month.')
        from calendar import monthrange
        end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        label = f'Monthly expense total · {month}'
        source = 'monthly_summary'
    elif period == 'weekly':
        picked = date.fromisoformat(validate_date(payload.get('date', '')))
        start = week_start(picked)
        end = start + timedelta(days=6)
        label = f'Weekly expense total · {start.isoformat()}'
        source = 'weekly_summary'
    else:
        raise ApiError(400, 'Choose weekly or monthly history.')
    if end >= local_today():
        raise ApiError(400, 'Choose a fully completed week or month; the current period is still in progress.')
    if start.year < 2000:
        raise ApiError(400, 'History must be from year 2000 onward.')
    return start.isoformat(), end.isoformat(), label, source


def summary_interval(day, source):
    """Expand a stored summary's end date to the real covered date interval."""
    d = date.fromisoformat(day)
    if source == 'monthly_summary':
        return d.replace(day=1), d
    if source == 'weekly_summary':
        return d - timedelta(days=6), d
    return d, d


def overlapping_expenses(conn, user_id, first, last):
    """Find expenses that cover any part of an interval, even cross-month weeks."""
    start = date.fromisoformat(first)
    end = date.fromisoformat(last)
    # Include all summaries, including ones with a Sunday after the period end.
    rows = conn.execute("""SELECT id,date,source FROM transactions
                           WHERE user_id=? AND kind='expense' AND date>=?""",
                        (user_id,(start-timedelta(days=31)).isoformat())).fetchall()
    return [row for row in rows
            if (lambda a,b: a <= end and b >= start)(*summary_interval(row['date'],row['source']))]


def check_summary_overlap(conn, user_id, day, ignored_id=None):
    for row in overlapping_expenses(conn,user_id,day,day):
        if row['source'] in ('monthly_summary','weekly_summary') and row['id'] != ignored_id:
            raise ApiError(409, 'This date is covered by a manually entered total. Delete the summary first to avoid double counting.')


def safe_user(row):
    return {'id':row['id'],'name':row['name'],'email':row['email'],'created_at':row['created_at']}


def hashed_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    rounds = 240000
    digest = hashlib.pbkdf2_hmac('sha256',password.encode(),salt,rounds)
    return f'pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}'


def verify_password(password, stored):
    try:
        algo,rounds,salt,expected=stored.split('$')
        if algo!='pbkdf2_sha256': return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(rounds))
        return hmac.compare_digest(actual,bytes.fromhex(expected))
    except (ValueError,AttributeError): return False


def to_public_tx(row):
    return {
        'id':row['id'], 'date':row['date'], 'description':row['description'],
        'category':row['category'], 'type':row['kind'],
        'amount':round(row['amount_cents']/100,2), 'source':row['source'],
    }


def month_filter(month):
    return month + '-01',following_month(month)+'-01'


def compute_dashboard(conn,user_id,month):
    low,high=month_filter(month)
    rows=conn.execute('SELECT * FROM transactions WHERE user_id=? AND date>=? AND date<? ORDER BY date DESC,id DESC',
                      (user_id,low,high)).fetchall()
    spent=sum(r['amount_cents'] for r in rows if r['kind']=='expense')
    income=sum(r['amount_cents'] for r in rows if r['kind']=='income')
    b=conn.execute('SELECT amount_cents FROM budgets WHERE user_id=? AND month=?',(user_id,month)).fetchone()
    # No preset budget: a new user's limit remains unset until they enter one.
    budget=b['amount_cents'] if b else None
    cats=Counter()
    for r in rows:
        if r['kind']=='expense': cats[r['category']]+=r['amount_cents']
    latest=month
    months=[latest]
    for _ in range(11):months.append(preceding_month(months[-1]))
    months.reverse()
    first=months[0]+'-01'; end=following_month(month)+'-01'
    historical=conn.execute('''SELECT substr(date,1,7) AS month, kind, SUM(amount_cents) AS total
                               FROM transactions WHERE user_id=? AND date>=? AND date<?
                               GROUP BY month,kind''',(user_id,first,end)).fetchall()
    metrics={(r['month'],r['kind']):r['total'] for r in historical}
    chart=[{'month':m,'expense':round(metrics.get((m,'expense'),0)/100,2),
            'income':round(metrics.get((m,'income'),0)/100,2)} for m in months]
    return {
        'month':month,
        'spent':round(spent/100,2),'income':round(income/100,2),
        'budget':round(budget/100,2) if budget is not None else None,
        'remaining':round((budget-spent)/100,2) if budget is not None else None,
        'budget_customized':bool(b),
        'transaction_count':len(rows),
        'categories':[{'name':c,'amount':round(n/100,2)} for c,n in cats.most_common()],
        'recent':[to_public_tx(r) for r in rows[:7]],
        'trend':chart,
    }


def compute_analytics(conn,user_id,month):
    dash=compute_dashboard(conn,user_id,month)
    low,high=month_filter(month)
    rows=conn.execute('SELECT * FROM transactions WHERE user_id=? AND date>=? AND date<?',(user_id,low,high)).fetchall()
    dailies=Counter()
    expenses=[]
    for r in rows:
        if r['kind']=='expense':
            dailies[r['date']]+=r['amount_cents']
            expenses.append(r['amount_cents']/100)
    import calendar
    y,m=map(int,month.split('-'))
    days=calendar.monthrange(y,m)[1]
    daily=[{'day':str(i),'amount':round(dailies.get(f'{month}-{i:02d}',0)/100,2)}
           for i in range(1,days+1)]
    from statistics import median
    prev=compute_dashboard(conn,user_id,preceding_month(month))
    growth = None if prev['spent']==0 else round((dash['spent']-prev['spent'])/prev['spent']*100,1)
    return {
        **dash,'daily':daily,'average_transaction':round(sum(expenses)/len(expenses),2) if expenses else 0,
        'median_transaction':round(median(expenses),2) if expenses else 0,
        'max_transaction':round(max(expenses),2) if expenses else 0,
        'growth_vs_previous':growth,
        'expense_count':len(expenses),
    }


def load_weekly_counts(conn, user_id):
    completed=week_start(local_today())
    rows=conn.execute("SELECT date,amount_cents FROM transactions WHERE user_id=? AND kind=? AND source!='monthly_summary' AND date<?",
                      (user_id,'expense',completed.isoformat())).fetchall()
    totals=Counter()
    for r in rows:
        monday=week_start(date.fromisoformat(r['date'])).isoformat()
        totals[monday]+=r['amount_cents']
    return [{'week':k,'total':v} for k,v in sorted(totals.items())]


def load_monthly_counts(conn,user_id):
    result=conn.execute('''SELECT substr(date,1,7) AS month, SUM(amount_cents) total
                           FROM transactions WHERE user_id=? AND kind='expense' AND source!='weekly_summary'
                           AND date < ? GROUP BY month ORDER BY month''',
                        (user_id,local_today().strftime('%Y-%m')+'-01')).fetchall()
    return [dict(r) for r in result]


class Handler(BaseHTTPRequestHandler):
    server_version = 'SmartSpend/1.0'

    def _reply(self,code,payload,headers=None,content_type='application/json; charset=utf-8'):
        data=(json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
              if content_type.startswith('application/json') else payload)
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store' if content_type.startswith('application/json') else 'public, max-age=300')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):pass

    def json(self, code, obj, headers=None):
        self._reply(code,obj,headers)

    def body(self):
        try: size=int(self.headers.get('Content-Length',0))
        except ValueError: raise ApiError(400,'Invalid request length.')
        if size<=0 or size>MAX_BODY: raise ApiError(413,'Request must contain data and be under 3 MB.')
        return self.rfile.read(size)

    def payload(self):
        if 'application/json' not in self.headers.get('Content-Type',''):
            raise ApiError(415,'Send application/json.')
        try: data=json.loads(self.body())
        except (ValueError,UnicodeDecodeError): raise ApiError(400,'Invalid JSON.')
        if not isinstance(data,dict):raise ApiError(400,'Expected a JSON object.')
        return data

    def session(self,conn):
        raw=self.headers.get('Cookie','')
        try:
            cookie=SimpleCookie();cookie.load(raw)
            token=cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ''
        except Exception:token=''
        if not token:return None
        token_hash=hashlib.sha256(token.encode()).hexdigest()
        row=conn.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?',
                         (token_hash, utc_now().isoformat())).fetchone()
        return row

    def require(self,conn,role=None,csrf=False):
        sess=self.session(conn)
        if sess is None: raise ApiError(401,'Sign in to continue.')
        if role and sess['role']!=role: raise ApiError(403,'This area is restricted.')
        if csrf and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),sess['csrf']):
            raise ApiError(403,'Security token expired. Refresh the page and sign in again.')
        return sess

    def set_session(self,conn,role,user_id=None):
        token=secrets.token_urlsafe(36)
        csrf=secrets.token_urlsafe(32)
        expiry=(utc_now()+timedelta(days=7)).isoformat()
        conn.execute('INSERT INTO sessions(token_hash,user_id,role,csrf,expires_at) VALUES (?,?,?,?,?)',
                     (hashlib.sha256(token.encode()).hexdigest(),user_id,role,csrf,expiry))
        secure='; Secure' if os.environ.get('SMARTSPEND_SECURE_COOKIE')=='1' else ''
        cookie=f'{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{secure}'
        return cookie,csrf

    def handle_request(self,method):
        parsed=urlparse(self.path);path=parsed.path
        params={k:v[0] for k,v in parse_qs(parsed.query).items()}
        if not path.startswith('/api/'):
            if method != 'GET':raise ApiError(405,'Method not allowed.')
            return self.static(path)
        if path=='/api/health' and method=='GET':return self.json(200,{'ok':True,'service':'smartspend'})
        with get_db() as conn:
            if path=='/api/auth/register' and method=='POST':
                data=self.payload()
                name=str(data.get('name','')).strip()
                email=str(data.get('email','')).strip().lower()
                password=str(data.get('password',''))
                if not 2<=len(name)<=70:raise ApiError(400,'Name must be 2–70 characters.')
                if len(email)>160 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):raise ApiError(400,'Enter a valid email.')
                if email==ADMIN_EMAIL:raise ApiError(400,'This email is reserved.')
                if not 8<=len(password)<=128:raise ApiError(400,'Password must contain 8–128 characters.')
                try:
                    cursor=conn.execute('INSERT INTO users(name,email,password_hash) VALUES (?,?,?)',
                                        (name,email,hashed_password(password)))
                except sqlite3.IntegrityError:raise ApiError(409,'An account with this email already exists.')
                cookie,csrf=self.set_session(conn,'user',cursor.lastrowid)
                conn.commit()
                return self.json(201,{'ok':True,'role':'user','csrf':csrf,'user':{'id':cursor.lastrowid,'name':name,'email':email}}, {'Set-Cookie':cookie})
            if path=='/api/auth/login' and method=='POST':
                data=self.payload()
                email=str(data.get('email','')).strip().lower()
                pw=str(data.get('password',''))
                if (hmac.compare_digest(email,ADMIN_EMAIL) and
                    hmac.compare_digest(pw,ADMIN_PASSWORD)):
                    cookie,csrf=self.set_session(conn,'admin')
                    conn.commit()
                    return self.json(200,{'ok':True,'role':'admin','csrf':csrf,'user':{'name':'Administrator','email':ADMIN_EMAIL}}, {'Set-Cookie':cookie})
                user=conn.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
                if not user or not verify_password(pw,user['password_hash']):
                    raise ApiError(401,'Incorrect email or password.')
                cookie,csrf=self.set_session(conn,'user',user['id'])
                conn.commit()
                return self.json(200,{'ok':True,'role':'user','csrf':csrf,'user':safe_user(user)}, {'Set-Cookie':cookie})
            if path=='/api/me' and method=='GET':
                sess=self.session(conn)
                if not sess:return self.json(200,{'authenticated':False})
                if sess['role']=='admin':
                    return self.json(200,{'authenticated':True,'role':'admin','csrf':sess['csrf'],
                                          'user':{'name':'Administrator','email':ADMIN_EMAIL}})
                user=conn.execute('SELECT * FROM users WHERE id=?',(sess['user_id'],)).fetchone()
                if user is None:return self.json(200,{'authenticated':False})
                return self.json(200,{'authenticated':True,'role':'user','csrf':sess['csrf'],'user':safe_user(user)})
            if path=='/api/auth/logout' and method=='POST':
                sess=self.require(conn,csrf=True)
                conn.execute('DELETE FROM sessions WHERE token_hash=?',(sess['token_hash'],))
                conn.commit()
                return self.json(200,{'ok':True},{'Set-Cookie':f'{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'})
            if path.startswith('/api/admin/'):
                sess=self.require(conn,'admin',csrf=(method!='GET'))
                if method!='GET':raise ApiError(405,'Admin dashboard is read-only.')
                return self.admin_routes(conn,path,params)
            sess=self.require(conn,'user',csrf=method in ('POST','PATCH','PUT','DELETE'))
            uid=sess['user_id']
            if path=='/api/dashboard' and method=='GET':
                month=iso_month(params.get('month',local_today().strftime('%Y-%m')))
                return self.json(200,compute_dashboard(conn,uid,month))
            if path=='/api/analytics' and method=='GET':
                month=iso_month(params.get('month',local_today().strftime('%Y-%m')))
                return self.json(200,compute_analytics(conn,uid,month))
            if path=='/api/transactions' and method=='GET':
                return self.transaction_list(conn,uid,params)
            if path=='/api/transactions' and method=='POST':
                tx=validate_transaction(self.payload())
                if tx['kind']=='expense':check_summary_overlap(conn,uid,tx['date'])
                cursor=conn.execute('''INSERT INTO transactions(user_id,date,description,category,kind,amount_cents,source)
                                    VALUES (?,?,?,?,?,?,?)''',
                                    (uid,tx['date'],tx['description'],tx['category'],tx['kind'],tx['amount_cents'],tx['source']))
                conn.commit()
                return self.json(201,{'ok':True,'id':cursor.lastrowid})
            match=re.fullmatch(r'/api/transactions/(\d+)',path)
            if match and method in ('PATCH','DELETE'):
                txid=int(match.group(1))
                current=conn.execute('SELECT * FROM transactions WHERE id=? AND user_id=?',(txid,uid)).fetchone()
                if not current: raise ApiError(404,'Transaction not found.')
                if method=='DELETE':
                    conn.execute('DELETE FROM transactions WHERE id=? AND user_id=?',(txid,uid))
                else:
                    if current['source'] in ('monthly_summary','weekly_summary'):
                        raise ApiError(400,'To update a period total, use Add past spending. You can delete it here.')
                    data=self.payload()
                    merged={'date':data.get('date',current['date']),
                            'description':data.get('description',current['description']),
                            'category':data.get('category',current['category']),
                            'type':data.get('type',current['kind']),
                            'amount':data.get('amount',current['amount_cents']/100)}
                    tx=validate_transaction(merged,current['source'])
                    if tx['kind']=='expense' and current['source'] not in ('monthly_summary','weekly_summary'):
                        check_summary_overlap(conn,uid,tx['date'],txid)
                    conn.execute('''UPDATE transactions SET date=?,description=?,category=?,kind=?,amount_cents=?
                                   WHERE id=? AND user_id=?''',
                                 (tx['date'],tx['description'],tx['category'],tx['kind'],tx['amount_cents'],txid,uid))
                conn.commit()
                return self.json(200,{'ok':True})
            if path=='/api/budget' and method=='POST':
                data=self.payload()
                month=iso_month(data.get('month',''))
                try:amount=float(data.get('amount'))
                except (TypeError,ValueError):raise ApiError(400,'Enter a valid budget.')
                if not 0<=amount<=10000000:raise ApiError(400,'Enter a budget from ₱0 to ₱10,000,000.')
                conn.execute('''INSERT INTO budgets(user_id,month,amount_cents) VALUES (?,?,?)
                                ON CONFLICT(user_id,month) DO UPDATE SET amount_cents=excluded.amount_cents''',
                             (uid,month,int(round(amount*100))))
                conn.commit()
                return self.json(200,{'ok':True})
            if path=='/api/history/manual' and method=='POST':
                data=self.payload()
                first,last,label,source=manual_period_bounds(data)
                cents=parse_money(data.get('amount'))
                overlapping=overlapping_expenses(conn,uid,first,last)
                same=[r for r in overlapping if r['source']==source]
                if len(overlapping)>1 or (overlapping and not same):
                    raise ApiError(409, 'This period already contains expense records or another summary. '
                                         'To avoid double counting, use your existing transactions or delete them before entering a period total.')
                if same:
                    conn.execute('''UPDATE transactions SET date=?,amount_cents=?,description=?,category=?
                        WHERE id=? AND user_id=?''',(last,cents,label,'Other',same[0]['id'],uid))
                    record_id=same[0]['id']; status=200
                else:
                    c=conn.execute('''INSERT INTO transactions(user_id,date,description,category,kind,amount_cents,source)
                        VALUES (?,?,?,?,?,?,?)''',(uid,last,label,'Other','expense',cents,source))
                    record_id=c.lastrowid; status=201
                conn.commit()
                return self.json(status,{'ok':True,'id':record_id,'period_start':first,
                                         'period_end':last,'updated':bool(same)})
            if path=='/api/predict' and method=='POST':
                period=self.payload().get('period','monthly')
                if period not in ('monthly','weekly'):
                    raise ApiError(400,'Choose weekly or monthly forecast.')
                excluded='monthly_summary' if period=='weekly' else 'weekly_summary'
                rows=conn.execute('SELECT date,amount_cents FROM transactions WHERE user_id=? AND kind=? AND source!=?',
                                  (uid,'expense',excluded)).fetchall()
                try:result=fit_and_forecast([dict(r) for r in rows],today=local_today(),period=period)
                except InsufficientHistory as e:
                    raise ApiError(422,str(e),months_available=e.available,months_required=e.needed)
                selected=next((x for x in result['evaluation'] if x['model']==result['selected_model']), None)
                cursor=conn.execute('''INSERT INTO predictions(user_id,forecast_month,predicted_cents,model,mae,rmse,metadata_json)
                                       VALUES (?,?,?,?,?,?,?)''',
                                    (uid,result['forecast_month'],int(round(result['predicted_amount']*100)),
                                     result['selected_model'],selected['mae'] if selected else -1,
                                     selected['rmse'] if selected else -1,json.dumps(result)))
                conn.commit()
                return self.json(201,{'ok':True,'id':cursor.lastrowid,**result})
            if path=='/api/predictions' and method=='GET':
                rows=conn.execute('SELECT * FROM predictions WHERE user_id=? ORDER BY id DESC LIMIT 15',(uid,)).fetchall()
                entries=[]
                for r in rows:
                    d=json.loads(r['metadata_json']);d['id']=r['id'];d['created_at']=r['created_at']
                    entries.append(d)
                return self.json(200,{'predictions':entries,'month_coverage':load_monthly_counts(conn,uid),
                                      'week_coverage':load_weekly_counts(conn,uid)})
            if path=='/api/datasets/info' and method=='GET':
                count=conn.execute('SELECT COUNT(*) c FROM transactions WHERE user_id=?',(uid,)).fetchone()['c']
                logs=conn.execute('SELECT * FROM imports WHERE user_id=? ORDER BY id DESC LIMIT 8',(uid,)).fetchall()
                months=conn.execute("SELECT COUNT(DISTINCT substr(date,1,7)) c FROM transactions WHERE user_id=? AND kind='expense'",(uid,)).fetchone()['c']
                sample=conn.execute("SELECT COUNT(*) c FROM transactions WHERE user_id=? AND source='sample'",(uid,)).fetchone()['c']
                return self.json(200,{'transaction_count':count,'expense_months':months,'sample_count':sample,
                                      'imports':[dict(r) for r in logs]})
            if path=='/api/datasets/import' and method=='POST':
                return self.import_data(conn,uid,self.payload())
            if path=='/api/datasets/sample' and method=='POST':
                if conn.execute("SELECT 1 FROM transactions WHERE user_id=? AND source='sample' LIMIT 1",(uid,)).fetchone():
                    raise ApiError(409,'Sample data has already been added to this account. Remove sample transactions before loading it again.')
                rows=sample_rows(today=local_today())
                for r in rows:
                    if r['kind']=='expense':check_summary_overlap(conn,uid,r['date'])
                conn.executemany('''INSERT INTO transactions(user_id,date,description,category,kind,amount_cents,source)
                                    VALUES (?,?,?,?,?,?,?)''',
                                 [(uid,r['date'],r['description'],r['category'],r['kind'],r['amount_cents'],'sample') for r in rows])
                conn.execute('INSERT INTO imports(user_id,filename,imported,duplicates,invalid,outliers) VALUES (?,?,?,?,?,?)',
                             (uid,'FICTIONAL classroom demo dataset',len(rows),0,0,0))
                conn.commit()
                return self.json(201,{'ok':True,'imported':len(rows),'message':'Fictional sample records added. They are labeled Sample in the ledger.'})
            if path=='/api/datasets/export' and method=='GET':
                rows=conn.execute('SELECT date,description,category,amount_cents,kind,source FROM transactions WHERE user_id=? ORDER BY date DESC,id DESC',(uid,)).fetchall()
                buffer=io.StringIO();writer=csv.writer(buffer)
                writer.writerow(['date','description','category','amount','type','source'])
                for r in rows:writer.writerow([r['date'],r['description'],r['category'],f"{r['amount_cents']/100:.2f}",r['kind'],r['source']])
                content=buffer.getvalue().encode('utf-8-sig')
                return self._reply(200,content,{'Content-Disposition':'attachment; filename="smartspend_transactions.csv"'},'text/csv; charset=utf-8')
            if path=='/api/settings' and method=='PATCH':
                data=self.payload()
                current=conn.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
                name=str(data.get('name',current['name'])).strip()
                if not 2<=len(name)<=70:raise ApiError(400,'Name must be 2–70 characters.')
                if data.get('new_password'):
                    if not verify_password(str(data.get('current_password','')),current['password_hash']):
                        raise ApiError(403,'Current password is incorrect.')
                    password=str(data['new_password'])
                    if not 8<=len(password)<=128:raise ApiError(400,'New password needs 8–128 characters.')
                    conn.execute('UPDATE users SET name=?,password_hash=? WHERE id=?',
                                 (name,hashed_password(password),uid))
                else:conn.execute('UPDATE users SET name=? WHERE id=?',(name,uid))
                conn.commit()
                return self.json(200,{'ok':True})
            raise ApiError(404,'Endpoint not found.')

    def transaction_list(self,conn,uid,params):
        conditions=['user_id=?'];args=[uid]
        month=params.get('month',local_today().strftime('%Y-%m'))
        if month!='all':
            iso_month(month);lo,hi=month_filter(month)
            conditions+=['date>=?','date<?'];args += [lo,hi]
        kind=params.get('type','all')
        if kind not in ('expense','income','all'):raise ApiError(400,'Invalid transaction type.')
        if kind!='all':conditions.append('kind=?');args.append(kind)
        search=str(params.get('search','')).strip()[:80]
        if search:conditions.append('(description LIKE ? OR category LIKE ?)');args.extend(['%'+search+'%']*2)
        where=' AND '.join(conditions)
        try:page=max(1,int(params.get('page',1)))
        except ValueError:page=1
        page=min(page,10000);limit=50
        total=conn.execute('SELECT COUNT(*) c FROM transactions WHERE '+where,args).fetchone()['c']
        rows=conn.execute('SELECT * FROM transactions WHERE '+where+' ORDER BY date DESC,id DESC LIMIT ? OFFSET ?',
                          args+[limit,(page-1)*limit]).fetchall()
        return self.json(200,{'transactions':[to_public_tx(r) for r in rows],
                              'total':total,'page':page,'pages':max(1,(total+limit-1)//limit)})

    def import_data(self,conn,uid,payload):
        raw=payload.get('csv','')
        filename=re.sub(r'[^\w. -]','_',str(payload.get('filename','upload.csv')))[:100]
        if not isinstance(raw,str) or len(raw)>2_000_000:raise ApiError(400,'CSV content missing or too large (max 2 MB).')
        try:
            reader=csv.DictReader(io.StringIO(raw.lstrip('\ufeff')))
            if reader.fieldnames is None:raise ApiError(400,'CSV must have a header row.')
            reader.fieldnames=[str(h).strip().lower() for h in reader.fieldnames]
            required={'date','description','amount'}
            if not required.issubset(set(reader.fieldnames)):
                raise ApiError(400,'CSV needs date, description and amount columns. category and type are optional.')
            rows=list(reader)
        except csv.Error:raise ApiError(400,'Invalid CSV formatting.')
        if len(rows)>10000:raise ApiError(400,'Maximum 10,000 CSV rows per upload.')
        inserted=duplicate=invalid=outlier=0
        seen=set()
        for r in rows:
            try:
                tx=validate_transaction(r,'import')
            except (ApiError,TypeError):invalid+=1;continue
            key=(tx['date'],tx['description'].lower(),tx['category'],tx['kind'],tx['amount_cents'])
            if key in seen:duplicate+=1;continue
            seen.add(key)
            old=conn.execute('''SELECT 1 FROM transactions WHERE user_id=? AND date=? AND LOWER(description)=?
                                 AND category=? AND kind=? AND amount_cents=? LIMIT 1''',
                             (uid,*key)).fetchone()
            if old:duplicate+=1;continue
            if tx['kind']=='expense':
                try:check_summary_overlap(conn,uid,tx['date'])
                except ApiError:
                    invalid+=1;continue
            if tx['amount_cents']>10000000:outlier+=1  # > ₱100,000, flagged, still retained
            conn.execute('''INSERT INTO transactions(user_id,date,description,category,kind,amount_cents,source)
                            VALUES (?,?,?,?,?,?,?)''',
                         (uid,tx['date'],tx['description'],tx['category'],tx['kind'],tx['amount_cents'],'import'))
            inserted+=1
        conn.execute('INSERT INTO imports(user_id,filename,imported,duplicates,invalid,outliers) VALUES (?,?,?,?,?,?)',
                     (uid,filename,inserted,duplicate,invalid,outlier))
        conn.commit()
        return self.json(200,{'ok':True,'imported':inserted,'duplicates':duplicate,'invalid':invalid,
                              'outliers_flagged':outlier,'total_rows':len(rows),
                              'notes':['Rows missing required values or containing invalid dates/amounts are skipped.',
                                       'Duplicate rows are skipped; categories and types are normalized.',
                                       'Amounts above ₱100,000 are flagged but kept for review.']})

    def admin_routes(self,conn,path,params):
        if path=='/api/admin/export':
            kind=params.get('dataset','transactions')
            available={
                'users':('SELECT id,name,email,created_at FROM users ORDER BY id',
                         ['id','name','email','created_at']),
                'transactions':('''SELECT t.id,t.user_id,u.email,t.date,t.description,t.category,
                                        t.kind,ROUND(t.amount_cents/100.0,2) AS amount_php,t.source,t.created_at
                                 FROM transactions t JOIN users u ON u.id=t.user_id ORDER BY t.id''',
                                ['id','user_id','email','date','description','category','kind',
                                 'amount_php','source','created_at']),
                'predictions':('''SELECT p.id,p.user_id,u.email,p.forecast_month,
                                        ROUND(p.predicted_cents/100.0,2) AS predicted_php,
                                        p.model,CASE WHEN p.mae<0 THEN NULL ELSE p.mae END AS mae,
                                        CASE WHEN p.rmse<0 THEN NULL ELSE p.rmse END AS rmse,p.created_at
                                 FROM predictions p JOIN users u ON u.id=p.user_id ORDER BY p.id''',
                               ['id','user_id','email','forecast_month','predicted_php','model','mae','rmse','created_at']),
                'imports':('''SELECT i.id,i.user_id,u.email,i.filename,i.imported,i.duplicates,
                                     i.invalid,i.outliers,i.created_at
                              FROM imports i JOIN users u ON u.id=i.user_id ORDER BY i.id''',
                           ['id','user_id','email','filename','imported','duplicates','invalid','outliers','created_at']),
            }
            if kind not in available:raise ApiError(400,'Unknown dataset for export.')
            query,fields=available[kind]
            buf=io.StringIO();w=csv.writer(buf);w.writerow(fields)
            for row in conn.execute(query):w.writerow([row[name] for name in fields])
            return self._reply(200,buf.getvalue().encode('utf-8-sig'),
                               {'Content-Disposition':f'attachment; filename="smartspend_admin_{kind}.csv"'},
                               'text/csv; charset=utf-8')
        if path=='/api/admin/summary':
            a={}
            for key,sql in [('users','SELECT COUNT(*) FROM users'),
                            ('transactions','SELECT COUNT(*) FROM transactions'),
                            ('predictions','SELECT COUNT(*) FROM predictions'),
                            ('imports','SELECT COUNT(*) FROM imports'),
                            ('total_expenses','SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE kind="expense"')]:
                a[key]=conn.execute(sql).fetchone()[0]
            a['total_expenses']=round(a['total_expenses']/100,2)
            cats=conn.execute('''SELECT category,SUM(amount_cents) amount FROM transactions
                                 WHERE kind='expense' GROUP BY category ORDER BY amount DESC LIMIT 6''').fetchall()
            a['categories']=[{'name':r['category'],'amount':round(r['amount']/100,2)} for r in cats]
            return self.json(200,a)
        if path=='/api/admin/users':
            rows=conn.execute('''SELECT u.id,u.name,u.email,u.created_at,
                                  COUNT(t.id) transaction_count,
                                  COALESCE(SUM(CASE WHEN t.kind='expense' THEN t.amount_cents ELSE 0 END),0) spent
                                  FROM users u LEFT JOIN transactions t ON t.user_id=u.id
                                  GROUP BY u.id ORDER BY u.id DESC LIMIT 300''').fetchall()
            return self.json(200,{'users':[{'id':r['id'],'name':r['name'],'email':r['email'],
                                              'created_at':r['created_at'], 'transaction_count':r['transaction_count'],
                                              'spent':round(r['spent']/100,2)} for r in rows]})
        if path=='/api/admin/transactions':
            rows=conn.execute('''SELECT t.*,u.name user_name,u.email user_email FROM transactions t
                                 JOIN users u ON t.user_id=u.id ORDER BY t.id DESC LIMIT 150''').fetchall()
            return self.json(200,{'transactions':[{'id':r['id'],'user':r['user_name'],
                                                     'email':r['user_email'],**to_public_tx(r)} for r in rows]})
        if path=='/api/admin/predictions':
            rows=conn.execute('''SELECT p.*,u.name user_name,u.email user_email FROM predictions p
                                 JOIN users u ON p.user_id=u.id ORDER BY p.id DESC LIMIT 100''').fetchall()
            return self.json(200,{'predictions':[{'id':r['id'],'user':r['user_name'],'email':r['user_email'],
                            'forecast_month':r['forecast_month'],'prediction':round(r['predicted_cents']/100,2),
                            'model':r['model'],'mae':r['mae'] if r['mae']>=0 else None,
                            'rmse':r['rmse'] if r['rmse']>=0 else None,
                            'created_at':r['created_at']} for r in rows]})
        if path=='/api/admin/imports':
            rows=conn.execute('''SELECT i.*,u.name user_name FROM imports i JOIN users u ON i.user_id=u.id
                                 ORDER BY i.id DESC LIMIT 100''').fetchall()
            return self.json(200,{'imports':[dict(r) for r in rows]})
        raise ApiError(404,'Admin endpoint not found.')

    def static(self,path):
        routes={'/':'index.html','/index.html':'index.html','/app.css':'app.css','/app.js':'app.js','/favicon.svg':'favicon.svg'}
        file=routes.get(path)
        if not file:raise ApiError(404,'Not found.')
        p=STATIC / file
        mime={'html':'text/html; charset=utf-8','css':'text/css; charset=utf-8',
              'js':'text/javascript; charset=utf-8','svg':'image/svg+xml'}.get(p.suffix.lstrip('.'),'application/octet-stream')
        return self._reply(200,p.read_bytes(),content_type=mime)

    def _dispatch(self,method):
        try:self.handle_request(method)
        except ApiError as e:self.json(e.code,{'error':e.message,**e.details})
        except BrokenPipeError:pass
        except Exception as e:
            print(f'[server-error] {type(e).__name__}: {e}',flush=True)
            self.json(500,{'error':'Unexpected server error. Check server logs.'})

    def do_GET(self):self._dispatch('GET')
    def do_POST(self):self._dispatch('POST')
    def do_PATCH(self):self._dispatch('PATCH')
    def do_DELETE(self):self._dispatch('DELETE')


def main():
    setup()
    host=os.environ.get('HOST','127.0.0.1')
    port=int(os.environ.get('PORT','8000'))
    httpd=ThreadingHTTPServer((host,port),Handler)
    print(f'Smart Spend → http://localhost:{port}',flush=True)
    try:httpd.serve_forever()
    except KeyboardInterrupt:pass
    finally:httpd.server_close()


if __name__=='__main__':main()