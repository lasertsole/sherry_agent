import os
import networkx as nx
from typing import Dict, Any
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches


plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

NODE_COLORS = {
    "root": "#FF6B6B",
    "dimension": "#4ECDC4",
    "hypothesis": "#45B7D1",
    "evidence": "#96CEB4",
    "interdisciplinary_bridge": "#DDA0DD",
    "default": "#95A5A6"
}

EDGE_COLORS = {
    "supports": "#2ECC71",
    "contradicts": "#E74C3C",
    "hyperedge_virtual": "#F39C12",
    "ibn_source": "#9B59B6",
    "ibn_target": "#9B59B6",
    "default": "#34495E"
}


def visualize_graph(graph: nx.DiGraph, hyperedges: Dict[str, Any], 
                    stage_name: str, output_dir: str = "output") -> str:
    os.makedirs(output_dir, exist_ok=True)
    
    fig, ax = plt.subplots(1, 1, figsize=(16, 12))
    
    if len(graph.nodes) == 0:
        ax.text(0.5, 0.5, "Empty Graph - No nodes yet", 
                ha='center', va='center', fontsize=16)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis('off')
    else:
        pos = nx.spring_layout(graph, k=2, iterations=50, seed=42)
        
        node_colors = []
        node_labels = {}
        
        for node_id, data in graph.nodes(data=True):
            node_type = data.get("node_type", "default")
            color = NODE_COLORS.get(node_type, NODE_COLORS["default"])
            node_colors.append(color)
            
            label = data.get("label", node_id)
            if len(label) > 30:
                label = label[:27] + "..."
            node_labels[node_id] = label
        
        edge_colors = []
        edge_widths = []
        
        for u, v, data in graph.edges(data=True):
            edge_type = data.get("edge_type", "default")
            color = EDGE_COLORS.get(edge_type, EDGE_COLORS["default"])
            edge_colors.append(color)
            
            confidence = data.get("confidence", 0.5)
            edge_widths.append(0.5 + confidence * 2)
        
        nx.draw_networkx_edges(graph, pos, ax=ax, 
           edge_color=edge_colors,
           width=edge_widths,
           alpha=0.6,
           arrows=True,
           arrowsize=15,
           connectionstyle="arc3,rad=0.1"
        )

        nx.draw_networkx_nodes(graph, pos, ax=ax,
           node_color=node_colors,
           node_size=800,
           alpha=0.9
        )
        
        nx.draw_networkx_labels(graph, pos, ax=ax,
            labels=node_labels,
            font_size=6,
            font_weight="bold",
            font_family='Microsoft YaHei'
        )

        legend_elements = []
        for node_type, color in NODE_COLORS.items():
            if node_type != "default":
                legend_elements.append(mpatches.Patch(color=color, label=node_type))
        
        for edge_type, color in EDGE_COLORS.items():
            if edge_type != "default":
                legend_elements.append(mpatches.Patch(color=color, label=edge_type))
        
        ax.legend(handles=legend_elements, loc='upper left', fontsize=8)
    
    ax.set_title(f"AGoT Graph - {stage_name}\nNodes: {len(graph.nodes)}, Edges: {len(graph.edges)}", 
                 fontsize=14, fontweight='bold')
    ax.axis('off')
    
    safe_name = stage_name.replace(" ", "_").replace("/", "_")
    filepath = os.path.join(output_dir, f"stage_{safe_name}.png")
    plt.tight_layout()
    plt.savefig(filepath, dpi=150, bbox_inches='tight', 
                facecolor='white', edgecolor='none')
    plt.close()
    
    return filepath


def visualize_layers(graph: nx.DiGraph, layers: Dict[str, set],
                     stage_name: str, output_dir: str = "output") -> str:
    os.makedirs(output_dir, exist_ok=True)
    
    fig, ax = plt.subplots(1, 1, figsize=(18, 10))
    
    if len(graph.nodes) == 0:
        ax.text(0.5, 0.5, "Empty Graph", ha='center', va='center', fontsize=16)
        ax.axis('off')
    else:
        pos = nx.spring_layout(graph, k=2.5, iterations=50, seed=42)
        
        layer_colors = plt.cm.Set3.colors
        
        node_colors = []
        for node_id, data in graph.nodes(data=True):
            layer_id = data.get("layer_id", "default")
            layer_idx = list(layers.keys()).index(layer_id) if layer_id in layers else 0
            node_colors.append(layer_colors[layer_idx % len(layer_colors)])
        
        node_labels = {}
        for node_id, data in graph.nodes(data=True):
            label = data.get("label", node_id)
            if len(label) > 25:
                label = label[:22] + "..."
            node_labels[node_id] = label
        
        nx.draw_networkx_edges(graph, pos, ax=ax, alpha=0.4, arrows=True)
        nx.draw_networkx_nodes(graph, pos, ax=ax, node_color=node_colors, 
                               node_size=1000, alpha=0.8)
        nx.draw_networkx_labels(
            graph, pos,
            ax=ax,
            labels=node_labels,
            font_size=5,
            font_family='Microsoft YaHei'
        )
        
        legend_elements = []
        for i, layer_id in enumerate(layers.keys()):
            legend_elements.append(mpatches.Patch(
                color=layer_colors[i % len(layer_colors)], 
                label=layer_id
            ))
        ax.legend(handles=legend_elements, loc='upper left', fontsize=8)
    
    ax.set_title(f"AGoT Layers - {stage_name}", fontsize=14, fontweight='bold')
    ax.axis('off')
    
    safe_name = stage_name.replace(" ", "_").replace("/", "_")
    filepath = os.path.join(output_dir, f"layers_{safe_name}.png")
    plt.tight_layout()
    plt.savefig(filepath, dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    plt.close()
    
    return filepath