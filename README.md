# CW Card Company Analytics

A browser-based Streamlit dashboard for eBay Transaction Report CSV files.

## Run locally on Windows

1. Install Python 3.11 or newer.
2. Extract this folder.
3. Open PowerShell inside the folder.
4. Run:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

The dashboard should open at `http://localhost:8501`.

## Current features

- Finds and removes the introductory rows in eBay exports
- Cleans dates and currency fields
- Deduplicates overlapping transactions
- Tracks monthly net sales, items sold, order count, goal completion, and required daily pace
- Shows daily sales, running goal progress, transaction impact, and transaction detail

## Next phase

- Store transactions in SQLite or PostgreSQL
- Add drag-and-drop import history
- Connect directly to the eBay Finances API
- Deploy the app online
- Add inventory costs and profit analytics
