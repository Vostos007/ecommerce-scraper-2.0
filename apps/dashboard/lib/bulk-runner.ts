import archiver from 'archiver';
import { createWriteStream } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';

import { scheduleExport, subscribeToProcess, getProcess, cancelQueuedExport } from './processes';
import { assertSiteAllowed } from './sites.server';
import { getSiteExportPreset } from './export-presets';
import { generateId } from './utils';
import { resolveRepoPath } from './paths';

import type { SpawnOptions } from './processes';

export type BulkRunStatus = 'idle' | 'running' | 'completed' | 'failed';
export type BulkRunSiteStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'error';

export interface BulkRunSiteSnapshot {
  site: string;
  status: BulkRunSiteStatus;
  jobId: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  lastEventAt: string | null;
  queueId: string | null;
  processedUrls: number | null;
  totalUrls: number | null;
  successUrls: number | null;
  failedUrls: number | null;
  progressPercent: number | null;
  estimatedSecondsRemaining: number | null;
  expectedTotalUrls: number | null;
  error: string | null;
}

export interface BulkRunSnapshot {
  id: string;
  status: BulkRunStatus;
  startedAt: string;
  updatedAt: string;
  completedAt: string | null;
  processedUrls: number;
  totalUrls: number;
  progressPercent: number;
  estimatedSecondsRemaining: number | null;
  sites: BulkRunSiteSnapshot[];
  archiveReady: boolean;
  archivePath: string | null;
  archiveSize: number | null;
  archiveError: string | null;
  errors: Array<{ site: string; message: string }>;
}

interface BulkRunSiteState {
  site: string;
  status: BulkRunSiteStatus;
  jobId: string | null;
  queueId: string | null;
  startedAt: Date | null;
  finishedAt: Date | null;
  lastEventAt: Date | null;
  processedUrls: number | null;
  totalUrls: number | null;
  successUrls: number | null;
  failedUrls: number | null;
  progressPercent: number | null;
  estimatedSecondsRemaining: number | null;
  expectedTotalUrls: number | null;
  error: string | null;
  unsubscribe: (() => void) | null;
}

interface BulkRunInternal {
  id: string;
  status: BulkRunStatus;
  startedAt: Date;
  updatedAt: Date;
  completedAt: Date | null;
  sites: Map<string, BulkRunSiteState>;
  pendingSites: string[];
  resume: boolean;
  concurrencyOverrides: Record<string, number | undefined>;
  errors: Array<{ site: string; message: string }>;
  archivePath: string | null;
  archiveSize: number | null;
  archiveError: string | null;
  archiveBuilding: boolean;
}

interface BulkRunSubscriber {
  (snapshot: BulkRunSnapshot): void;
}

const bulkRuns = new Map<string, BulkRunInternal>();
let activeBulkRunId: string | null = null;
let latestBulkRunId: string | null = null;

const subscribers = new Map<string, Set<BulkRunSubscriber>>();

function getOrCreateSubscribers(id: string): Set<BulkRunSubscriber> {
  let set = subscribers.get(id);
  if (!set) {
    set = new Set();
    subscribers.set(id, set);
  }
  return set;
}

function notify(run: BulkRunInternal): void {
  run.updatedAt = new Date();
  const snapshot = buildSnapshot(run);
  const targetSubscribers = subscribers.get(run.id);
  if (targetSubscribers) {
    for (const listener of targetSubscribers) {
      try {
        listener(snapshot);
      } catch (error) {
        console.error('[bulk-runner] subscriber failed', error);
      }
    }
  }
}

function siteStateToSnapshot(state: BulkRunSiteState): BulkRunSiteSnapshot {
  return {
    site: state.site,
    status: state.status,
    jobId: state.jobId,
    startedAt: state.startedAt ? state.startedAt.toISOString() : null,
    finishedAt: state.finishedAt ? state.finishedAt.toISOString() : null,
    lastEventAt: state.lastEventAt ? state.lastEventAt.toISOString() : null,
    queueId: state.queueId,
    processedUrls: state.processedUrls,
    totalUrls: state.totalUrls,
    successUrls: state.successUrls,
    failedUrls: state.failedUrls,
    progressPercent: state.progressPercent,
    estimatedSecondsRemaining: state.estimatedSecondsRemaining,
    expectedTotalUrls: state.expectedTotalUrls,
    error: state.error
  };
}

function buildSnapshot(run: BulkRunInternal): BulkRunSnapshot {
  let processedTotal = 0;
  let totalTotal = 0;
  let remainingSecondsSum = 0;
  let knownEtaSegments = 0;
  let aggregateProgressNumerator = 0;
  let aggregateProgressDenominator = 0;

  const siteSnapshots: BulkRunSiteSnapshot[] = [];

  for (const state of run.sites.values()) {
    const snapshot = siteStateToSnapshot(state);
    siteSnapshots.push(snapshot);

    const processed = snapshot.processedUrls ?? 0;
    const total = snapshot.totalUrls ?? snapshot.expectedTotalUrls ?? 0;
    if (total > 0) {
      processedTotal += Math.min(processed, total);
      totalTotal += total;
      aggregateProgressNumerator += Math.min(processed, total);
      aggregateProgressDenominator += total;
    } else if (snapshot.progressPercent != null) {
      aggregateProgressNumerator += snapshot.progressPercent;
      aggregateProgressDenominator += 100;
    }

    if (snapshot.estimatedSecondsRemaining != null) {
      remainingSecondsSum += snapshot.estimatedSecondsRemaining;
      knownEtaSegments += 1;
    }
  }

  let progressPercent = 0;
  if (aggregateProgressDenominator > 0) {
    progressPercent = Math.round((aggregateProgressNumerator / aggregateProgressDenominator) * 100);
  }

  let estimatedSecondsRemaining: number | null = null;
  if (knownEtaSegments > 0) {
    estimatedSecondsRemaining = Math.max(0, Math.round(remainingSecondsSum / knownEtaSegments));
  }

  let status: BulkRunStatus = run.status;
  if (status === 'running' && run.sites.size === 0) {
    status = 'idle';
  }

  return {
    id: run.id,
    status,
    startedAt: run.startedAt.toISOString(),
    updatedAt: run.updatedAt.toISOString(),
    completedAt: run.completedAt ? run.completedAt.toISOString() : null,
    processedUrls: processedTotal,
    totalUrls: totalTotal,
    progressPercent,
    estimatedSecondsRemaining,
    sites: siteSnapshots,
    archiveReady: Boolean(run.archivePath),
    archivePath: run.archivePath,
    archiveSize: run.archiveSize,
    archiveError: run.archiveError,
    errors: [...run.errors]
  };
}

function attachProcessSubscribers(run: BulkRunInternal, siteState: BulkRunSiteState, jobId: string): void {
  const unsubscribe = subscribeToProcess(
    jobId,
    {
      onLog() {
        const record = getProcess(jobId);
        if (!record) return;
        siteState.processedUrls = record.processedUrls ?? siteState.processedUrls;
        siteState.totalUrls = record.totalUrls ?? siteState.totalUrls;
        siteState.successUrls = record.successUrls ?? siteState.successUrls;
        siteState.failedUrls = record.failedUrls ?? siteState.failedUrls;
        siteState.progressPercent = record.progressPercent ?? siteState.progressPercent;
        siteState.estimatedSecondsRemaining = record.estimatedSecondsRemaining ?? siteState.estimatedSecondsRemaining;
        siteState.lastEventAt = new Date();
        notify(run);
      },
      onClose(info) {
        siteState.finishedAt = new Date();
        siteState.lastEventAt = new Date();
        siteState.unsubscribe?.();
        siteState.unsubscribe = null;

        if (info.code === 0) {
          siteState.status = 'completed';
        } else {
          siteState.status = 'failed';
          siteState.error = info.signal ? `exit with signal ${info.signal}` : `exit code ${info.code}`;
          run.errors.push({ site: siteState.site, message: siteState.error ?? 'job failed' });
        }

        notify(run);
        drainPendingSites(run);
        maybeFinalizeRun(run);
      }
    }
  );

  siteState.unsubscribe = unsubscribe;
}

function tryStartSite(run: BulkRunInternal, site: string): boolean {
  const siteInfo = assertSiteAllowed(site);
  const preset = getSiteExportPreset(siteInfo.domain);
  const concurrencyOverride = run.concurrencyOverrides[siteInfo.domain];
  const spawnOptions: SpawnOptions = {
    concurrency: concurrencyOverride ?? preset.concurrency,
    resume: run.resume
  };

  try {
    const result = scheduleExport(siteInfo.domain, spawnOptions);
    if (result.state === 'started') {
      const state = run.sites.get(siteInfo.domain);
      if (!state) {
        throw new Error('Site state not found');
      }
      state.status = 'running';
      state.jobId = result.job.jobId;
      state.startedAt = new Date(result.job.startedAt);
      state.lastEventAt = new Date();
      state.expectedTotalUrls = preset.typicalUrlCount ?? null;
      attachProcessSubscribers(run, state, result.job.jobId);
      notify(run);
      return true;
    }

    // Если экспорт попал в глобальную очередь — убираем из неё и попробуем позже.
    cancelQueuedExport(result.queued.id);
    const state = run.sites.get(siteInfo.domain);
    if (state) {
      state.status = 'pending';
      state.queueId = null;
      state.jobId = null;
    }
    run.pendingSites.push(siteInfo.domain);
    notify(run);
    return false;
  } catch (error) {
    const state = run.sites.get(siteInfo.domain);
    if (state) {
      state.status = 'error';
      state.error = error instanceof Error ? error.message : 'Не удалось запустить экспорт';
      run.errors.push({ site: state.site, message: state.error });
    }
    notify(run);
    return false;
  }
}

function drainPendingSites(run: BulkRunInternal): void {
  if (run.status !== 'running') {
    return;
  }

  let progressed = false;

  while (run.pendingSites.length > 0) {
    const site = run.pendingSites[0];
    const started = tryStartSite(run, site);
    if (started) {
      run.pendingSites.shift();
      progressed = true;
    } else {
      break;
    }
  }

  if (progressed) {
    notify(run);
  }
}

async function buildArchive(run: BulkRunInternal): Promise<{ path: string; size: number }> {
  const archiveDir = resolveRepoPath('data', 'bulk-runs');
  await fs.mkdir(archiveDir, { recursive: true });
  const archivePath = path.join(archiveDir, `${run.id}.zip`);

  const output = createWriteStream(archivePath);
  const archive = archiver('zip', { zlib: { level: 9 } });
  archive.pipe(output);

  for (const siteState of run.sites.values()) {
    const exportDir = resolveRepoPath('data', 'sites', siteState.site, 'exports');
    const files = ['full.csv', 'seo.csv', 'diff.csv', 'latest.xlsx', `${siteState.site}_latest.xlsx`, 'latest.json'];
    for (const filename of files) {
      const filePath = path.join(exportDir, filename);
      try {
        const stat = await fs.stat(filePath);
        if (stat.isFile()) {
          archive.file(filePath, { name: path.join(siteState.site, filename) });
        }
      } catch {
        // файл отсутствует — пропускаем
      }
    }
  }

  await archive.finalize();

  await new Promise<void>((resolve, reject) => {
    output.on('close', () => resolve());
    archive.on('error', (error) => reject(error));
  });

  const stat = await fs.stat(archivePath);
  return { path: archivePath, size: stat.size };
}

function maybeFinalizeRun(run: BulkRunInternal): void {
  if (run.status !== 'running') {
    return;
  }

  const allCompleted = Array.from(run.sites.values()).every((state) => {
    return state.status === 'completed' || state.status === 'failed' || state.status === 'skipped' || state.status === 'error';
  });

  if (!allCompleted) {
    return;
  }

  const hasFailure = Array.from(run.sites.values()).some((state) => state.status === 'failed' || state.status === 'error');
  run.status = hasFailure ? 'failed' : 'completed';
  run.completedAt = new Date();
  notify(run);

  triggerArchive(run).catch((error) => {
    run.archiveError = error instanceof Error ? error.message : 'Не удалось собрать архив';
    notify(run);
  });

  if (activeBulkRunId === run.id) {
    activeBulkRunId = null;
  }
}

async function triggerArchive(run: BulkRunInternal): Promise<void> {
  if (run.archiveBuilding || run.archivePath) {
    return;
  }
  run.archiveBuilding = true;
  notify(run);
  try {
    const result = await buildArchive(run);
    run.archivePath = result.path;
    run.archiveSize = result.size;
  } catch (error) {
    run.archiveError = error instanceof Error ? error.message : 'Не удалось собрать архив';
  } finally {
    run.archiveBuilding = false;
    notify(run);
  }
}

export interface StartBulkRunParams {
  sites: string[];
  resume: boolean;
  concurrencyOverrides?: Record<string, number>;
}

export interface StartBulkRunResult {
  id: string;
  snapshot: BulkRunSnapshot;
}

export function startBulkRun(params: StartBulkRunParams): StartBulkRunResult {
  if (activeBulkRunId) {
    const active = bulkRuns.get(activeBulkRunId);
    if (active && active.status === 'running') {
      throw new Error('Массовый прогон уже выполняется. Дождитесь завершения текущего запуска.');
    }
  }

  const id = generateId();
  const run: BulkRunInternal = {
    id,
    status: 'running',
    startedAt: new Date(),
    updatedAt: new Date(),
    completedAt: null,
    sites: new Map(),
    pendingSites: [],
    resume: params.resume,
    concurrencyOverrides: params.concurrencyOverrides ?? {},
    errors: [],
    archivePath: null,
    archiveSize: null,
    archiveError: null,
    archiveBuilding: false
  };

  for (const site of params.sites) {
    run.sites.set(site, {
      site,
      status: 'pending',
      jobId: null,
      queueId: null,
      startedAt: null,
      finishedAt: null,
      lastEventAt: null,
      processedUrls: null,
      totalUrls: null,
      successUrls: null,
      failedUrls: null,
      progressPercent: null,
      estimatedSecondsRemaining: null,
      expectedTotalUrls: getSiteExportPreset(site).typicalUrlCount ?? null,
      error: null,
      unsubscribe: null
    });
  }

  bulkRuns.set(id, run);
  latestBulkRunId = id;
  activeBulkRunId = id;

  for (const site of params.sites) {
    const started = tryStartSite(run, site);
    if (!started) {
      // если не удалось стартовать сразу — оставлено в pending, попытаемся позже
      continue;
    }
  }

  notify(run);
  return { id, snapshot: buildSnapshot(run) };
}

export function getBulkRunSnapshot(id: string): BulkRunSnapshot | null {
  const run = bulkRuns.get(id);
  if (!run) {
    return null;
  }
  return buildSnapshot(run);
}

export function getLatestBulkRun(): BulkRunSnapshot | null {
  if (!latestBulkRunId) {
    return null;
  }
  return getBulkRunSnapshot(latestBulkRunId);
}

export function subscribeBulkRun(id: string, listener: BulkRunSubscriber, signal?: AbortSignal): () => void {
  const run = bulkRuns.get(id);
  if (!run) {
    throw new Error('Указанный массовый прогон не найден');
  }
  const set = getOrCreateSubscribers(id);
  set.add(listener);
  listener(buildSnapshot(run));
  const cleanup = () => {
    set.delete(listener);
  };
  if (signal) {
    if (signal.aborted) {
      cleanup();
    } else {
      signal.addEventListener('abort', cleanup, { once: true });
    }
  }
  return cleanup;
}

export async function ensureBulkRunArchive(id: string): Promise<{ path: string; size: number }> {
  const run = bulkRuns.get(id);
  if (!run) {
    throw new Error('Массовый прогон не найден');
  }
  if (run.status !== 'completed') {
    throw new Error('Массовый прогон еще не завершён');
  }
  if (run.archivePath && run.archiveSize != null) {
    return { path: run.archivePath, size: run.archiveSize };
  }
  await triggerArchive(run);
  if (!run.archivePath || run.archiveSize == null) {
    throw new Error(run.archiveError ?? 'Архив не готов');
  }
  return { path: run.archivePath, size: run.archiveSize };
}

export function getActiveBulkRunId(): string | null {
  const runId = activeBulkRunId;
  if (!runId) {
    return null;
  }
  const run = bulkRuns.get(runId);
  if (!run || run.status !== 'running') {
    return null;
  }
  return runId;
}

export function listBulkRuns(): BulkRunSnapshot[] {
  return Array.from(bulkRuns.values())
    .sort((a, b) => b.startedAt.getTime() - a.startedAt.getTime())
    .map((run) => buildSnapshot(run));
}
