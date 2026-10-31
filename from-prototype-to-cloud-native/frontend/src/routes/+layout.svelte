<script>
  let { children } = $props();
</script>

<header>
  <strong>Solar home</strong>
  <nav><a href="/">Home</a> <a href="/about">About</a></nav>
</header>
<main>{@render children()}</main>

<style>
  :global(:root) {
    color-scheme: light dark;
    font: 17px/1.6 system-ui, sans-serif;
    color: light-dark(#1c1c1e, #f1f1ef);
    background: light-dark(#fafaf8, #141416);
  }
  :global(body) { margin: 0; }
  :global(a) { color: light-dark(#b45309, #fbbf24); }
  header, main { max-width: 32rem; margin: 0 auto; padding: 1.5rem 1rem; }
  header { display: flex; justify-content: space-between; }
  nav a { margin-left: 1rem; }
</style>
