import asyncio
from collections import deque
import os
import sys
import platform

try:
    import psutil
except ImportError:
    psutil = None


class SystemMonitor:
    """Мониторинг системных ресурсов с использованием psutil."""

    @staticmethod
    def get_cpu_usage():
        """Возвращает текущую загрузку CPU в процентах."""
        try:
            return psutil.cpu_percent(interval=None)
        except Exception as e:
            # Fallback для систем без psutil
            try:
                if platform.system() == "Linux":
                    with open("/proc/stat", "r") as f:
                        cpu_stats = f.readline().split()[1:5]
                        # Простой fallback, не точный
                        return 50.0  # Заглушка
                else:
                    return 0.0
            except:
                return 0.0

    @staticmethod
    def get_memory_info():
        """Возвращает доступную память в MB."""
        try:
            mem = psutil.virtual_memory()
            return mem.available / (1024 * 1024)  # В MB
        except Exception as e:
            # Fallback
            try:
                if platform.system() == "Linux":
                    with open("/proc/meminfo", "r") as f:
                        for line in f:
                            if line.startswith("MemAvailable:"):
                                return float(line.split()[1]) / 1024  # В MB
                return 0.0
            except:
                return 0.0

    @staticmethod
    def get_network_stats():
        """Возвращает статистику сетевого I/O."""
        try:
            net = psutil.net_io_counters()
            return {
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv,
                "packets_sent": net.packets_sent,
                "packets_recv": net.packets_recv,
            }
        except Exception:
            return {
                "bytes_sent": 0,
                "bytes_recv": 0,
                "packets_sent": 0,
                "packets_recv": 0,
            }

    @staticmethod
    def is_system_overloaded(cpu_threshold=80, memory_threshold=80):
        """Проверяет, перегружена ли система."""
        cpu = SystemMonitor.get_cpu_usage()
        memory = SystemMonitor.get_memory_info()
        total_mem = (
            psutil.virtual_memory().total / (1024 * 1024) if psutil else 1024 * 1024
        )
        mem_usage_percent = (
            ((total_mem - memory) / total_mem) * 100 if total_mem > 0 else 0
        )
        return cpu > cpu_threshold or mem_usage_percent > memory_threshold


class ResourceTracker:
    """Отслеживание исторических метрик ресурсов для анализа трендов."""

    def __init__(self, retention_window=100, window_size=10):
        self.cpu_history = deque(maxlen=retention_window)
        self.memory_history = deque(maxlen=retention_window)
        self.window_size = window_size
        self.lock = asyncio.Lock()

    async def track(self):
        """Асинхронно отслеживает и сохраняет метрики."""
        async with self.lock:
            cpu = SystemMonitor.get_cpu_usage()
            mem = SystemMonitor.get_memory_info()
            self.cpu_history.append(cpu)
            self.memory_history.append(mem)

    def get_moving_average(self, history):
        """Вычисляет скользящее среднее для истории."""
        if len(history) < self.window_size:
            return sum(history) / len(history) if history else 0
        return sum(list(history)[-self.window_size :]) / self.window_size

    def get_cpu_average(self):
        return self.get_moving_average(self.cpu_history)

    def get_memory_average(self):
        return self.get_moving_average(self.memory_history)

    def detect_sustained_high_usage(self, threshold=80, sustained_periods=5):
        """Обнаруживает устойчиво высокую загрузку vs пики."""
        recent_cpu = list(self.cpu_history)[-sustained_periods:]
        return (
            all(avg > threshold for avg in recent_cpu)
            if len(recent_cpu) == sustained_periods
            else False
        )


def get_system_resources():
    """Возвращает основные метрики системных ресурсов с fallback."""
    cpu_percent = SystemMonitor.get_cpu_usage()

    # Расчет процента использования памяти
    if psutil:
        total_mem = psutil.virtual_memory().total / (1024 * 1024)
        available_mem = SystemMonitor.get_memory_info()
        memory_percent = (
            ((total_mem - available_mem) / total_mem) * 100 if total_mem > 0 else 0
        )
    else:
        # Fallback: попытка рассчитать из /proc/meminfo
        try:
            if platform.system() == "Linux":
                with open("/proc/meminfo", "r") as f:
                    mem_total = None
                    mem_available = None
                    for line in f:
                        if line.startswith("MemTotal:"):
                            mem_total = float(line.split()[1]) / 1024  # В MB
                        elif line.startswith("MemAvailable:"):
                            mem_available = float(line.split()[1]) / 1024  # В MB
                    if mem_total and mem_available:
                        memory_percent = ((mem_total - mem_available) / mem_total) * 100
                    else:
                        memory_percent = 50.0  # Заглушка
            else:
                memory_percent = 50.0  # Заглушка для других систем
        except:
            memory_percent = 50.0  # Заглушка

    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "network": 1.0,
    }


def calculate_optimal_concurrency(current_load, target_load=0.7):
    """Предлагает оптимальный уровень параллелизма на основе текущей нагрузки."""
    if current_load >= target_load:
        return max(1, int(1 / (current_load / target_load)))
    return 10  # Дефолтный максимум


def should_scale_down(metrics, thresholds={"cpu": 30, "memory": 30}):
    """Логика решения о масштабировании вниз."""
    cpu_avg = metrics.get("cpu_avg", 0)
    mem_avg = metrics.get("mem_avg", 0)
    return cpu_avg < thresholds["cpu"] and mem_avg < thresholds["memory"]


def format_resource_stats(stats):
    """Форматирует статистику ресурсов для читаемости."""
    if "cpu" in stats:
        return f"CPU: {stats['cpu']:.1f}% | Memory: {stats.get('memory', 0):.0f}MB | Net: {stats.get('net_sent', 0)/1024/1024:.1f}MB sent"
    return "No stats available"
