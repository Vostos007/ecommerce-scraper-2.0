import path from 'node:path';

import type { Role } from './permissions';

interface AuditFilesystem {
  appendFile: (filepath: string, data: string) => Promise<void>;
  mkdir: (dir: string, options?: { recursive?: boolean }) => Promise<void>;
  readdir: (dir: string) => Promise<string[]>;
  stat: (filepath: string) => Promise<{ mtimeMs: number }>;
  unlink: (filepath: string) => Promise<void>;
}

const LOG_DIRECTORY = process.env.AUDIT_LOG_DIR
  ? path.resolve(process.env.AUDIT_LOG_DIR)
  : path.resolve(process.cwd(), '..', '..', 'logs');
const PRIMARY_LOG = path.join(LOG_DIRECTORY, 'audit.jsonl');
const RETENTION_DAYS = Number(process.env.AUDIT_LOG_RETENTION_DAYS ?? 90);

async function loadFs(): Promise<AuditFilesystem | null> {
  if (typeof process === 'undefined' || process.env?.NEXT_RUNTIME === 'edge') {
    return null;
  }

  try {
    const fs = await import('node:fs/promises');
    return {
      appendFile: async (filepath, data) => {
        await fs.appendFile(filepath, data);
      },
      mkdir: async (dir) => {
        await fs.mkdir(dir, { recursive: true });
      },
      readdir: fs.readdir,
      stat: fs.stat,
      unlink: fs.unlink
    };
  } catch (error) {
    console.error('Не удалось загрузить fs/promises для audit logging', error);
    return null;
  }
}

function sanitizeValue<T>(value: T): T {
  if (typeof value === 'string') {
    return value.replace(/[\r\n]+/g, ' ') as T;
  }
  if (typeof value === 'object' && value !== null) {
    return JSON.parse(JSON.stringify(value)) as T;
  }
  return value;
}

export interface AuditEntry {
  timestamp: string;
  userId: string;
  username: string;
  role?: Role;
  action: string;
  resource?: string;
  details?: Record<string, unknown>;
  ip: string;
  userAgent: string;
  success: boolean;
  error?: string;
}

function getDailyLogPath(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    const fallback = new Date();
    return path.join(LOG_DIRECTORY, `audit-${fallback.toISOString().slice(0, 10)}.jsonl`);
  }
  return path.join(LOG_DIRECTORY, `audit-${date.toISOString().slice(0, 10)}.jsonl`);
}

async function appendToFiles(fs: AuditFilesystem, entry: AuditEntry, data: string) {
  await fs.mkdir(LOG_DIRECTORY, { recursive: true });
  await Promise.all([
    fs.appendFile(PRIMARY_LOG, data),
    fs.appendFile(getDailyLogPath(entry.timestamp), data)
  ]);
}

async function rotateOldLogs(fs: AuditFilesystem) {
  const retentionMs = RETENTION_DAYS * 24 * 60 * 60 * 1000;
  if (retentionMs <= 0) {
    return;
  }

  let entries: string[] = [];
  try {
    entries = await fs.readdir(LOG_DIRECTORY);
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') {
      return;
    }
    console.error('Не удалось прочитать каталог audit logs', error);
    return;
  }

  const cutoff = Date.now() - retentionMs;
  await Promise.all(
    entries
      .filter((file) => file.startsWith('audit-') && file.endsWith('.jsonl'))
      .map(async (file) => {
        try {
          const { mtimeMs } = await fs.stat(path.join(LOG_DIRECTORY, file));
          if (mtimeMs < cutoff) {
            await fs.unlink(path.join(LOG_DIRECTORY, file));
          }
        } catch (error) {
          console.error('Не удалось удалить устаревший audit log', error);
        }
      })
  );
}

export async function logAuditEvent(entry: AuditEntry): Promise<void> {
  const fs = await loadFs();
  const sanitizedEntry: AuditEntry = {
    ...entry,
    action: sanitizeValue(entry.action),
    username: sanitizeValue(entry.username),
    ip: sanitizeValue(entry.ip),
    userAgent: sanitizeValue(entry.userAgent),
    ...(entry.resource ? { resource: sanitizeValue(entry.resource) } : {}),
    ...(entry.error ? { error: sanitizeValue(entry.error) } : {}),
    ...(entry.details ? { details: sanitizeValue(entry.details) } : {}),
    ...(entry.role ? { role: entry.role } : {})
  };
  const payload = `${JSON.stringify(sanitizedEntry)}\n`;

  if (!fs) {
    console.warn('Audit log недоступен в Edge runtime', sanitizedEntry);
    return;
  }

  await appendToFiles(fs, sanitizedEntry, payload);
  void rotateOldLogs(fs);
}

export async function logLogin(params: {
  userId: string;
  username: string;
  role?: Role;
  success: boolean;
  ip: string;
  userAgent: string;
  error?: string;
}): Promise<void> {
  await logAuditEvent({
    timestamp: new Date().toISOString(),
    action: 'login',
    resource: 'auth/login',
    ...params
  });
}

export async function logLogout(params: {
  userId: string;
  username: string;
  role?: Role;
  ip: string;
  userAgent: string;
}): Promise<void> {
  await logAuditEvent({
    timestamp: new Date().toISOString(),
    action: 'logout',
    resource: 'auth/logout',
    success: true,
    ...params
  });
}

export async function logExportAction(
  userId: string,
  username: string,
  action: 'export_start' | 'export_stop',
  site: string,
  details: Record<string, unknown>,
  metadata: { ip: string; userAgent: string; success: boolean }
): Promise<void> {
  await logAuditEvent({
    timestamp: new Date().toISOString(),
    action,
    resource: site,
    userId,
    username,
    success: metadata.success,
    details,
    ip: metadata.ip,
    userAgent: metadata.userAgent
  });
}

export async function logFileOperation(
  userId: string,
  username: string,
  action: 'file_upload' | 'file_download' | 'file_delete',
  filePath: string,
  metadata: { size?: number; ip: string; userAgent: string; success: boolean }
): Promise<void> {
  const details: Record<string, unknown> = {};
  if (typeof metadata.size === 'number') {
    details.size = metadata.size;
  }
  await logAuditEvent({
    timestamp: new Date().toISOString(),
    action,
    resource: filePath,
    userId,
    username,
    success: metadata.success,
    details,
    ip: metadata.ip,
    userAgent: metadata.userAgent
  });
}
