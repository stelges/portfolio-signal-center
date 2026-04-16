#!/usr/bin/env python3
"""
Portfolio Signal Center – KI Intelligence Updater v3
Läuft jeden Freitag um 18:00 UTC via GitHub Actions.

ARCHITEKTUR v3:
  Rohdaten (APIs) → Claude identifiziert Makro-Regime → entscheidet
  welche Indikatoren relevant sind → Signal + dynamische Preisziele → JSON

  Jedes Asset hat eine eigene vollständige Analyse-Funktion.
  Keine hardcoded Schwellenwerte – die KI urteilt im vollständigen Kontext.
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
MTPLF_HOLD        = 40_177     # BTC Holdings Metaplanet (manuell aktualisieren)
MTPLF_DEBT_USD_M  = 280        # Schulden in Mio USD
MTPLF_SHARES_M    = 1_459      # Ausgegebene Aktien in Mio


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


def parse_ai_json(raw: str) -> dict:
    """Parst Claude-Antwort zu JSON – robust gegen häufige Formatfehler."""
    import re
    text = raw.strip()

    # Markdown-Codeblöcke entfernen
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                text = candidate
                break

    # Ersten { bis letzten } extrahieren
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end+1]

    # Häufige Fehler bereinigen
    text = text.replace("\n", " ")
    # Zahlen mit Tausender-Komma in JSON-Strings fixen (z.B. "74,800" → 74800)
    text = re.sub(r'"(\d{1,3}(?:,\d{3})+)"', lambda m: '"' + m.group(1).replace(",","") + '"', text)
    # Zahlen mit Komma als reine Zahlen (nicht in Strings)
    text = re.sub(r':\s*(\d{1,3}(?:,\d{3})+)([,}\s])', lambda m: ': ' + m.group(1).replace(",","") + m.group(2), text)
    # Trailing commas vor } oder ]
    text = re.sub(r',\s*([}\]])', r'\1', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.warning(f"JSON-Parse fehlgeschlagen ({e}). Raw[:300]: {text[:300]}")
        # Partial-Extraktion: zumindest Signal, Confidence, Summary, Regime retten
        import re as _re
        partial = {}
        for field, pattern in [
            ("signal",       r'"signal"\s*:\s*"(BUY|HOLD|CASH|SELL)"'),
            ("confidence",   r'"confidence"\s*:\s*"([^"]+)"'),
            ("macro_regime", r'"macro_regime"\s*:\s*"([^"]+)"'),
            ("summary",      r'"summary"\s*:\s*"([^"]+)"'),
            ("bull_case",    r'"bull_case"\s*:\s*"([^"]+)"'),
            ("bear_case",    r'"bear_case"\s*:\s*"([^"]+)"'),
            ("signal_changer", r'"signal_changer"\s*:\s*"([^"]+)"'),
        ]:
            m = _re.search(pattern, text)
            if m:
                partial[field] = m.group(1)
        # active_indicators als Array extrahieren
        ai_m = _re.search(r'"active_indicators"\s*:\s*\[(.*?)\]', text, _re.DOTALL)
        if ai_m:
            indicators = _re.findall(r'"([^"]+)"', ai_m.group(1))
            if indicators:
                partial["active_indicators"] = indicators
        if partial.get("signal"):
            log.info(f"Partial-Extraktion erfolgreich: signal={partial['signal']}")
        return partial


# ─────────────────────────────────────────────────────────────────────────────
# 1. KRYPTO-MARKTDATEN (CoinGecko)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_crypto_prices() -> dict:
    """BTC, ETH, SOL, TAO – aktueller Preis."""
    data = safe_get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin,ethereum,solana,bittensor", "vs_currencies": "usd"}
    )
    if data:
        log.info(f"Preise: BTC=${data.get('bitcoin',{}).get('usd')} "
                 f"ETH=${data.get('ethereum',{}).get('usd')} "
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
            "eth_dominance": round(d.get("market_cap_percentage", {}).get("eth", 0), 1),
            "total_market_cap_usd_trillions": round(
                d.get("total_market_cap", {}).get("usd", 0) / 1e12, 2
            )
        }
        log.info(f"Global: BTC-Dominanz={result['btc_dominance']}% "
                 f"ETH={result['eth_dominance']}% "
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
        label = data["data"][0].get("value_classification", "")
        log.info(f"Fear & Greed: {value}/100 ({label})")
        return value
    return None


def fetch_halving_data() -> dict:
    """Berechnet das nächste Halving-Datum blockchain-basiert."""
    NEXT_HALVING_BLOCK = 1_050_000
    AVG_BLOCK_SECONDS  = 600

    height = None
    try:
        r = requests.get("https://mempool.space/api/blocks/tip/height", timeout=10)
        r.raise_for_status()
        height = int(r.text.strip())
        log.info(f"Aktuelle Blockhöhe (mempool.space): {height:,}")
    except Exception:
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

    log.info(f"Halving: {blocks_remaining:,} Blöcke → ~{estimated_dt.strftime('%Y-%m-%d')}")
    return {
        "next_halving_estimated_utc": estimated_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    """S&P 500, DXY, Gold, 10Y-Rendite, NVDA, USD/JPY, Metaplanet-Aktie."""
    tickers = {
        "sp500":   "^GSPC",
        "dxy":     "DX-Y.NYB",
        "gold":    "GC=F",
        "us10y":   "^TNX",
        "nvda":    "NVDA",
        "usdjpy":  "USDJPY=X",
        "mtplf":   "3350.T",    # Metaplanet an der Tokyo Stock Exchange
    }
    result = {}
    for key, ticker in tickers.items():
        d = fetch_yahoo(ticker)
        if d:
            result[key] = d
            log.info(f"Yahoo {ticker}: {d.get('price')}")
        time.sleep(1)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 3. DERIVATIVES (Bybit – Funding Rate + Long/Short)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_funding_rate() -> float | None:
    """BTC Funding Rate via Bybit."""
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
    """BTC Long/Short Ratio via Bybit."""
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
# 4. NEWS (Google RSS)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_news(query: str, limit: int = 5) -> list[str]:
    """Google News RSS Headlines."""
    import xml.etree.ElementTree as ET
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:limit]
        headlines = [item.find("title").text for item in items if item.find("title") is not None]
        log.info(f"News '{query}': {len(headlines)} Headlines")
        return headlines
    except Exception as e:
        log.warning(f"News RSS Fehler: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 5. mNAV BERECHNUNG
# ─────────────────────────────────────────────────────────────────────────────

def calc_mnav(btc_price: float, mtplf_price_usd: float = None) -> float:
    """mNAV = Enterprise Value / BTC-NAV."""
    price = mtplf_price_usd if mtplf_price_usd and mtplf_price_usd > 0 else 1.90
    ev  = MTPLF_SHARES_M * 1_000_000 * price + MTPLF_DEBT_USD_M * 1_000_000
    nav = MTPLF_HOLD * btc_price
    return round(ev / nav, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 6. KI-ANALYSEN – VOLLSTÄNDIGER KONTEXT PRO ASSET
# ─────────────────────────────────────────────────────────────────────────────

REGIME_INSTRUCTION = """
SCHRITT 1 – MAKRO-REGIME:
Identifiziere das aktuelle Makro-Regime (z.B. "Risk-off + starker Dollar", "Stagflation",
"Risk-on + Liquiditätszyklus", "Rezessionsangst + Flucht in Sicherheit", etc.).
Welche 2-3 Indikatoren sind in DIESEM Regime für dieses Asset historisch am aussagekräftigsten?

SCHRITT 2 – ANALYSE:
Bewerte das Asset mit Fokus auf die in Schritt 1 identifizierten Indikatoren.
Berücksichtige alle Daten, aber gewichte sie entsprechend dem Regime.

SCHRITT 3 – PREISZIELE:
Leite konkrete, aktuelle Preisziele ab – keine fixen Regeln, sondern basierend auf:
- Aktuellem Preisniveau und Momentum
- Technischen Zonen (MA200, ATH-Abständen, Unterstützungen)
- Makro-Kontext und Sentiment
- Zyklusphase (vor/nach Halving, Zinszyklus, etc.)
"""

FALLBACK = {
    "signal": "HOLD",
    "confidence": "niedrig",
    "macro_regime": "Unbekannt – Analyse nicht verfügbar",
    "active_indicators": [],
    "summary": "Analyse temporär nicht verfügbar.",
    "price_targets": {},
    "bull_case": "—",
    "bear_case": "—",
    "signal_changer": "—"
}


def analyze_btc(client: anthropic.Anthropic, market: dict) -> dict:
    """
    Vollständige BTC-Analyse mit Makro-Kontext und dynamischen Preiszielen.
    Claude identifiziert das Regime und entscheidet Signal + Preisziele.
    """
    btc      = market.get("btc")
    fg       = market.get("fg")
    p30      = market.get("p30")
    ma200    = market.get("ma200")
    ma50     = market.get("ma50")
    dom      = market.get("btc_dominance")
    eth_dom  = market.get("eth_dominance")
    total_mc = market.get("total_market_cap_usd_trillions")
    funding  = market.get("funding_rate")
    ls       = market.get("long_short_ratio")
    sp500    = market.get("sp500", {})
    dxy      = market.get("dxy", {})
    gold     = market.get("gold", {})
    us10y    = market.get("us10y", {})
    halving  = market.get("halving", {})
    news     = market.get("btc_news", [])

    ath_dist     = round(((btc - BTC_ATH) / BTC_ATH) * 100, 1) if btc else None
    above_200ma  = (btc > ma200) if (btc and ma200) else None
    sp500_30d    = pct_change(sp500.get("prev", 0), sp500.get("price", 0)) if sp500 else None
    gold_trend   = "steigend" if gold.get("price", 0) > gold.get("prev", 0) else "fallend"
    blocks_rem   = halving.get("blocks_remaining")
    halving_date = halving.get("next_halving_estimated_utc", "")[:10]

    prompt = f"""Du bist ein unabhängiger Makro- und Krypto-Analyst mit 20 Jahren Erfahrung.
Heute ist {TODAY}. Kontext: 7-Jahres-Investitionsplan, monatliches DCA, Sparrate 1.000–4.000€/Monat.

{REGIME_INSTRUCTION}

=== BITCOIN – VOLLSTÄNDIGE DATENLAGE ===

PREISSTRUKTUR:
  Aktueller Preis:  ${btc:,.0f}
  ATH ($109k):      {ath_dist:+.1f}%
  30d-Performance:  {p30:+.1f}%
  200-Tage MA:      ${ma200:,.0f} → BTC {"DARÜBER ✓ (bullish)" if above_200ma else "DARUNTER ✗ (bearish Warnung)"}
  50-Tage MA:       ${ma50:,.0f}

SENTIMENT & DERIVATE:
  Fear & Greed:     {fg}/100 ({"Extremangst – historisch Akkumulationszone" if fg and fg < 25 else "Extreme Gier – Überhitzungsgefahr" if fg and fg > 75 else "Neutral" if fg and fg > 40 else "Angst – erhöhte Vorsicht"})
  Funding Rate:     {funding if funding else "N/A"}% ({"Negativ – Short-Druck, Kapitulation möglich" if funding and funding < 0 else "Positiv hoch – Long-Überhang, Überhitzung" if funding and funding > 0.05 else "Neutral"})
  Long/Short Ratio: {ls if ls else "N/A"} ({"Long-lastig – möglicher Kontraindikator" if ls and ls > 1.2 else "Ausgeglichen"})

MARKTSTRUKTUR:
  BTC-Dominanz:     {dom}% ({"Risk-off, Altcoins schwach" if dom and dom > 57 else "Altcoin-Season möglich" if dom and dom < 48 else "Normal"})
  ETH-Dominanz:     {eth_dom}%
  Total Crypto MC:  ${total_mc}T

MAKRO-UMFELD:
  S&P 500:          ${sp500.get('price', '—')} ({sp500_30d:+.1f}% 30d) | 52W: ${sp500.get('low_52w','—')}–${sp500.get('high_52w','—')}
  DXY (Dollar):     {dxy.get('price', '—')} ({"Starker Dollar → Gegenwind für BTC" if dxy.get('price', 0) > 104 else "Schwacher Dollar → Rückenwind für BTC" if dxy.get('price', 0) < 100 else "Neutral"})
  Gold:             ${gold.get('price', '—')} ({gold_trend}) – Risikoindikator
  10Y US-Rendite:   {us10y.get('price', '—')}% ({"Hoch – teures Kapital, belastet Risk-Assets" if us10y.get('price', 0) > 4.5 else "Moderat – kein größerer Gegenwind" if us10y.get('price', 0) < 4.0 else "Erhöht"})

ZYKLUSKONTEXT:
  Nächstes Halving: Block 1.050.000 (~{halving_date}) – noch {blocks_rem:,} Blöcke
  Halving-Logik:    Historisch steigt BTC 12–18 Monate nach Halving auf neue ATHs.
                    Jetzt {"Pre-Halving – Akkumulationsphase" if blocks_rem and blocks_rem > 50000 else "Nahe Halving – maximale Aggression historisch sinnvoll"}

AKTUELLE HEADLINES:
{chr(10).join(f'  - {h}' for h in news[:6])}

=== DEINE ENTSCHEIDUNG ===
Signal-Definitionen:
  BUY:  Sparrate 1.5–2× erhöhen + Cash-Reserve einsetzen
  HOLD: Normales monatliches DCA fortführen
  CASH: Sparrate halbieren, keine Neukäufe, Pulver trocken halten
  SELL: Teilgewinne sichern (nur bei extremen Überbewertungssignalen)

Antworte NUR mit diesem JSON (kein Markdown, keine Erklärungen außerhalb):
{{
  "signal": "BUY|HOLD|CASH|SELL",
  "confidence": "hoch|mittel|niedrig",
  "macro_regime": "Kurze Beschreibung des dominanten Regimes",
  "active_indicators": ["Indikator 1 (warum jetzt relevant)", "Indikator 2", "Indikator 3"],
  "summary": "2-3 Sätze zur aktuellen Phase. Was dominiert gerade?",
  "price_targets": {{
    "buy_zone": ZAHL,
    "buy_aggressiv": ZAHL,
    "take_profit_1_price": ZAHL,
    "take_profit_1_action": "z.B. 20% verkaufen",
    "take_profit_2_price": ZAHL,
    "take_profit_2_action": "z.B. 50% verkaufen",
    "concern_below": ZAHL
  }},
  "bull_case": "Max 2 Sätze.",
  "bear_case": "Max 2 Sätze.",
  "signal_changer": "Was müsste sich ändern damit das Signal kippt?"
}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}]
        )
        result = parse_ai_json(response.content[0].text)
        if result:
            log.info(f"BTC Signal: {result.get('signal')} | Regime: {result.get('macro_regime','—')[:50]}")
            old_signal = load_json("btc.json").get("signal", "HOLD")
            if result.get("signal") != old_signal:
                log_strategy(f"BTC SIGNAL: {old_signal} → {result['signal']}",
                             regime=result.get("macro_regime", "—")[:60])
            return result
        return FALLBACK.copy()
    except Exception as e:
        log.error(f"BTC Claude Fehler: {e}")
        return FALLBACK.copy()


def analyze_mtplf(client: anthropic.Anthropic, market: dict) -> dict:
    """
    Metaplanet-Analyse: mNAV + BTC-Kontext + Japan-Makro + Aktienkurs.
    """
    btc        = market.get("btc")
    mnav       = market.get("mnav")
    btc_signal = market.get("btc_signal", "HOLD")
    mtplf_px   = market.get("mtplf_price_usd")
    mtplf_52h  = market.get("mtplf_52w_high")
    mtplf_52l  = market.get("mtplf_52w_low")
    usdjpy     = market.get("usdjpy")
    sp500      = market.get("sp500", {})
    dxy        = market.get("dxy", {})
    fg         = market.get("fg")
    btc_dom    = market.get("btc_dominance")
    news       = market.get("news", [])

    ath_dist_btc = round(((btc - BTC_ATH) / BTC_ATH) * 100, 1) if btc else None
    btc_nav_b    = round(MTPLF_HOLD * btc / 1e9, 2) if btc else None
    ev_b         = round((MTPLF_SHARES_M * 1e6 * (mtplf_px or 1.90) + MTPLF_DEBT_USD_M * 1e6) / 1e9, 2)

    prompt = f"""Du bist ein unabhängiger Analyst spezialisiert auf BTC-Treasury-Companies und Japan-Aktien.
Heute ist {TODAY}. Kontext: 7-Jahres-Plan, monatliches DCA.

{REGIME_INSTRUCTION}

=== METAPLANET (MTPLF / 3350.T) – VOLLSTÄNDIGE DATENLAGE ===

KERN-INDIKATOR mNAV:
  Aktueller mNAV:   {mnav:.4f}x
  Formel:           Enterprise Value ÷ BTC-NAV
  EV:               ${ev_b:.2f}B (MarketCap + Schulden)
  BTC-NAV:          ${btc_nav_b:.2f}B ({MTPLF_HOLD:,} BTC × aktueller BTC-Preis)
  Schulden:         ${MTPLF_DEBT_USD_M}M USD
  Aktien:           {MTPLF_SHARES_M}M
  mNAV-Interpretation:
    < 0.9x = Bitcoin mit starkem Rabatt (sehr selten, aggressiver Kauf)
    0.9–1.2x = Kaufzone (BTC günstiger durch Aktie als direkt)
    1.2–1.8x = Fair bewertet, normales DCA
    1.8–2.5x = Aufpreis, keine Neukäufe
    > 2.5x = Signifikanter Aufpreis, Teilverkauf erwägen
    > 4.0x = Starke Überbewertung, 50% Verkauf

AKTIE & MARKT:
  Aktienkurs (USD): ${mtplf_px if mtplf_px else "N/A"} | 52W: ${mtplf_52l}–${mtplf_52h}
  USD/JPY:          {usdjpy if usdjpy else "N/A"} ({"Schwacher Yen → Metaplanet profitiert von USD-Aktiva" if usdjpy and usdjpy > 145 else "Starker Yen → Gegenwind für USD-Assets auf TSE" if usdjpy and usdjpy < 130 else "Neutral"})
  BTC-Kontext:      BTC ${btc:,.0f} ({ath_dist_btc:+.1f}% vom ATH)
  BTC-Signal (KI):  {btc_signal}

MAKRO:
  Fear & Greed:     {fg}/100
  BTC-Dominanz:     {btc_dom}%
  S&P 500:          ${sp500.get('price','—')}
  DXY:              {dxy.get('price','—')}

AKTUELLE HEADLINES:
{chr(10).join(f'  - {h}' for h in news[:5])}

STRATEGIE-KONTEXT:
  Metaplanet ist kein normales Unternehmen – es ist ein gehebelter BTC-Proxy.
  Wenn BTC 5x macht, macht MTPLF potenziell 10–20x (durch mNAV-Expansion + BTC-Anstieg).
  Primäres Kaufkriterium: mNAV, nicht Chartmuster.

=== DEINE ENTSCHEIDUNG ===
Antworte NUR mit diesem JSON:
{{
  "signal": "BUY|HOLD|CASH|SELL",
  "confidence": "hoch|mittel|niedrig",
  "macro_regime": "Kurze Beschreibung",
  "active_indicators": ["Indikator 1 (warum relevant)", "Indikator 2", "Indikator 3"],
  "summary": "2-3 Sätze zur aktuellen Metaplanet-Situation.",
  "price_targets": {{
    "buy_zone_mnav": ZAHL,
    "buy_aggressiv_mnav": ZAHL,
    "take_profit_1_mnav": ZAHL,
    "take_profit_1_action": "z.B. 20% verkaufen",
    "take_profit_2_mnav": ZAHL,
    "take_profit_2_action": "z.B. 50% verkaufen",
    "concern_mnav": ZAHL
  }},
  "bull_case": "Max 2 Sätze.",
  "bear_case": "Max 2 Sätze.",
  "signal_changer": "Was würde das Signal kippen?"
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = parse_ai_json(response.content[0].text)
        if result:
            log.info(f"MTPLF Signal: {result.get('signal')} | mNAV: {mnav:.3f}x")
            return result
        return FALLBACK.copy()
    except Exception as e:
        log.error(f"Metaplanet Claude Fehler: {e}")
        return FALLBACK.copy()


def analyze_nvda(client: anthropic.Anthropic, market: dict) -> dict:
    """
    Nvidia-Analyse: KI-Capex-Zyklus + Makro + Bewertung + Earnings.
    """
    price    = market.get("price")
    high_52w = market.get("high_52w")
    low_52w  = market.get("low_52w")
    sp500    = market.get("sp500", {})
    dxy      = market.get("dxy", {})
    us10y    = market.get("us10y", {})
    fg       = market.get("fg")
    news     = market.get("news", [])

    dist_from_high = round(((price - high_52w) / high_52w) * 100, 1) if price and high_52w else None
    dist_from_low  = round(((price - low_52w)  / low_52w)  * 100, 1) if price and low_52w  else None
    sp500_30d = pct_change(sp500.get("prev", 0), sp500.get("price", 0)) if sp500 else None

    prompt = f"""Du bist ein erfahrener Tech-Analyst spezialisiert auf Halbleiter und KI-Infrastruktur.
Heute ist {TODAY}. Kontext: 7-Jahres-Plan, monatliches DCA, kein Trading.

{REGIME_INSTRUCTION}

=== NVIDIA (NVDA) – VOLLSTÄNDIGE DATENLAGE ===

PREISSTRUKTUR:
  Aktueller Preis:     ${price}
  52W-Hoch:            ${high_52w} (aktuell {dist_from_high:+.1f}% davon)
  52W-Tief:            ${low_52w} (aktuell {dist_from_low:+.1f}% davon)

FUNDAMENTALE TREIBER:
  KI-Capex-Zyklus:     Microsoft, Google, Amazon, Meta erhöhen 2026 Data-Center-Ausgaben
                       auf kombiniert >$300B (Rekordhoch). Nvidia liefert >80% der Chips.
  Marktstellung:       CUDA-Ökosystem = faktisches Monopol auf KI-Training
  Nächste Earnings:    Mai 2026 (Blackwell-Ramp, Data-Center-Wachstum im Fokus)
  Risiken:             Export-Beschränkungen (China), AMD/Intel-Konkurrenz, Bewertung

MAKRO-UMFELD:
  S&P 500:             ${sp500.get('price','—')} ({sp500_30d:+.1f}% 30d)
  10Y US-Rendite:      {us10y.get('price','—')}% ({"Zinsdruck auf Wachstumswerte" if us10y.get('price',0) > 4.5 else "Moderat"})
  DXY:                 {dxy.get('price','—')} ({"Starker Dollar → Exportrisiko" if dxy.get('price',0) > 104 else "Neutral"})
  Markt-Sentiment:     Fear & Greed {fg}/100

AKTUELLE HEADLINES:
{chr(10).join(f'  - {h}' for h in news[:6])}

PORTFOLIO-KONTEXT:
  NVDA = "Stabilitätsanker" im Portfolio – läuft auch wenn Crypto schläft.
  Kein Trading, aber Rebalancing wenn stark über-/unterbewertet.
  KGV 30–60x = akzeptabel bei diesem Wachstum. Über 70x erhöhte Vorsicht.

=== DEINE ENTSCHEIDUNG ===
Antworte NUR mit diesem JSON:
{{
  "signal": "BUY|HOLD|CASH|SELL",
  "confidence": "hoch|mittel|niedrig",
  "macro_regime": "Kurze Beschreibung",
  "active_indicators": ["Indikator 1 (warum relevant)", "Indikator 2", "Indikator 3"],
  "summary": "2-3 Sätze zur aktuellen Nvidia-Situation.",
  "price_targets": {{
    "buy_zone": ZAHL,
    "buy_aggressiv": ZAHL,
    "take_profit_1_price": ZAHL,
    "take_profit_1_action": "z.B. 20% verkaufen",
    "take_profit_2_price": ZAHL,
    "take_profit_2_action": "z.B. 50% verkaufen",
    "concern_below": ZAHL
  }},
  "bull_case": "Max 2 Sätze.",
  "bear_case": "Max 2 Sätze.",
  "signal_changer": "Was würde das Signal kippen?"
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = parse_ai_json(response.content[0].text)
        if result:
            log.info(f"NVDA Signal: {result.get('signal')} | ${price} ({dist_from_high:+.1f}% vom 52W-Hoch)")
            return result
        return FALLBACK.copy()
    except Exception as e:
        log.error(f"Nvidia Claude Fehler: {e}")
        return FALLBACK.copy()


def analyze_sol(client: anthropic.Anthropic, market: dict) -> dict:
    """
    Solana-Analyse: L1-Fundamentaldaten + BTC-Kontext + Altcoin-Zyklus + ETF.
    """
    sol      = market.get("sol")
    eth      = market.get("eth")
    btc      = market.get("btc")
    btc_dom  = market.get("btc_dominance")
    eth_dom  = market.get("eth_dominance")
    fg       = market.get("fg")
    sp500    = market.get("sp500", {})
    us10y    = market.get("us10y", {})
    btc_sig  = market.get("btc_signal", "HOLD")
    news     = market.get("news", [])

    ath_dist = round(((sol - SOL_ATH) / SOL_ATH) * 100, 1) if sol else None
    sol_eth_ratio = round(sol / eth, 4) if sol and eth and eth > 0 else None

    prompt = f"""Du bist ein Layer-1-Blockchain-Analyst mit Fokus auf fundamentale Netzwerkwerte.
Heute ist {TODAY}. Kontext: 7-Jahres-Plan, Solana = 14% Portfolio, kein Trading.

{REGIME_INSTRUCTION}

=== SOLANA (SOL) – VOLLSTÄNDIGE DATENLAGE ===

PREISSTRUKTUR:
  Aktueller Preis:    ${sol:.2f}
  ATH ($294):         {ath_dist:+.1f}%
  ETH-Preis:          ${eth:.2f} | SOL/ETH-Ratio: {sol_eth_ratio} (höher = SOL outperformt)

FUNDAMENTALDATEN:
  Network Revenue:    ~$2.85B/Jahr (echte Gebühreneinnahmen, kein Narrativ)
  Firedancer:         2026-Upgrade → Ziel 1M TPS (aktuell ~5.000)
  DeFi TVL:           Marktführer bei DEX-Volumen, Raydium, Jupiter wachsen
  ETF-Status:         SOL-ETF Antrag bei SEC eingereicht (21Shares, Bitwise)
  Staking-Yield:      ~6-7% APY
  Konkurrenz:         ETH (${ eth:.0f}) vs SOL (${sol:.0f}) – SOL gewinnt bei Throughput

ALTCOIN-ZYKLUS-KONTEXT:
  BTC-Dominanz:       {btc_dom}% ({"Zu hoch – Altcoins hinken nach" if btc_dom and btc_dom > 57 else "Rückgang möglich – Altcoin-Season" if btc_dom and btc_dom < 50 else "Übergang"})
  ETH-Dominanz:       {eth_dom}%
  BTC-Signal (KI):    {btc_sig} – Altcoins folgen BTC, aber mit Hebel
  Fear & Greed:       {fg}/100

MAKRO:
  S&P 500:            ${sp500.get('price','—')}
  10Y Rendite:        {us10y.get('price','—')}%

AKTUELLE HEADLINES:
{chr(10).join(f'  - {h}' for h in news[:5])}

PORTFOLIO-KONTEXT:
  SOL = "L1-Wachstumswert" mit realem Netzwerk-Revenue.
  Nicht nur Narrativ – echte Adoption macht es robuster als reine Meme-Chains.
  Kaufziel: Wenn BTC BUY und BTC-Dominanz fällt = doppelter Rückenwind für SOL.

=== DEINE ENTSCHEIDUNG ===
Antworte NUR mit diesem JSON:
{{
  "signal": "BUY|HOLD|CASH|SELL",
  "confidence": "hoch|mittel|niedrig",
  "macro_regime": "Kurze Beschreibung",
  "active_indicators": ["Indikator 1 (warum relevant)", "Indikator 2", "Indikator 3"],
  "summary": "2-3 Sätze zur aktuellen Solana-Situation.",
  "price_targets": {{
    "buy_zone": ZAHL,
    "buy_aggressiv": ZAHL,
    "take_profit_1_price": ZAHL,
    "take_profit_1_action": "z.B. 25% verkaufen",
    "take_profit_2_price": ZAHL,
    "take_profit_2_action": "z.B. 50% verkaufen",
    "concern_below": ZAHL
  }},
  "bull_case": "Max 2 Sätze.",
  "bear_case": "Max 2 Sätze.",
  "signal_changer": "Was würde das Signal kippen?"
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = parse_ai_json(response.content[0].text)
        if result:
            log.info(f"SOL Signal: {result.get('signal')} | ${sol:.2f} ({ath_dist:+.1f}% ATH)")
            return result
        return FALLBACK.copy()
    except Exception as e:
        log.error(f"Solana Claude Fehler: {e}")
        return FALLBACK.copy()


def analyze_tao(client: anthropic.Anthropic, market: dict) -> dict:
    """
    Bittensor-Analyse: KI-Narrativ + Marktkapitalisierung + BTC-Zykluskontext.
    """
    tao       = market.get("tao")
    tao_mcap  = market.get("tao_mcap_b")
    btc       = market.get("btc")
    btc_dom   = market.get("btc_dominance")
    btc_sig   = market.get("btc_signal", "HOLD")
    fg        = market.get("fg")
    sp500     = market.get("sp500", {})
    news      = market.get("news", [])

    # Vergleichs-Market-Caps für Einordnung
    prompt = f"""Du bist ein Analyst spezialisiert auf KI-Krypto-Tokens und dezentrale Infrastruktur.
Heute ist {TODAY}. Kontext: 7-Jahres-Plan, TAO = 8% Portfolio (höchstes Risiko/Reward), kein Trading.

{REGIME_INSTRUCTION}

=== BITTENSOR (TAO) – VOLLSTÄNDIGE DATENLAGE ===

PREISSTRUKTUR:
  Aktueller Preis:    ${tao:.2f}
  Market Cap:         ${tao_mcap:.2f}B
  Zirkulierend:       ~{TAO_CIRC/1e6:.1f}M TAO von 21M Max-Supply
  TAO-Halving:        Dezember 2025 (abgeschlossen) – Emission halbiert

EINORDNUNG MARKTKAPITALISIERUNG:
  Aktuell ${tao_mcap:.1f}B – Vergleich:
  - Chainlink (LINK): ~$8B MCap
  - Render (RNDR):    ~$3B MCap
  - Fetch.ai:         ~$1B MCap
  TAO adressiert dezentrale KI-Infrastruktur – theoretisch größter Markt davon.

FUNDAMENTALDATEN:
  Subnets:            Wachsendes Ökosystem (60+ aktive Subnets)
  Use Case:           Dezentrales KI-Training + Inferenz-Marktplatz
  Grayscale-ETF:      Antrag eingereicht (wie bei SOL, BTC zuvor)
  Bitcoin-Mechanik:   21M Supply Cap + Halving → deflationär
  Risiko:             Frühe Phase, hohes Ausführungsrisiko, Konkurrenz durch Zentralisierung

KI-NARRATIV-KONTEXT:
  Nvidia-Capex wächst → steigert Bewusstsein für KI-Infrastruktur allgemein
  ChatGPT/Claude-Boom → Nachfrage nach dezentraler KI-Alternative wächst
  Aber: Dezentrale KI ist noch Nische vs. zentralisierte Hyperscaler

ALTCOIN-KONTEXT:
  BTC-Signal (KI):    {btc_sig}
  BTC-Dominanz:       {btc_dom}% ({"Ungünstig für High-Beta-Altcoins wie TAO" if btc_dom and btc_dom > 57 else "Rückgang = positiv für Altcoins"})
  Fear & Greed:       {fg}/100
  S&P 500:            ${sp500.get('price','—')}

AKTUELLE HEADLINES:
{chr(10).join(f'  - {h}' for h in news[:5])}

PORTFOLIO-KONTEXT:
  TAO = "asymmetrische Wette" – kleinster MCap = größtes Upside-Potenzial.
  Akzeptiert höchstes Risiko für potenzielle 20–50x-Rendite bis 2030.
  Kaufentscheidung: Nur wenn BTC-Signal BUY oder HOLD, nie in Risk-off-Phase aggressiv kaufen.

=== DEINE ENTSCHEIDUNG ===
Antworte NUR mit diesem JSON:
{{
  "signal": "BUY|HOLD|CASH|SELL",
  "confidence": "hoch|mittel|niedrig",
  "macro_regime": "Kurze Beschreibung",
  "active_indicators": ["Indikator 1 (warum relevant)", "Indikator 2", "Indikator 3"],
  "summary": "2-3 Sätze zur aktuellen TAO-Situation.",
  "price_targets": {{
    "buy_zone": ZAHL,
    "buy_aggressiv": ZAHL,
    "take_profit_1_price": ZAHL,
    "take_profit_1_action": "z.B. 30% verkaufen",
    "take_profit_2_price": ZAHL,
    "take_profit_2_action": "z.B. 50% verkaufen",
    "concern_below": ZAHL
  }},
  "bull_case": "Max 2 Sätze.",
  "bear_case": "Max 2 Sätze.",
  "signal_changer": "Was würde das Signal kippen?"
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = parse_ai_json(response.content[0].text)
        if result:
            log.info(f"TAO Signal: {result.get('signal')} | ${tao:.0f} MCap=${tao_mcap:.2f}B")
            return result
        return FALLBACK.copy()
    except Exception as e:
        log.error(f"Bittensor Claude Fehler: {e}")
        return FALLBACK.copy()


def create_weekly_briefing(client: anthropic.Anthropic, signals: dict, market: dict) -> dict:
    """Strukturiertes Wochenbriefing als JSON."""
    prompt = f"""Heute ist {TODAY}. Du schreibst das Wochenbriefing für ein Portfolio-Dashboard.

Portfolio: 38% BTC, 20% Metaplanet, 15% NVDA, 14% SOL, 8% TAO. 5% Cash-Reserve.
7-Jahres-Plan. Monatliches DCA. Kein Trading.

Signale diese Woche:
- BTC:       {signals.get('btc','HOLD')} | ${market.get('btc','—'):,.0f} | F&G {market.get('fg','—')}/100
- Metaplanet:{signals.get('mtplf','HOLD')} | mNAV {market.get('mnav','—')}x
- NVDA:      {signals.get('nvda','HOLD')} | ${market.get('nvda','—')}
- SOL:       {signals.get('sol','HOLD')} | ${market.get('sol','—')}
- TAO:       {signals.get('tao','HOLD')} | MCap ${market.get('tao_mcap_b','—')}B

Makro: DXY={market.get('dxy','—')} | S&P={market.get('sp500','—')} | 10Y={market.get('us10y','—')}%

Antworte als JSON (kein Markdown):
{{
  "title": "Kurzer Titel (max 6 Wörter)",
  "status": "Ein Satz zum Portfolio-Status.",
  "insight": "Die wichtigste Erkenntnis dieser Woche (1-2 Sätze).",
  "action": "Was konkret tun? (1 Satz)"
}}
Sprache: Deutsch. Klar, direkt."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        result = parse_ai_json(raw)
        if result:
            return result
        return {"title": "Wochenbriefing", "status": raw, "insight": "", "action": ""}
    except Exception as e:
        log.error(f"Briefing Fehler: {e}")
        return {"title": "Briefing", "status": "Wochenbriefing temporär nicht verfügbar.", "insight": "", "action": ""}


# ─────────────────────────────────────────────────────────────────────────────
# 7. MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"Portfolio Signal Center · KI-Update v3 · {TODAY}")
    log.info("=" * 60)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY fehlt!")
    client = anthropic.Anthropic(api_key=api_key)

    # ── SCHRITT 1: Alle Marktdaten sammeln ───────────────────────────────────
    log.info("── Schritt 1: Marktdaten ──")

    prices = fetch_crypto_prices();            time.sleep(2)
    global_market = fetch_global_market();     time.sleep(2)
    fg = fetch_fear_greed();                   time.sleep(2)
    halving = fetch_halving_data();            time.sleep(2)

    btc_history = fetch_btc_history(days=210); time.sleep(3)
    ma200 = calc_moving_average(btc_history, 200)
    ma50  = calc_moving_average(btc_history, 50)
    p30   = fetch_btc_30d_change();            time.sleep(3)
    log.info(f"MA200=${ma200} MA50=${ma50} 30d={p30}%")

    macro = fetch_macro_data();                time.sleep(2)
    funding = fetch_funding_rate();            time.sleep(2)
    ls_ratio = fetch_long_short_ratio();       time.sleep(2)

    # Preise extrahieren
    btc  = prices.get("bitcoin",   {}).get("usd")
    eth  = prices.get("ethereum",  {}).get("usd")
    sol  = prices.get("solana",    {}).get("usd")
    tao  = prices.get("bittensor", {}).get("usd")

    # Metaplanet-Aktienkurs (JPY → USD via USD/JPY)
    mtplf_raw = macro.get("mtplf", {})
    usdjpy_price = macro.get("usdjpy", {}).get("price")
    mtplf_price_jpy = mtplf_raw.get("price")  # Kurs in JPY von Yahoo
    mtplf_price_usd = round(mtplf_price_jpy / usdjpy_price, 4) if mtplf_price_jpy and usdjpy_price else None
    mtplf_52h_usd   = round(mtplf_raw.get("high_52w", 0) / usdjpy_price, 4) if usdjpy_price else None
    mtplf_52l_usd   = round(mtplf_raw.get("low_52w",  0) / usdjpy_price, 4) if usdjpy_price else None

    mnav = calc_mnav(btc, mtplf_price_usd) if btc else None
    nvda_price = macro.get("nvda", {}).get("price")
    tao_mcap_b = round(tao * TAO_CIRC / 1e9, 2) if tao else None
    ath_dist_btc = round(((btc - BTC_ATH) / BTC_ATH) * 100, 1) if btc else None

    log.info(f"Preise: BTC=${btc:,.0f} ETH=${eth} SOL=${sol} TAO=${tao} mNAV={mnav}")
    log.info(f"Metaplanet: {mtplf_price_jpy}JPY = ${mtplf_price_usd} | USD/JPY={usdjpy_price}")

    # ── SCHRITT 2: News ───────────────────────────────────────────────────────
    log.info("── Schritt 2: News ──")
    btc_news   = fetch_news("Bitcoin BTC macro market 2026",             5); time.sleep(2)
    mtplf_news = fetch_news("Metaplanet Bitcoin treasury Japan 3350",    4); time.sleep(2)
    nvda_news  = fetch_news("Nvidia NVDA earnings AI datacenter export", 5); time.sleep(2)
    sol_news   = fetch_news("Solana SOL ETF Firedancer DeFi 2026",       4); time.sleep(2)
    tao_news   = fetch_news("Bittensor TAO AI decentralized Grayscale",  4); time.sleep(2)

    # ── SCHRITT 3: KI-Analysen ────────────────────────────────────────────────
    log.info("── Schritt 3: KI-Analysen (Claude v3) ──")

    # BTC – vollständiger Makro-Kontext
    btc_market = {
        "btc": btc, "fg": fg, "p30": p30,
        "ma200": ma200, "ma50": ma50,
        "btc_dominance": global_market.get("btc_dominance"),
        "eth_dominance": global_market.get("eth_dominance"),
        "total_market_cap_usd_trillions": global_market.get("total_market_cap_usd_trillions"),
        "funding_rate": funding,
        "long_short_ratio": ls_ratio,
        "sp500": macro.get("sp500", {}),
        "dxy":   macro.get("dxy",   {}),
        "gold":  macro.get("gold",  {}),
        "us10y": macro.get("us10y", {}),
        "halving": halving,
        "btc_news": btc_news
    }
    btc_ai = analyze_btc(client, btc_market)
    time.sleep(4)

    # Metaplanet
    mtplf_market = {
        "btc": btc, "mnav": mnav,
        "btc_signal": btc_ai.get("signal", "HOLD"),
        "mtplf_price_usd": mtplf_price_usd,
        "mtplf_52w_high": mtplf_52h_usd,
        "mtplf_52w_low":  mtplf_52l_usd,
        "usdjpy": usdjpy_price,
        "sp500": macro.get("sp500", {}),
        "dxy":   macro.get("dxy",   {}),
        "fg": fg,
        "btc_dominance": global_market.get("btc_dominance"),
        "news": mtplf_news
    }
    mtp_ai = analyze_mtplf(client, mtplf_market)
    time.sleep(4)

    # NVDA
    nvda_market = {
        "price":    nvda_price,
        "high_52w": macro.get("nvda", {}).get("high_52w"),
        "low_52w":  macro.get("nvda", {}).get("low_52w"),
        "sp500":  macro.get("sp500", {}),
        "dxy":    macro.get("dxy",   {}),
        "us10y":  macro.get("us10y", {}),
        "fg": fg,
        "news": nvda_news
    }
    nvda_ai = analyze_nvda(client, nvda_market)
    time.sleep(4)

    # Solana
    sol_market = {
        "sol": sol, "eth": eth, "btc": btc,
        "btc_signal": btc_ai.get("signal", "HOLD"),
        "btc_dominance": global_market.get("btc_dominance"),
        "eth_dominance": global_market.get("eth_dominance"),
        "sp500":  macro.get("sp500", {}),
        "us10y":  macro.get("us10y", {}),
        "fg": fg,
        "news": sol_news
    }
    sol_ai = analyze_sol(client, sol_market)
    time.sleep(4)

    # Bittensor
    tao_market = {
        "tao": tao, "tao_mcap_b": tao_mcap_b,
        "btc": btc,
        "btc_signal": btc_ai.get("signal", "HOLD"),
        "btc_dominance": global_market.get("btc_dominance"),
        "sp500": macro.get("sp500", {}),
        "fg": fg,
        "news": tao_news
    }
    tao_ai = analyze_tao(client, tao_market)
    time.sleep(4)

    # Wochenbriefing
    signals = {
        "btc":  btc_ai.get("signal",  "HOLD"),
        "mtplf": mtp_ai.get("signal", "HOLD"),
        "nvda": nvda_ai.get("signal", "HOLD"),
        "sol":  sol_ai.get("signal",  "HOLD"),
        "tao":  tao_ai.get("signal",  "HOLD"),
    }
    briefing_market = {
        "btc": btc, "eth": eth, "sol": sol, "nvda": nvda_price,
        "mnav": mnav, "tao_mcap_b": tao_mcap_b, "fg": fg,
        "dxy":   macro.get("dxy",   {}).get("price"),
        "sp500": macro.get("sp500", {}).get("price"),
        "us10y": macro.get("us10y", {}).get("price"),
    }
    briefing = create_weekly_briefing(client, signals, briefing_market)

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
        "last_updated":   NOW_UTC,
        "updated_by":     "update_intelligence.py v3 (GitHub Actions)",
        "global_signal":  global_signal,
        "global_summary": global_summary,
        "fear_greed":     fg,
        "btc_price_usd":  btc,
        "btc_ath_dist_pct": ath_dist_btc,
        "btc_30d_change_pct": p30,
        "btc_dominance_pct": global_market.get("btc_dominance"),
        "macro": {
            "sp500": macro.get("sp500", {}).get("price"),
            "dxy":   macro.get("dxy",   {}).get("price"),
            "gold":  macro.get("gold",  {}).get("price"),
            "us10y": macro.get("us10y", {}).get("price"),
            "usdjpy": usdjpy_price,
        },
        "next_halving":      halving,
        "next_update":       (datetime.date.today() + datetime.timedelta(days=7)).isoformat() + "T18:00:00Z",
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
        "signal":        btc_ai.get("signal", "HOLD"),
        "price_usd":     btc,
        "ath_dist_pct":  ath_dist_btc,
        "change_30d_pct": p30,
        "ma200":         ma200,
        "ma50":          ma50,
        "above_200ma":   (btc > ma200) if (btc and ma200) else None,
        "funding_rate":  funding,
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
        "signal":           mtp_ai.get("signal", "HOLD"),
        "mnav":             mnav,
        "btc_nav_usd_billions": round(MTPLF_HOLD * btc / 1e9, 2) if btc else None,
        "stock_price_usd":  mtplf_price_usd,
        "stock_price_jpy":  mtplf_price_jpy,
        "usdjpy":           usdjpy_price,
        "ai_analysis": {
            **mtp_ai,
            "news_headlines": mtplf_news,
            "updated": TODAY
        }
    })
    save_json("mtplf.json", mtp_data)

    # nvda.json
    nvda_52h = macro.get("nvda", {}).get("high_52w")
    nvda_52l = macro.get("nvda", {}).get("low_52w")
    nvda_dist = round(((nvda_price - nvda_52h) / nvda_52h) * 100, 1) if nvda_price and nvda_52h else None
    nvda_data = load_json("nvda.json")
    nvda_data.update({
        "signal":               nvda_ai.get("signal", "HOLD"),
        "price_usd":            nvda_price,
        "price_52w_high_usd":   nvda_52h,
        "price_52w_low_usd":    nvda_52l,
        "dist_from_52w_high_pct": nvda_dist,
        "ai_analysis": {
            **nvda_ai,
            "news_headlines": nvda_news,
            "updated": TODAY
        }
    })
    save_json("nvda.json", nvda_data)

    # sol.json
    sol_ath_dist = round(((sol - SOL_ATH) / SOL_ATH) * 100, 1) if sol else None
    sol_data = load_json("sol.json")
    sol_data.update({
        "signal":      sol_ai.get("signal", "HOLD"),
        "price_usd":   sol,
        "ath_dist_pct": sol_ath_dist,
        "eth_price_usd": eth,
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
        "signal":               tao_ai.get("signal", "HOLD"),
        "price_usd":            tao,
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
    log.info(f"Update v3 abgeschlossen!")
    log.info(f"Signale: BTC={signals['btc']} | MTPLF={signals['mtplf']} | "
             f"NVDA={signals['nvda']} | SOL={signals['sol']} | TAO={signals['tao']}")
    log.info(f"BTC ${btc:,.0f} | F&G {fg} | MA200 ${ma200:,.0f} | mNAV {mnav:.3f}x")
    log.info("=" * 60)

    log_strategy(
        "Wöchentlicher Update v3 abgeschlossen",
        btc=f"${btc:,.0f}/{signals['btc']}",
        fg=fg, ma200=f"${ma200:,.0f}",
        mnav=f"{mnav:.3f}x/{signals['mtplf']}",
        dxy=macro.get("dxy", {}).get("price"),
        sp500=macro.get("sp500", {}).get("price"),
        global_signal=global_signal
    )


if __name__ == "__main__":
    main()
