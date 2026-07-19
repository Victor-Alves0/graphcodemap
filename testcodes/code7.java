import java.lang.annotation.*;
import java.lang.reflect.Method;
import java.time.LocalDateTime;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.Function;
import java.util.stream.Collectors;

//////////////////////////////////////////////////////////////
// ANNOTATION
//////////////////////////////////////////////////////////////

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@interface Component {
    String value();
}

//////////////////////////////////////////////////////////////
// ENUM
//////////////////////////////////////////////////////////////

enum TaskState {
    CREATED,
    RUNNING,
    FINISHED,
    FAILED
}

//////////////////////////////////////////////////////////////
// RECORD
//////////////////////////////////////////////////////////////

record LogEntry(LocalDateTime time, String message) {}

//////////////////////////////////////////////////////////////
// EXCEPTION
//////////////////////////////////////////////////////////////

class TaskException extends Exception {
    public TaskException(String message) {
        super(message);
    }
}

//////////////////////////////////////////////////////////////
// MODEL
//////////////////////////////////////////////////////////////

class Task {

    private final int id;
    private final String name;
    private final int priority;
    private TaskState state = TaskState.CREATED;

    public Task(int id, String name, int priority) {
        this.id = id;
        this.name = name;
        this.priority = priority;
    }

    public int getId() {
        return id;
    }

    public int getPriority() {
        return priority;
    }

    public String getName() {
        return name;
    }

    public TaskState getState() {
        return state;
    }

    public void setState(TaskState state) {
        this.state = state;
    }

    @Override
    public String toString() {
        return id + ":" + name;
    }
}

//////////////////////////////////////////////////////////////
// LISTENER
//////////////////////////////////////////////////////////////

interface TaskListener {
    void processed(Task task);
}

//////////////////////////////////////////////////////////////
// INTERFACE
//////////////////////////////////////////////////////////////

interface Processor {
    int process(Task task) throws TaskException;
}

//////////////////////////////////////////////////////////////
// ABSTRACT CLASS
//////////////////////////////////////////////////////////////

abstract class AbstractProcessor {

    protected void validate(Task task)
            throws TaskException {

        if (task.getPriority() < 0)
            throw new TaskException("Invalid priority");
    }

    public abstract String version();
}

//////////////////////////////////////////////////////////////
// IMPLEMENTATION
//////////////////////////////////////////////////////////////

@Component("ComplexProcessor")
class ComplexProcessor
        extends AbstractProcessor
        implements Processor {

    private final List<TaskListener> listeners =
            new ArrayList<>();

    public void addListener(TaskListener listener) {
        listeners.add(listener);
    }

    @Override
    public String version() {
        return "2.0";
    }

    @Override
    public int process(Task task)
            throws TaskException {

        validate(task);

        task.setState(TaskState.RUNNING);

        if (task.getPriority() % 17 == 0) {
            task.setState(TaskState.FAILED);
            throw new TaskException(task.getName());
        }

        task.setState(TaskState.FINISHED);

        listeners.forEach(l -> l.processed(task));

        return task.getPriority() * 4;
    }
}

//////////////////////////////////////////////////////////////
// GENERIC REPOSITORY
//////////////////////////////////////////////////////////////

class Repository<T> {

    private final List<T> data = new ArrayList<>();

    public void add(T item) {
        data.add(item);
    }

    public List<T> all() {
        return data;
    }
}

//////////////////////////////////////////////////////////////
// BUILDER
//////////////////////////////////////////////////////////////

class TaskBuilder {

    private int id;
    private String name;
    private int priority;

    public TaskBuilder id(int id) {
        this.id = id;
        return this;
    }

    public TaskBuilder name(String name) {
        this.name = name;
        return this;
    }

    public TaskBuilder priority(int priority) {
        this.priority = priority;
        return this;
    }

    public Task build() {
        return new Task(id, name, priority);
    }
}

//////////////////////////////////////////////////////////////
// FACTORY
//////////////////////////////////////////////////////////////

class ProcessorFactory {

    public static Processor create() {
        return new ComplexProcessor();
    }
}

//////////////////////////////////////////////////////////////
// RECURSION
//////////////////////////////////////////////////////////////

class MathUtil {

    public static int fibonacci(int n) {

        if (n < 2)
            return n;

        return fibonacci(n - 1)
                + fibonacci(n - 2);
    }
}

//////////////////////////////////////////////////////////////
// SCHEDULER
//////////////////////////////////////////////////////////////

class Scheduler {

    private final Queue<Task> queue =
            new LinkedList<>();

    private final Processor processor;

    public Scheduler(Processor processor) {
        this.processor = processor;
    }

    public synchronized void enqueue(Task task) {
        queue.offer(task);
    }

    public List<Integer> execute() {

        List<Integer> result =
                new ArrayList<>();

        while (!queue.isEmpty()) {

            Task task = queue.poll();

            try {

                result.add(
                        processor.process(task)
                );

            } catch (Exception ignored) {

            }
        }

        return result;
    }
}

//////////////////////////////////////////////////////////////
// REFLECTION
//////////////////////////////////////////////////////////////

class Inspector {

    public static void inspect(Class<?> c) {

        System.out.println(c.getName());

        for (Method method :
                c.getDeclaredMethods()) {

            System.out.println(method.getName());
        }
    }
}

//////////////////////////////////////////////////////////////
// MAIN
//////////////////////////////////////////////////////////////

public class Main {

    public static void main(String[] args)
            throws Exception {

        Repository<Task> repository =
                new Repository<>();

        ComplexProcessor processor =
                new ComplexProcessor();

        processor.addListener(
                task -> System.out.println(
                        "Processed " + task.getName()));

        Scheduler scheduler =
                new Scheduler(processor);

        for (int i = 0; i < 100; i++) {

            Task task =
                    new TaskBuilder()
                            .id(i)
                            .name("Task-" + i)
                            .priority((i * 13) % 100 + 1)
                            .build();

            repository.add(task);

            scheduler.enqueue(task);
        }

        List<Integer> values =
                scheduler.execute();

        int total =
                values.stream()
                        .reduce(0, Integer::sum);

        System.out.println(total);

        List<Task> top =
                repository.all()
                        .stream()
                        .sorted(
                                Comparator.comparingInt(
                                        Task::getPriority)
                                        .reversed())
                        .limit(5)
                        .toList();

        System.out.println(top);

        ExecutorService executor =
                Executors.newFixedThreadPool(4);

        CompletableFuture<Integer> fib =
                CompletableFuture.supplyAsync(
                        () -> MathUtil.fibonacci(25),
                        executor);

        System.out.println(fib.get());

        executor.shutdown();

        Optional<Task> optional =
                repository.all()
                        .stream()
                        .filter(t -> t.getId() == 10)
                        .findFirst();

        optional.ifPresent(System.out::println);

        Map<Integer, List<Task>> grouped =
                repository.all()
                        .stream()
                        .collect(
                                Collectors.groupingBy(
                                        t -> t.getPriority() / 10));

        grouped.forEach((k, v) ->
                System.out.println(
                        k + " -> " + v.size()));

        Set<Integer> priorities =
                repository.all()
                        .stream()
                        .map(Task::getPriority)
                        .collect(Collectors.toSet());

        System.out.println(priorities.size());

        PriorityQueue<Integer> pq =
                new PriorityQueue<>(
                        Comparator.reverseOrder());

        pq.addAll(values);

        System.out.println(pq.poll());

        Function<Integer, Integer> pipeline =
                x -> (x + 5) * 2;

        System.out.println(
                pipeline.apply(20));

        Inspector.inspect(
                ComplexProcessor.class);

        Object obj = "Hello";

        if (obj instanceof String text) {
            System.out.println(text);
        }

        List<LogEntry> logs =
                List.of(
                        new LogEntry(
                                LocalDateTime.now(),
                                "Application Started")
                );

        logs.forEach(System.out::println);
    }
}