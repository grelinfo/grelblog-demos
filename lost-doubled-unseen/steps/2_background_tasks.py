from db import family_emails, mark_finished
from fastapi import BackgroundTasks, FastAPI
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
async def finish_plan(week: str, tasks: BackgroundTasks) -> Plan:
    async with postgres.client.acquire() as conn:
        await mark_finished(conn, week)
        family = await family_emails(conn)
    mail = render("plan_ready", week=week)
    # Runs after the answer, in this process only.
    for address in family:
        tasks.add_task(send_email, address, mail)
    return Plan(week=week)
