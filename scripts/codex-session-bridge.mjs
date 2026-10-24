#!/usr/bin/env node
/**
 * codex-session-bridge.mjs
 * 
 * Strict live MCP server for Codex inter-session steering and delegation.
 * Dynamically connects directly to the live Codex app-server via:
 * 1. Unix Domain Socket (~/.codex/app-server-control/app-server-control.sock)
 * 2. Codex app-server proxy stdio bridge (`codex app-server proxy`)
 * 3. TCP WebSocket (ws://127.0.0.1:45000 or CODEX_APP_SERVER_WS)
 * 
 * NO FALLBACK: Never falls back to passive CLI queue or stale SQLite cache.
 * All commands run strictly against the live session engine or fail fast with
 * clear, actionable error messages.
 */

import readline from 'node:readline';
import net from 'node:net';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';

function logErr(...args) {
  process.stderr.write(`[codex-session-bridge] ${args.join(' ')}\n`);
}

// ---------------------------------------------------------------------------
// 1. Pure Node Stream-based WebSocket Client
// ---------------------------------------------------------------------------
class StreamWebSocket extends EventEmitter {
  constructor() {
    super();
    this.stream = null;
    this.childProc = null;
    this.upgraded = false;
    this.incomingBuffer = Buffer.alloc(0);
    this.connected = false;
    this.transportInfo = '';
  }

  async connect() {
    if (this.connected) return;

    return new Promise((resolve, reject) => {
      let settled = false;
      const onSuccess = () => {
        if (settled) return;
        settled = true;
        this.connected = true;
        this.emit('open');
        resolve();
      };
      const onFail = (err) => {
        if (settled) return;
        settled = true;
        this.cleanup();
        reject(err);
      };

      try {
        const stream = this._createStream();
        this.stream = stream;

        const timeout = setTimeout(() => {
          onFail(new Error(`Connection to Codex app-server (${this.transportInfo}) timed out after 5s`));
        }, 5000);

        const key = crypto.randomBytes(16).toString('base64');
        const handshake = 
          `GET / HTTP/1.1\r\n` +
          `Host: localhost\r\n` +
          `Upgrade: websocket\r\n` +
          `Connection: Upgrade\r\n` +
          `Sec-WebSocket-Key: ${key}\r\n` +
          `Sec-WebSocket-Version: 13\r\n\r\n`;

        stream.write(Buffer.from(handshake, 'utf8'));

        stream.on('data', (chunk) => {
          this.incomingBuffer = Buffer.concat([this.incomingBuffer, chunk]);
          if (!this.upgraded) {
            const idx = this.incomingBuffer.indexOf('\r\n\r\n');
            if (idx !== -1) {
              const header = this.incomingBuffer.subarray(0, idx).toString('utf8');
              if (!header.includes('101 Switching Protocols')) {
                clearTimeout(timeout);
                onFail(new Error(`WebSocket handshake rejected: ${header.split('\r\n')[0]}`));
                return;
              }
              this.upgraded = true;
              this.incomingBuffer = this.incomingBuffer.subarray(idx + 4);
              clearTimeout(timeout);
              onSuccess();
            }
          }

          if (this.upgraded) {
            this._parseFrames();
          }
        });

        stream.on('error', (err) => {
          this.emit('error', err);
          onFail(err);
        });

        stream.on('close', () => {
          this.cleanup();
          this.emit('close');
        });

        stream.on('end', () => {
          this.cleanup();
          this.emit('close');
        });
      } catch (err) {
        onFail(err);
      }
    });
  }

  _createStream() {
    // 1. Explicit WS TCP if configured
    if (process.env.CODEX_APP_SERVER_WS) {
      try {
        const parsed = new URL(process.env.CODEX_APP_SERVER_WS);
        const host = parsed.hostname || '127.0.0.1';
        const port = parseInt(parsed.port || '45000', 10);
        this.transportInfo = `tcp://${host}:${port}`;
        return net.createConnection({ host, port });
      } catch {}
    }

    // 2. UNIX domain socket (default daemon control socket on Linux/macOS)
    const defaultSock = path.join(os.homedir(), '.codex', 'app-server-control', 'app-server-control.sock');
    const sockPath = process.env.CODEX_APP_SERVER_SOCK || defaultSock;
    if (fs.existsSync(sockPath)) {
      this.transportInfo = `unix://${sockPath}`;
      return net.createConnection(sockPath);
    }

    // 3. Fallback: codex app-server proxy stdio bridge
    this.transportInfo = 'codex app-server proxy';
    const child = spawn('codex', ['app-server', 'proxy'], {
      stdio: ['pipe', 'pipe', 'ignore']
    });
    this.childProc = child;

    child.on('error', (err) => {
      this.emit('error', err);
    });

    const duplex = {
      write: (data) => child.stdin.write(data),
      on: (event, handler) => {
        if (event === 'data') child.stdout.on('data', handler);
        else if (event === 'error') child.on('error', handler);
        else if (event === 'close' || event === 'end') child.stdout.on(event, handler);
      },
      end: () => child.stdin.end(),
      destroy: () => child.kill()
    };
    return duplex;
  }

  _parseFrames() {
    while (this.incomingBuffer.length >= 2) {
      const b0 = this.incomingBuffer[0];
      const b1 = this.incomingBuffer[1];
      const opcode = b0 & 0x0f;
      let len = b1 & 0x7f;
      let offset = 2;

      if (len === 126) {
        if (this.incomingBuffer.length < 4) break;
        len = this.incomingBuffer.readUInt16BE(2);
        offset = 4;
      } else if (len === 127) {
        if (this.incomingBuffer.length < 10) break;
        len = Number(this.incomingBuffer.readBigUInt64BE(2));
        offset = 10;
      }

      if (this.incomingBuffer.length < offset + len) break;

      const payload = this.incomingBuffer.subarray(offset, offset + len);
      this.incomingBuffer = this.incomingBuffer.subarray(offset + len);

      if (opcode === 0x1) { // Text frame
        this.emit('message', payload.toString('utf8'));
      } else if (opcode === 0x9) { // Ping
        this._sendFrame(payload, 0xa); // Send Pong
      } else if (opcode === 0x8) { // Close
        this.cleanup();
        this.emit('close');
        break;
      }
    }
  }

  send(text) {
    if (!this.connected || !this.stream) {
      throw new Error('Not connected to Codex app-server');
    }
    const payload = Buffer.from(text, 'utf8');
    this._sendFrame(payload, 0x1);
  }

  _sendFrame(payload, opcode) {
    const mask = crypto.randomBytes(4);
    let header;
    let offset = 2;

    if (payload.length < 126) {
      header = Buffer.alloc(6);
      header[0] = 0x80 | opcode;
      header[1] = 0x80 | payload.length;
    } else if (payload.length < 65536) {
      header = Buffer.alloc(8);
      header[0] = 0x80 | opcode;
      header[1] = 0x80 | 126;
      header.writeUInt16BE(payload.length, 2);
      offset = 4;
    } else {
      header = Buffer.alloc(14);
      header[0] = 0x80 | opcode;
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(payload.length), 2);
      offset = 10;
    }

    mask.copy(header, offset);

    const maskedPayload = Buffer.alloc(payload.length);
    for (let i = 0; i < payload.length; i++) {
      maskedPayload[i] = payload[i] ^ mask[i % 4];
    }

    this.stream.write(Buffer.concat([header, maskedPayload]));
  }

  cleanup() {
    this.connected = false;
    this.upgraded = false;
    this.incomingBuffer = Buffer.alloc(0);
    if (this.stream) {
      try { this.stream.destroy?.(); } catch {}
      this.stream = null;
    }
    if (this.childProc) {
      try { this.childProc.kill(); } catch {}
      this.childProc = null;
    }
  }
}

// ---------------------------------------------------------------------------
// 2. Direct Live Client for Codex app-server
// ---------------------------------------------------------------------------
class CodexAppServerClient {
  constructor() {
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.activeTurnsByThread = new Map();
    this.lastResumeErrorByThread = new Map();
  }

  async connect() {
    if (this.ws && this.ws.connected) return;

    const ws = new StreamWebSocket();
    await ws.connect();
    this.ws = ws;

    ws.on('message', (data) => {
      try {
        const msg = JSON.parse(data);
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
    });

    ws.on('close', () => {
      for (const [, { reject: rej }] of this.pending) {
        rej(new Error('WebSocket connection closed by Codex app-server'));
      }
      this.pending.clear();
      this.ws = null;
    });

    ws.on('error', (e) => {
      logErr('Transport error:', e.message);
    });

    // Initialize session with app-server
    try {
      await this.request('initialize', {
        clientInfo: { name: 'codex-session-bridge', title: 'Codex Session Bridge', version: '1.0.0' },
        capabilities: null
      });
      this.ws.send(JSON.stringify({ jsonrpc: '2.0', method: 'initialized' }));
      logErr(`Connected to live Codex engine via ${ws.transportInfo}`);
    } catch (e) {
      this.ws.cleanup();
      this.ws = null;
      throw new Error(`Failed to initialize session with app-server: ${e.message}`);
    }
  }

  request(method, params = {}) {
    if (!this.ws || !this.ws.connected) {
      throw new Error('Not connected to Codex app-server');
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

  async readThread(threadId) {
    await this.connect();
    const res = await this.request('thread/read', { threadId });
    return res?.thread || res;
  }

  async listThreadItems(threadId, limit = 20, sortDirection = 'desc') {
    await this.connect();
    try {
      const res = await this.request('thread/items/list', { threadId, limit, sortDirection });
      return res?.data || [];
    } catch {
      return [];
    }
  }

  async resumeThread(threadId) {
    await this.connect();
    try {
      const res = await this.request('thread/resume', { threadId, excludeTurns: true });
      this.lastResumeErrorByThread.delete(threadId);
      return res?.thread || res;
    } catch (err) {
      logErr(`resumeThread error for ${threadId}:`, err.message);
      this.lastResumeErrorByThread.set(threadId, err.message);
      throw err;
    }
  }

  async ensureThreadLoaded(threadId) {
    try {
      const thread = await this.readThread(threadId);
      if (!thread || thread.status?.type === 'notLoaded' || thread.canAcceptDirectInput === false || thread.canAcceptDirectInput === null) {
        logErr(`Thread ${threadId} is notLoaded, resuming via thread/resume...`);
        await this.resumeThread(threadId);
      }
    } catch (e) {
      if (e.message && e.message.includes('already has an active writer')) {
        throw new Error(`Thread \`${threadId}\` đang bị mở độc quyền bởi một cửa sổ/tab ChatGPT Desktop khác (active writer lock). Hãy tạm thời đổi sang tab khác hoặc đóng tab đó để thread nhận lệnh điều hướng.`);
      }
      logErr(`ensureThreadLoaded check failed (${e.message}), attempting resume...`);
      try {
        await this.resumeThread(threadId);
      } catch (resumeErr) {
        if (resumeErr.message && resumeErr.message.includes('already has an active writer')) {
          throw new Error(`Thread \`${threadId}\` đang bị mở độc quyền bởi một cửa sổ/tab ChatGPT Desktop khác (active writer lock). Hãy tạm thời đổi sang tab khác hoặc đóng tab đó để thread nhận lệnh điều hướng.`);
        }
      }
    }
  }

  async startTurn(threadId, text, turnTrigger = 'user') {
    await this.connect();
    try {
      const res = await this.request('turn/start', {
        threadId,
        input: [{ type: 'text', text, text_elements: [] }],
        turnTrigger
      });
      const turnId = res?.turn?.id || res?.id;
      if (turnId) {
        this.activeTurnsByThread.set(threadId, turnId);
      }
      return res;
    } catch (err) {
      const msg = err.message || '';
      if (msg.includes('not found') || msg.includes('not loaded') || msg.includes('notLoaded')) {
        logErr(`startTurn failed with '${msg}', resuming thread ${threadId} and retrying...`);
        await this.resumeThread(threadId);
        const retryRes = await this.request('turn/start', {
          threadId,
          input: [{ type: 'text', text, text_elements: [] }],
          turnTrigger
        });
        const turnId = retryRes?.turn?.id || retryRes?.id;
        if (turnId) {
          this.activeTurnsByThread.set(threadId, turnId);
        }
        return retryRes;
      }
      throw err;
    }
  }

  async steerTurn(threadId, expectedTurnId, text) {
    await this.connect();
    const res = await this.request('turn/steer', {
      threadId,
      expectedTurnId,
      input: [{ type: 'text', text, text_elements: [] }]
    });
    return res;
  }

  async interruptTurn(threadId, turnId = null) {
    await this.connect();
    let targetTurnId = turnId;
    if (!targetTurnId) {
      targetTurnId = this.activeTurnsByThread.get(threadId);
      if (!targetTurnId) {
        const items = await this.listThreadItems(threadId, 5);
        if (items.length > 0) {
          targetTurnId = items[items.length - 1]?.turnId;
        }
      }
    }
    if (!targetTurnId) {
      throw new Error(`Cannot interrupt thread ${threadId}: No active turn found to interrupt.`);
    }
    const res = await this.request('turn/interrupt', { threadId, turnId: targetTurnId });
    this.activeTurnsByThread.delete(threadId);
    return res;
  }

  async createThread(model = null, prompt = null) {
    await this.connect();
    const res = await this.request('thread/start', model ? { model } : {});
    const threadId = res?.thread?.id || res?.threadId || res?.id || res?.data?.id;
    if (threadId && prompt) {
      await this.startTurn(threadId, prompt);
    }
    return { ...res, threadId };
  }
}

const client = new CodexAppServerClient();

// ---------------------------------------------------------------------------
// 3. Thread ID Resolution (Live Only)
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
// 4. Tool Handlers (Strict Live Operations)
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

function formatItem(itemEntry) {
  const it = itemEntry.item || {};
  const turnStr = itemEntry.turnId ? `\`${itemEntry.turnId.slice(0, 8)}...\`` : '';
  const timeStr = itemEntry.startedAtMs ? new Date(itemEntry.startedAtMs).toLocaleTimeString('vi-VN') : '';
  const headerTime = timeStr ? ` [${timeStr}]` : '';

  if (it.type === 'userMessage') {
    let text = '';
    if (typeof it.content === 'string') text = it.content;
    else if (Array.isArray(it.content)) {
      text = it.content.map(c => {
        if (typeof c === 'string') return c;
        if (c && typeof c === 'object') return c.text || c.content || '';
        return '';
      }).filter(Boolean).join('\n');
    } else if (it.text) {
      text = it.text;
    }
    return `### 👤 User ${turnStr}${headerTime}\n${text.trim()}`;
  }

  if (it.type === 'agentMessage') {
    let text = it.text || '';
    if (!text && Array.isArray(it.content)) {
      text = it.content.map(c => (typeof c === 'string' ? c : c?.text || '')).filter(Boolean).join('\n');
    }
    return `### 🤖 Assistant ${turnStr}${headerTime}\n${text.trim()}`;
  }

  if (it.type === 'commandExecution') {
    const cmd = it.command || '';
    const exitStr = it.exitCode !== undefined && it.exitCode !== null ? ` (exit: ${it.exitCode})` : '';
    const output = it.aggregatedOutput ? `\n> Output: ${it.aggregatedOutput.slice(-200).trim().replace(/\n/g, ' ')}` : '';
    return `> ⚙️ \`[Command] ${cmd}\`${exitStr}${output}`;
  }

  if (it.type === 'mcpToolCall') {
    const tool = it.tool || it.name || 'mcpTool';
    return `> 🛠️ \`[MCP Tool] ${tool}\``;
  }

  if (it.type === 'reasoning') {
    const summary = Array.isArray(it.summary) ? it.summary.join(' ') : (it.summary || '');
    if (summary) {
      return `> 💭 *[Reasoning]* ${summary.slice(0, 300)}...`;
    }
    return null;
  }

  return null;
}

async function handleSendMessage(args = {}) {
  const targetRaw = args.threadId || args.thread_id;
  const message = args.message || args.text || args.content;
  const mode = args.mode || 'auto'; // 'auto' | 'steer' | 'start'

  if (!targetRaw || !message) {
    throw new Error('Must provide threadId and message');
  }

  const threadId = await resolveLiveThreadId(targetRaw);

  // Ensure thread is loaded into daemon memory
  await client.ensureThreadLoaded(threadId);

  let activeTurnId = client.activeTurnsByThread.get(threadId);

  // 1. Force STEER mode
  if (mode === 'steer') {
    if (!activeTurnId) {
      const recent = await client.listThreadItems(threadId, 3, 'desc');
      activeTurnId = recent[0]?.turnId;
    }
    if (!activeTurnId) {
      throw new Error(`Cannot steer thread ${threadId}: No active turn is known to be in progress. Use mode="start" or mode="auto" to start a new turn.`);
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
    const turnId = startRes?.turn?.id || startRes?.id || 'started';
    return {
      content: [{
        type: 'text',
        text: `🚀 [STARTED NEW TURN] Successfully triggered new turn \`${turnId}\` on thread \`${threadId}\`:\n\n> ${message}`
      }],
      action: 'started_turn',
      threadId,
      turnId,
      result: startRes
    };
  }

  // 3. AUTO mode: Steer if active turn known, else start turn
  if (activeTurnId) {
    try {
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
    } catch (steerErr) {
      logErr(`Auto-steer failed (${steerErr.message}), falling back to startTurn`);
      client.activeTurnsByThread.delete(threadId);
    }
  }

  try {
    const startRes = await client.startTurn(threadId, message);
    const turnId = startRes?.turn?.id || startRes?.id || 'started';
    return {
      content: [{
        type: 'text',
        text: `🚀 [STARTED NEW TURN] Successfully started new turn \`${turnId}\` on thread \`${threadId}\`:\n\n> ${message}`
      }],
      action: 'started_turn',
      threadId,
      turnId,
      result: startRes
    };
  } catch (startErr) {
    const errStr = startErr.message || '';
    if (errStr.includes('thread not found') && client.lastResumeErrorByThread.has(threadId)) {
      const resumeErr = client.lastResumeErrorByThread.get(threadId);
      if (resumeErr && resumeErr.includes('already has an active writer')) {
        throw new Error(`Thread \`${threadId}\` đang bị khóa độc quyền bởi một cửa sổ/tab ChatGPT Desktop khác (active writer lock). Hãy tạm thời đổi sang tab khác hoặc đóng tab đó để thread nhận lệnh điều hướng.`);
      }
    }
    if (errStr.includes('active') || errStr.includes('running') || errStr.includes('conflict')) {
      const recent = await client.listThreadItems(threadId, 3, 'desc');
      const fallbackTurnId = recent[0]?.turnId;
      if (fallbackTurnId) {
        const steerRes = await client.steerTurn(threadId, fallbackTurnId, message);
        return {
          content: [{
            type: 'text',
            text: `✅ [STEERED ACTIVE TURN] Thread had active turn \`${fallbackTurnId}\`. Steered turn:\n\n> ${message}`
          }],
          action: 'steered',
          threadId,
          turnId: fallbackTurnId,
          result: steerRes
        };
      }
    }
    throw startErr;
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
  const limit = typeof args.limit === 'number' ? args.limit : 20;
  if (!targetRaw) throw new Error('Must provide threadId');
  const threadId = await resolveLiveThreadId(targetRaw);

  const thread = await client.readThread(threadId);
  const rawItems = await client.listThreadItems(threadId, limit, 'desc');
  const items = rawItems.slice().reverse();

  const isRunning = client.activeTurnsByThread.has(threadId) || thread?.status?.type === 'active';
  const activeTurn = client.activeTurnsByThread.get(threadId);

  const summary = [
    `# Thread: ${thread.name || thread.preview || thread.id}`,
    `- **ID:** \`${thread.id}\``,
    `- **CWD:** \`${thread.cwd || 'default'}\``,
    `- **Status:** ${isRunning ? `🟢 In progress${activeTurn ? ` (Turn: \`${activeTurn}\`)` : ''}` : '⚪ Idle'}`,
    `- **Model:** ${thread.model || 'default'} (${thread.modelProvider || 'openai'})`,
    `- **Latest Activity (${items.length} items, chronological):**`,
    ``
  ];

  if (items.length > 0) {
    for (const item of items) {
      const formatted = formatItem(item);
      if (formatted) {
        summary.push(formatted);
        summary.push('');
      }
    }
  } else {
    summary.push('_No recent items found in this thread._');
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
  const threadId = res?.threadId || res?.thread?.id || res?.id || res?.data?.id || 'created';

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
      text: `🛑 [INTERRUPTED] Successfully interrupted thread \`${threadId}\``
    }],
    threadId,
    result: res
  };
}

// ---------------------------------------------------------------------------
// 5. Tools Catalog for MCP
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
// 6. MCP stdio JSON-RPC Server
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
          version: '1.1.0'
        },
        instructions: 'Direct live Codex inter-session steering tools: steer active turns, send messages between threads, list threads, and delegate tasks via app-server live connection. No passive queuing.'
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

logErr('Ready (strict live mode, multi-transport enabled)');
