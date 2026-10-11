// How the bridge reads its settings (bridge.env, through the unit's EnvironmentFile):
// by the rule the brain reads every teaport setting with (brain/teaport_brain/settings.py,
// normalize + parse), so the config page, which shows a value as that rule reads it,
// shows what the bridge runs on.
//
// Whitespace around a value is not part of it: the characters Python's str.strip()
// removes, which are not quite JS's trim() (U+001C-001F and U+0085 are whitespace there,
// U+FEFF is not). A blank value is not set. A value that is not of its schema row's type
// is not used either: the bridge says so and runs on the default, as the brain does.
// brain/tests/test_bridge_env.py holds this to the brain's reading, row by row.

const SPACE = "\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const EDGES = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");

// The schema types the bridge's rows check (settings.PARSERS); any other type is text.
const TYPES = {
  int: /^[+-]?[0-9]+$/,
  snowflake: /^[0-9]+$/,
  ws_url: new RegExp(`^wss?://[^${SPACE}]+$`),
};

export const strip = (text) => text.replace(EDGES, "");

// The text of setting `name`, or undefined: unset, blank, or (said once) not a `type`.
export function setting(name, type, env = process.env) {
  const text = strip(env[name] ?? "");
  if (!text) return undefined;
  if (type && !TYPES[type].test(text)) {
    console.warn(`${name}=${JSON.stringify(text)} is not a ${type}; using the default`);
    return undefined;
  }
  return text;
}
