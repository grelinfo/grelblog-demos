import adapter from "@sveltejs/adapter-static";

// adapter-static writes build/, fallback makes it a single-page app (as the post says).
export default { kit: { adapter: adapter({ fallback: "index.html" }) } };
