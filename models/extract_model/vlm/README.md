---
license: apache-2.0
language:
- zh
- en
pipeline_tag: image-text-to-text
library_name: transformers
---


<div align="center">

<p align="center">
    <img src="https://hotelll.github.io/MinerU2.5-Pro/MinerU25-Pro-LOGO.png" style="max-width:45%; height:auto;" />
<p>

<h1 align="center" style="font-size: 28px">
MinerU2.5-Pro: Pushing the Limits of Data-Centric Document Parsing at Scale
</h1>

<p align="center">
	<a href="https://arxiv.org/abs/2604.04771"><img src="https://img.shields.io/badge/arxiv-Tech_Report-052962?logo=arxiv&logoColor=white" alt="Tech Report" /></a>
	<a href="https://huggingface.co/opendatalab/MinerU2.5-Pro-2604-1.2B"><img src="https://img.shields.io/badge/HuggingFace-black.svg?logo=data:image/png;base64,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&labelColor=white" alt="HuggingFace" /></a>
	<a href="https://modelscope.cn/models/OpenDataLab/MinerU2.5-Pro-2604-1.2B"><img src="https://img.shields.io/badge/ModelScope-black?logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjIzIiBoZWlnaHQ9IjIwMCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KCiA8Zz4KICA8dGl0bGU+TGF5ZXIgMTwvdGl0bGU+CiAgPHBhdGggaWQ9InN2Z18xNCIgZmlsbD0iIzYyNGFmZiIgZD0ibTAsODkuODRsMjUuNjUsMGwwLDI1LjY0OTk5bC0yNS42NSwwbDAsLTI1LjY0OTk5eiIvPgogIDxwYXRoIGlkPSJzdmdfMTUiIGZpbGw9IiM2MjRhZmYiIGQ9Im05OS4xNCwxMTUuNDlsMjUuNjUsMGwwLDI1LjY1bC0yNS42NSwwbDAsLTI1LjY1eiIvPgogIDxwYXRoIGlkPSJzdmdfMTYiIGZpbGw9IiM2MjRhZmYiIGQ9Im0xNzYuMDksMTQxLjE0bC0yNS42NDk5OSwwbDAsMjIuMTlsNDcuODQsMGwwLC00Ny44NGwtMjIuMTksMGwwLDI1LjY1eiIvPgogIDxwYXRoIGlkPSJzdmdfMTciIGZpbGw9IiMzNmNmZDEiIGQ9Im0xMjQuNzksODkuODRsMjUuNjUsMGwwLDI1LjY0OTk5bC0yNS42NSwwbDAsLTI1LjY0OTk5eiIvPgogIDxwYXRoIGlkPSJzdmdfMTgiIGZpbGw9IiMzNmNmZDEiIGQ9Im0wLDY0LjE5bDI1LjY1LDBsMCwyNS42NWwtMjUuNjUsMGwwLC0yNS42NXoiLz4KICA8cGF0aCBpZD0ic3ZnXzE5IiBmaWxsPSIjNjI0YWZmIiBkPSJtMTk4LjI4LDg5Ljg0bDI1LjY0OTk5LDBsMCwyNS42NDk5OWwtMjUuNjQ5OTksMGwwLC0yNS42NDk5OXoiLz4KICA8cGF0aCBpZD0ic3ZnXzIwIiBmaWxsPSIjMzZjZmQxIiBkPSJtMTk4LjI4LDY0LjE5bDI1LjY0OTk5LDBsMCwyNS42NWwtMjUuNjQ5OTksMGwwLC0yNS42NXoiLz4KICA8cGF0aCBpZD0ic3ZnXzIxIiBmaWxsPSIjNjI0YWZmIiBkPSJtMTUwLjQ0LDQybDAsMjIuMTlsMjUuNjQ5OTksMGwwLDI1LjY1bDIyLjE5LDBsMCwtNDcuODRsLTQ3Ljg0LDB6Ii8+CiAgPHBhdGggaWQ9InN2Z18yMiIgZmlsbD0iIzM2Y2ZkMSIgZD0ibTczLjQ5LDg5Ljg0bDI1LjY1LDBsMCwyNS42NDk5OWwtMjUuNjUsMGwwLC0yNS42NDk5OXoiLz4KICA8cGF0aCBpZD0ic3ZnXzIzIiBmaWxsPSIjNjI0YWZmIiBkPSJtNDcuODQsNjQuMTlsMjUuNjUsMGwwLC0yMi4xOWwtNDcuODQsMGwwLDQ3Ljg0bDIyLjE5LDBsMCwtMjUuNjV6Ii8+CiAgPHBhdGggaWQ9InN2Z18yNCIgZmlsbD0iIzYyNGFmZiIgZD0ibTQ3Ljg0LDExNS40OWwtMjIuMTksMGwwLDQ3Ljg0bDQ3Ljg0LDBsMCwtMjIuMTlsLTI1LjY1LDBsMCwtMjUuNjV6Ii8+CiA8L2c+Cjwvc3ZnPg==&labelColor=white" alt="ModelScope" /></a>
</p>
</div>

---

<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/leaderboard.png"/>
<p>

### News 🚀🚀

  2026.05.21 🎉🎉 We are pleased to announce the release of MinerU2.5-Pro-2605, an updated version of our model.

  - **Enhanced Layout Detection**
    To address the category misclassification issues observed in the 2604 version during layout detection, we conducted a comprehensive data cleaning process. This has substantially reduced category errors in layout detection. Notably, the missed detection rate for the `image_block` category has been significantly reduced.
  
  - **Improved Image Analysis**
    To overcome the limitations of the 2604 version in image analysis, we constructed a large-scale training dataset for this task. As a result, the 2605 version demonstrates markedly enhanced recognition capabilities across a wide range of charts, flowcharts, and even seals.

  - **Comparable Performance on OmniDocBench**
    The 2605 version primarily focuses on enhancing user experience, with performance metrics showing only marginal differences compared to the 2604 version. A detailed comparison of the metrics is presented below:

    <table style="width:100%; border-collapse: collapse;">
        <caption>MinerU2.5-Pro-2605 vs. MinerU2.5-Pro-2604 on OmniDocBench (v1.6_full)</caption>
        <thead>
            <tr>
                <th>Model Version</th>
                <th>Overall&#x2191;</th>
                <th>Text<sup>Edit</sup>&#x2193;</th>
                <th>Formula<sup>CDM</sup>&#x2191;</th>
                <th>Table<sup>TEDS</sup>&#x2191;</th>
                <th>Table<sup>TEDS-S</sup>&#x2191;</th>
                <th>Read Order<sup>Edit</sup>&#x2193;</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>MinerU2.5-Pro-2605</td>
                <td>95.72</td>
                <td>0.036</td>
                <td>97.15</td>
                <td>93.62</td>
                <td>96.01</td>
                <td>0.123</td>
            </tr>
            <tr>
                <td>MinerU2.5-Pro-2604</td>
                <td>95.69</td>
                <td>0.036</td>
                <td>97.29</td>
                <td>93.42</td>
                <td>95.92</td>
                <td>0.120</td>
            </tr>
        </tbody>
    </table>


### 🏆 Unmatched SOTA Performance
**MinerU2.5-Pro** is our latest document parsing model (PDF-to-Markdown) that establishes a new industry standard. By focusing entirely on data engineering without altering the original 1.2B-parameter architecture, it delivers exceptional results across the board:

**1. Defeating Leading Models on OmniDocBench v1.6**
On the newly proposed, highly rigorous OmniDocBench v1.6, MinerU2.5-Pro achieves the absolute **SOTA overall score of 95.69**. It comprehensively outperforms both top-tier specialized OCR models (**GLM-OCR, PaddleOCR-VL-1.5**) and massive frontier VLMs (**Gemini 3 Pro, Qwen3-VL-235B**). 

**2. Massive Leap from MinerU 2.5 via Data Engineering**
Compared to the previous MinerU 2.5 baseline, the overall score skyrocketed from **92.98 to 95.69**. This breakthrough was achieved not by scaling model parameters, but through meticulous data engineering—drastically expanding data scale, enriching distribution and difficulty diversity, and systematically elevating annotation quality.

**3. Exceptional Modality-Specific Breakthroughs**
*   📊 **Table Parsing:** Evaluated across 5 diverse table benchmarks, MinerU2.5-Pro dominates the leaderboard. It outperforms the 2nd place model by **1.39 points** and surpasses the original MinerU by **3.06 points** (with Table TEDS jumping specifically by **+5.54** on OmniDocBench).
*   🧮 **Formulas & Text:** Achieves SOTA levels with Dense Formula parsing (CDM) reaching **97.29** (+1.70), and Text Edit Distance dropping to an industry-best **0.036**.

**4. ✨ New Practical Capabilities**
Beyond metric improvements, MinerU2.5-Pro now natively supports: **Image & Chart Parsing**, **Truncated Paragraph Merging**, **Cross-Page Table Merging** and **In-Table Image Recognition**.

---

### 💡 How We Achieved It: The Data Engine
Current SOTA models (regardless of architecture) consistently fail on the same set of complex layouts. We realized the true bottleneck is **training data deficiency and annotation noise**. To fix this, we built a novel Data Engine:

1.  **Difficulty & Diversity-Aware Scaling:** We expanded the training corpus from under 10M to **65.5M pages**, heavily targeting long-tail hard samples while controlling distribution shifts.
2.  **Solving the "Annotation Paradox":** Complex tables and dense formulas usually suffer from noisy automatic labels. We generated ultra-reliable annotations using **Cross-Model Consistency Verification (CMCV)** and an iterative **Judge-and-Refine** pipeline.
3.  **3-Stage Progressive Training:** We maximized data utility by matching data quality tiers to a structured training pipeline: Large-scale Pre-training ➡️ High-quality Hard-Sample Fine-Tuning ➡️ GRPO Format Alignment.

**Bottom Line:** MinerU2.5-Pro proves that systematic data engineering is the ultimate lever for document parsing, providing the most accurate structural extraction available today for LLM data pipelines and advanced RAG systems.



# 1. Quick Start
For convenience, we provide `mineru-vl-utils`, a Python package that simplifies the process of sending requests and handling responses from MinerU2.5-Pro Vision-Language Model. Here we give some examples to use MinerU2.5-Pro. For more information and usages, please refer to [mineru-vl-utils](https://github.com/opendatalab/mineru-vl-utils/tree/main).

📌 We strongly recommend using vllm for inference, as the `vllm-async-engine` can achieve a concurrent inference speed of **2.12 fps** on one A100.

## 1.1. Install packages
```bash
# For `transformers` backend
pip install "mineru-vl-utils[transformers]"
# For `vllm-engine` and `vllm-async-engine` backend
pip install "mineru-vl-utils[vllm]"
```

## 1.2. `transformers` Example

```python
from transformers import AutoProcessor, Qwen2VLForConditionalGeneration
from PIL import Image
from mineru_vl_utils import MinerUClient

# for transformers>=4.56.0
model = Qwen2VLForConditionalGeneration.from_pretrained(
    "opendatalab/MinerU2.5-Pro-2604-1.2B", dtype="auto", device_map="auto"
)

processor = AutoProcessor.from_pretrained(
    "opendatalab/MinerU2.5-Pro-2604-1.2B", use_fast=True
)

client = MinerUClient(
    backend="transformers", model=model, processor=processor,
    image_analysis=False # default False, set True to enable image/chart analysis
)

print(client.two_step_extract(Image.open("/path/to/page.png")))
```

## 1.3. `vllm-engine` Example (Recommended!)

```python
from vllm import LLM
from PIL import Image
from mineru_vl_utils import MinerUClient
from mineru_vl_utils import MinerULogitsProcessor  # if vllm>=0.10.1

llm = LLM(
    model="opendatalab/MinerU2.5-Pro-2604-1.2B",
    logits_processors=[MinerULogitsProcessor]  # if vllm>=0.10.1
)

client = MinerUClient(
    backend="vllm-engine", vllm_llm=llm,
    image_analysis=False # default False, set True to enable image/chart analysis
)

print(client.two_step_extract(Image.open("/path/to/page.png")))
```

## 1.4. JSON result to Markdown (enable truncated paragraph merging)

```python
from mineru_vl_utils.post_process import json2md

# ... omit client initialize
content_list = client.two_step_extract(Image.open("path/to/page.png"))
md_res = json2md(content_list)
```

🚧 Cross-Page Table Merging: Currently under integration. Stay tuned!


# 2. Performance
## 2.1. End-to-End Document Parsing on OmniDocBench v1.6
<p align="left">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/end2end.png"/>
<p>

## 2.2. Text Recognition
<p align="left">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/text_performance.png" style="max-width:60%; height:auto;" />
</p>

## 2.3. Formula Recognition
<p align="left">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/formula_performance.png" style="max-width:80%; height:auto;" />
<p>

## 2.4. Table Recognition
<p align="left">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/table_performance.png" style="max-width:100%; height:auto;" />
<p>

# 3. Showcase
## 3.1. Basic Parsing Capability

<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/text.png"/>
<p>

<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/formula.png"/>
<p>

<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/table.png"/>
<p>

## 3.2. Extra Supported Features
<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/image_analysis.png"/>
<p>

<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/paragraph_merge.png"/>
<p>

<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/table_w_img.png"/>
<p>

<p align="center">
    <img alt="Image" src="https://hotelll.github.io/MinerU2.5-Pro/table_merge.png"/>
<p>


# 4. Acknowledgement & Citation
We would like to thank [Qwen Team](https://github.com/QwenLM), [vLLM](https://github.com/vllm-project/vllm), [OmniDocBench](https://github.com/opendatalab/OmniDocBench), [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), [UniMERNet](https://github.com/opendatalab/UniMERNet), [DocLayout-YOLO](https://github.com/opendatalab/DocLayout-YOLO) for providing valuable code and models. We also appreciate everyone's contribution to this open-source project!

If you find our work useful in your research, please consider giving a star ⭐ and citation 📝 :
```BibTeX
@misc{wang2026mineru25propushinglimitsdatacentric,
      title={MinerU2.5-Pro: Pushing the Limits of Data-Centric Document Parsing at Scale}, 
      author={Bin, Wang and Tianyao, He and Linke, Ouyang and Fan, Wu and Zhiyuan, Zhao and Tao, Chu and Yuan, Qu and Zhenjiang, Jin and Weijun, Zeng and Ziyang, Miao and Bangrui, Xu and Junbo, Niu and others},
      year={2026},
      eprint={2604.04771},
      archivePrefix={arXiv},
      primaryClass={cs.CV},
      url={https://arxiv.org/abs/2604.04771}, 
}
```