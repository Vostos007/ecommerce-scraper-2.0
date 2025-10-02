import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { NextRequest } from 'next/server';

describe('GET /api/proxy/stats', () => {
  const __filename = fileURLToPath(import.meta.url);
  const __dirname = path.dirname(__filename);
  const repoRoot = path.resolve(__dirname, '../../../../../../');

  beforeEach(() => {
    vi.resetModules();
    vi.doMock('@/lib/paths', async () => {
      const actual = await vi.importActual<typeof import('@/lib/paths')>('@/lib/paths');
      return {
        ...actual,
        resolveRepoPath: (...segments: string[]) => path.resolve(repoRoot, ...segments),
        getProjectRoot: () => repoRoot
      };
    });
  });

  afterEach(() => {
    vi.resetModules();
  });

  it('returns aggregated proxy metrics based on repository data', async () => {
    const { GET } = await import('@/app/api/proxy/stats/route');

    const response = await GET(new NextRequest('http://localhost/api/proxy/stats'));
    expect(response.status).toBe(200);

    const payload = await response.json();
    expect(payload.total_proxies).toBeGreaterThan(0);
    expect(payload.success_rate).toBeGreaterThan(0);
    expect(Array.isArray(payload.top_performing_proxies)).toBe(true);
    expect(payload.top_performing_proxies.length).toBeGreaterThan(0);
    expect(Object.keys(payload.proxy_countries ?? {})).not.toHaveLength(0);
  });

  it('serves cached data on subsequent requests within TTL', async () => {
    const { GET } = await import('@/app/api/proxy/stats/route');

    const firstResponse = await GET(new NextRequest('http://localhost/api/proxy/stats'));
    const firstPayload = await firstResponse.json();

    const cachedResponse = await GET(new NextRequest('http://localhost/api/proxy/stats'));
    const cachedPayload = await cachedResponse.json();

    expect(cachedResponse.status).toBe(200);
    expect(cachedPayload.generated_at).toEqual(firstPayload.generated_at);
    expect(cachedPayload.total_requests).toEqual(firstPayload.total_requests);
  });
});
