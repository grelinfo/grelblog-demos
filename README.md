# grelblog demos

Runnable code behind the posts on [blog.grel.info](https://blog.grel.info). Every code block in a post comes from here, and the tests prove it runs.

## From prototype to cloud-native app with grelmicro

[`from-prototype-to-cloud-native/`](from-prototype-to-cloud-native/) is a small solar home app: a FastAPI backend and a Svelte single-page app that read an inverter and switch a water heater. It grows one day at a time, from one container to replicas on a shared Postgres, then guards, metrics and a cache. Its [README](from-prototype-to-cloud-native/README.md) shows how to run it.

## Lost, doubled, unseen

[`lost-doubled-unseen/`](lost-doubled-unseen/) is a small family meal planner that emails the family when the week's plan is done. It shows how FastAPI background tasks can lose the email, how a Postgres outbox keeps it, and what comes next: retries that add twice, and emails that bounce. Its [README](lost-doubled-unseen/README.md) shows how to run it.

## License

MIT
