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
  const sitesPath = path.resolve(repoRoot, 'config', 'sites.json');
  const customDomain = 'electro-test.ru';
  let originalContents: string | null = null;
  let hadOriginal = false;
  let originalSites: string | null = null;

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

    try {
      originalSites = await fs.readFile(sitesPath, 'utf-8');
    } catch {
      originalSites = null;
    }
  });

  afterEach(async () => {
    vi.resetModules();
    if (hadOriginal && originalContents !== null) {
      await fs.writeFile(summaryPath, originalContents, 'utf-8');
    } else {
      await fs.unlink(summaryPath).catch(() => {});
    }
    if (originalSites !== null) {
      await fs.writeFile(sitesPath, originalSites, 'utf-8');
    }
    await fs.rm(path.resolve(repoRoot, 'data', 'sites', customDomain), {
      recursive: true,
      force: true
    });
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

  it('omits electronic or bulk inventory products from aggregates', async () => {
    const sitesRaw = originalSites ?? (await fs.readFile(sitesPath, 'utf-8'));
    const sitesConfig = JSON.parse(sitesRaw) as { sites?: Array<Record<string, unknown>> };
    const sitesList = Array.isArray(sitesConfig.sites) ? [...sitesConfig.sites] : [];
    sitesList.push({
      name: 'Electro Test',
      domain: customDomain,
      script: 'electro-test'
    });
    await fs.writeFile(
      sitesPath,
      JSON.stringify({ ...sitesConfig, sites: sitesList }, null, 2),
      'utf-8'
    );

    const exportDir = path.resolve(repoRoot, 'data', 'sites', customDomain, 'exports');
    await fs.mkdir(exportDir, { recursive: true });
    const exportPayload = {
      generated_at: '2025-10-02T12:00:00Z',
      products: [
        {
          name: 'Набор спиц деревянных',
          price: 1200,
          stock: 42,
          in_stock: true
        },
        {
          name: 'Электронный подарочный сертификат',
          description: 'Электронный сертификат, высылается на email',
          price: 3000,
          stock: 25000,
          in_stock: true
        },
        {
          name: 'Exclusive digital pattern',
          price: 900,
          variations: [
            {
              name: 'PDF версия',
              stock_quantity: 15000
            }
          ]
        }
      ]
    };
    await fs.writeFile(path.resolve(exportDir, 'latest.json'), JSON.stringify(exportPayload, null, 2), 'utf-8');

    const { GET } = await import('@/app/api/summary/route');
    const request = new NextRequest('http://localhost/api/summary');
    const response = await GET(request);

    expect(response.status).toBe(200);
    const payload = await response.json();
    const domainSummary = payload.sites[customDomain];
    expect(domainSummary).toBeDefined();
    expect(domainSummary.products).toBe(1);
    expect(domainSummary.products_total_stock).toBe(42);
    expect(domainSummary.products_with_price).toBe(1);
    expect(domainSummary.total_variations).toBe(0);
  });
});
