// The teaport docs hint: one static paragraph appended to the agent's system prompt telling
// it where teaport's own documentation is.
//
// install.sh puts the docs in OpenClaw's memory wiki, one page per section, titled
// "teaport docs: <page> — <section>" (packaging/deb/docs-wiki-sync). A plain memory_search
// never searches the wiki (only corpus=wiki or corpus=all do), which keeps the docs out of
// the owner's personal recall, but it also means that without a hint the agent has no
// reason to look there. The hint is sent only where the docs are: install.sh sets this
// plugin's config.docsInWiki after it wrote them (and clears it when it could not), and the
// wiki must still be there for the agent: plugins on, memory-wiki enabled and not denied (or
// left out of an allow list), and one shared vault (with one vault per agent the installer
// adds nothing). Anywhere else the hint would point at nothing, and the agent would say the
// docs do not cover the question.
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

/** Whether the memory-wiki plugin is enabled in this OpenClaw config, and allowed to load. */
export function wikiEnabled(config) {
  const plugins = config?.plugins;
  if (plugins?.enabled === false) return false;
  if (Array.isArray(plugins?.deny) && plugins.deny.includes("memory-wiki")) return false;
  if (Array.isArray(plugins?.allow) && plugins.allow.length > 0 && !plugins.allow.includes("memory-wiki")) {
    return false;
  }
  return plugins?.entries?.["memory-wiki"]?.enabled === true;
}

/** Whether the teaport docs are in this OpenClaw's memory wiki, for the agent to search. */
export function docsInWiki(config) {
  const entries = config?.plugins?.entries;
  return (
    wikiEnabled(config) &&
    entries?.["teaport-realtime"]?.config?.docsInWiki === true &&
    entries?.["memory-wiki"]?.config?.vault?.scope !== "agent"
  );
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
    return docsInWiki(cfg) ? { appendSystemContext: DOCS_HINT } : undefined;
  };
}
