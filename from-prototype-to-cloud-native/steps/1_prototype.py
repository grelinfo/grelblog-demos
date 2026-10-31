import logging

from fastapi import FastAPI
from grelmicro import Grelmicro
from grelmicro.log import AccessLog, Log
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.task import Tasks

logger = logging.getLogger(__name__)
tasks = Tasks()
micro = Grelmicro(
    uses=[
        SQLiteProvider("/data/app.db"),
        Log(),  # JSON logs, text in a terminal
        AccessLog(),  # one log line per HTTP request
        tasks,  # runs the scheduled tasks
    ]
)

app = FastAPI()
micro.install(app)

SURPLUS_KW = 2.0


async def read_power_kw() -> float:
    return 4.2  # GET the inverter's local API


async def switch_heater(*, on: bool) -> None:
    # POST to the heater's local API
    logger.info("water heater %s", "on" if on else "off")


@app.get("/api/solar")
async def solar() -> dict[str, float]:
    return {"power_kw": await read_power_kw()}


# Every 10 s, one replica takes a lock for the tick and runs it.
@tasks.every(interval=10, gate="claim")
async def use_solar_surplus() -> None:
    await switch_heater(on=await read_power_kw() > SURPLUS_KW)


# The SPA: API routes match first, then index.html.
app.frontend("/", directory="static", fallback="index.html")
