"""Honest monthly/weekly forecasting with a useful early-history fallback.

Three complete months / six complete weeks permit a low-confidence trend estimate.
ML is evaluated only with enough examples to reserve an untouched time holdout.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


class InsufficientHistory(ValueError):
    def __init__(self, message, available=0, needed=3):
        super().__init__(message)
        self.available = available
        self.needed = needed


def following_month(month: str) -> str:
    year, m = map(int, month.split('-'))
    return f'{year + (m == 12):04d}-{m % 12 + 1:02d}'


def preceding_month(month: str) -> str:
    year, m = map(int, month.split('-'))
    return f'{year - (m == 1):04d}-{12 if m == 1 else m - 1:02d}'


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def advance(period: str, key: str) -> str:
    if period == 'monthly':
        return following_month(key)
    return (date.fromisoformat(key) + timedelta(days=7)).isoformat()


def retreat(period: str, key: str) -> str:
    if period == 'monthly':
        return preceding_month(key)
    return (date.fromisoformat(key) - timedelta(days=7)).isoformat()


def lag_features(values, target, period):
    """Only prior observed/predicted periods become inputs, never the target."""
    a, b, c = [float(x) for x in values[-3:]]
    if period == 'monthly':
        seasonal = int(target[5:7]) / 12
    else:
        seasonal = date.fromisoformat(target).timetuple().tm_yday / 365.25
    return [c, b, a, (a + b + c) / 3, (c - a) / 2,
            math.sin(2 * math.pi * seasonal), math.cos(2 * math.pi * seasonal)]


def simple_forecast(values):
    """Robust conservative trend: recency-weighted mean with a capped change."""
    recent = values[-3:]
    weighted = 0.2 * recent[0] + 0.3 * recent[1] + 0.5 * recent[2]
    change = (recent[-1] - recent[0]) / 2
    correction = max(-0.15 * weighted, min(0.15 * weighted, change * 0.2))
    return max(0., float(weighted + correction))


def fit_and_forecast(expense_rows: list[dict], today: date | None = None, period='monthly'):
    """Forecast next calendar month / next full Mon-Sun week.

    Only complete periods are used; missing periods are NOT treated as zero.
    Latest contiguous history is used, so older isolated records don't break training.
    """
    if period not in ('weekly', 'monthly'):
        raise ValueError('Period must be weekly or monthly.')
    today = today or date.today()
    if period == 'monthly':
        last_complete = preceding_month(today.strftime('%Y-%m'))
        current = today.strftime('%Y-%m')
        target = following_month(current)
        minimum = 3
        ml_min = 12
        period_name = 'months'
    else:
        current = week_start(today).isoformat()
        last_complete = retreat('weekly', current)
        target = advance('weekly', current)
        minimum = 6
        ml_min = 14
        period_name = 'weeks'

    totals_by_period = {}
    for row in expense_rows:
        d = date.fromisoformat(str(row['date']))
        key = d.strftime('%Y-%m') if period == 'monthly' else week_start(d).isoformat()
        if key <= last_complete:
            totals_by_period[key] = totals_by_period.get(key, 0) + int(row['amount_cents']) / 100

    contiguous = []
    cursor = last_complete
    while cursor in totals_by_period and len(contiguous) < 260:
        contiguous.append(cursor)
        cursor = retreat(period, cursor)
    contiguous.reverse()
    if len(contiguous) < minimum:
        raise InsufficientHistory(
            f'{len(contiguous)} consecutive complete {period_name} recorded (ending {last_complete}). '
            f'Add expense transactions or manual totals for at least {minimum} consecutive completed {period_name}. '
            'An unfinished current period is excluded; missing periods are not assumed to be zero.',
            len(contiguous), minimum,
        )

    values = [totals_by_period[k] for k in contiguous]
    reports = []
    selected = 'Recency-weighted trend (early estimate)'
    stage = 'early'
    train_samples = test_samples = 0

    if len(values) >= ml_min:
        X = np.asarray([lag_features(values[i-3:i], contiguous[i], period)
                        for i in range(3, len(values))], dtype=float)
        y = np.asarray(values[3:], dtype=float)
        test_samples = max(2, math.ceil(len(y) * .25))
        X_train, X_test = X[:-test_samples], X[-test_samples:]
        y_train, y_test = y[:-test_samples], y[-test_samples:]
        train_samples = len(X_train)
        models = {
            'Ridge Regression': make_pipeline(StandardScaler(), Ridge(alpha=15.0)),
            'Random Forest': RandomForestRegressor(n_estimators=120, max_depth=5,
                                                   min_samples_leaf=2, random_state=42),
        }
        for name, model in models.items():
            model.fit(X_train, y_train)
            predicted = np.maximum(model.predict(X_test), 0)
            reports.append((name, predicted))
        reports.append(('Last-period baseline', np.maximum(X_test[:, 0], 0)))
        evaluation = []
        for name, predicted in reports:
            evaluation.append({
                'model': name,
                'mae': round(float(mean_absolute_error(y_test, predicted)), 2),
                'rmse': round(float(np.sqrt(mean_squared_error(y_test, predicted))), 2),
                'r2': round(float(r2_score(y_test, predicted)), 3) if len(y_test) > 1 else None,
            })
        selected = min(evaluation, key=lambda x: x['mae'])['model']
        if selected in models:
            fitted = models[selected]
            fitted.fit(X, y)
            def forecast_one(history, key):
                return max(0., float(fitted.predict(np.asarray([lag_features(history[-3:], key, period)]))[0]))
        else:
            def forecast_one(history, key):
                return max(0., float(history[-1]))
        reports = evaluation
        stage = 'evaluated'
    else:
        def forecast_one(history, key):
            return simple_forecast(history)

    # An incomplete current month/week is projected before the upcoming period.
    working = values[:]
    current_projection = None
    pointer = advance(period, last_complete)
    while pointer <= target:
        prediction = forecast_one(working, pointer)
        working.append(prediction)
        if pointer == current:
            current_projection = prediction
        pointer = advance(period, pointer)

    warning = ('Limited history: this is a recency-weighted trend estimate, NOT a trained or '
               'validated machine-learning prediction. It has no measured accuracy score. '
               'Record more complete periods for model evaluation. '
               if stage == 'early' else
               'Models and a last-period baseline were compared on a later chronological test set. '
               'Past error does not guarantee future accuracy. ')
    warning += ('The current incomplete period is estimated before forecasting the next period. '
                'Missing spending and unexpected changes can alter the result. '
                'Manually entered period totals must not overlap detailed transactions.')
    return {
        'period_type': period,
        'forecast_month': target,  # legacy database column; week forecasts store YYYY-MM-DD
        'forecast_period': target,
        'predicted_amount': round(working[-1], 2),
        'current_period_projection': round(current_projection, 2) if current_projection is not None else None,
        'selected_model': selected,
        'prediction_stage': stage,
        'confidence_label': 'Limited history' if stage == 'early' else 'Measured historical holdout',
        'evaluation': reports,
        'train_samples': train_samples,
        'test_samples': test_samples,
        'history_periods': len(contiguous),
        'history_months': len(contiguous) if period == 'monthly' else 0,
        'history_start': contiguous[0],
        'history_end': contiguous[-1],
        'selection_metric': ('No model comparison with limited history' if stage == 'early'
                             else 'Lowest MAE across Ridge, Random Forest and last-period baseline on a chronological holdout'),
        'warning': warning,
    }