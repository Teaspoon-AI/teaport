# teaport-docs

Documentation site for [teaport](https://github.com/Teaspoon-AI/teaport), a
local voice assistant for the NVIDIA Jetson Orin. Built with
[Starlight](https://starlight.astro.build), deployed to Cloudflare Pages at
[docs.teaspoon.tech](https://docs.teaspoon.tech).

Content in `src/content/docs/` is ported from the `docs/` scaffold in the
[teaport repo](https://github.com/Teaspoon-AI/teaport/tree/main/docs) — keep
the two in sync until there's a single source of truth.

## Commands

| Command           | Action                                       |
| :----------------- | :-------------------------------------------- |
| `npm install`      | Install dependencies                          |
| `npm run dev`       | Start local dev server at `localhost:4321`    |
| `npm run build`     | Build production site to `./dist/`            |
| `npm run preview`   | Preview the build locally, before deploying   |
