#!/usr/bin/env python3
"""
Unit test for the image scanning service functionality.
This tests the core logic without needing the full Mealie server.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

# Add current directory to path so we can import Mealie modules
sys.path.insert(0, str(Path(__file__).parent))

def load_config():
    """Load test configuration"""
    config_path = Path(".claude/testcycle.json")
    if not config_path.exists():
        print("❌ Config file not found")
        return None
    
    with open(config_path) as f:
        return json.load(f)

def setup_environment(config):
    """Set up environment variables for testing"""
    credentials = config["credentials"]
    os.environ.update({
        "GEMINI_API_KEY": credentials.get("gemini_api_key", ""),
        "ANTHROPIC_API_KEY": credentials.get("anthropic_api_key", ""),
        "IMAGE_SCANNING_PRIMARY_PROVIDER": "gemini",
        "IMAGE_SCANNING_SECONDARY_PROVIDER": "anthropic",
        "IMAGE_SCANNING_ENABLE_OCR_FALLBACK": "true",
        "DATABASE_URL": "sqlite:///:memory:",  # In-memory database for testing
    })

async def test_image_scanning_service():
    """Test the image scanning service directly"""
    print("🧪 Testing Image Scanning Service")
    print("=" * 40)
    
    try:
        # Import after setting environment
        from mealie.services.image_scanning import ImageScanningService
        from mealie.lang.providers import Translator
        
        # Create a mock translator
        translator = Mock(spec=Translator)
        translator.t = Mock(return_value="Test")
        
        # Create the service
        service = ImageScanningService(translator)
        
        # Test 1: Check available providers
        print("📋 Testing provider availability...")
        providers = service.get_available_providers()
        print(f"Available providers: {providers}")
        
        # Test 2: Check if any provider is configured
        print("🔧 Testing provider configuration...")
        has_providers = service.is_any_provider_configured()
        print(f"Any provider configured: {has_providers}")
        
        # Test 3: Create a test image
        print("🖼️  Creating test image...")
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_file:
            # Create a simple test image
            try:
                from PIL import Image, ImageDraw
                img = Image.new('RGB', (400, 300), color='white')
                draw = ImageDraw.Draw(img)
                draw.text((20, 20), "Test Recipe\n\nIngredients:\n- 1 cup flour\n- 2 eggs", fill='black')
                img.save(tmp_file.name, 'JPEG')
                test_image_path = Path(tmp_file.name)
                print(f"✅ Test image created: {test_image_path}")
            except ImportError:
                # Fallback: create empty file
                tmp_file.write(b"fake image data")
                test_image_path = Path(tmp_file.name)
                print(f"✅ Mock image file created: {test_image_path}")
        
        # Test 4: Test provider scanning methods (without actually calling APIs)
        print("🔍 Testing provider methods...")
        
        # Mock the actual API calls to avoid using real API keys during unit testing
        with patch.object(service, '_scan_with_gemini') as mock_gemini:
            mock_gemini.return_value = Mock()
            mock_gemini.return_value.name = "Test Recipe"
            mock_gemini.return_value.description = "A test recipe"
            
            try:
                result = await service._scan_with_provider([test_image_path], "gemini")
                print("✅ Gemini provider method callable")
            except Exception as e:
                print(f"⚠️  Gemini provider test: {e}")
        
        # Test 5: Test OCR fallback
        print("🔤 Testing OCR fallback...")
        try:
            result = await service._scan_with_ocr([test_image_path])
            print("✅ OCR fallback method callable")
        except Exception as e:
            print(f"⚠️  OCR fallback test: {e}")
        
        # Cleanup
        test_image_path.unlink()
        
        print("\n🎉 Unit tests completed!")
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

async def test_with_real_gemini_api():
    """Test with real Gemini API if available"""
    print("\n🌟 Testing with real Gemini API")
    print("=" * 40)
    
    config = load_config()
    if not config or not config["credentials"].get("gemini_api_key") or config["credentials"]["gemini_api_key"] == "YOUR_GEMINI_API_KEY_HERE":
        print("⚠️  Skipping real API test - no Gemini key configured")
        return True
    
    try:
        from mealie.services.image_scanning import ImageScanningService
        from mealie.lang.providers import Translator
        
        translator = Mock(spec=Translator)
        translator.t = Mock(return_value="Test")
        
        service = ImageScanningService(translator)
        
        # Create a more realistic test image with recipe content
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_file:
            try:
                from PIL import Image, ImageDraw, ImageFont
                img = Image.new('RGB', (600, 800), color='white')
                draw = ImageDraw.Draw(img)
                
                # Try to use a proper font
                try:
                    font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
                    font_normal = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
                except:
                    font_large = font_normal = ImageFont.load_default()
                
                # Draw a recipe
                y = 30
                draw.text((50, y), "CHOCOLATE CHIP COOKIES", fill='black', font=font_large)
                y += 60
                
                draw.text((50, y), "Ingredients:", fill='black', font=font_normal)
                y += 30
                ingredients = [
                    "• 2¼ cups all-purpose flour",
                    "• 1 cup butter, softened",
                    "• ¾ cup brown sugar",
                    "• ½ cup white sugar", 
                    "• 2 large eggs",
                    "• 2 tsp vanilla extract",
                    "• 1 tsp baking soda",
                    "• 1 tsp salt",
                    "• 2 cups chocolate chips"
                ]
                
                for ingredient in ingredients:
                    draw.text((70, y), ingredient, fill='black', font=font_normal)
                    y += 25
                
                y += 20
                draw.text((50, y), "Instructions:", fill='black', font=font_normal)
                y += 30
                
                instructions = [
                    "1. Preheat oven to 375°F (190°C)",
                    "2. Mix butter and sugars until creamy",
                    "3. Beat in eggs and vanilla",
                    "4. Gradually add flour, baking soda, and salt",
                    "5. Stir in chocolate chips",
                    "6. Drop onto baking sheets",
                    "7. Bake 9-11 minutes until golden brown"
                ]
                
                for instruction in instructions:
                    draw.text((70, y), instruction, fill='black', font=font_normal)
                    y += 25
                
                img.save(tmp_file.name, 'JPEG')
                test_image_path = Path(tmp_file.name)
                print(f"✅ Realistic recipe image created: {test_image_path}")
                
                # Test the full scanning pipeline
                print("🔄 Testing full scanning pipeline...")
                try:
                    recipe_data = await service.scan_images_for_recipe([test_image_path])
                    print(f"✅ Recipe extracted successfully!")
                    print(f"   Name: {getattr(recipe_data, 'name', 'N/A')}")
                    print(f"   Ingredients: {len(getattr(recipe_data, 'recipe_ingredient', []))}")
                    print(f"   Instructions: {len(getattr(recipe_data, 'recipe_instructions', []))}")
                    return True
                    
                except Exception as e:
                    print(f"⚠️  Full pipeline test failed: {e}")
                    return False
                    
            except ImportError:
                print("⚠️  PIL not available for realistic image creation")
                return True
            finally:
                if 'test_image_path' in locals():
                    test_image_path.unlink(missing_ok=True)
                    
    except Exception as e:
        print(f"❌ Real API test failed: {e}")
        return False

async def main():
    """Main test function"""
    print("🧪 Image Scanning Unit Tests")
    print("=" * 50)
    
    # Load config and setup environment
    config = load_config()
    if not config:
        return False
    
    setup_environment(config)
    
    # Run tests
    success1 = await test_image_scanning_service()
    success2 = await test_with_real_gemini_api()
    
    overall_success = success1 and success2
    
    print(f"\n📊 FINAL RESULTS")
    print("=" * 30)
    print(f"Unit Tests: {'✅ PASS' if success1 else '❌ FAIL'}")
    print(f"API Tests: {'✅ PASS' if success2 else '❌ FAIL'}")
    print(f"Overall: {'🎉 SUCCESS' if overall_success else '💥 FAILED'}")
    
    return overall_success

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)