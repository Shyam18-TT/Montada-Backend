import requests
import re
import json

url = "https://www.tradays.com/en/economic-calendar"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}
r = requests.get(url, headers=headers)
print("Page length:", len(r.text))

# Let's check for any JSON-like structures that might be the state
state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', r.text, re.DOTALL)
if state_match:
    print("Found INITIAL_STATE!")
    try:
        data = json.loads(state_match.group(1))
        print(list(data.keys()))
    except:
        pass
else:
    print("No INITIAL_STATE found")
    
# check for anything containing 'events'
events_match = re.search(r'events\s*:\s*\[', r.text)
if events_match:
    print("Found 'events: [' pattern!")
