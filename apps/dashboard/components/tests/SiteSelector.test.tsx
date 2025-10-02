import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, test } from 'vitest';

import { SiteSelector } from '../SiteSelector';
import type { SiteSummary } from '@/lib/sites';
import { useDashboardStore } from '@/stores/dashboard';

const sites: SiteSummary[] = [
  {
    domain: 'atmospherestore.ru',
    name: 'Atmosphere Store',
    lastExport: '2025-09-27T10:00:00.000Z',
    status: 'ready',
    script: 'atmosphere_fast_export',
    mapFile: 'atmospherestore.ru.URL-map.json',
    mapStatus: 'available',
    mapLastModified: '2025-09-27T09:00:00.000Z',
    mapLinkCount: 1200
  },
  {
    domain: 'knitshop.ru',
    name: 'Knitshop',
    lastExport: null,
    status: 'missing_export',
    script: 'knitshop_fast_export',
    mapFile: null,
    mapStatus: 'missing',
    mapLastModified: null,
    mapLinkCount: null
  }
];

beforeEach(() => {
  useDashboardStore.setState({ activeSite: null });
});

describe('SiteSelector', () => {
  test('рендерит список сайтов', () => {
    render(<SiteSelector sites={sites} />);
    expect(screen.getByText('Atmosphere Store')).toBeInTheDocument();
    expect(screen.getByText('Knitshop')).toBeInTheDocument();
  });

  test('фильтрует сайты по поиску', () => {
    render(<SiteSelector sites={sites} />);
    const input = screen.getByTestId('site-search');
    fireEvent.change(input, { target: { value: 'knit' } });
    expect(screen.queryByText('Atmosphere Store')).not.toBeInTheDocument();
    expect(screen.getByText('Knitshop')).toBeInTheDocument();
  });

  test('устанавливает активный сайт при клике', () => {
    render(<SiteSelector sites={sites} />);
    const card = screen.getByTestId('site-card-atmospherestore.ru');
    fireEvent.click(card);
    expect(useDashboardStore.getState().activeSite).toBe('atmospherestore.ru');
  });
});
