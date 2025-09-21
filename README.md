# 🚛 Truck Tracker - Real-time GPS Tracking

A comprehensive truck tracking application with real-time GPS updates, journey analytics, and smart ETA calculations.

## ✨ Features

- 🚛 **Real-time GPS Tracking** - Live location updates via SuperDispatch API
- 📊 **Journey Analytics** - Average speed and daily performance metrics  
- ⏱️ **Smart ETA** - 12-hour driving schedule with weekend awareness
- 📈 **Historical Data** - Complete 5-day cross-country journey visualization
- 🗺️ **Interactive Map** - Real-time route display with truck icon

## 🚀 Quick Deploy to Render.com

This app is ready for immediate deployment to Render.com:

1. **Fork this repository** to your GitHub account
2. **Connect to Render.com** and create a Blueprint
3. **Deploy automatically** using the included `render.yaml`

See `DEPLOYMENT.md` for detailed instructions.

## 📊 Current Journey Data

- **Route:** Cary, NC → Milpitas, CA (1,937 miles)
- **Average Performance:** 37 mph (387mi/10.6h daily)
- **Current Status:** Real-time tracking with 12-hour driving schedule
- **ETA Calculation:** Smart scheduling (8am-8pm) with weekend handling

## 🔧 Local Development

```bash
# Install dependencies
pip install -r render-requirements.txt

# Run the application  
python simple_app_fixed.py

# Access at http://localhost:8080
```

## 📁 Project Structure

```
├── simple_app_fixed.py      # Main application
├── wsgi.py                  # Production WSGI entry point
├── render.yaml              # Render.com deployment config
├── render-requirements.txt  # Production dependencies
├── build.sh                 # Build script for deployment
├── truck_data/              # Application data
│   ├── location_history.json
│   ├── daily_stats.json
│   ├── last_location.json
│   └── daily/               # Per-day tracking files
└── DEPLOYMENT.md            # Deployment instructions
```

## 🌐 API Endpoints

- `GET /` - Main tracking interface
- `GET /api/location` - Current location and journey data
- `GET /api/journey_average` - Performance metrics
- `GET /health` - Health check endpoint

## 📈 Data Features

- **Real-time Tracking:** GPS updates every 60 seconds
- **Journey Average:** Distance and time from start date (Sept 15, 2025)
- **Smart ETA:** Realistic arrival times with driving hour limits
- **Historical Visualization:** Complete journey replay and analytics

Built with Flask, JavaScript, and real-time GPS integration.
