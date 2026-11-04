# Smart Spend
**Machine Learning-Powered Personal Monthly & Weekly Expense Predictor**

Responsive Python website with user registration/login, expense management, spending analytics, machine-learning forecasts, and a **read-only admin database dashboard**.

## Deploy from GitHub to Render
1. **Extract the ZIP.** Create a new GitHub repository and upload **all the files and folders inside the extracted folder** (not the ZIP itself). `server.py`, `render.yaml`, and `requirements.txt` must be at the root of the repository.
2. In Render, choose **New → Blueprint** and connect your repository. Render reads `render.yaml` and configures the Python web service. Enter a private value for **`SMARTSPEND_ADMIN_PASSWORD`** when prompted.
3. When the deployment finishes, open the `onrender.com` link from Render.

**Alternative manual Render Web Service settings:**
- Language/runtime: `Python`
- Root directory: leave blank
- Build command: `pip install -r requirements.txt`
- Start command: `python server.py`
- Environment: `HOST=0.0.0.0`, `SMARTSPEND_SECURE_COOKIE=1`, `SMARTSPEND_TZ=Asia/Manila`, `SMARTSPEND_ADMIN_EMAIL=admin@smartspend.local`, and **your private** `SMARTSPEND_ADMIN_PASSWORD`.

**Important database warning:** This project uses local **SQLite**. Render Free has **ephemeral storage**. Accounts, transactions, and forecasts can disappear after restarts/redeploys. It is suitable for a short class demonstration with fictional data only. For real use, migrate the database to hosted persistent storage first, and harden account protections. SQLite is not hosted in Google Sheets. Never put real sensitive financial data into this demo.

## Admin access
Log in from the normal login page using `SMARTSPEND_ADMIN_EMAIL` and `SMARTSPEND_ADMIN_PASSWORD`. The configured admin can inspect users, transactions, model results, and imports and export CSVs. **It cannot see plaintext user passwords; passwords are salted and hashed.**

If you run the website locally without configuring environment variables, the classroom demo credentials are:
- Email: `admin@smartspend.local`
- Password: `SmartSpend@2026`

**Never use that published demo password on a public website.** Configure a unique strong admin password in Render.

## Run on Mac / locally
Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Open **http://localhost:8000**. The database (`smartspend.sqlite3`) is created automatically on first start. `.gitignore` prevents committing it to GitHub.

## Features and required academic functionality
- **Accounts:** register, log in, log out, update user settings; server-side administrator role and access control.
- **Expense tracking:** add, edit, delete, filter, and export individual transactions; PHP currency; user-chosen monthly budget (no automatic suggested amount).
- **Manual history:** on **Datasets → Enter a total**, type a completed calendar month’s total or completed Monday–Sunday week’s total; no spreadsheet needed. Summary entries show in Transactions and can be updated through the same form or deleted from Transactions. Entries cannot overlap existing expenses or period totals (prevents double counting). You can also add normal daily expense transactions manually.
- **Datasets:** optional CSV import, handle missing categories, normalize categories, flag high-value outliers, discard invalid/duplicate rows; view import statistics.
- **Exploratory analysis:** monthly spending trend, category doughnut chart, category comparison bars, daily activity chart, descriptive statistics.
- **Prediction:** Choose **Monthly** or **Weekly** in AI Predictor. At least **3 consecutive completed months** or **6 consecutive completed weeks** enable a **clearly labeled, low-confidence early trend estimate**; this is not trained ML and has no fabricated accuracy score. With **12 completed months** or **14 completed weeks**, Ridge Regression, Random Forest, and a last-period baseline are compared on a chronological holdout using MAE, RMSE, and R². The method with the lowest holdout MAE is used to forecast the next month or next full week.
- **Administrator:** read-only overview of accounts, transactions, imports and ML predictions; CSV exports.
- **Privacy:** passwords are hashed; user records are isolated by account; admin views financial records as part of its project role.

### Dataset
The optional `sample_data/demo_transactions.csv` contains **fictional** example records and is accompanied by `sample_data/DATA_DICTIONARY.md`. The website also has a **Load fictional demo data** button. Monthly budgets are never automatically filled in: each user sets a budget for each month using the **Set my budget** control on Overview. Until they do, the dashboard shows **Not set** and does not invent a remaining balance. Only **3 consecutive completed months** or **6 consecutive completed weeks** are needed for an early estimate. For trained/evaluated ML, supply **12 completed months** or **14 completed weeks**, respectively. More consistent and representative data may improve reliability but accuracy is not guaranteed.

CSV columns: `date`, `description`, `amount`; optional: `category`, `type` (`expense` or `income`).

### ML methodology and limitations
Historical expenses are grouped by *completed calendar month* or *completed Monday–Sunday week*; incomplete periods and missing periods are not interpreted as zero. For limited data (3–11 months or 6–13 weeks), a recency-weighted average and capped trend provide an early estimate without pretending it was evaluated. With enough data, features from the preceding three periods, their rolling average, recent trend and seasonal encoding are used. The last ~25% of training examples are reserved as a chronological test set for a Ridge regressor, Random Forest, and an unchanged-last-period baseline. The method with the lowest MAE is used, refitting trained ML models on all completed data after evaluation. The current incomplete period is estimated recursively before forecasting the next calendar month/week.

Forecasts are estimates, not guarantees, especially early estimates with 3 months / 6 weeks of history. Weekly totals and monthly totals are kept separate in forecasting to avoid combining incompatible summaries. Very small or fictional datasets cannot establish real-world predictive accuracy; abrupt spending changes, emergencies, missing expenses and broader economic factors are not represented. The project should not be used for important financial decisions without independent verification. The full academic research report/presentation are **not** included.

## Files
```
server.py                Backend web server, authentication, admin and API
database.py             SQLite schema and connections
ml_model.py              Model training, evaluation and prediction
demo_data.py             Fictional demo data generator
requirements.txt        Python dependencies
render.yaml              Render Blueprint deployment settings
static/                  Website HTML, CSS, JS, favicon
sample_data/             Documented fictional CSV sample dataset
.gitignore               Excludes secrets, databases and temporary files
README.md                Setup and project explanation
```

### Update your existing Render deployment
Push updated source files to the GitHub `main` branch. If auto-deploy is enabled for Render, it will rebuild the service automatically. Open the public `onrender.com` URL, reload, and use **Datasets → Enter a total** or **AI Predictor → Add history manually**. The SQLite database is unchanged in format and can read the existing accounts, although Render Free's ephemeral filesystem can still lose them on restart/redeploy.