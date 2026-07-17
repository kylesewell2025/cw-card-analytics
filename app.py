from __future__ import annotations

import calendar
import csv
from io import BytesIO, StringIO, TextIOWrapper

import pandas as pd
import plotly.express as px
import streamlit as st

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
    """
    Locate the real transaction header without asking pandas to parse
    eBay's irregular metadata rows above the table.
    """
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
        raise ValueError(
            "Missing required columns: " + ", ".join(sorted(missing))
        )

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

    dedupe_columns = [
        column
        for column in ["Transaction ID", "Type", "Reference ID", "Net amount"]
        if column in df.columns
    ]
    if dedupe_columns:
        df = df.drop_duplicates(subset=dedupe_columns, keep="last")

    df = df[df["Transaction creation date"].notna()].copy()
    if df.empty:
        raise ValueError(
            "The transaction table was found, but it contained no readable dated transactions."
        )

    df["Date"] = df["Transaction creation date"].dt.date
    df["Month"] = (
        df["Transaction creation date"]
        .dt.to_period("M")
        .astype(str)
    )
    return df


def money(value: float) -> str:
    return f"${value:,.2f}"


st.title("CW Card Company Analytics")
st.caption("Browser-based eBay sales analytics")

with st.sidebar:
    st.header("Controls")
    uploaded = st.file_uploader(
        "Upload eBay Transaction Report",
        type=["csv"],
    )
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

if uploaded is None:
    st.info("Upload your eBay Transaction Report CSV to begin.")
    st.stop()

try:
    df = load_ebay_csv(uploaded.getvalue())
except Exception as exc:
    st.error(str(exc))
    st.stop()

months = sorted(df["Month"].dropna().unique(), reverse=True)
selected_month = st.sidebar.selectbox("Reporting month", months)
month_df = df[df["Month"] == selected_month].copy()

orders = month_df[month_df["Type"].eq("Order")].copy()
shipping = month_df[month_df["Type"].eq("Shipping label")].copy()
other = month_df[
    ~month_df["Type"].isin(
        list(IGNORED_TYPES | {"Order", "Shipping label"})
    )
].copy()

order_net = float(orders["Net amount"].sum())
shipping_net = (
    float(shipping["Net amount"].sum())
    if subtract_shipping
    else 0.0
)
other_net = (
    float(other["Net amount"].sum())
    if include_other
    else 0.0
)
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
columns[1].metric(
    "Needed per Remaining Day",
    money(needed_per_day),
)
columns[2].metric("Orders", f"{order_count:,}")

st.progress(min(max(progress, 0.0), 1.0))

month_df["Operating Net"] = 0.0
order_mask = month_df["Type"].eq("Order")
month_df.loc[order_mask, "Operating Net"] = month_df.loc[
    order_mask,
    "Net amount",
]

if subtract_shipping:
    shipping_mask = month_df["Type"].eq("Shipping label")
    month_df.loc[shipping_mask, "Operating Net"] = month_df.loc[
        shipping_mask,
        "Net amount",
    ]

if include_other:
    other_mask = ~month_df["Type"].isin(
        list(IGNORED_TYPES | {"Order", "Shipping label"})
    )
    month_df.loc[other_mask, "Operating Net"] = month_df.loc[
        other_mask,
        "Net amount",
    ]

daily_net = (
    month_df.groupby("Date", as_index=False)["Operating Net"]
    .sum()
)
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
