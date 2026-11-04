"""Clearly labeled fictional expense data, deterministic for classroom demonstrations."""
from datetime import date
import calendar
import random

from ml_model import preceding_month

CATEGORIES = ['Food & Drinks', 'Transportation', 'Shopping', 'Bills & Utilities',
              'Health', 'Entertainment', 'Education', 'Other']
MERCHANTS = {
    'Food & Drinks': ['Groceries', 'Lunch', 'Coffee', 'Dinner'],
    'Transportation': ['Jeepney / Bus', 'Fuel', 'Ride booking'],
    'Shopping': ['Clothing', 'Household items'],
    'Bills & Utilities': ['Internet', 'Electricity', 'Water'],
    'Health': ['Pharmacy', 'Health essentials'],
    'Entertainment': ['Streaming', 'Cinema'],
    'Education': ['School supplies', 'Books'],
    'Other': ['Miscellaneous', 'Personal care'],
}


def sample_rows(today=None, num_months=24):
    today = today or date.today()
    rng = random.Random(137)
    last = preceding_month(today.strftime('%Y-%m'))
    months = [last]
    for _ in range(num_months-1):
        months.append(preceding_month(months[-1]))
    months.reverse()
    rows = []
    for i, month in enumerate(months):
        year, mm = map(int, month.split('-'))
        last_day = calendar.monthrange(year,mm)[1]
        season = 1.15 if mm in (12, 1, 6) else 1.0
        trend = 1 + i*0.008
        for category, count, lo, hi in [
            ('Food & Drinks', 6, 180, 950),
            ('Transportation', 5, 65, 410),
            ('Shopping', 2, 240, 1250),
            ('Bills & Utilities', 3, 490, 2100),
            ('Health', 1, 180, 550),
            ('Entertainment', 2, 120, 520),
            ('Education', 1, 160, 760),
        ]:
            for j in range(count):
                amt = round(rng.uniform(lo,hi)*trend*season, 2)
                day = (j*6 + 2 + rng.randint(0,5)) % last_day + 1
                rows.append({
                    'date': f'{month}-{day:02d}',
                    'description': rng.choice(MERCHANTS[category]),
                    'category': category,
                    'amount_cents': int(round(amt*100)),
                    'kind': 'expense',
                    'source': 'sample',
                })
        for j in range(2):
            rows.append({
                'date': f'{month}-{5 if j==0 else 20:02d}',
                'description': 'Sample income',
                'category': 'Income',
                'amount_cents': int((14500 + i*80 + rng.randint(-350,350))*100),
                'kind': 'income',
                'source': 'sample',
            })
    return rows
