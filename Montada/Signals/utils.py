MARKET_DATA_SYMBOLS_BY_CATEGORY = {
    "forex": [
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURAUD", "EURCAD", "EURCHF",
        "EURGBP", "EURJPY", "EURNZD", "GBPAUD", "GBPCAD", "GBPCHF", "GBPJPY", "AUDCAD", "AUDCHF", "AUDNZD",
        "NZDCAD", "NZDCHF", "CADCHF", "CADJPY", "CHFJPY", "AUDNOK", "AUDSEK", "AUDSGD", "CADSGD", "CHFNOK",
        "CHFSGD", "EURCZK", "EURHUF", "EURNOK", "EURPLN", "EURSEK", "EURSGD", "EURTRY", "EURZAR", "GBPHUF",
        "GBPMXN", "GBPNOK", "GBPPLN", "GBPSEK", "GBPSGD", "NOKJPY", "NOKSEK", "SGDJPY", "TRYJPY", "USDCNH",
        "USDCZK", "USDHKD", "USDHUF", "USDMXN", "USDNOK", "USDPLN", "USDRON", "USDSEK", "USDSGD", "USDTHB",
        "USDTRY", "USDZAR", "ZARJPY",
    ],
    "shares": [
        "AAL", "AAPL", "ABNB", "ADBE", "AIG", "AMZN", "AXP", "BA", "BABA", "BAC", "BK", "BKNG", "BMRN", "BMY",
        "CAT", "CME", "COST", "CSCO", "DAL", "DELL", "DIS", "EBAY", "FDX", "GE", "GM", "GOOG", "GOOGL", "GPRO",
        "GS", "GT", "HD", "HLT", "HOG", "HPQ", "IBM", "INTC", "JNJ", "JPM", "KMI", "KO", "MA", "MCD", "MCO",
        "MMM", "MO", "MRK", "MRVL", "MS", "MSFT", "NFLX", "NKE", "NVDA", "ORCL", "PEP", "PFE", "PM", "PYPL",
        "QCOM", "RACE", "ROKU", "SBUX", "SHOP", "SONY", "SPOT", "SQ", "TMUS", "TSLA", "UA", "UAL", "UBER", "UPS",
        "VALE", "VZ", "WFC", "WMT", "XOM", "YUM", "ZM", "ADSGn", "AIRF", "ALVG", "BAYGn", "BMWG", "BNPP", "CBKG",
        "DAIGn", "DANO", "DBKGn", "DPWGn", "EONGn", "IBE", "LHAG", "LVMH", "MAP", "SAN", "SIEGn", "SOGN", "TEF",
        "TOTF", "VOWG",
    ],
    "metals": ["GOLD", "SILVER", "XAUEUR", "PLATINUM", "PALLADIUM", "COPPER"],
    "indices": [
        "US30", "US100", "US500", "US2000", "GER40", "FRA40", "NETH25", "SPA35", "EU50", "SWI20", "UK100",
        "JAP225", "AUS200", "HKIND", "CHINAAS", "USDIDX", "DOW", "NASDAQ", "S&P", "DAX", "CAC", "FTSE", "AUS",
    ],
    "commodity": ["SOYBEAN", "COCOA", "COFFEE"],
    "energy": ["CL", "USOIL", "BRENT", "UKOIL", "NATGAS"],
    "menashares": [
        "CBD", "DEWA", "DIB", "DU", "Emaar.Devel", "Emaar.Propt", "GULFNAV", "NBD.Bank", "Parkin", "Salik",
        "Taaleem", "Tecom.Group", "AD.Aviation", "AD.Insuranc", "AD.Natl.Tak", "AD.Ship", "ADCB", "ADIB",
        "ADNOC.Drill", "ADNOC.Gas", "ADNOC.Logis", "Agthia.Grp", "Alpha.Dhabi", "Apex", "Chimera", "FAB.Bank",
        "Ghitha.Hold", "IHC", "Modon", "NMDC", "Palms.Sport", "Pure.Health", "RAK.Bank", "RPH",
    ],
}


ASSET_CLASS_DISPLAY_NAMES = {
    "forex": "Forex",
    "shares": "Shares",
    "metals": "Metals",
    "indices": "Indices",
    "commodity": "Commodity",
    "energy": "Energy",
    "menashares": "Mena Shares",
}


def seed_asset_classes_and_instruments():
    from .models import AssetClass, Instrument

    print("📦 Seeding AssetClass and Instrument data...\n")

    category_names = list(ASSET_CLASS_DISPLAY_NAMES.values())
    existing_assets = {
        asset.name: asset
        for asset in AssetClass.objects.filter(name__in=category_names)
    }

    new_assets = []
    for category_key, display_name in ASSET_CLASS_DISPLAY_NAMES.items():
        if display_name not in existing_assets:
            new_assets.append(
                AssetClass(
                    name=display_name,
                    description=f"Auto-seeded asset class for {category_key}.",
                )
            )

    if new_assets:
        AssetClass.objects.bulk_create(new_assets)
        existing_assets = {
            asset.name: asset
            for asset in AssetClass.objects.filter(name__in=category_names)
        }

    asset_ids = [asset.id for asset in existing_assets.values()]
    existing_pairs = set(
        Instrument.objects.filter(asset_class_id__in=asset_ids).values_list("asset_class_id", "symbol")
    )

    instrument_objects = []
    total_symbols = 0
    skipped_existing = 0

    for category_key, symbols in MARKET_DATA_SYMBOLS_BY_CATEGORY.items():
        asset_name = ASSET_CLASS_DISPLAY_NAMES[category_key]
        asset = existing_assets[asset_name]
        for symbol in symbols:
            total_symbols += 1
            pair = (asset.id, symbol)
            if pair in existing_pairs:
                skipped_existing += 1
                continue
            instrument_objects.append(Instrument(asset_class=asset, symbol=symbol))
            existing_pairs.add(pair)

    if instrument_objects:
        Instrument.objects.bulk_create(instrument_objects)

    print(f"✅ Asset classes available: {len(existing_assets)}")
    print(f"✅ Total symbols checked: {total_symbols}")
    print(f"✅ New instruments inserted: {len(instrument_objects)}")
    print(f"ℹ️ Existing instruments skipped: {skipped_existing}\n")


def seed_timeframes():
    from .models import Timeframe

    print("⏱ Seeding Timeframes...\n")

    data = [
        ("S1", "1 Second"),
        ("S15", "15 Seconds"),
        ("S30", "30 Seconds"),
        ("M1", "1 Minute"),
        ("M3", "3 Minutes"),
        ("M5", "5 Minutes"),
        ("M10", "10 Minutes"),
        ("M15", "15 Minutes"),
        ("M30", "30 Minutes"),
        ("H1", "1 Hour"),
        ("H2", "2 Hours"),
        ("H4", "4 Hours"),
        ("D1", "1 Day"),
        ("W1", "1 Week"),
        ("MN", "1 Month"),
    ]

    existing_codes = set(
        Timeframe.objects.filter(code__in=[code for code, _ in data]).values_list("code", flat=True)
    )
    objects = [
        Timeframe(code=code, name=name)
        for code, name in data
        if code not in existing_codes
    ]

    if objects:
        Timeframe.objects.bulk_create(objects)

    print(f"✅ Total timeframes checked: {len(data)}")
    print(f"✅ New timeframes inserted: {len(objects)}")
    print(f"ℹ️ Existing timeframes skipped: {len(data) - len(objects)} 🚀\n")