from db import family_emails, mark_finished
from fastapi import FastAPI
from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from mail import render, send_email
from pydantic import BaseModel

postgres = PostgresProvider()  # reads POSTGRES_URL
micro = Grelmicro(uses=[postgres])
app = FastAPI()
micro.install(app)


class Plan(BaseModel):
    week: str


@app.post("/plans/{week}/finish")
async def finish_plan(week: str) -> Plan:
    async with postgres.client.acquire() as conn:
        await mark_finished(conn, week)
        family = await family_emails(conn)
    mail = render("plan_ready", week=week)
    # The user waits for the SMTP server, once per person.
    for address in family:
        await send_email(address, mail)
    return Plan(week=week)
