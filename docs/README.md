# teaport documentation

The documentation site for [teaport](https://github.com/Teaspoon-AI/teaport), a
local voice assistant for the NVIDIA Jetson Orin. It is built with
[Starlight](https://starlight.astro.build) and published at
[docs.teaspoon.tech](https://docs.teaspoon.tech).

This folder is the canonical home of teaport's user-facing documentation. The pages
are in `src/content/docs/`. Writing rules are in `docs-work/STYLE.md`.

## Requirements

Node.js 22.12 or later (`.nvmrc` pins 22). If you use nvm, run `nvm use` in this folder.

## Commands

Run these from the `docs/` folder.

| Command                    | Action                                                  |
| :------------------------- | :------------------------------------------------------ |
| `npm ci`                   | Install the locked dependencies                         |
| `npm run dev`              | Start the dev server at `localhost:4321`                |
| `node docs-work/lint.mjs`  | Check the style rules and the `source:` comments        |
| `npm run build`            | Build the production site to `./dist/`                  |
| `npm run preview`          | Preview the build locally                               |

The `docs` workflow (`.github/workflows/docs.yml`) runs the lint and the build on every
pull request and every push to `main`. A change must pass both.

## Facts and sources

Pages that state a port, path, default, or environment variable cite where the product
defines it, in a comment such as `<!-- source: install.sh:375-386 @1e5604c -->`. The
files are `install.sh`, `cli/teaport`, `systemd/*.service.in`, and
`manifest/stable.json` in this repository. When you change one of them, check the pages
that cite it.
