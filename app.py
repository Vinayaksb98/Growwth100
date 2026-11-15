import html
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(
    page_title="Nifty 500 Earnings Scanner",
    page_icon="📈",
    layout="wide",
)

st.title("📈 Nifty 500 Core Earnings & Margin Expansion Scanner")
st.caption(
    "Revenue growth ≥15% YoY | EBITDA growth ≥20% YoY | PAT growth ≥40% YoY "
    "| EBITDA margin must expand"
)

MIN_REVENUE_GROWTH = 15.0
MIN_EBITDA_GROWTH = 20.0
MIN_PAT_GROWTH = 40.0

NSE_CSV_URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]
FALLBACK_CSV_URL = (
    "https://raw.githubusercontent.com/BKKB20/nse-index-history/main/"
    "current_members/NIFTY_500.csv"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}


def find_symbol_column(frame):
    for col in frame.columns:
        if str(col).strip().lower() in {"symbol", "ticker"}:
            return col
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def load_nifty500_universe():
    errors = []
    for url in NSE_CSV_URLS + [FALLBACK_CSV_URL]:
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            frame = pd.read_csv(io.BytesIO(response.content))
            symbol_col = find_symbol_column(frame)
            if symbol_col is None:
                raise ValueError("CSV does not contain a Symbol column")
            symbols = (
                frame[symbol_col]
                .dropna()
                .astype(str)
                .str.strip()
                .str.replace(r"\.NS$", "", regex=True)
            )
            symbols = [s for s in symbols.tolist() if s and s.lower() != "nan"]
            symbols = list(dict.fromkeys(symbols))
            if len(symbols) < 450:
                raise ValueError(f"Only {len(symbols)} symbols received; expected a Nifty 500 list")
            return symbols[:500], url, None
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("Could not load Nifty 500 constituents. " + " | ".join(errors))


def clean_statement_index(statement):
    if statement is None or statement.empty:
        return pd.DataFrame()
    result = statement.copy()
    result.index = [str(x).strip().lower() for x in result.index]
    result.columns = pd.to_datetime(result.columns, errors="coerce")
    result = result.loc[:, ~result.columns.isna()]
    return result


def find_metric(statement, names):
    if statement.empty:
        return None
    for name in names:
        key = name.lower()
        if key in statement.index:
            series = pd.to_numeric(statement.loc[key], errors="coerce").dropna()
            if not series.empty:
                return series
    return None


def choose_yoy_pair(series):
    if series is None:
        return None
    series = series.dropna().sort_index(ascending=False)
    if len(series) < 5:
        return None
    current_date = series.index[0]
    target_date = current_date - pd.DateOffset(years=1)
    prior_candidates = series.iloc[1:].copy()
    gaps = (prior_candidates.index - target_date).days
    valid = prior_candidates[(abs(gaps) <= 45)]
    if valid.empty:
        return None
    prior_date = min(valid.index, key=lambda d: abs((d - target_date).days))
    return current_date, float(series.loc[current_date]), prior_date, float(series.loc[prior_date])


def growth(current, previous):
    if previous is None or previous <= 0 or current is None:
        return None
    return (current - previous) / abs(previous) * 100.0


def scan_one(symbol):
    ticker_symbol = symbol + ".NS"
    try:
        ticker = yf.Ticker(ticker_symbol)
        statement = clean_statement_index(ticker.quarterly_income_stmt)
        if statement.empty:
            return {"symbol": symbol, "status": "Skipped", "reason": "Quarterly income statement unavailable"}

        revenue = find_metric(statement, ["total revenue", "operating revenue"])
        ebitda = find_metric(statement, ["ebitda", "normalized ebitda"])
        pat = find_metric(statement, ["net income", "net income common stockholders"])

        rev_pair = choose_yoy_pair(revenue)
        ebitda_pair = choose_yoy_pair(ebitda)
        pat_pair = choose_yoy_pair(pat)

        if not all([rev_pair, ebitda_pair, pat_pair]):
            missing = []
            if not rev_pair:
                missing.append("Revenue YoY pair")
            if not ebitda_pair:
                missing.append("EBITDA YoY pair")
            if not pat_pair:
                missing.append("PAT YoY pair")
            return {
                "symbol": symbol,
                "status": "Skipped",
                "reason": "Yahoo Finance data missing: " + ", ".join(missing),
            }

        # Use the same current and prior-year quarter for every metric.
        current_date, current_rev, prior_date, prior_rev = rev_pair
        e_current = ebitda.get(current_date) if ebitda is not None else None
        e_prior = ebitda.get(prior_date) if ebitda is not None else None
        p_current = pat.get(current_date) if pat is not None else None
        p_prior = pat.get(prior_date) if pat is not None else None

        if any(pd.isna(v) for v in [e_current, e_prior, p_current, p_prior]):
            return {"symbol": symbol, "status": "Skipped", "reason": "Metrics do not share matching quarter dates"}

        rev_g = growth(current_rev, prior_rev)
        ebitda_g = growth(float(e_current), float(e_prior))
        pat_g = growth(float(p_current), float(p_prior))
        if any(v is None for v in [rev_g, ebitda_g, pat_g]):
            return {"symbol": symbol, "status": "Skipped", "reason": "Prior-year revenue/EBITDA/PAT base is zero or negative"}

        current_margin = float(e_current) / current_rev * 100 if current_rev > 0 else None
        prior_margin = float(e_prior) / prior_rev * 100 if prior_rev > 0 else None
        if current_margin is None or prior_margin is None:
            return {"symbol": symbol, "status": "Skipped", "reason": "Cannot calculate EBITDA margin"}

        passes = (
            rev_g >= MIN_REVENUE_GROWTH
            and ebitda_g >= MIN_EBITDA_GROWTH
            and pat_g >= MIN_PAT_GROWTH
            and current_margin > prior_margin
        )
        return {
            "symbol": symbol,
            "status": "MATCH" if passes else "Does not meet all rules",
            "period": current_date.date().isoformat(),
            "revenue_growth": round(rev_g, 2),
            "ebitda_growth": round(ebitda_g, 2),
            "pat_growth": round(pat_g, 2),
            "current_ebitda_margin": round(current_margin, 2),
            "prior_ebitda_margin": round(prior_margin, 2),
            "margin_expansion_pp": round(current_margin - prior_margin, 2),
            "reason": "",
        }
    except Exception as exc:
        return {"symbol": symbol, "status": "Skipped", "reason": str(exc)[:180]}


def send_telegram(matches, token, chat_id):
    if not token or not chat_id:
        raise ValueError("Enter both Telegram Bot Token and Chat ID.")
    lines = ["🚀 <b>Nifty 500 Earnings Scanner Matches</b>"]
    for _, row in matches.iterrows():
        lines.append(
            f"\n<b>{html.escape(str(row['Symbol']))}</b> | {row['Quarter']}"
            f"\nRevenue: {row['Revenue Growth (%)']:.2f}%"
            f" | EBITDA: {row['EBITDA Growth (%)']:.2f}%"
            f" | PAT: {row['PAT Growth (%)']:.2f}%"
            f"\nEBITDA margin: {row['Current EBITDA Margin (%)']:.2f}% vs "
            f"{row['Prior EBITDA Margin (%)']:.2f}%"
        )
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) > 3500 and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    for chunk in chunks:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram alert failed"))


with st.sidebar:
    st.header("⚙️ Scanner settings")
    st.metric("Revenue growth", "≥15% YoY")
    st.metric("EBITDA growth", "≥20% YoY")
    st.metric("PAT growth", "≥40% YoY")
    st.metric("EBITDA margin", "Must expand")
    st.divider()
    st.subheader("Telegram alerts (optional)")
    telegram_token = st.text_input("Telegram Bot Token", type="password")
    telegram_chat_id = st.text_input("Telegram Chat ID")
    enable_telegram = st.checkbox("Send matching alerts", value=False)
    workers = st.select_slider("Parallel downloads", options=[2, 3, 4, 5, 6], value=4)

st.info(
    "The scanner loads the Nifty 500 constituent list and checks quarterly financial statements "
    "through Yahoo Finance (yfinance). Financial data coverage varies by company. A stock is marked "
    "MATCH only when all four metrics are present for the same quarter and pass every rule. "
    "Unavailable or incomplete data is shown as Skipped—not treated as a failed earnings result."
)

if "scan_rows" not in st.session_state:
    st.session_state.scan_rows = None

try:
    with st.spinner("Loading Nifty 500 constituent list..."):
        universe, source_url, _ = load_nifty500_universe()
    st.success(f"Nifty 500 universe loaded: {len(universe)} symbols")
    st.caption(f"Constituent source: {source_url} | Cached for 24 hours")
except Exception as exc:
    st.error(str(exc))
    st.stop()

col_a, col_b = st.columns([1, 3])
with col_a:
    run_scan = st.button("🔍 Scan all Nifty 500", type="primary", use_container_width=True)
with col_b:
    st.caption("A full scan can take several minutes. Yahoo Finance may rate-limit requests; rerun later if many symbols are skipped.")

if run_scan:
    progress = st.progress(0, text="Preparing scan...")
    status_line = st.empty()
    rows = []
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(scan_one, symbol): symbol for symbol in universe}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:
                rows.append({"symbol": futures[future], "status": "Skipped", "reason": str(exc)[:180]})
            completed += 1
            if completed % 5 == 0 or completed == len(universe):
                progress.progress(completed / len(universe), text=f"Scanned {completed}/{len(universe)}")
                status_line.write(f"Completed {completed} of {len(universe)} symbols")
    st.session_state.scan_rows = pd.DataFrame(rows)
    progress.empty()
    status_line.empty()

if st.session_state.scan_rows is not None:
    scan_df = st.session_state.scan_rows.copy()
    total = len(scan_df)
    matches = scan_df[scan_df["status"] == "MATCH"].copy()
    skipped = scan_df[scan_df["status"] == "Skipped"].copy()
    checked = total - len(skipped)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Universe", f"{len(universe)}")
    c2.metric("Processed", f"{total}")
    c3.metric("Usable comparisons", f"{checked}")
    c4.metric("Qualifying matches", f"{len(matches)}")

    if not matches.empty:
        display = matches.rename(columns={
            "symbol": "Symbol",
            "period": "Quarter",
            "revenue_growth": "Revenue Growth (%)",
            "ebitda_growth": "EBITDA Growth (%)",
            "pat_growth": "PAT Growth (%)",
            "current_ebitda_margin": "Current EBITDA Margin (%)",
            "prior_ebitda_margin": "Prior EBITDA Margin (%)",
            "margin_expansion_pp": "Margin Expansion (pp)",
        })
        display = display[[
            "Symbol", "Quarter", "Revenue Growth (%)", "EBITDA Growth (%)",
            "PAT Growth (%)", "Current EBITDA Margin (%)",
            "Prior EBITDA Margin (%)", "Margin Expansion (pp)"
        ]].sort_values("PAT Growth (%)", ascending=False)
        st.subheader("🚀 Stocks passing all four conditions")
        st.dataframe(display, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇️ Download qualifying Nifty 500 stocks",
            display.to_csv(index=False).encode("utf-8"),
            "nifty500_earnings_matches.csv",
            "text/csv",
        )
        if enable_telegram and st.button("📨 Send Telegram alerts"):
            try:
                send_telegram(display, telegram_token, telegram_chat_id)
                st.success("Telegram alerts sent.")
            except Exception as exc:
                st.error(f"Telegram alert failed: {exc}")
    else:
        st.warning("No stocks passed all four rules in the available data. This is not proof that all other companies failed; some may have incomplete data.")

    with st.expander(f"All processed stocks ({len(scan_df)})"):
        st.dataframe(scan_df, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇️ Download complete scan log",
            scan_df.to_csv(index=False).encode("utf-8"),
            "nifty500_full_scan_log.csv",
            "text/csv",
        )
    if not skipped.empty:
        with st.expander(f"Skipped / unavailable data ({len(skipped)})"):
            st.dataframe(skipped[["symbol", "reason"]], use_container_width=True, hide_index=True)

st.divider()
st.caption(
    "Data caveat: Yahoo Finance statements are third-party, may be delayed/incomplete, and may omit EBITDA "
    "or matching year-ago quarters. Financial companies may not be comparable using EBITDA. "
    "Verify qualifying results against the company's NSE-filed results before trading."
)
