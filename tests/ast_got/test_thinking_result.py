"""
Test script to verify that AGoT output can be converted to a string for AI models.
"""
import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from tests.ast_got.agot_processor import AGoTProcessor

def test_thinking_result_extraction():
    """Test extracting thinking result as string from AGoT output."""
    
    print("=" * 70)
    print("Testing AGoT Thinking Result Extraction")
    print("=" * 70)
    
    # Create processor
    processor = AGoTProcessor()
    
    # Test query
    query = "What is the relationship between quantum mechanics and consciousness?"
    
    print(f"\n[1] Processing query: {query[:60]}...")
    
    try:
        # Process the query (this will run all 8 stages)
        result = processor.process_query(query)
        
        print("\n[2] Raw result type:", type(result))
        print("    Raw result keys:", list(result.keys()))
        
        # Extract thinking result as string
        thinking_string = processor.extract_thinking_result(result)
        
        print("\n[3] Thinking result type:", type(thinking_string))
        print("    Thinking result length:", len(thinking_string), "characters")
        
        print("\n" + "=" * 70)
        print("EXTRACTED THINKING RESULT (String Format):")
        print("=" * 70)
        print(thinking_string)
        print("=" * 70)
        
        # Verify it's a string
        assert isinstance(thinking_string, str), "Thinking result must be a string!"
        assert len(thinking_string) > 0, "Thinking result should not be empty!"
        
        print("\n✅ SUCCESS: AGoT output successfully converted to string format!")
        print("   The thinking result can now be used directly as AI model output.")
        
        return True
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_thinking_result_extraction()
    sys.exit(0 if success else 1)
