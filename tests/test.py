"""
Test file to run the full AGoT algorithm pipeline
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from tests.ast_got.agot_processor import AGoTProcessor


def full_algorithm():
    """Test the complete 8-stage algorithm pipeline"""
    print("=" * 60)
    print("Testing AGoT Algorithm - Full Pipeline")
    print("=" * 60)
    
    processor = AGoTProcessor()
    
    test_query = "What are the key factors affecting skin cancer progression?"
    
    parameters = {
        "hypotheses_per_dimension": 3,
        "dimension_confidence": [0.8, 0.8, 0.8, 0.8],
        "hypothesis_confidence": [0.5, 0.5, 0.5, 0.5],
        "evidence_max_iterations": 3,
        "pruning_threshold": 0.2,
        "impact_threshold": 0.3,
        "merging_threshold": 0.8,
        "extraction_criteria": {
            "min_confidence": 0.6,
            "min_impact": 0.5
        },
        "output_dir": "output"
    }
    
    print(f"\nQuery: {test_query}")
    print(f"Parameters: {parameters}")
    print("\n" + "-" * 60)
    
    result = processor.process_query(test_query, parameters=parameters)
    
    print("\n" + "=" * 60)
    print("ALGORITHM EXECUTION COMPLETE")
    print("=" * 60)
    
    print("\n--- Reasoning Trace ---")
    for trace in result["reasoning_trace"]:
        print(f"Stage {trace['stage']}: {trace['name']}")
        print(f"  Summary: {trace['summary']}")
        print(f"  Metrics: {trace['metrics']}")
    
    print("\n--- Final Confidence ---")
    print(f"Confidence vector: {result['confidence']}")
    print(f"  - Empirical: {result['confidence'][0]}")
    print(f"  - Theoretical: {result['confidence'][1]}")
    print(f"  - Methodological: {result['confidence'][2]}")
    print(f"  - Consensus: {result['confidence'][3]}")
    
    print("\n--- Graph Statistics ---")
    graph_state = result["graph_state"]
    print(f"Total nodes: {graph_state['metadata']['node_count']}")
    print(f"Total edges: {graph_state['metadata']['edge_count']}")
    print(f"Hyperedges: {graph_state['metadata']['hyperedge_count']}")
    print(f"IBN count: {graph_state['metadata']['ibn_count']}")
    
    print("\n--- Result Sections ---")
    if result["result"] and result["result"].get("sections"):
        for section in result["result"]["sections"]:
            print(f"  - {section.get('title', 'Untitled')}: {section.get('type', 'unknown')}")
    
    print("\n" + "=" * 60)
    print("TEST PASSED - Full algorithm executed successfully!")
    print("=" * 60)
    
    return result


if __name__ == "__main__":
    print("\n")
    result = full_algorithm()