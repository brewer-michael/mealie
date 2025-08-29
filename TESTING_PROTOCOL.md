# Image Scanning Testing Protocol

This directory contains comprehensive testing tools for the new image scanning functionality with provider fallback chains.

## Quick Start

1. **Set up credentials** in `.claude/testcycle.json`:
   ```json
   {
     "credentials": {
       "gemini_api_key": "your-actual-gemini-key",
       "openai_api_key": "your-actual-openai-key-if-available",
       "anthropic_api_key": "your-actual-anthropic-key-if-available"
     }
   }
   ```

2. **Run quick test**:
   ```bash
   python quick_test.py
   ```

3. **Upload test image** via the running server at `http://localhost:9000`

## Testing Scripts

### `quick_test.py` - Simple Integration Test
- Starts server with Gemini as primary provider
- Creates default test user
- Provides endpoint for manual image upload testing
- Ideal for immediate validation

### `test_image_scanning.py` - Comprehensive Test Suite
- Full automated testing protocol
- Multiple provider configurations
- Automated image upload and validation
- Detailed results reporting

Run with:
```bash
# Full test suite
python test_image_scanning.py

# Setup server only (for manual testing)  
python test_image_scanning.py --setup-only

# Run specific scenario
python test_image_scanning.py --scenario gemini_primary_test
```

## Test Scenarios

The comprehensive test includes:

1. **Gemini Primary**: Tests Gemini as primary provider with OCR fallback
2. **OpenAI Fallback**: Tests OpenAI primary → Gemini secondary → OCR chain  
3. **OCR Only**: Tests pure OCR functionality when no AI providers configured

## Configuration File: `.claude/testcycle.json`

```json
{
  "description": "Test configuration for image scanning with Gemini provider",
  "credentials": {
    "gemini_api_key": "YOUR_GEMINI_API_KEY_HERE",
    "openai_api_key": "YOUR_OPENAI_API_KEY_HERE", 
    "anthropic_api_key": "YOUR_ANTHROPIC_API_KEY_HERE"
  },
  "test_settings": {
    "server_host": "localhost",
    "server_port": 9000,
    "api_base_url": "http://localhost:9000/api",
    "test_image_path": "/config/workspace/mealie/test_recipe_image.jpg",
    "default_user": {
      "email": "test@example.com",
      "password": "MySecurePassword123!",
      "username": "testuser",
      "full_name": "Test User"
    }
  }
}
```

## Manual Testing Workflow

1. **Start Server**: `python quick_test.py`
2. **Access Frontend**: Navigate to `http://localhost:3000` (if running separately)
3. **Login**: Use credentials from config (`test@example.com` / password from config)
4. **Test Recipe Creation**:
   - Go to `/g/[group]/r/create`
   - Select "Create from Images (AI)" option
   - Upload your test recipe image
   - Verify recipe extraction results

## API Testing

Direct API endpoint testing:

```bash
# Get auth token
TOKEN=$(curl -X POST "http://localhost:9000/api/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=test@example.com&password=MySecurePassword123!" | jq -r '.access_token')

# Upload image for recipe scanning
curl -X POST "http://localhost:9000/api/recipes/create/image" \
  -H "Authorization: Bearer $TOKEN" \
  -F "images=@your_test_image.jpg"
```

## Provider Configuration Testing

Test different provider chains by modifying environment variables:

```bash
# Gemini primary with OpenAI fallback
export IMAGE_SCANNING_PRIMARY_PROVIDER="gemini"
export IMAGE_SCANNING_SECONDARY_PROVIDER="openai" 
export IMAGE_SCANNING_ENABLE_OCR_FALLBACK="true"

# OpenAI only
export IMAGE_SCANNING_PRIMARY_PROVIDER="openai"
export IMAGE_SCANNING_SECONDARY_PROVIDER="none"

# OCR fallback only
export IMAGE_SCANNING_PRIMARY_PROVIDER="none"
export IMAGE_SCANNING_SECONDARY_PROVIDER="none"
export IMAGE_SCANNING_ENABLE_OCR_FALLBACK="true"
```

## Expected Results

✅ **Success Indicators**:
- Server starts without errors
- User creation and authentication works
- Image upload returns recipe slug
- Recipe contains extracted name, ingredients, instructions
- Fallback chain works when providers fail

❌ **Failure Indicators**:
- API key authentication errors
- Server startup failures
- Empty or malformed recipe extraction
- Provider fallback chain not triggering

## Troubleshooting

- **Server won't start**: Check port 9000 availability, dependencies installed
- **Authentication fails**: Verify API keys in config file
- **Image upload fails**: Check image format, file size, API endpoint
- **Empty recipe results**: Test with different image, check provider logs

## File Structure

```
/config/workspace/mealie/
├── .claude/
│   └── testcycle.json          # Test configuration with your API keys
├── test_image_scanning.py      # Comprehensive automated test suite  
├── quick_test.py               # Simple integration test
├── TESTING_PROTOCOL.md         # This documentation
└── test_recipe_image.jpg       # Your test recipe image (add manually)
```