#!/usr/bin/env python3
"""
Simple test server to demo OCR functionality without full Mealie setup.
"""
import tempfile
import shutil
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import pytesseract
from PIL import Image
import uvicorn

# Enable HEIC support for iPhone images
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

app = FastAPI(title="Mealie OCR Test Server", version="1.0.0")

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

class SimpleOCRService:
    """Simplified version of our OCR service for testing."""
    
    @staticmethod
    def extract_text_from_image(image_path: Path) -> str:
        """Extract text from image using Tesseract."""
        try:
            # Try to open the image (supports HEIC if pillow-heif is installed)
            image = Image.open(image_path)
            print(f"Opened image: {image.format}, {image.mode}, {image.size}")
            
            # Handle EXIF rotation data (common in phone photos)
            try:
                exif = image.getexif()
                if exif:
                    orientation = exif.get(274)  # EXIF orientation tag
                    if orientation:
                        print(f"EXIF orientation: {orientation}")
                        # Apply rotation based on EXIF data
                        if orientation == 3:
                            image = image.rotate(180, expand=True)
                        elif orientation == 6:
                            image = image.rotate(270, expand=True)
                        elif orientation == 8:
                            image = image.rotate(90, expand=True)
                        print(f"Image rotated, new size: {image.size}")
            except Exception as exif_e:
                print(f"EXIF processing failed (non-fatal): {exif_e}")
            
            # Convert to RGB if necessary (handles various formats)
            if image.mode != 'RGB':
                print(f"Converting from {image.mode} to RGB")
                image = image.convert('RGB')
            
            # Handle special formats that Tesseract might not support directly
            # Save as temporary PNG for OCR processing
            import tempfile
            import os
            
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
                temp_png_path = temp_file.name
                image.save(temp_png_path, 'PNG')
                print(f"Converted image to PNG for OCR: {temp_png_path}")
            
            try:
                # Try different OCR page segmentation modes for rotated/phone images
                # PSM 6 = single uniform block, PSM 3 = fully automatic page segmentation
                configs_to_try = [
                    '--psm 6',  # Original
                    '--psm 3',  # Fully automatic
                    '--psm 4',  # Single column of text
                    '--psm 1'   # Automatic with OSD (orientation detection)
                ]
                
                best_result = ""
                for config in configs_to_try:
                    try:
                        # Use the temp PNG file for OCR
                        text = pytesseract.image_to_string(temp_png_path, config=config)
                        if len(text.strip()) > len(best_result.strip()):
                            best_result = text
                            print(f"Better result with {config}: {len(text.strip())} chars")
                    except Exception as ocr_e:
                        print(f"OCR config {config} failed: {ocr_e}")
                        continue
                
                result = best_result.strip()
                print(f"Final OCR result: {len(result)} characters")
                return result
                
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_png_path)
                except:
                    pass
            
        except Exception as e:
            # Provide more helpful error messages
            error_msg = str(e)
            print(f"Image processing error: {error_msg}")
            if "cannot identify image file" in error_msg.lower():
                return f"Error: Unsupported image format. Supported formats: JPG, PNG, TIFF, BMP" + (", HEIC" if HEIC_SUPPORTED else "")
            elif "tesseract" in error_msg.lower():
                return f"Error: OCR processing failed - {e}"
            else:
                return f"Error extracting text: {e}"
    
    @staticmethod
    def parse_recipe_text(text: str) -> dict:
        """Simple recipe parsing from extracted text."""
        if not text:
            return {"error": "No text extracted", "raw_text": ""}
        
        # Check if the text is an error message
        if text.startswith("Error"):
            return {"error": text, "raw_text": text}
        
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        if not lines:
            return {"error": "No readable text found", "raw_text": text}
        
        # Simple parsing
        recipe_name = lines[0]
        ingredients = []
        instructions = []
        
        current_section = "description"
        
        for line in lines[1:]:
            line_lower = line.lower()
            
            # Detect ingredient-like lines (contain measurements)
            if any(word in line_lower for word in ['cup', 'tsp', 'tbsp', 'oz', 'lb', 'gram']):
                ingredients.append(line)
                current_section = "ingredients"
            elif any(word in line_lower for word in ['bake', 'cook', 'mix', 'add', 'stir', 'heat']):
                instructions.append(line)
                current_section = "instructions"
            elif current_section == "ingredients" and len(line) > 3:
                ingredients.append(line)
            elif current_section == "instructions" and len(line) > 10:
                instructions.append(line)
        
        return {
            "name": recipe_name,
            "ingredients": ingredients,
            "instructions": instructions,
            "raw_text": text,
            "is_ocr_recipe": True,
        }

@app.get("/")
async def root():
    """Serve the OCR test page."""
    return FileResponse("static/test_ocr.html")

@app.get("/api")
async def api_info():
    """API information endpoint."""
    return {
        "message": "Mealie OCR Test Server",
        "endpoints": {
            "POST /ocr/recipe": "Upload image(s) to extract recipe using OCR",
            "GET /health": "Check if OCR is working"
        }
    }

@app.get("/health")
async def health_check():
    """Check if OCR is working."""
    try:
        # Test with a simple image
        test_img = Image.new('RGB', (100, 50), color='white')
        with tempfile.NamedTemporaryFile(suffix='.png') as tmp:
            test_img.save(tmp.name)
            text = pytesseract.image_to_string(tmp.name)
            
        return {
            "status": "healthy",
            "tesseract_version": str(pytesseract.get_tesseract_version()),
            "ocr_working": True,
            "heic_support": HEIC_SUPPORTED,
            "supported_formats": "JPG, PNG, TIFF, BMP" + (", HEIC" if HEIC_SUPPORTED else "")
        }
    except Exception as e:
        return {
            "status": "error", 
            "error": str(e),
            "ocr_working": False
        }

@app.post("/ocr/recipe")
async def extract_recipe_from_image(images: List[UploadFile] = File(...)):
    """Extract recipe from uploaded image using OCR."""
    if not images:
        raise HTTPException(status_code=400, detail="No images provided")
    
    # Process the first image
    image = images[0]
    
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    try:
        ocr_service = SimpleOCRService()
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save uploaded image
            temp_path = Path(temp_dir) / image.filename
            with temp_path.open("wb") as buffer:
                shutil.copyfileobj(image.file, buffer)
            
            # Extract text
            print(f"Processing image: {image.filename}, size: {temp_path.stat().st_size} bytes")
            extracted_text = ocr_service.extract_text_from_image(temp_path)
            print(f"Extracted text length: {len(extracted_text)}, first 100 chars: {extracted_text[:100]}")
            
            # Parse into recipe structure
            recipe_data = ocr_service.parse_recipe_text(extracted_text)
            
            return JSONResponse(content={
                "success": True,
                "recipe": recipe_data,
                "message": f"Processed image: {image.filename}"
            })
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR processing failed: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Mealie OCR Test Server...")
    print("📝 Upload recipe card images to http://localhost:8000/ocr/recipe")
    print("💊 Check health at http://localhost:8000/health")
    print("📋 API docs at http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000)