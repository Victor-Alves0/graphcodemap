exntusing System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;

namespace ComplexExample
{
    //=========================================================
    // ATTRIBUTE
    //=========================================================

    [AttributeUsage(AttributeTargets.Class)]
    public class ComponentAttribute : Attribute
    {
        public string Name { get; }

        public ComponentAttribute(string name)
        {
            Name = name;
        }
    }

    //=========================================================
    // ENUM
    //=========================================================

    public enum TaskState
    {
        Created,
        Running,
        Finished,
        Failed
    }

    //=========================================================
    // RECORD
    //=========================================================

    public record LogEntry(DateTime Time, string Message);

    //=========================================================
    // STRUCT
    //=========================================================

    public struct Metrics
    {
        public int Executed;
        public int TotalPriority;

        public double Average =>
            Executed == 0 ? 0 :
            (double)TotalPriority / Executed;
    }

    //=========================================================
    // EXCEPTION
    //=========================================================

    public class TaskException : Exception
    {
        public TaskException(string msg)
            : base(msg)
        {
        }
    }

    //=========================================================
    // MODEL
    //=========================================================

    public class WorkTask
    {
        public int Id { get; set; }

        public string Name { get; set; } = "";

        public int Priority { get; set; }

        public TaskState State { get; set; }

        public override string ToString()
        {
            return $"{Id} - {Name}";
        }
    }

    //=========================================================
    // INTERFACE
    //=========================================================

    public interface IProcessor
    {
        int Process(WorkTask task);
    }

    //=========================================================
    // ABSTRACT
    //=========================================================

    public abstract class ProcessorBase
    {
        public abstract string Version { get; }

        protected void Validate(WorkTask task)
        {
            if (task.Priority < 0)
                throw new TaskException("Invalid priority");
        }
    }

    //=========================================================
    // IMPLEMENTATION
    //=========================================================

    [Component("Main Processor")]
    public class ComplexProcessor :
        ProcessorBase,
        IProcessor
    {
        public override string Version => "1.0";

        public event Action<WorkTask>? TaskProcessed;

        public int Process(WorkTask task)
        {
            Validate(task);

            task.State = TaskState.Running;

            if (task.Priority % 17 == 0)
            {
                task.State = TaskState.Failed;
                throw new TaskException(task.Name);
            }

            Thread.Sleep(5);

            task.State = TaskState.Finished;

            TaskProcessed?.Invoke(task);

            return task.Priority * 3;
        }
    }

    //=========================================================
    // GENERIC REPOSITORY
    //=========================================================

    public class Repository<T>
    {
        private readonly List<T> _items = new();

        public void Add(T item)
            => _items.Add(item);

        public IEnumerable<T> All()
            => _items;

        public int Count => _items.Count;

        public T this[int index]
        {
            get => _items[index];
            set => _items[index] = value;
        }
    }

    //=========================================================
    // EXTENSION METHODS
    //=========================================================

    public static class Extensions
    {
        public static int SumPriority(
            this IEnumerable<WorkTask> tasks)
        {
            return tasks.Sum(t => t.Priority);
        }

        public static IEnumerable<T> Filter<T>(
            this IEnumerable<T> items,
            Func<T, bool> predicate)
        {
            foreach (var item in items)
            {
                if (predicate(item))
                    yield return item;
            }
        }
    }

    //=========================================================
    // OPERATOR OVERLOAD
    //=========================================================

    public class Counter
    {
        public int Value { get; }

        public Counter(int value)
        {
            Value = value;
        }

        public static Counter operator +(
            Counter a,
            Counter b)
        {
            return new Counter(a.Value + b.Value);
        }
    }

    //=========================================================
    // RECURSION
    //=========================================================

    static class MathUtil
    {
        public static int Fibonacci(int n)
        {
            if (n < 2)
                return n;

            return Fibonacci(n - 1)
                 + Fibonacci(n - 2);
        }
    }

    //=========================================================
    // SCHEDULER
    //=========================================================

    public class Scheduler
    {
        private readonly IProcessor _processor;

        private readonly object _lock = new();

        public Metrics Metrics;

        public Queue<WorkTask> Queue { get; } = new();

        public Scheduler(IProcessor processor)
        {
            _processor = processor;
        }

        public void Enqueue(WorkTask task)
        {
            lock (_lock)
            {
                Queue.Enqueue(task);
            }
        }

        public void Execute()
        {
            while (Queue.Count > 0)
            {
                var task = Queue.Dequeue();

                try
                {
                    var result =
                        _processor.Process(task);

                    Metrics.Executed++;
                    Metrics.TotalPriority += result;
                }
                catch
                {

                }
            }
        }
    }

    //=========================================================
    // REFLECTION
    //=========================================================

    static class Inspector
    {
        public static void Print(Type t)
        {
            Console.WriteLine($"Type: {t.Name}");

            foreach (var m in t.GetMethods())
            {
                Console.WriteLine(m.Name);
            }
        }
    }

    //=========================================================
    // MAIN
    //=========================================================

    internal class Program
    {
        static async Task Main()
        {
            var repository =
                new Repository<WorkTask>();

            var processor =
                new ComplexProcessor();

            processor.TaskProcessed +=
                task =>
                {
                    Console.WriteLine(
                        $"Processed {task.Name}");
                };

            var scheduler =
                new Scheduler(processor);

            for (int i = 0; i < 100; i++)
            {
                var task = new WorkTask
                {
                    Id = i,
                    Name = $"Task-{i}",
                    Priority = (i * 13) % 100 + 1,
                    State = TaskState.Created
                };

                repository.Add(task);

                scheduler.Enqueue(task);
            }

            scheduler.Execute();

            Console.WriteLine(
                scheduler.Metrics.Average);

            var top =
                repository.All()
                    .OrderByDescending(
                        x => x.Priority)
                    .Take(5)
                    .ToList();

            Console.WriteLine(
                top.SumPriority());

            var concurrent =
                new ConcurrentDictionary<int,
                    WorkTask>();

            Parallel.ForEach(
                repository.All(),
                task =>
                {
                    concurrent[task.Id] = task;
                });

            var reflection =
                typeof(ComplexProcessor);

            Inspector.Print(reflection);

            var fib =
                await Task.Run(() =>
                    MathUtil.Fibonacci(25));

            Console.WriteLine(fib);

            var counter =
                new Counter(5)
                + new Counter(10);

            Console.WriteLine(
                counter.Value);

            var logs =
                new List<LogEntry>();

            logs.Add(
                new LogEntry(
                    DateTime.Now,
                    "Application Started"));

            foreach (var log in logs)
            {
                Console.WriteLine(log);
            }

            var grouped =
                repository.All()
                    .GroupBy(
                        x => x.Priority / 10);

            foreach (var g in grouped)
            {
                Console.WriteLine(
                    $"{g.Key}: {g.Count()}");
            }

            CancellationTokenSource cts =
                new();

            try
            {
                await Task.Delay(
                    100,
                    cts.Token);
            }
            catch
            {

            }

            var stack =
                new Stack<int>();

            var dictionary =
                new Dictionary<int, string>();

            var set =
                new HashSet<int>();

            foreach (var t in repository.All())
            {
                stack.Push(t.Id);

                dictionary[t.Id] = t.Name;

                set.Add(t.Priority);
            }

            Console.WriteLine(
                stack.Count);

            Console.WriteLine(
                dictionary.Count);

            Console.WriteLine(
                set.Count);

            object obj = repository.Count;

            if (obj is int count)
            {
                Console.WriteLine(count);
            }
        }
    }
}