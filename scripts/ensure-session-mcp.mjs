#!/usr/bin/env node
/**
 * ensure-session-mcp.mjs
 * 
 * Checks if ~/.codex/config.toml contains codex_session MCP server configuration.
 * If missing, automatically adds it pointing to codex-session-bridge.mjs.
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const configPath = path.join(os.homedir(), '.codex', 'config.toml');
const bridgeScript = path.resolve(import.meta.dirname, 'codex-session-bridge.mjs');

if (!fs.existsSync(configPath)) {
  console.log(`[setup-mcp] config.toml not found at ${configPath}`);
  process.exit(0);
}

let content = fs.readFileSync(configPath, 'utf-8');

const nodeExe = process.execPath.replace(/\\/g, '\\\\');
const bridgePath = bridgeScript.replace(/\\/g, '\\\\');

const desiredEntry = `[mcp_servers.codex_session]
command = '${nodeExe}'
args = ['${bridgePath}']`;

if (content.includes('[mcp_servers.codex_session]')) {
  // Update path if changed
  const regex = /\[mcp_servers\.codex_session\][\s\S]*?args\s*=\s*\[.*?\]/;
  if (regex.test(content)) {
    content = content.replace(regex, desiredEntry);
    fs.writeFileSync(configPath, content, 'utf-8');
    console.log(`[setup-mcp] Updated [mcp_servers.codex_session] in ${configPath}`);
  } else {
    console.log('[setup-mcp] [mcp_servers.codex_session] already configured in config.toml.');
  }
  process.exit(0);
}

fs.appendFileSync(configPath, `\n${desiredEntry}\n`, 'utf-8');
console.log(`[setup-mcp] Successfully registered [mcp_servers.codex_session] in ${configPath}`);
