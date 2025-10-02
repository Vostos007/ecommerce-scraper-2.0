import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { accessSync, constants as fsConstants, existsSync } from 'node:fs';
import path from 'node:path';

import { resolveRepoPath } from './paths';
import { assertSiteAllowed } from './sites.server';
import { sanitizeSite } from './sites';
import { generateId } from './utils';
import { recordExportResult, recordExportStart } from './metrics';

export interface SpawnOptions {
  concurrency?: number | undefined;
  resume?: boolean | undefined;
  extraArgs?: string[] | undefined;
  limit?: number | undefined;
}

export type LogKind = 'out' | 'err';

export interface LogEntry {
  k: LogKind;
  m: string;
  t: number;
}

export interface ProcessSubscriber {
  onLog?: (entry: LogEntry) => void;
  onClose?: (info: { code: number | null; signal: NodeJS.Signals | null }) => void;
}

export interface ProcessRecord {
  jobId: string;
  site: string;
  script: string;
  args: string[];
  python: string;
  process: ChildProcessWithoutNullStreams;
  createdAt: Date;
  logs: LogEntry[];
  subscribers: Set<ProcessSubscriber>;
  stdoutBuffer: string;
  stderrBuffer: string;
  killTimer: NodeJS.Timeout;
  reaperTimer: NodeJS.Timeout | null;
  exitCode: number | null;
  exitSignal: NodeJS.Signals | null;
  status: 'running' | 'completed';
  totalUrls?: number;
  processedUrls?: number;
  successUrls?: number;
  failedUrls?: number;
  progressPercent?: number;
  estimatedSecondsRemaining?: number;
}

export interface SpawnResult {
  jobId: string;
  site: string;
  script: string;
  args: string[];
  python: string;
  startedAt: string;
}

export interface QueuedExport {
  id: string;
  site: string;
  options: SpawnOptions;
  createdAt: Date;
}

const MAX_CONCURRENT_PROCESSES = 4;
const LOG_BUFFER_SIZE = 2048;
const PROCESS_TIMEOUT_MS = 60 * 60 * 1000;
const PROCESS_REAPER_DELAY_MS = 24 * 60 * 60 * 1000;

const globalStore = globalThis as typeof globalThis & {
  __dashboardActiveProcesses?: Map<string, ProcessRecord>;
};

const activeProcesses: Map<string, ProcessRecord> = globalStore.__dashboardActiveProcesses ?? new Map();

if (!globalStore.__dashboardActiveProcesses) {
  globalStore.__dashboardActiveProcesses = activeProcesses;
}

const queuedExports: Map<string, QueuedExport> = new Map();

let cachedPythonBinary: string | null = null;

function isExecutable(candidate: string): boolean {
  try {
    accessSync(candidate, fsConstants.X_OK);
    return true;
  } catch {
    if (process.platform === 'win32') {
      return existsSync(candidate);
    }
    return false;
  }
}

function hasPathSeparator(value: string): boolean {
  return value.includes(path.sep) || (process.platform === 'win32' && value.includes('/'));
}

function resolveViaPath(command: string): string | null {
  const pathEnv = process.env.PATH;
  if (!pathEnv) {
    return null;
  }

  if (process.platform === 'win32') {
    const pathext = process.env.PATHEXT?.split(';').filter(Boolean) ?? ['.exe', '.bat', '.cmd'];
    for (const dir of pathEnv.split(path.delimiter)) {
      if (!dir) continue;
      for (const ext of pathext) {
        const candidate = path.join(dir, command.endsWith(ext.toLowerCase()) ? command : `${command}${ext.toLowerCase()}`);
        if (isExecutable(candidate)) {
          return candidate;
        }
      }
    }
    return null;
  }

  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, command);
    if (isExecutable(candidate)) {
      return candidate;
    }
  }
  return null;
}

function resolveExecutable(command: string): string | null {
  if (!command) {
    return null;
  }

  if (hasPathSeparator(command)) {
    return isExecutable(command) ? command : null;
  }

  const resolved = resolveViaPath(command);
  return resolved ?? (isExecutable(command) ? command : null);
}

function resolvePythonBinary(): string {
  if (cachedPythonBinary) {
    return cachedPythonBinary;
  }

  const fromEnv = process.env.PYTHON_BIN;
  if (fromEnv) {
    const resolvedFromEnv = resolveExecutable(fromEnv);
    if (!resolvedFromEnv) {
      throw new Error(`PYTHON_BIN=${fromEnv} не является исполняемым файлом`);
    }
    cachedPythonBinary = resolvedFromEnv;
    return cachedPythonBinary;
  }

  for (const command of ['python3', 'python']) {
    const resolved = resolveExecutable(command);
    if (resolved) {
      cachedPythonBinary = resolved;
      return resolved;
    }
  }

  throw new Error('Не удалось определить путь до Python интерпретатора. Установите PYTHON_BIN.');
}

export function getPythonBinary(): string {
  return resolvePythonBinary();
}

function clampConcurrency(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.max(1, Math.min(128, Math.trunc(value)));
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number.parseInt(value, 10);
    if (Number.isFinite(parsed)) {
      return Math.max(1, Math.min(128, parsed));
    }
  }
  return undefined;
}

function sanitizeExtraArgs(args: unknown): string[] | undefined {
  if (!Array.isArray(args)) {
    return undefined;
  }
  const safe: string[] = [];
  for (const value of args) {
    if (typeof value !== 'string') {
      continue;
    }
    const trimmed = value.trim();
    if (!trimmed) {
      continue;
    }
    if (/[^\w\-./=]/.test(trimmed)) {
      continue;
    }
    safe.push(trimmed);
  }
  return safe.length ? safe : undefined;
}

function ensureCapacity(): void {
  let running = 0;
  for (const record of activeProcesses.values()) {
    if (record.status === 'running') {
      running += 1;
    }
  }
  if (running >= MAX_CONCURRENT_PROCESSES) {
    throw new Error('Превышен лимит одновременных экспортов');
  }
}

function pushLog(record: ProcessRecord, kind: LogKind, message: string): void {
  const entry: LogEntry = { k: kind, m: message, t: Date.now() };
  record.logs.push(entry);
  if (record.logs.length > LOG_BUFFER_SIZE) {
    record.logs.splice(0, record.logs.length - LOG_BUFFER_SIZE);
  }
  for (const subscriber of record.subscribers) {
    subscriber.onLog?.(entry);
  }
}

function flushBuffers(record: ProcessRecord): void {
  if (record.stdoutBuffer) {
    pushLog(record, 'out', record.stdoutBuffer);
    record.stdoutBuffer = '';
  }
  if (record.stderrBuffer) {
    pushLog(record, 'err', record.stderrBuffer);
    record.stderrBuffer = '';
  }
}

function scheduleReaper(record: ProcessRecord): void {
  if (record.reaperTimer) {
    record.reaperTimer.refresh();
    return;
  }
  record.reaperTimer = setTimeout(() => {
    activeProcesses.delete(record.jobId);
  }, PROCESS_REAPER_DELAY_MS);
  record.reaperTimer.unref();
}

export function buildEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };

  // Гарантируем, что Python-интерпретатор доступен и корректно указан.
  if (!env.PYTHON_BIN || !env.PYTHON_BIN.trim()) {
    env.PYTHON_BIN = getPythonBinary();
  }

  env.PYTHONIOENCODING = env.PYTHONIOENCODING ?? 'utf-8';

  const repoRoot = resolveRepoPath('.');
  const pythonPathEntries = [repoRoot];
  if (env.PYTHONPATH && env.PYTHONPATH.trim()) {
    pythonPathEntries.push(env.PYTHONPATH);
  }
  env.PYTHONPATH = pythonPathEntries.join(path.delimiter);

  return env;
}

export function spawnExport(siteInput: string, options: SpawnOptions = {}): SpawnResult {
  const sanitized = sanitizeSite(siteInput);
  if (!sanitized) {
    throw new Error('Недопустимый параметр site');
  }

  ensureCapacity();

  const site = assertSiteAllowed(sanitized);
  const python = getPythonBinary();
  const jobId = generateId();
  const concurrency = clampConcurrency(options.concurrency);
  const extraArgs = sanitizeExtraArgs(options.extraArgs);
  const limit = typeof options.limit === 'number' && Number.isFinite(options.limit) && options.limit > 0
    ? Math.trunc(options.limit)
    : undefined;

  const args: string[] = ['-u', '-m', `scripts.${site.script}`];
  if (typeof concurrency === 'number') {
    args.push('--concurrency', String(concurrency));
  }
  if (options.resume === true) {
    args.push('--resume');
  } else if (options.resume === false) {
    args.push('--no-resume');
  }
  if (typeof limit === 'number') {
    args.push('--limit', String(limit));
  }
  if (extraArgs) {
    args.push(...extraArgs);
  }

  const child = spawn(python, args, {
    cwd: resolveRepoPath('.'),
    env: buildEnv(),
    shell: false
  });

  child.stdout.setEncoding('utf-8');
  child.stderr.setEncoding('utf-8');

  const killTimer = setTimeout(() => {
    if (!child.killed) {
      child.kill('SIGTERM');
      setTimeout(() => {
        if (!child.killed) {
          child.kill('SIGKILL');
        }
      }, 10_000).unref();
    }
  }, PROCESS_TIMEOUT_MS);
  killTimer.unref();

  const record: ProcessRecord = {
    jobId,
    site: site.domain,
    script: site.script,
    args,
    python,
    process: child,
    createdAt: new Date(),
    logs: [],
    subscribers: new Set(),
    stdoutBuffer: '',
    stderrBuffer: '',
    killTimer,
    reaperTimer: null,
    exitCode: null,
    exitSignal: null,
    status: 'running'
  };

  child.stdout.on('data', (chunk: string) => {
    record.stdoutBuffer += chunk;
    const lines = record.stdoutBuffer.split(/\r?\n/);
    record.stdoutBuffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line) continue;
      pushLog(record, 'out', line);
    }
  });

  child.stderr.on('data', (chunk: string) => {
    record.stderrBuffer += chunk;
    const lines = record.stderrBuffer.split(/\r?\n/);
    record.stderrBuffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line) continue;
      pushLog(record, 'err', line);
    }
  });

  let finalized = false;
  const finalize = (status: 'success' | 'failure') => {
    if (finalized) {
      return;
    }
    finalized = true;
    const duration = Date.now() - record.createdAt.getTime();
    recordExportResult(record.site, status, duration);
  };

  child.on('exit', (code, signal) => {
    clearTimeout(killTimer);
    record.exitCode = code;
    record.exitSignal = signal;
    record.status = 'completed';
    flushBuffers(record);
    scheduleReaper(record);
    finalize(code === 0 ? 'success' : 'failure');
    for (const subscriber of record.subscribers) {
      subscriber.onClose?.({ code, signal });
    }
  });

  child.on('error', (error) => {
    pushLog(record, 'err', `Spawn error: ${error.message}`);
    finalize('failure');
    stopProcess(jobId, 'SIGTERM');
  });

  activeProcesses.set(jobId, record);

  recordExportStart(record.site);

  console.info('[dashboard] spawn export', { jobId, site: site.domain, script: site.script, args });

  return {
    jobId,
    site: site.domain,
    script: site.script,
    args,
    python,
    startedAt: record.createdAt.toISOString()
  };
}

export function getProcess(jobId: string): ProcessRecord | null {
  return activeProcesses.get(jobId) ?? null;
}

export function stopProcess(jobId: string, signal: NodeJS.Signals = 'SIGTERM'): boolean {
  const record = activeProcesses.get(jobId);
  if (!record) {
    return false;
  }
  const result = record.process.kill(signal);
  return result;
}

interface SubscribeOptions {
  replay?: boolean;
  signal?: AbortSignal;
}

export function subscribeToProcess(jobId: string, subscriber: ProcessSubscriber, options: SubscribeOptions = {}): () => void {
  const record = activeProcesses.get(jobId);
  if (!record) {
    throw new Error('Экспорт с указанным jobId не найден или уже завершён');
  }

  record.subscribers.add(subscriber);

  if (options.replay !== false) {
    for (const entry of record.logs) {
      subscriber.onLog?.(entry);
    }
    if (record.status === 'completed') {
      subscriber.onClose?.({ code: record.exitCode, signal: record.exitSignal });
    }
  }

  const cleanup = () => {
    record.subscribers.delete(subscriber);
  };

  if (options.signal) {
    if (options.signal.aborted) {
      cleanup();
    } else {
      options.signal.addEventListener(
        'abort',
        () => {
          cleanup();
        },
        { once: true }
      );
    }
  }

  return cleanup;
}

export function findActiveJobForSite(site: string): ProcessRecord | null {
  for (const record of activeProcesses.values()) {
    if (record.site === site && record.status === 'running') {
      return record;
    }
  }
  return null;
}

export function scheduleExport(site: string, options: SpawnOptions = {}): { state: 'started'; job: SpawnResult } | { state: 'queued'; queued: QueuedExport } {
  const running = findActiveJobForSite(site);
  if (running) {
    throw new Error('Экспорт для этого сайта уже запущен');
  }

  try {
    ensureCapacity();
    const result = spawnExport(site, options);
    return { state: 'started', job: result };
  } catch (error) {
    if (error instanceof Error && error.message === 'Превышен лимит одновременных экспортов') {
      const id = generateId();
      const queued: QueuedExport = {
        id,
        site,
        options,
        createdAt: new Date()
      };
      queuedExports.set(id, queued);
      return { state: 'queued', queued };
    }
    throw error;
  }
}

export function cancelQueuedExport(queueId: string): boolean {
  return queuedExports.delete(queueId);
}

export function listQueuedExports(): QueuedExport[] {
  return Array.from(queuedExports.values());
}

export function listProcesses(): ProcessRecord[] {
  return Array.from(activeProcesses.values());
}
