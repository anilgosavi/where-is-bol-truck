#!/bin/bash
# Build script for Render.com deployment

echo "🚀 Starting build for Where is BOL Truck..."

# Install Python dependencies
echo "📦 Installing Python dependencies..."
pip install -r render-requirements.txt

# Create data directory structure
echo "📁 Creating data directory structure..."
mkdir -p truck_data/daily

# Create initial empty data files if they don't exist
echo "📄 Initializing data files..."
if [ ! -f truck_data/location_history.json ]; then
    echo '[]' > truck_data/location_history.json
fi

if [ ! -f truck_data/daily_stats.json ]; then
    echo '{}' > truck_data/daily_stats.json
fi

if [ ! -f truck_data/last_location.json ]; then
    echo '{"latitude": 35.779, "longitude": -78.638, "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' > truck_data/last_location.json
fi

echo "✅ Build completed successfully!"
