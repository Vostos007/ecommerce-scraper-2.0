import { getSiteExportPreset } from './export-presets';

export type EstimateSource = 'map' | 'preset' | 'fallback';

export interface ExportEstimate {
  durationSeconds: number | null;
  durationLabel: string;
  urlCount: number | null;
  urlCountLabel: string | null;
  source: EstimateSource;
}

const DEFAULT_URLS_PER_MINUTE = 240;
const numberFormatter = new Intl.NumberFormat('ru-RU');

function clampPositiveInteger(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    return Math.round(value);
  }
  return null;
}

function resolveRatePerMinute(site: string, fallbackUrls: number | null): number {
  const preset = getSiteExportPreset(site);
  if (preset.urlsPerMinute && preset.urlsPerMinute > 0) {
    return preset.urlsPerMinute;
  }
  if (preset.estimatedMinutes && preset.estimatedMinutes > 0 && fallbackUrls && fallbackUrls > 0) {
    return fallbackUrls / preset.estimatedMinutes;
  }
  return DEFAULT_URLS_PER_MINUTE;
}

export function formatDurationExact(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return '0 с';
  }
  if (seconds < 60) {
    return `${Math.ceil(seconds)} с`;
  }
  if (seconds < 3600) {
    const minutes = Math.ceil(seconds / 60);
    return `${minutes} мин`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.ceil((seconds % 3600) / 60);
  if (minutes === 0) {
    return `${hours} ч`;
  }
  return `${hours} ч ${minutes} мин`;
}

export function formatDurationApprox(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return '≈1 минута';
  }
  if (seconds < 60) {
    return `≈${Math.max(1, Math.round(seconds))} с`;
  }
  const totalMinutes = Math.max(1, Math.round(seconds / 60));
  if (totalMinutes < 60) {
    return `≈${totalMinutes} минут`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (minutes === 0) {
    return `≈${hours} ч`;
  }
  return `≈${hours} ч ${minutes} мин`;
}

export function estimateExportDuration(site: string, urlCountInput?: number | null): ExportEstimate {
  const preset = getSiteExportPreset(site);
  const explicit = clampPositiveInteger(urlCountInput);
  const fallback = clampPositiveInteger(preset.typicalUrlCount);
  const effectiveUrlCount = explicit ?? fallback;

  const source: EstimateSource = explicit !== null ? 'map' : fallback !== null ? 'preset' : 'fallback';

  let durationSeconds: number | null = null;
  if (effectiveUrlCount !== null) {
    const rate = resolveRatePerMinute(site, fallback);
    if (rate > 0) {
      durationSeconds = Math.round((effectiveUrlCount / rate) * 60);
    }
  }

  if (durationSeconds === null && preset.estimatedMinutes && preset.estimatedMinutes > 0) {
    durationSeconds = Math.round(preset.estimatedMinutes * 60);
  }

  const durationLabel = durationSeconds !== null ? formatDurationApprox(durationSeconds) : preset.estimatedDuration;
  const urlCountLabel = effectiveUrlCount !== null ? numberFormatter.format(effectiveUrlCount) : null;

  return {
    durationSeconds,
    durationLabel,
    urlCount: effectiveUrlCount,
    urlCountLabel,
    source
  };
}

export function formatUrlCount(count: number | null): string | null {
  if (count === null || !Number.isFinite(count)) {
    return null;
  }
  return numberFormatter.format(Math.max(0, Math.round(count)));
}

