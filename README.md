# Nifty 500 Core Earnings & Margin Expansion Scanner

## Files
Keep `app.py` and `requirements.txt` together in the **root** of the GitHub repository.

## Streamlit Community Cloud deployment
1. Extract this ZIP on your device.
2. Upload the files inside the extracted folder (not the enclosing folder) to the root of your GitHub repository.
3. In Streamlit Community Cloud, select that repository and choose `app.py` as the app's main file.
4. Reboot the app after replacing the files. If a previous failed environment is cached, use the app's reboot/clear-cache option.

## Dependencies
The requirements file explicitly lists Streamlit, pandas, NumPy, yfinance and requests with compatible major-version ranges. Standard-library modules such as `html`, `io`, `time`, `datetime` and `concurrent.futures` do not need installation.

## Scanner limitations
Nifty 500 constituents are loaded from the NSE Indices CSV with a fallback source. Quarterly statement coverage from Yahoo Finance is not complete for every symbol. Missing values are reported as unavailable/skipped, not estimated. Verify qualifying results against official company filings before making investment decisions.
