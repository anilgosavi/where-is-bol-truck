from flask import Flask, render_template_string
import threading
import time
import math
import os
import json
import redis

# Initialize Redis connection (use Render's internal URL or REDIS_URL env var)
REDIS_URL = os.environ.get("REDIS_INTERNAL_URL") or os.environ.get("REDIS_URL")
redis_client = redis.from_url(REDIS_URL) if REDIS_URL else None
from datetime import datetime, date, timedelta

import requests

app = Flask(__name__)

REDIS_NOT_CONFIGURED_MSG = "Redis not configured"
# Debug route to list all Redis keys (for development only!)
@app.route('/debug/redis-keys')
def debug_redis_keys():
    if not redis_client:
        return REDIS_NOT_CONFIGURED_MSG, 500
    try:
        keys = [k.decode() if isinstance(k, bytes) else k for k in redis_client.keys('*')]
        table = []
        for key in keys:
            try:
                value = redis_client.get(key)
                # Try to decode as utf-8 and pretty-print JSON if possible
                if value is not None:
                    try:
                        value_str = value.decode() if isinstance(value, bytes) else str(value)
                        if value_str.startswith('{') or value_str.startswith('['):
                            value_str = json.dumps(json.loads(value_str), indent=2)
                    except Exception:
                        value_str = str(value)
                else:
                    value_str = None
                table.append({"key": key, "value": value_str})
            except Exception as e:
                table.append({"key": key, "value": f"Error: {e}"})
        return {"data": table}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/debug/load-daily-stats/<date_str>')
def debug_load_daily_stats(date_str):
    if not redis_client:
        return REDIS_NOT_CONFIGURED_MSG, 500
    file_path = get_daily_file_path(date_str)
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}, 404
    try:
        with open(file_path, 'r') as f:
            file_data = json.load(f)
        redis_client.set(f"truck:daily:{date_str}", json.dumps(file_data))
        return {"status": "success", "message": f"Loaded {file_path} into Redis as truck:daily:{date_str}"}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/debug/load-all-daily-stats')
def debug_load_all_daily_stats():
    if not redis_client:
        return REDIS_NOT_CONFIGURED_MSG, 500
    daily_stats_path = os.path.join('truck_data', 'daily_stats.json')
    if not os.path.exists(daily_stats_path):
        return {"error": f"File not found: {daily_stats_path}"}, 404
    try:
        with open(daily_stats_path, 'r') as f:
            all_stats = json.load(f)
        loaded = []
        for date_str, stats in all_stats.items():
            redis_client.set(f"truck:daily:{date_str}", json.dumps(stats))
            loaded.append(date_str)
        return {"status": "success", "loaded_dates": loaded, "message": f"Loaded {len(loaded)} days from daily_stats.json into Redis."}
    except Exception as e:
        return {"error": str(e)}, 500

# Data file paths
DATA_DIR = "truck_data"
HISTORY_FILE = os.path.join(DATA_DIR, "location_history.json")
DAILY_STATS_FILE = os.path.join(DATA_DIR, "daily_stats.json")
LAST_LOCATION_FILE = os.path.join(DATA_DIR, "last_location.json")
DAILY_FILES_DIR = os.path.join(DATA_DIR, "daily")

# Maximum realistic speed (90 mph)
MAX_REALISTIC_SPEED = 90

def process_fetched_location(location):
    """Process a fetched location dict and update internal state; returns (success, has_moved)."""
    try:
        current_time = time.time()
        new_lat = location['latitude']
        new_lng = location['longitude']

        # Detect movement relative to last stored driver position
        has_moved, distance_moved = detect_last_position_movement(new_lat, new_lng)

        # Append the new GPS point and manage history size
        append_history_point(new_lat, new_lng, current_time)

        # Persist history periodically and trim to 1 hour
        periodic_persist_history()
        trim_history_hours(1)

        # Update daily stats using the previous segment when appropriate
        handle_segment_update_for_new_point(new_lat, new_lng, current_time)

        # Compute display speed and movement flag
        current_speed = calculate_vehicle_icon_speed()
        is_moving_segment = compute_segment_moving_flag_for_new_point(new_lat, new_lng, current_time)
        update_travel_time(current_time, is_moving_segment)

        # NEW: Update daily file with per-minute location data
        update_daily_tracking(new_lat, new_lng, current_time, current_speed, is_moving_segment)

        # Update driver state and persist last location
        update_driver_state(new_lat, new_lng, current_time, current_speed)
        save_last_location(new_lat, new_lng, current_time)

        status = 'moving' if current_speed > 0 else 'stopped'
        movement_info = f" (moved {distance_moved*5280:.0f}ft)" if has_moved and distance_moved > 0 else ""
        print(f"✅ Real location updated: {new_lat}, {new_lng} at {location.get('time', '')} - Speed: {round(current_speed)} mph - Status: {status}{movement_info}")
        return True, has_moved
    except Exception as e:
        print(f"❌ Error processing fetched location: {e}")
        return False, False


def update_daily_tracking(lat, lng, timestamp, speed, is_moving):
    """Update the current day's tracking file with new location data"""
    try:
        # Get or create today's daily data
        today = date.today().isoformat()
        daily_data = get_current_day_data()
        
        # Add minute location data
        daily_data = add_minute_location(daily_data, timestamp, lat, lng, speed, is_moving)
        
        # Update distance calculation
        daily_data['total_distance_miles'] = calculate_daily_distance(daily_data)
        
        # Update movement tracking
        if is_moving:
            if daily_data['summary']['first_movement_time'] is None:
                daily_data['summary']['first_movement_time'] = timestamp
            daily_data['summary']['last_movement_time'] = timestamp
        
        # Save updated data
        save_daily_file(today, daily_data)
        
        return daily_data
        
    except Exception as e:
        print(f"❌ Error updating daily tracking: {e}")
        return None

def calculate_startup_speed(last_location, current_lat, current_lng, current_time):
    """Calculate initial speed using last saved location and current location"""
    if not last_location:
        return 0
    
    # Calculate distance between last known location and current location
    distance_miles = haversine_distance(
        last_location["latitude"], last_location["longitude"],
        current_lat, current_lng
    )
    
    # Calculate time difference (in hours)
    time_diff_hours = (current_time - last_location["timestamp"]) / 3600
    
    if time_diff_hours > 0 and distance_miles > 0.001:  # More than ~5 feet
        calculated_speed = distance_miles / time_diff_hours
        
        # Only return speed if realistic and time gap is reasonable (less than 8 hours)
        if is_realistic_speed(calculated_speed) and time_diff_hours <= 8:
            print(f"📍 Calculated startup speed: {calculated_speed:.1f} mph (based on {distance_miles:.2f} miles over {time_diff_hours:.2f} hours)")
            return calculated_speed
        else:
            if time_diff_hours > 8:
                print(f"⏰ Time gap too large ({time_diff_hours:.2f} hours) - resetting speed to 0")
            else:
                print(f"⚠️ Unrealistic startup speed: {calculated_speed:.1f} mph - resetting to 0")
    
    return 0

def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate the distance between two GPS coordinates in miles"""
    R = 3959  # Earth's radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2) * math.sin(dlat/2) + 
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * 
         math.sin(dlon/2) * math.sin(dlon/2))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def is_realistic_speed(speed_mph):
    """Check if calculated speed is realistic (not GPS error)"""
    return 0 <= speed_mph <= MAX_REALISTIC_SPEED


def load_historical_data():
    """Load historical GPS points from disk (list of dicts)."""
    if redis_client:
        try:
            data = redis_client.get("truck:location_history")
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                data = json.loads(data)
                return data if isinstance(data, list) else []
        except Exception as e:
            print(f"❌ Error loading historical data from Redis: {e}")
    # Fallback to file if Redis is not available
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception as e:
        print(f"❌ Error loading historical data: {e}")
    return []


def save_historical_data(history):
    """Persist historical GPS points to disk."""
    if redis_client:
        try:
            redis_client.set("truck:location_history", json.dumps(history))
            return
        except Exception as e:
            print(f"❌ Error saving historical data to Redis: {e}")
    # Fallback to file if Redis is not available
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)
    except Exception as e:
        print(f"❌ Error saving historical data: {e}")


def load_daily_stats():
    """Load daily stats dict from disk."""
    if redis_client:
        try:
            data = redis_client.get("truck:daily_stats")
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                data = json.loads(data)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"❌ Error loading daily stats from Redis: {e}")
    # Fallback to file if Redis is not available
    try:
        if os.path.exists(DAILY_STATS_FILE):
            with open(DAILY_STATS_FILE, 'r') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"❌ Error loading daily stats: {e}")
    return {}


def save_daily_stats(stats):
    """Persist daily stats to disk."""
    if redis_client:
        try:
            redis_client.set("truck:daily_stats", json.dumps(stats))
            return
        except Exception as e:
            print(f"❌ Error saving daily stats to Redis: {e}")
    # Fallback to file if Redis is not available
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DAILY_STATS_FILE, 'w') as f:
            json.dump(stats, f)
    except Exception as e:
        print(f"❌ Error saving daily stats: {e}")


def load_last_location():
    """Load last saved location from disk (dict with latitude, longitude, timestamp)."""
    if redis_client:
        try:
            data = redis_client.get("truck:last_location")
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                return json.loads(data)
        except Exception as e:
            print(f"❌ Error loading last location from Redis: {e}")
        return None
    # Fallback to file if Redis is not available
    try:
        if os.path.exists(LAST_LOCATION_FILE):
            with open(LAST_LOCATION_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"❌ Error loading last location: {e}")
    return None


def save_last_location(lat, lng, ts):
    """Persist the last known location to disk."""
    if redis_client:
        try:
            redis_client.set("truck:last_location", json.dumps({"latitude": lat, "longitude": lng, "timestamp": ts}))
            return
        except Exception as e:
            print(f"❌ Error saving last location to Redis: {e}")
    # Fallback to file if Redis is not available
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_LOCATION_FILE, 'w') as f:
            json.dump({"latitude": lat, "longitude": lng, "timestamp": ts}, f)
    except Exception as e:
        print(f"❌ Error saving last location: {e}")


def get_daily_file_path(date_str):
    """Get the path for a daily data file"""
    return os.path.join(DAILY_FILES_DIR, f"daily_{date_str}.json")


def create_empty_daily_file(date_str, start_time, start_lat, start_lng):
    """Create a new daily tracking file"""
    daily_data = {
        "date": date_str,
        "start_time": start_time,
        "end_time": None,
        "start_location": {
            "latitude": start_lat,
            "longitude": start_lng,
            "timestamp": start_time
        },
        "end_location": None,
        "total_distance_miles": 0.0,
        "total_travel_time_seconds": 0,
        "minute_locations": [],
        "summary": {
            "first_movement_time": None,
            "last_movement_time": None,
            "moving_time_seconds": 0,
            "stopped_time_seconds": 0
        }
    }
    
    if redis_client:
        try:
            redis_client.set(f"truck:daily:{date_str}", json.dumps(daily_data))
            print(f"📁 Created new daily file in Redis: truck:daily:{date_str}")
            return daily_data
        except Exception as e:
            print(f"❌ Error creating daily file in Redis: {e}")
    # Fallback to file if Redis is not available
    os.makedirs(DAILY_FILES_DIR, exist_ok=True)
    file_path = get_daily_file_path(date_str)
    with open(file_path, 'w') as f:
        json.dump(daily_data, f, indent=2)
    print(f"📁 Created new daily file: {file_path}")
    return daily_data


def load_daily_file(date_str):
    """Load daily tracking data for a specific date"""
    if redis_client:
        try:
            data = redis_client.get(f"truck:daily:{date_str}")
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                data = json.loads(data)
                # Migrate old format to new format if needed
                if 'samples_by_minute' in data and 'minute_locations' not in data:
                    data = migrate_old_daily_format(data)
                    save_daily_file(date_str, data)  # Save migrated format
                    print(f"🔄 Migrated daily file {date_str} to new format (Redis)")
                return data
        except Exception as e:
            print(f"❌ Error loading daily file from Redis: {e}")
    # Fallback to file if Redis is not available
    file_path = get_daily_file_path(date_str)
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                data = json.load(f)
            # Migrate old format to new format if needed
            if 'samples_by_minute' in data and 'minute_locations' not in data:
                data = migrate_old_daily_format(data)
                save_daily_file(date_str, data)  # Save migrated format
                print(f"🔄 Migrated daily file {date_str} to new format")
            return data
    except Exception as e:
        print(f"❌ Error loading daily file {file_path}: {e}")
    return None

def migrate_old_daily_format(old_data):
    """Migrate old daily file format to new format"""
    # Create new format structure
    new_data = {
        "date": old_data.get("date"),
        "start_time": old_data.get("start_time"),
        "end_time": old_data.get("end_time"),
        "start_location": old_data.get("start_location"),
        "end_location": old_data.get("end_location"),
        "total_distance_miles": old_data.get("total_miles", 0),
        "total_travel_time_seconds": old_data.get("total_travel_time", 0),
        "minute_locations": [],
        "summary": {
            "first_movement_time": None,
            "last_movement_time": None,
            "moving_time_seconds": 0,
            "stopped_time_seconds": 0
        }
    }
    
    # Convert samples_by_minute to minute_locations
    if 'samples_by_minute' in old_data:
        for timestamp_str, sample in old_data['samples_by_minute'].items():
            timestamp = int(timestamp_str)
            
            # Calculate minute since start of day
            if new_data['start_time']:
                start_of_day = datetime.fromtimestamp(new_data['start_time']).replace(hour=0, minute=0, second=0, microsecond=0)
                current_time = datetime.fromtimestamp(timestamp)
                minutes_since_start = int((current_time - start_of_day).total_seconds() / 60)
                
                minute_entry = {
                    "minute": minutes_since_start,
                    "timestamp": timestamp,
                    "latitude": sample.get("latitude"),
                    "longitude": sample.get("longitude"),
                    "speed": sample.get("speed", 0),
                    "moving": sample.get("speed", 0) > 1
                }
                new_data['minute_locations'].append(minute_entry)
    
    return new_data


def save_daily_file(date_str, daily_data):
    """Save daily tracking data"""
    try:
        os.makedirs(DAILY_FILES_DIR, exist_ok=True)
        file_path = get_daily_file_path(date_str)
        with open(file_path, 'w') as f:
            json.dump(daily_data, f, indent=2)
    except Exception as e:
        print(f"❌ Error saving daily file: {e}")


def add_minute_location(daily_data, timestamp, lat, lng, speed, is_moving):
    """Add a per-minute location entry to daily data"""

    # Only add to cache if the truck is moving
    if not is_moving:
        return daily_data

    # Only add if location or timestamp is different from last entry
    if daily_data['minute_locations']:
        last = daily_data['minute_locations'][-1]
        if (
            last['latitude'] == lat and
            last['longitude'] == lng and
            last['timestamp'] == timestamp
        ):
            return daily_data

    # Calculate minutes since start of day
    start_of_day = datetime.fromtimestamp(daily_data['start_time']).replace(hour=0, minute=0, second=0, microsecond=0)
    current_time = datetime.fromtimestamp(timestamp)
    minutes_since_start = int((current_time - start_of_day).total_seconds() / 60)

    # Check if we already have this minute
    existing_minute = None
    for loc in daily_data['minute_locations']:
        if loc['minute'] == minutes_since_start:
            existing_minute = loc
            break

    if existing_minute:
        # Update existing minute with latest data
        existing_minute.update({
            "timestamp": timestamp,
            "latitude": lat,
            "longitude": lng,
            "speed": speed,
            "moving": is_moving
        })
    else:
        # Add new minute entry
        minute_entry = {
            "minute": minutes_since_start,
            "timestamp": timestamp,
            "latitude": lat,
            "longitude": lng,
            "speed": speed,
            "moving": is_moving
        }
        daily_data['minute_locations'].append(minute_entry)

        # Keep sorted by minute
        daily_data['minute_locations'].sort(key=lambda x: x['minute'])

    # Update end location and time
    daily_data['end_location'] = {
        "latitude": lat,
        "longitude": lng,
        "timestamp": timestamp
    }
    daily_data['end_time'] = timestamp
    
    return daily_data


def calculate_daily_distance(daily_data):
    """Calculate total distance from minute locations"""
    locations = daily_data.get('minute_locations')
    if not locations or len(locations) < 2:
        return 0.0
    total_distance = 0.0
    for i in range(1, len(locations)):
        prev_loc = locations[i-1]
        curr_loc = locations[i]
        distance = haversine_distance(
            prev_loc['latitude'], prev_loc['longitude'],
            curr_loc['latitude'], curr_loc['longitude']
        )
        total_distance += distance
    return total_distance


def get_current_day_data():
    """Get or create today's daily tracking data"""
    today = date.today().isoformat()
    daily_data = load_daily_file(today)

    # Migrate today's file data to Redis if Redis is available and cache is empty
    if redis_client and daily_data is None:
        file_path = get_daily_file_path(today)
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    file_data = json.load(f)
                redis_client.set(f"truck:daily:{today}", json.dumps(file_data))
                print(f"🔄 Migrated today's file data to Redis for {today}")
                daily_data = file_data
            except Exception as e:
                print(f"❌ Error migrating today's file data to Redis: {e}")

    if daily_data is None:
        # Create new file with current location as start
        current_time = time.time()
        current_lat = location_data["driver"]["latitude"]
        current_lng = location_data["driver"]["longitude"]
        daily_data = create_empty_daily_file(today, current_time, current_lat, current_lng)
        print(f"🆕 Started new day tracking for {today}")

    return daily_data


def get_previous_day_stats():
    """Calculate previous day travel stats"""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    daily_data = load_daily_file(yesterday)
    
    if daily_data is None:
        return None
    
    # Calculate distance if not stored
    if daily_data.get('total_distance_miles', 0) == 0:
        daily_data['total_distance_miles'] = calculate_daily_distance(daily_data)
        save_daily_file(yesterday, daily_data)
    
    # Calculate travel time from start/end times
    travel_time_hours = 0
    if daily_data.get('start_time') and daily_data.get('end_time'):
        travel_time_hours = (daily_data['end_time'] - daily_data['start_time']) / 3600
    
    return {
        'date': yesterday,  # Add date field for API response
        'distance_miles': daily_data.get('total_distance_miles', 0),
        'travel_time_hours': travel_time_hours
    }

def initialize_daily_files_on_startup():
    """Initialize daily file system on app startup"""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    
    # Calculate previous day stats if not done
    prev_data = load_daily_file(yesterday)
    if prev_data and prev_data.get('total_distance_miles', 0) == 0:
        prev_data['total_distance_miles'] = calculate_daily_distance(prev_data)
        save_daily_file(yesterday, prev_data)
        print(f"📊 Calculated previous day ({yesterday}) stats: {prev_data['total_distance_miles']:.1f} miles")
    
    # Ensure current day file exists
    today_data = load_daily_file(today)
    if today_data is None:
        # Get current location for start
        current_time = time.time()
        current_lat = location_data["driver"]["latitude"]
        current_lng = location_data["driver"]["longitude"]
        
        today_data = create_empty_daily_file(today, current_time, current_lat, current_lng)
        print(f"🆕 Created new daily file for {today}")
    else:
        print(f"📂 Using existing daily file for {today}")
    
    return today_data

# Real truck location from SuperDispatch
SUPERDISPATCH_URL = "https://carrier.superdispatch.com/internal/web/bol/online/dRLleEwmQQR0Laj4qP3WRBjVB/driver-location/"

# Load historical data on startup
historical_data = load_historical_data()
daily_stats = load_daily_stats()

# Initialize location from historical data if available
last_known_location = None
if historical_data:
    last_known_location = historical_data[-1]

# Location data storage with history for speed calculation
location_data = {
    "driver": {
        "latitude": last_known_location['latitude'] if last_known_location else 32.32177734375,
        "longitude": last_known_location['longitude'] if last_known_location else -86.33056640625,
        "last_updated": last_known_location['timestamp'] if last_known_location else time.time(),
        "speed": 0,  # Current speed in mph
        "stopped_since": None,  # Timestamp when truck stopped
        "last_position": None  # Store last known position to detect movement
    },
    "history": historical_data  # Use loaded historical data
}

def update_daily_stats(distance_miles, current_time, lat, lng):
    """Update daily mileage statistics with enhanced tracking"""
    today = date.fromtimestamp(current_time).isoformat()
    
    if today not in daily_stats:
        daily_stats[today] = {
            "total_miles": 0, 
            "start_time": current_time,
            "start_location": None,
            "end_location": None,
            "first_movement_time": None,
            "last_movement_time": None,
            "total_travel_time": 0,
            "last_update_time": current_time
        }
    
    daily_stats[today]["total_miles"] += distance_miles
    daily_stats[today]["last_update_time"] = current_time
    daily_stats[today]["last_movement_time"] = current_time
    # Always update end location to track where the vehicle ended up
    daily_stats[today]["end_location"] = {"latitude": lat, "longitude": lng}
    save_daily_stats(daily_stats)

def set_daily_start_location(lat, lng, current_time):
    """Set the start location for the day when first movement is detected"""
    today = date.fromtimestamp(current_time).isoformat()
    
    if today not in daily_stats:
        daily_stats[today] = {
            "total_miles": 0, 
            "start_time": current_time,
            "start_location": None,
            "first_movement_time": None,
            "total_travel_time": 0,
            "last_update_time": current_time
        }
    
    # Set start location if not already set for today
    if daily_stats[today]["start_location"] is None:
        daily_stats[today]["start_location"] = {"latitude": lat, "longitude": lng}
        daily_stats[today]["first_movement_time"] = current_time
        save_daily_stats(daily_stats)
        print(f"📍 Daily start location set: {lat}, {lng} at {datetime.fromtimestamp(current_time)}")

def update_travel_time(current_time, is_moving):
    """Update total travel time for the day"""
    today = date.fromtimestamp(current_time).isoformat()
    
    if today in daily_stats:
        # Only count time when actually moving
        if is_moving and "last_movement_update" in daily_stats[today]:
            time_diff = current_time - daily_stats[today]["last_movement_update"]
            daily_stats[today]["total_travel_time"] += time_diff
        
        if is_moving:
            daily_stats[today]["last_movement_update"] = current_time
        
        save_daily_stats(daily_stats)

def calculate_road_distance(start_lat, start_lng, end_lat, end_lng):
    """Calculate actual road distance between two points using OSRM routing API"""
    try:
        start_coord = f"{start_lng},{start_lat}"
        end_coord = f"{end_lng},{end_lat}"
        osrm_url = f"http://router.project-osrm.org/route/v1/driving/{start_coord};{end_coord}?overview=false&alternatives=false&steps=false"
        
        response = requests.get(osrm_url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('routes') and len(data['routes']) > 0:
                # Distance in meters, convert to miles
                distance_miles = data['routes'][0]['distance'] / 1609.34
                print(f"🛣️ Road distance calculated: {distance_miles:.1f} miles (from {start_lat:.4f},{start_lng:.4f} to {end_lat:.4f},{end_lng:.4f})")
                return distance_miles
        
        # Fallback to straight-line distance with road factor if OSRM fails
        straight_distance = haversine_distance(start_lat, start_lng, end_lat, end_lng)
        road_distance = straight_distance * 1.3  # Add 30% for road curves/detours
        print(f"🗺️ Using estimated road distance: {road_distance:.1f} miles (straight-line: {straight_distance:.1f} + 30%)")
        return road_distance
        
    except Exception as e:
        print(f"❌ Error calculating road distance: {e}")
        # Fallback to straight-line distance with road factor
        straight_distance = haversine_distance(start_lat, start_lng, end_lat, end_lng)
        return straight_distance * 1.3

def get_daily_travel_stats(target_date=None):
    """Get comprehensive daily travel statistics with enhanced start/end tracking"""
    if target_date is None:
        target_date = date.today().isoformat()
    
    day_stats = daily_stats.get(target_date, {})
    
    result = {
        "total_miles": day_stats.get("total_miles", 0),
        "travel_time_hours": day_stats.get("total_travel_time", 0) / 3600,
        "start_location": day_stats.get("start_location"),
        "end_location": day_stats.get("end_location"),
        "first_movement_time": day_stats.get("first_movement_time"),
        "last_movement_time": day_stats.get("last_movement_time"),
        "straight_line_distance": 0,
        "road_distance": 0
    }
    
    # Calculate distances using start and current location (always use current for today)
    start_loc = day_stats.get("start_location")

    # Prefer using stored start/end locations for past days and today when available
    today = date.today().isoformat()
    if start_loc:
        use_live_current = (target_date == today)
        end_lat, end_lng, end_time = get_day_end_info(day_stats, use_live_current)

        if end_lat is not None and end_lng is not None:
            # Straight-line distance
            result['straight_line_distance'] = haversine_distance(start_loc['latitude'], start_loc['longitude'], end_lat, end_lng)

            # Road distance using OSRM (fallback to multiplier)
            road_distance = calculate_road_distance(start_loc['latitude'], start_loc['longitude'], end_lat, end_lng)
            result['road_distance'] = road_distance

            # Estimate travel time if not recorded
            result['travel_time_hours'] = estimate_travel_time_from_stats(day_stats, end_time)
            if result['travel_time_hours'] > 0:
                print(f"📊 Estimated travel time for {target_date} from stored start/end: {result['travel_time_hours']:.2f}h")

            print(f"📊 Daily distance ({target_date}): Start({start_loc['latitude']:.4f},{start_loc['longitude']:.4f}) -> End({end_lat:.4f},{end_lng:.4f}) = {road_distance:.1f}mi")
    
    # Calculate actual travel hours from first movement to last movement if available
    if day_stats.get("first_movement_time") and day_stats.get("last_movement_time"):
        actual_travel_hours = (day_stats["last_movement_time"] - day_stats["first_movement_time"]) / 3600
        result["actual_day_duration_hours"] = actual_travel_hours
        print(f"📊 Daily travel duration ({target_date}): {actual_travel_hours:.1f} hours (from {datetime.fromtimestamp(day_stats['first_movement_time']).strftime('%H:%M')} to {datetime.fromtimestamp(day_stats['last_movement_time']).strftime('%H:%M')})")
    
    return result

def get_daily_mileage(target_date=None):
    """Get mileage for a specific date (default: today)"""
    if target_date is None:
        target_date = date.today().isoformat()
    
    return daily_stats.get(target_date, {}).get("total_miles", 0)

def get_previous_days_travel(days_back=2):
    """Get previous days travel history in format: month/day (Xh, Ymi)"""
    previous_travel = []
    today = date.today()

    # Load full historical data once (fallback only)
    full_history = load_historical_data()

    def _compute_day_road_miles(target_date_obj, day_data):
        """Compute road miles for a day using stored start/end, history fallback, or accumulated segments."""
        # Prefer explicit start/end stored in daily_stats
        if day_data.get('start_location') and day_data.get('end_location'):
            s = day_data['start_location']
            e = day_data['end_location']
            return calculate_road_distance(s['latitude'], s['longitude'], e['latitude'], e['longitude'])

        if day_data.get('start_location'):
            # Start exists but no stored end - try historical points for that day
            s = day_data['start_location']
            day_start_ts = datetime.combine(target_date_obj, datetime.min.time()).timestamp()
            day_end_ts = day_start_ts + 86400
            day_points = [p for p in full_history if day_start_ts <= p.get('timestamp', 0) < day_end_ts]
            if day_points:
                day_points.sort(key=lambda x: x['timestamp'])
                e = day_points[-1]
                return calculate_road_distance(s['latitude'], s['longitude'], e['latitude'], e['longitude'])

        # Fallback to accumulated segments
        return day_data.get('total_miles', 0)

    for i in range(1, days_back + 1):
        target_date = today - timedelta(days=i)
        date_str = target_date.isoformat()

        if date_str not in daily_stats:
            continue

        day_data = daily_stats[date_str]
        travel_time_hours = day_data.get('total_travel_time', 0) / 3600
        road_miles = _compute_day_road_miles(target_date, day_data)

        # If travel time missing, estimate from stored movement times
        if travel_time_hours == 0 and day_data.get('first_movement_time') and day_data.get('last_movement_time'):
            travel_time_hours = (day_data['last_movement_time'] - day_data['first_movement_time']) / 3600

        if road_miles > 0 or travel_time_hours > 0:
            formatted_date = f"{target_date.month}/{target_date.day}"
            travel_text = f"{formatted_date} ({round(travel_time_hours)}h, {round(road_miles)}mi)"
            previous_travel.append(travel_text)

    return ", ".join(previous_travel) if previous_travel else "No recent travel"

def calculate_speed(minutes=5):
    """Calculate current speed based on specified minutes of location data with realistic speed validation"""
    if len(location_data["history"]) < 2:
        return 0
    
    # Get locations from specified timeframe
    now = time.time()
    recent_locations = [loc for loc in location_data["history"] if now - loc["timestamp"] <= minutes * 60]
    
    if len(recent_locations) < 2:
        return 0
    
    # Calculate distance between most recent and oldest location in timeframe
    newest = recent_locations[-1]
    oldest = recent_locations[0]
    
    distance_miles = haversine_distance(
        oldest["latitude"], oldest["longitude"],
        newest["latitude"], newest["longitude"]
    )
    
    time_hours = (newest["timestamp"] - oldest["timestamp"]) / 3600
    
    if time_hours > 0:
        calculated_speed = distance_miles / time_hours
        
        # Validate speed is realistic (not GPS error)
        if is_realistic_speed(calculated_speed):
            return calculated_speed
        else:
            print(f"⚠️ Unrealistic speed detected: {calculated_speed:.1f} mph - ignoring GPS error")
            return 0
    
    return 0

def calculate_vehicle_icon_speed():
    """Calculate speed for vehicle icon display: prioritize 1-minute, fallback to 5-minute average"""
    # First try 1-minute average for most responsive display
    one_min_speed = calculate_speed(1)
    
    if one_min_speed > 0:
        print(f"🚛 Vehicle icon speed: {one_min_speed:.1f} mph (1-min average)")
        return one_min_speed
    
    # Fallback to 5-minute average if 1-minute data insufficient
    five_min_speed = calculate_speed(5)
    
    if five_min_speed > 0:
        print(f"🚛 Vehicle icon speed: {five_min_speed:.1f} mph (5-min fallback)")
        return five_min_speed
    
    # No movement detected
    print("🚛 Vehicle icon speed: 0 mph (no movement)")
    return 0

def calculate_average_moving_speed(minutes=30):
    """Calculate average speed when moving over specified timeframe with speed validation"""
    if len(location_data["history"]) < 2:
        return 0
    
    now = time.time()
    recent_locations = [loc for loc in location_data["history"] if now - loc["timestamp"] <= minutes * 60]
    
    if len(recent_locations) < 2:
        return 0
    
    total_distance = 0
    total_time = 0
    
    # Calculate cumulative distance and time for moving segments
    for i in range(1, len(recent_locations)):
        prev_loc = recent_locations[i-1]
        curr_loc = recent_locations[i]
        
        distance = haversine_distance(
            prev_loc["latitude"], prev_loc["longitude"],
            curr_loc["latitude"], curr_loc["longitude"]
        )
        
        time_diff = curr_loc["timestamp"] - prev_loc["timestamp"]
        segment_speed = (distance / (time_diff / 3600)) if time_diff > 0 else 0
        
        # Only include realistic moving segments
        if is_realistic_speed(segment_speed) and segment_speed > 0.5:  # Moving if > 0.5 mph
            total_distance += distance
            total_time += time_diff
    
    if total_time > 0:
        avg_speed = total_distance / (total_time / 3600)
        return avg_speed if is_realistic_speed(avg_speed) else 0
    
    return 0

def get_positions_in_window(end_time, window_seconds):
    """Return sorted positions within a time window ending at end_time."""
    start_time = end_time - window_seconds
    positions = [p for p in location_data.get("history", []) if start_time <= p.get('timestamp', 0) <= end_time]
    positions.sort(key=lambda x: x['timestamp'])
    return positions

def compute_window_distance(end_time, window_seconds):
    """Compute cumulative haversine distance for positions inside the given time window."""
    positions = get_positions_in_window(end_time, window_seconds)
    if len(positions) < 2:
        return 0.0

    total_distance = 0.0
    for i in range(1, len(positions)):
        prev = positions[i-1]
        curr = positions[i]
        total_distance += haversine_distance(prev['latitude'], prev['longitude'], curr['latitude'], curr['longitude'])
    return total_distance

def get_day_end_info(day_stats_entry, use_live_current=False):
    """Return (end_lat, end_lng, end_time) using either live history or stored end info."""
    if use_live_current and len(location_data.get('history', [])) > 0:
        current_loc = location_data['history'][-1]
        return current_loc['latitude'], current_loc['longitude'], current_loc.get('timestamp')

    end_loc = day_stats_entry.get('end_location')
    if end_loc:
        end_time = day_stats_entry.get('last_movement_time') or day_stats_entry.get('last_update_time')
        return end_loc.get('latitude'), end_loc.get('longitude'), end_time

    return None, None, None

def estimate_travel_time_from_stats(day_stats_entry, end_time):
    """Estimate travel time in hours from stored day stats start/first movement and provided end_time."""
    if day_stats_entry.get('total_travel_time', 0) > 0:
        return day_stats_entry.get('total_travel_time', 0) / 3600

    start_time = day_stats_entry.get('first_movement_time') or day_stats_entry.get('start_time')
    if start_time and end_time and end_time > start_time:
        return (end_time - start_time) / 3600

    return 0

def compute_day_summary(target_date_obj, day_data, full_history):
    """Compute a formatted summary string for a past day or return None if no activity."""
    travel_time_hours = day_data.get('total_travel_time', 0) / 3600
    # Compute road miles using the shared helper
    def _compute_day_road_miles_local(target_date_obj_inner, day_data_inner):
        if day_data_inner.get('start_location') and day_data_inner.get('end_location'):
            s = day_data_inner['start_location']
            e = day_data_inner['end_location']
            return calculate_road_distance(s['latitude'], s['longitude'], e['latitude'], e['longitude'])
        if day_data_inner.get('start_location'):
            s = day_data_inner['start_location']
            day_start_ts = datetime.combine(target_date_obj_inner, datetime.min.time()).timestamp()
            day_end_ts = day_start_ts + 86400
            day_points = [p for p in full_history if day_start_ts <= p.get('timestamp', 0) < day_end_ts]
            if day_points:
                day_points.sort(key=lambda x: x['timestamp'])
                e = day_points[-1]
                return calculate_road_distance(s['latitude'], s['longitude'], e['latitude'], e['longitude'])
        return day_data_inner.get('total_miles', 0)

    road_miles = _compute_day_road_miles_local(target_date_obj, day_data)
    if travel_time_hours == 0 and day_data.get('first_movement_time') and day_data.get('last_movement_time'):
        travel_time_hours = (day_data['last_movement_time'] - day_data['first_movement_time']) / 3600

    if road_miles > 0 or travel_time_hours > 0:
        formatted_date = f"{target_date_obj.month}/{target_date_obj.day}"
        return f"{formatted_date} ({round(travel_time_hours)}h, {round(road_miles)}mi)"
    return None
def get_movement_status():
    """Determine if truck is moving or stopped based on last 5 minutes.

    Uses small helpers to compute windowed distances which reduces nested loops
    and makes intent clearer.
    """
    history = location_data.get("history", [])
    if len(history) < 2:
        return "stopped", location_data["driver"].get("stopped_since")

    current_time = time.time()

    # Use helper to get cumulative distance over last 5 minutes
    total_distance = compute_window_distance(current_time, 300)
    if total_distance < 0.0001:
        # Not enough points in window to decide
        recent_positions = get_positions_in_window(current_time, 300)
        if len(recent_positions) < 2:
            return 'unknown', None

    is_moving = total_distance > 0.028  # ~150 feet in miles

    if is_moving:
        # Find when movement started by scanning backwards; stop when a 5-min window shows no movement
        movement_start = current_time
        for pos in reversed(history):
            end_ts = pos.get('timestamp', movement_start)
            if compute_window_distance(end_ts, 300) <= 0.028:
                movement_start = end_ts
                break
        return 'moving', movement_start

    # Otherwise, determine when it last moved
    stop_time = current_time
    for pos in reversed(history):
        end_ts = pos.get('timestamp', stop_time)
        if compute_window_distance(end_ts, 300) > 0.028:
            stop_time = end_ts
            break
    return 'stopped', stop_time

def get_stopped_duration():
    """Get how long the vehicle has been stopped in seconds"""
    history = location_data.get("history", [])
    if len(history) < 2:
        return 0

    current_time = time.time()

    # If recent 5-minute window shows movement, it's not stopped
    recent_distance = compute_window_distance(current_time, 300)
    if recent_distance > 0.028:
        return 0

    # Find when it last moved by scanning backward for a window showing movement
    last_moving_time = None
    for pos in reversed(history):
        end_ts = pos.get('timestamp', current_time)
        if compute_window_distance(end_ts, 300) > 0.028:
            last_moving_time = end_ts
            break

    if last_moving_time:
        return int(current_time - last_moving_time)

    # If never found, return time since oldest record
    oldest_ts = history[0].get('timestamp') if history else current_time
    return int(current_time - oldest_ts)

def calculate_speed_from_subset(history_subset):
    """Helper function to calculate speed from a subset of history"""
    if len(history_subset) < 2:
        return 0
    
    newest = history_subset[-1]
    oldest = history_subset[0]
    
    def haversine_distance(lat1, lon1, lat2, lon2):
        R = 3959  # Earth's radius in miles
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat/2) * math.sin(dlat/2) + 
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * 
             math.sin(dlon/2) * math.sin(dlon/2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c
    
    distance_miles = haversine_distance(
        oldest["latitude"], oldest["longitude"],
        newest["latitude"], newest["longitude"]
    )
    
    time_hours = (newest["timestamp"] - oldest["timestamp"]) / 3600
    
    if time_hours > 0:
        return distance_miles / time_hours
    
    return 0

def build_journey_history(history, stats, max_points=50):
    """Build a simplified journey history for visualization.

    - Ensures we start from the absolute earliest available point (including daily_stats start locations)
    - Samples points to limit to ~max_points for map rendering
    """
    journey_history = []
    if not history and not stats:
        print("⚠️ No location history or stats available for journey visualization")
        return journey_history

    # Sort GPS history by timestamp
    sorted_history = sorted(history, key=lambda x: x["timestamp"]) if history else []

    earliest_gps_time = sorted_history[0]["timestamp"] if sorted_history else time.time()
    earliest_daily_start = find_earliest_daily_start(stats, earliest_gps_time)

    # Include earliest daily start first if found
    if earliest_daily_start:
        journey_history.append({
            "lat": earliest_daily_start["latitude"],
            "lng": earliest_daily_start["longitude"],
            "timestamp": earliest_daily_start["timestamp"]
        })
        print(f"🗺️ Starting journey from EARLIEST daily start: {earliest_daily_start['latitude']:.4f},{earliest_daily_start['longitude']:.4f} from {earliest_daily_start['date']}")

    # Decide sampling step to keep points reasonable for rendering
    total_points = len(sorted_history)
    if total_points <= max_points:
        step = 1
    else:
        step = max(1, total_points // max_points)

    print(f"🗺️ Using step size {step} to create journey route with ~{total_points // step} GPS points")

    # Sample history points and append to journey_history
    sampled = sample_history_points(sorted_history, earliest_daily_start, step)
    journey_history.extend(sampled)

    # Ensure the most recent point is included
    if sorted_history:
        last_point = sorted_history[-1]
        if not journey_history or journey_history[-1]["timestamp"] != last_point["timestamp"]:
            journey_history.append({
                "lat": last_point["latitude"],
                "lng": last_point["longitude"],
                "timestamp": last_point["timestamp"]
            })

    print(f"🗺️ Created journey history with {len(journey_history)} points")
    if journey_history:
        first_point = journey_history[0]
        last_point = journey_history[-1]
        print(f"🗺️ Complete Journey: EARLIEST({first_point['lat']:.4f},{first_point['lng']:.4f}) -> CURRENT({last_point['lat']:.4f},{last_point['lng']:.4f})")
        print(f"🗺️ Journey timespan: {datetime.fromtimestamp(first_point['timestamp']).strftime('%Y-%m-%d %H:%M')} to {datetime.fromtimestamp(last_point['timestamp']).strftime('%Y-%m-%d %H:%M')}")

    return journey_history

def sample_history_points(sorted_history, earliest_daily_start, step):
    """Return sampled points from sorted_history, skipping the first GPS point if it duplicates earliest_daily_start."""
    results = []
    start_index = 0
    if earliest_daily_start and sorted_history:
        first_gps = sorted_history[0]
        distance = haversine_distance(
            earliest_daily_start["latitude"], earliest_daily_start["longitude"],
            first_gps["latitude"], first_gps["longitude"]
        )
        if distance < 1.0:
            start_index = step

    for i in range(start_index, len(sorted_history), step):
        p = sorted_history[i]
        results.append({
            "lat": p["latitude"],
            "lng": p["longitude"],
            "timestamp": p["timestamp"]
        })

    return results

def find_earliest_daily_start(stats, earliest_gps_time):
    """Find the earliest daily start location older than earliest_gps_time.

    Returns a dict with latitude, longitude, timestamp and date, or None.
    """
    if not stats:
        return None

    earliest = None
    for date_str, entry in stats.items():
        loc = entry.get("start_location")
        start_time = entry.get("start_time")
        if loc and start_time and start_time < earliest_gps_time:
            if earliest is None or start_time < earliest.get("start_time", float('inf')):
                earliest = {
                    "latitude": loc["latitude"],
                    "longitude": loc["longitude"],
                    "timestamp": start_time,
                    "date": date_str,
                    "start_time": start_time
                }
    return earliest

def fetch_real_location():
    """Fetch real truck location from SuperDispatch API"""
    try:
        response = requests.get(SUPERDISPATCH_URL, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'driver' in data['data'] and 'location' in data['data']['driver']:
                location = data['data']['driver']['location']
                # Delegate processing of the fetched location to a helper to reduce complexity
                return process_fetched_location(location)
        else:
            print(f"❌ SuperDispatch API error: {response.status_code}")
    except Exception as e:
        print(f"❌ Error fetching real location: {e}")

    return False, False


def update_driver_state(lat, lng, current_time, current_speed):
    """Update driver metadata: position, speed, stopped_since and last_position."""
    try:
        d = location_data['driver']
        d['latitude'] = lat
        d['longitude'] = lng
        d['last_updated'] = current_time
        d['speed'] = current_speed

        # Update last_position for movement detection
        d['last_position'] = (lat, lng)

        # Manage stopped_since using 5-minute windowed movement detection
        # This avoids marking as stopped due to GPS jitter or slow traffic.
        movement_status, _ = get_movement_status()
        if movement_status == 'stopped':
            # If it's been stopped for more than 5 minutes, set stopped_since to when it stopped
            stopped_secs = get_stopped_duration()
            if stopped_secs >= 300:  # 300 seconds = 5 minutes
                # If we don't already have a stopped_since, set it to the time the stop started
                if d.get('stopped_since') is None:
                    d['stopped_since'] = current_time - stopped_secs
            else:
                # Not yet considered stopped long enough
                d['stopped_since'] = None
        else:
            # Moving -> clear stopped_since
            d['stopped_since'] = None
    except Exception as e:
        print(f"❌ Error updating driver state: {e}")

def calculate_distance_simple(lat1, lon1, lat2, lon2):
    """Simple distance calculation using Haversine formula"""
    R = 3959  # Earth's radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2) * math.sin(dlat/2) + 
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * 
         math.sin(dlon/2) * math.sin(dlon/2))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def append_history_point(lat, lng, ts):
    """Append a GPS point to in-memory history."""
    p = {"latitude": lat, "longitude": lng, "timestamp": ts}
    location_data.setdefault('history', []).append(p)
    return p

def trim_history_hours(hours=1):
    """Trim in-memory history to the last `hours` hours."""
    now = time.time()
    cutoff = now - (hours * 3600)
    location_data['history'] = [loc for loc in location_data.get('history', []) if loc.get('timestamp', 0) >= cutoff]

def periodic_persist_history():
    """Persist history to disk every 3 points to reduce IO frequency."""
    if len(location_data.get('history', [])) % 3 == 0:
        save_historical_data(location_data['history'])

def handle_segment_update_for_new_point(new_lat, new_lng, current_time):
    """Update daily stats using the previous GPS segment when appropriate."""
    if len(location_data.get('history', [])) < 2:
        return False
    prev_location = location_data['history'][-2]
    seg_dist = haversine_distance(prev_location['latitude'], prev_location['longitude'], new_lat, new_lng)
    seg_dt = current_time - prev_location.get('timestamp', current_time)
    if seg_dt <= 0:
        return False
    seg_speed = seg_dist / (seg_dt / 3600.0)
    if is_realistic_speed(seg_speed) and seg_dist > 0.001:
        update_daily_stats(seg_dist, current_time, new_lat, new_lng)
        set_daily_start_location(new_lat, new_lng, current_time)
        return True
    return False

def compute_segment_moving_flag_for_new_point(new_lat, new_lng, current_time):
    """Determine if the last GPS segment indicates movement (used for travel time accounting)."""
    if len(location_data.get('history', [])) < 2:
        return False
    prev_location = location_data['history'][-2]
    seg_dt = current_time - prev_location.get('timestamp', current_time)
    if seg_dt <= 0:
        return False
    seg_dist = haversine_distance(prev_location['latitude'], prev_location['longitude'], new_lat, new_lng)
    seg_speed = seg_dist / (seg_dt / 3600.0)
    return seg_speed > 0.5 and is_realistic_speed(seg_speed)

def detect_last_position_movement(new_lat, new_lng):
    """Detect if the driver moved relative to the last stored driver position."""
    last = location_data['driver'].get('last_position')
    if not last:
        return True, 0
    last_lat, last_lng = last
    dist = calculate_distance_simple(last_lat, last_lng, new_lat, new_lng)
    return (dist > 0.009), dist

def update_location_periodically():
    """Update truck location from SuperDispatch every 60 seconds"""
    first_update = True
    last_saved_location = load_last_location()
    
    if last_saved_location:
        print(f"📍 Found last saved location from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_saved_location['timestamp']))}")
    
    while True:
        success, has_moved = fetch_real_location()
        
        # On first successful update, calculate startup speed if we have a last saved location
        if success and first_update and last_saved_location:
            current_time = time.time()
            current_lat = location_data["driver"]["latitude"]
            current_lng = location_data["driver"]["longitude"]
            
            startup_speed = calculate_startup_speed(last_saved_location, current_lat, current_lng, current_time)
            if startup_speed > 0:
                # Update the current speed with calculated startup speed
                location_data["driver"]["speed"] = startup_speed
                print(f"🚀 Applied startup speed calculation: {startup_speed:.1f} mph")
            
            first_update = False
        
        if not success:
            print("⚠️ Using last known location (SuperDispatch unavailable)")
        elif not has_moved:
            print("📍 Location unchanged - truck stationary")
        time.sleep(60)  # Update every 60 seconds

@app.route('/')
def index():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Find Truck Tracker</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.7.1/dist/leaflet.css" />
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: #f4f4f4; }
        .header { background: #2c3e50; color: white; padding: 4px; text-align: center; }
        .header h1 { font-size: 0.9em; margin: 0; font-weight: normal; }
        .container { display: flex; flex-direction: column; height: 100vh; }
        .status-bar { background: white; padding: 6px; border-bottom: 1px solid #ddd; 
                     display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; 
                     font-size: 0.75em; line-height: 1.2; }
        .status-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .status-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .status-item { white-space: nowrap; }
        .status-value { font-weight: bold; color: #2c3e50; }
        .refresh-btn { background: #3498db; color: white; border: none; padding: 3px 6px; 
                      border-radius: 3px; cursor: pointer; font-size: 0.7em; }
        .refresh-btn:hover { background: #2980b9; }
        #map { flex: 1; min-height: 400px; }
        .separator { color: #bdc3c7; margin: 0 4px; }
        
        /* Truck marker with speed display */
        .truck-marker { position: relative; }
        .truck-speed-label { 
            position: absolute; 
            top: -25px; 
            left: 50%; 
            transform: translateX(-50%);
            background: rgba(0, 0, 0, 0.8); 
            color: white; 
            padding: 2px 8px; 
            border-radius: 12px; 
            font-size: 10px; 
            font-weight: bold;
            white-space: nowrap;
            z-index: 1000;
            pointer-events: none;
            min-width: 60px;
            text-align: center;
        }
        .truck-speed-label.moving { background: rgba(46, 204, 113, 0.9); }
        .truck-speed-label.stopped { background: rgba(231, 76, 60, 0.9); }
        
        /* Layer control styling */
        .leaflet-control-layers {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.2);
        }
        
        .leaflet-control-layers-toggle {
            background-image: url('data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTMgMTJMMTIgM0wyMSAxMk0zIDEyTDEyIDIxTDIxIDEyIiBzdHJva2U9IiMzMzMiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+Cjwvc3ZnPgo=');
            width: 24px;
            height: 24px;
        }
        
        @media (max-width: 768px) {
            .status-bar { flex-direction: column; align-items: flex-start; gap: 4px; }
            .status-left, .status-right { flex-wrap: wrap; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚛 Find My Truck - Created by Anil</h1>
        </div>
        
        <div class="status-bar">
            <div class="status-left">
                <span class="status-item">Last updated: <span class="status-value" id="lastUpdate">Loading...</span></span>
                <span class="separator">•</span>
                <button class="refresh-btn" onclick="updateLocation()">
                    Refresh (<span id="countdown">60</span>s)
                </button>
                <span class="separator">•</span>
                <span class="status-item"><span class="status-value" id="currentLocation">Loading...</span> to Milpitas, CA</span>
                <span class="separator">•</span>
                <span class="status-item">ETA: <span class="status-value" id="etaHours">-</span>h (<span class="status-value" id="etaDays">-</span>d)</span>
                <span class="separator">•</span>
                <span class="status-item">Arrival: <span class="status-value" id="etaDateTime">-</span></span>
            </div>
        </div>
        
        <div id="map"></div>
    </div>

    <script src="https://unpkg.com/leaflet@1.7.1/dist/leaflet.js"></script>
    <script>
        let map, driverMarker, destinationMarker, routeLine, dailyTravelRoute, remainingRoute;
        let countdownTimer;
        let lastKnownPosition = null; // Track last position to detect movement
        let firstRouteLoad = true; // Track if this is the first route to auto-fit only once
        const milpitasCoords = [37.4419, -121.9080]; // Milpitas, CA coordinates
        
        // Initialize map
        function initMap() {
            console.log('🗺️ Starting map initialization...');
            
            // Get saved zoom level from localStorage, default to 6 if not found
            const savedZoom = localStorage.getItem('truckMapZoom') || 6;
            const savedLat = localStorage.getItem('truckMapLat') || 32.5415039;
            const savedLng = localStorage.getItem('truckMapLng') || -97.1180592;
            
            console.log('Loading saved map view:', { zoom: savedZoom, lat: savedLat, lng: savedLng });
            
            // Check if map container exists
            const mapContainer = document.getElementById('map');
            if (!mapContainer) {
                console.error('❌ Map container not found!');
                return;
            }
            console.log('✅ Map container found:', mapContainer);
            
            try {
                map = L.map('map').setView([parseFloat(savedLat), parseFloat(savedLng)], parseInt(savedZoom));
                console.log('✅ Leaflet map created successfully');
            } catch (error) {
                console.error('❌ Error creating Leaflet map:', error);
                return;
            }
            
            // Define different map layers
            console.log('🗺️ Creating map layers...');
            const openStreetMap = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenStreetMap contributors',
                name: 'OpenStreetMap'
            });
            
            const satelliteMap = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
                attribution: '© Esri, Maxar, Earthstar Geographics, and the GIS User Community',
                name: 'Satellite'
            });
            
            const topoMap = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
                attribution: '© Esri, HERE, Garmin, Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), (c) OpenStreetMap contributors, and the GIS User Community',
                name: 'Topographic'
            });
            
            const terrainMap = L.tileLayer('https://stamen-tiles-{s}.a.ssl.fastly.net/terrain/{z}/{x}/{y}.{ext}', {
                attribution: 'Map tiles by <a href="http://stamen.com">Stamen Design</a>, <a href="http://creativecommons.org/licenses/by/3.0">CC BY 3.0</a> &mdash; Map data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
                subdomains: 'abcd',
                minZoom: 0,
                maxZoom: 18,
                ext: 'png',
                name: 'Terrain'
            });
            
            // Add default layer (Standard)
            console.log('🗺️ Adding default OpenStreetMap layer...');
            openStreetMap.addTo(map);
            console.log('✅ Default layer added successfully');
            
            // Create layer control
            console.log('🗺️ Creating layer control...');
            const baseMaps = {
                "🗺️ Standard": openStreetMap,
                "🛰️ Satellite": satelliteMap,
                "🏔️ Topographic": topoMap,
                "🌄 Terrain": terrainMap
            };
            
            // Add layer control to map
            L.control.layers(baseMaps).addTo(map);
            console.log('✅ Layer control added successfully');
            
            // Add map event listeners to save user preferences for zoom and position
            map.on('zoomend', function() {
                const zoom = map.getZoom();
                localStorage.setItem('mapZoom', zoom);
                console.log(`💾 Saved user zoom level: ${zoom}`);
            });
            
            map.on('moveend', function() {
                // Only save position if user manually moved (not auto-centering on vehicle)
                if (!isAutoPositioning) {
                    const center = map.getCenter();
                    localStorage.setItem('mapLat', center.lat);
                    localStorage.setItem('mapLng', center.lng);
                    console.log(`💾 Saved user map position: ${center.lat}, ${center.lng}`);
                }
            });
            
            // Flag to track when we're auto-positioning vs user interaction
            let isAutoPositioning = false;
            
            // Create custom truck marker
            console.log('🚛 Creating truck marker...');
            const truckIcon = L.divIcon({
                html: '<div style="font-size: 24px; text-align: center; line-height: 1;">🚛</div>',
                className: 'truck-marker',
                iconSize: [30, 30],
                iconAnchor: [15, 15]
            });
            
            // Create custom factory marker for destination
            console.log('🏭 Creating factory marker...');
            const factoryIcon = L.divIcon({
                html: '<div style="font-size: 24px; text-align: center; line-height: 1;">🏭</div>',
                className: 'factory-marker',
                iconSize: [30, 30],
                iconAnchor: [15, 15]
            });
            
            // Add destination marker (Milpitas, CA)
            destinationMarker = L.marker(milpitasCoords, {icon: factoryIcon})
                .addTo(map)
                .bindPopup('<b>Destination: Milpitas, CA</b><br>Target delivery location');
            console.log('✅ Destination marker added at:', milpitasCoords);
            
            // Save zoom level and center position when map view changes
            map.on('zoomend moveend', function() {
                const center = map.getCenter();
                const zoom = map.getZoom();
                console.log('Saving map view:', { zoom: zoom, lat: center.lat, lng: center.lng });
                localStorage.setItem('truckMapZoom', zoom.toString());
                localStorage.setItem('truckMapLat', center.lat.toString());
                localStorage.setItem('truckMapLng', center.lng.toString());
            });
            
            console.log('✅ Map initialization completed successfully');
        }
        
        function calculateDistance(lat1, lon1, lat2, lon2) {
            const R = 3959; // Earth's radius in miles
            const dLat = (lat2 - lat1) * Math.PI / 180;
            const dLon = (lon2 - lon1) * Math.PI / 180;
            const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                     Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                     Math.sin(dLon/2) * Math.sin(dLon/2);
            const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            return R * c;
        }
        
        function calculateJourneyDistance(journeyPoints) {
            let totalDistance = 0;
            for (let i = 1; i < journeyPoints.length; i++) {
                const prev = journeyPoints[i-1];
                const curr = journeyPoints[i];
                totalDistance += calculateDistance(prev.lat, prev.lng, curr.lat, curr.lng);
            }
            return totalDistance;
        }
        
        function updateRoute(currentLat, currentLng, dailyStartLocation, journeyHistory) {
            // Remove existing route lines
            if (routeLine) {
                map.removeLayer(routeLine);
            }
            if (dailyTravelRoute) {
                map.removeLayer(dailyTravelRoute);
            }
            if (remainingRoute) {
                map.removeLayer(remainingRoute);
            }
            
            console.log(`Updating route - Current: ${currentLat},${currentLng}, Daily Start:`, dailyStartLocation);
            
            // Draw multi-segment journey with different colors for each day/phase
            if (dailyStartLocation && dailyStartLocation.latitude && dailyStartLocation.longitude) {
                drawMultiDayJourney(dailyStartLocation.latitude, dailyStartLocation.longitude, currentLat, currentLng, journeyHistory);
            }
            
            // Draw remaining route (current to destination) in green
            drawRemainingRoute(currentLat, currentLng);
        }
        
        function drawMultiDayJourney(startLat, startLng, currentLat, currentLng, journeyHistory) {
            console.log('🗺️ Drawing multi-day journey with color-coded segments');
            console.log('Journey history points available:', journeyHistory ? journeyHistory.length : 0);
            
            if (!journeyHistory || journeyHistory.length < 2) {
                console.log('⚠️ Insufficient journey history for multi-day visualization');
                return;
            }
            
            // Get today's date to separate current day from previous days
            const today = new Date().toISOString().split('T')[0];
            const todayStart = new Date(today + 'T00:00:00').getTime() / 1000;
            
            // Separate journey points into previous days and current day
            const previousDaysPoints = [];
            const currentDayPoints = [];
            
            journeyHistory.forEach(point => {
                if (point.timestamp < todayStart) {
                    previousDaysPoints.push(point);
                } else {
                    currentDayPoints.push(point);
                }
            });
            
            console.log(`📊 Previous days: ${previousDaysPoints.length} points, Current day: ${currentDayPoints.length} points`);
            
            // 1. Draw previous days route (first day start → previous day end) in light grey
            if (previousDaysPoints.length >= 2) {
                const prevDaysCoords = previousDaysPoints.map(point => [point.lat, point.lng]);
                const previousDaysRoute = L.polyline(prevDaysCoords, {
                    color: '#95a5a6',  // Light grey for previous days
                    weight: 5,
                    opacity: 0.7
                }).addTo(map);
                
                const prevDaysDistance = calculateJourneyDistance(previousDaysPoints);
                const prevStartTime = new Date(previousDaysPoints[0].timestamp * 1000);
                const prevEndTime = new Date(previousDaysPoints[previousDaysPoints.length - 1].timestamp * 1000);
                
                previousDaysRoute.bindPopup(`<b>Previous Days Journey</b><br>${prevDaysDistance.toFixed(1)} miles<br>From: ${prevStartTime.toLocaleDateString()} ${prevStartTime.toLocaleTimeString()}<br>To: ${prevEndTime.toLocaleDateString()} ${prevEndTime.toLocaleTimeString()}`);
                console.log(`✅ Previous days route drawn: ${prevDaysDistance.toFixed(1)} miles (light grey)`);
            }
            
            // 2. Draw current day route (today start → current position) in dark grey/black
            if (currentDayPoints.length >= 2) {
                const currentDayCoords = currentDayPoints.map(point => [point.lat, point.lng]);
                dailyTravelRoute = L.polyline(currentDayCoords, {
                    color: '#2c3e50',  // Dark grey/black for current day
                    weight: 6,
                    opacity: 0.9
                }).addTo(map);
                
                const currentDayDistance = calculateJourneyDistance(currentDayPoints);
                const currentStartTime = new Date(currentDayPoints[0].timestamp * 1000);
                const currentEndTime = new Date(currentDayPoints[currentDayPoints.length - 1].timestamp * 1000);
                
                dailyTravelRoute.bindPopup(`<b>Today's Journey</b><br>${currentDayDistance.toFixed(1)} miles<br>From: ${currentStartTime.toLocaleTimeString()}<br>To: ${currentEndTime.toLocaleTimeString()}`);
                console.log(`✅ Current day route drawn: ${currentDayDistance.toFixed(1)} miles (dark grey)`);
            } else if (currentDayPoints.length === 1) {
                // If only one point today, show it as a marker for today's start
                const todayStart = currentDayPoints[0];
                const todayStartMarker = L.circleMarker([todayStart.lat, todayStart.lng], {
                    color: '#2c3e50',
                    fillColor: '#2c3e50',
                    fillOpacity: 0.8,
                    radius: 8
                }).addTo(map);
                
                const startTime = new Date(todayStart.timestamp * 1000);
                todayStartMarker.bindPopup(`<b>Today's Start</b><br>${startTime.toLocaleTimeString()}`);
                console.log(`✅ Today's start point marked at ${todayStart.lat}, ${todayStart.lng}`);
            }
            
            // Only fit map to show complete journey on first load
            if (firstRouteLoad) {
                try {
                    const allRoutes = [];
                    if (previousDaysPoints.length > 0) allRoutes.push(L.polyline(previousDaysPoints.map(p => [p.lat, p.lng])));
                    if (currentDayPoints.length > 0) allRoutes.push(L.polyline(currentDayPoints.map(p => [p.lat, p.lng])));
                    
                    if (allRoutes.length > 0) {
                        const group = new L.featureGroup(allRoutes);
                        map.fitBounds(group.getBounds().pad(0.1));
                        console.log('✅ Map fitted to show complete multi-day journey (first load only)');
                    }
                } catch (e) {
                    console.log('⚠️ Could not fit map bounds:', e);
                }
            }
        }
        
        function drawRemainingRoute(currentLat, currentLng) {
            // Get actual driving route using OSRM (Open Source Routing Machine)
            const start = `${currentLng},${currentLat}`;
            const end = `${milpitasCoords[1]},${milpitasCoords[0]}`;
            
            console.log(`Drawing remaining route from ${currentLat},${currentLng} to ${milpitasCoords[0]},${milpitasCoords[1]}`);
            
            // Using OSRM API for actual driving directions (free, no API key needed)
            const osrmUrl = `https://router.project-osrm.org/route/v1/driving/${start};${end}?overview=full&geometries=geojson`;
            
            fetch(osrmUrl)
                .then(response => {
                    console.log('OSRM Response status:', response.status);
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    console.log('OSRM Data received:', data);
                    if (data.routes && data.routes[0] && data.routes[0].geometry) {
                        const coords = data.routes[0].geometry.coordinates;
                        // Convert [lng, lat] to [lat, lng] for Leaflet
                        const routeCoords = coords.map(coord => [coord[1], coord[0]]);
                        
                        console.log(`Drawing remaining route with ${routeCoords.length} points`);
                        
                        // Dark green route for remaining journey
                        remainingRoute = L.polyline(routeCoords, {
                            color: '#27ae60',
                            weight: 5,
                            opacity: 0.8
                        }).addTo(map);
                        
                        // Use actual route distance from API
                        const routeDistance = (data.routes[0].distance / 1609.34).toFixed(1); // Convert meters to miles
                        
                        // Calculate realistic truck driving time based on actual route distance
                        // Average truck speed: 55 mph on highways, accounting for stops, traffic, etc.
                        const avgTruckSpeed = 55; // mph
                        const routeDuration = (parseFloat(routeDistance) / avgTruckSpeed).toFixed(1); // hours
                        
                        console.log(`Remaining route: ${routeDistance} miles, ${routeDuration} hours (dark green)`);
                        
                        // Add popup to remaining route
                        remainingRoute.bindPopup(`<b>Remaining Journey</b><br>${routeDistance} miles to destination<br>ETA: ${routeDuration} hours`);
                        
                        // Update route info with actual data
                        updateRouteInfo(routeDistance, routeDuration);
                        
                        // Fit map to show complete journey on first load
                        if (firstRouteLoad) {
                            const allRoutes = [driverMarker, destinationMarker];
                            if (dailyTravelRoute) allRoutes.push(dailyTravelRoute);
                            if (remainingRoute) allRoutes.push(remainingRoute);
                            
                            const group = new L.featureGroup(allRoutes);
                            map.fitBounds(group.getBounds().pad(0.1));
                            firstRouteLoad = false;
                        }
                        
                    } else {
                        console.log('No remaining route found in OSRM response, using fallback');
                        createStraightLineRemainingRoute(currentLat, currentLng);
                    }
                })
                .catch(error => {
                    console.log('Remaining route OSRM API error, using straight line:', error);
                    createStraightLineRemainingRoute(currentLat, currentLng);
                });
        }
        
        function createStraightLineRemainingRoute(currentLat, currentLng) {
            console.log('Creating straight line remaining route as fallback');
            
            remainingRoute = L.polyline([
                [currentLat, currentLng],
                milpitasCoords
            ], {
                color: '#27ae60',
                weight: 4,
                opacity: 0.6,
                dashArray: '10, 10'
            }).addTo(map);
            
            // Calculate straight line distance using Haversine formula
            const distance = calculateDistance(currentLat, currentLng, milpitasCoords[0], milpitasCoords[1]);
            
            // Calculate realistic truck driving time for straight line distance
            // Add 25% extra for actual roads vs straight line, use 55 mph avg truck speed
            const actualDistance = distance * 1.25; // Account for roads not being straight
            const avgTruckSpeed = 55; // mph
            const hours = (actualDistance / avgTruckSpeed).toFixed(1);
            
            console.log(`Fallback remaining route: ${distance.toFixed(1)} miles direct, ${actualDistance.toFixed(1)} miles estimated, ${hours} hours`);
            
            remainingRoute.bindPopup(`<b>Remaining Journey</b><br>${actualDistance.toFixed(1)} miles estimated<br>ETA: ${hours} hours`);
            
            updateRouteInfo(actualDistance.toFixed(1), hours);
            
            // Fit map to show complete journey on first load
            if (firstRouteLoad) {
                const allRoutes = [driverMarker, destinationMarker];
                if (dailyTravelRoute) allRoutes.push(dailyTravelRoute);
                if (remainingRoute) allRoutes.push(remainingRoute);
                
                const group = new L.featureGroup(allRoutes);
                map.fitBounds(group.getBounds().pad(0.1));
                firstRouteLoad = false;
            }
        }
    
    // Note: Legacy function kept for compatibility - no longer used with multi-route system
    function createStraightLineRoute(currentLat, currentLng) {
        console.log('Legacy straight line route function - should not be called with multi-route system');
        // Legacy function kept for compatibility but not used in multi-route system
    }
        
        function updateRouteInfo(distance, duration) {
            // Calculate ETA based on driving schedule (8am-8pm = 12 hours daily)
            const hoursPerDay = 12; // 8am to 8pm
            const totalHours = parseFloat(duration);
            
            // Calculate arrival date/time considering driving schedule
            const now = new Date();
            let remainingHours = totalHours;
            let currentDate = new Date(now);
            
            // Start from next 8am if it's currently outside driving hours
            const currentHour = currentDate.getHours();
            if (currentHour < 8) {
                // Before 8am - start today at 8am
                currentDate.setHours(8, 0, 0, 0);
            } else if (currentHour >= 20) {
                // After 8pm - start tomorrow at 8am
                currentDate.setDate(currentDate.getDate() + 1);
                currentDate.setHours(8, 0, 0, 0);
            } else {
                // Currently in driving hours - can start driving now
                // Calculate remaining hours for today
                const hoursLeftToday = 20 - currentHour;
                if (remainingHours <= hoursLeftToday) {
                    // Can finish today
                    currentDate.setHours(currentHour + Math.ceil(remainingHours), 0, 0, 0);
                    remainingHours = 0;
                } else {
                    // Need more days
                    remainingHours -= hoursLeftToday;
                    currentDate.setDate(currentDate.getDate() + 1);
                    currentDate.setHours(8, 0, 0, 0);
                }
            }
            
            // Add full driving days for remaining hours
            while (remainingHours > 0) {
                // Skip weekends (Saturday = 6, Sunday = 0)
                if (currentDate.getDay() === 0) { // Sunday
                    currentDate.setDate(currentDate.getDate() + 1); // Move to Monday
                } else if (currentDate.getDay() === 6) { // Saturday
                    currentDate.setDate(currentDate.getDate() + 2); // Move to Monday
                }
                
                // Check if it's a weekday (Monday = 1, Friday = 5)
                if (currentDate.getDay() >= 1 && currentDate.getDay() <= 5) {
                    if (remainingHours <= hoursPerDay) {
                        // Last driving day
                        currentDate.setHours(8 + Math.ceil(remainingHours), 0, 0, 0);
                        remainingHours = 0;
                    } else {
                        // Full driving day
                        remainingHours -= hoursPerDay;
                        if (remainingHours > 0) {
                            currentDate.setDate(currentDate.getDate() + 1);
                        } else {
                            currentDate.setHours(20, 0, 0, 0); // End at 8pm
                        }
                    }
                } else {
                    // Weekend, move to next day
                    currentDate.setDate(currentDate.getDate() + 1);
                }
            }
            
            // Calculate actual days based on arrival date
            const startDate = new Date(now);
            const arrivalDate = new Date(currentDate);
            const diffTime = arrivalDate - startDate;
            const days = Math.ceil(diffTime / (1000 * 60 * 60 * 24)); // Convert milliseconds to days
            
            // Update all status elements
            document.getElementById('distance').textContent = distance;
            document.getElementById('etaHours').textContent = duration;
            document.getElementById('etaDays').textContent = days;
            document.getElementById('etaDateTime').textContent = currentDate.toLocaleDateString() + ' ' + currentDate.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
            
            console.log(`Route calculation: ${distance} miles, ${duration} hours, ${days} days, arriving ${currentDate.toLocaleString()}`);
        }
        
        function updateLocation() {
            fetch('/api/location')
                .then(response => response.json())
                .then(data => {
                    const lat = data.latitude;
                    const lng = data.longitude;
                    const currentSpeed = data.speed || 0;
                    const avgSpeed10min = data.avg_speed_10min || 0;
                    const avgSpeed30min = data.avg_speed_30min || 0;
                    const movementStatus = data.movement_status || 'unknown';
                    const statusTimestamp = data.status_timestamp;
                    const stoppedDurationSeconds = data.stopped_duration_seconds || 0;
                    const dailyTravelHours = data.daily_travel_time_hours || 0;
                    const dailyRoadDistance = data.daily_road_distance || 0;
                    const previousTravel = data.previous_travel || 'No recent travel';
                    const lastUpdate = new Date(data.last_updated * 1000);
                    const journeyHistory = data.journey_history || [];
                    
                    // Check if truck has moved significantly
                    let hasMoved = false;
                    if (lastKnownPosition) {
                        const distance = calculateDistance(lastKnownPosition.lat, lastKnownPosition.lng, lat, lng);
                        hasMoved = distance > 0.009; // More than ~50 feet
                    } else {
                        hasMoved = true; // First load
                    }
                    // --- ETA display from backend ---
                    document.getElementById('etaHours').textContent = (data.eta_hours !== null && data.eta_hours !== undefined) ? data.eta_hours : '-';
                    
                    // Calculate ETA days from eta_utc
                    if (data.eta_utc) {
                        const etaDate = new Date(data.eta_utc * 1000);
                        const now = new Date();
                        const diffTime = etaDate - now;
                        const days = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
                        document.getElementById('etaDays').textContent = days > 0 ? days : 0;
                    } else {
                        document.getElementById('etaDays').textContent = '-';
                    }
                    
                    // Show ETA as destination timezone (Milpitas, CA = Pacific Time)
                    if (data.eta_utc) {
                        const etaDate = new Date(data.eta_utc * 1000);
                        // Force Pacific timezone for destination (Milpitas, CA)
                        document.getElementById('etaDateTime').textContent = etaDate.toLocaleString('en-US', {
                            hour: '2-digit', 
                            minute:'2-digit', 
                            year: 'numeric', 
                            month: 'short', 
                            day: 'numeric', 
                            timeZone: 'America/Los_Angeles',
                            timeZoneName: 'short'
                        });
                    } else {
                        document.getElementById('etaDateTime').textContent = '-';
                    }
                    
                    // Get current location name using reverse geocoding
                    fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lng}&zoom=14&addressdetails=1`)
                        .then(response => response.json())
                        .then(locationData => {
                            const address = locationData.address || {};
                            console.log('Geocoding response:', address); // Debug log
                            
                            // Try to get the most specific location possible
                            const city = address.city || address.town || address.village || address.hamlet || 
                                        address.municipality || address.suburb || address.neighbourhood || '';
                            const county = address.county || '';
                            let state = address.state || '';
                            
                            // Convert full state names to abbreviations
                            const stateAbbreviations = {
                                'California': 'CA',
                                'Texas': 'TX',
                                'Florida': 'FL',
                                'New York': 'NY',
                                'Pennsylvania': 'PA',
                                'Illinois': 'IL',
                                'Ohio': 'OH',
                                'Georgia': 'GA',
                                'North Carolina': 'NC',
                                'Michigan': 'MI',
                                'New Jersey': 'NJ',
                                'Virginia': 'VA',
                                'Washington': 'WA',
                                'Arizona': 'AZ',
                                'Massachusetts': 'MA',
                                'Tennessee': 'TN',
                                'Indiana': 'IN',
                                'Missouri': 'MO',
                                'Maryland': 'MD',
                                'Wisconsin': 'WI',
                                'Colorado': 'CO',
                                'Minnesota': 'MN',
                                'South Carolina': 'SC',
                                'Alabama': 'AL',
                                'Louisiana': 'LA',
                                'Kentucky': 'KY',
                                'Oregon': 'OR',
                                'Oklahoma': 'OK',
                                'Connecticut': 'CT',
                                'Iowa': 'IA',
                                'Utah': 'UT',
                                'Nevada': 'NV',
                                'Arkansas': 'AR',
                                'Mississippi': 'MS',
                                'Kansas': 'KS',
                                'New Mexico': 'NM',
                                'Nebraska': 'NE',
                                'West Virginia': 'WV',
                                'Idaho': 'ID',
                                'Hawaii': 'HI',
                                'New Hampshire': 'NH',
                                'Maine': 'ME',
                                'Montana': 'MT',
                                'Rhode Island': 'RI',
                                'Delaware': 'DE',
                                'South Dakota': 'SD',
                                'North Dakota': 'ND',
                                'Alaska': 'AK',
                                'Vermont': 'VT',
                                'Wyoming': 'WY'
                            };
                            
                            // Use abbreviation if available, otherwise use original
                            if (state && stateAbbreviations[state]) {
                                state = stateAbbreviations[state];
                            }
                            
                            let currentLocation = '';
                            
                            // Priority: City first, then county if no city, then state
                            if (city) {
                                currentLocation = state ? `${city}, ${state}` : city;
                            } else if (county) {
                                // Remove "County" suffix if present
                                const cleanCounty = county.replace(/ County$/i, '');
                                currentLocation = state ? `${cleanCounty}, ${state}` : cleanCounty;
                            } else if (state) {
                                currentLocation = state;
                            } else {
                                currentLocation = 'Current Location';
                            }
                            
                            document.getElementById('currentLocation').textContent = currentLocation;
                        })
                        .catch(error => {
                            console.log('Reverse geocoding failed:', error);
                            document.getElementById('currentLocation').textContent = 'Current Location';
                        });
                    
                    // Create truck icon with ETA bubble above status bubble
                    let truckHtml = '<div style="font-size: 24px; text-align: center; line-height: 1; position: relative;">🚛</div>';
                    
                    // ETA bubble (green) - positioned above the status bubble
                    if (data.eta_hours !== null && data.eta_hours !== undefined) {
                        const etaRounded = Math.round(data.eta_hours * 10) / 10;
                        truckHtml += `<div style="text-align: center; margin-top: 4px;"><div style="display: inline-block; background: #2ecc40; color: #fff; padding: 2px 6px; border-radius: 8px; font-size: 10px; font-weight: bold; box-shadow: 0 1px 3px rgba(0,0,0,0.2); white-space: nowrap;">ETA ${etaRounded}h</div></div>`;
                    }
                    
                    // Status bubble (below ETA bubble)
                    if (movementStatus === 'moving') {
                        truckHtml += `<div class="truck-speed-label moving" style="margin-top: 2px;">${Math.round(currentSpeed)} mph</div>`;
                    } else if (movementStatus === 'stopped' && stoppedDurationSeconds > 0) {
                        const hours = Math.floor(stoppedDurationSeconds / 3600);
                        const minutes = Math.floor((stoppedDurationSeconds % 3600) / 60);
                        let stoppedText;
                        if (hours >= 1) {
                            stoppedText = `${hours}h ${minutes}m ago`;
                        } else {
                            stoppedText = `${minutes}m ago`;
                        }
                        truckHtml += `<div class="truck-speed-label stopped" style="margin-top: 2px;">Stopped ${stoppedText}</div>`;
                    } else {
                        truckHtml += `<div class="truck-speed-label stopped" style="margin-top: 2px;">Stopped</div>`;
                    }
                    
                    // Update truck marker position (always update marker even if not redrawing route)
                    if (driverMarker) {
                        driverMarker.setLatLng([lat, lng]);
                        
                        // Center map on vehicle while preserving user's zoom level (after first load)
                        if (!firstRouteLoad) {
                            const currentZoom = map.getZoom();
                            isAutoPositioning = true; // Flag to prevent saving this auto-move
                            map.setView([lat, lng], currentZoom);
                            setTimeout(() => { isAutoPositioning = false; }, 100); // Reset flag after move
                            console.log(`🎯 Centered map on vehicle at ${lat}, ${lng} with zoom ${currentZoom}`);
                        }
                        
                        // Update the icon with speed overlay
                        const updatedIcon = L.divIcon({
                            html: truckHtml,
                            className: 'truck-marker',
                            iconSize: [30, 30],
                            iconAnchor: [15, 15]
                        });
                        driverMarker.setIcon(updatedIcon);
                    } else {
                        const truckIcon = L.divIcon({
                            html: truckHtml,
                            className: 'truck-marker',
                            iconSize: [30, 30],
                            iconAnchor: [15, 15]
                        });
                        
                        driverMarker = L.marker([lat, lng], {icon: truckIcon})
                            .addTo(map)
                            .bindPopup(`<b>Truck Location</b><br>Speed: ${Math.round(currentSpeed)} mph<br>Moving to Milpitas, CA`);
                        hasMoved = true; // Force route update on first marker creation
                    }
                    
                    // Update popup with current speed and status
                    let popupContent = `<b>Truck Location</b><br>Speed: ${Math.round(currentSpeed)} mph`;
                    if (movementStatus === 'moving') {
                        popupContent += `<br>Status: Moving<br>10-min avg: ${Math.round(avgSpeed10min)} mph`;
                    } else {
                        const hours = Math.floor(stoppedDurationSeconds / 3600);
                        const minutes = Math.floor((stoppedDurationSeconds % 3600) / 60);
                        if (hours >= 1) {
                            popupContent += `<br>Status: Stopped ${hours}h ${minutes}m ago`;
                        } else {
                            popupContent += `<br>Status: Stopped ${minutes}m ago`;
                        }
                    }
                    popupContent += `<br>Moving to Milpitas, CA`;
                    driverMarker.getPopup().setContent(popupContent);
                    
                    // Only update route if truck has moved significantly OR this is the first load
                    if (hasMoved || !lastKnownPosition) {
                        console.log('Truck moved or first load - updating route');
                        updateRoute(lat, lng, data.daily_start_location, journeyHistory);
                        lastKnownPosition = {lat: lat, lng: lng};
                    } else {
                        console.log('Truck stationary - but checking if route exists');
                        // If no route exists yet, draw it anyway
                        if (!dailyTravelRoute && !remainingRoute) {
                            console.log('No route drawn yet - drawing initial route');
                            updateRoute(lat, lng, data.daily_start_location, journeyHistory);
                        }
                    }
                    
                    // Update status with speed and movement info - show local time with timezone
                    const localTimeWithTZ = lastUpdate.toLocaleTimeString('en-US', {
                        hour: 'numeric',
                        minute: '2-digit',
                        timeZoneName: 'short'
                    });
                    document.getElementById('lastUpdate').textContent = localTimeWithTZ;
                    document.getElementById('speed').textContent = Math.round(currentSpeed);
                    
                    // Show status with 10-minute average when moving or stopped timestamp
                    let statusText;
                    if (movementStatus === 'moving') {
                        if (avgSpeed10min > 0) {
                            statusText = `Moving - 10min avg: ${Math.round(avgSpeed10min)} mph`;
                        } else {
                            const startTime = new Date(statusTimestamp * 1000);
                            statusText = `Moving (${startTime.toLocaleDateString()} ${startTime.toLocaleTimeString()})`;
                        }
                    } else if (movementStatus === 'stopped') {
                        const stopTime = new Date(statusTimestamp * 1000);
                        statusText = `Stopped (${stopTime.toLocaleDateString()} ${stopTime.toLocaleTimeString()})`;
                    } else {
                        // Fallback
                        statusText = Math.round(currentSpeed) > 0 ? 'Moving' : 'Stopped';
                    }
                    document.getElementById('status').textContent = statusText;
                    
                    // Update new metrics
                    document.getElementById('avg30min').textContent = avgSpeed30min > 0 ? Math.round(avgSpeed30min) : '-';
                    document.getElementById('travelTime').textContent = dailyTravelHours.toFixed(1);
                    document.getElementById('travelDistance').textContent = Math.round(dailyRoadDistance);
                    document.getElementById('prevTravel').textContent = previousTravel;
                    document.getElementById('journeyAvg').textContent = data.journey_average || 'N/A';
                    
                    console.log('Location updated:', lat, lng, 'Speed:', currentSpeed, 'mph', 'Status:', movementStatus, 'Travel time:', dailyTravelHours, 'hrs', 'Road distance:', dailyRoadDistance, 'mi', 'Previous:', previousTravel);
                })
                .catch(error => {
                    console.error('Error fetching location:', error);
                    document.getElementById('status').textContent = 'Error';
                });
        }
        
        function startCountdown() {
            let seconds = 60;
            countdownTimer = setInterval(() => {
                seconds--;
                document.getElementById('countdown').textContent = seconds;
                
                if (seconds <= 0) {
                    updateLocation();
                    seconds = 60;
                }
            }, 1000);
        }
        
        // Initialize everything
        document.addEventListener('DOMContentLoaded', function() {
            console.log('🚀 DOM loaded - initializing app');
            initMap();
            updateLocation();
            startCountdown();
            
            // Force initial route draw after a short delay
            setTimeout(function() {
                console.log('🔄 Forcing initial route update');
                updateLocation();
            }, 2000);
        });
    </script>
</body>
</html>
    """)

def calculate_journey_average():
    """Calculate average daily miles/hours from journey start (Sept 15) to current day"""
    try:
        # Journey start date
        start_date = date(2025, 9, 15)
        current_date = date.today()
        
        # Get start location from Sept 15
        start_file = os.path.join(DATA_DIR, "daily", f"daily_{start_date.isoformat()}.json")
        if not os.path.exists(start_file):
            return "N/A"
        
        with open(start_file, 'r') as f:
            start_day_data = json.load(f)
        
        start_location = start_day_data.get('start_location')
        if not start_location:
            return "N/A"
        
        # Get current location from today's data or current location
        today_data = get_current_day_data()
        if today_data and today_data.get('end_location'):
            current_location = today_data['end_location']
        else:
            # Use current live location
            driver_data = location_data["driver"]
            current_location = {
                'latitude': driver_data['latitude'],
                'longitude': driver_data['longitude']
            }
        
        # Calculate total distance from start to current location
        from math import radians, cos, sin, asin, sqrt
        
        def haversine(lat1, lon1, lat2, lon2):
            # Convert to radians
            lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
            
            # Haversine formula
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
            c = 2 * asin(sqrt(a))
            
            # Earth radius in miles
            r = 3956
            return c * r
        
        total_distance = haversine(
            start_location['latitude'], start_location['longitude'],
            current_location['latitude'], current_location['longitude']
        )
        
        # Calculate total driving hours from daily_stats.json
        total_hours = 0
        if os.path.exists(DAILY_STATS_FILE):
            with open(DAILY_STATS_FILE, 'r') as f:
                daily_stats = json.load(f)
            
            for day_key, day_data in daily_stats.items():
                if day_key >= start_date.isoformat():
                    # Use total_travel_time (in seconds) and convert to hours
                    travel_time_seconds = day_data.get('total_travel_time', 0)
                    total_hours += travel_time_seconds / 3600
        
        # Calculate averages
        days_elapsed = (current_date - start_date).days + 1  # Include current day
        
        if total_hours > 0:
            avg_miles_per_hour = total_distance / total_hours
            avg_miles_per_day = total_distance / days_elapsed
            avg_hours_per_day = total_hours / days_elapsed
            
            return f"{avg_miles_per_hour:.0f} mph ({avg_miles_per_day:.0f}mi/{avg_hours_per_day:.1f}h daily)"
        else:
            return "N/A"
            
    except Exception as e:
        print(f"Error calculating journey average: {e}")
        return "N/A"

@app.route('/api/location')
def get_location():
    # Always fetch the latest truck location on every request
    fetch_real_location()
    driver_data = location_data["driver"]

    # Get current speed (1-min priority, 5-min fallback for vehicle icon)
    current_speed = round(driver_data["speed"])

    # Get 10-minute average speed when moving (for status bar)
    avg_speed_10min = round(calculate_average_moving_speed(10))

    # Get 30-minute average speed (persistent data)
    avg_speed_30min = round(calculate_average_moving_speed(30))

    # Get 1-hour average speed for better overall tracking (using enhanced history)
    avg_speed_1hour = round(calculate_average_moving_speed(60))

    # Get movement status (5-minute detection window)
    status, timestamp = get_movement_status()

    # Get stopped duration in seconds
    stopped_duration_seconds = get_stopped_duration()

    # NEW: Get current day data from daily file
    today_data = get_current_day_data()
    today_distance = today_data.get('total_distance_miles', 0) if today_data else 0
    today_travel_time = 0
    if today_data and today_data.get('start_time') and today_data.get('end_time'):
        today_travel_time = (today_data['end_time'] - today_data['start_time']) / 3600

    # Get previous day stats
    prev_day_stats = get_previous_day_stats()
    previous_travel = "No recent travel"
    if prev_day_stats:
        prev_date = datetime.fromisoformat(prev_day_stats['date']).strftime('%m/%d')
        prev_hours = round(prev_day_stats['travel_time_hours'])
        prev_miles = round(prev_day_stats['distance_miles'])
        previous_travel = f"{prev_date} ({prev_hours}h, {prev_miles}mi)"

    # Calculate journey average
    journey_average = calculate_journey_average()

    # Build journey history using current day data
    journey_history = []
    if today_data and today_data.get('minute_locations'):
        # Sample the minute locations for visualization
        locations = today_data['minute_locations']
        step = max(1, len(locations) // 50)  # Limit to ~50 points

        for i in range(0, len(locations), step):
            loc = locations[i]
            journey_history.append({
                "lat": loc['latitude'],
                "lng": loc['longitude'],
                "timestamp": loc['timestamp']
            })

        # Always include the latest point
        if locations and journey_history[-1]['timestamp'] != locations[-1]['timestamp']:
            last_loc = locations[-1]
            journey_history.append({
                "lat": last_loc['latitude'],
                "lng": last_loc['longitude'],
                "timestamp": last_loc['timestamp']
            })

    # Get daily start location from today's data
    daily_start_location = None
    if today_data and today_data.get('start_location'):
        daily_start_location = today_data['start_location']


    # --- ETA calculation using OSRM ---
    DEST_LAT, DEST_LNG = 37.4419, -121.9080  # Milpitas, CA
    osrm_url = f"http://router.project-osrm.org/route/v1/driving/{driver_data['longitude']},{driver_data['latitude']};{DEST_LNG},{DEST_LAT}?overview=false&alternatives=false&steps=false"
    osrm_eta_hours = None
    osrm_distance_miles = None
    import time as _time
    try:
        osrm_resp = requests.get(osrm_url, timeout=5)
        if osrm_resp.status_code == 200:
            osrm_data = osrm_resp.json()
            if osrm_data.get('routes') and len(osrm_data['routes']) > 0:
                route = osrm_data['routes'][0]
                osrm_eta_hours = route['duration'] / 3600.0
                osrm_distance_miles = route['distance'] / 1609.34
    except Exception as e:
        print(f"❌ OSRM ETA error: {e}")
    eta_hours = osrm_eta_hours
    road_distance = osrm_distance_miles
    eta_utc = int(_time.time() + eta_hours * 3600) if eta_hours is not None else None
    return {
        "latitude": driver_data["latitude"],
        "longitude": driver_data["longitude"],
        "last_updated": driver_data["last_updated"],
        "speed": current_speed,
        "avg_speed_10min": avg_speed_10min,
        "avg_speed_30min": avg_speed_30min,
        "avg_speed_1hour": avg_speed_1hour,
        "movement_status": status,
        "status_timestamp": timestamp,
        "stopped_duration_seconds": stopped_duration_seconds,
        "today_mileage": round(today_distance, 1),
        "daily_travel_time_hours": round(today_travel_time, 1),
    "daily_road_distance": round(today_distance, 1),
    "osrm_road_distance": round(road_distance, 1) if road_distance is not None else None,
        "daily_straight_distance": 0,
        "daily_start_location": daily_start_location,
        "daily_end_location": today_data.get('end_location') if today_data else None,
        "first_movement_time": today_data.get('summary', {}).get('first_movement_time') if today_data else None,
        "last_movement_time": today_data.get('summary', {}).get('last_movement_time') if today_data else None,
        "actual_day_duration_hours": round(today_travel_time, 1),
        "previous_travel": previous_travel,
        "journey_average": journey_average,
        "journey_history": journey_history,
        "stopped_since": driver_data["stopped_since"],
    "eta_utc": eta_utc,
    "eta_hours": round(eta_hours, 1) if eta_hours is not None else None
    }

# Initialize application (for both local and production)
def initialize_app():
    """Initialize the application data and background processes"""
    # Initialize data directory and load existing data
    os.makedirs(DATA_DIR, exist_ok=True)
    
    # Load historical location data
    historical_data = load_historical_data()
    if historical_data:
        location_data["history"] = historical_data[-10:]  # Keep last 10 points in memory
        print(f"📂 Loaded {len(historical_data)} historical location points")
    
    # Load daily stats (legacy system, being phased out)
    daily_stats = load_daily_stats()
    print(f"📊 Loaded daily stats for {len(daily_stats)} days")
    
    # Initialize daily file system
    initialize_daily_files_on_startup()
    
    # Start background thread for real location updates
    location_thread = threading.Thread(target=update_location_periodically, daemon=True)
    location_thread.start()
    
    print("🚛 Truck Tracker initialized with REAL SuperDispatch location...")

# Initialize the app when module is imported (for production)
initialize_app()

if __name__ == '__main__':
    print("📍 Open: http://localhost:8080")
    app.run(debug=True, host='0.0.0.0', port=8080, use_reloader=False)
