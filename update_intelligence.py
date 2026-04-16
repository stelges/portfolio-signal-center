#!/usr/bin/env python3
"""
Portfolio Signal Center – KI Intelligence Updater v2
Läuft jeden Freitag um 18:00 UTC via GitHub Actions.

ARCHITEKTUR:
  Rohdaten (APIs) → Claude bewertet alles im Kontext → Signal + Analyse → data/*.json

  BTC/Makro: Claude entscheidet das Signal – keine hardcoded Schwellenwerte.
  MTPLF:     mNAV bleibt Hauptindikator, Claude bewertet den Kontext.
  NVDA:      Claude bewertet Earnings + KI-Capex-Trends.
  SOL/TAO:   Kombiniert Preisstruktur + Netzwerkdaten + Claude-Bewertung.
"""

import os
import json
import time
import logging
import datetime
import requests
import anthropic

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

STRATEGY_LOG = "strategy.log"
DATA_DIR      = "data"
TODAY         = datetime.date.today().isoformat()
NOW_UTC       = datetime.datetime.utcnow().isoformat() + "Z"

# ── Portfolio-Konstanten ──────────────────────────────────────────────────────
BTC_ATH           = 109_000
SOL_ATH           = 294
TAO_CIRC          = 10_750_000
MTPLF_HOLD        = 40_177
MTPLF_DEBT_USD_M  = 280
MTPLF_SHARES_M    = 1_459
MTPLF_PRICE_USD   = 1.90  # OTC-Kurs, manuell aktuell halten


# ─────────────────────────────────────────────────────────────────────────────
# HILFSFUNKTIONEN
# ─────────────────────────────────────────────────────────────────────────────

def safe_get(url: str, params: dict = None, headers: dict = None, timeout: int = 15) -> dict | list | None:
    """HTTP GET mit Fehlerbehandlung."""
    try:
        r = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {url} → Fehler: {e}")
        return None


def log_strategy(message: str, **kwargs):
    """Schreibt Strategie-Anpassungen in strategy.log."""
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    entry = f"[{ts}] {message}"
    if kwargs:
        entry += " | " + " | ".join(f"{k}={v}" for k, v in kwargs.items())
    log.info(f"STRATEGY: {entry}")
    with open(STRATEGY_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def load_json(filename: str) -> dict:
    path = os.path.join(DATA_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(filename: str, data: dict):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"Gespeichert: {path}")


def pct_change(old: float, new: float) -> float:
    if old and old != 0:
        return round(((new - old) / old) * 100, 2)
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 1. KRYPTO-MARKTDATEN (CoinGecko)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_crypto_prices() -> dict:
    """BTC, SOL, TAO – aktueller Preis."""
    data = safe_get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin,solana,bittensor", "vs_currencies": "usd"}
    )
    if data:
        log.info(f"Preise: BTC=${data.get('bitcoin',{}).get('usd')} "
                 f"SOL=${data.get('solana',{}).get('usd')} "
                 f"TAO=${data.get('bittensor',{}).get('usd')}")
    return data or {}


def fetch_global_market() -> dict:
    """BTC-Dominanz + Gesamt-Market-Cap."""
    data = safe_get("https://api.coingecko.com/api/v3/global")
    if data:
        d = data.get("data", {})
        result = {
            "btc_dominance": round(d.get("market_cap_percentage", {}).get("btc", 0), 1),
            "total_market_cap_usd_trillions": round(
                d.get("total_market_cap", {}).get("usd", 0) / 1e12, 2
            )
        }
        log.info(f"Global: BTC-Dominanz={result['btc_dominance']}% "
                 f"Total={result['total_market_cap_usd_trillions']}T$")
        return result
    return {}


def fetch_btc_history(days: int = 200) -> list[float]:
    """BTC Preishistorie für MA-Berechnung."""
    data = safe_get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"}
    )
    if data and "prices" in data:
        return [p[1] for p in data["prices"]]
    return []


def calc_moving_average(prices: list[float], period: int) -> float | None:
    if len(prices) >= period:
        return round(sum(prices[-period:]) / period, 0)
    return None


def fetch_btc_30d_change() -> float | None:
    prices = fetch_btc_history(days=32)
    if len(prices) > 1:
        return pct_change(prices[0], prices[-1])
    return None


def fetch_fear_greed() -> int | None:
    data = safe_get("https://api.alternative.me/fng/?limit=1")
    if data:
        value = int(data["data"][0]["value"])
        log.info(f"Fear & Greed: {value}/100")
        return value
    return None


def fetch_halving_data() -> dict:
    """
    Berechnet das nächste Halving-Datum blockchain-basiert.
    Blockhöhe via mempool.space (Fallback: blockchain.info).
    Nächstes Halving: Block 1.050.000 (nach dem 4. Halving bei Block 840.000, April 2024)
    """
    NEXT_HALVING_BLOCK = 1_050_000
    AVG_BLOCK_SECONDS  = 600  # 10 Minuten

    # Aktuelle Blockhöhe abrufen
    height = None
    try:
        r = requests.get("https://mempool.space/api/blocks/tip/height", timeout=10)
        r.raise_for_status()
        height = int(r.text.strip())
        log.info(f"Aktuelle Blockhöhe (mempool.space): {height:,}")
    except Exception:
        # Fallback
        try:
            r = requests.get("https://blockchain.info/q/getblockcount", timeout=10)
            r.raise_for_status()
            height = int(r.text.strip())
            log.info(f"Aktuelle Blockhöhe (blockchain.info): {height:,}")
        except Exception as e:
            log.warning(f"Blockhöhe nicht abrufbar: {e}")

    if height is None:
        return {"next_halving_estimated_utc": "2028-04-01T00:00:00Z",
                "next_halving_block": NEXT_HALVING_BLOCK,
                "blocks_remaining": None,
                "halving_source": "fallback_fixed"}

    blocks_remaining = NEXT_HALVING_BLOCK - height
    seconds_remaining = blocks_remaining * AVG_BLOCK_SECONDS
    estimated_dt = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds_remaining)
    estimated_iso = estimated_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info(f"Halving: {blocks_remaining:,} Blöcke verbleibend → ~{estimated_dt.strftime('%Y-%m-%d')}")

    return {
        "next_halving_estimated_utc": estimated_iso,
        "next_halving_block": NEXT_HALVING_BLOCK,
        "blocks_remaining": blocks_remaining,
        "current_block_height": height,
        "halving_source": "blockchain_calculated"
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. MAKRO-DATEN (Yahoo Finance)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_yahoo(ticker: str) -> dict:
    """Holt aktuellen Kurs + 52W-High/Low von Yahoo Finance."""
    headers = {"User-Agent": "Mozilla/5.0"}
    data = safe_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        headers=headers
    )
    if data:
        try:
            meta = data["chart"]["result"][0]["meta"]
            return {
                "price":    round(meta.get("regularMarketPrice", 0), 2),
                "prev":     round(meta.get("chartPreviousClose", 0), 2),
                "high_52w": round(meta.get("fiftyTwoWeekHigh", 0), 2),
                "low_52w":  round(meta.get("fiftyTwoWeekLow", 0), 2),
            }
        except (KeyError, IndexError, TypeError) as e:
            log.warning(f"Yahoo {ticker} Parse-Fehler: {e}")
    return {}


def fetch_macro_data() -> dict:
    """S&P 500, DXY, Gold, 10Y-Rendite, NVDA."""
    tickers = {
        "sp500": "^GSPC",
        "dxy":   "DX-Y.NYB",
        "gold":  "GC=F",
        "us10y": "^TNX",
        "nvda":  "NVDA",
    }
    result = {}
    for key, ticker in tickers.items():
        d = fetch_yahoo(ticker)
        if d:
            result[key] = d
            log.info(f"Yahoo {ticker}: ${d.get('price')}")
        time.sleep(1)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 3. BINANCE DERIVATIVES (Funding Rate + Long/Short)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_funding_rate() -> float | None:
    """
    BTC Funding Rate via Bybit (funktioniert auf GitHub Actions).
    Positiv = Markt Long-lastig (überhitzt), Negativ = Short-Druck.
    """
    data = safe_get(
        "https://api.bybit.com/v5/market/funding/history",
        params={"category": "linear", "symbol": "BTCUSDT", "limit": 1}
    )
    try:
        rate = float(data["result"]["list"][0]["fundingRate"]) * 100
        log.info(f"Funding Rate (Bybit): {rate:.4f}%")
        return round(rate, 4)
    except Exception:
        log.warning("Funding Rate nicht verfügbar")
        return None


def fetch_long_short_ratio() -> float | None:
    """
    BTC Long/Short Ratio via Bybit.
    Über 1.0 = mehr Longs im Markt.
    """
    data = safe_get(
        "https://api.bybit.com/v5/market/account-ratio",
        params={"category": "linear", "symbol": "BTCUSDT", "period": "1d", "limit": 1}
    )
    try:
        ratio = float(data["result"]["list"][0]["buyRatio"]) / float(data["result"]["list"][0]["sellRatio"])
        log.info(f"Long/Short Ratio (Bybit): {ratio:.2f}")
        return round(ratio, 2)
    except Exception:
        log.warning("Long/Short Ratio nicht verfügbar")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. NEWS (Google News RSS)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_news(query: str, max_items: int = 5) -> list[str]:
    import xml.etree.ElementTree as ET
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    data = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    try:
        root = ET.fromstring(data.content)
        items = root.findall(".//item")[:max_items]
        headlines = [item.find("title").text for item in items if item.find("title") is not None]
        log.info(f"News '{query}': {len(headlines)} Headlines")
        return headlines
    except Exception as e:
        log.warning(f"News RSS Fehler: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 5. mNAV BERECHNUNG
# ─────────────────────────────────────────────────────────────────────────────

def calc_mnav(btc_price: float) -> float:
    ev  = MTPLF_SHARES_M * 1_000_000 * MTPLF_PRICE_USD + MTPLF_DEBT_USD_M * 1_000_000
    nav = MTPLF_HOLD * btc_price
    return round(ev / nav, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 6. CLAUDE KI-ANALYSE – NEUE ARCHITEKTUR
# ─────────────────────────────────────────────────────────────────────────────

def analyze_btc(client: anthropic.Anthropic, market: dict) -> dict:
    """
    Claude bewertet das vollständige Marktbild und entscheidet das Signal.
    Keine hardcoded Schwellenwerte – die KI urteilt im Kontext.
    """
    btc     = market.get("btc")
    fg      = market.get("fg")
    p30     = market.get("p30")
    ma200   = market.get("ma200")
    ma50    = market.get("ma50")
    dom     = market.get("btc_dominance")
    total_mc = market.get("total_market_cap_usd_trillions")
    funding = market.get("funding_rate")
    ls      = market.get("long_short_ratio")
    sp500   = market.get("sp500", {})
    dxy     = market.get("dxy", {})
    gold    = market.get("gold", {})
    us10y   = market.get("us10y", {})
    news    = market.get("btc_news", [])

    ath_dist = round(((btc - BTC_ATH) / BTC_ATH) * 100, 1) if btc else None
    above_200ma = (btc > ma200) if (btc and ma200) else None
    sp500_30d = pct_change(sp500.get("prev", 0), sp500.get("price", 0)) if sp500 else None

    context = f"""
Du bist ein erfahrener Makro- und Krypto-Analyst. Heute ist {TODAY}.

Analysiere folgende Daten und entscheide das Handelssignal für Bitcoin im Kontext eines
7-Jahres-Investitionsplans mit monatlichem DCA. Kurzfristige Schwankungen sind irrelevant –
du bewertest die mittelfristige Phase (3–6 Monate).

=== BITCOIN PREISSTRUKTUR ===
Preis:              ${btc:,.0f}
ATH ($109k):        {ath_dist:+.1f}%
30d-Performance:    {p30:+.1f}% {"(WARNUNG: starker Rückgang)" if p30 and p30 < -20 else ""}
200-Tage MA:        ${ma200:,.0f} → BTC {"DARÜBER ✓" if above_200ma else "DARUNTER ✗ (bearish)"}
50-Tage MA:         ${ma50:,.0f}

=== MARKTSTRUKTUR ===
Fear & Greed:       {fg}/100 {"(Extremangst – historisch Kaufzone)" if fg and fg < 25 else "(Extreme Gier – Vorsicht)" if fg and fg > 75 else "(neutral)"}
BTC-Dominanz:       {dom}% {"(steigend – Risk-off, Altcoins schwach)" if dom and dom > 55 else "(fallend – Altcoin-Season möglich)"}
Total Market Cap:   ${total_mc}T
Funding Rate:       {funding}% {"(NEGATIV – Short-Druck, mögliche Erholung)" if funding and funding < 0 else "(hoch positiv – überhitzt)" if funding and funding > 0.05 else "(neutral)"}
Long/Short Ratio:   {ls} {"(mehr Longs – Kontraindikator bei >1.2)" if ls and ls > 1.2 else "(ausgeglichen)" if ls else ""}

=== MAKRO-UMFELD ===
S&P 500:            ${sp500.get('price', '—')} ({sp500_30d:+.1f}% 30d) {"(Risk-off Markt)" if sp500_30d and sp500_30d < -5 else ""}
DXY (Dollar):       {dxy.get('price', '—')} {"(starker Dollar – negativ für BTC)" if dxy.get('price', 0) > 104 else "(schwacher Dollar – positiv für BTC)"}
Gold:               ${gold.get('price', '—')} {"(Gold steigt – Flucht in Sicherheit)" if gold.get('price', 0) > gold.get('prev', 0) else ""}
10Y US-Rendite:     {us10y.get('price', '—')}% {"(hoch – teures Kapital, belastet Risk-Assets)" if us10y.get('price', 0) > 4.5 else ""}

=== AKTUELLE HEADLINES ===
{chr(10).join(f'- {h}' for h in news[:5])}

=== PORTFOLIO-KONTEXT ===
Strategie:    7 Jahre DCA, nächstes BTC-Halving April 2028
Ziel:         Maximale Akkumulation in Schwächephasen
Kaufzonen:    Historisch: BTC -40% bis -60% vom ATH, F&G unter 30
Sparrate:     1.000–4.000€/Monat (kein aktives Trading)

=== DEINE AUFGABE ===
Bewerte das GESAMTBILD und antworte NUR mit diesem JSON (kein Markdown, keine Erklärungen):

{{
  "signal": "BUY|HOLD|CASH|SELL",
  "confidence": "hoch|mittel|niedrig",
  "summary": "2-3 Sätze zur aktuellen Phase. Was dominiert das Bild gerade?",
  "bull_case": "Was spricht für Kursanstieg? Max 2 Sätze.",
  "bear_case": "Was sind die größten Risiken? Max 2 Sätze.",
  "key_drivers": ["Wichtigster Faktor 1", "Faktor 2", "Faktor 3"],
  "signal_changer": "Was müsste sich ändern damit das Signal kippt?",
  "buy_zone_note": "Empfehlung zur Kaufzone basierend auf aktuellem Kontext"
}}

Signal-Definitionen für diesen Plan:
- BUY:  Sparrate 1.5–2× erhöhen, Cash-Reserve einsetzen
- HOLD: Normales monatliches DCA fortführen
- CASH: Sparrate halbieren, keine Neukäufe
- SELL: Teilgewinne sichern (nur bei extremen Überbewertungen)
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",  # Sonnet für BTC – komplexere Analyse
            max_tokens=800,
            messages=[{"role": "user", "content": context}]
        )
        raw = response.content[0].text.strip()
        result = json.loads(raw)
        log.info(f"BTC KI-Signal: {result.get('signal')} (Konfidenz: {result.get('confidence')})")

        # Strategy Log bei Signal-Änderung
        old_signal = load_json("btc.json").get("signal", "HOLD")
        if result.get("signal") != old_signal:
            log_strategy(
                f"BTC SIGNAL GEÄNDERT: {old_signal} → {result['signal']}",
                konfidenz=result.get("confidence"),
                grund=result.get("signal_changer", "—")
            )
        return result
    except json.JSONDecodeError:
        log.error(f"BTC Claude JSON-Fehler. Raw: {response.content[0].text[:300]}")
        return _fallback_analysis()
    except Exception as e:
        log.error(f"BTC Claude Fehler: {e}")
        return _fallback_analysis()


def analyze_asset(client: anthropic.Anthropic, asset_name: str, context_str: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    """Generische Analyse für MTPLF, NVDA, SOL, TAO."""
    prompt = f"""Du bist Finanzanalyst für einen 7-Jahres-Investitionsplan (DCA, kein Trading).
Heute ist {TODAY}. Antworte auf Deutsch. NUR JSON, kein Markdown.

{context_str}

Antworte mit:
{{
  "signal": "BUY|HOLD|CASH|SELL",
  "confidence": "hoch|mittel|niedrig",
  "summary": "2-3 Sätze zur aktuellen Lage.",
  "bull_case": "Max 2 Sätze.",
  "bear_case": "Max 2 Sätze.",
  "key_drivers": ["Faktor 1", "Faktor 2", "Faktor 3"],
  "signal_changer": "Was würde das Signal kippen?"
}}"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        result = json.loads(raw)
        log.info(f"{asset_name} Signal: {result.get('signal')} ({result.get('confidence')})")
        return result
    except Exception as e:
        log.error(f"{asset_name} Claude Fehler: {e}")
        return _fallback_analysis()


def _fallback_analysis() -> dict:
    return {
        "signal": "HOLD",
        "confidence": "niedrig",
        "summary": "Analyse temporär nicht verfügbar.",
        "bull_case": "—",
        "bear_case": "—",
        "key_drivers": [],
        "signal_changer": "—"
    }


def create_weekly_briefing(client: anthropic.Anthropic, signals: dict, market: dict) -> str:
    """Kurzes Wochenbriefing für das Portfolio-Banner."""
    prompt = f"""Heute ist {TODAY}. Du schreibst das Wochenbriefing für ein Portfolio-Dashboard.

Portfolio: 38% BTC, 20% Metaplanet, 15% NVDA, 14% SOL, 8% TAO.
7-Jahres-Plan. Monatliches DCA. Kein Trading.

Aktuelle Signale:
- BTC: {signals.get('btc', 'HOLD')} | Preis: ${market.get('btc', '—'):,.0f}
- Metaplanet: {signals.get('mtplf', 'HOLD')} | mNAV: {market.get('mnav', '—')}x
- NVDA: {signals.get('nvda', 'HOLD')}
- SOL: {signals.get('sol', 'HOLD')} | Preis: ${market.get('sol', '—')}
- TAO: {signals.get('tao', 'HOLD')} | MCap: ${market.get('tao_mcap_b', '—')}B

Makro: DXY={market.get('dxy_price', '—')} | S&P={market.get('sp500_price', '—')} | F&G={market.get('fg', '—')}/100

Schreibe 2-3 Sätze auf Deutsch. Klar, direkt, keine Floskeln.
Was ist diese Woche die wichtigste Erkenntnis für diesen Plan?"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.error(f"Briefing Fehler: {e}")
        return "Wochenbriefing temporär nicht verfügbar."


# ─────────────────────────────────────────────────────────────────────────────
# 7. MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"Portfolio Signal Center · KI-Update v2 · {TODAY}")
    log.info("=" * 60)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY fehlt!")
    client = anthropic.Anthropic(api_key=api_key)

    # ── SCHRITT 1: Alle Marktdaten sammeln ───────────────────────────────────
    log.info("── Schritt 1: Marktdaten ──")

    prices = fetch_crypto_prices();           time.sleep(2)
    global_market = fetch_global_market();    time.sleep(2)
    fg = fetch_fear_greed();                  time.sleep(2)
    halving = fetch_halving_data();           time.sleep(2)
    p30 = fetch_btc_30d_change();             time.sleep(3)

    # 200d + 50d MA aus Preishistorie berechnen
    btc_history = fetch_btc_history(days=210); time.sleep(3)
    ma200 = calc_moving_average(btc_history, 200)
    ma50  = calc_moving_average(btc_history, 50)
    log.info(f"MA200=${ma200} MA50=${ma50}")

    macro = fetch_macro_data();               time.sleep(2)
    funding = fetch_funding_rate();           time.sleep(2)
    ls_ratio = fetch_long_short_ratio();      time.sleep(2)

    btc  = prices.get("bitcoin",  {}).get("usd")
    sol  = prices.get("solana",   {}).get("usd")
    tao  = prices.get("bittensor",{}).get("usd")
    mnav = calc_mnav(btc) if btc else None
    nvda_price = macro.get("nvda", {}).get("price")

    # ── SCHRITT 2: News ───────────────────────────────────────────────────────
    log.info("── Schritt 2: News ──")
    btc_news   = fetch_news("Bitcoin BTC price market macro 2026",         4); time.sleep(2)
    mtplf_news = fetch_news("Metaplanet Bitcoin Japan stock 3350",          4); time.sleep(2)
    nvda_news  = fetch_news("Nvidia NVDA earnings AI datacenter 2026",      5); time.sleep(2)
    sol_news   = fetch_news("Solana SOL Firedancer ETF network 2026",       4); time.sleep(2)
    tao_news   = fetch_news("Bittensor TAO AI blockchain Grayscale",        4); time.sleep(2)

    # ── SCHRITT 3: KI-Analysen ────────────────────────────────────────────────
    log.info("── Schritt 3: KI-Analysen (Claude) ──")

    # Komplettes Marktbild für BTC-Analyse
    btc_market = {
        "btc": btc, "fg": fg, "p30": p30,
        "ma200": ma200, "ma50": ma50,
        "btc_dominance": global_market.get("btc_dominance"),
        "total_market_cap_usd_trillions": global_market.get("total_market_cap_usd_trillions"),
        "funding_rate": funding,
        "long_short_ratio": ls_ratio,
        "sp500": macro.get("sp500", {}),
        "dxy": macro.get("dxy", {}),
        "gold": macro.get("gold", {}),
        "us10y": macro.get("us10y", {}),
        "btc_news": btc_news
    }

    btc_ai = analyze_btc(client, btc_market)
    time.sleep(4)

    # Metaplanet
    ath_dist_btc = round(((btc - BTC_ATH) / BTC_ATH) * 100, 1) if btc else None
    mtp_context = f"""
Asset: Metaplanet (MTPLF) – Japanische BTC-Treasury-Company (Tokyo Stock Exchange)
Kernindikator: mNAV (Market-NAV-Ratio) = {mnav:.3f}x
  < 1.0x = Bitcoin mit Rabatt kaufen | 1.0-1.8x = fair | > 2.5x = Aufpreis
BTC Holdings: {MTPLF_HOLD:,} BTC | BTC-NAV: ${(MTPLF_HOLD*btc/1e9):.2f}B
Schulden: ${MTPLF_DEBT_USD_M}M | Aktien: {MTPLF_SHARES_M}M
BTC-Preis: ${btc:,.0f} ({ath_dist_btc:+.1f}% vom ATH) – beeinflusst mNAV direkt
BTC-Signal (KI): {btc_ai.get('signal', '—')}
Makro: DXY={macro.get('dxy',{}).get('price','—')} | S&P={macro.get('sp500',{}).get('price','—')}
Aktuelle Headlines: {chr(10).join(f'- {h}' for h in mtplf_news[:4])}

Portfolio-Regel: Nur kaufen wenn mNAV < 1.2x. Über 2.5x kein Neukauf.
Verkauf: mNAV > 2.5x → 20-30%, mNAV > 4.0x → 50%."""
    mtp_ai = analyze_asset(client, "Metaplanet", mtp_context)
    time.sleep(4)

    # NVDA
    nvda_52h = macro.get("nvda", {}).get("high_52w")
    nvda_dist = round(((nvda_price - nvda_52h) / nvda_52h) * 100, 1) if nvda_price and nvda_52h else None
    nvda_context = f"""
Asset: Nvidia (NVDA) – KI-Chip-Monopolist, NASDAQ
Preis: ${nvda_price} | 52W-High: ${nvda_52h} | Abstand vom High: {nvda_dist}%
52W-Low: ${macro.get('nvda',{}).get('low_52w','—')}
Makro: S&P 500={macro.get('sp500',{}).get('price','—')} | DXY={macro.get('dxy',{}).get('price','—')}
KI-Capex-Trend: Alle großen Hyperscaler (Microsoft, Google, Amazon, Meta) erhöhen Data-Center-Ausgaben
Nächste Earnings: Mai 2026
Aktuelle Headlines: {chr(10).join(f'- {h}' for h in nvda_news[:5])}

Portfolio-Regel: Kaufen bei -25% vom 52W-High. HOLD bei KGV 30-60x. Prüfen bei KGV > 70x."""
    nvda_ai = analyze_asset(client, "Nvidia", nvda_context)
    time.sleep(4)

    # Solana
    sol_ath_dist = round(((sol - SOL_ATH) / SOL_ATH) * 100, 1) if sol else None
    sol_context = f"""
Asset: Solana (SOL) – Layer-1 Blockchain
Preis: ${sol:.1f} | ATH: ${SOL_ATH} | ATH-Distanz: {sol_ath_dist}%
Network Revenue: $2.85B/Jahr (echtes Revenue, kein reines Narrativ)
ETF Status: In Prüfung bei SEC | Firedancer Upgrade: 2026
BTC-Kontext: BTC {btc_ai.get('signal','—')} – Altcoins folgen BTC-Trend meist
BTC-Dominanz: {global_market.get('btc_dominance')}% ({"hoch – ungünstig für Altcoins" if global_market.get('btc_dominance', 0) > 55 else "normal"})
Aktuelle Headlines: {chr(10).join(f'- {h}' for h in sol_news[:4])}

Portfolio-Regel: BUY unter -50% ATH (~$147). Verkauf ab $400 (25%), $700 (weitere 25%)."""
    sol_ai = analyze_asset(client, "Solana", sol_context)
    time.sleep(4)

    # Bittensor
    tao_mcap_b = round(tao * TAO_CIRC / 1e9, 2) if tao else None
    tao_context = f"""
Asset: Bittensor (TAO) – KI-Infrastruktur auf Blockchain
Preis: ${tao:.0f} | Market Cap: ${tao_mcap_b}B
Max Supply: 21M TAO (Bitcoin-Mechanik) | Halving: Dez 2025 (abgeschlossen)
Grayscale ETF: Beantragt | KI-Narrative: Dezentralisierte KI-Infrastruktur
BTC-Kontext: BTC {btc_ai.get('signal','—')} | Dominanz: {global_market.get('btc_dominance')}%
Makro KI-Trend: Nvidia-Capex wächst → positiv für KI-Narrative
Aktuelle Headlines: {chr(10).join(f'- {h}' for h in tao_news[:4])}

Portfolio-Regel: BUY bei MCap < $3B. HOLD $3-8B. Verkauf ab MCap > $20B (30% sichern)."""
    tao_ai = analyze_asset(client, "Bittensor", tao_context)
    time.sleep(4)

    # Wochenbriefing
    signals = {
        "btc": btc_ai.get("signal", "HOLD"),
        "mtplf": mtp_ai.get("signal", "HOLD"),
        "nvda": nvda_ai.get("signal", "HOLD"),
        "sol": sol_ai.get("signal", "HOLD"),
        "tao": tao_ai.get("signal", "HOLD"),
    }
    briefing_market = {
        "btc": btc, "mnav": mnav, "sol": sol,
        "tao_mcap_b": tao_mcap_b, "fg": fg,
        "dxy_price": macro.get("dxy", {}).get("price"),
        "sp500_price": macro.get("sp500", {}).get("price"),
    }
    briefing = create_weekly_briefing(client, signals, briefing_market)

    # Globales Signal = BTC-Signal (BTC dominiert das Portfolio)
    global_signal = btc_ai.get("signal", "HOLD")
    global_summary = {
        "BUY":  "Akkumulieren – günstige Phase aktiv",
        "HOLD": "Normales DCA fortführen",
        "CASH": "Vorsicht – Cash aufbauen",
        "SELL": "Teilgewinne sichern"
    }.get(global_signal, "Normales DCA fortführen")

    # ── SCHRITT 4: JSON Dateien schreiben ─────────────────────────────────────
    log.info("── Schritt 4: JSON speichern ──")

    # portfolio.json
    portfolio = load_json("portfolio.json")
    portfolio.update({
        "last_updated": NOW_UTC,
        "updated_by": "update_intelligence.py v2 (GitHub Actions)",
        "global_signal": global_signal,
        "global_summary": global_summary,
        "fear_greed": fg,
        "btc_price_usd": btc,
        "btc_ath_dist_pct": ath_dist_btc,
        "btc_30d_change_pct": p30,
        "btc_dominance_pct": global_market.get("btc_dominance"),
        "macro": {
            "sp500": macro.get("sp500", {}).get("price"),
            "dxy": macro.get("dxy", {}).get("price"),
            "gold": macro.get("gold", {}).get("price"),
            "us10y": macro.get("us10y", {}).get("price"),
        },
        "next_halving": halving,
        "next_update": (datetime.date.today() + datetime.timedelta(days=7)).isoformat() + "T18:00:00Z",
        "ai_weekly_briefing": briefing,
        "market_regime": (
            "bullish" if fg and fg > 65 else
            "fearful" if fg and fg < 30 else
            "neutral"
        )
    })
    save_json("portfolio.json", portfolio)

    # btc.json
    btc_data = load_json("btc.json")
    btc_data.update({
        "signal": btc_ai.get("signal", "HOLD"),
        "price_usd": btc,
        "ath_dist_pct": ath_dist_btc,
        "change_30d_pct": p30,
        "ma200": ma200,
        "ma50": ma50,
        "above_200ma": (btc > ma200) if (btc and ma200) else None,
        "funding_rate": funding,
        "long_short_ratio": ls_ratio,
        "ai_analysis": {
            **btc_ai,
            "news_headlines": btc_news,
            "updated": TODAY
        }
    })
    save_json("btc.json", btc_data)

    # mtplf.json
    mtp_data = load_json("mtplf.json")
    mtp_data.update({
        "signal": mtp_ai.get("signal", "HOLD"),
        "mnav": mnav,
        "btc_nav_usd_billions": round(MTPLF_HOLD * btc / 1e9, 2) if btc else None,
        "ai_analysis": {
            **mtp_ai,
            "mnav_assessment": (
                "Kaufzone: Bitcoin mit Rabatt" if mnav and mnav < 1.1 else
                "Fair bewertet" if mnav and mnav < 1.8 else
                "Aufpreis – kein Neukauf"
            ),
            "news_headlines": mtplf_news,
            "updated": TODAY
        }
    })
    save_json("mtplf.json", mtp_data)

    # nvda.json
    nvda_data = load_json("nvda.json")
    nvda_data.update({
        "signal": nvda_ai.get("signal", "HOLD"),
        "price_usd": nvda_price,
        "price_52w_high_usd": macro.get("nvda", {}).get("high_52w"),
        "price_52w_low_usd": macro.get("nvda", {}).get("low_52w"),
        "dist_from_52w_high_pct": nvda_dist,
        "ai_analysis": {
            **nvda_ai,
            "ai_capex_trend": "Wachsend",
            "news_headlines": nvda_news,
            "updated": TODAY
        }
    })
    save_json("nvda.json", nvda_data)

    # sol.json
    sol_data = load_json("sol.json")
    sol_data.update({
        "signal": sol_ai.get("signal", "HOLD"),
        "price_usd": sol,
        "ath_dist_pct": sol_ath_dist,
        "ai_analysis": {
            **sol_ai,
            "news_headlines": sol_news,
            "updated": TODAY
        }
    })
    save_json("sol.json", sol_data)

    # tao.json
    tao_data = load_json("tao.json")
    tao_data.update({
        "signal": tao_ai.get("signal", "HOLD"),
        "price_usd": tao,
        "market_cap_usd_billions": tao_mcap_b,
        "ai_analysis": {
            **tao_ai,
            "news_headlines": tao_news,
            "updated": TODAY
        }
    })
    save_json("tao.json", tao_data)

    # ── Abschluss ─────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info(f"Update abgeschlossen!")
    log.info(f"Signale: BTC={signals['btc']} | MTPLF={signals['mtplf']} | "
             f"NVDA={signals['nvda']} | SOL={signals['sol']} | TAO={signals['tao']}")
    log.info(f"BTC ${btc:,.0f} | F&G {fg} | MA200 ${ma200:,.0f} | mNAV {mnav:.3f}x")
    log.info("=" * 60)

    log_strategy(
        f"Wöchentlicher Update abgeschlossen",
        btc=f"${btc:,.0f}/{signals['btc']}",
        fg=fg, ma200=f"${ma200:,.0f}",
        mnav=f"{mnav:.3f}x/{signals['mtplf']}",
        dxy=macro.get("dxy", {}).get("price"),
        sp500=macro.get("sp500", {}).get("price"),
        global_signal=global_signal
    )


if __name__ == "__main__":
    main()
