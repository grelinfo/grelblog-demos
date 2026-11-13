# Lost, doubled, unseen

Runnable code for the blog series of the same name on [blog.grel.info](https://blog.grel.info). Every code block in the posts comes from here.

The app is a small family meal planner: a weekly plan with lunch and dinner, and an email to the family when someone finishes the plan. It is written from scratch for the series.

## Steps

| Part | File | Shows |
|---|---|---|
| 1 | `steps/1_inline.py` | Sending the emails inside the request |
| 1 | `steps/2_background_tasks.py` | Sending them with FastAPI's `BackgroundTasks` |
| 1 | `steps/3_outbox.py` | Saving them in a Postgres outbox, sent by a relay |
| 2 | `steps/4_add_meal.py` | Adding a meal once, even when the request is retried |
| 3 | `steps/5_delivery.py` | Reading bounces and counting failed emails |

`steps/db.py` and `steps/mail.py` hold the helpers the posts do not show.

## Run the tests

The tests need [uv](https://docs.astral.sh/uv/) and Podman. They start Postgres and a test mailbox in containers, and a fake email server in Python.

```bash
uv run pytest
```

`SPEC.md` lists every claim the posts make. Each test name ends with the id of the claim it proves.
