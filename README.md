# Daybreak (Streamlit, Python): US stocks edition

US large-cap news signals plus a paper-trading portfolio (starts at $1,000, fractional shares allowed; stop/target/15:45 ET square-off; P/L compounds). Market hours are 09:30–16:00 ET, which is 15:30–22:00 CET (14:30–21:00 CET for the ~1 week in Mar and ~3 weeks in Oct/Nov when US and EU clocks change on different dates). Rules run on New York time; times are *shown* in `DISPLAY_TZ` (default `Europe/Berlin`). Weekends and NYSE holidays (list in `market.py`, verify yearly) are non-trading days; the overnight news window reaches back to the last completed session's close, so Monday covers the whole weekend. Simulated only, not advice.

## Run
```
pip install -r requirements.txt
streamlit run app.py
python -m pytest
```
`ALLOW_OFF_HOURS=1` lets you paper-trade outside market hours (demo; default is off).

## Sign-in
- Username/password: built in (scrypt, lockout). Enable sign-up with `allow_registration = true` in secrets.
- Google: add an `[auth]` block (redirect_uri, cookie_secret, client_id, client_secret, server_metadata_url) to `.streamlit/secrets.toml`; optional `allowed_emails`.

## Hosting (Streamlit Community Cloud)
The app sleeps when idle and its disk is ephemeral, so SQLite data (`data/daybreak.db`, override with `DAYBREAK_DB`) can be lost. Stops/targets are replayed from 5-minute candles on each page load instead of a background job. For durable data use a hosted database. Password sessions are lost on browser refresh.
