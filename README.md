# AgentDesk — multi-agent Indian stock-analysis dashboard

A local, one-click dashboard that runs a panel of named agents which analyze
Indian (NSE) stocks, debate each pick with an LLM (or a deterministic
fallback), and automatically send BUY signals to Telegram. Runs entirely on
your own machine — no cloud backend.

**Analysis only. No orders are ever placed. Not investment advice.**

## How to run

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in the values below
python app.py
```

Open **http://127.0.0.1:5000**, pick **Demo** or **Live** from the dropdown,
and click **Start agents**.

### Viewing it from an iPad or phone — no computer needed 24/7

If you don't have a computer you can leave running, host it on a free cloud
server instead and just open the URL from Safari. **Render** works well for
this and needs no terminal — everything below is done in a browser.

**1. Put the code on GitHub (no `git` command needed)**
- Go to [github.com](https://github.com), sign up if needed, click **New
  repository** (make it Public or Private, doesn't matter).
- On the new repo's page, click **uploading an existing file**, then drag in
  every file/folder from this project (`app.py`, `scoring.py`, `llm.py`,
  `data_sources.py`, `dashboard.html`, `envconfig.py`, `universe.json`,
  `requirements.txt`, `Procfile`, `README.md`, and the `demo_data/` folder).
  Commit the upload.
- Do **not** upload your `.env` file — secrets go into Render's dashboard
  instead (step 3).

**2. Create the web service on Render**
- Go to [render.com](https://render.com), sign up, click **New → Web
  Service**, connect your GitHub account, and pick the repo you just made.
- Runtime: **Python 3**. Render auto-detects the `Procfile` and runs
  `gunicorn app:app` — you don't need to set a start command manually.
- Build command: `pip install -r requirements.txt` (Render usually fills
  this in automatically).
- Choose the **Free** plan.

**3. Set environment variables**
In the service's **Environment** tab, add the same keys from `.env.example`:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and either `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY` if you want the LLM debate (the `claude_code` CLI
subscription mode only works on your own logged-in machine, not a cloud
server — use an API key here instead, or leave both blank to run on the
deterministic engine, which always works).

**4. Deploy**
Click **Create Web Service**. Render builds and starts it, then gives you a
permanent URL like `https://your-app-name.onrender.com`. Open that in Safari
on your iPad — that's the whole dashboard, same as local.

Tip: on iPad, tap the Share icon → **Add to Home Screen** to get an app-like
icon that opens straight to the dashboard.

**Free-tier caveats:**
- Render's free web services spin down after ~15 minutes of no traffic and
  take ~30–60 seconds to wake back up on the next visit — expect a short
  delay the first time you open it after a while.
- The filesystem is not persistent on the free plan, so `audit.db` (the
  SQLite log) resets on redeploys/restarts. The dashboard itself still works
  fine — you'd just lose historical run logs between deploys. If you want
  persistent audit history, Render's paid plans support attached disks.

- **Demo** — loads the bundled evidence files in `demo_data/*.json`. Fully
  offline, no keys or network needed. Good for a first run.
- **Live** — pulls real NSE data via `yfinance` for the tickers in
  `universe.json`. Only useful during NSE market hours (Mon–Fri,
  09:15–15:30 IST) — outside that window quotes are stale/closed.

## LLM debate engine (optional)

The debate panel runs on an LLM if one is available, and **falls back to a
transparent, always-on deterministic rule engine** if not — the app never
crashes or blocks for lack of an LLM.

Provider priority (auto-detected, or force one with `LLM_PROVIDER` in `.env`):

1. **`claude_code`** — install [Claude Code](https://claude.com/product/claude-code),
   run `claude`, and `/login` with your Claude Pro/Max plan. The app shells
   out to the `claude` CLI — no API key, no per-call billing.
2. **`anthropic`** — set `ANTHROPIC_API_KEY` in `.env`.
3. **`openai`** — set `OPENAI_API_KEY` in `.env`.

If none of the above are available, every stock is scored by the
deterministic engine in `scoring.py` — same output shape, same grounding
guarantee (every number cited must come from the evidence bundle; nothing is
invented).

## Telegram signals

1. Create a bot with [@BotFather](https://t.me/BotFather) → copy the bot token.
2. Get your chat id from [@userinfobot](https://t.me/userinfobot).
3. Put both in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   TELEGRAM_CHAT_ID=123456789
   ```

A stock fires a BUY message when its verdict is `BUY` **and** confidence ≥
`CONFIDENCE_THRESHOLD` (default 7). One message per fired stock, plus one
daily summary at the end of the run. The bot token is never printed to logs
or the UI.

## Config reference (`.env`)

| Variable | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram delivery | — |
| `LLM_PROVIDER` | Force `claude_code` \| `anthropic` \| `openai` | auto-detect |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | API-key providers | — |
| `CLAUDE_CODE_MODEL` | Model passed to `claude -p ... --model` | `haiku` |
| `BRAND` | Header brand name | `AgentDesk` |
| `CONFIDENCE_THRESHOLD` | Min confidence (1–10) to fire a BUY | `7` |
| `AGENT_DELAY` | Seconds of pacing between pipeline stages (visual only) | `0.6` |
| `SHORTLIST_PER_BUCKET` | Stocks kept per cap-segment bucket in live mode | `4` |
| `PORT` | Local server port | `5000` |

## File layout

```
app.py             server, background pipeline, SQLite audit, Telegram
scoring.py          deterministic agents + Judge (always-on fallback)
llm.py               LLM debate + provider detection + fallback + verifier
data_sources.py     demo loader + yfinance adapter + evidence-bundle builder
dashboard.html      UI (inline CSS/JS, no build step, no external libs)
universe.json       editable NSE tickers per cap-segment bucket
envconfig.py        tiny dependency-free .env loader
requirements.txt
demo_data/*.json    a few real evidence bundles for the offline demo
audit.db            created on first run — SQLite log of every run/verdict
```

## Swapping the data source

`data_sources.py` uses `yfinance` for portability. To use a broker API, a
paid data feed, or an MCP connector instead, replace the body of
`fetch_live_evidence()` / `screen_universe()` — keep the evidence-bundle
shape identical (see the field list in `_empty_bundle()`) and the scoring
and LLM engines need no changes.

## Notes

- The evidence bundle has **no raw fundamental ratios** (P/E, ROE, etc.) —
  `yfinance`'s free feed doesn't reliably provide them for NSE tickers. This
  is called out in every bundle's `note` field and the Fundamentalist agent
  only ever cites analyst targets/consensus, never invented ratios.
- Every field that couldn't be computed is `null` **and** listed in that
  bundle's `data_gaps[]` — both engines are instructed to say "data
  unavailable" rather than guess.
- `llm.py` includes a lightweight verifier that flags any number an LLM
  agent cites that isn't traceable back to the evidence bundle.
