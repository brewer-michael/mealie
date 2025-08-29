# OCR Recipe Creation in Mealie

This document describes the OCR (Optical Character Recognition) functionality for creating recipes from handwritten or printed recipe card images.

## Overview

Mealie provides two methods for extracting recipes from images:

1. **OpenAI Vision API** (Primary) - Advanced AI-powered image understanding
2. **Traditional OCR** (Fallback) - Tesseract-based text extraction

## Current Implementation Status

### ✅ OpenAI-Based Image Processing (Already Available)
- **Endpoint**: `/api/recipes/create/image`
- **Frontend**: `/g/{groupSlug}/r/create/image`
- **Features**:
  - Multiple image support
  - Advanced context understanding
  - Recipe structure recognition
  - Language translation support
  - Image cropping and rotation
  - High accuracy for handwritten text

### ✅ Traditional OCR (Newly Added)
- **Endpoint**: `/api/recipes/create/image/ocr`  
- **Frontend**: `/g/{groupSlug}/r/create/ocr`
- **Features**:
  - Offline processing (no external API calls)
  - Privacy-friendly (images stay local)
  - Tesseract-based text extraction
  - Basic recipe parsing
  - Fallback option when OpenAI is not available

## Installation

### For OpenAI Features (Already Included)
OpenAI functionality is already available in Mealie. Configure with:
```bash
OPENAI_API_KEY=your_api_key
OPENAI_ENABLED=true
OPENAI_ENABLE_IMAGE_SERVICES=true
```

### For Traditional OCR (New)
Install additional dependencies:

```bash
# Using poetry (recommended)
poetry install --with ocr

# Or using pip
pip install pytesseract

# System dependencies (required for Tesseract)
# Ubuntu/Debian:
sudo apt-get install tesseract-ocr

# MacOS:
brew install tesseract

# Docker users: Add to your Dockerfile
RUN apt-get update && apt-get install -y tesseract-ocr
```

## Usage

### Via Web Interface

1. **OpenAI Method** (Recommended):
   - Navigate to `/g/{groupSlug}/r/create/image`
   - Upload one or more recipe images
   - Optionally enable translation
   - Process with AI understanding

2. **OCR Method** (Fallback):
   - Navigate to `/g/{groupSlug}/r/create/ocr`
   - Upload a single clear recipe image
   - Process with traditional OCR

### Via API

#### OpenAI Method
```http
POST /api/recipes/create/image
Content-Type: multipart/form-data

images: [file1.jpg, file2.jpg]
translateLanguage: en (optional)
```

#### OCR Method
```http
POST /api/recipes/create/image/ocr
Content-Type: multipart/form-data

images: [recipe.jpg]
```

## Image Requirements

### For Best Results
- **Clear, well-lit images**
- **High contrast** between text and background
- **Minimal skew** (straight orientation)
- **Readable text size** (at least 12pt equivalent)

### OpenAI Method
- Handles handwritten text very well
- Can process multiple recipe cards
- Understands context and recipe structure
- Works with various orientations and lighting

### OCR Method
- Works best with **printed text**
- Prefers **black text on white background**
- Requires **clear, straight orientation**
- Single recipe per image recommended

## Recipe Data Structure

Both methods create recipes with the `is_ocr_recipe: true` flag, indicating they were created from image processing.

### Generated Fields
- **Name**: Extracted from image title/header
- **Ingredients**: Parsed ingredient lines with measurements
- **Instructions**: Step-by-step cooking directions
- **Description**: Additional notes or context
- **Image**: Original uploaded image is saved

## Implementation Details

### Files Added/Modified

#### Backend
- `mealie/services/ocr/ocr_service.py` - OCR text extraction and parsing
- `mealie/services/recipe/recipe_service.py` - Added `create_from_images_ocr()` method
- `mealie/routes/recipe/recipe_crud_routes.py` - Added `/create/image/ocr` endpoint
- `pyproject.toml` - Added OCR dependencies group

#### Frontend
- `frontend/pages/g/[groupSlug]/r/create/ocr.vue` - OCR upload page
- `frontend/pages/g/[groupSlug]/r/create.vue` - Added OCR option to menu
- `frontend/lib/api/user/recipes/recipe.ts` - Added OCR API method

#### Tests
- `tests/integration_tests/user_recipe_tests/test_recipe_create_from_image_ocr.py`

### Database Schema
The existing `is_ocr_recipe` boolean field (added in migration 2022-08-13) is used to mark recipes created from images.

## Error Handling

### Common Issues
1. **Missing Tesseract**: Returns 400 error with installation instructions
2. **No text extracted**: Creates recipe with informative message
3. **Poor image quality**: May result in garbled text extraction

### Graceful Degradation
- OCR method gracefully falls back to basic parsing if structured sections aren't found
- Always creates a recipe object, even if text extraction fails
- Provides clear error messages for missing dependencies

## Performance Considerations

### OpenAI Method
- **Pros**: High accuracy, context understanding, handles handwritten text
- **Cons**: Requires API key, external dependency, costs per request

### OCR Method  
- **Pros**: Offline processing, no external dependencies, privacy-friendly
- **Cons**: Lower accuracy, requires clear printed text, basic parsing

## Future Improvements

### Potential Enhancements
1. **NLP Integration**: Use ingredient-parser-nlp for better ingredient parsing
2. **Image Preprocessing**: Add automatic contrast/rotation correction
3. **Multi-language OCR**: Support for different language models
4. **Batch Processing**: Handle multiple images in OCR method
5. **Manual Correction Interface**: Allow users to edit extracted text before creating recipe

### Configuration Options
Consider adding settings for:
- OCR language selection
- Text extraction confidence thresholds  
- Recipe parsing sensitivity
- Image preprocessing options

## Testing

Run OCR-specific tests:
```bash
# Install OCR dependencies first
poetry install --with ocr

# Run OCR tests
pytest tests/integration_tests/user_recipe_tests/test_recipe_create_from_image_ocr.py

# Test OCR service availability
python -c "from mealie.services.ocr import OCRService; print('OCR Available:', OCRService.is_available())"
```

## Conclusion

The OCR functionality provides Mealie users with flexible options for digitizing recipe cards:

- **OpenAI method** for maximum accuracy and convenience
- **Traditional OCR** for offline processing and privacy

Both methods integrate seamlessly with Mealie's existing recipe management system while maintaining the same user experience and data structure.