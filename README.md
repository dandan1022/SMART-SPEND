# Smart Spend
**Machine Learning-Powered Personal Monthly Expense Predictor**

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
- **Expense tracking:** add, edit, delete, filter, and export transactions; PHP currency; user-chosen monthly budget (no automatic suggested amount).
- **Datasets:** import CSV, handle missing categories, normalize categories, flag high-value outliers, discard invalid/duplicate rows; view import statistics.
- **Exploratory analysis:** monthly spending trend, category doughnut chart, category comparison bars, daily activity chart, descriptive statistics.
- **Prediction:** Ridge Regression and Random Forest trained with scikit-learn; evaluates them against a previous-month baseline using a chronological holdout with MAE, RMSE, and R²; forecasts next month's expenses.
- **Administrator:** read-only overview of accounts, transactions, imports and ML predictions; CSV exports.
- **Privacy:** passwords are hashed; user records are isolated by account; admin views financial records as part of its project role.

### Dataset
The optional `sample_data/demo_transactions.csv` contains **fictional** example records and is accompanied by `sample_data/DATA_DICTIONARY.md`. The website also has a **Load fictional demo data** button. Monthly budgets are never automatically filled in: each user sets a budget for each month using the **Set my budget** control on Overview. Until they do, the dashboard shows **Not set** and does not invent a remaining balance. The ML algorithm needs **at least 12 consecutive complete months** of expense history to run.

CSV columns: `date`, `description`, `amount`; optional: `category`, `type` (`expense` or `income`).

### ML methodology and limitations
Historical expenses are grouped by *completed calendar month*; features include the previous three months' totals, rolling average, recent trend, and encoded seasonality. The last 25% of usable months are held out in chronological order to evaluate candidate regressors. The algorithm selects the better-performing ML candidate on test MAE and retrains it on available historical months to forecast the next calendar month, potentially recursively forecasting the incomplete current month first.

Forecasts are estimates, not guarantees. Very small or fictional datasets cannot establish real-world predictive accuracy; abrupt spending changes, emergencies, missing expenses and broader economic factors are not represented. The project should not be used for important financial decisions without independent verification. The full academic research report/presentation are **not** included.

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
