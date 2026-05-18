import requests
import json

url = "https://www.tradays.com/en/economic-calendar/widget/content"
# Let's try to pass dateFrom and dateTo
headers = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded"
}
payload = {
    "dateFrom": "2026-05-18T00:00:00.000Z",
    "dateTo": "2026-06-07T00:00:00.000Z",
    "mode": 2
}
r = requests.post(url, headers=headers, data=payload)
if r.status_code == 200:
    print(r.text[:500])
    try:
        events = r.json()
        print("Fetched items:", len(events))
    except:
        pass
else:
    print("Failed with:", r.status_code)
