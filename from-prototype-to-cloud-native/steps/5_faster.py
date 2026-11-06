import logging

from fastapi import FastAPI
from grelmicro import Grelmicro
from grelmicro.cache import TTLCache, cached
from grelmicro.health import HealthChecks
from grelmicro.http import ErrorResponses, OpsServer
from grelmicro.log import AccessLog, Log
from grelmicro.metrics import Metrics
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience import (
    CircuitBreaker,
    CircuitBreakerError,
    Fallback,
    RateLimiter,
    Stack,
    Timeout,
)
from grelmicro.task import Tasks
from httpx import HTTPError

logger = logging.getLogger(__name__)
tasks = Tasks()
health = HealthChecks()
micro = Grelmicro(
    uses=[
        PostgresProvider(),
        Log(),  # JSON logs, text in a terminal
        AccessLog(),  # one log line per HTTP request
        # Needs the opentelemetry extra of grelmicro.
        Metrics(exporter="prometheus"),  # /metrics on :8080
        ErrorResponses(),  # problem details for errors
        health,
        OpsServer(),  # probes on :8080, kept private
        tasks,  # runs the scheduled tasks
    ]
)

app = FastAPI()
micro.install(app)

SURPLUS_KW = 2.0

breaker = CircuitBreaker("inverter")
# An open breaker shows in /healthz only.
health.add_circuitbreaker(breaker)

# Stack always runs fallback, then breaker, then the limit.
inverter_call = Stack(
    "inverter",
    patterns=[
        # Down, hung or breaker open: answer 0 kW, the safe default.
        Fallback(
            "inverter",
            when=(CircuitBreakerError, TimeoutError, HTTPError),
            default=0.0,
        ),
        breaker,
        # The inverter's API takes bursts of 5, then 1 per second.
        RateLimiter.token_bucket(
            "inverter", capacity=5, refill_rate=1
        ),
        # A device that hangs counts as a failure for the breaker.
        Timeout("inverter", seconds=2),
    ],
)


# One read per 5 s, stored in Postgres for every replica.
# It also keeps the 0 kW fallback for 5 s.
@cached(
    TTLCache(ttl=5),
    lock="cluster",  # one call serves every replica
)
@inverter_call
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
