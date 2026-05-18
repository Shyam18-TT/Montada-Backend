import requests
import re
import json

def fetch_economic_calendar():
    url = "https://www.tradays.com/en/economic-calendar/widget?mode=2"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        # Find the Calendar.Data array assignment in the JavaScript
        match = re.search(r'Calendar\.Data\s*=\s*(\[.*?\]);', response.text, re.DOTALL)
        
        if match:
            json_data = match.group(1)
            try:
                # Parse the JSON string into a Python list
                calendar_events = json.loads(json_data)
                return calendar_events
            except json.JSONDecodeError as e:
                print(f"Error parsing JSON data: {e}")
                return None
        else:
            print("Could not find Calendar.Data in the page source.")
            return None
    else:
        print(f"Failed to fetch data. Status code: {response.status_code}")
        return None

if __name__ == "__main__":
    events = fetch_economic_calendar()
    
    if events:
        print(f"Successfully scraped {len(events)} events.")
        print("\nFirst 3 events:")
        for event in events[:3]:
            print(f"- {event.get('EventName')} ({event.get('CurrencyCode')})")
            print(f"  Importance: {event.get('Importance')}")
            print(f"  Actual: {event.get('ActualValue')} | Forecast: {event.get('ForecastValue')}")
            print(f"  Release Date (Timestamp): {event.get('ReleaseDate')}")
            print()
