# Unit Testing Plan for OCR/Image Scanning Features

## Overview
Comprehensive testing plan for the new OCR and image scanning functionality, covering API integration, provider fallback chain, settings persistence, and error handling.

## 1. Settings Persistence Tests

### 1.1 Backend Settings Tests
**Location**: `tests/unit_tests/test_settings_persistence.py`

**Test Cases**:
- ✅ `test_gemini_api_key_persistence()` - Verify Gemini API key is stored and retrieved
- ✅ `test_openai_api_key_persistence()` - Verify OpenAI API key is stored and retrieved
- ✅ `test_anthropic_api_key_persistence()` - Verify Anthropic API key is stored and retrieved
- ✅ `test_ollama_url_persistence()` - Verify Ollama URL is stored and retrieved
- ✅ `test_provider_selection_persistence()` - Verify primary/secondary provider choices persist
- ✅ `test_ocr_fallback_setting_persistence()` - Verify OCR fallback toggle persists
- ✅ `test_masked_api_keys_in_response()` - Verify API keys are masked in API responses
- ✅ `test_settings_validation()` - Test validation of invalid settings values

### 1.2 Frontend-Backend Integration Tests
**Location**: `tests/integration_tests/test_recipe_scanning_admin.py`

**Test Cases**:
- ✅ `test_save_gemini_configuration()` - Test saving Gemini config via admin API
- ✅ `test_save_openai_configuration()` - Test saving OpenAI config via admin API
- ✅ `test_load_configuration_on_page_load()` - Test loading saved config on page refresh
- ✅ `test_configuration_update_after_save()` - Test config updates reflect immediately
- ✅ `test_invalid_api_key_rejection()` - Test malformed API keys are rejected

## 2. Image Scanning Service Tests

### 2.1 Core Service Tests
**Location**: `tests/unit_tests/test_image_scanning_service.py`

**Test Cases**:
- ✅ `test_provider_fallback_chain()` - Test primary → secondary → OCR fallback
- ✅ `test_single_provider_success()` - Test successful scan with single provider
- ✅ `test_all_providers_fail_exception()` - Test exception when all providers fail
- ✅ `test_provider_skip_when_disabled()` - Test providers skipped when not configured
- ✅ `test_duplicate_provider_skip()` - Test secondary provider skipped if same as primary
- ✅ `test_ocr_fallback_disabled()` - Test OCR skipped when disabled in settings
- ✅ `test_get_available_providers()` - Test provider availability reporting
- ✅ `test_empty_images_list_handling()` - Test handling of empty image list

### 2.2 Provider-Specific Tests
**Location**: `tests/unit_tests/test_image_providers/`

#### 2.2.1 OpenAI Provider Tests (`test_openai_provider.py`)
- ✅ `test_openai_recipe_extraction()` - Test successful recipe extraction
- ✅ `test_openai_api_key_missing()` - Test failure when API key missing
- ✅ `test_openai_api_error_handling()` - Test handling of OpenAI API errors
- ✅ `test_openai_translation_request()` - Test translation functionality
- ✅ `test_openai_multiple_images()` - Test multi-image processing
- ✅ `test_openai_response_parsing()` - Test parsing of OpenAI JSON response
- ✅ `test_openai_malformed_response()` - Test handling of malformed responses

#### 2.2.2 Gemini Provider Tests (`test_gemini_provider.py`)
- ✅ `test_gemini_recipe_extraction()` - Test successful recipe extraction
- ✅ `test_gemini_api_key_missing()` - Test failure when API key missing  
- ✅ `test_gemini_api_error_handling()` - Test handling of Gemini API errors
- ✅ `test_gemini_model_selection()` - Test different model selection
- ✅ `test_gemini_rate_limiting()` - Test rate limit handling
- ✅ `test_gemini_image_format_support()` - Test supported image formats

#### 2.2.3 Anthropic Provider Tests (`test_anthropic_provider.py`)
- ✅ `test_anthropic_recipe_extraction()` - Test successful recipe extraction
- ✅ `test_anthropic_api_key_missing()` - Test failure when API key missing
- ✅ `test_anthropic_api_error_handling()` - Test handling of Anthropic API errors

#### 2.2.4 Ollama Provider Tests (`test_ollama_provider.py`)
- ✅ `test_ollama_recipe_extraction()` - Test successful recipe extraction
- ✅ `test_ollama_connection_failure()` - Test handling of connection failures
- ✅ `test_ollama_model_availability()` - Test model availability checking

#### 2.2.5 OCR Provider Tests (`test_ocr_provider.py`)
- ✅ `test_ocr_recipe_extraction()` - Test OCR text extraction
- ✅ `test_ocr_tesseract_missing()` - Test error when Tesseract not installed
- ✅ `test_ocr_image_preprocessing()` - Test image preprocessing for OCR
- ✅ `test_ocr_text_parsing()` - Test parsing OCR text into recipe format
- ✅ `test_ocr_language_support()` - Test multiple language OCR

## 3. API Route Tests

### 3.1 Recipe Scanning Routes
**Location**: `tests/integration_tests/test_recipe_scanning_routes.py`

**Test Cases**:
- ✅ `test_scan_recipe_images_endpoint()` - Test the main scanning endpoint
- ✅ `test_scan_with_authentication()` - Test endpoint requires authentication
- ✅ `test_scan_with_invalid_images()` - Test handling of invalid image files
- ✅ `test_scan_with_no_images()` - Test error when no images provided
- ✅ `test_scan_with_oversized_images()` - Test handling of large images
- ✅ `test_scan_result_format()` - Test response format matches CreateRecipe schema

### 3.2 Admin Settings Routes
**Location**: `tests/integration_tests/test_admin_recipe_scanning_routes.py`

**Test Cases**:
- ✅ `test_get_scanning_settings()` - Test retrieval of current settings
- ✅ `test_update_scanning_settings()` - Test updating settings via API
- ✅ `test_settings_admin_only()` - Test settings require admin privileges
- ✅ `test_settings_validation()` - Test invalid settings are rejected
- ✅ `test_api_key_masking()` - Test API keys are masked in responses

## 4. Error Handling & Edge Cases

### 4.1 Error Handling Tests
**Location**: `tests/unit_tests/test_image_scanning_errors.py`

**Test Cases**:
- ✅ `test_network_timeout_handling()` - Test API timeout handling
- ✅ `test_api_quota_exceeded_handling()` - Test quota/rate limit handling
- ✅ `test_invalid_api_key_handling()` - Test invalid API key responses
- ✅ `test_service_unavailable_handling()` - Test service downtime handling
- ✅ `test_malformed_image_handling()` - Test corrupted image handling
- ✅ `test_unsupported_format_handling()` - Test unsupported image formats
- ✅ `test_memory_limit_handling()` - Test large image memory limits

### 4.2 Data Validation Tests
**Location**: `tests/unit_tests/test_recipe_data_validation.py`

**Test Cases**:
- ✅ `test_recipe_schema_validation()` - Test CreateRecipe schema validation
- ✅ `test_ingredient_parsing_validation()` - Test ingredient list validation
- ✅ `test_instruction_parsing_validation()` - Test instruction step validation
- ✅ `test_nutrition_data_validation()` - Test nutrition information validation
- ✅ `test_time_parsing_validation()` - Test cooking time parsing validation

## 5. Performance & Load Tests

### 5.1 Performance Tests
**Location**: `tests/performance_tests/test_image_scanning_performance.py`

**Test Cases**:
- ✅ `test_single_image_processing_time()` - Measure single image processing time
- ✅ `test_multiple_image_processing_time()` - Measure multi-image processing
- ✅ `test_concurrent_scanning_requests()` - Test concurrent request handling
- ✅ `test_memory_usage_large_images()` - Test memory usage with large images
- ✅ `test_provider_response_times()` - Compare provider response times

### 5.2 Load Tests
**Location**: `tests/load_tests/test_image_scanning_load.py`

**Test Cases**:
- ✅ `test_sustained_scanning_load()` - Test sustained high load
- ✅ `test_burst_scanning_requests()` - Test burst request handling
- ✅ `test_provider_fallback_under_load()` - Test fallback under heavy load

## 6. Security Tests

### 6.1 Security Tests
**Location**: `tests/security_tests/test_image_scanning_security.py`

**Test Cases**:
- ✅ `test_api_key_storage_encryption()` - Test API keys are stored securely
- ✅ `test_api_key_not_logged()` - Test API keys don't appear in logs
- ✅ `test_malicious_image_handling()` - Test handling of malicious images
- ✅ `test_path_traversal_prevention()` - Test image path traversal prevention
- ✅ `test_admin_endpoint_protection()` - Test admin endpoints require auth
- ✅ `test_rate_limiting_protection()` - Test rate limiting on scanning endpoints

## 7. Integration Tests

### 7.1 End-to-End Tests
**Location**: `tests/e2e/test_recipe_scanning_e2e.py`

**Test Cases**:
- ✅ `test_complete_recipe_scanning_flow()` - Test full scanning workflow
- ✅ `test_recipe_creation_from_scan()` - Test recipe creation after successful scan
- ✅ `test_settings_to_scanning_integration()` - Test settings affect scanning behavior
- ✅ `test_user_workflow_realistic()` - Test realistic user workflow scenarios

## 8. Mock & Fixture Setup

### 8.1 Test Fixtures
**Location**: `tests/fixtures/image_scanning_fixtures.py`

**Fixtures Needed**:
- ✅ `mock_openai_api_responses()` - Mock OpenAI API responses
- ✅ `mock_gemini_api_responses()` - Mock Gemini API responses  
- ✅ `mock_anthropic_api_responses()` - Mock Anthropic API responses
- ✅ `mock_ollama_api_responses()` - Mock Ollama API responses
- ✅ `sample_recipe_images()` - Sample test recipe images
- ✅ `sample_recipe_data()` - Sample expected recipe outputs
- ✅ `mock_settings_configurations()` - Various settings configurations

### 8.2 Test Data
**Location**: `tests/data/image_scanning/`

**Required Test Data**:
- ✅ Sample recipe card images (various formats: JPG, PNG, HEIC, etc.)
- ✅ Expected recipe extraction results for each image
- ✅ Invalid/corrupted image files for error testing
- ✅ Large images for performance testing
- ✅ Multi-language recipe images for translation testing

## 9. Test Implementation Priority

### Phase 1: Critical Path (Week 1)
1. Settings persistence tests (fix current Gemini key issue)
2. Basic image scanning service tests
3. Provider fallback chain tests
4. API route tests

### Phase 2: Provider Integration (Week 2)  
1. OpenAI provider tests (already working)
2. Gemini provider implementation and tests
3. OCR provider tests
4. Error handling tests

### Phase 3: Robustness (Week 3)
1. Security tests
2. Performance tests  
3. Edge case handling
4. Integration tests

### Phase 4: Full Coverage (Week 4)
1. Load tests
2. End-to-end tests
3. Complete fixture setup
4. Documentation and test maintenance

## 10. Test Automation

### 10.1 CI/CD Integration
- All unit tests run on every PR
- Integration tests run on merge to main
- Performance tests run nightly
- Security tests run weekly

### 10.2 Coverage Goals
- Unit test coverage: 90%+
- Integration test coverage: 80%+
- Critical path coverage: 100%

## 11. Test Tools & Framework

### 11.1 Testing Stack
- **Framework**: pytest
- **Mocking**: pytest-mock, responses
- **Fixtures**: pytest fixtures
- **Coverage**: pytest-cov
- **Performance**: pytest-benchmark
- **API Testing**: httpx, fastapi.testclient
- **Database Testing**: pytest-postgresql (for integration tests)

### 11.2 Custom Test Utilities
- Image comparison utilities
- Recipe data comparison utilities
- API response validation helpers
- Mock provider response generators