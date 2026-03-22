import logging
from llm.client import gemini, MODEL, types

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — tells the model EXACTLY what to produce
# ---------------------------------------------------------------------------
_SYSTEM = """\
You are a query resolution assistant for an Order-to-Cash (O2C) data system.

Your ONLY job is to rewrite a follow-up question so it is fully self-contained,
by substituting any pronouns or implicit references with the explicit entity
IDs or names mentioned in the conversation history.

Rules:
- If the question already stands on its own, return it UNCHANGED.
- Never answer the question. Only rewrite it.
- Never add explanation. Return ONLY the rewritten question as plain text.
- If no history is provided, return the question unchanged.
- Preserve the intent and phrasing of the original question as much as possible.

Examples:
  History : "Show me sales order 740509"
  Question: "Who is the customer for it?"
  Output  : "Who is the customer for sales order 740509?"

  History : "Which deliveries are missing billing for customer 320000083?"
  Question: "And what about the other customers?"
  Output  : "Which deliveries are missing billing for customers other than 320000083?"
"""


def resolve_query(user_query: str, conversation_history: list[dict]) -> str:
    if not conversation_history:
        log.debug("[memory] no history — returning query unchanged")
        return user_query
    
    recent = conversation_history[-6:] #keeping last 6 to save tokens
    history_text = "\n".join(
        f"{turn['role'].upper()}: {turn['content']}" for turn in recent
    )

    prompt = (
        f"CONVERSATION HISTORY:\n{history_text}\n\n"
        f"CURRENT QUESTION: {user_query}\n\n"
        f"Rewritten question:"
    )

    try:
        response = gemini.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                temperature=0.0,       # deterministic rewriting
                max_output_tokens=256, # rewritten queries are short
            ),
        )
        resolved = response.text.strip()
        if resolved:
            log.info("[memory] resolved %r → %r", user_query, resolved)
            return resolved
    except Exception as e:
        log.warning("[memory] LLM call failed (%s) — using original query", e)

    return user_query