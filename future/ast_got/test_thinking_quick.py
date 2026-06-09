"""
Quick test to verify extract_thinking_result works correctly with mock data.
"""
import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from tests import AGoTProcessor


def test_with_mock_data():
    """Test extract_thinking_result with mock composition result."""
    
    print("=" * 70)
    print("Quick Test: extract_thinking_result with Mock Data")
    print("=" * 70)
    
    # Create processor
    processor = AGoTProcessor()
    
    # Create a mock final_result (simulating what process_query would return)
    mock_final_result = {
        "result": {
            "title": "AGoT Analysis: Quantum Mechanics and Consciousness",
            "timestamp": "2026-05-26 12:00:00",
            "sections": [
                {
                    "title": "Executive Summary",
                    "content": "This analysis explores the relationship between quantum mechanics and consciousness through multiple disciplinary lenses. Key findings suggest potential connections via quantum coherence in neural microtubules and integrated information theory.",
                    "type": "summary"
                },
                {
                    "title": "Quantum Biology Analysis",
                    "content": "Recent research indicates quantum effects may play a role in biological systems, particularly in photosynthesis and potentially in neural processes.",
                    "type": "analysis",
                    "subgraph": "quantum_biology"
                },
                {
                    "title": "Neuroscience Perspective",
                    "content": "The neuroscience dimension examines how quantum processes might influence neural activity and conscious experience at the microtubule level.",
                    "type": "analysis",
                    "subgraph": "neuroscience"
                },
                {
                    "title": "Philosophy of Mind",
                    "content": "Philosophical implications include questions about the nature of consciousness, free will, and the measurement problem in quantum mechanics.",
                    "type": "analysis",
                    "subgraph": "philosophy"
                },
                {
                    "title": "Interdisciplinary Insights",
                    "content": "Cross-disciplinary patterns reveal converging evidence from physics, biology, and philosophy suggesting that quantum effects may be more relevant to consciousness than previously thought.",
                    "type": "interdisciplinary"
                },
                {
                    "title": "Knowledge Gaps and Research Opportunities",
                    "content": "Major gaps include lack of direct experimental evidence for quantum coherence in neurons and unclear mechanisms for quantum-to-classical transition in biological systems.",
                    "type": "gaps"
                }
            ],
            "citations": [
                {"id": 1, "label": "Penrose & Hameroff (2014) - Orchestrated Objective Reduction"},
                {"id": 2, "label": "Tegmark (2000) - Importance of quantum decoherence"},
                {"id": 3, "label": "Fisher (2015) - Quantum cognition hypothesis"}
            ],
            "node_count": 45,
            "edge_count": 78,
            "hyperedge_count": 12,
            "ibn_count": 5
        },
        "reasoning_trace": [],
        "confidence": [0.75, 0.68, 0.82, 0.71],
        "graph_state": {},
        "processor": "AGoT",
        "uses_original": False
    }
    
    print("\n[1] Testing extract_thinking_result with mock data...")
    
    try:
        # Extract thinking result
        thinking_string = processor.extract_thinking_result(mock_final_result)
        
        print("\n[2] Result type:", type(thinking_string))
        print("    Result length:", len(thinking_string), "characters")
        
        print("\n" + "=" * 70)
        print("EXTRACTED THINKING RESULT:")
        print("=" * 70)
        print(thinking_string)
        print("=" * 70)
        
        # Verify it's a string
        assert isinstance(thinking_string, str), "Thinking result must be a string!"
        assert len(thinking_string) > 0, "Thinking result should not be empty!"
        
        # Verify it's ULTRA-MINIMALIST (no markdown headers, no sections)
        assert "##" not in thinking_string, "Should NOT contain markdown headers!"
        assert "Key Findings" not in thinking_string, "Should NOT contain detailed findings!"
        assert "References" not in thinking_string, "Should NOT contain citations!"
        assert "Confidence:" in thinking_string, "Should contain compact confidence!"
        
        print("\n✅ SUCCESS: All assertions passed!")
        print("   ✓ Result is a string")
        print("   ✓ Result is ULTRA-MINIMALIST (only summary + confidence)")
        print("   ✓ Eliminated paths are excluded (no Key Findings, no References)")
        print("   ✓ Result can be used directly as AI model output")
        
        return True
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_with_mock_data()
    sys.exit(0 if success else 1)
