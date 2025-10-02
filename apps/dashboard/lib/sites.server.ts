import fs from 'node:fs';
import path from 'node:path';

import { resolveRepoPath } from './paths';
import type { SiteConfig, SiteStatus, SiteSummary, MapStatus } from './sites';
import { sanitizeSite } from './sites';

const SCRIPT_ALLOWLIST: Record<string, string> = {
  'atmospherestore.ru': 'atmosphere_fast_export',
  // City Knitting скрипт обслуживает домен sittingknitting.ru
  'sittingknitting.ru': 'cityknitting_fast_export',
  'knitshop.ru': 'knitshop_fast_export',
  'mpyarn.ru': 'mpyarn_fast_export',
  'ili-ili.com': 'ili_ili_fast_export',
  '6wool.ru': 'sixwool_fast_export',
  'triskeli.ru': 'triskeli_fast_export',
  'triskeli.com': 'triskeli_fast_export',
  'mp-yarn.ru': 'mpyarn_fast_export'
};

const CACHE_TTL_MS = 60_000;

const scriptHealthCache = new Map<string, boolean>();

const configPath = resolveRepoPath('config', 'sites.json');

const CANONICAL_MAP_FILENAMES: Record<string, string> = {
  'mpyarn.ru': 'mpyarn.ru.URL_map.json'
};

export const CSV_EXPORT_FILES: Record<string, string> = {
  full_data: 'full_data.csv',
  seo: 'seo.csv',
  changes: 'changes.csv'
};

interface CacheEntry {
  expiresAt: number;
  summaries: SiteSummary[];
  lookup: Map<string, SiteConfig>;
}

let cache: CacheEntry | null = null;

const MAP_FRESH_THRESHOLD_MS = 7 * 24 * 60 * 60 * 1000;

type MapSource = 'canonical' | 'uploaded' | 'legacy';

export interface MapFileMetadata {
  size: number;
  modified: string;
  linkCount: number | null;
  isValid: boolean;
}

export interface MapFileInfo {
  filePath: string;
  fileName: string;
  source: MapSource;
  metadata: MapFileMetadata;
}

function readConfig(): SiteConfig[] {
  const file = fs.readFileSync(configPath, 'utf-8');
  const parsed = JSON.parse(file) as { sites?: SiteConfig[] };
  return parsed.sites ?? [];
}

function computeExportMetadata(domain: string): {
  lastExport: string | null;
  exportStatus: SiteStatus;
  mapFile: string | null;
  mapStatus: MapStatus;
  mapLastModified: string | null;
  mapLinkCount: number | null;
} {
  try {
    const exportDir = resolveRepoPath('data', 'sites', domain, 'exports');
    const excelPath = path.join(exportDir, 'latest.xlsx');
    const jsonPath = path.join(exportDir, 'latest.json');

    let stat: fs.Stats | null = null;
    if (fs.existsSync(excelPath)) {
      stat = fs.statSync(excelPath);
    } else if (fs.existsSync(jsonPath)) {
      stat = fs.statSync(jsonPath);
    }

    if (!stat) {
      const mapDetails = resolveMapMetadata(domain);
      return {
        lastExport: null,
        exportStatus: 'missing_export',
        ...mapDetails
      };
    }

    const mapDetails = resolveMapMetadata(domain);
    return {
      lastExport: stat.mtime.toISOString(),
      exportStatus: 'ready',
      ...mapDetails
    };
  } catch {
    const mapDetails = resolveMapMetadata(domain);
    return { lastExport: null, exportStatus: 'unknown', ...mapDetails };
  }
}

function resolveMapMetadata(domain: string): {
  mapFile: string | null;
  mapStatus: MapStatus;
  mapLastModified: string | null;
  mapLinkCount: number | null;
} {
  try {
    const active = getActiveMapFile(domain);
    if (!active) {
      return {
        mapFile: null,
        mapStatus: 'missing',
        mapLastModified: null,
        mapLinkCount: null
      };
    }

    const modifiedTime = Date.parse(active.metadata.modified);
    const isFresh = Number.isFinite(modifiedTime)
      ? Date.now() - modifiedTime <= MAP_FRESH_THRESHOLD_MS
      : false;
    return {
      mapFile: active.fileName,
      mapStatus: isFresh ? 'available' : 'outdated',
      mapLastModified: active.metadata.modified,
      mapLinkCount: active.metadata.linkCount
    };
  } catch {
    return {
      mapFile: null,
      mapStatus: 'missing',
      mapLastModified: null,
      mapLinkCount: null
    };
  }
}

function buildCache(): CacheEntry {
  const sites = readConfig();
  const lookup = new Map<string, SiteConfig>();
  const summaries: SiteSummary[] = [];

  for (const site of sites) {
    const normalizedDomain = sanitizeSite(site.domain);
    if (!normalizedDomain) {
      continue;
    }
    const script = SCRIPT_ALLOWLIST[normalizedDomain];
    if (!script) {
      continue;
    }

    if (!scriptHealthCache.has(script)) {
      const scriptPath = resolveRepoPath('scripts', `${script}.py`);
      const exists = fs.existsSync(scriptPath);
      if (!exists) {
        console.warn('[dashboard] export script missing', { script, scriptPath });
      }
      scriptHealthCache.set(script, exists);
    }

    const metadata = computeExportMetadata(normalizedDomain);
    const exportStatus =
      metadata.exportStatus === 'ready' && metadata.mapStatus === 'missing'
        ? 'missing_map'
        : metadata.exportStatus;
    lookup.set(normalizedDomain, { ...site, domain: normalizedDomain });
    summaries.push({
      domain: normalizedDomain,
      name: site.name,
      lastExport: metadata.lastExport,
      status: exportStatus,
      script,
      mapFile: metadata.mapFile,
      mapStatus: metadata.mapStatus,
      mapLastModified: metadata.mapLastModified,
      mapLinkCount: metadata.mapLinkCount
    });
  }

  cache = {
    expiresAt: Date.now() + CACHE_TTL_MS,
    summaries,
    lookup
  };
  return cache;
}

function getCache(): CacheEntry {
  if (!cache || cache.expiresAt < Date.now()) {
    return buildCache();
  }
  return cache;
}

export function getSiteSummaries(): SiteSummary[] {
  return getCache().summaries;
}

export function getSiteByDomain(domain: string): (SiteConfig & { domain: string; script: string }) | null {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    return null;
  }
  const entry = getCache().lookup.get(sanitized);
  if (!entry) {
    return null;
  }
  const script = SCRIPT_ALLOWLIST[sanitized];
  if (!script) {
    return null;
  }
  return {
    ...entry,
    domain: sanitized,
    script
  };
}

export function assertSiteAllowed(domain: string): (SiteConfig & { domain: string; script: string }) {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    throw new Error('Invalid site domain');
  }
  const entry = getCache().lookup.get(sanitized);
  const script = SCRIPT_ALLOWLIST[sanitized];
  if (!entry || !script) {
    console.warn('[dashboard] попытка доступа к неразрешенному сайту', { domain });
    throw new Error(`Site ${domain} is not configured`);
  }
  return { ...entry, domain: sanitized, script };
}

export function getSiteDirectory(domain: string): string {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    throw new Error('Invalid site domain');
  }
  const dir = resolveRepoPath('data', 'sites', sanitized);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  return dir;
}

export function getExportPath(domain: string): string {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    throw new Error('Invalid site domain');
  }
  const exportPath = path.join(getSiteDirectory(sanitized), 'exports', 'latest.xlsx');
  if (!fs.existsSync(exportPath)) {
    throw new Error(`Export file not found for ${sanitized}`);
  }
  return exportPath;
}

export function getCsvExportPath(domain: string, sheet: string): string | null {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    return null;
  }
  const fileName = CSV_EXPORT_FILES[sheet];
  if (!fileName) {
    return null;
  }
  const candidate = path.join(getSiteDirectory(sanitized), 'exports', fileName);
  return fs.existsSync(candidate) ? candidate : null;
}

export function getScriptAllowList(): Record<string, string> {
  return { ...SCRIPT_ALLOWLIST };
}

export function invalidateSiteSummariesCache(): void {
  cache = null;
}

export function getCanonicalMapFileName(domain: string): string {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    throw new Error('Invalid site domain');
  }
  return CANONICAL_MAP_FILENAMES[sanitized] ?? `${sanitized}.URL-map.json`;
}

function safeStats(filePath: string): fs.Stats | null {
  try {
    return fs.statSync(filePath);
  } catch {
    return null;
  }
}

export function getMapFileMetadata(filePath: string): MapFileMetadata {
  const stats = safeStats(filePath);
  if (!stats) {
    return {
      size: 0,
      modified: new Date(0).toISOString(),
      linkCount: null,
      isValid: false
    };
  }

  let linkCount: number | null = null;
  let isValid = false;
  try {
    const raw = fs.readFileSync(filePath, 'utf-8');
    const parsed = JSON.parse(raw) as { links?: unknown } | unknown;
    if (parsed && typeof parsed === 'object') {
      const links = (parsed as { links?: unknown }).links;
      if (Array.isArray(links)) {
        linkCount = links.length;
        isValid = true;
      }
    }
  } catch {
    linkCount = null;
    isValid = false;
  }

  return {
    size: stats.size,
    modified: stats.mtime.toISOString(),
    linkCount,
    isValid
  };
}

function collectJsonFiles(dirPath: string): string[] {
  try {
    return fs
      .readdirSync(dirPath)
      .filter((file) => file.toLowerCase().endsWith('.json'))
      .map((file) => path.join(dirPath, file));
  } catch {
    return [];
  }
}

export function getAvailableMapFiles(domain: string): MapFileInfo[] {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    throw new Error('Invalid site domain');
  }
  const siteDir = getSiteDirectory(sanitized);
  const canonicalName = getCanonicalMapFileName(sanitized);
  const canonicalPath = path.join(siteDir, canonicalName);
  const mapsDir = path.join(siteDir, 'maps');

  const files = new Map<string, MapFileInfo>();

  if (fs.existsSync(canonicalPath)) {
    files.set(canonicalPath, {
      filePath: canonicalPath,
      fileName: canonicalName,
      source: 'canonical',
      metadata: getMapFileMetadata(canonicalPath)
    });
  }

  const uploaded = collectJsonFiles(mapsDir);
  for (const filePath of uploaded) {
    const fileName = path.basename(filePath);
    files.set(filePath, {
      filePath,
      fileName,
      source: 'uploaded',
      metadata: getMapFileMetadata(filePath)
    });
  }

  const legacy = collectJsonFiles(siteDir).filter((filePath) => {
    if (filePath === canonicalPath) {
      return false;
    }
    if (filePath.startsWith(path.join(siteDir, 'maps', path.sep))) {
      return false;
    }
    if (filePath.includes(`${path.sep}exports${path.sep}`)) {
      return false;
    }
    return true;
  });

  for (const filePath of legacy) {
    const fileName = path.basename(filePath);
    if (!files.has(filePath)) {
      files.set(filePath, {
        filePath,
        fileName,
        source: 'legacy',
        metadata: getMapFileMetadata(filePath)
      });
    }
  }

  return Array.from(files.values()).sort((a, b) => {
    const aTime = Date.parse(a.metadata.modified);
    const bTime = Date.parse(b.metadata.modified);
    return (Number.isFinite(bTime) ? bTime : 0) - (Number.isFinite(aTime) ? aTime : 0);
  });
}

export function getActiveMapFile(domain: string): MapFileInfo | null {
  const sanitized = sanitizeSite(domain);
  if (!sanitized) {
    throw new Error('Invalid site domain');
  }

  const canonicalName = getCanonicalMapFileName(sanitized);
  const siteDir = getSiteDirectory(sanitized);
  const canonicalPath = path.join(siteDir, canonicalName);
  const canonicalExists = fs.existsSync(canonicalPath);
  const canonicalInfo = canonicalExists
    ? {
        filePath: canonicalPath,
        fileName: canonicalName,
        source: 'canonical' as MapSource,
        metadata: getMapFileMetadata(canonicalPath)
      }
    : null;

  if (canonicalInfo) {
    const modifiedTime = Date.parse(canonicalInfo.metadata.modified);
    const isFresh = Number.isFinite(modifiedTime)
      ? Date.now() - modifiedTime <= MAP_FRESH_THRESHOLD_MS
      : false;
    if (canonicalInfo.metadata.isValid && isFresh) {
      return canonicalInfo;
    }
  }

  const available = getAvailableMapFiles(sanitized).filter((info) => info.metadata.isValid);

  if (canonicalInfo && canonicalInfo.metadata.isValid && !available.includes(canonicalInfo)) {
    available.unshift(canonicalInfo);
  }

  if (available.length > 0) {
    return available[0];
  }

  // fallback to any json even if invalid
  const anyFiles = getAvailableMapFiles(sanitized);
  return anyFiles.length > 0 ? anyFiles[0] : null;
}

export function ensureMapFileExists(domain: string): boolean {
  return getActiveMapFile(domain) !== null;
}
