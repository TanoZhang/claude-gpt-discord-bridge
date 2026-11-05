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
//
// The plugin's prompt only names the tool ("🔐 Permission: Read"). Claude Code sends the
// details with the request (the tool input and a reason) as an MCP notification on stdin.
// A second 'data' listener, added right after the MCP transport's own, reads those lines
// (the transport still gets every byte), so the channel post says exactly what Claude wants
// to do: which file, which command, which URL. Without details the post stays as it was.
import { createRequire } from 'node:module' // hoisted: loads before the plugin code runs
import { StringDecoder } from 'node:string_decoder'
const PERM_CHANNEL = process.env.DISCORD_PERMISSION_CHANNEL_ID

// Wording of the channel post.
const PERM_T = {
  head: (what, tool) => `🔐 **Claude wants to ${what}** (${tool})`,
  useTool: (name) => `use the tool ${name}`,
  purpose: (s) => `Purpose: ${s}`,
  chars: (n) => ` (${n} chars)`,
  edits: (n) => ` (${n} edits, showing the first)`,
  actions: {
    Read: 'read a file', Write: 'write a file', Edit: 'edit a file', MultiEdit: 'edit a file', NotebookEdit: 'edit a notebook',
    Bash: 'run a command', PowerShell: 'run a command', WebFetch: 'open a web page', WebSearch: 'search the web',
    Glob: 'find files', Grep: 'search file contents', Agent: 'start a subagent', Task: 'start a subagent', Skill: 'use a skill',
  },
}

const clip = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s }
const fence = (s, lang = '') => '```' + lang + '\n' + String(s).replace(/```/g, '`​``') + '\n```'
const inline = (s) => '`' + String(s).replace(/`/g, "'") + '`'
const MAIN_KEYS = ['command', 'file_path', 'notebook_path', 'url', 'query', 'pattern', 'path', 'skill', 'prompt']

// Returns { text, cut } for the channel post, or null when there is nothing to add.
function describePermission(d) {
  const tool = String(d.tool_name ?? '')
  const mcp = /^mcp__(.+?)__(.+)$/.exec(tool)
  const lines = [PERM_T.head(PERM_T.actions[tool] ?? PERM_T.useTool(mcp ? mcp[2] : tool), tool)]
  let input = null
  try { const v = JSON.parse(d.input_preview); if (v && typeof v === 'object' && !Array.isArray(v)) input = v } catch {}
  let cut = false
  if (input) {
    if ((tool === 'Bash' || tool === 'PowerShell') && typeof input.command === 'string') {
      if (input.description) lines.push(PERM_T.purpose(clip(input.description, 200)))
      lines.push(fence(clip(input.command, 900), tool === 'PowerShell' ? 'powershell' : 'bash'))
      cut = input.command.length > 900
    } else if ((tool === 'Edit' || tool === 'MultiEdit') && typeof input.file_path === 'string') {
      const edits = Array.isArray(input.edits) ? input.edits : [input]
      lines.push(inline(clip(input.file_path, 300)) + (edits.length > 1 ? PERM_T.edits(edits.length) : ''))
      const e = edits[0] ?? {}
      const side = (s, mark) => clip(s, 400).split('\n').map((l) => mark + l).join('\n')
      if (typeof e.old_string === 'string' || typeof e.new_string === 'string') {
        lines.push(fence(side(e.old_string ?? '', '- ') + '\n' + side(e.new_string ?? '', '+ '), 'diff'))
        cut = String(e.old_string ?? '').length > 400 || String(e.new_string ?? '').length > 400 || edits.length > 1
      }
    } else if (tool === 'Write' && typeof input.file_path === 'string') {
      const body = String(input.content ?? '')
      lines.push(inline(clip(input.file_path, 300)) + PERM_T.chars(body.length))
      if (body) lines.push(fence(clip(body, 300)))
      cut = body.length > 300
    } else {
      const key = MAIN_KEYS.find((k) => typeof input[k] === 'string')
      if (key) lines.push(key === 'prompt' ? clip(input[key], 300) : inline(clip(input[key], 300)))
      const rest = Object.fromEntries(Object.entries(input).filter(([k]) => k !== key))
      if (Object.keys(rest).length) {
        const j = JSON.stringify(rest)
        lines.push(fence(clip(j, 500), 'json'))
        cut = j.length > 500
      }
    }
  } else if (d.input_preview) {
    lines.push(fence(clip(d.input_preview, 900)))
    cut = String(d.input_preview).length > 900
  }
  const why = String(d.description ?? '').trim()
  if (why && why !== tool) lines.push('> ' + clip(why, 300).replace(/\n/g, ' '))
  let text = lines.join('\n')
  if (text.length > 1800) { text = lines[0] + '\n' + fence(clip(d.input_preview, 1200)); cut = true }
  return { text, cut }
}

if (isPluginServer && PERM_CHANNEL) {
  const details = new Map() // request_id -> { tool_name, description, input_preview }
  try {
    const stdin = process.stdin
    const origOn = stdin.on
    const decoder = new StringDecoder('utf8')
    let buf = ''
    let attached = false
    const sniff = (chunk) => {
      buf += typeof chunk === 'string' ? chunk : decoder.write(chunk)
      let nl
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)
        if (!line.includes('channel/permission_request')) continue
        try {
          const m = JSON.parse(line)
          const p = m?.params
          if (m?.method === 'notifications/claude/channel/permission_request' && p?.request_id) {
            details.set(p.request_id, p)
            if (details.size > 100) details.delete(details.keys().next().value)
          }
        } catch {}
      }
      if (buf.length > 8e6) buf = '' // a huge unfinished line is never a permission request
    }
    stdin.on = function (event, listener) {
      const r = origOn.call(this, event, listener)
      if (event === 'data' && !attached) { attached = true; origOn.call(this, 'data', sniff) }
      return r
    }
  } catch (e) {
    log(`permission details sniffer failed: ${e}`)
  }

  try {
    const { User } = createRequire(process.argv[1])('discord.js')
    const origSend = User.prototype.send
    const posted = new Set() // one channel post per request, though the plugin DMs every allowlisted user
    User.prototype.send = async function (options) {
      const content = typeof options === 'string' ? options : options?.content
      if (typeof content !== 'string' || !content.startsWith('🔐 Permission:') || !options?.components?.length) {
        return origSend.call(this, options)
      }
      const rows = options.components.map((c) => (typeof c.toJSON === 'function' ? c.toJSON() : c))
      const key = JSON.stringify(rows)
      if (posted.has(key)) return null
      posted.add(key)
      if (posted.size > 200) posted.delete(posted.values().next().value)
      const id = rows.flatMap((r) => r.components ?? [])
        .map((b) => /^perm:(?:allow|deny|more):([a-km-z]{5})$/.exec(b.custom_id ?? '')?.[1]).find(Boolean)
      for (let i = 0; id && !details.has(id) && i < 10; i++) await new Promise((r) => setTimeout(r, 30))
      let text = content
      let components = options.components
      let info = null
      try { info = id && details.has(id) ? describePermission(details.get(id)) : null } catch (e) { log(`permission details failed: ${e}`) }
      if (info) {
        text = info.text
        // the details are in the post now; keep "See more" only when something was cut
        if (!info.cut) components = rows.map((r) => ({ ...r, components: (r.components ?? []).filter((b) => !String(b.custom_id ?? '').startsWith('perm:more:')) }))
      }
      const ch = await this.client.channels.fetch(PERM_CHANNEL)
      log(`permission prompt -> channel ${PERM_CHANNEL} (details=${info ? 'yes' : 'no'})`)
      return ch.send({ ...options, content: `<@${this.id}> ${text}`, components, allowedMentions: { users: [this.id] } })
    }
    log(`permission prompts go to channel ${PERM_CHANNEL}`)
  } catch (e) {
    log(`permission redirect failed: ${e}`)
  }
}
