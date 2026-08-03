"""Prior turns of a conversation, shaped for a model prompt.

Every model call used to receive only the current question, so a follow-up
like "now only Ceramic-C" had nothing to resolve against.
"""

from typing import Any

from .database import connection, rows_as_dicts

# Bounded so a long conversation cannot grow the prompt without limit. Costs
# roughly a few thousand tokens at the ceiling.
MAX_TURNS = 6
MAX_CHARS_PER_MESSAGE = 800


def recent_messages(conversation_id: str, before_message_id: str) -> list[dict[str, str]]:
    """Completed turns that came before the message being answered.

    The current turn's rows already exist when this runs, so they are excluded
    by timestamp rather than assumed absent.
    """
    with connection() as conn:
        rows = rows_as_dicts(
            conn.execute(
                """
                SELECT role, content FROM messages
                WHERE conversation_id = ?
                  AND status = 'COMPLETED'
                  AND content <> ''
                  AND created_at < (
                      SELECT created_at FROM messages WHERE message_id = ?
                  )
                ORDER BY created_at DESC, CASE role WHEN 'ASSISTANT' THEN 0 ELSE 1 END
                LIMIT ?
                """,
                [conversation_id, before_message_id, MAX_TURNS * 2],
            )
        )
    ordered = list(reversed(rows))
    return [
        {
            "role": "user" if row["role"] == "USER" else "assistant",
            "content": _clip(str(row["content"])),
        }
        for row in ordered
    ]


def _clip(content: str) -> str:
    if len(content) <= MAX_CHARS_PER_MESSAGE:
        return content
    return f"{content[:MAX_CHARS_PER_MESSAGE]}…"


def with_history(
    system_prompt: str, history: list[dict[str, str]] | None, user_prompt: str
) -> list[dict[str, Any]]:
    """Build a messages array with earlier turns between system and question."""
    return [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": user_prompt},
    ]
