import { promises as fs } from 'node:fs';
import path from 'node:path';
import { NextRequest, NextResponse } from 'next/server';

import { uploadJsonSchema } from '@/lib/validations';
import { sanitizeSite } from '@/lib/sites';
import {
  assertSiteAllowed,
  getCanonicalMapFileName,
  getSiteDirectory,
  invalidateSiteSummariesCache
} from '@/lib/sites.server';
import { TokenBucketLimiter } from '@/lib/rate-limit';
import { recordFileUpload, withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const limiter = new TokenBucketLimiter({ capacity: 5, windowMs: 60_000 });
const MAX_FILE_SIZE = 50 * 1024 * 1024;

function getClientIp(request: NextRequest): string {
  const forwarded = request.headers.get('x-forwarded-for');
  if (forwarded) {
    const candidate = forwarded.split(',')[0]?.trim();
    if (candidate) {
      return candidate;
    }
  }
  const real = request.headers.get('x-real-ip');
  return real ?? '127.0.0.1';
}

function sanitizeFileName(name: string): string {
  const base = path.basename(name).replace(/[^a-zA-Z0-9_.-]/g, '_');
  return base || 'map.json';
}

const handler = async (request: NextRequest) => {
  const ip = getClientIp(request);
  if (!limiter.take(ip)) {
    return NextResponse.json({ error: 'Слишком много загрузок, попробуйте позже' }, { status: 429 });
  }

  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    return NextResponse.json({ error: 'Некорректная форма' }, { status: 400 });
  }

  const siteField = formData.get('site');
  const sanitizedSite = sanitizeSite(typeof siteField === 'string' ? siteField : null);
  if (!sanitizedSite) {
    recordFileUpload('unknown', 0, 'failure');
    return NextResponse.json({ error: 'Параметр site обязателен' }, { status: 400 });
  }

  try {
    assertSiteAllowed(sanitizedSite);
  } catch (error) {
    recordFileUpload(sanitizedSite, 0, 'failure');
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Сайт не поддерживается' },
      { status: 404 }
    );
  }

  const fileField = formData.get('file');
  if (!(fileField instanceof Blob)) {
    recordFileUpload(sanitizedSite, 0, 'failure');
    return NextResponse.json({ error: 'Файл JSON обязателен' }, { status: 400 });
  }

  const file = fileField as Blob & { name?: string; type?: string };
  if (file.size > MAX_FILE_SIZE) {
    recordFileUpload(sanitizedSite, file.size, 'failure');
    return NextResponse.json({ error: 'Размер файла превышает 50MB' }, { status: 413 });
  }

  const mime = file.type || 'application/json';
  if (!['application/json', 'text/json', 'application/octet-stream'].includes(mime)) {
    recordFileUpload(sanitizedSite, file.size, 'failure');
    return NextResponse.json({ error: 'Допускаются только JSON файлы' }, { status: 415 });
  }

  const arrayBuffer = await file.arrayBuffer();
  const dataBuffer = Buffer.from(arrayBuffer);

  let payload: unknown;
  try {
    payload = JSON.parse(dataBuffer.toString('utf-8'));
  } catch {
    recordFileUpload(sanitizedSite, dataBuffer.byteLength, 'failure');
    return NextResponse.json({ error: 'Файл содержит некорректный JSON' }, { status: 400 });
  }

  const parsed = uploadJsonSchema.safeParse(payload);
  if (!parsed.success) {
    const issue = parsed.error.issues.at(0);
    recordFileUpload(sanitizedSite, dataBuffer.byteLength, 'failure');
    return NextResponse.json(
      { error: issue?.message ?? 'Некорректный JSON формат' },
      { status: 422 }
    );
  }

  if (parsed.data.domain) {
    const normalizedDomain = sanitizeSite(parsed.data.domain);
    if (!normalizedDomain || normalizedDomain !== sanitizedSite) {
      recordFileUpload(sanitizedSite, dataBuffer.byteLength, 'failure');
      return NextResponse.json({ error: 'Домен карты не совпадает с выбранным site' }, { status: 422 });
    }
  }

  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const originalName = file.name ?? 'map.json';
  const safeName = sanitizeFileName(originalName.endsWith('.json') ? originalName : `${originalName}.json`);
  const finalName = `${timestamp}-${safeName}`;

  const targetDir = getSiteDirectory(sanitizedSite);
  const mapsDir = path.join(targetDir, 'maps');
  await fs.mkdir(mapsDir, { recursive: true });
  await fs.mkdir(targetDir, { recursive: true });
  const historyPath = path.join(mapsDir, finalName);
  await fs.writeFile(historyPath, dataBuffer);

  const canonicalName = getCanonicalMapFileName(sanitizedSite);
  const canonicalPath = path.join(targetDir, canonicalName);
  await fs.copyFile(historyPath, canonicalPath);
  invalidateSiteSummariesCache();
  recordFileUpload(sanitizedSite, dataBuffer.byteLength, 'success');

  return NextResponse.json({
    site: sanitizedSite,
    filename: finalName,
    bytes: dataBuffer.byteLength,
    savedPath: historyPath,
    canonicalPath
  });
};

export const POST = withApiMetrics('upload_map', handler);
