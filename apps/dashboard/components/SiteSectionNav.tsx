'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

import { cn } from '@/lib/utils';

export interface SiteSectionDefinition {
  id: string;
  label: string;
  target: string; // hash selector e.g. #export
}

interface SiteSectionNavProps {
  sections: SiteSectionDefinition[];
}

interface Metrics {
  topOffset: number;
}

export function SiteSectionNav({ sections }: SiteSectionNavProps) {
  const [active, setActive] = useState<string>(sections[0]?.target ?? '');
  const [isPinned, setIsPinned] = useState(false);
  const [navHeight, setNavHeight] = useState(0);
  const [stickyOffset, setStickyOffset] = useState(64);
  const navRef = useRef<HTMLDivElement | null>(null);
  const metricsRef = useRef<Metrics>({ topOffset: 64 });

  useEffect(() => {
    if (sections.length === 0 || typeof window === 'undefined') {
      return;
    }

    const topNavSelector = '[data-top-nav]';

    const updateMetrics = () => {
      const navElement = navRef.current;
      const topNavElement = document.querySelector<HTMLElement>(topNavSelector);

      if (navElement) {
        const { height } = navElement.getBoundingClientRect();
        setNavHeight(height);
      }

      const topOffset = Math.round((topNavElement?.getBoundingClientRect().height ?? 56) + 16);
      metricsRef.current.topOffset = topOffset;
      setStickyOffset(topOffset);
    };

    updateMetrics();

    const resizeObserver =
      typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => updateMetrics()) : null;
    if (resizeObserver) {
      if (navRef.current) {
        resizeObserver.observe(navRef.current);
      }
      const topNavElement = document.querySelector<HTMLElement>(topNavSelector);
      if (topNavElement) {
        resizeObserver.observe(topNavElement);
      }
    }

    const sectionElements = sections
      .map((section) => document.querySelector<HTMLElement>(section.target))
      .filter((element): element is HTMLElement => Boolean(element));

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio);
        if (visible[0]) {
          const matching = sections.find((section) => section.target === `#${visible[0]!.target.id}`);
          if (matching) {
            setActive(matching.target);
          }
        }
      },
      {
        root: null,
        rootMargin: '-140px 0px -60%',
        threshold: [0.1, 0.3, 0.6]
      }
    );

    sectionElements.forEach((element) => observer.observe(element));

    const initialHash = window.location.hash;
    if (initialHash) {
      const exists = sections.some((section) => section.target === initialHash);
      if (exists) {
        setActive(initialHash);
      }
    }

    const handleScroll = () => {
      const threshold = metricsRef.current.topOffset + 4;
      setIsPinned(window.scrollY > threshold);
    };

    handleScroll();
    window.addEventListener('scroll', handleScroll, { passive: true });
    window.addEventListener('resize', updateMetrics);

    return () => {
      sectionElements.forEach((element) => observer.unobserve(element));
      observer.disconnect();
      window.removeEventListener('scroll', handleScroll);
      window.removeEventListener('resize', updateMetrics);
      resizeObserver?.disconnect();
    };
  }, [sections]);

  const handleNavigate = (target: string) => {
    if (typeof window === 'undefined') {
      return;
    }
    const element = document.querySelector<HTMLElement>(target);
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    setActive(target);
  };

  const items = useMemo(() => sections, [sections]);

  if (items.length === 0) {
    return null;
  }

  return (
    <div style={navHeight ? { height: `${navHeight}px` } : undefined}>
      <div
        ref={navRef}
        data-site-sections-nav
        className={cn(
          'sticky z-20 overflow-x-auto rounded-lg border border-border bg-card/40 px-2 py-2 backdrop-blur supports-[backdrop-filter]:bg-card/60 transition-shadow',
          isPinned ? 'shadow-lg' : 'shadow-none'
        )}
        style={{ top: `${stickyOffset}px` }}
      >
        <div className="flex items-center gap-2">
          {items.map((section) => {
            const isActive = active === section.target;
            return (
              <button
                key={section.id}
                type="button"
                onClick={() => handleNavigate(section.target)}
                className={cn(
                  'whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition-colors hover:bg-secondary/40 hover:text-foreground',
                  isActive ? 'bg-secondary/40 text-foreground' : 'text-muted-foreground'
                )}
              >
                {section.label}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
