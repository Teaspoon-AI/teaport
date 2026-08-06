## Source of truth

This site is canonical for teaport's user-facing documentation. The product repo
(`github.com/Teaspoon-AI/teaport`, checked out locally at `~/Desktop/teagram-mini-pub`)
keeps only its README and license files; its `docs/` folder points here.

Facts about ports, paths, defaults, and environment variables are defined in that repo's
`install.sh`, `cli/teaport`, `systemd/*.service.in`, and `manifest/stable.json`. When a
page states one, cite where it came from so the next drift is detectable:

```markdown
<!-- source: install.sh:375-386 -->
```

## Writing style

Procedural pages — Installation, Troubleshooting, Updating, CLI reference — are written in
[ASD-STE100 Simplified Technical English](https://www.asd-ste100.org/). One idea per
sentence, approved vocabulary, active voice, no participial phrases, and every acronym
expanded on first use: "large language model (LLM)". Once a term is chosen, reuse it
exactly; do not reach for a synonym.

Conceptual pages — Architecture, FAQ, the index splash — use ordinary clear technical
English. STE buys unambiguous instructions for a reader under stress; it does not buy
clarity when explaining how something works, and it reads stilted there.

## Development

When starting the dev server, use background mode:

```
astro dev --background
```

Manage the background server with `astro dev stop`, `astro dev status`, and `astro dev logs`.

## Documentation

Full documentation: https://docs.astro.build

Consult these guides before working on related tasks:

- [Adding pages, dynamic routes, or middleware](https://docs.astro.build/en/guides/routing/)
- [Working with Astro components](https://docs.astro.build/en/basics/astro-components/)
- [Using React, Vue, Svelte, or other framework components](https://docs.astro.build/en/guides/framework-components/)
- [Adding or managing content](https://docs.astro.build/en/guides/content-collections/)
- [Adding styles or using Tailwind](https://docs.astro.build/en/guides/styling/)
- [Supporting multiple languages](https://docs.astro.build/en/guides/internationalization/)
