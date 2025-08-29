#!/usr/bin/env python3
"""
Quick test script for image scanning - simpler version for immediate testing
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
import requests

def load_config():
    """Load test configuration"""
    config_path = Path(".claude/testcycle.json")
    if not config_path.exists():
        print("❌ Config file not found. Please fill out .claude/testcycle.json with your API keys")
        return None
    
    with open(config_path) as f:
        return json.load(f)

def setup_environment(config):
    """Set up environment variables"""
    print("🔧 Setting up environment...")
    
    credentials = config["credentials"]
    os.environ.update({
        "GEMINI_API_KEY": credentials.get("gemini_api_key", ""),
        "OPENAI_API_KEY": credentials.get("openai_api_key", ""),
        "IMAGE_SCANNING_PRIMARY_PROVIDER": "gemini",
        "IMAGE_SCANNING_SECONDARY_PROVIDER": "openai",
        "IMAGE_SCANNING_ENABLE_OCR_FALLBACK": "true",
        "API_PORT": "9000",
        "BASE_URL": "http://localhost:9000",
        "PRODUCTION": "false",
    })

def start_server():
    """Start the server and wait for it to be ready"""
    print("🚀 Starting server...")
    
    process = subprocess.Popen([
        sys.executable, "-m", "uvicorn", "mealie.app:app",
        "--host", "0.0.0.0", "--port", "9000", "--reload"
    ])
    
    # Wait for server to start
    for i in range(30):
        try:
            response = requests.get("http://localhost:9000/api/docs", timeout=2)
            if response.status_code == 200:
                print("✅ Server is ready!")
                return process
        except:
            pass
        print(f"⏳ Waiting for server... ({i+1}/30)")
        time.sleep(2)
    
    print("❌ Server failed to start")
    process.terminate()
    return None

def create_test_user():
    """Create test user and get token"""
    print("👤 Creating test user...")
    
    # Create user
    requests.post("http://localhost:9000/api/users/signup", json={
        "email": "test@example.com",
        "password": "password123",
        "passwordConfirm": "password123", 
        "username": "testuser",
        "fullName": "Test User"
    })
    
    # Login
    response = requests.post("http://localhost:9000/api/auth/token", data={
        "username": "test@example.com",
        "password": "password123"
    })
    
    if response.status_code == 200:
        token = response.json()["access_token"]
        print("✅ User created and logged in")
        return token
    else:
        print(f"❌ Failed to login: {response.status_code}")
        return None

def test_image_upload(token, image_path):
    """Test uploading an image for recipe scanning"""
    print(f"📸 Testing image upload: {image_path}")
    
    if not Path(image_path).exists():
        print(f"❌ Image not found: {image_path}")
        return False
    
    with open(image_path, 'rb') as f:
        files = {"images": ("test.jpg", f, "image/jpeg")}
        headers = {"Authorization": f"Bearer {token}"}
        
        response = requests.post(
            "http://localhost:9000/api/recipes/create/image",
            files=files,
            headers=headers,
            timeout=120
        )
    
    if response.status_code in [200, 201]:
        print(f"✅ Recipe created: {response.text}")
        return True
    else:
        print(f"❌ Failed to create recipe: {response.status_code} - {response.text}")
        return False

def main():
    """Main test function"""
    print("🧪 Quick Image Scanning Test")
    print("=" * 40)
    
    # Load config
    config = load_config()
    if not config:
        return
    
    # Check for API keys
    if not config["credentials"].get("gemini_api_key") or config["credentials"]["gemini_api_key"] == "YOUR_GEMINI_API_KEY_HERE":
        print("⚠️  Please set your Gemini API key in .claude/testcycle.json")
        return
    
    setup_environment(config)
    
    # Start server
    server_process = start_server()
    if not server_process:
        return
    
    try:
        # Create user and get token
        token = create_test_user()
        if not token:
            return
        
        # Test image upload
        test_image = config["test_settings"]["test_image_path"]
        success = test_image_upload(token, test_image)
        
        if success:
            print("\n🎉 Test completed successfully!")
        else:
            print("\n❌ Test failed")
        
        print(f"\n🌐 Server is running at: http://localhost:9000")
        print("📖 API docs at: http://localhost:9000/docs")
        print("Press Ctrl+C to stop")
        
        # Keep server running
        server_process.wait()
        
    except KeyboardInterrupt:
        print("\n🛑 Stopping server...")
    finally:
        server_process.terminate()

if __name__ == "__main__":
    main()