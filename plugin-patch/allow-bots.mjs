// Runtime patch for the official Discord channel plugin (discord@claude-plugins-official,
// tested with 0.0.4).
//
// Why: the plugin drops every message written by a bot
//   server.ts, messageCreate handler:  if (msg.author.bot) return
// so another bot's posts (here: the GPT bridge) would never reach Claude.
//
// How: this file is loaded with Bun's preload, through the environment of the
// group-chat Claude session only (scripts/launch-claude.* set it):
//   BUN_OPTIONS=--preload=/absolute/path/to/plugin-patch/allow-bots.mjs
// The plugin cache is not edited, so the patch survives plugin updates as long as
// the plugin keeps using discord.js's 'messageCreate' event. (A copy of the plugin
// loaded with --plugin-dir would not work: --channels only takes installed,
// allowlisted plugins unless you accept a dev-mode prompt at every start.)
//
// Every bun process started from that session loads this file; it does nothing
// unless the process is the Discord plugin's server.ts.
//
// Environment:
//   DISCORD_ALLOW_BOT_IDS              comma-separated bot user IDs whose messages are let through
//   DISCORD_ALLOW_BOT_REQUIRE_MENTION  "1" (default): a listed bot's message passes only if it
//                                      @mentions our bot; "0": every message passes
//   DISCORD_SKIP_HUMAN_TO_BOT_IDS      comma-separated bot IDs: a human message that @mentions one
//                                      of these bots, does not @mention our bot and does not say
//                                      "claude" is not delivered (saves Claude turns; Claude can
//                                      still read it with fetch_messages)
//
// Safety:
//   - our own bot and unlisted bots stay dropped (the plugin's own check still runs)
//   - a bot message shaped like a permission reply ("yes abcde") is always dropped,
//     so a bot can never approve a tool call
//   - the plugin's access.json gate still runs after this: the channel must be in
//     groups and the bot ID must be in that group's allowFrom
//
// Log: plugin-patch/patch.log (decisions only, no message text).

import { EventEmitter } from 'node:events'
import { appendFileSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const entry = (process.argv[1] ?? '').replace(/\\/g, '/')
const ids = (v) => (v ?? '').split(',').map((s) => s.trim()).filter(Boolean)
const ALLOW = ids(process.env.DISCORD_ALLOW_BOT_IDS)
const SKIP_TO = ids(process.env.DISCORD_SKIP_HUMAN_TO_BOT_IDS)
const REQUIRE_MENTION = (process.env.DISCORD_ALLOW_BOT_REQUIRE_MENTION ?? '1') !== '0'
const PERM_RE = /^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$/i
const LOG = join(dirname(fileURLToPath(import.meta.url)), 'patch.log')

function log(line) {
  try { appendFileSync(LOG, `${new Date().toISOString()} pid=${process.pid} ${line}\n`) } catch {}
}

const isPluginServer = basename(entry) === 'server.ts' && /\/discord\//i.test(entry)

if (isPluginServer && (ALLOW.length > 0 || SKIP_TO.length > 0)) {
  const mentions = (msg, id) => {
    try { return Boolean(id && msg.mentions?.users?.has(id)) } catch { return false }
  }

  // Returns: undefined = leave the event as is, 'drop' = swallow it.
  const decide = (client, msg) => {
    const author = msg?.author
    if (!author) return undefined
    const me = client?.user?.id
    if (author.bot) {
      if (author.id === me || !ALLOW.includes(author.id)) return undefined
      if (PERM_RE.test(msg.content ?? '')) { log(`drop bot=${author.id} reason=permission-shaped`); return 'drop' }
      if (REQUIRE_MENTION && !mentions(msg, me)) { log(`skip bot=${author.id} reason=no-mention`); return 'drop' }
      // Mask the bot flag for this one message so the plugin's check lets it through.
      const masked = new Proxy(author, { get: (t, p) => (p === 'bot' ? false : Reflect.get(t, p)) })
      Object.defineProperty(msg, 'author', { value: masked, configurable: true, writable: true, enumerable: true })
      log(`pass bot=${author.id} channel=${msg.channelId}`)
      return undefined
    }
    if (SKIP_TO.length > 0 && !mentions(msg, me) && SKIP_TO.some((id) => mentions(msg, id)) && !/claude/i.test(msg.content ?? '')) {
      log(`skip human=${author.id} reason=addressed-to-other-bot`)
      return 'drop'
    }
    return undefined
  }

  // Wrap every 'messageCreate' listener when it is registered. (Patching emit does not
  // work: Bun's EventEmitter gives discord.js's Client its own instance-level emit.)
  const origOn = EventEmitter.prototype.on
  const wrapped = function (event, listener) {
    if (event === 'messageCreate' && typeof listener === 'function') {
      const inner = listener
      listener = function (msg, ...rest) {
        try {
          if (decide(this, msg) === 'drop') return
        } catch (e) {
          log(`error ${e}`)
        }
        return inner.call(this, msg, ...rest)
      }
    }
    return origOn.call(this, event, listener)
  }
  EventEmitter.prototype.on = wrapped
  EventEmitter.prototype.addListener = wrapped
  log(`active allow=[${ALLOW}] skipTo=[${SKIP_TO}] requireMention=${REQUIRE_MENTION}`)
}

// Optional: DISCORD_PERMISSION_CHANNEL_ID=<channel id> posts Claude's permission prompts
// (Allow / Deny buttons) in that channel, mentioning the user, instead of a DM to each
// allowlisted user. The plugin's own button handler still checks that whoever clicks is
// in access.json allowFrom, so other members and bots cannot approve.
import { createRequire } from 'node:module' // hoisted: loads before the plugin code runs
const PERM_CHANNEL = process.env.DISCORD_PERMISSION_CHANNEL_ID
if (isPluginServer && PERM_CHANNEL) {
  try {
    const { User } = createRequire(process.argv[1])('discord.js')
    const origSend = User.prototype.send
    const posted = new Set() // one channel post per request, though the plugin DMs every allowlisted user
    User.prototype.send = async function (options) {
      const content = typeof options === 'string' ? options : options?.content
      if (typeof content !== 'string' || !content.startsWith('🔐 Permission:') || !options?.components?.length) {
        return origSend.call(this, options)
      }
      const key = JSON.stringify(options.components.map((c) => (typeof c.toJSON === 'function' ? c.toJSON() : c)))
      if (posted.has(key)) return null
      posted.add(key)
      if (posted.size > 200) posted.delete(posted.values().next().value)
      const ch = await this.client.channels.fetch(PERM_CHANNEL)
      log(`permission prompt -> channel ${PERM_CHANNEL}`)
      return ch.send({ ...options, content: `<@${this.id}> ${content}`, allowedMentions: { users: [this.id] } })
    }
    log(`permission prompts go to channel ${PERM_CHANNEL}`)
  } catch (e) {
    log(`permission redirect failed: ${e}`)
  }
}
