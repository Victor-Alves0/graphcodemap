from __future__ import annotations

import asyncio
import functools
import heapq
import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import lru_cache, reduce
from itertools import combinations
from statistics import mean
from typing import Dict, List, Protocol, Callable, Iterable

logging.basicConfig(level=logging.INFO)


# ============================================================
# ENUMS
# ============================================================

class TaskState(Enum):
    CREATED = auto()
    RUNNING = auto()
    FINISHED = auto()
    FAILED = auto()


# ============================================================
# EXCEPTIONS
# ============================================================

class TaskError(Exception):
    pass


# ============================================================
# DECORATORS
# ============================================================

def timer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        value = func(*args, **kwargs)
        end = time.perf_counter()
        logging.info("%s levou %.6fs", func.__name__, end - start)
        return value

    return wrapper


# ============================================================
# CONTEXT MANAGER
# ============================================================

@contextmanager
def execution(name: str):
    print(f"[START] {name}")
    start = time.time()
    try:
        yield
    finally:
        print(f"[END] {name} ({time.time()-start:.3f}s)")


# ============================================================
# PROTOCOL
# ============================================================

class Runnable(Protocol):
    def run(self) -> int:
        ...


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Metrics:
    values: List[int] = field(default_factory=list)

    def add(self, value: int):
        self.values.append(value)

    @property
    def average(self):
        return mean(self.values) if self.values else 0


@dataclass
class Task:
    id: int
    name: str
    weight: int
    state: TaskState = TaskState.CREATED

    def __hash__(self):
        return hash(self.id)


# ============================================================
# ABSTRACT BASE CLASS
# ============================================================

class Processor(ABC):

    @abstractmethod
    def process(self, task: Task) -> int:
        ...


# ============================================================
# IMPLEMENTAÇÃO
# ============================================================

class ComplexProcessor(Processor):

    multiplier = 2

    @classmethod
    def create(cls):
        return cls()

    @staticmethod
    def normalize(value):
        return abs(value)

    def process(self, task: Task):
        task.state = TaskState.RUNNING

        result = self.normalize(task.weight * self.multiplier)

        if random.random() < 0.03:
            task.state = TaskState.FAILED
            raise TaskError(task.name)

        task.state = TaskState.FINISHED
        return result


# ============================================================
# CACHE + RECURSÃO
# ============================================================

@lru_cache(maxsize=None)
def fibonacci(n: int):

    if n <= 1:
        return n

    return fibonacci(n - 1) + fibonacci(n - 2)


# ============================================================
# GERADOR
# ============================================================

def generate_tasks(amount):

    for i in range(amount):
        yield Task(
            id=i,
            name=f"TASK_{i}",
            weight=random.randint(1, 100)
        )


# ============================================================
# CLASSE PRINCIPAL
# ============================================================

class Scheduler:

    def __init__(self):
        self.processor = ComplexProcessor.create()
        self.metrics = Metrics()
        self.queue = deque()

    def load(self, tasks):

        for t in tasks:
            self.queue.append(t)

    @timer
    def execute(self):

        while self.queue:

            task = self.queue.popleft()

            try:
                result = self.processor.process(task)
                self.metrics.add(result)

            except TaskError:
                pass

    def summary(self):

        return {
            "average": self.metrics.average,
            "count": len(self.metrics.values)
        }


# ============================================================
# THREAD
# ============================================================

class Worker(threading.Thread):

    def __init__(self, scheduler):

        super().__init__()
        self.scheduler = scheduler

    def run(self):

        self.scheduler.execute()


# ============================================================
# ASYNCIO
# ============================================================

async def async_job(x):

    await asyncio.sleep(random.random() / 10)

    return x * x


async def async_pipeline():

    values = await asyncio.gather(
        *(async_job(i) for i in range(15))
    )

    return sum(values)


# ============================================================
# UTILIDADES
# ============================================================

def frequency(tasks):

    return Counter(t.state for t in tasks)


def graph(tasks):

    g = defaultdict(list)

    for a, b in combinations(tasks, 2):

        if abs(a.weight - b.weight) < 15:
            g[a.id].append(b.id)
            g[b.id].append(a.id)

    return g


def top_weights(tasks):

    return heapq.nlargest(
        5,
        tasks,
        key=lambda t: t.weight
    )


def transform(tasks):

    data = list(
        map(
            lambda x: x.weight,
            filter(
                lambda x: x.weight > 30,
                tasks
            )
        )
    )

    total = reduce(
        lambda a, b: a + b,
        data,
        0
    )

    return total


# ============================================================
# REGISTRY
# ============================================================

class Registry:

    def __init__(self):
        self.storage: Dict[int, Task] = {}

    def register(self, task):

        self.storage[task.id] = task

    def __contains__(self, item):

        return item in self.storage

    def __getitem__(self, item):

        return self.storage[item]

    def __iter__(self):

        return iter(self.storage.values())


# ============================================================
# HIGH ORDER FUNCTION
# ============================================================

def pipeline(value: int, funcs: Iterable[Callable[[int], int]]):

    result = value

    for f in funcs:
        result = f(result)

    return result


# ============================================================
# COMPLEX REPORT
# ============================================================

class Report:

    def __init__(self, registry):

        self.registry = registry

    def generate(self):

        weights = [t.weight for t in self.registry]

        if not weights:
            return {}

        return {
            "max": max(weights),
            "min": min(weights),
            "avg": mean(weights),
            "sum": sum(weights)
        }


# ============================================================
# MAIN
# ============================================================

def main():

    with execution("FULL RUN"):

        registry = Registry()

        tasks = list(generate_tasks(50))

        for t in tasks:
            registry.register(t)

        scheduler = Scheduler()

        scheduler.load(tasks)

        worker = Worker(scheduler)

        worker.start()
        worker.join()

        print(scheduler.summary())

        print(graph(tasks))

        print(top_weights(tasks))

        print(transform(tasks))

        print(fibonacci(30))

        print(frequency(tasks))

        report = Report(registry)

        print(report.generate())

        print(
            pipeline(
                10,
                [
                    lambda x: x + 5,
                    lambda x: x * 3,
                    lambda x: x - 4,
                    abs
                ]
            )
        )

        asyncio.run(async_pipeline())


if __name__ == "__main__":
    main()