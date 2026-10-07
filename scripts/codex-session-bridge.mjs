#!/usr/bin/env node
/**
 * codex-session-bridge.mjs
 * 
 * Strict live MCP server for Codex inter-session steering and delegation.
 * Connects 100% directly to Codex app-server WebSocket (ws://127.0.0.1:45000).
 * 
 * NO FALLBACK: Never falls back to passive CLI queue or stale SQLite cache.
 * All commands run strictly against the live session engine or fail fast with
 * clear, actionable error messages.
 */

import readline from 'node:readline';

const WS_URL = process.env.CODEX_APP_SERVER_WS || 'ws://127.0.0.1:45000';

function logErr(...args) {
  process.stderr.write(`[codex-session-bridge] ${args.join(' ')}\n`);
}

// ---------------------------------------------------------------------------
// 1. Direct WebSocket Client for Codex app-server
// ---------------------------------------------------------------------------
class CodexAppServerClient {
  constructor(url = WS_URL) {
    this.url = url;
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.activeTurnsByThread = new Map();
  }

  async connect() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

    return new Promise((resolve, reject) => {
      try {
        const ws = new WebSocket(this.url);
        const timeout = setTimeout(() => {
          try { ws.close(); } catch {}
          reject(new Error(`Cannot connect to Codex app-server at ${this.url}. Please ensure Codex was started with remote.bat (--listen ws://127.0.0.1:45000).`));
        }, 3000);

        ws.onopen = async () => {
          clearTimeout(timeout);
          this.ws = ws;
          try {
            await this.request('initialize', {
              clientInfo: { name: 'codex-session-bridge', title: 'Codex Session Bridge', version: '1.0.0' },
              capabilities: null
            });
            resolve();
          } catch (e) {
            reject(new Error(`Failed to initialize session with app-server: ${e.message}`));
          }
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.id !== undefined && this.pending.has(msg.id)) {
              const { resolve: res, reject: rej } = this.pending.get(msg.id);
              this.pending.delete(msg.id);
              if (msg.error) rej(new Error(msg.error.message || JSON.stringify(msg.error)));
              else res(msg.result);
            } else if (msg.method) {
              if (msg.method === 'turn/started' && msg.params?.threadId && msg.params?.turn?.id) {
                this.activeTurnsByThread.set(msg.params.threadId, msg.params.turn.id);
              } else if (msg.method === 'turn/ended' && msg.params?.threadId) {
                this.activeTurnsByThread.delete(msg.params.threadId);
              }
            }
          } catch (e) {
            logErr('Message parse error:', e.message);
          }
        };

        ws.onerror = (e) => {
          clearTimeout(timeout);
          reject(new Error(`WebSocket connection error to ${this.url}: ${e.message || 'connection failed'}`));
        };

        ws.onclose = () => {
          this.ws = null;
          for (const [, { reject: rej }] of this.pending) {
            rej(new Error('WebSocket connection closed by Codex app-server'));
          }
          this.pending.clear();
        };
      } catch (err) {
        reject(err);
      }
    });
  }

  request(method, params = {}) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error(`Not connected to Codex app-server at ${this.url}`);
    }
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Request ${method} (id=${id}) timed out after 30s`));
      }, 30000);

      this.pending.set(id, {
        resolve: (res) => { clearTimeout(timer); resolve(res); },
        reject: (err) => { clearTimeout(timer); reject(err); }
      });

      this.ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
    });
  }

  async listThreads() {
    await this.connect();
    const res = await this.request('thread/list', {});
    return res?.data || res?.threads || [];
  }

  async readThread(threadId, includeTurns = true) {
    await this.connect();
    const res = await this.request('thread/read', { threadId, includeTurns });
    return res?.thread || res;
  }

  async startTurn(threadId, text, turnTrigger = 'user') {
    await this.connect();
    return await this.request('turn/start', {
      threadId,
      input: [{ type: 'text', text }],
      turnTrigger
    });
  }

  async steerTurn(threadId, expectedTurnId, text) {
    await this.connect();
    return await this.request('turn/steer', {
      threadId,
      expectedTurnId,
      input: [{ type: 'text', text }]
    });
  }

  async interruptTurn(threadId, turnId = null) {
    await this.connect();
    return await this.request('turn/interrupt', { threadId, turnId });
  }

  async createThread(model = null, prompt = null) {
    await this.connect();
    const res = await this.request('thread/start', { model });
    const threadId = res?.threadId || res?.id || res?.data?.id;
    if (threadId && prompt) {
      await this.startTurn(threadId, prompt);
    }
    return res;
  }
}

const client = new CodexAppServerClient();

// ---------------------------------------------------------------------------
// 2. Thread ID Resolution (Live Only)
// ---------------------------------------------------------------------------
async function resolveLiveThreadId(input) {
  if (!input) throw new Error('Must provide a threadId or search query');
  const trimmed = input.trim();
  const uuidMatch = trimmed.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
  if (uuidMatch) return uuidMatch[1].toLowerCase();

  // Search in live thread list
  const threads = await client.listThreads();
  const lower = trimmed.toLowerCase();
  for (const t of threads) {
    const id = t.id || t.threadId;
    if (id && id.toLowerCase() === lower) return id;
    if (t.name && t.name.toLowerCase().includes(lower)) return id;
    if (t.title && t.title.toLowerCase().includes(lower)) return id;
    if (t.preview && t.preview.toLowerCase().includes(lower)) return id;
  }

  return trimmed;
}

// ---------------------------------------------------------------------------
// 3. Tool Handlers (Strict Live Operations)
// ---------------------------------------------------------------------------
async function handleListThreads(args = {}) {
  const limit = typeof args.limit === 'number' ? args.limit : 15;
  const query = typeof args.query === 'string' ? args.query.trim().toLowerCase() : '';

  const rawThreads = await client.listThreads();
  let threads = rawThreads.map(t => {
    const id = t.id || t.threadId;
    const isRunning = t.status?.type === 'active' || client.activeTurnsByThread.has(id);
    const activeTurn = client.activeTurnsByThread.get(id);
    return {
      id,
      title: t.name || t.preview || t.title || id,
      status: isRunning ? 'active' : (t.status?.type || 'idle'),
      activeTurnId: activeTurn || null,
      updatedAt: t.updatedAt || t.updated_at || ''
    };
  });

  if (query) {
    threads = threads.filter(t => t.id.toLowerCase().includes(query) || t.title.toLowerCase().includes(query));
  }
  threads = threads.slice(0, limit);

  const tableHeader = '| Thread Title | Thread ID | Status | Active Turn | Updated |\n|---|---|---|---|---|';
  const tableRows = threads.map(t => {
    const activeTurnStr = t.activeTurnId ? `\`${t.activeTurnId.slice(0, 8)}...\`` : '-';
    const statusStr = t.status === 'active' ? '🟢 active' : '⚪ idle';
    return `| **${t.title}** | \`${t.id}\` | ${statusStr} | ${activeTurnStr} | ${t.updatedAt ? new Date(t.updatedAt * 1000).toISOString().slice(0, 19).replace('T', ' ') : '-'} |`;
  }).join('\n');

  return {
    content: [{
      type: 'text',
      text: `## Live Codex Threads (${threads.length} threads found via app-server):\n\n${tableHeader}\n${tableRows}`
    }],
    threads
  };
}

async function handleSendMessage(args = {}) {
  const targetRaw = args.threadId || args.thread_id;
  const message = args.message || args.text || args.content;
  const mode = args.mode || 'auto'; // 'auto' | 'steer' | 'start'

  if (!targetRaw || !message) {
    throw new Error('Must provide threadId and message');
  }

  const threadId = await resolveLiveThreadId(targetRaw);

  // Inspect thread state from app-server
  const threadData = await client.readThread(threadId, true);
  const turns = threadData?.turns || [];
  const inProgressTurn = turns.find(t => t.status === 'inProgress');
  const activeTurnId = inProgressTurn?.id || client.activeTurnsByThread.get(threadId);

  // 1. Force STEER mode
  if (mode === 'steer') {
    if (!activeTurnId) {
      throw new Error(`Cannot steer thread ${threadId}: No active turn is in progress (thread is idle). Use mode="start" or mode="auto" to start a new turn.`);
    }
    const steerRes = await client.steerTurn(threadId, activeTurnId, message);
    return {
      content: [{
        type: 'text',
        text: `✅ [STEERED ACTIVE TURN] Successfully steered turn \`${activeTurnId}\` on thread \`${threadId}\`:\n\n> ${message}`
      }],
      action: 'steered',
      threadId,
      turnId: activeTurnId,
      result: steerRes
    };
  }

  // 2. Force START mode
  if (mode === 'start') {
    const startRes = await client.startTurn(threadId, message);
    return {
      content: [{
        type: 'text',
        text: `🚀 [STARTED NEW TURN] Successfully triggered new turn on thread \`${threadId}\`:\n\n> ${message}`
      }],
      action: 'started_turn',
      threadId,
      result: startRes
    };
  }

  // 3. AUTO mode: Steer if active, start if idle
  if (activeTurnId) {
    const steerRes = await client.steerTurn(threadId, activeTurnId, message);
    return {
      content: [{
        type: 'text',
        text: `✅ [STEERED ACTIVE TURN] Thread \`${threadId}\` had turn \`${activeTurnId}\` in progress. Successfully steered turn:\n\n> ${message}`
      }],
      action: 'steered',
      threadId,
      turnId: activeTurnId,
      result: steerRes
    };
  } else {
    const startRes = await client.startTurn(threadId, message);
    return {
      content: [{
        type: 'text',
        text: `🚀 [STARTED NEW TURN] Thread \`${threadId}\` was idle. Successfully started new turn:\n\n> ${message}`
      }],
      action: 'started_turn',
      threadId,
      result: startRes
    };
  }
}

async function handleSteerThread(args = {}) {
  const targetRaw = args.threadId || args.thread_id;
  const instructions = args.instructions || args.message;
  if (!targetRaw || !instructions) {
    throw new Error('Must provide threadId and instructions');
  }
  return await handleSendMessage({ threadId: targetRaw, message: instructions, mode: 'steer' });
}

async function handleReadThread(args = {}) {
  const targetRaw = args.threadId || args.thread_id;
  if (!targetRaw) throw new Error('Must provide threadId');
  const threadId = await resolveLiveThreadId(targetRaw);

  const thread = await client.readThread(threadId, true);
  const turns = thread?.turns || [];
  const inProgressTurn = turns.find(t => t.status === 'inProgress');

  const summary = [
    `# Thread: ${thread.name || thread.preview || thread.id}`,
    `- **ID:** \`${thread.id}\``,
    `- **CWD:** \`${thread.cwd || 'default'}\``,
    `- **Status:** ${inProgressTurn ? `🟢 In progress (Turn: \`${inProgressTurn.id}\`)` : '⚪ Idle'}`,
    `- **Total Turns:** ${turns.length}`,
    ``,
    `## Recent Turns:`
  ];

  const recent = turns.slice(-5);
  for (let i = 0; i < recent.length; i++) {
    const t = recent[i];
    summary.push(`### Turn ${turns.length - recent.length + i + 1} (\`${t.id}\` - Status: ${t.status})`);
    if (t.error) {
      summary.push(`⚠️ **Error:** ${t.error.message || JSON.stringify(t.error)}`);
    }
    if (Array.isArray(t.items)) {
      for (const item of t.items) {
        if (item.type === 'userMessage') {
          summary.push(`**User:**\n${(item.content || item.text || '').slice(0, 1000)}`);
        } else if (item.type === 'agentMessage') {
          summary.push(`**Assistant:**\n${(item.content || item.text || '').slice(0, 1500)}`);
        }
      }
    }
    summary.push('');
  }

  return {
    content: [{ type: 'text', text: summary.join('\n') }],
    thread
  };
}

async function handleCreateThread(args = {}) {
  const prompt = args.prompt || args.message || '';
  const model = args.model || null;

  const res = await client.createThread(model, prompt);
  const threadId = res?.threadId || res?.id || res?.data?.id || 'created';

  return {
    content: [{
      type: 'text',
      text: `✨ [THREAD CREATED] Successfully created thread \`${threadId}\`${prompt ? ` and started initial turn with prompt:\n\n> ${prompt}` : ''}`
    }],
    threadId,
    result: res
  };
}

async function handleInterruptThread(args = {}) {
  const targetRaw = args.threadId || args.thread_id;
  const turnId = args.turnId || args.turn_id || null;
  if (!targetRaw) throw new Error('Must provide threadId');
  const threadId = await resolveLiveThreadId(targetRaw);

  const res = await client.interruptTurn(threadId, turnId);
  return {
    content: [{
      type: 'text',
      text: `🛑 [INTERRUPTED] Successfully interrupted thread \`${threadId}\`${turnId ? ` (turn: ${turnId})` : ''}`
    }],
    threadId,
    result: res
  };
}

// ---------------------------------------------------------------------------
// 4. Tools Catalog for MCP
// ---------------------------------------------------------------------------
const TOOLS = [
  {
    name: 'send_message_to_thread',
    description: 'Send instructions or prompts to a Codex thread/session via live WebSocket RPC. In auto mode, immediately steers in-progress turns (turn/steer) or starts a new turn if idle (turn/start). No passive queueing.',
    inputSchema: {
      type: 'object',
      properties: {
        threadId: {
          type: 'string',
          description: 'The target Codex thread UUID or thread title keyword'
        },
        message: {
          type: 'string',
          description: 'Instructions, prompts, or feedback for the agent in that thread'
        },
        mode: {
          type: 'string',
          enum: ['auto', 'steer', 'start'],
          description: 'Steering mode: "auto" (steer if active turn running, else start new turn), "steer" (only steer in-progress turn, fails if idle), "start" (always start new turn). Default: "auto"'
        }
      },
      required: ['threadId', 'message']
    }
  },
  {
    name: 'steer_thread',
    description: 'Actively steer and redirect an in-progress turn in a Codex thread. Fails fast if the target thread has no active turn running.',
    inputSchema: {
      type: 'object',
      properties: {
        threadId: {
          type: 'string',
          description: 'The target Codex thread UUID or title'
        },
        instructions: {
          type: 'string',
          description: 'Steering instructions to guide the in-progress turn'
        }
      },
      required: ['threadId', 'instructions']
    }
  },
  {
    name: 'list_threads',
    description: 'List all live Codex sessions/threads directly from the running app-server with IDs, titles, status (active/idle), and active turn IDs.',
    inputSchema: {
      type: 'object',
      properties: {
        limit: {
          type: 'number',
          description: 'Max number of threads to return (default: 15)'
        },
        query: {
          type: 'string',
          description: 'Optional filter by title or ID'
        }
      }
    }
  },
  {
    name: 'read_thread',
    description: 'Read live conversation history, turns, status, and errors of a specific Codex thread from app-server.',
    inputSchema: {
      type: 'object',
      properties: {
        threadId: {
          type: 'string',
          description: 'Codex thread UUID or title'
        }
      },
      required: ['threadId']
    }
  },
  {
    name: 'create_thread',
    description: 'Create a new Codex thread/session on the app-server and start its first turn immediately.',
    inputSchema: {
      type: 'object',
      properties: {
        prompt: {
          type: 'string',
          description: 'Initial prompt / instructions for the new thread'
        },
        model: {
          type: 'string',
          description: 'Optional model override'
        }
      }
    }
  },
  {
    name: 'interrupt_thread',
    description: 'Immediately interrupt or cancel an ongoing turn in a Codex thread.',
    inputSchema: {
      type: 'object',
      properties: {
        threadId: {
          type: 'string',
          description: 'Codex thread UUID or title'
        },
        turnId: {
          type: 'string',
          description: 'Optional turn ID to cancel'
        }
      },
      required: ['threadId']
    }
  }
];

// ---------------------------------------------------------------------------
// 5. MCP stdio JSON-RPC Server
// ---------------------------------------------------------------------------
function sendJsonRpc(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on('line', async (line) => {
  const trimmed = line.trim();
  if (!trimmed.startsWith('{')) return;

  let rpc;
  try {
    rpc = JSON.parse(trimmed);
  } catch {
    return;
  }

  const { id, method, params } = rpc;

  // Notifications: ignore with no response
  if (method === 'notifications/initialized' || method === 'initialized' || id === undefined) {
    return;
  }

  // Ping
  if (method === 'ping') {
    sendJsonRpc({ jsonrpc: '2.0', id, result: {} });
    return;
  }

  // Initialize
  if (method === 'initialize') {
    sendJsonRpc({
      jsonrpc: '2.0',
      id,
      result: {
        protocolVersion: params?.protocolVersion || '2024-11-05',
        capabilities: { tools: { listChanged: true } },
        serverInfo: {
          name: 'codex-session-bridge',
          version: '1.0.0'
        },
        instructions: 'Direct live Codex inter-session steering tools: steer active turns, send messages between threads, list threads, and delegate tasks via app-server WebSocket. No passive queuing.'
      }
    });
    return;
  }

  // Tools list
  if (method === 'tools/list') {
    sendJsonRpc({
      jsonrpc: '2.0',
      id,
      result: { tools: TOOLS }
    });
    return;
  }

  // Tools call
  if (method === 'tools/call') {
    const toolName = params?.name;
    const args = params?.arguments || {};

    try {
      let result;
      if (toolName === 'send_message_to_thread') {
        result = await handleSendMessage(args);
      } else if (toolName === 'steer_thread') {
        result = await handleSteerThread(args);
      } else if (toolName === 'list_threads') {
        result = await handleListThreads(args);
      } else if (toolName === 'read_thread') {
        result = await handleReadThread(args);
      } else if (toolName === 'create_thread') {
        result = await handleCreateThread(args);
      } else if (toolName === 'interrupt_thread') {
        result = await handleInterruptThread(args);
      } else {
        throw new Error(`Unknown tool: ${toolName}`);
      }

      sendJsonRpc({
        jsonrpc: '2.0',
        id,
        result: {
          content: result.content || [{ type: 'text', text: JSON.stringify(result) }]
        }
      });
    } catch (err) {
      logErr(`Tool ${toolName} execution error:`, err.message);
      sendJsonRpc({
        jsonrpc: '2.0',
        id,
        result: {
          content: [{ type: 'text', text: `❌ Error: ${err.message}` }],
          isError: true
        }
      });
    }
    return;
  }

  // Fallback for unknown methods
  sendJsonRpc({
    jsonrpc: '2.0',
    id,
    error: { code: -32601, message: `Method not found: ${method}` }
  });
});

logErr(`Ready (strict live mode, direct WebSocket target: ${WS_URL})`);
