///////////////////////////////////////////////////////////////
// TYPES
///////////////////////////////////////////////////////////////

export type ID = number;

export type Nullable<T> = T | null;

export type Json =
    | string
    | number
    | boolean
    | null
    | Json[]
    | { [key: string]: Json };

///////////////////////////////////////////////////////////////
// ENUM
///////////////////////////////////////////////////////////////

export enum TaskState {
    Created,
    Running,
    Finished,
    Failed
}

///////////////////////////////////////////////////////////////
// DECORATOR
///////////////////////////////////////////////////////////////

function Component(name: string) {
    return function (target: Function) {
        Reflect.defineProperty(target, "__component", {
            value: name
        });
    };
}

///////////////////////////////////////////////////////////////
// INTERFACES
///////////////////////////////////////////////////////////////

export interface IEntity {
    id: ID;
}

export interface Runnable {
    run(): Promise<void>;
}

export interface Processor<T> {
    process(item: T): number;
}

///////////////////////////////////////////////////////////////
// ABSTRACT CLASS
///////////////////////////////////////////////////////////////

abstract class BaseProcessor<T> {

    abstract process(item: T): number;

    protected validate(value: number): void {

        if (value < 0)
            throw new Error("Invalid value");
    }

}

///////////////////////////////////////////////////////////////
// MODEL
///////////////////////////////////////////////////////////////

export class Task implements IEntity {

    constructor(
        public id: ID,
        public name: string,
        public priority: number,
        public state: TaskState = TaskState.Created
    ) { }

}

///////////////////////////////////////////////////////////////
// OBSERVER
///////////////////////////////////////////////////////////////

type Listener<T> = (value: T) => void;

class EventEmitter<T>{

    private listeners: Listener<T>[] = [];

    subscribe(listener: Listener<T>) {

        this.listeners.push(listener);

    }

    emit(value: T) {

        for (const l of this.listeners)
            l(value);

    }

}

///////////////////////////////////////////////////////////////
// IMPLEMENTATION
///////////////////////////////////////////////////////////////

@Component("Processor")
class ComplexProcessor
    extends BaseProcessor<Task>
    implements Processor<Task> {

    process(task: Task): number {

        this.validate(task.priority);

        task.state = TaskState.Running;

        if (task.priority % 17 === 0) {

            task.state = TaskState.Failed;

            throw new Error(task.name);

        }

        task.state = TaskState.Finished;

        return task.priority * 5;

    }

}

///////////////////////////////////////////////////////////////
// GENERIC REPOSITORY
///////////////////////////////////////////////////////////////

class Repository<T extends IEntity>{

    protected data = new Map<ID, T>();

    add(item: T) {

        this.data.set(item.id, item);

    }

    get(id: ID) {

        return this.data.get(id);

    }

    values() {

        return [...this.data.values()];

    }

}

///////////////////////////////////////////////////////////////
// SINGLETON
///////////////////////////////////////////////////////////////

class Logger {

    private static instance: Logger;

    private constructor() { }

    static getInstance() {

        if (!Logger.instance)
            Logger.instance = new Logger();

        return Logger.instance;

    }

    log(message: string) {

        console.log(message);

    }

}

///////////////////////////////////////////////////////////////
// FACTORY
///////////////////////////////////////////////////////////////

class ProcessorFactory {

    static create(): Processor<Task> {

        return new ComplexProcessor();

    }

}

///////////////////////////////////////////////////////////////
// STRATEGY
///////////////////////////////////////////////////////////////

interface SortStrategy {

    sort(tasks: Task[]): Task[];

}

class DescStrategy
    implements SortStrategy {

    sort(tasks: Task[]) {

        return tasks.sort(
            (a, b) =>
                b.priority - a.priority
        );

    }

}

///////////////////////////////////////////////////////////////
// SCHEDULER
///////////////////////////////////////////////////////////////

class Scheduler
    implements Runnable {

    private queue: Task[] = [];

    readonly events =
        new EventEmitter<Task>();

    constructor(
        private processor:
            Processor<Task>
    ) { }

    enqueue(task: Task) {

        this.queue.push(task);

    }

    async run() {

        while (this.queue.length) {

            const task =
                this.queue.shift()!;

            await Promise.resolve();

            try {

                this.processor.process(task);

                this.events.emit(task);

            }
            catch {

            }

        }

    }

}

///////////////////////////////////////////////////////////////
// GENERATOR
///////////////////////////////////////////////////////////////

function* generateTasks(
    amount: number
) {

    for (let i = 0; i < amount; i++) {

        yield new Task(
            i,
            `Task-${i}`,
            (i * 19) % 100 + 1
        );

    }

}

///////////////////////////////////////////////////////////////
// RECURSION
///////////////////////////////////////////////////////////////

function fibonacci(
    n: number
): number {

    if (n < 2)
        return n;

    return fibonacci(n - 1)
        + fibonacci(n - 2);

}

///////////////////////////////////////////////////////////////
// OVERLOAD
///////////////////////////////////////////////////////////////

function format(value: number): string;
function format(value: string): string;

function format(value: any) {

    return `[${value}]`;

}

///////////////////////////////////////////////////////////////
// TYPE GUARD
///////////////////////////////////////////////////////////////

function isTask(
    value: any
): value is Task {

    return value instanceof Task;

}

///////////////////////////////////////////////////////////////
// CONDITIONAL TYPE
///////////////////////////////////////////////////////////////

type ElementType<T> =
    T extends (infer U)[]
    ? U
    : never;

///////////////////////////////////////////////////////////////
// NAMESPACE
///////////////////////////////////////////////////////////////

namespace Utils {

    export function average(
        values: number[]
    ) {

        return values.reduce(
            (a, b) => a + b,
            0
        ) / values.length;

    }

}

///////////////////////////////////////////////////////////////
// MAIN
///////////////////////////////////////////////////////////////

async function main() {

    const logger =
        Logger.getInstance();

    const repository =
        new Repository<Task>();

    const scheduler =
        new Scheduler(
            ProcessorFactory.create()
        );

    scheduler.events.subscribe(
        task =>
            logger.log(task.name)
    );

    for (const task of generateTasks(100)) {

        repository.add(task);

        scheduler.enqueue(task);

    }

    await scheduler.run();

    const strategy =
        new DescStrategy();

    const ordered =
        strategy.sort(
            repository.values()
        );

    const priorities =
        ordered.map(
            x => x.priority
        );

    console.log(
        Utils.average(priorities)
    );

    console.log(
        fibonacci(20)
    );

    console.log(
        format(123)
    );

    console.log(
        format("typescript")
    );

    const weak =
        new WeakMap<Task, Json>();

    weak.set(
        ordered[0],
        {
            created: true
        }
    );

    console.log(
        weak.get(ordered[0])
    );

    const set =
        new Set(priorities);

    console.log(
        set.size
    );

    const readonly:
        ReadonlyMap<ID, Task> =
        new Map(
            ordered.map(
                x => [x.id, x]
            )
        );

    console.log(
        readonly.get(1)?.name
            ?? "Not Found"
    );

    const partial:
        Partial<Task> = {
        id: 999
    };

    console.log(partial);

    const picked:
        Pick<Task,
            "id" | "name"> = {
        id: 1,
        name: "Demo"
    };

    console.log(picked);

    const record:
        Record<string, number> = {
        a: 1,
        b: 2
    };

    console.log(record);

    if (isTask(ordered[0])) {

        console.log(
            ordered[0].priority
        );

    }

}

main();