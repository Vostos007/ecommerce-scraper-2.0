'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useMemo, useState, type ChangeEvent } from 'react';

import { useQuery } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/hooks/useAuth';
import { getSites } from '@/lib/api';
import type { SiteSummary } from '@/lib/sites';
import { useDashboardStore } from '@/stores/dashboard';
import { Select } from './ui/select';
import { cn } from '@/lib/utils';

const PLACEHOLDER_OPTION = { value: '', label: 'Выберите сайт', disabled: true };

function UserAvatar({ username }: { username: string }) {
  const initials = useMemo(() => username.slice(0, 2).toUpperCase(), [username]);
  return (
    <div className="flex h-9 w-9 items-center justify-center rounded-full bg-primary/10 text-sm font-semibold text-primary">
      {initials}
    </div>
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  if (!user) {
    return null;
  }

  const handleLogout = async () => {
    if (!window.confirm('Вы уверены, что хотите выйти из системы?')) {
      return;
    }
    await logout();
    router.replace('/login');
  };

  return (
    <details className="relative" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="flex cursor-pointer list-none items-center gap-3 rounded-full border border-transparent px-2 py-1 text-left transition hover:border-border">
        <UserAvatar username={user.username} />
        <div className="hidden text-left sm:block">
          <p className="text-sm font-medium leading-tight">{user.username}</p>
          <Badge variant="outline" className="mt-0.5 text-xs uppercase tracking-wide">
            {user.role}
          </Badge>
        </div>
      </summary>
      <div className="absolute right-0 mt-2 w-56 space-y-1 rounded-md border border-border bg-popover p-2 shadow-lg">
        <div className="rounded-md bg-muted/40 p-2 text-xs text-muted-foreground">
          Последний вход: {user.lastLogin ? new Date(user.lastLogin).toLocaleString() : '—'}
        </div>
        <Button variant="ghost" size="sm" className="w-full justify-start" disabled>
          Профиль (скоро)
        </Button>
        <Button variant="ghost" size="sm" className="w-full justify-start" disabled>
          Настройки (скоро)
        </Button>
        {user.role === 'admin' ? (
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-start"
            onClick={() => router.push('/dashboard/users')}
          >
            Управление пользователями
          </Button>
        ) : null}
        <Button
          variant="destructive"
          size="sm"
          className="w-full justify-start"
          onClick={handleLogout}
        >
          Выйти
        </Button>
      </div>
    </details>
  );
}

export function TopNav() {
  const router = useRouter();
  const pathname = usePathname();
  const { data: sites = [], isLoading, isError } = useQuery<SiteSummary[]>({
    queryKey: ['sites'],
    queryFn: getSites,
    staleTime: 5 * 60 * 1000
  });

  const { activeSite, setActiveSite } = useDashboardStore();
  const { isAuthenticated, isLoading: authLoading } = useAuth();

  useEffect(() => {
    if (!activeSite && sites.length > 0) {
      setActiveSite(sites[0]!.domain);
    }
  }, [activeSite, setActiveSite, sites]);

  const options = useMemo(() => {
    const siteOptions = sites.map((site) => ({
      value: site.domain,
      label: site.name
    }));

    const placeholderLabel = isError ? 'Ошибка загрузки' : isLoading ? 'Загрузка...' : PLACEHOLDER_OPTION.label;
    return [
      { ...PLACEHOLDER_OPTION, label: placeholderLabel },
      ...siteOptions
    ];
  }, [isError, isLoading, sites]);

  const normalizedPath = useMemo(() => {
    if (!pathname) {
      return '/';
    }
    return pathname.replace(/\/$/, '') || '/';
  }, [pathname]);

  const activeSiteEntry = useMemo(
    () => sites.find((site) => site.domain === activeSite) ?? null,
    [activeSite, sites]
  );

  const navItems = useMemo(() => {
    const baseItems = [
      {
        label: 'Обзор',
        href: '/dashboard',
        isActive: (path: string) => path === '/dashboard'
      },
      {
        label: 'Загрузки',
        href: '/dashboard/downloads',
        isActive: (path: string) => path.startsWith('/dashboard/downloads')
      },
      {
        label: 'Прокси',
        href: '/dashboard/proxies',
        isActive: (path: string) => path.startsWith('/dashboard/proxies')
      }
    ];

    if (activeSite) {
      const siteLabel = activeSiteEntry?.name ?? activeSite;
      baseItems.push({
        label: `Сайт: ${siteLabel}`,
        href: `/dashboard/${activeSite}`,
        isActive: (path: string) =>
          path.startsWith(`/dashboard/${activeSite}`) && !path.startsWith('/dashboard/downloads') && !path.startsWith('/dashboard/proxies')
      });
    }

    return baseItems;
  }, [activeSite, activeSiteEntry]);

  const handleChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const nextSite = event.target.value;
    if (!nextSite || nextSite === activeSite) {
      return;
    }
    setActiveSite(nextSite);
    router.push(`/dashboard/${nextSite}`);
  };

  if (authLoading || !isAuthenticated) {
    return null;
  }

  return (
    <header
      data-top-nav
      className="sticky top-0 z-30 border-b border-border bg-background/90 backdrop-blur"
    >
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center justify-between gap-6 px-4 sm:px-6">
        <div className="flex items-center gap-6">
          <Link href="/dashboard" className="text-sm font-semibold tracking-wide text-muted-foreground">
            UI Dashboard
          </Link>
          <nav className="hidden items-center gap-2 md:flex">
            {navItems.map((item) => {
              const isActive = item.isActive(normalizedPath);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={cn(
                    'rounded-md px-3 py-1.5 text-sm transition-colors hover:bg-secondary/40 hover:text-foreground',
                    isActive ? 'bg-secondary/40 text-foreground' : 'text-muted-foreground'
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>
        <div className="flex items-center gap-3">
          <Select
            aria-label="Сайт"
            value={activeSite ?? ''}
            onChange={handleChange}
            options={options}
            disabled={isLoading || sites.length === 0 || isError}
            variant="ghost"
            size="sm"
            className="w-60"
          />
          <UserMenu />
        </div>
      </div>
      <nav className="md:hidden">
        <div className="mx-auto flex w-full max-w-6xl items-center gap-2 overflow-x-auto px-4 pb-2 pt-1 sm:px-6">
          {navItems.map((item) => {
            const isActive = item.isActive(normalizedPath);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  'whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition-colors hover:bg-secondary/40 hover:text-foreground',
                  isActive ? 'bg-secondary/40 text-foreground' : 'text-muted-foreground'
                )}
              >
                {item.label}
              </Link>
            );
          })}
        </div>
      </nav>
    </header>
  );
}
