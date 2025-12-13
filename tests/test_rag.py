import os
import logging
import numpy as np
from typing import Dict
from models.chat_model import chat_model
from models.embed_model.core import embed_model
from rag.mutil_hop_graphrag.store import PersistentGraph
from rag.mutil_hop_graphrag.core import multi_hop_search
from rag.mutil_hop_graphrag.dynamic_graph_builder import DynamicGraphBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger("MultiHopGraphRAG")

def build_demo_graph() -> PersistentGraph:
    """
    Build a demo graph with predefined documents and edges.
    This is kept for backward compatibility with existing demo code.
    For dynamic graph building, use DynamicGraphBuilder instead.
    """
    raw_docs = [
        # Layer 0 – entry points (2 branches)
        # 0: Einstein background
        "Albert Einstein was a theoretical physicist born in Germany in 1879. "
        "He developed the theory of relativity, one of the two pillars of modern physics. "
        "His work on the photoelectric effect won him the Nobel Prize in 1921.",
        # 1: Darwin background
        "Charles Darwin was an English naturalist who established that all species "
        "descended from common ancestors. He published On the Origin of Species in 1859, "
        "which introduced the theory of evolution by natural selection.",

        # Layer 1 – direct children of Layer 0
        # 2: relativity
        "The theory of relativity transformed our understanding of space, time, and gravity. "
        "Its famous equation E=mc2 shows that energy and mass are equivalent. "
        "This principle is the foundation of nuclear energy and modern cosmology.",
        # 3: photoelectric effect
        "The photoelectric effect is the emission of electrons when light shines on a material. "
        "Einstein explained this phenomenon using quantum theory, for which he won the Nobel Prize. "
        "This discovery laid the groundwork for modern electronics and solar panels.",
        # 4: natural selection
        "Natural selection is the differential survival and reproduction of individuals "
        "due to differences in phenotype. It is a key mechanism of evolution, "
        "first fully articulated by Charles Darwin.",

        # Layer 2 – deeper children (physics branch)
        # 5: E=mc2
        "E=mc2 is the mass-energy equivalence formula discovered by Einstein. "
        "It states that energy equals mass times the speed of light squared. "
        "This equation is crucial for understanding nuclear reactions and star formation.",
        # 6: quantum mechanics
        "Quantum mechanics describes nature at atomic and subatomic scales. "
        "It introduces concepts like wave-particle duality and superposition. "
        "Modern technologies like lasers and transistors rely on quantum principles.",
        # 7: solar panels
        "Solar panels convert sunlight into electricity using the photovoltaic effect. "
        "The photovoltaic effect was first observed by Alexandre Edmond Becquerel in 1839 "
        "and is closely related to Einstein's work on the photoelectric effect.",

        # Layer 3 – deeper children (biology branch)
        # 8: genetics
        "Genetics is the study of genes, heredity, and genetic variation in organisms. "
        "Gregor Mendel pioneered genetics through his work on pea plants. "
        "Modern genetics explains how traits are passed from parents to offspring.",
        # 9: DNA structure
        "DNA is a double helix molecule that carries genetic instructions for life. "
        "Watson and Crick discovered its structure in 1953. "
        "DNA replication enables cells to divide and pass genetic information.",
        # 10: speciation
        "Speciation is the evolutionary process by which populations become new species. "
        "It occurs when populations become reproductively isolated. "
        "Darwin's finches in the Galapagos are a classic example of adaptive radiation.",

        # Layer 4 – leaf nodes (physics)
        # 11: nuclear energy
        "Nuclear energy is released from atomic nuclei via fission or fusion. "
        "Nuclear power plants use fission to generate electricity. "
        "The mass-energy equivalence E=mc2 explains the enormous energy released.",
        # 12: lasers
        "A laser emits light through optical amplification by stimulated emission. "
        "Lasers are used in barcodes, surgery, welding, and fibre optics. "
        "The principle of stimulated emission comes from quantum mechanics.",
        # 13: transistors
        "A transistor is a semiconductor device used to amplify or switch signals. "
        "Transistors are the fundamental building blocks of modern electronics. "
        "They operate based on quantum mechanical principles like band theory.",

        # Layer 5 – leaf nodes (biology)
        # 14: CRISPR
        "CRISPR is a gene-editing technology that allows precise changes to DNA. "
        "It was adapted from a natural bacterial immune system. "
        "CRISPR has revolutionary potential for treating genetic diseases.",
        # 15: antibiotic resistance
        "Antibiotic resistance occurs when bacteria evolve to survive antibiotics. "
        "It is a direct example of natural selection in action. "
        "Overuse of antibiotics accelerates the spread of resistant strains.",
        # 16: human genome project
        "The Human Genome Project mapped all human genes from 1990 to 2003. "
        "It identified approximately 20,000-25,000 human genes. "
        "The project advanced understanding of genetics and disease.",

        # Cross-branch connectors – nodes that link physics and biology
        # 17: Marie Curie
        "Marie Curie was a Polish-born physicist and chemist who conducted pioneering research "
        "on radioactivity. She discovered the elements polonium and radium. "
        "She was the first woman to win a Nobel Prize.",
        # 18: Nobel Prize org
        "Nobel Prizes are awarded annually in Physics, Chemistry, Physiology or Medicine, "
        "Literature, and Peace. They were established by Alfred Nobel's will in 1895. "
        "The prizes recognise outstanding contributions to humanity.",
        # 19: radioactivity
        "Radioactivity is the spontaneous emission of radiation from unstable atomic nuclei. "
        "It was discovered by Henri Becquerel and studied by Marie Curie. "
        "Radioactive decay is used in medicine, energy, and carbon dating.",
        # 20: Einstein Nobel
        "Einstein won the Nobel Prize in Physics in 1921 for his explanation "
        "of the photoelectric effect, not for relativity. "
        "The Nobel committee specifically cited his discovery of the law of the photoelectric effect.",
        # 21: radiation in biology
        "Ionising radiation can cause mutations in DNA by breaking chemical bonds. "
        "This can lead to cancer but also enables medical imaging. "
        "Radiation biology studies how radiation affects living organisms.",
        # 22: carbon dating
        "Radiocarbon dating uses the decay of carbon-14 to determine the age of organic materials. "
        "It was developed by Willard Libby in 1949. "
        "The method relies on understanding radioactive half-life from nuclear physics.",
    ]

    logger.info("Embedding %d documents ...", len(raw_docs))
    embeddings = embed_model.embed_documents(raw_docs)

    graph = PersistentGraph()  # Use in-memory SQLite for demo
    for i, (doc, emb) in enumerate(zip(raw_docs, embeddings)):
        graph.add_node(i, doc, np.array(emb))

    # --- Pre-defined edges (semantic bridge relations) ---
    # Physics branch (depth 5)
    edges = [
        # Layer 0 -> Layer 1
        (0, 2, "developed theory of relativity"),
        (0, 3, "explained photoelectric effect"),
        (1, 4, "proposed natural selection"),
        # Layer 1 -> Layer 2
        (2, 5, "contains equation E=mc2"),
        (5, 2, "derived from relativity"),
        (3, 6, "motivated quantum mechanics"),
        (6, 3, "explained by photoelectric effect"),
        (3, 7, "enabled solar panel technology"),
        (7, 3, "based on photoelectric effect"),
        (4, 8, "underpins genetics"),
        (4, 10, "drives speciation"),
        # Layer 2 -> Layer 3
        (5, 11, "explains nuclear energy"),
        (11, 5, "based on E=mc2"),
        (6, 12, "enabled laser technology"),
        (12, 6, "applies quantum mechanics"),
        (6, 13, "enabled transistor invention"),
        (13, 6, "relies on quantum mechanics"),
        (8, 9, "encoded in DNA"),
        (9, 8, "explains heredity"),
        (10, 15, "illustrated by antibiotic resistance"),
        # Layer 3 -> Layer 4 (leaf)
        (9, 14, "enabled CRISPR gene editing"),
        (14, 9, "modifies DNA"),
        (9, 16, "mapped by Human Genome Project"),
        (16, 9, "studied human DNA"),
        (15, 8, "example of natural selection"),
        # Cross-branch: Nobel path (Einstein <-> Curie <-> Nobel org)
        (0, 20, "won Nobel Prize"),
        (20, 0, "Einstein's Nobel award"),
        (0, 17, "both Nobel laureates"),
        (17, 0, "both Nobel laureates"),
        (17, 19, "discovered radioactivity"),
        (19, 17, "discovered by Marie Curie"),
        (20, 18, "is a Nobel laureate"),
        (18, 20, "awarded Nobel to"),
        (17, 18, "is a Nobel laureate"),
        (18, 17, "awarded Nobel to"),
        # Cross-branch: radioactivity <-> biology
        (19, 21, "causes DNA mutations"),
        (21, 19, "emitted by radioactive decay"),
        (21, 9, "can damage DNA structure"),
        (9, 21, "susceptible to radiation"),
        (19, 22, "enables carbon dating"),
        (22, 19, "uses radioactive decay"),
        # physics <-> biology direct
        (11, 22, "provides decay knowledge"),
        (22, 11, "applies nuclear physics"),
    ]

    for src, tgt, rel in edges:
        graph.add_edge(src, tgt, rel)

    graph.build_embedding_index()
    stats = graph.get_stats()
    logger.info(f"Graph built: {stats['num_nodes']} nodes, {stats['num_edges']} edges")
    return graph

def demo_dynamic():
    """
    Demo showing how to use DynamicGraphBuilder for flexible graph construction.
    """
    print("\n" + "=" * 70)
    print("Dynamic Graph Builder Demo")
    print("=" * 70)

    # Create a new dynamic graph builder
    builder = DynamicGraphBuilder()

    # Add documents dynamically
    print("\n[Step 1] Adding documents...")
    docs = [
        "Python is a high-level programming language known for its simplicity.",
        "Machine learning is a subset of artificial intelligence.",
        "TensorFlow is an open-source library for machine learning developed by Google.",
        "PyTorch is another popular machine learning framework created by Facebook.",
        "Deep learning uses neural networks with many layers.",
        "Neural networks are inspired by the human brain structure.",
    ]
    node_ids = builder.add_documents(docs)
    print(f"Added {len(docs)} documents with node_ids: {node_ids}")
    print(f"Stats: {builder.get_stats()}")

    # Add edges dynamically
    print("\n[Step 2] Adding edges...")
    edges = [
        (0, 1, "used_in"),  # Python -> ML
        (1, 2, "implemented_by"),  # ML -> TensorFlow
        (1, 3, "implemented_by"),  # ML -> PyTorch
        (1, 4, "includes"),  # ML -> Deep Learning
        (4, 5, "based_on"),  # Deep Learning -> Neural Networks
        (2, 4, "supports"),  # TensorFlow -> Deep Learning
        (3, 4, "supports"),  # PyTorch -> Deep Learning
    ]
    builder.add_edges(edges)
    print(f"Added {len(edges)} edges")
    print(f"Stats: {builder.get_stats()}")

    # Build the graph
    print("\n[Step 3] Building graph...")
    graph = builder.build()

    # Test queries
    print("\n[Step 4] Testing queries...")
    q1 = "What frameworks support deep learning?"
    r1 = multi_hop_search(graph, q1, chat_model, max_hops=2)
    print_paths("Dynamic Q1", q1, r1)

    q2 = "How are neural networks related to AI?"
    r2 = multi_hop_search(graph, q2, chat_model, max_hops=3)
    print_paths("Dynamic Q2", q2, r2)

    print("=" * 70)
    print("DYNAMIC DEMO COMPLETE")
    print("=" * 70)

    return r1, r2

def test_persistence_and_hybrid_search():
    """
    Test persistence and hybrid search functionality.
    """
    print("\n" + "=" * 70)
    print("Testing Persistence & Hybrid Search")
    print("=" * 70)

    db_path = "test_graph_temp.db"

    # Clean up if exists
    if os.path.exists(db_path):
        os.remove(db_path)

    try:
        # ---- Test 1: Create and save graph ----
        print("\n[Test 1] Creating persistent graph...")
        builder = DynamicGraphBuilder(db_path=db_path)

        docs = [
            "Albert Einstein developed the theory of relativity.",
            "The theory of relativity includes E=mc2 equation.",
            "E=mc2 shows mass-energy equivalence.",
            "Nuclear energy is based on mass-energy equivalence.",
            "Marie Curie won Nobel Prize for radioactivity research.",
            "Radioactivity involves emission of radiation from atoms.",
        ]
        builder.add_documents(docs)

        edges = [
            (0, 1, "developed"),
            (1, 2, "contains"),
            (2, 3, "enables"),
            (4, 5, "studied"),
        ]
        builder.add_edges(edges)

        graph = builder.build()
        stats = graph.get_stats()
        print(f"Created graph: {stats}")

        # ---- Test 2: Query with hybrid search ----
        print("\n[Test 2] Testing hybrid search (embedding + FTS5)...")

        # Query that should match both semantically and textually
        query1 = "Einstein relativity nuclear energy"
        print(f"\nQuery: '{query1}'")
        q_emb = np.array(embed_model.embed_query(query1))
        matches = graph.hybrid_search(query1, q_emb, top_k=3)

        print("\nHybrid search results:")
        for i, match in enumerate(matches, 1):
            print(f"  {i}. Node {match.node_id} (score: {match.combined_score:.3f})")
            print(f"     Emb: {match.embedding_score:.3f}, FTS: {match.fts_score:.3f}")
            print(f"     Text: {match.text[:80]}...")

        # Query that relies more on FTS
        query2 = "Nobel Prize radioactivity"
        print(f"\nQuery: '{query2}'")
        q_emb2 = np.array(embed_model.embed_query(query2))
        matches2 = graph.hybrid_search(query2, q_emb2, top_k=3)

        print("\nHybrid search results:")
        for i, match in enumerate(matches2, 1):
            print(f"  {i}. Node {match.node_id} (score: {match.combined_score:.3f})")
            print(f"     Emb: {match.embedding_score:.3f}, FTS: {match.fts_score:.3f}")
            print(f"     Text: {match.text[:80]}...")

        # ---- Test 3: Compare hybrid vs pure embedding ----
        print("\n[Test 3] Comparing hybrid vs pure embedding search...")
        query3 = "Einstein"
        q_emb3 = np.array(embed_model.embed_query(query3))

        # Hybrid search
        hybrid_results = graph.search_entry_nodes(query3, q_emb3, top_k=3, use_hybrid=True)
        print(f"\nHybrid search for '{query3}':")
        for nid, score in hybrid_results:
            print(f"  Node {nid}: {score:.3f} - {graph.node_text(nid)[:60]}...")

        # Pure embedding search
        embed_results = graph.search_entry_nodes(query3, q_emb3, top_k=3, use_hybrid=False)
        print(f"\nPure embedding search for '{query3}':")
        for nid, score in embed_results:
            print(f"  Node {nid}: {score:.3f} - {graph.node_text(nid)[:60]}...")

        # ---- Test 4: Reload from disk ----
        print("\n[Test 4] Testing persistence - reloading from disk...")

        # Load existing database
        loaded_graph = PersistentGraph()
        loaded_graph.build_embedding_index()
        loaded_stats = loaded_graph.get_stats()
        print(f"Loaded graph: {loaded_stats}")

        # Verify data integrity
        assert loaded_stats['num_nodes'] == stats['num_nodes'], "Node count mismatch!"
        assert loaded_stats['num_edges'] == stats['num_edges'], "Edge count mismatch!"

        # Test query on reloaded graph
        test_query = "relativity"
        test_emb = np.array(embed_model.embed_query(test_query))
        reloaded_matches = loaded_graph.hybrid_search(test_query, test_emb, top_k=2)

        print(f"\nQuery on reloaded graph: '{test_query}'")
        for match in reloaded_matches:
            print(f"  Node {match.node_id}: {match.combined_score:.3f}")

        print("\n✓ All persistence tests passed!")

    finally:
        # Cleanup
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"\nCleaned up temporary database: {db_path}")

    print("=" * 70)
    print("PERSISTENCE & HYBRID SEARCH TEST COMPLETE")
    print("=" * 70)

def print_paths(title: str, query: str, result: Dict):
    print("-" * 70)
    print(f"Query: {query}")
    print(f"Entry nodes: {result['entry_nodes']}")
    print(f"Visited: {result['visited']} nodes")
    print(f"Paths:")
    if result["paths"]:
        for p in result["paths"]:
            print(f"  Hop {p['hop']}: node_{p['node_id']} via '{p['relation']}'")
    else:
        print("  (no expansion beyond entry nodes)")
    print(f"Answer: {result['answer']}")
    print()


def demo():
    print("=" * 70)
    print("Multi-hop GraphRAG Demo")
    print("=" * 70)

    # Use the original hardcoded demo graph
    graph = build_demo_graph()

    # ---- Query 1: 2-hop (Einstein -> relativity -> E=mc2 -> nuclear) ----
    q1 = "What did Einstein discover that leads to nuclear energy?"
    r1 = multi_hop_search(graph, q1, chat_model, max_hops=2)
    print_paths("Q1", q1, r1)

    # ---- Query 2: 2-hop cross-entity (Curie -> Nobel -> Einstein) ----
    q2 = "What is the connection between Marie Curie and the Nobel Prize?"
    r2 = multi_hop_search(graph, q2, chat_model, max_hops=2)
    print_paths("Q2", q2, r2)

    # ---- Query 3: 3-hop (Einstein -> photoelectric -> Einstein -> E=mc2 -> ...) ----
    q3 = "How does Einstein's work relate to modern electronics?"
    r3 = multi_hop_search(graph, q3, chat_model, max_hops=3)
    print_paths("Q3", q3, r3)

    print("=" * 70)
    print("DEMO COMPLETE - Original Hardcoded Graph")
    print("=" * 70)

    return r1, r2, r3

if __name__ == "__main__":
    # Run original demo
    print("\n>>> Running original demo...")
    r1, r2, r3 = demo()

    # Run dynamic demo
    print("\n>>> Running dynamic demo...")
    r4, r5 = demo_dynamic()

    # Run persistence and hybrid search test
    print("\n>>> Running persistence & hybrid search test...")
    test_persistence_and_hybrid_search()

    print("\n" + "=" * 70)
    print("ALL TESTS COMPLETE! ✨")
    print("=" * 70)
