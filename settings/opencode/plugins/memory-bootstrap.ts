import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import type { Plugin } from "@opencode-ai/plugin";

const execFileAsync = promisify(execFile);

const SERVICE = "memory-bootstrap";
const memoryCache = new Map<string, { mtimeMs: number; content: string }>();

function resolveAgentName(value: unknown): string | undefined {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed || undefined;
  }

  if (value && typeof value === "object") {
    const maybeName = (value as { name?: unknown }).name;
    if (typeof maybeName === "string") {
      const trimmed = maybeName.trim();
      return trimmed || undefined;
    }
  }

  return undefined;
}

async function warn(client: Parameters<Plugin>[0]["client"], message: string, extra?: Record<string, unknown>) {
  await client.app.log({
    body: {
      service: SERVICE,
      level: "warn",
      message,
      extra,
    },
  });
}

async function info(client: Parameters<Plugin>[0]["client"], message: string, extra?: Record<string, unknown>) {
  await client.app.log({
    body: {
      service: SERVICE,
      level: "info",
      message,
      extra,
    },
  });
}

async function loadMemoryIndex(
  client: Parameters<Plugin>[0]["client"],
  root: string,
  agentName: string,
  sessionID?: string,
) {
  const memoryPath = path.join(root, ".claude", "agent-memory", agentName, "MEMORY.md");

  try {
    const stats = await fs.stat(memoryPath);
    const cached = memoryCache.get(memoryPath);

    if (cached && cached.mtimeMs === stats.mtimeMs) {
      return cached.content;
    }

    const content = await fs.readFile(memoryPath, "utf8");
    if (!content.trim()) return "";

    const memoryPrelude = [
      `## Loaded Session Memory (${agentName})`,
      `Loaded automatically from \`${memoryPath}\`.`,
      "Use this as active memory index for the current session.",
      "If you need details from referenced topic files, load them on demand.",
      "",
      content,
    ].join("\n");

    memoryCache.set(memoryPath, {
      mtimeMs: stats.mtimeMs,
      content: memoryPrelude,
    });

    await info(client, "Reloaded agent memory index", {
      sessionID,
      agent: agentName,
      memoryPath,
      mtimeMs: stats.mtimeMs,
    });

    return memoryPrelude;
  } catch (error: any) {
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") {
      memoryCache.delete(memoryPath);
      return "";
    }
    throw Object.assign(error, { memoryPath });
  }
}

async function runSessionInit(client: Parameters<Plugin>[0]["client"], sessionID: string): Promise<void> {
  if (process.env.LAOMEI_MEMORY_SYNC === "0") return;
  const repo = process.env.ASSISTANT_SETUP_REPO ?? path.join(os.homedir(), "workspace", "assistant");
  const scriptPath = path.join(repo, "scripts", "memory-sync.sh");
  try {
    await execFileAsync(scriptPath, ["--init", "opencode"]);
    await info(client, "Session init completed", { sessionID });
  } catch (error: any) {
    await warn(client, "Session init failed (non-fatal)", {
      sessionID,
      code: error?.code,
      message: error?.message,
    });
  }
}

export const MemoryBootstrapPlugin: Plugin = async ({ client, directory, worktree }) => {
  const sessionAgents = new Map<string, string>();
  const warnedSessions = new Set<string>();
  const initializedSessions = new Set<string>();

  return {
    "chat.message": async (input, output) => {
      const agentName = resolveAgentName(output.message.agent) ?? resolveAgentName(input.agent);
      if (agentName) {
        sessionAgents.set(input.sessionID, agentName);
      }
    },

    "chat.params": async (input) => {
      const agentName = resolveAgentName(input.agent);
      if (agentName) {
        sessionAgents.set(input.sessionID, agentName);
      }

      // Session init — run once per session (first message only)
      if (input.sessionID && !initializedSessions.has(input.sessionID)) {
        initializedSessions.add(input.sessionID);
        await runSessionInit(client, input.sessionID);
      }
    },

    "experimental.chat.system.transform": async (input, output) => {
      if (!Array.isArray(output.system)) {
        await warn(client, "System transform output is not an array", {
          sessionID: input.sessionID,
          model: input.model,
        });
        return;
      }

      if (!input.sessionID) {
        return;
      }

      const agentName = resolveAgentName(sessionAgents.get(input.sessionID));
      if (!agentName) {
        if (!warnedSessions.has(input.sessionID)) {
          warnedSessions.add(input.sessionID);
          await warn(client, "Unable to resolve agent for memory bootstrap", {
            sessionID: input.sessionID,
            model: input.model,
          });
        }
        return;
      }

      try {
        const memoryRoot = worktree === "/" ? directory : worktree;
        const memoryPrelude = await loadMemoryIndex(client, memoryRoot, agentName, input.sessionID);
        if (!memoryPrelude) {
          return;
        }

        if (!output.system.includes(memoryPrelude)) {
          output.system.push(memoryPrelude);
        }
      } catch (error: any) {
        await warn(client, "Failed to load agent memory index", {
          sessionID: input.sessionID,
          agent: agentName,
          memoryPath: error?.memoryPath,
          code: error?.code,
          message: error?.message,
        });
      }
    },
  };
};
