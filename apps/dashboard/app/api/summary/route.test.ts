import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { NextRequest } from 'next/server';

async function fileExists(target: string): Promise<boolean> {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

describe('GET /api/summary', () => {
  const __filename = fileURLToPath(import.meta.url);
  const __dirname = path.dirname(__filename);
  const repoRoot = path.resolve(__dirname, '../../../../../');
  const summaryPath = path.resolve(repoRoot, 'reports', 'firecrawl_baseline_summary.json');
  let originalContents: string | null = null;
  let hadOriginal = false;

  beforeEach(async () => {
    vi.resetModules();
    vi.doMock('@/lib/paths', async () => {
      const actual = await vi.importActual<typeof import('@/lib/paths')>('@/lib/paths');
      return {
        ...actual,
        resolveRepoPath: (...segments: string[]) => path.resolve(repoRoot, ...segments),
        getProjectRoot: () => repoRoot
      };
    });

    try {
      originalContents = await fs.readFile(summaryPath, 'utf-8');
      hadOriginal = true;
    } catch {
      originalContents = null;
      hadOriginal = false;
    }
    await fs.unlink(summaryPath).catch(() => {});
  });

  afterEach(async () => {
    vi.resetModules();
    if (hadOriginal && originalContents !== null) {
      await fs.writeFile(summaryPath, originalContents, 'utf-8');
    } else {
      await fs.unlink(summaryPath).catch(() => {});
    }
  });

  it('rebuilds summary from site exports when file is missing', async () => {
    const { GET } = await import('@/app/api/summary/route');

    const request = new NextRequest('http://localhost/api/summary');
    const response = await GET(request);

    expect(response.status).toBe(200);
    const payload = await response.json();

    expect(typeof payload.generated_at).toBe('string');
    expect(payload.totals.total_sites).toBeGreaterThan(0);
    expect(Object.keys(payload.sites).length).toBe(payload.totals.total_sites);
    expect(payload.sites['atmospherestore.ru']).toBeDefined();
    expect(payload.sites['atmospherestore.ru'].status).toBeTypeOf('string');

    expect(await fileExists(summaryPath)).toBe(true);
  });
});
