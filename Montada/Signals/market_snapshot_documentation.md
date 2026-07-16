# Market Data Categories and Notification Trigger Guide

## Overview

This document lists the market symbol categories from the snapshot you provided and explains how notifications are triggered.

It is written in a non-technical style for easy understanding.

This snapshot includes the full set of symbols from your payload, including base instruments and the variant forms used in the stream.

## Categories of symbols used

### Forex

All forex symbols included in the snapshot:

EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD, AUDUSD, NZDUSD, EURAUD, EURCAD, EURCHF,
EURGBP, EURJPY, EURNZD, GBPAUD, GBPCAD, GBPCHF, GBPJPY, AUDCAD, AUDCHF,
AUDNZD, NZDCAD, NZDCHF, CADCHF, CADJPY, CHFJPY, AUDNOK, AUDSEK, AUDSGD, CADSGD,
CHFNOK, CHFSGD, EURCZK, EURHUF, EURNOK, EURPLN, EURSEK, EURSGD, EURTRY, EURZAR,
GBPHUF, GBPMXN, GBPNOK, GBPPLN, GBPSEK, GBPSGD, NOKJPY, NOKSEK, SGDJPY, TRYJPY,
USDCNH, USDCZK, USDHKD, USDHUF, USDMXN, USDNOK, USDPLN, USDRON, USDSEK, USDSGD,
USDTHB, USDTRY, USDZAR, ZARJPY

### Shares

All share symbols included in the snapshot:

AAL, AAPL, ABNB, ADBE, AIG, AMZN, AXP, BA, BABA, BAC, BK, BKNG, BMRN, BMY, CAT,
CME, COST, CSCO, DAL, DELL, DIS, EBAY, FDX, GE, GM, GOOG, GOOGL, GPRO, GS, GT,
HD, HLT, HOG, HPQ, IBM, INTC, JNJ, JPM, KMI, KO, MA, MCD, MCO, MMM, MO, MRK,
MRVL, MS, MSFT, NFLX, NKE, NVDA, ORCL, PEP, PFE, PM, PYPL, QCOM, RACE, ROKU,
SBUX, SHOP, SONY, SPOT, SQ, TMUS, TSLA, UA, UAL, UBER, UPS, VALE, VZ, WFC,
WMT, XOM, YUM, ZM, ADSGn, AIRF, ALVG, BAYGn, BMWG, BNPP, CBKG, DAIGn, DANO,
DBKGn, DPWGn, EONGn, IBE, LHAG, LVMH, MAP, SAN, SIEGn, SOGN, TEF, TOTF, VOWG

### Metals

All metal symbols included in the snapshot:

GOLD, SILVER, XAUEUR, PLATINUM, PALLADIUM, COPPER

### Indices

All index symbols included in the snapshot:

US30, US100, US500, US2000, GER40, FRA40, NETH25, SPA35, EU50, SWI20, UK100,
JAP225, AUS200, HKIND, CHINAAS, USDIDX, DOW, NASDAQ, S&P, DAX, CAC, FTSE, AUS

### Commodity

All commodity symbols included in the snapshot:

SOYBEAN, COCOA, COFFEE

### Energy

All energy symbols included in the snapshot:

CL, USOIL, BRENT, UKOIL, NATGAS

### MENA Shares

All MENA share symbols included in the snapshot:

CBD, DEWA, DIB, DU, Emaar.Devel, Emaar.Propt, GULFNAV, NBD.Bank, Parkin, Salik,
Taaleem, Tecom.Group, AD.Aviation, AD.Insuranc, AD.Natl.Tak, AD.Ship, ADCB, ADIB,
ADNOC.Drill, ADNOC.Gas, ADNOC.Logis, Agthia.Grp, Alpha.Dhabi, Apex, Chimera, FAB.Bank,
Ghitha.Hold, IHC, Modon, NMDC, Palms.Sport, Pure.Health, RAK.Bank, RPH

## Notification trigger movements

Notifications are generated when a symbol moves enough from its opening level for the day.

### What causes a notification

- The system watches each symbol’s price movement from its opening level.
- A notification is sent only when the move is significant.
- Small, normal price swings do not create alerts.

### Main trigger levels

- Most symbols use a movement threshold of `0.5%`.
- Share symbols use a higher threshold of `1.0%`.

### Direction of movement

- A rising price is treated as an `up` move.
- A falling price is treated as a `down` move.

### Multiple trigger points

- If a symbol keeps moving past one threshold, it can trigger another notification later.
- This allows the same symbol to produce alerts for stronger moves.

### Avoiding repeated alerts

- The same threshold is not repeated unless the price reverses and resets.
- This keeps notifications clearer and prevents alert noise.

## Summary

This snapshot covers all configured symbols in the current market categories:
- Forex
- Shares
- Metals
- Indices
- Commodity
- Energy
- MENA Shares

Notifications are based on meaningful daily price movement, not every small tick, so alerts focus on stronger moves.