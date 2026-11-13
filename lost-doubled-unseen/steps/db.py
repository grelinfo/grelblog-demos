"""The app's plain SQL, shared by every step. Not shown in the posts."""

from datetime import date
from uuid import UUID

from asyncpg import Connection
from pydantic import BaseModel

FINISH = """
UPDATE plans SET finished_at = now()
WHERE week = $1 AND finished_at IS NULL RETURNING week
"""


async def mark_finished(conn: Connection, week: str) -> bool:
    """Finish the plan. False if it was already finished."""
    return await conn.fetchval(FINISH, week) is not None


async def family_emails(conn: Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT email FROM family ORDER BY email"
    )
    return [row["email"] for row in rows]


async def set_slot(
    conn: Connection, day: date, meal_type: str, recipe: str
) -> None:
    await conn.execute(
        "INSERT INTO slots (day, meal_type, recipe) VALUES ($1, $2, $3)"
        " ON CONFLICT (day, meal_type) DO UPDATE SET recipe = $3",
        day,
        meal_type,
        recipe,
    )


async def insert_meal(
    conn: Connection, meal_id: UUID, week: str, recipe: str
) -> None:
    await conn.execute(
        "INSERT INTO meals (id, week, recipe) VALUES ($1, $2, $3)",
        meal_id,
        week,
        recipe,
    )


class StoredMeal(BaseModel):
    week: str
    recipe: str


async def find_meal(conn: Connection, meal_id: UUID) -> StoredMeal:
    row = await conn.fetchrow(
        "SELECT week, recipe FROM meals WHERE id = $1", meal_id
    )
    return StoredMeal(week=row["week"], recipe=row["recipe"])


async def save_sending(
    conn: Connection, message_id: UUID, meal_id: UUID
) -> None:
    """Record the email before the send, so a bounce always finds it."""
    await conn.execute(
        "INSERT INTO emails (message_id, meal_id, status) VALUES ($1, $2, 'sending')"
        " ON CONFLICT (message_id) DO NOTHING",
        message_id,
        meal_id,
    )


async def mark_accepted(conn: Connection, message_id: UUID) -> None:
    """The SMTP server took it. A bounce that came first wins."""
    await conn.execute(
        "UPDATE emails SET status = 'accepted' WHERE message_id = $1 AND status = 'sending'",
        message_id,
    )


async def mark_bounced(conn: Connection, message_id: UUID) -> bool:
    """A state, not an event: marking it twice changes nothing. True if it changed."""
    changed = await conn.fetchval(
        "UPDATE emails SET status = 'bounced' WHERE message_id = $1 AND status <> 'bounced'"
        " RETURNING message_id",
        message_id,
    )
    return changed is not None
