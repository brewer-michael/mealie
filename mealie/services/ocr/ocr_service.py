import logging
import re
from pathlib import Path
from typing import Any

try:
    import pytesseract
    from PIL import Image
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

from mealie.schema.recipe.recipe import CreateRecipe
from mealie.services._base_service import BaseService

logger = logging.getLogger(__name__)


class OCRService(BaseService):
    """Traditional OCR service using Tesseract as a fallback to OpenAI."""
    
    def __init__(self) -> None:
        if not TESSERACT_AVAILABLE:
            raise ImportError("Tesseract OCR is not available. Install with: pip install pytesseract pillow")
        super().__init__()
    
    @staticmethod
    def is_available() -> bool:
        """Check if OCR service is available."""
        return TESSERACT_AVAILABLE
    
    def extract_text_from_image(self, image_path: Path) -> str:
        """Extract raw text from an image using Tesseract OCR."""
        try:
            image = Image.open(image_path)
            # Convert to RGB if necessary
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            # Use Tesseract to extract text
            text = pytesseract.image_to_string(image, config='--psm 6')
            return text.strip()
        except Exception as e:
            logger.error(f"Failed to extract text from image {image_path}: {e}")
            return ""
    
    def parse_recipe_text(self, text: str) -> CreateRecipe:
        """Parse extracted text into a recipe structure."""
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        if not lines:
            return CreateRecipe(name="OCR Recipe", description="No text could be extracted from the image.")
        
        # Basic parsing logic - this is a simple implementation
        # For production use, you might want more sophisticated NLP parsing
        
        recipe_name = lines[0] if lines else "OCR Recipe"
        
        ingredients = []
        instructions = []
        description = ""
        
        current_section = "description"
        
        for i, line in enumerate(lines[1:], 1):
            line_lower = line.lower()
            
            # Detect ingredients section
            if any(keyword in line_lower for keyword in ['ingredient', 'need', 'require']):
                current_section = "ingredients"
                continue
            
            # Detect instructions section
            elif any(keyword in line_lower for keyword in ['instruction', 'direction', 'method', 'step']):
                current_section = "instructions" 
                continue
            
            # Parse based on current section
            if current_section == "description" and i <= 3:  # First few lines might be description
                description += f"{line} "
            
            elif current_section == "ingredients":
                # Look for lines that might be ingredients (contain measurements, food items)
                if self._looks_like_ingredient(line):
                    ingredients.append({"text": line, "original_text": line})
            
            elif current_section == "instructions":
                # Lines in instructions section
                if line and len(line) > 10:  # Filter out very short lines
                    instructions.append({"text": line})
        
        # If we didn't find structured sections, try to parse the whole text
        if not ingredients and not instructions:
            ingredients, instructions = self._fallback_parsing(lines)
        
        return CreateRecipe(
            name=recipe_name[:255],  # Limit name length
            description=description.strip()[:500] if description.strip() else "Recipe extracted from image using OCR.",
            recipe_ingredient=[{"text": ing["text"]} for ing in ingredients] if ingredients else [],
            recipe_instructions=[{"text": inst["text"]} for inst in instructions] if instructions else [],
            is_ocr_recipe=True,
        )
    
    def _looks_like_ingredient(self, line: str) -> bool:
        """Simple heuristic to identify ingredient lines."""
        # Look for common measurement patterns
        measurement_patterns = [
            r'\d+\s*(cup|cups|tsp|tbsp|teaspoon|tablespoon|oz|lb|pound|gram|kg|ml|liter)',
            r'\d+/\d+',  # fractions
            r'\d+\s*(large|medium|small|whole)',
            r'^\d+\s+',  # starts with number
        ]
        
        line_lower = line.lower()
        return any(re.search(pattern, line_lower) for pattern in measurement_patterns)
    
    def _fallback_parsing(self, lines: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fallback parsing when no clear structure is detected."""
        ingredients = []
        instructions = []
        
        for line in lines[1:]:  # Skip the first line (title)
            if self._looks_like_ingredient(line):
                ingredients.append({"text": line})
            elif len(line) > 15:  # Longer lines are likely instructions
                instructions.append({"text": line})
        
        return ingredients, instructions
    
    def process_image(self, image_path: Path) -> CreateRecipe:
        """Main method to process an image and return a recipe."""
        logger.info(f"Processing image for OCR: {image_path}")
        
        # Extract text from image
        text = self.extract_text_from_image(image_path)
        
        if not text:
            logger.warning(f"No text extracted from image: {image_path}")
            return CreateRecipe(
                name="OCR Recipe",
                description="No text could be extracted from this image. Please ensure the image is clear and contains readable text.",
                is_ocr_recipe=True,
            )
        
        # Parse text into recipe structure
        recipe = self.parse_recipe_text(text)
        logger.info(f"Successfully processed recipe: {recipe.name}")
        
        return recipe