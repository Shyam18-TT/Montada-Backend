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