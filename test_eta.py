#!/usr/bin/env python3
import requests
import time

# Test OSRM API call directly
current_lat = 35.00244140625
current_lng = -118.93798828125
DEST_LAT, DEST_LNG = 37.4419, -121.9080  # Milpitas, CA

osrm_url = f"http://router.project-osrm.org/route/v1/driving/{current_lng},{current_lat};{DEST_LNG},{DEST_LAT}?overview=false&alternatives=false&steps=false"

print(f"Testing OSRM API with current location: {current_lat}, {current_lng}")
print(f"Destination: {DEST_LAT}, {DEST_LNG}")
print(f"OSRM URL: {osrm_url}")

try:
    print("Making OSRM request...")
    osrm_resp = requests.get(osrm_url, timeout=10)
    print(f"Response status: {osrm_resp.status_code}")
    
    if osrm_resp.status_code == 200:
        osrm_data = osrm_resp.json()
        print(f"OSRM response: {osrm_data}")
        
        if osrm_data.get('routes') and len(osrm_data['routes']) > 0:
            route = osrm_data['routes'][0]
            eta_hours = route['duration'] / 3600.0
            distance_miles = route['distance'] / 1609.34
            eta_utc = int(time.time() + eta_hours * 3600)
            
            print(f"✅ ETA calculation successful:")
            print(f"   Duration: {eta_hours:.1f} hours")
            print(f"   Distance: {distance_miles:.1f} miles")
            print(f"   ETA UTC: {eta_utc}")
        else:
            print("❌ No routes found in OSRM response")
    else:
        print(f"❌ OSRM API error: {osrm_resp.status_code}")
        print(f"Response: {osrm_resp.text}")
        
except Exception as e:
    print(f"❌ OSRM request failed: {e}")