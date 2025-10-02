'use client';

import { create } from 'zustand';

interface DashboardState {
  activeSite: string | null;
  setActiveSite: (site: string | null) => void;
}

export const useDashboardStore = create<DashboardState>((set, get) => ({
  activeSite: null,
  setActiveSite: (site) => {
    if (get().activeSite === site) {
      return;
    }
    set({ activeSite: site });
  }
}));
