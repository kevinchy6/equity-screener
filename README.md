# Equity Screener — SMA Trend Alignment

A free, open-source stock screener for **US and Hong Kong** equities. Automatically scans for stocks with strong SMA trend alignment (10 > 20 > 50 > 100 > 200) and other quality filters, updated 4x daily via GitHub Actions.

**Live demo:** After setup, your screener will be available at `https://<your-username>.github.io/<repo-name>/`

---

## Features

- **US & HK Markets** — Toggle between US and Hong Kong stock screens
- **SMA Trend Alignment** — SMA 10 > 20 > 50 > 100 > 200, Price > SMA 30
- **Volume & Liquidity Filters** — Avg Volume (10/60/90d) > 500K, Avg Trading Value > $50M (US) / HK$50M (HK)
- **Market Cap Filter** — US: > $3B, HK: > HK$1B
- **Daily/5D Change Filters** — Dropdown filters for price change ranges
- **Sector Data** — Sector column for each stock
- **Copy Tickers** — One-click copy all tickers (HK tickers in TradingView format: `HKEX:700`)
- **Dark Mode** — Auto-detects system preference, with manual toggle
- **Auto-refresh** — GitHub Actions runs scanner 4x daily on weekdays
- **Zero cost** — Uses yfinance (free) + GitHub Pages (free) + GitHub Actions (free for public repos)

---

## Filter Criteria

| Filter | US | HK |
|--------|----|----|
| Market Cap | > $3B | > HK$1B |
| Daily Volume | > 500K | > 500K |
| Avg Vol 10d/60d/90d | > 500K each | > 500K each |
| Avg Trading Value | > $50M | > HK$50M |
| Price | > $10 | > $10 |
| SMA Alignment | 10 > 20 > 50 > 100 > 200 | 10 > 20 > 50 > 100 > 200 |
| Price vs SMA 30 | Price > SMA 30 | Price > SMA 30 |

---

## Setup Instructions

### 1. Create a GitHub Repository

1. Go to [github.com/new](https://github.com/new)
2. Name it whatever you like (e.g., `equity-screener`)
3. Set it to **Public** (required for free GitHub Pages)
4. **Do NOT** initialize with README (we'll push our own)
5. Click **Create repository**

### 2. Push the Code

Unzip the downloaded file, then in your terminal:

```bash
cd equity-screener   # or whatever you named the unzipped folder

git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

### 3. Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages**
2. Under **Source**, select **GitHub Actions**
3. Save

### 4. Run the First Scan

1. Go to **Actions** tab in your repo
2. You'll see "Scan & Deploy" workflow
3. Click **Run workflow** → select `both` → **Run workflow**
4. Wait ~5 minutes for the scan to complete
5. Your site will be live at `https://<your-username>.github.io/<repo-name>/`

After the first run, the scanner will automatically run 4x daily on weekdays:

| Time (HKT) | UTC | What it scans |
|-------------|-----|---------------|
| 05:00 | 21:00 (prev day) | US (post US close) |
| 08:00 | 00:00 | Both (pre HK open) |
| 16:30 | 08:30 | HK (post HK close) |
| 22:00 | 14:00 | Both |

---

## Project Structure

```
├── public/                  # Static site (deployed to GitHub Pages)
│   ├── index.html           # Main app (HTML + CSS + JS, no build step)
│   └── data/
│       ├── us.json          # US scan results
│       └── hk.json          # HK scan results
├── scripts/
│   ├── scan_us.py           # US market scanner (yfinance)
│   ├── scan_hk.py           # HK market scanner (yfinance)
│   ├── us_universe.txt      # ~5,780 US tickers
│   └── hk_universe.txt      # ~496 HK tickers
└── .github/
    └── workflows/
        └── scan-and-deploy.yml   # GitHub Actions workflow
```

---

## Manual Scan

You can trigger a scan manually anytime:

1. Go to **Actions** → **Scan & Deploy**
2. Click **Run workflow**
3. Choose market: `both`, `us`, or `hk`

---

## Customization

### Modify Filters

Edit `scripts/scan_us.py` or `scripts/scan_hk.py`. Key variables at the top:

- `MIN_MARKET_CAP` — Minimum market cap
- `MIN_DAILY_VOLUME` — Minimum daily volume
- `MIN_AVG_VOLUME` — Minimum average volume
- `MIN_AVG_VALUE` — Minimum average trading value
- `MIN_PRICE` — Minimum price

### Update Ticker Universe

- US: Replace `scripts/us_universe.txt` with one ticker per line
- HK: Replace `scripts/hk_universe.txt` with one ticker per line (format: `0700.HK`)

### Change Schedule

Edit `.github/workflows/scan-and-deploy.yml` and modify the cron expressions.

---

## Data Source

All data comes from **Yahoo Finance** via the `yfinance` Python library (free, no API key needed).

---

## License

MIT — use it however you like.
