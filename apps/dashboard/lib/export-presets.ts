export interface SiteExportPreset {
  concurrency: number;
  estimatedDuration: string;
  estimatedMinutes: number;
  urlsPerMinute?: number;
  typicalUrlCount?: number;
  notes?: string;
}

const DEFAULT_PRESET: SiteExportPreset = {
  concurrency: 8,
  estimatedDuration: '≈45 минут',
  estimatedMinutes: 45,
  urlsPerMinute: 240,
  typicalUrlCount: 1000
};

const PRESETS: Record<string, SiteExportPreset> = {
  'atmospherestore.ru': {
    concurrency: 64,
    estimatedDuration: '≈45 минут',
    estimatedMinutes: 45,
    typicalUrlCount: 970
  },
  'sittingknitting.ru': {
    concurrency: 48,
    estimatedDuration: '≈35 минут',
    estimatedMinutes: 35,
    typicalUrlCount: 680
  },
  'mpyarn.ru': {
    concurrency: 48,
    estimatedDuration: '≈40 минут',
    estimatedMinutes: 40,
    typicalUrlCount: 900
  },
  'ili-ili.com': {
    concurrency: 48,
    estimatedDuration: '≈50 минут',
    estimatedMinutes: 50,
    typicalUrlCount: 5600
  },
  'knitshop.ru': {
    concurrency: 2,
    estimatedDuration: '≈20 минут',
    estimatedMinutes: 20,
    typicalUrlCount: 450,
    notes: 'Сайт чувствителен к нагрузке'
  },
  'triskeli.ru': {
    concurrency: 4,
    estimatedDuration: '≈30 минут',
    estimatedMinutes: 30,
    typicalUrlCount: 1200
  },
  '6wool.ru': {
    concurrency: 3,
    estimatedDuration: '≈25 минут',
    estimatedMinutes: 25,
    typicalUrlCount: 800,
    notes: 'Возможны антибот проверки'
  }
};

export function getSiteExportPreset(site: string): SiteExportPreset {
  return PRESETS[site] ?? DEFAULT_PRESET;
}

export function getAllPresetSites(): string[] {
  return Object.keys(PRESETS);
}
