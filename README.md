# grelblog demos

Runnable code behind the posts on [blog.grel.info](https://blog.grel.info). Every code block in a post comes from here, and the tests prove it runs.

## From prototype to cloud-native app with grelmicro

[`from-prototype-to-cloud-native/`](from-prototype-to-cloud-native/) is a small solar home app: a FastAPI backend and a Svelte single-page app that read an inverter and switch a water heater. It grows one day at a time, from one container to replicas on a shared Postgres, then guards, metrics and a cache. Its [README](from-prototype-to-cloud-native/README.md) shows how to run it.

## License

MIT
