# Dataset dictionary and provenance

**File:** `demo_transactions.csv`

**Provenance:** Synthetic example transactions created by `demo_data.py` using Python's `random.Random(137)` generator and relative calendar dates. These are **NOT actual bank, merchant, household, or student records** and are suitable only for classroom demonstration. They have no personal identifiers and no licensed external dataset. They cannot validate the model's real-world accuracy.

**Number of months:** 24 complete months immediately preceding the generation date, plus fictional income records. The app's **Load fictional demo data** button generates an equivalent fresh 24-month sample anchored to the month before the server's current local date in Asia/Manila.

| CSV column | Data type | Meaning | Example |
|---|---|---|---|
| `date` | ISO date | Transaction date, YYYY-MM-DD | `2026-08-02` |
| `description` | string | Brief label or fictional merchant | `Groceries` |
| `category` | categorical string | Spending group (or `Income`) | `Food & Drinks` |
| `amount` | number, PHP | Transaction amount, positive number | `584.50` |
| `type` | category | `expense` or `income` | `expense` |

**Available spending categories:** Food & Drinks, Transportation, Shopping, Bills & Utilities, Health, Entertainment, Education, Other. Income uses the `Income` category.

**Preprocessing:** validate ISO dates, remove invalid data rows, normalize category strings, skip exact duplicates, flag individual amounts above PHP 100,000, and aggregate expenses to monthly targets. Monthly gaps are not silently filled, and incomplete current months are excluded from training.

**Recommended disclosure for an academic report:** “The example dataset is a deterministic, synthetic classroom demonstration dataset generated within the project. It is not a representative sample of real households. The computed evaluation metrics describe only model behavior on that artificial dataset.”
