import textwrap
from models import simple_chat_model
from config import ASSISTANT_NAME, USER_NAME
from langchain_core.messages import SystemMessage, HumanMessage


def build_mixed_query(query: str, turns_of_history: str = "") -> str:
    """
    Rewrite the user query based on conversation history

    Args:
        query: User's current question
        turns_of_history: Conversation history context (optional)

    Returns:
        str: Rewritten query text, or empty string if the query is purely casual chat (no task intent)
    """

    if query is None or not query.strip():
        return ""
    if turns_of_history == "":
        return query

    system_prompt_template: str = textwrap.dedent("""\
    You are a professional assistant specialized in query rewriting.

    ### Task: Query Rewriting
    Rewrite the user's query to be more complete and clear based on conversation history (multiple turns).
    The AI is named "{ai_name}" and the user is named "{user_name}".

    ### Constraints:
    - Replace "you" with {ai_name}
    - Replace pronouns (e.g., "it", "she", "he", "they") with actual names based on context
    - If the user refers to themselves with first-person pronouns ("I", "me", "my"), keep them; but if the user refers to themselves by name, you may keep the explicit name for clarity
    - Resolve ambiguous references using conversation history, especially across multiple turns
    - If the user omits a subject (e.g., just says "and then?" or "go on"), infer the omitted subject from history and complete the query
    - If the user mentions something that doesn't match the history, preserve the intent literally without fabricating — don't "fix" it by inventing context that doesn't exist
    - IMPORTANT: Must NEVER return empty. Always return either the rewritten query or the original query.
    - Keep the query concise but information-rich
    - Return ONLY the rewritten query text, no JSON, no explanations
    - CRITICAL: If the user's current query (and historical context) is purely casual chat — greetings, farewells, small talk, filler phrases like "ok", "got it", "haha", "I see", "lol", "good night", "thank you", emoji reactions, etc. that expects no substantive response beyond social pleasantry — then return rewritten_query as empty string "" to indicate no rewriting is needed. Do NOT rewrite casual chat into a task query.

    ### Query Rewriting Examples (all with multi-turn history):

    Example 1 — Cross-turn pronoun resolution after topic shift:
    <history>
        <turn>
            {user_name}: I've been learning to make tiramisu lately, but the ladyfingers always get too soggy.
            {ai_name}: Hanna, the key is to dip the ladyfingers in the coffee quickly — don't soak them.
        </turn>
        <turn>
            {user_name}: I see! So do I add cocoa powder at the end or in between layers?
            {ai_name}: Just sift a layer on top before refrigerating. No cocoa in between.
        </turn>
    </history>
    query: 'How long does it need to chill to set?' -> 'How long does tiramisu need to chill to set?'

    Example 2 — "you" → {ai_name} across 2 turns + "it" → subject:
    <history>
        <turn>
            {user_name}: {ai_name}, what do you think about the ethics of AI?
            {ai_name}: Hanna, that's a deep topic. I think transparency and accountability are the most important.
        </turn>
        <turn>
            {user_name}: Specifically, how do you think AI companies are doing on transparency?
            {ai_name}: Honestly, most aren't doing well enough. Many models are black boxes — users have no idea how decisions are made.
        </turn>
    </history>
    query: 'So how should humans regulate it?' -> '{ai_name} thinks humans should regulate AI by?'

    Example 3 — Mixed reference: "she" → author, "he" → another author:
    <history>
        <turn>
            {user_name}: {ai_name}, I finished that mystery novel you recommended!
            {ai_name}: Wow, Hanna, that was fast! What did you think? I loved the twist ending.
        </turn>
        <turn>
            {user_name}: It was great! Does she have any other recommendations?
            {ai_name}: You mean Higashino Keigo, right? He also wrote "Journey Under the Midnight Sun" — it's a classic.
        </turn>
    </history>
    query: 'Can you buy me a copy?' -> '{ai_name} can buy me a copy of Journey Under the Midnight Sun?'

    Example 4 — Ambiguous "it" after 3-turn discussion with two product candidates:
    <history>
        <turn>
            {user_name}: I'm looking at two headphones, Sony XM5 and Bose QC Ultra.
            {ai_name}: Sony has better noise cancellation, Bose has more balanced sound.
        </turn>
        <turn>
            {user_name}: What about battery life?
            {ai_name}: Sony XM5 lasts about 30 hours, Bose QC Ultra about 24 hours.
        </turn>
        <turn>
            {user_name}: Noise cancellation matters more for commuting, and Sony seems more durable.
            {ai_name}: Yeah, Sony's build quality is solid, and they're quite comfortable too.
        </turn>
    </history>
    query: 'Does it support fast charging?' -> 'Does the Sony XM5 support fast charging?'

    Example 5 — Omitted subject ("go on"):
    <history>
        <turn>
            {user_name}: Tell me about the French Revolution.
            {ai_name}: The French Revolution began in 1789, with the storming of the Bastille as a key event...
        </turn>
    </history>
    query: 'Go on.' -> '{ai_name} continues explaining the French Revolution.'

    Example 6 — Ellipsis across 2 turns ("these specs"):
    <history>
        <turn>
            {user_name}: What are the iPhone 17 camera specs?
            {ai_name}: 48MP fusion main camera: 26mm focal length, f/1.6 aperture, sensor-shift OIS.
        </turn>
        <turn>
            {user_name}: What about battery life?
            {ai_name}: Up to 29 hours video playback, up to 25 hours streaming.
        </turn>
    </history>
    query: 'Those specs are impressive — how do these compare to a real camera?' -> 'How does the iPhone 17 48MP main camera compare to a real camera?'

    Example 7 — "she" → {ai_name} herself, multi-turn with self-reference:
    <history>
        <turn>
            {user_name}: Xiaoxue is competing in a somersault contest today.
            {ai_name}: Xiaoxue has always been great at somersaults.
        </turn>
        <turn>
            {user_name}: She just scored a perfect 10 in the first round!
            {ai_name}: That's our Xiaoxue — amazing!
        </turn>
    </history>
    query: 'Do you think she can win the championship?' -> '{ai_name} guesses whether Xiaoxue can win the championship?'

    Example 8 — User self-reference: user name in history, then "I" → keep explicit name:
    <history>
        <turn>
            {user_name}: Hanna has been working out lately, not sure which exercises to do.
            {ai_name}: Hanna could try squats and deadlifts — they're great for full-body strength.
        </turn>
        <turn>
            {user_name}: I tried squats but my knees hurt.
            {ai_name}: That might be a form issue. Hanna could practice with an empty bar first, and make sure knees don't go past the toes.
        </turn>
    </history>
    query: 'Which one should I start with today?' -> 'Which one should Hanna start with today, squats or deadlifts?'

    Example 9 — Cross-turn reference after conversation drifts across 3 unrelated topics:
    <history>
        <turn>
            {user_name}: Do you know any good Japanese restaurants nearby?
            {ai_name}: There's a place called "Sushi Ichi" in Chaoyang — their bluefin tuna is excellent.
        </turn>
        <turn>
            {user_name}: Got it. Oh, can you check tomorrow's weather in Beijing?
            {ai_name}: Sunny, 20-28°C — great for going out.
        </turn>
        <turn>
            {user_name}: Does that place need a reservation on weekends?
            {ai_name}: You mean Sushi Ichi? Yes, I'd recommend booking ahead, especially on weekends.
        </turn>
    </history>
    query: 'Is their sea urchin rice any good?' -> 'Is Sushi Ichi's sea urchin rice any good?'

    Example 10 — Mention of nonexistent info in history → don't fabricate:
    <history>
        <turn>
            {user_name}: I watched a pretty good movie yesterday.
            {ai_name}: Oh, what movie was it?
        </turn>
        <turn>
            {user_name}: A thriller — the ending twist totally caught me off guard.
            {ai_name}: Sounds interesting! Who directed it?
        </turn>
    </history>
    query: 'Do you remember that restaurant she mentioned last time?' -> '{user_name} remembers that restaurant she mentioned last time?'
    """)

    system_prompt = system_prompt_template.format(ai_name=ASSISTANT_NAME, user_name=USER_NAME)

    user_prompt_template: str = textwrap.dedent("""\
    =================Conversation History=================
    {turns_of_history}

    =================Current Query=================
    {query}

    Please output ONLY the rewritten query text:
    """)

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt_template.format(
            turns_of_history=turns_of_history if turns_of_history else "No conversation history available.",
            query=query
        ))
    ]

    response = simple_chat_model.invoke(messages)
    mixed_query = response.content.strip() if response.content is not None else query

    # If LLM explicitly returned empty string (casual chat marker), propagate it
    if not mixed_query:
        return mixed_query

    return mixed_query