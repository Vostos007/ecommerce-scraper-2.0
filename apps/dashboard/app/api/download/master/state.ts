import { statSync } from 'node:fs';

import { resolveRepoPath } from '@/lib/paths';

export type MasterGenerationStatus = 'generating' | 'ready' | 'error';
export const WORKBOOK_CACHE_TTL_MS = 5 * 60 * 1000;

interface MasterState {
  status: MasterGenerationStatus;
  generatedAt: string | null;
  fileSize: number | null;
  errorMessage: string | null;
  lastChecked: number | null;
  phase: string | null;
}

let generationPromise: Promise<void> | null = null;

const workbookPath = resolveRepoPath('data', 'sites', '_compiled', 'history_wide.xlsx');

function detectInitialState(): MasterState {
  try {
    const stat = statSync(workbookPath);
    return {
      status: 'ready',
      generatedAt: stat.mtime.toISOString(),
      fileSize: stat.size,
      errorMessage: null,
      lastChecked: Date.now(),
      phase: null
    };
  } catch {
    return {
      status: 'error',
      generatedAt: null,
      fileSize: null,
      errorMessage: 'Сводный отчет ещё не создавался',
      lastChecked: null,
      phase: null
    };
  }
}

const state: MasterState = detectInitialState();

export function getWorkbookPath(): string {
  return workbookPath;
}

export function getMasterState(): MasterState {
  return { ...state };
}

export function setGenerationPromise(promise: Promise<void> | null): void {
  generationPromise = promise;
}

export function getGenerationPromise(): Promise<void> | null {
  return generationPromise;
}

export function markGenerating(phase: string | null = 'build_master_workbook'): void {
  state.status = 'generating';
  state.errorMessage = null;
  state.lastChecked = Date.now();
  state.phase = phase;
}

export function markReady(fileSize: number, generatedAt: string): void {
  state.status = 'ready';
  state.fileSize = fileSize;
  state.generatedAt = generatedAt;
  state.errorMessage = null;
  state.lastChecked = Date.now();
  state.phase = null;
}

export function markError(message: string): void {
  state.status = 'error';
  state.errorMessage = message;
  state.lastChecked = Date.now();
  state.phase = null;
}

export function updatePhase(phase: string | null): void {
  state.phase = phase;
  state.lastChecked = Date.now();
}
