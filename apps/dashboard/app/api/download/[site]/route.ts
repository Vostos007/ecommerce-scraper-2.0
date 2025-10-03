import fs from 'node:fs';
import { NextRequest, NextResponse } from 'next/server';

import { sanitizeSite } from '@/lib/sites';
import { assertSiteAllowed, getCsvExportPath, getExportPath } from '@/lib/sites.server';
import { recordFileDownload, withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const CSV_DEFAULT_SHEET = 'full';

const handler = async (request: NextRequest, context: { params: { site: string } }) => {
  const sanitized = sanitizeSite(context.params.site);
  if (!sanitized) {
    return NextResponse.json({ status: 'invalid_site' }, { status: 400 });
  }

  try {
    assertSiteAllowed(sanitized);
  } catch (error) {
    return NextResponse.json(
      { status: 'not_allowed', error: error instanceof Error ? error.message : 'site not allowed' },
      { status: 404 }
    );
  }

  const searchParams = request.nextUrl.searchParams;
  const format = (searchParams.get('format') ?? 'xlsx').toLowerCase();

  if (format === 'csv') {
    const sheet = (searchParams.get('sheet') ?? CSV_DEFAULT_SHEET).toLowerCase();
    const targetPath = getCsvExportPath(sanitized, sheet);
    if (!targetPath) {
      return NextResponse.json({ status: 'not_found' }, { status: 404 });
    }

    try {
      const fileBuffer = await fs.promises.readFile(targetPath);
      const stat = await fs.promises.stat(targetPath);
      recordFileDownload(sanitized, stat.size, 'export_csv');
      return new Response(fileBuffer as unknown as BodyInit, {
        headers: {
          'Content-Type': 'text/csv; charset=utf-8',
          'Content-Length': stat.size.toString(),
          'Content-Disposition': `attachment; filename="${sanitized}-${sheet}.csv"`
        }
      });
    } catch (error) {
      return NextResponse.json(
        { status: 'error', error: error instanceof Error ? error.message : 'unknown error' },
        { status: 500 }
      );
    }
  }

  if (format !== 'xlsx') {
    return NextResponse.json({ status: 'invalid_format' }, { status: 400 });
  }

  try {
    const targetPath = getExportPath(sanitized);
    const fileBuffer = await fs.promises.readFile(targetPath);
    const stat = await fs.promises.stat(targetPath);
    recordFileDownload(sanitized, stat.size, 'export');
    return new Response(fileBuffer as unknown as BodyInit, {
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Length': stat.size.toString(),
        'Content-Disposition': `attachment; filename="${sanitized}-latest.xlsx"`
      }
    });
  } catch (error) {
    if ((error as Error).message?.includes('Export file not found')) {
      return NextResponse.json({ status: 'not_found' }, { status: 404 });
    }
    return NextResponse.json(
      { status: 'error', error: error instanceof Error ? error.message : 'unknown error' },
      { status: 500 }
    );
  }
};

export const GET = withApiMetrics('download_site', handler);
