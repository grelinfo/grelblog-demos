<script>
  let power = $state();
  let failed = $state(false);

  async function refresh() {
    try {
      const response = await fetch("/api/solar");
      if (!response.ok) throw new Error(response.statusText);
      power = (await response.json()).power_kw;
      failed = false;
    } catch {
      failed = true;
    }
  }

  $effect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  });
</script>

<p>Solar power</p>
<p class="value">{power ?? "…"} <small>kW</small></p>
<p class="note">{failed ? "No answer, retrying." : "Updated every 5 seconds."}</p>

<style>
  p { margin: 0; }
  .value { font-size: 4rem; font-weight: 700; line-height: 1.2; }
  small { font-size: 1.5rem; font-weight: 400; }
  .note { opacity: 0.6; font-size: 0.9rem; }
</style>
