import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { NextRequest } from 'next/server';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

describe('/api/proxy/datacenter', () => {
  let repoRoot: string;

  beforeEach(async () => {
    repoRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'proxy-admin-'));

    const configDir = path.join(repoRoot, 'config');
    const proxyDir = path.join(configDir, 'proxy');
    await fs.mkdir(proxyDir, { recursive: true });

    await fs.writeFile(
      path.join(proxyDir, 'proxy_pools.yml'),
      [
        'datacenter:',
        '  rotation: success_weighted',
        '  list:',
        '    - "https://user:pass@198.51.100.10:8000"',
        '    - "socks5://user:pass@198.51.100.11:9000"',
        'residential:',
        '  provider: none'
      ].join('\n'),
      'utf-8'
    );

    await fs.writeFile(
      path.join(configDir, 'manual_proxies.txt'),
      ['https://user:pass@198.51.100.10:8000', 'socks5://user:pass@198.51.100.11:9000'].join('\n'),
      'utf-8'
    );
    await fs.writeFile(
      path.join(configDir, 'proxies_https.txt'),
      ['198.51.100.10:8000:user:pass'].join('\n'),
      'utf-8'
    );

    vi.resetModules();
    vi.doMock('@/lib/paths', async () => {
      const actual = await vi.importActual<typeof import('@/lib/paths')>('@/lib/paths');
      return {
        ...actual,
        resolveRepoPath: (...segments: string[]) => path.resolve(repoRoot, ...segments),
        getProjectRoot: () => repoRoot,
        validatePath: () => true
      };
    });
  });

  afterEach(async () => {
    vi.resetModules();
    await fs.rm(repoRoot, { recursive: true, force: true });
  });

  it('lists, adds and removes proxies while updating config files', async () => {
    const { GET, POST, DELETE } = await import('./route');

    // initial list
    const listResponse = await GET(new NextRequest('http://localhost/api/proxy/datacenter'));
    expect(listResponse.status).toBe(200);
    const listPayload = await listResponse.json();
    expect(Array.isArray(listPayload.proxies)).toBe(true);
    expect(listPayload.proxies).toHaveLength(2);
    expect(listPayload.total).toBe(2);

    // add new proxy
    const addRequest = new NextRequest('http://localhost/api/proxy/datacenter', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        proxies: ['https://new:secret@203.0.113.50:7000']
      })
    });
    const addResponse = await POST(addRequest);
    const addPayload = await addResponse.json();
    expect(addResponse.status).toBe(200);
    expect(addPayload.added).toHaveLength(1);
    expect(addPayload.total).toBe(3);

    const poolAfterAdd = await fs.readFile(path.join(repoRoot, 'config', 'proxy', 'proxy_pools.yml'), 'utf-8');
    expect(poolAfterAdd).toContain('203.0.113.50:7000');

    const manualAfterAdd = await fs.readFile(path.join(repoRoot, 'config', 'manual_proxies.txt'), 'utf-8');
    expect(manualAfterAdd).toContain('https://new:secret@203.0.113.50:7000');

    const httpsAfterAdd = await fs.readFile(path.join(repoRoot, 'config', 'proxies_https.txt'), 'utf-8');
    expect(httpsAfterAdd).toContain('203.0.113.50:7000:new:secret');

    // remove proxy
    const deleteRequest = new NextRequest('http://localhost/api/proxy/datacenter', {
      method: 'DELETE',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        proxies: ['https://new:secret@203.0.113.50:7000']
      })
    });
    const deleteResponse = await DELETE(deleteRequest);
    const deletePayload = await deleteResponse.json();
    expect(deleteResponse.status).toBe(200);
    expect(deletePayload.removed).toHaveLength(1);
    expect(deletePayload.total).toBe(2);

    const manualAfterDelete = await fs.readFile(path.join(repoRoot, 'config', 'manual_proxies.txt'), 'utf-8');
    expect(manualAfterDelete).not.toContain('203.0.113.50:7000');
  });
});
