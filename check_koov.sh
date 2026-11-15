#!/bin/bash
# Quick diagnostic script for Koov

echo "=== Koov Diagnostic ==="
echo ""

echo "1. Checking if API server is running on port 5000..."
if lsof -i :5000 > /dev/null 2>&1; then
    echo "✅ Port 5000 is in use (server might be running)"
    lsof -i :5000 | grep Python
else
    echo "❌ Port 5000 is not in use - API server is NOT running"
    echo "   Start it with: ./START_SERVER.sh"
fi

echo ""
echo "2. Testing API server health endpoint..."
if curl -s http://127.0.0.1:5000/health > /dev/null 2>&1; then
    echo "✅ API server is responding"
    curl -s http://127.0.0.1:5000/health | python3 -m json.tool 2>/dev/null || curl -s http://127.0.0.1:5000/health
else
    echo "❌ API server is NOT responding"
    echo "   The server might be running but not working properly"
fi

echo ""
echo "3. Checking current status..."
if curl -s http://127.0.0.1:5000/api/status > /dev/null 2>&1; then
    echo "✅ Status endpoint is working"
    curl -s http://127.0.0.1:5000/api/status | python3 -m json.tool 2>/dev/null || curl -s http://127.0.0.1:5000/api/status
else
    echo "❌ Status endpoint is NOT working"
fi

echo ""
echo "4. Checking .env file..."
if [ -f .env ]; then
    echo "✅ .env file exists"
    if grep -q "KOOV_INPUT_DEVICE" .env; then
        echo "✅ Audio devices configured"
        grep "KOOV_INPUT_DEVICE\|KOOV_OUTPUT_DEVICE" .env | head -2
    else
        echo "⚠️  Audio devices not configured in .env"
    fi
else
    echo "❌ .env file not found"
fi

echo ""
echo "5. Checking Vosk model..."
if [ -d "models/vosk-model-small-en-us-0.15" ]; then
    echo "✅ Vosk model found"
else
    echo "❌ Vosk model not found"
    echo "   Run: python3 scripts/download_vosk_model.py --out models"
fi

echo ""
echo "=== Diagnostic Complete ==="
echo ""
echo "Next steps:"
echo "1. If API server is not running, start it: ./START_SERVER.sh"
echo "2. Check the API server terminal for errors"
echo "3. Try joining a meeting from your dashboard"
echo "4. Look for 'Koov dormant. Wake phrase: computer' in the logs"
