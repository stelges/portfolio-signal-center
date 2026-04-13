#!/usr/bin/env python3
"""
Portfolio Signal Center – KI Intelligence Updater
Läuft jeden Freitag um 18:00 UTC via GitHub Actions.
Aktualisiert alle data/*.json Dateien mit live Marktdaten + Claude AI Analyse.
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

# ── Konstanten (müssen manuell aktuell gehalten werden) ──────────────────────
BTC_ATH           = 109_000
SOL_ATH           = 294
TAO_CIRC          = 10_750_000
MTPLF_HOLD        = 40_177        # BTC Holdings Metaplanet
MTPLF_DEBT_USD_M  = 280           # Schulden in Mio USD
MTPLF_SHARES_M    = 1_459         # Aktien in Mio
MTPLF_PRICE_USD   = 1.90          # Aktienkurs OTC (manuell)

# ─────────────────────────────────────────────────────────────────────────────
# 1. MARKTDATEN ABRUFEN
# ─────────────────────────────────────────────────────────────────────────────

def fetch_crypto_prices() -> dict:
    """CoinGecko: BTC, SOL, TAO Preise."""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "bitcoin,solana,bittensor", "vs_currencies": "usd"}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        log.info(f"CoinGecko: BTC=${data.get('bitcoin',{}).get('usd')}, "
                 f"SOL=${data.get('solana',{}).get('usd')}, "
                 f"TAO=${data.get('bittensor',{}).get('usd')}")
        return data
    except Exception as e:
        log.error(f"CoinGecko Fehler: {e}")
        return {}


def fetch_btc_30d_change() -> float | None:
    """CoinGecko: BTC 30-Tage-Performance."""
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": 30, "interval": "daily"}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        prices = r.json().get("prices", [])
        if len(prices) > 1:
            start, end = prices[0][1], prices[-1][1]
            change = ((end - start) / start) * 100
            log.info(f"BTC 30d-Change: {change:.1f}%")
            return round(change, 2)
    except Exception as e:
        log.error(f"BTC 30d Fehler: {e}")
    return None


def fetch_fear_greed() -> int | None:
    """Alternative.me: Fear & Greed Index."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        r.raise_for_status()
        value = int(r.json()["data"][0]["value"])
        log.info(f"Fear & Greed: {value}/100")
        return value
    except Exception as e:
        log.error(f"Fear & Greed Fehler: {e}")
        return None


def fetch_nvda_data() -> dict:
    """Yahoo Finance: NVDA Kurs + Metriken."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/NVDA"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result", [{}])[0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        high52 = meta.get("fiftyTwoWeekHigh")
        low52  = meta.get("fiftyTwoWeekLow")
        log.info(f"NVDA: ${price}, 52W-High: ${high52}")
        return {"price_usd": round(price, 2) if price else None,
                "price_52w_high_usd": round(high52, 2) if high52 else None,
                "price_52w_low_usd": round(low52, 2) if low52 else None}
    except Exception as e:
        log.error(f"NVDA Yahoo Fehler: {e}")
        return {}


def fetch_news_rss(query: str, max_items: int = 5) -> list[str]:
    """
    Holt Headlines via Google News RSS (kein API-Key nötig).
    Gibt eine Liste von Strings zurück.
    """
    import xml.etree.ElementTree as ET
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:max_items]
        headlines = [item.find("title").text for item in items if item.find("title") is not None]
        log.info(f"News für '{query}': {len(headlines)} Headlines")
        return headlines
    except Exception as e:
        log.error(f"News RSS Fehler für '{query}': {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 2. mNAV BERECHNEN
# ─────────────────────────────────────────────────────────────────────────────

def calc_mnav(btc_price: float) -> float:
    ev  = MTPLF_SHARES_M * 1_000_000 * MTPLF_PRICE_USD + MTPLF_DEBT_USD_M * 1_000_000
    nav = MTPLF_HOLD * btc_price
    return ev / nav


# ─────────────────────────────────────────────────────────────────────────────
# 3. SIGNAL-LOGIK
# ─────────────────────────────────────────────────────────────────────────────

def signal_btc(btc: float, fg: int | None, p30: float | None) -> str:
    ath_dist = ((btc - BTC_ATH) / BTC_ATH) * 100
    if (fg is not None and fg < 20) or ath_dist < -55:
        return "BUY"
    if (fg is not None and fg > 78) or (p30 is not None and p30 > 70):
        return "CASH"
    if btc > 280_000:
        return "SELL"
    return "HOLD"


def signal_mnav(mnav: float) -> str:
    if mnav < 1.1:  return "BUY"
    if mnav < 1.8:  return "HOLD"
    if mnav < 2.5:  return "CASH"
    return "SELL"


def signal_sol(sol: float) -> str:
    ath_dist = ((sol - SOL_ATH) / SOL_ATH) * 100
    if ath_dist < -55: return "BUY"
    if sol > 400:      return "SELL"
    return "HOLD"


def signal_tao(tao: float) -> str:
    mcap = tao * TAO_CIRC
    if mcap < 2.5e9: return "BUY"
    if mcap < 8e9:   return "HOLD"
    if mcap > 20e9:  return "SELL"
    return "HOLD"


def signal_global(btc: float | None, fg: int | None) -> str:
    if btc is None: return "HOLD"
    ath_dist = ((btc - BTC_ATH) / BTC_ATH) * 100
    if (fg is not None and fg < 25) or ath_dist < -50: return "BUY"
    if fg is not None and fg > 75: return "CASH"
    return "HOLD"


# ─────────────────────────────────────────────────────────────────────────────
# 4. KAUFZONEN ANPASSEN (volatilitätsbasiert) + STRATEGY LOG
# ─────────────────────────────────────────────────────────────────────────────

def adjust_buy_zone_btc(btc: float, p30: float | None) -> dict:
    """
    Passt die BTC Kaufzone ±5% basierend auf 30d-Volatilität an.
    Dokumentiert Änderungen in strategy.log.
    """
    base_pct = -40.0  # Standard: -40% vom ATH
    note = ""

    if p30 is not None:
        vol_factor = abs(p30) / 30.0  # Normalisierte Volatilität
        if vol_factor > 1.5:  # Sehr hohe Volatilität
            adjusted_pct = base_pct - 5.0  # Kaufzone tiefer setzen (konservativer)
            note = f"Hohe Volatilität ({p30:+.1f}% 30d): Kaufzone auf {adjusted_pct:.1f}% vom ATH angepasst"
        elif vol_factor < 0.3:  # Sehr niedrige Volatilität (Seitwärtsmarkt)
            adjusted_pct = base_pct + 5.0  # Kaufzone höher setzen (aggressiver)
            note = f"Niedrige Volatilität ({p30:+.1f}% 30d): Kaufzone auf {adjusted_pct:.1f}% vom ATH angepasst"
        else:
            adjusted_pct = base_pct
            note = f"Normale Volatilität ({p30:+.1f}% 30d): Standard-Kaufzone {base_pct:.1f}% beibehalten"
    else:
        adjusted_pct = base_pct
        note = "Keine 30d-Daten: Standard-Kaufzone beibehalten"

    threshold_usd = round(BTC_ATH * (1 + adjusted_pct / 100))

    if note:
        log_strategy(note, asset="BTC", zone_pct=adjusted_pct, threshold_usd=threshold_usd)

    return {
        "buy": {"label": f"F&G<30 oder {adjusted_pct:.0f}% vom ATH", "threshold_usd": threshold_usd},
        "hold": {"label": f"{adjusted_pct:.0f}% bis -20% vom ATH"},
        "sell": {"label": "BTC > $300.000", "threshold_usd": 300_000}
    }


def log_strategy(message: str, **kwargs):
    """Schreibt Strategie-Anpassungen in strategy.log."""
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    entry = f"[{ts}] {message}"
    if kwargs:
        details = " | ".join(f"{k}={v}" for k, v in kwargs.items())
        entry += f" ({details})"
    log.info(f"STRATEGY LOG: {entry}")
    with open(STRATEGY_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# 5. CLAUDE KI ANALYSE
# ─────────────────────────────────────────────────────────────────────────────

def analyze_with_claude(client: anthropic.Anthropic, asset_name: str, context: str) -> dict:
    """
    Ruft Claude claude-3-5-haiku-20241022 auf, um eine strukturierte Analyse zu erstellen.
    Gibt dict mit summary, bull_case, bear_case, key_catalysts zurück.
    """
    prompt = f"""Du bist ein präziser Finanzanalyst für ein privates Portfolio-Dashboard.
Analysiere folgende Daten zu {asset_name} und antworte ausschließlich auf Deutsch.

KONTEXT:
{context}

Erstelle eine JSON-Analyse mit GENAU diesen Feldern:
{{
  "summary": "2-3 Sätze zur aktuellen Lage. Sachlich, keine Übertreibungen.",
  "bull_case": "1-2 Sätze: Was müsste passieren damit der Kurs steigt?",
  "bear_case": "1-2 Sätze: Was sind die größten Risiken?",
  "key_catalysts": ["Katalysator 1", "Katalysator 2", "Katalysator 3"]
}}

Antworte NUR mit dem JSON-Objekt, ohne Markdown-Backticks, ohne Erklärungen."""

    try:
        response = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # JSON parsen
        result = json.loads(raw)
        log.info(f"Claude Analyse für {asset_name}: OK")
        return result
    except json.JSONDecodeError as e:
        log.error(f"Claude JSON Parse Fehler für {asset_name}: {e}\nRaw: {raw[:200]}")
        return {"summary": "Analyse temporär nicht verfügbar.", "bull_case": "", "bear_case": "", "key_catalysts": []}
    except Exception as e:
        log.error(f"Claude API Fehler für {asset_name}: {e}")
        return {"summary": "Analyse temporär nicht verfügbar.", "bull_case": "", "bear_case": "", "key_catalysts": []}


def create_weekly_briefing(client: anthropic.Anthropic, market_data: dict) -> str:
    """Erstellt ein kurzes deutsches Wochenbriefing für das Portfolio."""
    btc = market_data.get("btc")
    fg  = market_data.get("fg")
    sol = market_data.get("sol")
    tao = market_data.get("tao")
    p30 = market_data.get("p30")
    mnav = market_data.get("mnav")

    context = f"""
Aktuelle Portfolio-Daten:
- Bitcoin: ${btc:,.0f} (ATH-Distanz: {((btc-BTC_ATH)/BTC_ATH*100):.1f}%, 30d: {p30:+.1f}% falls verfügbar)
- Fear & Greed: {fg}/100
- Metaplanet mNAV: {mnav:.3f}x
- Solana: ${sol:.1f}
- Bittensor: ${tao:.0f}
""" if all(v is not None for v in [btc, fg, sol, tao, mnav]) else "Marktdaten teilweise nicht verfügbar."

    prompt = f"""Du bist Analyst für ein 7-Jahres-Portfolio mit dem Ziel Finanzielle Freiheit 2033.
Portfolio: 38% BTC, 20% Metaplanet, 15% NVDA, 14% SOL, 8% TAO.

{context}

Schreibe ein Wochenbriefing in 2-3 Sätzen auf Deutsch. Nenne die wichtigste Handlungsempfehlung für diese Woche.
Kurz, präzise, kein Blabla. Kein "Ich" - direkte Ansprache."""

    try:
        response = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.error(f"Briefing Fehler: {e}")
        return "Wochenbriefing temporär nicht verfügbar."


# ─────────────────────────────────────────────────────────────────────────────
# 6. JSON DATEIEN SCHREIBEN
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# 7. MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Portfolio Signal Center · KI-Update startet")
    log.info(f"Datum: {TODAY}")
    log.info("=" * 60)

    # API Key prüfen
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY nicht gefunden! Setze die Environment-Variable.")
        raise EnvironmentError("ANTHROPIC_API_KEY fehlt")

    client = anthropic.Anthropic(api_key=api_key)

    # ── Marktdaten abrufen ──
    log.info("Schritt 1: Marktdaten abrufen...")
    prices = fetch_crypto_prices()
    time.sleep(2)  # Rate-Limiting
    btc  = prices.get("bitcoin",  {}).get("usd")
    sol  = prices.get("solana",   {}).get("usd")
    tao  = prices.get("bittensor",{}).get("usd")
    fg   = fetch_fear_greed()
    time.sleep(2)
    p30  = fetch_btc_30d_change()
    time.sleep(2)
    nvda = fetch_nvda_data()
    time.sleep(2)

    # mNAV berechnen
    mnav = calc_mnav(btc) if btc else None

    # Kaufzonen anpassen
    btc_zones = adjust_buy_zone_btc(btc, p30) if btc else None

    # ── News abrufen ──
    log.info("Schritt 2: News abrufen...")
    nvda_news = fetch_news_rss("Nvidia NVDA stock AI earnings 2026", max_items=5)
    time.sleep(2)
    mtplf_news = fetch_news_rss("Metaplanet Bitcoin Japan stock 3350", max_items=5)
    time.sleep(2)
    btc_news  = fetch_news_rss("Bitcoin BTC price market 2026", max_items=4)
    time.sleep(2)
    sol_news  = fetch_news_rss("Solana SOL crypto Firedancer ETF 2026", max_items=4)
    time.sleep(2)
    tao_news  = fetch_news_rss("Bittensor TAO AI crypto Grayscale", max_items=4)
    time.sleep(2)

    # ── KI Analysen ──
    log.info("Schritt 3: KI-Analysen erstellen (Claude)...")

    # BTC
    btc_context = f"""
Bitcoin Preis: ${btc:,.0f}
ATH: ${BTC_ATH:,} | ATH-Distanz: {((btc-BTC_ATH)/BTC_ATH*100):.1f}%
Fear & Greed: {fg}/100
30d-Performance: {p30:+.1f}%
Nächstes Halving: April 2028
Kaufzone (angepasst): {btc_zones['buy']['threshold_usd']:,}$ ({btc_zones['buy']['label']})
Signal: {signal_btc(btc, fg, p30)}
Aktuelle Headlines:
""" + "\n".join(f"- {h}" for h in btc_news[:4]) if btc else "Daten nicht verfügbar"

    btc_ai = analyze_with_claude(client, "Bitcoin (BTC)", btc_context)
    time.sleep(3)

    # Metaplanet
    mtp_context = f"""
Metaplanet mNAV: {mnav:.4f}x
BTC Holdings: {MTPLF_HOLD:,} BTC
BTC-NAV: ${(MTPLF_HOLD*btc/1e9):.2f}B
Schulden: ${MTPLF_DEBT_USD_M}M
Signal: {signal_mnav(mnav)}
Kaufzone: mNAV < 1.1x (aktuell {'GÜNSTIGER EINSTIEG' if mnav < 1.1 else 'Normale Bewertung'})
Aktuelle Headlines:
""" + "\n".join(f"- {h}" for h in mtplf_news[:4]) if mnav else "mNAV-Daten nicht verfügbar"

    mtp_ai = analyze_with_claude(client, "Metaplanet (MTPLF)", mtp_context)
    time.sleep(3)

    # NVDA
    nvda_price = nvda.get("price_usd")
    nvda_context = f"""
Nvidia Preis: ${nvda_price}
52W-High: ${nvda.get('price_52w_high_usd')}
52W-Low: ${nvda.get('price_52w_low_usd')}
KI-Capex-Trend: Wachsend (alle Hyperscaler erhöhen Data-Center-Ausgaben)
Nächste Earnings: Mai 2026
Aktuelle Headlines:
""" + "\n".join(f"- {h}" for h in nvda_news[:5])

    nvda_ai = analyze_with_claude(client, "Nvidia (NVDA)", nvda_context)
    time.sleep(3)

    # SOL
    sol_ath_dist = ((sol - SOL_ATH) / SOL_ATH * 100) if sol else None
    sol_context = f"""
Solana Preis: ${sol:.1f}
ATH: ${SOL_ATH} | ATH-Distanz: {sol_ath_dist:.1f}%
Network Revenue: $2.85B/Jahr
ETF-Status: In Prüfung
Firedancer Upgrade: 2026
Signal: {signal_sol(sol)}
Aktuelle Headlines:
""" + "\n".join(f"- {h}" for h in sol_news[:4]) if sol else "SOL-Daten nicht verfügbar"

    sol_ai = analyze_with_claude(client, "Solana (SOL)", sol_context)
    time.sleep(3)

    # TAO
    tao_mcap = (tao * TAO_CIRC / 1e9) if tao else None
    tao_context = f"""
Bittensor Preis: ${tao:.0f}
Market Cap: ${tao_mcap:.2f}B
Max Supply: 21M TAO (Bitcoin-Mechanik)
Halving: Dezember 2025 (abgeschlossen)
Grayscale ETF: Beantragt
Signal: {signal_tao(tao)}
Aktuelle Headlines:
""" + "\n".join(f"- {h}" for h in tao_news[:4]) if tao else "TAO-Daten nicht verfügbar"

    tao_ai = analyze_with_claude(client, "Bittensor (TAO)", tao_context)
    time.sleep(3)

    # Wochenbriefing
    market_data = {"btc": btc, "fg": fg, "sol": sol, "tao": tao, "p30": p30, "mnav": mnav}
    briefing = create_weekly_briefing(client, market_data)

    # ── JSON Dateien aktualisieren ──
    log.info("Schritt 4: JSON Dateien schreiben...")

    # portfolio.json
    portfolio = load_json("portfolio.json")
    portfolio.update({
        "last_updated": NOW_UTC,
        "updated_by": "update_intelligence.py (GitHub Actions)",
        "global_signal": signal_global(btc, fg),
        "global_summary": {
            "BUY": "Akkumulieren – günstige Phase aktiv",
            "HOLD": "Normales DCA fortführen",
            "CASH": "Vorsicht – Cash aufbauen"
        }.get(signal_global(btc, fg), "HOLD"),
        "fear_greed": fg,
        "btc_price_usd": btc,
        "btc_ath_dist_pct": round(((btc - BTC_ATH) / BTC_ATH * 100), 1) if btc else None,
        "btc_30d_change_pct": p30,
        "next_update": (datetime.date.today() + datetime.timedelta(days=7)).isoformat() + "T18:00:00Z",
        "ai_weekly_briefing": briefing,
        "market_regime": "bullish" if fg and fg > 65 else "fearful" if fg and fg < 30 else "neutral"
    })
    save_json("portfolio.json", portfolio)

    # btc.json
    btc_data = load_json("btc.json")
    btc_data.update({
        "signal": signal_btc(btc, fg, p30) if btc else "HOLD",
        "price_usd": btc,
        "ath_dist_pct": round(((btc - BTC_ATH) / BTC_ATH * 100), 1) if btc else None,
        "change_30d_pct": p30,
        "zones": btc_zones or btc_data.get("zones"),
        "ai_analysis": {
            **btc_ai,
            "news_headlines": btc_news,
            "confidence": "medium",
            "updated": TODAY
        }
    })
    save_json("btc.json", btc_data)

    # mtplf.json
    mtp_data = load_json("mtplf.json")
    mtp_data.update({
        "signal": signal_mnav(mnav) if mnav else "HOLD",
        "mnav": round(mnav, 4) if mnav else None,
        "btc_nav_usd_billions": round(MTPLF_HOLD * btc / 1e9, 2) if btc else None,
        "ai_analysis": {
            **mtp_ai,
            "mnav_assessment": (
                "Kaufzone: Bitcoin mit Rabatt" if mnav and mnav < 1.1
                else "Fair bewertet" if mnav and mnav < 1.8
                else "Aufpreis – kein Neukauf" if mnav
                else None
            ),
            "news_headlines": mtplf_news,
            "confidence": "high",
            "updated": TODAY
        }
    })
    save_json("mtplf.json", mtp_data)

    # nvda.json
    nvda_data = load_json("nvda.json")
    nvda_data.update({
        "signal": "BUY" if nvda_price and nvda.get("price_52w_high_usd") and
                  nvda_price < nvda["price_52w_high_usd"] * 0.75 else "HOLD",
        "price_usd": nvda.get("price_usd"),
        "price_52w_high_usd": nvda.get("price_52w_high_usd"),
        "price_52w_low_usd": nvda.get("price_52w_low_usd"),
        "ai_analysis": {
            **nvda_ai,
            "ai_capex_trend": "Wachsend",
            "news_headlines": nvda_news,
            "confidence": "medium",
            "updated": TODAY
        }
    })
    save_json("nvda.json", nvda_data)

    # sol.json
    sol_data = load_json("sol.json")
    sol_data.update({
        "signal": signal_sol(sol) if sol else "HOLD",
        "price_usd": sol,
        "ath_dist_pct": round(sol_ath_dist, 1) if sol_ath_dist else None,
        "ai_analysis": {
            **sol_ai,
            "news_headlines": sol_news,
            "confidence": "medium",
            "updated": TODAY
        }
    })
    save_json("sol.json", sol_data)

    # tao.json
    tao_data = load_json("tao.json")
    tao_data.update({
        "signal": signal_tao(tao) if tao else "HOLD",
        "price_usd": tao,
        "market_cap_usd": round(tao * TAO_CIRC) if tao else None,
        "ai_analysis": {
            **tao_ai,
            "news_headlines": tao_news,
            "confidence": "medium",
            "updated": TODAY
        }
    })
    save_json("tao.json", tao_data)

    # ── Zusammenfassung ──
    log.info("=" * 60)
    log.info("Update abgeschlossen!")
    log.info(f"Globales Signal: {signal_global(btc, fg)}")
    log.info(f"BTC: ${btc:,.0f} | F&G: {fg} | mNAV: {mnav:.3f}x")
    log.info(f"SOL: ${sol:.1f} | TAO: ${tao:.0f}")
    log.info(f"Nächster Update: {(datetime.date.today() + datetime.timedelta(days=7)).isoformat()}")
    log.info("=" * 60)

    log_strategy(
        f"Wöchentlicher Update: BTC=${btc:,.0f}, F&G={fg}, mNAV={mnav:.3f}x, Signal={signal_global(btc, fg)}",
        btc_signal=signal_btc(btc, fg, p30),
        mnav_signal=signal_mnav(mnav),
        global_signal=signal_global(btc, fg)
    )


if __name__ == "__main__":
    main()
