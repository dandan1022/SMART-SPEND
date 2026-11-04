"""Time-ordered monthly expense forecasting; no fabricated model scores."""
from __future__ import annotations

import math
from datetime import date

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


class InsufficientHistory(ValueError):
    def __init__(self, message, available=0, needed=12):
        super().__init__(message)
        self.available = available
        self.needed = needed


def following_month(month: str) -> str:
    year, m = map(int, month.split('-'))
    return f'{year + (m == 12):04d}-{m % 12 + 1:02d}'


def preceding_month(month: str) -> str:
    year, m = map(int, month.split('-'))
    return f'{year - (m == 1):04d}-{12 if m == 1 else m - 1:02d}'


def features(values, target_month):
    """All input features refer to MONTHS BEFORE the target month."""
    a, b, c = [float(x) for x in values[-3:]]  # oldest, middle, latest
    month_number = int(target_month.split('-')[1])
    return [c, b, a, (a+b+c)/3, (c-a)/2,
            math.sin(2*math.pi*month_number/12), math.cos(2*math.pi*month_number/12)]


def fit_and_forecast(expense_rows: list[dict], today: date | None = None):
    """Train only on complete historical months; forecast the next calendar month.

    expense_rows contains {'date': ISO date, 'amount_cents': integer}.
    If the current month is partial, forecasting recursively covers it before next month.
    """
    today = today or date.today()
    current_month = today.strftime('%Y-%m')
    last_complete = preceding_month(current_month)
    monthly = {}
    for r in expense_rows:
        month = str(r['date'])[:7]
        if month <= last_complete:
            monthly[month] = monthly.get(month, 0) + int(r['amount_cents']) / 100

    if not monthly:
        raise InsufficientHistory('Add at least 12 consecutive complete months of expense records before training.', 0)
    months = []
    cursor = min(monthly)
    while cursor <= last_complete:
        months.append(cursor)
        cursor = following_month(cursor)
        if len(months) > 2400:
            raise ValueError('Date range is too long.')
    missing = [m for m in months if m not in monthly]
    if missing:
        raise InsufficientHistory(
            'Training requires consecutive complete months. Missing expense records for: '
            + ', '.join(missing[:5]) + ('…' if len(missing)>5 else '')
            + '. Record those months or use the clearly labeled sample data.',
            len(months)-len(missing), 12,
        )
    if len(months) < 12:
        raise InsufficientHistory(
            f'{len(months)} complete months recorded. At least 12 consecutive complete months are required.',
            len(months), 12,
        )
    totals = [monthly[m] for m in months]
    X = np.array([features(totals[i-3:i], months[i]) for i in range(3,len(months))])
    y = np.array(totals[3:])
    test_n = max(2, math.ceil(len(y)*0.25))
    X_train, X_test = X[:-test_n], X[-test_n:]
    y_train, y_test = y[:-test_n], y[-test_n:]
    if len(X_train) < 6:
        raise InsufficientHistory('Add more complete months to create a meaningful training set.', len(months), 12)

    candidates = {
        'Ridge Regression': make_pipeline(StandardScaler(), Ridge(alpha=15.0)),
        'Random Forest': RandomForestRegressor(n_estimators=120, max_depth=5,
                                               min_samples_leaf=2, random_state=42),
    }
    reports = []
    for name, model in candidates.items():
        model.fit(X_train, y_train)
        preds = np.maximum(model.predict(X_test), 0)
        reports.append({
            'model': name,
            'mae': round(float(mean_absolute_error(y_test, preds)), 2),
            'rmse': round(float(np.sqrt(mean_squared_error(y_test, preds))), 2),
            'r2': round(float(r2_score(y_test,preds)), 3),
        })
    naive = X_test[:, 0]
    baseline = {
        'model': 'Last Month Baseline',
        'mae': round(float(mean_absolute_error(y_test,naive)), 2),
        'rmse': round(float(np.sqrt(mean_squared_error(y_test,naive))), 2),
        'r2': round(float(r2_score(y_test,naive)), 3),
    }
    reports.append(baseline)
    # Compare fixed models on one chronological holdout. No repeated tuning on test data.
    best = min(reports[:2], key=lambda x: x['mae'])
    winner = candidates[best['model']]
    winner.fit(X,y)  # only after evaluation, retrain on all completed historical months

    working = totals[:]
    projected_current = None
    forecast_month = following_month(current_month)
    cursor = following_month(last_complete)
    while cursor <= forecast_month:
        pred = max(0., float(winner.predict(np.asarray([features(working[-3:],cursor)]))[0]))
        working.append(pred)
        if cursor == current_month:
            projected_current = pred
        cursor = following_month(cursor)

    result = {
        'forecast_month': forecast_month,
        'predicted_amount': round(working[-1], 2),
        'current_month_projection': round(projected_current, 2) if projected_current is not None else None,
        'selected_model': best['model'],
        'evaluation': reports,
        'train_samples': len(X_train),
        'test_samples': len(X_test),
        'history_months': len(months),
        'history_start': months[0],
        'history_end': months[-1],
        'selection_metric': 'Lowest MAE among the two ML algorithms on one chronological holdout',
        'warning': ('Forecasts are estimates, not guarantees. A next-month forecast uses a recursive '
                    'projection for the still-incomplete current month; missing real-world expenses '
                    'and future changes can make the forecast inaccurate.'),
    }
    return result
