from db import family_emails, mark_finished
from fastapi import FastAPI
from grelmicro import Grelmicro
from grelmicro.outbox import Message, Outbox
from grelmicro.providers.postgres import PostgresProvider
from mail import render, send_email
from pydantic import BaseModel

postgres = PostgresProvider()  # reads POSTGRES_URL
outbox = Outbox(postgres)  # a table in the same Postgres
micro = Grelmicro(uses=[postgres, outbox])
app = FastAPI()
micro.install(app)


# The intent, not the rendered email.
class PlanReady(BaseModel):
    to: str
    week: str


# Runs in the relay, retried with backoff on failure.
@outbox.handler(PlanReady)
async def send_plan_ready(message: Message[PlanReady]) -> None:
    assert message.data is not None
    mail = render("plan_ready", week=message.data.week)
    await send_email(message.data.to, mail)


class Plan(BaseModel):
    week: str


@app.post("/plans/{week}/finish")
async def finish_plan(week: str) -> Plan:
    async with (
        postgres.client.acquire() as conn,
        conn.transaction(),
    ):
        # A retry finds the plan finished: no new emails.
        if not await mark_finished(conn, week):
            return Plan(week=week)
        # One message per person, saved with the plan.
        for email in await family_emails(conn):
            await outbox.publish(
                conn, PlanReady(to=email, week=week)
            )
    return Plan(week=week)
