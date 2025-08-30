#!/usr/bin/env python3
"""
Test script to verify the import fix for admin_recipe_scanning.py
This script checks if the import statements are structurally correct.
"""

import ast
import sys
from pathlib import Path

def test_import_structure():
    """Test that the import structure is valid without actually importing."""
    
    # Check the admin_recipe_scanning.py file
    admin_file = Path("mealie/routes/admin/admin_recipe_scanning.py")
    if not admin_file.exists():
        print(f"❌ File not found: {admin_file}")
        return False
    
    # Parse the file to check syntax
    try:
        with open(admin_file, 'r') as f:
            content = f.read()
        
        # Parse AST to validate syntax
        tree = ast.parse(content)
        print(f"✓ {admin_file} has valid Python syntax")
        
        # Check for the corrected import
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (node.module == "mealie.core.dependencies" and 
                    any(alias.name == "get_admin_user" for alias in node.names)):
                    print("✓ Found correct import: get_admin_user")
                    
                # Check for the old incorrect import
                if (node.module == "mealie.core.dependencies" and 
                    any(alias.name == "get_current_admin_user" for alias in node.names)):
                    print("❌ Found old incorrect import: get_current_admin_user")
                    return False
        
        # Check for usage of the correct function name
        function_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "get_admin_user":
                function_calls.append(node.lineno)
        
        if function_calls:
            print(f"✓ Found {len(function_calls)} usages of get_admin_user at lines: {function_calls}")
        
        # Check dependencies file exists and has the function
        deps_file = Path("mealie/core/dependencies/dependencies.py")
        if deps_file.exists():
            with open(deps_file, 'r') as f:
                deps_content = f.read()
            
            if "def get_admin_user" in deps_content:
                print("✓ get_admin_user function exists in dependencies")
            else:
                print("❌ get_admin_user function not found in dependencies")
                return False
        
        return True
        
    except SyntaxError as e:
        print(f"❌ Syntax error in {admin_file}: {e}")
        return False
    except Exception as e:
        print(f"❌ Error parsing {admin_file}: {e}")
        return False

if __name__ == "__main__":
    print("Testing import fix for admin_recipe_scanning.py...")
    print("=" * 50)
    
    if test_import_structure():
        print("\n✅ All tests passed! The import fix is correct.")
        sys.exit(0)
    else:
        print("\n❌ Tests failed! There are still import issues.")
        sys.exit(1)