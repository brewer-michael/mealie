# Building Mealie Development Image with OCR Support

This development build includes enhanced image scanning capabilities:

- **OCR Support**: Tesseract for handwritten recipe cards
- **Multi-provider AI**: OpenAI, Gemini, Anthropic, Ollama support
- **Intelligent Fallback**: Primary AI → Secondary AI → OCR
- **Enhanced Formats**: HEIC/MPO support for iPhone photos
- **Admin Interface**: Complete configuration at `/admin/recipe-scanning`

## Quick Start

### 1. Build the Development Image

```bash
# Build the development image with OCR support
docker build -f docker/Dockerfile.dev -t mealie:dev-ocr .
```

### 2. Run with Docker Compose

```bash
# Start the development container
docker-compose -f docker/docker-compose.dev-ocr.yml up -d

# View logs
docker-compose -f docker/docker-compose.dev-ocr.yml logs -f mealie-dev
```

### 3. Access the Application

- **Web Interface**: http://localhost:9092
- **Admin Config**: http://localhost:9092/admin/recipe-scanning
- **Recipe Creation**: http://localhost:9092/g/home/r/create

## Unraid Installation

### 1. Copy Template

Copy `mealie-dev.xml` to your Unraid templates directory:

```bash
cp mealie-dev.xml /boot/config/plugins/dockerMan/templates-user/
```

### 2. Configure Container

1. Go to Docker tab in Unraid
2. Add Container → Select "mealie-dev" template
3. Configure environment variables:
   - Set your API keys in the Advanced section
   - Choose your primary/secondary providers
   - Enable/disable OCR fallback

### 3. Deploy

The container will be available at: `http://[unraid-ip]:[port]`

## Configuration

### Image Scanning Providers

Set these environment variables:

```bash
# Primary provider (required)
IMAGE_SCANNING_PRIMARY_PROVIDER=openai    # openai, gemini, anthropic, ollama, none

# Secondary provider (optional fallback)  
IMAGE_SCANNING_SECONDARY_PROVIDER=gemini  # openai, gemini, anthropic, ollama, none

# OCR fallback (recommended)
IMAGE_SCANNING_ENABLE_OCR_FALLBACK=true   # true, false
```

### API Keys

```bash
# OpenAI (GPT-4 Vision)
OPENAI_API_KEY=sk-your-key-here

# Google Gemini
GEMINI_API_KEY=your-gemini-key-here

# Anthropic Claude
ANTHROPIC_API_KEY=your-anthropic-key-here

# Ollama (local)
OLLAMA_BASE_URL=http://localhost:11434
```

### Admin Configuration

After deployment, configure providers at:
`http://your-mealie-url/admin/recipe-scanning`

This interface provides:
- Provider selection dropdowns
- API key configuration
- Test functionality
- ELI5 setup instructions

## Testing

### 1. Upload Test Images

Test with the included sample images:
- `test_recipe_image.jpg` (generated recipe card)  
- `test_recipe_card.jpg` (handwritten sample)

### 2. Test OCR Fallback

1. Set `IMAGE_SCANNING_PRIMARY_PROVIDER=none`
2. Enable OCR fallback
3. Upload a recipe card image
4. Verify OCR extraction works

### 3. Test AI Providers

1. Configure API key for chosen provider
2. Set as primary provider  
3. Upload recipe card image
4. Verify AI extraction works

## Development Notes

### Differences from Production

- Includes Tesseract OCR runtime
- Additional Python dependencies (pytesseract, pillow-heif, httpx)
- HEIF/HEIC image format support
- Debug logging enabled
- Development port (9092 vs 9000)

### Architecture

The fallback chain works as follows:

1. **Primary Provider**: Configured AI service (OpenAI, Gemini, etc.)
2. **Secondary Provider**: Backup AI service if primary fails
3. **OCR Fallback**: Traditional OCR if all AI services fail

This ensures maximum reliability for recipe extraction.

### File Support

- **Standard**: JPG, PNG, WebP, TIFF
- **Mobile**: HEIC (iPhone), MPO (iPhone burst photos)
- **Processing**: Automatic EXIF rotation correction

## Troubleshooting

### OCR Not Working
```bash
# Check Tesseract installation
docker exec mealie-dev tesseract --version

# Test OCR manually
docker exec mealie-dev tesseract /app/data/test.jpg stdout
```

### AI Providers Failing
1. Check API keys in admin interface
2. Verify network connectivity
3. Check provider status/quotas
4. Review container logs

### Image Format Issues
```bash
# Check supported formats
docker exec mealie-dev python -c "from PIL import Image; print(Image.registered_extensions())"
```

## Security Notes

- API keys are masked in Unraid template
- Keys stored as Docker secrets in production
- OCR processing is completely offline
- No external dependencies for OCR fallback