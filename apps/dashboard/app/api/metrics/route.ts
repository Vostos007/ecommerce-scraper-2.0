import { NextResponse } from 'next/server';

import { metricsRegistry, updateProcessMetrics } from '@/lib/metrics';

export async function GET() {
  try {
    updateProcessMetrics();
    const payload = await metricsRegistry.metrics();
    return new NextResponse(payload, {
      status: 200,
      headers: {
        'Content-Type': metricsRegistry.contentType,
        'Cache-Control': 'no-store'
      }
    });
  } catch (error) {
    return new NextResponse((error as Error).message, { status: 500 });
  }
}
