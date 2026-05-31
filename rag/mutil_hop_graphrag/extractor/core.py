import io
import os
import sys
import textwrap
from pathlib import Path
import langextract as lx
from config import ENV_PATH
from dotenv import load_dotenv
from langextract import factory
from ..type import ExtractRelationResult

# 添加项目根目录到 Python 搜索路径
project_root = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(project_root))


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 加载环境变量
load_dotenv(ENV_PATH, override = True)
api_key = os.getenv("CHAT_API_KEY")
api_name = os.getenv("CHAT_API_NAME")
base_url = os.getenv("CHAT_API_BASE")

prompt = textwrap.dedent("""\
    Extract ALL relationship triples from the input text to build a comprehensive knowledge graph.
    This includes factual/business relations, social/personal relations, and emotional/affective relations.

    === OUTPUT FORMAT ===
    For each relationship, extract the **subject entity** as the `extraction_text`.
    Store the full triple details in `attributes`.

    === ENTITY TYPES ===
    - PERSON: Individual people (e.g., 张三, 李四, Elon Musk)
    - ORGANIZATION: Groups, institutions, teams, companies (e.g., 某公司, 研发团队, NASA)
    - LOCATION: Places, countries, regions (e.g., 北京, 纽约, 硅谷)
    - EVENT: Specific events or activities (e.g., 产品发布会, 签约仪式, conference)
    - CONCEPT: Abstract ideas, policies, initiatives, technologies (e.g., 人工智能, 碳中和, 战略合作)
    - PRODUCT: Tangible or digital products, devices, software (e.g., 微信, GPT-4, Model 3)
    - DATE: Temporal references, absolute dates or relative time (e.g., 2024年3月, 去年, Q3)

    === RELATION TYPES — CATEGORY: BUSINESS & FACTUAL ===
    - participates_in / hosts / attends (参与/举办/出席)
    - visits / invites (访问/邀请)
    - states / emphasizes / points_out (指出/强调/表示)
    - promotes / supports / advocates (推动/支持/倡导)
    - opens / enables / facilitates (开启/促进/便利)
    - acquires / merges / partners_with (收购/合并/合作)
    - develops / launches / releases (开发/发布/推出)
    - invests_in / funds (投资/资助)
    - competes_with (竞争)
    - succeeds / precedes (继任/前任)
    - impacts / influences / affects (影响)
    - belongs_to / is_part_of / subsidiary_of (属于/组成部分/子公司)
    - leads / manages / founded (领导/管理/创立)

    === RELATION TYPES — CATEGORY: SOCIAL & FAMILY ===
    - spouse_of / married_to (配偶/结婚)
    - parent_of / child_of (父母/子女)
    - sibling_of (兄弟姐妹)
    - friend_of / befriends (朋友)
    - colleague_of / works_with (同事/共事)
    - mentor_of / mentee_of (师徒)
    - lover_of / in_love_with (恋人/相爱)
    - rival_of / enemy_of (对手/敌人)
    - admirer_of / fan_of (崇拜者/粉丝)
    - neighbor_of (邻居)
    - classmate_of (同学)
    - partner_of (伴侣/搭档)

    === RELATION TYPES — CATEGORY: EMOTIONAL & AFFECTIVE ===
    - loves / adores (爱/深爱)
    - hates / despises (恨/厌恶)
    - admires / respects (钦佩/尊敬)
    - trusts / relies_on (信任/依赖)
    - fears / dreads (恐惧/害怕)
    - appreciates / grateful_to (感激/感恩)
    - misses / longs_for (思念/渴望)
    - envies / jealous_of (嫉妒)
    - pities / sympathizes_with (同情)
    - resents / bitter_toward (怨恨)
    - cares_for / worries_about (关心/担忧)
    - proud_of (自豪)
    - disappointed_in / dissatisfied_with (失望/不满)

    === EXTRACTION RULES ===
    1. COMPLETENESS: Extract EVERY relationship explicitly or implicitly mentioned in the text — factual, social, and emotional alike.
    2. EXACT TEXT: The `extraction_text` MUST be an exact substring from the source text (e.g., the subject entity verbatim).
    3. NO DUPLICATES: Avoid extracting the same relationship twice.
    4. SPECIFIC RELATIONS: Use the most specific predicate possible. Prefer "loves" over "likes", "spouse_of" over "related_to".
    5. CONCRETE ENTITIES: Both subject and object must be identifiable, non-pronominal entities.
    6. CONTEXT PRESERVATION: Include temporal/spatial/conditional/emotional context in attributes wherever possible.
    7. EMOTIONAL INFERENCE: Emotional relations can be stated directly (e.g., "张三很感激李四") or inferred from action/context (e.g., "张三紧紧握住李四的手，眼眶泛红" → gratitude or deep affection). Use your best judgment.

    === ATTRIBUTES TO INCLUDE ===
    - entity_types: dict {"subject_type": "...", "object_type": "..."}
    - temporal_context: string (time reference if mentioned, e.g., "2024年3月", "last quarter", "during the meeting")
    - spatial_context: string (location reference if mentioned, e.g., "北京", "at the headquarters")
    - sentiment: string (optional, one of "positive", "negative", "neutral" — indicate the emotional valence of the relation)

    === STEP-BY-STEP PROCESS ===
    1. Scan the entire text, identify all entities (PERSON, ORG, LOC, EVENT, CONCEPT, PRODUCT, DATE).
    2. For each sentence or clause, find directional relationships between entities — consider all three categories (business/factual, social/family, emotional/affective).
    3. Assemble each triple: subject, relation, object — stored in `attributes`.
    4. Trim the `extraction_text` to the minimal exact substring covering the subject entity.
    5. Verify both subject and object are concrete, non-ambiguous entities.
    6. Check for duplicates before finalizing.
    7. If a sentence contains multiple relations (of any category), extract them all.

    IMPORTANT: Be thorough! Implicit or cross-sentence relationships should also be captured.
    A single text can contain business relations, family ties, and emotional bonds simultaneously — extract ALL of them.""")

# 2. Provide high-quality examples covering business, social, and emotional relationships
examples = [
    # Example 1: Business + social relationships (tech company launch)
    lx.data.ExampleData(
        text="2024年3月，星云科技CEO张明在年度技术峰会上正式发布了新一代AI芯片\"天枢\"，该芯片由公司首席架构师陈雪带领团队历时两年研发完成。张明和陈雪是大学同学，两人一起创业多年。",
        extractions=[
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="星云科技CEO张明在年度技术峰会上正式发布了新一代AI芯片",
                attributes={
                    "subject": "张明",
                    "relation": "releases",
                    "object": "天枢",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PRODUCT"},
                    "temporal_context": "2024年3月",
                    "spatial_context": "年度技术峰会",
                    "sentiment": "neutral"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="首席架构师陈雪带领团队历时两年研发",
                attributes={
                    "subject": "陈雪",
                    "relation": "leads",
                    "object": "团队",
                    "entity_types": {"subject_type": "PERSON", "object_type": "ORGANIZATION"},
                    "temporal_context": "两年",
                    "sentiment": "neutral"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="张明和陈雪是大学同学",
                attributes={
                    "subject": "张明",
                    "relation": "classmate_of",
                    "object": "陈雪",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "",
                    "sentiment": "positive"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="两人一起创业多年",
                attributes={
                    "subject": "张明",
                    "relation": "colleague_of",
                    "object": "陈雪",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "多年",
                    "sentiment": "positive"
                }
            ),
        ]
    ),
    # Example 2: Family + emotional relationships
    lx.data.ExampleData(
        text="王建国老人今年八十了，儿子王磊在外地工作很少回家。老王嘴上从不说什么，但逢年过节总会站在巷口张望。邻居李婶看在眼里，常说'老王想儿子了'。",
        extractions=[
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="儿子王磊",
                attributes={
                    "subject": "王磊",
                    "relation": "child_of",
                    "object": "王建国",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "",
                    "sentiment": "positive"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="老王想儿子了",
                attributes={
                    "subject": "王建国",
                    "relation": "misses",
                    "object": "王磊",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "逢年过节",
                    "sentiment": "positive"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="邻居李婶",
                attributes={
                    "subject": "李婶",
                    "relation": "neighbor_of",
                    "object": "王建国",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "",
                    "sentiment": "neutral"
                }
            ),
        ]
    ),
    # Example 3: Romantic + emotional + business relationships
    lx.data.ExampleData(
        text="林小雨一直暗恋她的上司赵恒，每次开会都会偷偷看他。但赵恒只把她当普通下属，反而对竞争对手公司的总监苏婉青格外欣赏。林小雨心里既失落又有点嫉妒。",
        extractions=[
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="林小雨一直暗恋她的上司赵恒",
                attributes={
                    "subject": "林小雨",
                    "relation": "lover_of",
                    "object": "赵恒",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "一直",
                    "sentiment": "positive"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="她的上司赵恒",
                attributes={
                    "subject": "赵恒",
                    "relation": "leads",
                    "object": "林小雨",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "",
                    "sentiment": "neutral"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="对竞争对手公司的总监苏婉青格外欣赏",
                attributes={
                    "subject": "赵恒",
                    "relation": "admires",
                    "object": "苏婉青",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "",
                    "sentiment": "positive"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="竞争对手公司",
                attributes={
                    "subject": "公司",
                    "relation": "competes_with",
                    "object": "公司",
                    "entity_types": {"subject_type": "ORGANIZATION", "object_type": "ORGANIZATION"},
                    "temporal_context": "",
                    "sentiment": "negative"
                }
            ),
            lx.data.Extraction(
                extraction_class="relationship_triple",
                extraction_text="林小雨心里既失落又有点嫉妒",
                attributes={
                    "subject": "林小雨",
                    "relation": "envies",
                    "object": "苏婉青",
                    "entity_types": {"subject_type": "PERSON", "object_type": "PERSON"},
                    "temporal_context": "",
                    "sentiment": "negative"
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

def text_extract(input_text: str)-> list[ExtractRelationResult]:
    try:
        result = lx.extract(
            text_or_documents=input_text,
            prompt_description=prompt,
            examples=examples,
            config=config,
        )

        if result is None:
            return []

        extractions = result.extractions

        if extractions is None or len(extractions) == 0:
            return []

        extract_relation_results: list[ExtractRelationResult] = []
        for extraction in extractions:
            if getattr(extraction, "attributes", None) is None or not isinstance(extraction.attributes, dict):
                continue
            attributes= extraction.attributes
            subject = attributes.get("subject")
            object = attributes.get("object")
            relation = attributes.get("relation")
            temporal_context = attributes.get("temporal_context")
            spatial_context = attributes.get("spatial_context")
            sentiment = attributes.get("sentiment")

            entity_types = attributes.get("entity_types")
            if entity_types is None:
                continue
            subject_type = entity_types.get("subject_type", None)
            object_type = entity_types.get("object_type", None)


            if subject is None or object is None or relation is None:
                continue

            extract_relation_results.append(ExtractRelationResult(
                subject=subject,
                subject_type=subject_type,
                object=object,
                object_type=object_type,
                relation=relation,
                temporal_context=temporal_context,
                spatial_context=spatial_context,
                sentiment=sentiment
            ))

        return extract_relation_results
    except Exception as e:
        print(f"❌ Extraction failed: {e}")
        return []