import { sanitizeString } from './utils';

export type SiteStatus = 'ready' | 'missing_export' | 'unknown' | 'missing_map';

export type MapStatus = 'available' | 'missing' | 'outdated';

export interface SiteConfig {
  name: string;
  domain: string;
  backend?: string;
  [key: string]: unknown;
}

export interface SiteSummary {
  domain: string;
  name: string;
  lastExport: string | null;
  status: SiteStatus;
  script: string;
  mapFile: string | null;
  mapStatus: MapStatus;
  mapLastModified: string | null;
  mapLinkCount: number | null;
}

export type SiteSummaryList = SiteSummary[];

export function sanitizeSite(input: unknown): string | null {
  if (typeof input !== 'string') {
    return null;
  }
  const value = input.trim().toLowerCase();
  if (!value || value.includes('..') || value.includes('/') || value.includes('\\')) {
    return null;
  }
  const withoutWww = value.startsWith('www.') ? value.slice(4) : value;
  const sanitized = sanitizeString(withoutWww, 255);
  return sanitized.toLowerCase();
}

export function isSiteSummary(value: unknown): value is SiteSummary {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const record = value as Partial<SiteSummary>;
  return (
    typeof record.domain === 'string' &&
    typeof record.name === 'string' &&
    (record.lastExport === null || typeof record.lastExport === 'string') &&
    (record.status === 'ready' ||
      record.status === 'missing_export' ||
      record.status === 'unknown' ||
      record.status === 'missing_map') &&
    typeof record.script === 'string' &&
    (record.mapFile === null || typeof record.mapFile === 'string') &&
    (record.mapStatus === 'available' || record.mapStatus === 'missing' || record.mapStatus === 'outdated') &&
    (record.mapLastModified === null || typeof record.mapLastModified === 'string') &&
    (record.mapLinkCount === null || typeof record.mapLinkCount === 'number')
  );
}
