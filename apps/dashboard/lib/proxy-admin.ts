import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import YAML from 'yaml';

import { resolveRepoPath, validatePath } from './paths';

const PROXY_POOL_PATH = ['config', 'proxy', 'proxy_pools.yml'];
const MANUAL_PROXIES_PATH = ['config', 'manual_proxies.txt'];
const HTTPS_PROXIES_PATH = ['config', 'proxies_https.txt'];

const SUPPORTED_SCHEMES = new Set(['http', 'https', 'socks5', 'socks4', 'socks4a']);

export interface ProxyEntry {
  url: string;
  scheme: string;
  host: string;
  port: number;
  username: string | null;
  password: string | null;
  source: 'datacenter';
}

export interface ProxyMutationResult {
  added: ProxyEntry[];
  skipped: string[];
  total: number;
}

export interface ProxyRemovalResult {
  removed: ProxyEntry[];
  missing: string[];
  total: number;
}

interface ProxyParsed {
  normalized: string;
  raw: string;
  scheme: string;
  host: string;
  port: number;
  username: string | null;
  password: string | null;
}

function getAbsolutePath(segments: string[]): string {
  const absolute = resolveRepoPath(...segments);
  if (!validatePath(absolute)) {
    throw new Error(`Недопустимый путь: ${absolute}`);
  }
  return absolute;
}

function ensureSupportedScheme(scheme: string): void {
  if (!SUPPORTED_SCHEMES.has(scheme)) {
    throw new Error(`Неподдерживаемая схема прокси: ${scheme}`);
  }
}

function parseProxyUrl(input: string): ProxyParsed {
  const raw = input.trim();
  if (!raw) {
    throw new Error('Прокси URL не может быть пустым');
  }
  if (!raw.includes('://')) {
    throw new Error('Прокси URL должен содержать схему (например, http:// или socks5://)');
  }

  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`Некорректный формат прокси: ${raw}`);
  }

  const scheme = url.protocol.replace(/:$/, '').toLowerCase();
  ensureSupportedScheme(scheme);

  if (!url.hostname) {
    throw new Error(`Прокси ${raw} не содержит хоста`);
  }
  if (!url.port) {
    throw new Error(`Прокси ${raw} не содержит порт`);
  }

  const host = url.hostname.toLowerCase();
  const port = Number.parseInt(url.port, 10);
  if (Number.isNaN(port) || port <= 0 || port > 65535) {
    throw new Error(`Порт прокси ${raw} некорректен`);
  }

  const username = url.username ? decodeURIComponent(url.username) : null;
  const password = url.password ? decodeURIComponent(url.password) : null;

  const credentials =
    username !== null
      ? `${encodeURIComponent(username)}${password !== null ? `:${encodeURIComponent(password)}` : ''}@`
      : '';

  const normalized = `${scheme}://${credentials}${host}:${port}`;
  return {
    normalized,
    raw,
    scheme,
    host,
    port,
    username,
    password
  };
}

async function readProxyPool(): Promise<{ datacenter: string[]; full: Record<string, unknown> }> {
  const filePath = getAbsolutePath(PROXY_POOL_PATH);
  let contents = '';
  try {
    contents = await fs.readFile(filePath, 'utf-8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return { datacenter: [], full: {} };
    }
    throw error;
  }

  const parsed = YAML.parse(contents) ?? {};
  const datacenter = Array.isArray(parsed?.datacenter?.list) ? parsed.datacenter.list : [];
  return {
    datacenter: datacenter.filter((item: unknown): item is string => typeof item === 'string'),
    full: parsed
  };
}

async function writeProxyPool(config: Record<string, unknown>, proxies: string[]): Promise<void> {
  const clone = { ...config };
  const datacenter = { ...(clone.datacenter as Record<string, unknown> | undefined) };
  datacenter.list = proxies;
  clone.datacenter = datacenter;

  const yamlText = YAML.stringify(clone, { lineWidth: 0 });
  await writeFileAtomic(getAbsolutePath(PROXY_POOL_PATH), yamlText);
}

async function writeManualProxies(proxies: string[]): Promise<void> {
  const text = proxies.join(os.EOL) + (proxies.length ? os.EOL : '');
  await writeFileAtomic(getAbsolutePath(MANUAL_PROXIES_PATH), text);
}

async function writeHttpsProxies(proxies: ProxyParsed[]): Promise<void> {
  const lines = proxies
    .filter((proxy) => proxy.scheme === 'http' || proxy.scheme === 'https')
    .map((proxy) => {
      const username = proxy.username ?? '';
      const password = proxy.password ?? '';
      return `${proxy.host}:${proxy.port}:${username}:${password}`;
    });
  const text = lines.join(os.EOL) + (lines.length ? os.EOL : '');
  await writeFileAtomic(getAbsolutePath(HTTPS_PROXIES_PATH), text);
}

async function writeFileAtomic(targetPath: string, data: string): Promise<void> {
  const dir = path.dirname(targetPath);
  const tempPath = path.join(dir, `.tmp-${process.pid}-${Date.now()}`);
  await fs.writeFile(tempPath, data, { encoding: 'utf-8' });
  await fs.rename(tempPath, targetPath);
}

function serializeProxy(parsed: ProxyParsed): ProxyEntry {
  return {
    url: parsed.normalized,
    scheme: parsed.scheme,
    host: parsed.host,
    port: parsed.port,
    username: parsed.username,
    password: parsed.password,
    source: 'datacenter'
  };
}

export async function listDatacenterProxies(): Promise<{ proxies: ProxyEntry[]; total: number }> {
  const { datacenter } = await readProxyPool();
  const entries = datacenter.map((value) => serializeProxy(parseProxyUrl(value)));
  return {
    proxies: entries,
    total: entries.length
  };
}

export async function addDatacenterProxies(values: string[], replace = false): Promise<ProxyMutationResult> {
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error('Необходимо указать хотя бы один прокси');
  }

  const uniqueInputs = Array.from(new Set(values.map((item) => item.trim()))).filter(Boolean);
  if (uniqueInputs.length === 0) {
    throw new Error('Список прокси пустой после нормализации');
  }

  const parsedInputs = uniqueInputs.map((item) => {
    try {
      return parseProxyUrl(item);
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : String(error));
    }
  });

  const { datacenter, full } = await readProxyPool();
  const existingSet = new Map(
    datacenter.map((item) => {
      const parsed = parseProxyUrl(item);
      return [parsed.normalized, parsed];
    })
  );

  const added: ProxyEntry[] = [];
  const skipped: string[] = [];

  let nextList: ProxyParsed[];
  if (replace) {
    nextList = [];
  } else {
    nextList = Array.from(existingSet.values());
  }

  for (const proxy of parsedInputs) {
    if (existingSet.has(proxy.normalized) && !replace) {
      skipped.push(proxy.raw);
      continue;
    }
    existingSet.set(proxy.normalized, proxy);
    nextList.push(proxy);
    added.push(serializeProxy(proxy));
  }

  if (!replace) {
    // ensure deterministic ordering: existing order first, then new unique ones in input order
    const seen = new Set<string>();
    const combined: ProxyParsed[] = [];
    for (const proxy of datacenter.map((item) => parseProxyUrl(item))) {
      if (!seen.has(proxy.normalized)) {
        combined.push(proxy);
        seen.add(proxy.normalized);
      }
    }
    for (const proxy of nextList) {
      if (!seen.has(proxy.normalized)) {
        combined.push(proxy);
        seen.add(proxy.normalized);
      }
    }
    nextList = combined;
  } else {
    // in replace mode keep order from inputs only
    const seen = new Set<string>();
    nextList = parsedInputs.filter((proxy) => {
      if (seen.has(proxy.normalized)) {
        skipped.push(proxy.raw);
        return false;
      }
      seen.add(proxy.normalized);
      return true;
    });
    added.splice(0, added.length, ...nextList.map(serializeProxy));
  }

  const normalizedList = nextList.map((proxy) => proxy.normalized);
  await writeProxyPool(full, normalizedList);
  await writeManualProxies(normalizedList);
  await writeHttpsProxies(nextList);

  return {
    added,
    skipped,
    total: normalizedList.length
  };
}

export async function removeDatacenterProxies(values: string[]): Promise<ProxyRemovalResult> {
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error('Необходимо указать хотя бы один прокси для удаления');
  }

  const targets = values.map((value) => {
    const parsed = parseProxyUrl(value);
    return {
      raw: value.trim(),
      normalized: parsed.normalized
    };
  });
  const normalizedTargets = new Set(targets.map((item) => item.normalized));

  const { datacenter, full } = await readProxyPool();
  const parsedExisting = datacenter.map((item) => parseProxyUrl(item));

  const remaining: ProxyParsed[] = [];
  const removed: ProxyEntry[] = [];

  for (const proxy of parsedExisting) {
    if (normalizedTargets.has(proxy.normalized)) {
      removed.push(serializeProxy(proxy));
    } else {
      remaining.push(proxy);
    }
  }

  const missing: string[] = [];
  for (const target of targets) {
    if (!parsedExisting.some((proxy) => proxy.normalized === target.normalized)) {
      missing.push(target.raw);
    }
  }

  const normalizedList = remaining.map((proxy) => proxy.normalized);
  await writeProxyPool(full, normalizedList);
  await writeManualProxies(normalizedList);
  await writeHttpsProxies(remaining);

  return {
    removed,
    missing,
    total: normalizedList.length
  };
}
