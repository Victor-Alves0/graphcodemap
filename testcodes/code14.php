<?php

declare(strict_types=1);

namespace Demo;

use Attribute;
use Exception;
use ReflectionClass;
use IteratorAggregate;
use Traversable;

//////////////////////////////////////////////////////////////
// ATTRIBUTE
//////////////////////////////////////////////////////////////

#[Attribute]
class Component
{
    public function __construct(
        public string $name
    ) {}
}

//////////////////////////////////////////////////////////////
// ENUM
//////////////////////////////////////////////////////////////

enum TaskState
{
    case Created;
    case Running;
    case Finished;
    case Failed;
}

//////////////////////////////////////////////////////////////
// EXCEPTION
//////////////////////////////////////////////////////////////

class TaskException extends Exception {}

//////////////////////////////////////////////////////////////
// INTERFACE
//////////////////////////////////////////////////////////////

interface Processor
{
    public function process(Task $task): int;
}

//////////////////////////////////////////////////////////////
// TRAIT
//////////////////////////////////////////////////////////////

trait LoggerTrait
{
    public function log(string $message): void
    {
        echo "[LOG] {$message}\n";
    }
}

//////////////////////////////////////////////////////////////
// MODEL
//////////////////////////////////////////////////////////////

class Task
{
    public function __construct(
        public readonly int $id,
        public string $name,
        public int $priority,
        public TaskState $state = TaskState::Created
    ) {}

    public function __toString(): string
    {
        return "{$this->id}:{$this->name}";
    }
}

//////////////////////////////////////////////////////////////
// ABSTRACT CLASS
//////////////////////////////////////////////////////////////

abstract class BaseProcessor
{
    abstract public function process(Task $task): int;

    protected function validate(Task $task): void
    {
        if ($task->priority < 0) {
            throw new TaskException("Invalid priority");
        }
    }
}

//////////////////////////////////////////////////////////////
// IMPLEMENTATION
//////////////////////////////////////////////////////////////

#[Component("Main Processor")]
class ComplexProcessor extends BaseProcessor implements Processor
{
    use LoggerTrait;

    /** @var callable[] */
    private array $listeners = [];

    public function subscribe(callable $listener): void
    {
        $this->listeners[] = $listener;
    }

    public function process(Task $task): int
    {
        $this->validate($task);

        $task->state = TaskState::Running;

        if ($task->priority % 17 === 0) {
            $task->state = TaskState::Failed;
            throw new TaskException($task->name);
        }

        $task->state = TaskState::Finished;

        foreach ($this->listeners as $listener) {
            $listener($task);
        }

        return $task->priority * 3;
    }
}

//////////////////////////////////////////////////////////////
// GENERIC REPOSITORY
//////////////////////////////////////////////////////////////

/**
 * @template T
 */
class Repository implements IteratorAggregate
{
    /** @var array<int,mixed> */
    private array $items = [];

    public function add(mixed $item): void
    {
        $this->items[] = $item;
    }

    public function all(): array
    {
        return $this->items;
    }

    public function getIterator(): Traversable
    {
        yield from $this->items;
    }
}

//////////////////////////////////////////////////////////////
// SINGLETON
//////////////////////////////////////////////////////////////

final class Logger
{
    private static ?Logger $instance = null;

    private function __construct(){}

    public static function instance(): Logger
    {
        return self::$instance ??= new Logger();
    }

    public function info(string $msg): void
    {
        echo "[INFO] {$msg}\n";
    }
}

//////////////////////////////////////////////////////////////
// FACTORY
//////////////////////////////////////////////////////////////

class ProcessorFactory
{
    public static function create(): Processor
    {
        return new ComplexProcessor();
    }
}

//////////////////////////////////////////////////////////////
// GENERATOR
//////////////////////////////////////////////////////////////

function generateTasks(int $count): Generator
{
    for ($i = 0; $i < $count; $i++) {

        yield new Task(
            id: $i,
            name: "Task-$i",
            priority: ($i * 13) % 100 + 1
        );
    }
}

//////////////////////////////////////////////////////////////
// RECURSION
//////////////////////////////////////////////////////////////

function fibonacci(int $n): int
{
    return match (true) {
        $n < 2 => $n,
        default => fibonacci($n - 1) + fibonacci($n - 2)
    };
}

//////////////////////////////////////////////////////////////
// HELPER
//////////////////////////////////////////////////////////////

function average(array $values): float
{
    return array_sum($values) / count($values);
}

//////////////////////////////////////////////////////////////
// REFLECTION
//////////////////////////////////////////////////////////////

function inspect(object $object): void
{
    $reflection = new ReflectionClass($object);

    echo $reflection->getName() . PHP_EOL;

    foreach ($reflection->getMethods() as $method) {
        echo $method->getName() . PHP_EOL;
    }
}

//////////////////////////////////////////////////////////////
// MAGIC OBJECT
//////////////////////////////////////////////////////////////

class DynamicObject
{
    private array $storage = [];

    public function __set($name, $value)
    {
        $this->storage[$name] = $value;
    }

    public function __get($name)
    {
        return $this->storage[$name] ?? null;
    }
}

//////////////////////////////////////////////////////////////
// MAIN
//////////////////////////////////////////////////////////////

$repository = new Repository();

$processor = new ComplexProcessor();

$processor->subscribe(
    fn(Task $task) =>
        Logger::instance()->info($task->name)
);

foreach (generateTasks(100) as $task) {
    $repository->add($task);
}

$priorities = [];

foreach ($repository as $task) {

    try {

        $priorities[] =
            $processor->process($task);

    } catch (TaskException) {
    }
}

echo average($priorities) . PHP_EOL;

echo fibonacci(20) . PHP_EOL;

$top = array_slice(
    array_values(
        array_filter(
            $repository->all(),
            fn(Task $t) => $t->priority > 50
        )
    ),
    0,
    10
);

foreach ($top as $task) {
    echo $task . PHP_EOL;
}

$dynamic = new DynamicObject();

$dynamic->framework = "PHP";

echo $dynamic->framework . PHP_EOL;

inspect($processor);

$anonymous = new class {
    public function hello(): string
    {
        return "Anonymous Class";
    }
};

echo $anonymous->hello() . PHP_EOL;

$grouped = [];

foreach ($repository->all() as $task) {
    $grouped[intdiv($task->priority, 10)][] = $task;
}

print_r(array_keys($grouped));