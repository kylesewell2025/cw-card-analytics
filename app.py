from __future__ import annotations

import calendar
import csv
import hashlib
import json
from io import StringIO

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import text

st.set_page_config(
    page_title="CW Card Company Analytics",
    page_icon="📊",
    layout="wide",
)

MONEY_COLUMNS = [
    "Net amount",
    "Item subtotal",
    "Shipping and handling",
    "Seller collected tax",
    "eBay collected tax",
    "Final Value Fee - fixed",
    "Final Value Fee - variable",
    "Regulatory operating fee",
    'Very high "item not as described" fee',
    "Below standard performance fee",
    "International fee",
    "Charity donation",
    "Deposit processing fee",
    "Gross transaction amount",
]

IGNORED_TYPES = {
    "Payout",
    "Transfer",
    "Hold",
    "Reserve",
    "Secondary payout",
}


def decode_csv(file_bytes: bytes) -> str:
    """Decode an eBay CSV, including files exported with a UTF-8 BOM."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("The uploaded file could not be decoded as a CSV.")


def find_header_line(csv_text: str) -> int:
    """Find the actual eBay transaction header after the metadata rows."""
    for line_number, line in enumerate(csv_text.splitlines()):
        first_value = next(csv.reader([line]), [])
        if first_value and first_value[0].strip() == "Transaction creation date":
            return line_number
    raise ValueError(
        "Could not find the eBay Transaction Report header row. "
        "Please upload an eBay Transaction Report CSV."
    )


@st.cache_data(show_spinner=False)
def load_ebay_csv(file_bytes: bytes) -> pd.DataFrame:
    csv_text = decode_csv(file_bytes)
    header_line = find_header_line(csv_text)
    transaction_text = "\n".join(csv_text.splitlines()[header_line:])

    try:
        df = pd.read_csv(
            StringIO(transaction_text),
            engine="python",
            on_bad_lines="warn",
        )
    except Exception as exc:
        raise ValueError(f"Unable to read the transaction table: {exc}") from exc

    df.columns = [str(column).strip() for column in df.columns]
    df = df.dropna(how="all")

    required = {"Transaction creation date", "Type", "Net amount"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(sorted(missing)))

    df["Transaction creation date"] = pd.to_datetime(
        df["Transaction creation date"],
        errors="coerce",
    )
    df["Type"] = df["Type"].astype(str).str.strip()

    for column in MONEY_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(
                df[column]
                .astype(str)
                .str.replace(r"[$,]", "", regex=True)
                .str.strip(),
                errors="coerce",
            ).fillna(0.0)

    if "Quantity" in df.columns:
        df["Quantity"] = pd.to_numeric(
            df["Quantity"],
            errors="coerce",
        ).fillna(0)
    else:
        df["Quantity"] = 0

    df = df[df["Transaction creation date"].notna()].copy()
    if df.empty:
        raise ValueError(
            "The transaction table was found, but it contained no readable dated transactions."
        )

    return df


@st.cache_resource
def get_connection():
    return st.connection("neon", type="sql")


def initialize_database() -> None:
    conn = get_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ebay_transactions (
                    record_key TEXT PRIMARY KEY,
                    transaction_date TIMESTAMPTZ NOT NULL,
                    transaction_type TEXT,
                    transaction_id TEXT,
                    order_number TEXT,
                    item_title TEXT,
                    quantity NUMERIC,
                    net_amount NUMERIC(14, 2),
                    source_file TEXT,
                    payload JSONB NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        session.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_ebay_transactions_date
                ON ebay_transactions (transaction_date)
                """
            )
        )
        session.commit()


def clean_scalar(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def make_record_key(row: pd.Series) -> str:
    preferred_fields = [
        "Transaction ID",
        "Type",
        "Reference ID",
        "Order number",
        "Item ID",
        "Transaction creation date",
        "Net amount",
    ]
    values = {
        field: clean_scalar(row.get(field))
        for field in preferred_fields
        if field in row.index
    }
    raw_key = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def import_transactions(df: pd.DataFrame, source_file: str) -> tuple[int, int]:
    conn = get_connection()
    rows = []

    for _, row in df.iterrows():
        payload = {
            column: clean_scalar(row[column])
            for column in df.columns
        }
        rows.append(
            {
                "record_key": make_record_key(row),
                "transaction_date": row["Transaction creation date"].to_pydatetime(),
                "transaction_type": clean_scalar(row.get("Type")),
                "transaction_id": clean_scalar(row.get("Transaction ID")),
                "order_number": clean_scalar(row.get("Order number")),
                "item_title": clean_scalar(row.get("Item title")),
                "quantity": float(row.get("Quantity", 0) or 0),
                "net_amount": float(row.get("Net amount", 0) or 0),
                "source_file": source_file,
                "payload": json.dumps(payload, default=str),
            }
        )

    if not rows:
        return 0, 0

    insert_sql = text(
        """
        INSERT INTO ebay_transactions (
            record_key,
            transaction_date,
            transaction_type,
            transaction_id,
            order_number,
            item_title,
            quantity,
            net_amount,
            source_file,
            payload
        )
        VALUES (
            :record_key,
            :transaction_date,
            :transaction_type,
            :transaction_id,
            :order_number,
            :item_title,
            :quantity,
            :net_amount,
            :source_file,
            CAST(:payload AS JSONB)
        )
        ON CONFLICT (record_key) DO NOTHING
        """
    )

    with conn.session as session:
        before = session.execute(
            text("SELECT COUNT(*) FROM ebay_transactions")
        ).scalar_one()
        session.execute(insert_sql, rows)
        session.commit()
        after = session.execute(
            text("SELECT COUNT(*) FROM ebay_transactions")
        ).scalar_one()

    inserted = int(after - before)
    skipped = len(rows) - inserted

    load_saved_transactions.clear()
    
    return inserted, skipped


@st.cache_data(ttl=60, show_spinner=False)
def load_saved_transactions() -> pd.DataFrame:
    conn = get_connection()
    stored = conn.query(
        """
        SELECT payload
        FROM ebay_transactions
        ORDER BY transaction_date
        """,
        ttl=0,
    )

    if stored.empty:
        return pd.DataFrame()

    records = []
    for payload in stored["payload"]:
        if isinstance(payload, str):
            records.append(json.loads(payload))
        else:
            records.append(payload)

    df = pd.json_normalize(records)
    if df.empty:
        return df

    df["Transaction creation date"] = pd.to_datetime(
        df["Transaction creation date"],
        errors="coerce",
    )

    for column in MONEY_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)

    if "Quantity" in df.columns:
        df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0)
    else:
        df["Quantity"] = 0

    df["Type"] = df["Type"].astype(str).str.strip()
    df = df[df["Transaction creation date"].notna()].copy()
    df["Date"] = df["Transaction creation date"].dt.date
    df["Month"] = df["Transaction creation date"].dt.to_period("M").astype(str)
    return df


def money(value: float) -> str:
    return f"${value:,.2f}"


initialize_database()

st.title("CW Card Company Analytics")
st.caption("Persistent browser-based eBay sales analytics")

with st.sidebar:
    st.header("Import Data")
    uploaded = st.file_uploader(
        "Upload eBay Transaction Report",
        type=["csv"],
    )

    if uploaded is not None:
        try:
            preview_df = load_ebay_csv(uploaded.getvalue())
            st.success(f"Found {len(preview_df):,} readable transactions.")
            if st.button("Import transactions", type="primary", use_container_width=True):
    with st.spinner("Saving transactions to Neon..."):
        inserted, skipped = import_transactions(preview_df, uploaded.name)

    st.success(
        f"Imported {inserted:,} new transactions. "
        f"Skipped {skipped:,} duplicates."
    )

    st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    st.header("Dashboard Controls")
    monthly_goal = st.number_input(
        "Monthly net sales goal",
        min_value=0.0,
        value=2000.0,
        step=100.0,
    )
    subtract_shipping = st.checkbox(
        "Subtract shipping labels",
        value=True,
    )
    include_other = st.checkbox(
        "Include refunds and other operating fees",
        value=True,
    )


df = load_saved_transactions()


if df.empty:
    st.info(
        "No saved transactions yet. Upload an eBay Transaction Report in the sidebar "
        "and click **Import transactions**."
    )
    st.stop()

months = sorted(df["Month"].dropna().unique(), reverse=True)
selected_month = st.sidebar.selectbox("Reporting month", months)
month_df = df[df["Month"] == selected_month].copy()

orders = month_df[month_df["Type"].eq("Order")].copy()
shipping = month_df[month_df["Type"].eq("Shipping label")].copy()
other = month_df[
    ~month_df["Type"].isin(list(IGNORED_TYPES | {"Order", "Shipping label"}))
].copy()

order_net = float(orders["Net amount"].sum())
shipping_net = float(shipping["Net amount"].sum()) if subtract_shipping else 0.0
other_net = float(other["Net amount"].sum()) if include_other else 0.0
net_sales = order_net + shipping_net + other_net

items_sold = int(orders["Quantity"].sum())
order_count = int(len(orders))
avg_order = order_net / order_count if order_count else 0.0
remaining = max(0.0, monthly_goal - net_sales)
progress = net_sales / monthly_goal if monthly_goal else 0.0

year, month = map(int, selected_month.split("-"))
days_in_month = calendar.monthrange(year, month)[1]
latest_date = month_df["Transaction creation date"].max().date()
remaining_days = max(0, days_in_month - latest_date.day)
needed_per_day = remaining / remaining_days if remaining_days else 0.0

columns = st.columns(4)
columns[0].metric("Net eBay Sales", money(net_sales))
columns[1].metric("Items Sold", f"{items_sold:,}")
columns[2].metric("Average Order Net", money(avg_order))
columns[3].metric("Remaining to Goal", money(remaining))

columns = st.columns(3)
columns[0].metric("Goal Completion", f"{progress:.1%}")
columns[1].metric("Needed per Remaining Day", money(needed_per_day))
columns[2].metric("Orders", f"{order_count:,}")

st.progress(min(max(progress, 0.0), 1.0))

month_df["Operating Net"] = 0.0
order_mask = month_df["Type"].eq("Order")
month_df.loc[order_mask, "Operating Net"] = month_df.loc[order_mask, "Net amount"]

if subtract_shipping:
    shipping_mask = month_df["Type"].eq("Shipping label")
    month_df.loc[shipping_mask, "Operating Net"] = month_df.loc[
        shipping_mask, "Net amount"
    ]

if include_other:
    other_mask = ~month_df["Type"].isin(
        list(IGNORED_TYPES | {"Order", "Shipping label"})
    )
    month_df.loc[other_mask, "Operating Net"] = month_df.loc[
        other_mask, "Net amount"
    ]

daily_net = month_df.groupby("Date", as_index=False)["Operating Net"].sum()
daily_items = (
    orders.groupby("Date", as_index=False)["Quantity"]
    .sum()
    .rename(columns={"Quantity": "Items Sold"})
)
daily = daily_net.merge(daily_items, on="Date", how="left")
daily["Items Sold"] = daily["Items Sold"].fillna(0).astype(int)
daily = daily.sort_values("Date")
daily["Running Total"] = daily["Operating Net"].cumsum()
daily["Goal Pace"] = [
    monthly_goal * (pd.Timestamp(day).day / days_in_month)
    for day in daily["Date"]
]

left, right = st.columns(2)

with left:
    st.subheader("Daily Net Sales")
    figure = px.bar(
        daily,
        x="Date",
        y="Operating Net",
        labels={"Operating Net": "Net Sales", "Date": ""},
    )
    st.plotly_chart(figure, use_container_width=True)

with right:
    st.subheader("Running Progress vs Goal")
    melted = daily.melt(
        id_vars="Date",
        value_vars=["Running Total", "Goal Pace"],
        var_name="Series",
        value_name="Amount",
    )
    figure = px.line(
        melted,
        x="Date",
        y="Amount",
        color="Series",
        markers=True,
    )
    st.plotly_chart(figure, use_container_width=True)

left, right = st.columns(2)

with left:
    st.subheader("Transaction Impact")
    breakdown = pd.DataFrame(
        {
            "Category": [
                "Orders",
                "Shipping Labels",
                "Refunds / Other Fees",
            ],
            "Amount": [
                order_net,
                shipping_net,
                other_net,
            ],
        }
    )
    figure = px.bar(
        breakdown,
        x="Category",
        y="Amount",
    )
    st.plotly_chart(figure, use_container_width=True)

with right:
    st.subheader("Items Sold by Day")
    figure = px.bar(
        daily,
        x="Date",
        y="Items Sold",
    )
    st.plotly_chart(figure, use_container_width=True)

st.subheader("Daily Detail")
st.dataframe(
    daily.rename(columns={"Operating Net": "Net Sales"}),
    use_container_width=True,
    hide_index=True,
)

with st.expander("View cleaned transactions"):
    display_columns = [
        column
        for column in [
            "Transaction creation date",
            "Type",
            "Order number",
            "Item title",
            "Quantity",
            "Net amount",
            "Gross transaction amount",
            "Transaction ID",
        ]
        if column in month_df.columns
    ]

    st.dataframe(
        month_df[display_columns].sort_values(
            "Transaction creation date",
            ascending=False,
        ),
        use_container_width=True,
        hide_index=True,
    )
