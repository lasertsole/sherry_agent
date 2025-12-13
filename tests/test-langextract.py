import os
import sys
import textwrap
from pathlib import Path
from dotenv import load_dotenv
from langextract import factory

# 添加项目根目录到 Python 搜索路径
project_root = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(project_root))

import langextract as lx
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ENV_PATH = Path(__file__).parent.parent / '.env'
load_dotenv(ENV_PATH, override=True)
api_key = os.getenv("CHAT_API_KEY")
api_name = os.getenv("CHAT_API_NAME")
base_url = os.getenv("CHAT_API_BASE")

input_text: str = textwrap.dedent("""\
新华社北京4月11日电 题：习近平主席寄望中美青年

新华社记者马卓言

4月10日，国家主席习近平向中美“乒乓外交”55周年纪念大会暨中美青少年体育交流系列活动启动仪式致贺信，对两国青年一代寄予厚望——

“希望两国各界人士特别是青年一代从历史中汲取智慧和力量，在交流合作中相知相亲，在互学互鉴中携手前行，共同拉紧友谊纽带，为推动中美关系稳定、健康、可持续发展作出新贡献。”

1971年4月，美国乒乓球队应中方邀请，历史性地访问中国，中美“乒乓外交”打开了两国人民友好往来的大门。“小球转动大球”的创举超越了意识形态分歧，不仅开启了中美两国关系的新篇章，甚至对当时的世界格局产生了深远影响。

“中美关系的大门是由人民打开的。”习近平主席对这段历史佳话有着深刻的论断，“是时代潮流让我们走向彼此，是共同利益让中美超越分歧，是人民愿望让两国打破坚冰。”

历史长河大浪淘沙，最终沉淀下来的总是最有价值的东西。“乒乓外交”的历史证明，中美友好的事业必须从人民中找到根基，从人民中集聚力量，由人民来共同完成。而青年则是人民友好的未来和希望。

中美青年一代要从历史中汲取智慧和力量，自觉投身于人民友谊这件大事中去。

“乒乓外交”的成功，其意义在于以体育交流为纽带，推动两国人民在接触互动中增进相互了解，消融隔阂的坚冰。国之交在于民相亲，民相亲可促国之信。

新时代的两国青年，更应传承这份精神，通过交流打破偏见和隔阂，建立对彼此正确的认知。无论是球台对垒、赛场切磋，还是文化对话、学术交流，中美青年的每一次互动，都是在为两国关系行稳致远积累民意基础，为中美友好的大树培土固本。

习近平主席深刻指出，“中美关系的根基由人民浇筑，未来靠青年创造”“无论形势如何变化，中美两国人民交流合作的愿望不会改变，两国青少年相知相亲的情谊不会改变”。

中美两国虽然历史文化、社会制度、发展道路不同，但人民都善良友好、勤劳务实，都爱祖国、爱家庭、爱生活，都对彼此抱有好感和兴趣。正是善意友好的涓滴汇流，让宽广太平洋不再是天堑；正是人民的双向奔赴，让中美关系一次次从低谷重回正道。

近年来，从推进“未来5年邀请5万名美国青少年来华交流学习”等倡议，到为包括美国在内的数十国公民提供过境免签或单方面免签便利，一个愈发自信、开放、包容的中国，热情欢迎包括美国人民在内的各国人民来华感受真实立体的现代中国，结识真诚友好的中国人民，在文明交流互鉴中体悟相互尊重、和平共处、合作共赢的正确相处之道。

诚如习近平主席指出的那样，中美关系的大门一旦打开，就不会再被关上。两国人民友好事业一经开启，就不会半途而废。人民友谊之树已经长大，一定能经风历雨。

此次中美“乒乓外交”55周年纪念大会期间，中美青少年体育交流系列活动正式启动。赓续“乒乓外交”精神，新一轮丰富多彩的青少年体育交流，正在为中美人民友好交往注入新的青春活力。

小小银球，见证历史，也照亮未来。今年是中美关系的“大年”。期待两国各界更多人士特别是青年再续“乒乓情缘”，成为中美人民友好事业的参与者、支持者、推动者，为推动中美关系发展贡献更大的民间力量。""")

def main():
    prompt = textwrap.dedent("""\
        Extract ALL relationship triples from the text to build a comprehensive knowledge graph.

        === OUTPUT FORMAT ===
        For each relationship, extract the **subject entity** as the `extraction_text`.
        Store the full triple details in `attributes`.

        === ENTITY TYPES ===
        - PERSON: Individual people (e.g., 习近平, 马卓言)
        - ORGANIZATION: Groups, institutions, teams (e.g., 美国乒乓球队, 新华社)
        - LOCATION: Places, countries, regions (e.g., 中国, 美国, 北京)
        - EVENT: Specific events or activities (e.g., 乒乓外交, 纪念大会)
        - CONCEPT: Abstract ideas, policies, initiatives (e.g., 中美关系, 人民友谊)

        === RELATION TYPES ===
        Use these standardized relation types when possible:
        - participates_in / hosts / attends (参与/举办/出席)
        - visits / invites (访问/邀请)
        - states / emphasizes / points_out (指出/强调/表示)
        - promotes / supports / advocates (推动/支持/倡导)
        - opens / enables / facilitates (开启/促进/便利)
        - inherits / continues / carries_forward (传承/延续)
        - impacts / influences / affects (影响)
        - belongs_to / is_part_of (属于)

        === EXTRACTION RULES ===
        1. COMPLETENESS: Extract EVERY relationship mentioned in the text
        2. EXACT TEXT: The `extraction_text` MUST be an exact substring from the source text (e.g., the subject entity).
        3. NO DUPLICATES: Avoid extracting the same relationship twice
        4. SPECIFIC RELATIONS: Use specific predicates, avoid generic "related_to"
        5. CONCRETE ENTITIES: Both subject and object must be identifiable entities
        6. CONTEXT PRESERVATION: Include temporal/spatial context in attributes

        === ATTRIBUTES TO INCLUDE ===
        - entity_types: dict {"subject_type": "...", "object_type": "..."}
        - temporal_context: string (time reference if mentioned, e.g., "1971年4月")

        === STEP-BY-STEP PROCESS ===
        1. Identify all entities (PERSON, ORG, LOC, EVENT, CONCEPT)
        2. For each sentence, find relationships between entities
        3. Store relation details (subject, relation, object) in `attributes`.
        4. Verify both subject and object are concrete entities
        5. Check for duplicates before finalizing

        IMPORTANT: Be thorough! Don't miss implicit relationships.""")

    # 2. Provide a high-quality example to guide the model
    examples = [
        lx.data.ExampleData(
            text="1971年4月，美国乒乓球队应中方邀请，历史性地访问中国，中美'乒乓外交'打开了两国人民友好往来的大门。",
            extractions=[
                lx.data.Extraction(
                    extraction_class="relationship_triple",
                    extraction_text="美国乒乓球队应中方邀请，历史性地访问中国",
                    attributes={
                        "subject": "美国乒乓球队",
                        "relation": "visits",
                        "object": "中国",
                        "entity_types": {
                            "subject_type": "ORGANIZATION",
                            "object_type": "LOCATION"
                        },
                        "temporal_context": "1971年4月"
                    }
                ),
                lx.data.Extraction(
                    extraction_class="relationship_triple",
                    extraction_text="美国乒乓球队应中方邀请",
                    attributes={
                        "subject": "中方",
                        "relation": "invites",
                        "object": "美国乒乓球队",
                        "entity_types": {
                            "subject_type": "ORGANIZATION",
                            "object_type": "ORGANIZATION"
                        },
                        "temporal_context": "1971年4月"
                    }
                ),
                lx.data.Extraction(
                    extraction_class="relationship_triple",
                    extraction_text="中美'乒乓外交'打开了两国人民友好往来的大门",
                    attributes={
                        "subject": "乒乓外交",
                        "relation": "opens",
                        "object": "两国人民友好往来的大门",
                        "entity_types": {
                            "subject_type": "EVENT",
                            "object_type": "CONCEPT"
                        },
                        "temporal_context": "1971年4月"
                    }
                ),
            ]
        ),
    ]

    config = factory.ModelConfig(
        model_id=api_name,
        provider="openai",
        provider_kwargs={
            "api_key": api_key,
            "base_url": base_url,
            "format_type": "json",
            "include_char_intervals": True,
        }
    )

    try:
        result = lx.extract(
            text_or_documents=input_text,
            prompt_description=prompt,
            examples=examples,
            config=config,
        )

        print(f"\n✅ Extraction successful! Found {len(result.extractions)} entities\n")

        print("\n📊 Generating visualization report...")
        lx.io.save_annotated_documents([result], output_name="extraction_results.jsonl", output_dir=".")
        
        # Generate HTML visualization
        html_content = lx.visualize("extraction_results.jsonl")
        with open("visualization.html", "w", encoding="utf-8") as f:
            if hasattr(html_content, 'data'):
                f.write(html_content.data)
            else:
                f.write(html_content)
        print("✅ Visualization saved to visualization.html")
        
    except Exception as e:
        print(f"❌ Extraction failed: {e}")

if __name__ == "__main__":
    main()
