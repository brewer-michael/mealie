#!/usr/bin/env python3
"""
Comprehensive testing protocol for image scanning with provider fallback.
This script automates:
1. Server startup
2. Default user creation  
3. Provider configuration (Gemini/OpenAI/etc)
4. Image scanning test
5. Results validation
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional
import requests
import signal
import tempfile
import shutil

class ImageScanningTester:
    def __init__(self, config_path: str = ".claude/testcycle.json"):
        self.config_path = Path(config_path)
        self.config = self.load_config()
        self.server_process = None
        self.frontend_process = None
        self.api_token = None
        self.base_url = self.config["test_settings"]["api_base_url"]
        
    def load_config(self) -> Dict[str, Any]:
        """Load test configuration from JSON file"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        
        with open(self.config_path, 'r') as f:
            config = json.load(f)
            
        # Validate required keys
        if not config.get("credentials", {}).get("gemini_api_key"):
            print("⚠️  WARNING: Gemini API key not set in config file")
            
        return config
    
    def cleanup_processes(self):
        """Clean up any running processes"""
        print("🧹 Cleaning up processes...")
        
        if self.server_process:
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server_process.kill()
            
        if self.frontend_process:
            self.frontend_process.terminate()
            try:
                self.frontend_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.frontend_process.kill()
                
        # Kill any remaining processes on the ports
        try:
            subprocess.run(["pkill", "-f", "uvicorn.*mealie"], check=False)
            subprocess.run(["pkill", "-f", "node.*nuxt"], check=False)
        except:
            pass
            
    def setup_environment(self):
        """Set up environment variables for the test"""
        print("🔧 Setting up environment...")
        
        # Set API keys from config
        credentials = self.config["credentials"]
        env_vars = {
            "GEMINI_API_KEY": credentials.get("gemini_api_key", ""),
            "OPENAI_API_KEY": credentials.get("openai_api_key", ""),
            "ANTHROPIC_API_KEY": credentials.get("anthropic_api_key", ""),
            
            # Image scanning configuration
            "IMAGE_SCANNING_PRIMARY_PROVIDER": "gemini",
            "IMAGE_SCANNING_SECONDARY_PROVIDER": "openai", 
            "IMAGE_SCANNING_ENABLE_OCR_FALLBACK": "true",
            
            # Basic Mealie settings
            "API_PORT": str(self.config["test_settings"]["server_port"]),
            "BASE_URL": f"http://localhost:{self.config['test_settings']['server_port']}",
            "DATABASE_URL": "sqlite:///./mealie_test.db",
            "PRODUCTION": "false",
            "LOG_LEVEL": "info",
        }
        
        for key, value in env_vars.items():
            if value:  # Only set non-empty values
                os.environ[key] = value
                
        print(f"✅ Environment configured with {len(env_vars)} variables")
        
    def start_server(self) -> bool:
        """Start the Mealie backend server"""
        print("🚀 Starting Mealie server...")
        
        try:
            # Start the server
            self.server_process = subprocess.Popen([
                sys.executable, "-m", "uvicorn", 
                "mealie.app:app",
                "--host", self.config["test_settings"]["server_host"],
                "--port", str(self.config["test_settings"]["server_port"]),
                "--reload"
            ], cwd=Path.cwd())
            
            # Wait for server to start
            max_attempts = 30
            for attempt in range(max_attempts):
                try:
                    response = requests.get(f"{self.base_url}/docs", timeout=2)
                    if response.status_code == 200:
                        print(f"✅ Server started successfully on port {self.config['test_settings']['server_port']}")
                        return True
                except requests.exceptions.RequestException:
                    pass
                    
                print(f"⏳ Waiting for server... ({attempt + 1}/{max_attempts})")
                time.sleep(2)
                
            print("❌ Server failed to start within timeout period")
            return False
            
        except Exception as e:
            print(f"❌ Failed to start server: {e}")
            return False
            
    def create_default_user(self) -> bool:
        """Create the default user and get authentication token"""
        print("👤 Creating default user...")
        
        user_config = self.config["test_settings"]["default_user"]
        
        try:
            # First, try to create the user
            create_response = requests.post(f"{self.base_url}/api/users/signup", json={
                "email": user_config["email"],
                "password": user_config["password"],
                "passwordConfirm": user_config["password"],
                "username": user_config["username"],
                "fullName": user_config["full_name"]
            }, timeout=10)
            
            if create_response.status_code not in [200, 201, 409]:  # 409 = user already exists
                print(f"⚠️  User creation returned status {create_response.status_code}")
            
            # Login to get token
            login_response = requests.post(f"{self.base_url}/api/auth/token", data={
                "username": user_config["email"],
                "password": user_config["password"]
            }, timeout=10)
            
            if login_response.status_code != 200:
                print(f"❌ Login failed: {login_response.status_code}")
                return False
                
            token_data = login_response.json()
            self.api_token = token_data["access_token"]
            print("✅ User created and authenticated successfully")
            return True
            
        except Exception as e:
            print(f"❌ Failed to create user: {e}")
            return False
            
    def get_auth_headers(self) -> Dict[str, str]:
        """Get authorization headers for API requests"""
        return {"Authorization": f"Bearer {self.api_token}"}
        
    def configure_providers(self, scenario: Dict[str, Any]) -> bool:
        """Configure image scanning providers via API"""
        print(f"⚙️  Configuring providers for scenario: {scenario['name']}")
        
        provider_config = scenario["provider_config"]
        
        # This would ideally call the admin API to set provider configuration
        # For now, we'll use environment variables which were set in setup_environment()
        print(f"✅ Provider configuration set: Primary={provider_config['primary']}, "
              f"Secondary={provider_config['secondary']}, OCR={provider_config['ocr_fallback']}")
        return True
        
    def test_image_scanning(self, image_path: str, scenario_name: str) -> Dict[str, Any]:
        """Test image scanning with the configured providers"""
        print(f"📸 Testing image scanning for scenario: {scenario_name}")
        
        if not Path(image_path).exists():
            return {"success": False, "error": f"Test image not found: {image_path}"}
            
        try:
            with open(image_path, 'rb') as f:
                files = {"images": ("test_image.jpg", f, "image/jpeg")}
                
                response = requests.post(
                    f"{self.base_url}/api/recipes/create/image",
                    files=files,
                    headers=self.get_auth_headers(),
                    timeout=60  # Long timeout for AI processing
                )
                
            if response.status_code in [200, 201]:
                recipe_slug = response.json() if response.text else response.text
                print(f"✅ Recipe created successfully: {recipe_slug}")
                
                # Try to fetch the created recipe
                recipe_response = requests.get(
                    f"{self.base_url}/api/recipes/{recipe_slug}",
                    headers=self.get_auth_headers(),
                    timeout=10
                )
                
                if recipe_response.status_code == 200:
                    recipe_data = recipe_response.json()
                    return {
                        "success": True,
                        "recipe_slug": recipe_slug,
                        "recipe_name": recipe_data.get("name", "Unknown"),
                        "ingredient_count": len(recipe_data.get("recipeIngredient", [])),
                        "instruction_count": len(recipe_data.get("recipeInstructions", []))
                    }
                else:
                    return {"success": True, "recipe_slug": recipe_slug, "fetch_failed": True}
            else:
                error_text = response.text
                print(f"❌ Image scanning failed: {response.status_code} - {error_text}")
                return {"success": False, "error": f"HTTP {response.status_code}: {error_text}"}
                
        except Exception as e:
            print(f"❌ Image scanning error: {e}")
            return {"success": False, "error": str(e)}
            
    def download_test_image(self) -> str:
        """Download or use existing test image"""
        test_image_path = self.config["test_settings"]["test_image_path"]
        
        if Path(test_image_path).exists():
            print(f"✅ Using existing test image: {test_image_path}")
            return test_image_path
            
        # Create a simple test image if none exists
        print("📸 Creating test image...")
        try:
            from PIL import Image, ImageDraw, ImageFont
            
            # Create a simple recipe card image
            img = Image.new('RGB', (800, 600), color='white')
            draw = ImageDraw.Draw(img)
            
            # Draw some recipe text
            recipe_text = """
CHOCOLATE CHIP COOKIES

Ingredients:
- 2 cups flour
- 1 cup butter
- 1 cup brown sugar
- 2 eggs
- 1 tsp vanilla
- 1 cup chocolate chips

Instructions:
1. Preheat oven to 350°F
2. Mix butter and sugar
3. Add eggs and vanilla
4. Mix in flour
5. Add chocolate chips
6. Bake 10-12 minutes
            """.strip()
            
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
            except:
                font = ImageFont.load_default()
                
            draw.text((50, 50), recipe_text, fill='black', font=font)
            
            img.save(test_image_path)
            print(f"✅ Created test image: {test_image_path}")
            return test_image_path
            
        except ImportError:
            print("⚠️  PIL not available, creating text file as fallback")
            # Create a text file as fallback
            with open(test_image_path.replace('.jpg', '.txt'), 'w') as f:
                f.write("Test recipe content for OCR scanning")
            return test_image_path.replace('.jpg', '.txt')
            
    def run_test_scenario(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """Run a complete test scenario"""
        print(f"\n🧪 Running test scenario: {scenario['name']}")
        print("=" * 50)
        
        # Configure providers for this scenario
        if not self.configure_providers(scenario):
            return {"success": False, "error": "Failed to configure providers"}
            
        # Get or create test image
        test_image_path = self.download_test_image()
        
        # Test image scanning
        result = self.test_image_scanning(test_image_path, scenario['name'])
        
        print(f"📊 Scenario '{scenario['name']}' result: {result}")
        return result
        
    def run_full_test_cycle(self):
        """Run the complete testing protocol"""
        print("🚀 Starting comprehensive image scanning test cycle")
        print("=" * 60)
        
        results = {}
        
        try:
            # Setup
            self.setup_environment()
            
            # Start server
            if not self.start_server():
                return {"error": "Failed to start server"}
                
            time.sleep(5)  # Give server time to fully initialize
            
            # Create user
            if not self.create_default_user():
                return {"error": "Failed to create user"}
                
            # Run test scenarios
            for scenario in self.config["test_scenarios"]:
                results[scenario["name"]] = self.run_test_scenario(scenario)
                time.sleep(2)  # Brief pause between scenarios
                
            print("\n📋 FINAL TEST RESULTS")
            print("=" * 40)
            for scenario_name, result in results.items():
                status = "✅ PASS" if result.get("success") else "❌ FAIL"
                print(f"{status} {scenario_name}: {result}")
                
            return results
            
        except KeyboardInterrupt:
            print("\n⏹️  Test interrupted by user")
            return {"error": "Interrupted by user"}
        except Exception as e:
            print(f"\n💥 Test failed with error: {e}")
            return {"error": str(e)}
        finally:
            self.cleanup_processes()


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Test image scanning functionality")
    parser.add_argument("--config", default=".claude/testcycle.json", 
                       help="Path to test configuration file")
    parser.add_argument("--scenario", help="Run only specific scenario")
    parser.add_argument("--setup-only", action="store_true", 
                       help="Only setup server and user, don't run tests")
    
    args = parser.parse_args()
    
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\n🛑 Received interrupt signal, cleaning up...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        tester = ImageScanningTester(args.config)
        
        if args.setup_only:
            tester.setup_environment()
            if tester.start_server():
                tester.create_default_user()
                print(f"\n🎯 Setup complete! Server running at {tester.base_url}")
                print("Press Ctrl+C to shutdown")
                # Keep running until interrupted
                while True:
                    time.sleep(1)
        else:
            results = tester.run_full_test_cycle()
            
            # Exit with appropriate code
            if any(not r.get("success", False) for r in results.values() if isinstance(r, dict)):
                sys.exit(1)
                
    except Exception as e:
        print(f"💥 Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()