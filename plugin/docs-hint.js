// The teaport docs hint: one static paragraph appended to the agent's system prompt telling
// it where teaport's own documentation is.
//
// install.sh puts the docs in OpenClaw's memory wiki, one page per section, titled
// "teaport docs: <page> — <section>" (packaging/deb/docs-wiki-sync). A plain memory_search
// never searches the wiki (only corpus=wiki or corpus=all do), which keeps the docs out of
// the owner's personal recall, but it also means that without a hint the agent has no
// reason to look there. The hint is sent only while the memory-wiki plugin is enabled:
// with the wiki off it would point at nothing.
//
// before_prompt_build's appendSystemContext, not prependContext: the text is static, so
// providers can cache it and no turn pays for it twice. A non-bundled plugin needs
// plugins.entries.teaport-realtime.hooks.allowConversationAccess for this hook; install.sh
// grants it (write_talk_patch). The hook reads nothing from the conversation.

export const DOCS_HINT = [
  "## teaport documentation",
  "This OpenClaw is the agent behind teaport, the voice front end it runs alongside (the room",
  "microphone and speaker, the phone line, and Talk in the OpenClaw app). teaport's user",
  "documentation is in the memory wiki, one page per section, titled",
  '"teaport docs: <page> — <section>". It covers setup, settings, phone calls, Wi-Fi setup,',
  "updates and troubleshooting. When the user asks about teaport itself (how to set something",
  "up or change it, what a setting does, or why the voice, the microphone or the phone line is",
  "not working), search it with wiki_search or memory_search with corpus=wiki, answer from",
  "what it says, and say when it does not cover the question. Do not search it for the user's",
  "own memories.",
].join("\n");

/** Whether the memory-wiki plugin is enabled in this OpenClaw config. */
export function wikiEnabled(config) {
  const entry = config?.plugins?.entries?.["memory-wiki"];
  return entry?.enabled === true;
}

/**
 * The before_prompt_build handler. `config` returns the current OpenClaw config (read per
 * turn: api.runtime is built lazily, and the owner may turn the wiki off at any time).
 */
export function docsHintHandler({ config, log } = {}) {
  return () => {
    let cfg;
    try {
      cfg = config?.();
    } catch (err) {
      log?.debug?.(`teaport docs hint: no config (${err?.message ?? err})`);
      return undefined;
    }
    return wikiEnabled(cfg) ? { appendSystemContext: DOCS_HINT } : undefined;
  };
}
