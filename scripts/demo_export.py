"""Demo export workflow used inside the docker training image.

The real project запускает тяжёлые Python скрипты. Здесь мы имитируем
работу: читаем аргументы, выгружаем фиктивные URL и периодически
отправляем события прогресса в stdout. Эти события перехватываются
Next.js приложением и отображаются в UI.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from typing import Iterable

PROGRESS_EVENT = "progress"
COMPLETE_EVENT = "complete"
DEFAULT_TOTAL = 50
MIN_DELAY = 0.05
MAX_DELAY = 0.2


def _emit(event: str, **payload: object) -> None:
    message = {"event": event, **payload, "timestamp": time.time()}
    json.dump(message, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _iterate_urls(total: int) -> Iterable[int]:
    for index in range(1, total + 1):
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        yield index


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Demo exporter placeholder")
    parser.add_argument("--limit", type=int, default=None, help="Максимальное количество URL")
    parser.add_argument("--concurrency", type=int, default=None, help="Имитируемый уровень конкуренции")
    parser.add_argument("--resume", action="store_true", help="Пропустить до последнего обработанного элемента")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Явно отключить resume")
    parser.set_defaults(resume=True)
    return parser.parse_args(argv)


def run_demo_export(site: str, *, default_total: int = DEFAULT_TOTAL) -> None:
    args = _parse_args()
    total = args.limit if args.limit and args.limit > 0 else default_total

    _emit(
        PROGRESS_EVENT,
        site=site,
        processed=0,
        total=total,
        resume=args.resume,
        concurrency=args.concurrency,
    )

    processed = 0
    for processed in _iterate_urls(total):
        _emit(
            PROGRESS_EVENT,
            site=site,
            processed=processed,
            total=total,
            resume=args.resume,
            concurrency=args.concurrency,
        )

    _emit(
        COMPLETE_EVENT,
        site=site,
        processed=processed,
        total=total,
        resume=args.resume,
        concurrency=args.concurrency,
    )


__all__ = ["run_demo_export"]
