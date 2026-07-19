use std::{
    collections::{BinaryHeap, BTreeMap, HashMap, VecDeque},
    fmt::Display,
    sync::{mpsc, Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

const MAX_WORKERS: usize = 4;

// ======================================================
// ENUMS
// ======================================================

#[derive(Debug, Clone, Copy, PartialEq)]
enum TaskState {
    Created,
    Running,
    Finished,
    Failed,
}

// ======================================================
// STRUCTS
// ======================================================

#[derive(Debug, Clone)]
struct Task {
    id: usize,
    name: String,
    priority: u32,
    state: TaskState,
}

#[derive(Debug)]
struct Metrics {
    executed: usize,
    total_priority: u64,
}

impl Metrics {
    fn new() -> Self {
        Self {
            executed: 0,
            total_priority: 0,
        }
    }

    fn add(&mut self, p: u32) {
        self.executed += 1;
        self.total_priority += p as u64;
    }

    fn average(&self) -> f64 {
        if self.executed == 0 {
            return 0.0;
        }

        self.total_priority as f64 / self.executed as f64
    }
}

// ======================================================
// TRAIT
// ======================================================

trait Processor {
    fn process(&self, task: &mut Task) -> Result<u32, String>;
}

// ======================================================
// IMPLEMENTAÇÃO
// ======================================================

struct ComplexProcessor;

impl Processor for ComplexProcessor {
    fn process(&self, task: &mut Task) -> Result<u32, String> {
        task.state = TaskState::Running;

        if task.priority % 17 == 0 {
            task.state = TaskState::Failed;
            return Err(format!("Task {} failed", task.id));
        }

        thread::sleep(Duration::from_millis(5));

        task.state = TaskState::Finished;

        Ok(task.priority * 2)
    }
}

// ======================================================
// GENERICS
// ======================================================

fn print_any<T: Display>(v: T) {
    println!("{v}");
}

// ======================================================
// RECURSÃO
// ======================================================

fn fibonacci(n: u64) -> u64 {
    match n {
        0 => 0,
        1 => 1,
        _ => fibonacci(n - 1) + fibonacci(n - 2),
    }
}

// ======================================================
// LIFETIME
// ======================================================

fn longest<'a>(a: &'a str, b: &'a str) -> &'a str {
    if a.len() > b.len() {
        a
    } else {
        b
    }
}

// ======================================================
// SCHEDULER
// ======================================================

struct Scheduler<P: Processor> {
    processor: P,
    queue: VecDeque<Task>,
    metrics: Metrics,
}

impl<P: Processor> Scheduler<P> {
    fn new(processor: P) -> Self {
        Self {
            processor,
            queue: VecDeque::new(),
            metrics: Metrics::new(),
        }
    }

    fn push(&mut self, task: Task) {
        self.queue.push_back(task);
    }

    fn execute(&mut self) {
        while let Some(mut task) = self.queue.pop_front() {
            match self.processor.process(&mut task) {
                Ok(v) => self.metrics.add(v),
                Err(e) => println!("{e}"),
            }
        }
    }
}

// ======================================================
// ITERATORS
// ======================================================

fn transform(tasks: &[Task]) -> Vec<u32> {
    tasks
        .iter()
        .filter(|t| t.priority > 30)
        .map(|t| t.priority * 3)
        .collect()
}

// ======================================================
// HASHMAP
// ======================================================

fn histogram(tasks: &[Task]) -> HashMap<u32, usize> {
    let mut map = HashMap::new();

    for t in tasks {
        *map.entry(t.priority).or_insert(0) += 1;
    }

    map
}

// ======================================================
// BTREEMAP
// ======================================================

fn ordered(tasks: &[Task]) -> BTreeMap<u32, Vec<String>> {
    let mut tree = BTreeMap::new();

    for t in tasks {
        tree.entry(t.priority)
            .or_insert(Vec::new())
            .push(t.name.clone());
    }

    tree
}

// ======================================================
// BINARY HEAP
// ======================================================

fn top_priorities(tasks: &[Task]) -> BinaryHeap<u32> {
    let mut heap = BinaryHeap::new();

    for t in tasks {
        heap.push(t.priority);
    }

    heap
}

// ======================================================
// THREADS + CHANNEL
// ======================================================

fn threaded_sum(values: Vec<u32>) -> u32 {
    let (tx, rx) = mpsc::channel();

    let chunk = values.len() / MAX_WORKERS + 1;

    for part in values.chunks(chunk) {
        let tx = tx.clone();
        let local = part.to_vec();

        thread::spawn(move || {
            let sum: u32 = local.iter().sum();
            tx.send(sum).unwrap();
        });
    }

    drop(tx);

    rx.iter().sum()
}

// ======================================================
// ARC + MUTEX
// ======================================================

fn shared_counter() -> usize {
    let counter = Arc::new(Mutex::new(0));

    let mut handles = Vec::new();

    for _ in 0..8 {
        let c = Arc::clone(&counter);

        handles.push(thread::spawn(move || {
            for _ in 0..500 {
                let mut v = c.lock().unwrap();
                *v += 1;
            }
        }));
    }

    for h in handles {
        h.join().unwrap();
    }

    *counter.lock().unwrap()
}

// ======================================================
// CLOSURE
// ======================================================

fn pipeline(mut value: i32) -> i32 {
    let ops: Vec<Box<dyn Fn(i32) -> i32>> = vec![
        Box::new(|x| x + 5),
        Box::new(|x| x * 2),
        Box::new(|x| x - 8),
        Box::new(|x| x.abs()),
    ];

    for op in ops {
        value = op(value);
    }

    value
}

// ======================================================
// MAIN
// ======================================================

fn main() {
    let start = Instant::now();

    let mut scheduler = Scheduler::new(ComplexProcessor);

    let mut tasks = Vec::new();

    for i in 0..100 {
        let task = Task {
            id: i,
            name: format!("Task-{i}"),
            priority: ((i * 13) % 100) as u32 + 1,
            state: TaskState::Created,
        };

        scheduler.push(task.clone());
        tasks.push(task);
    }

    scheduler.execute();

    println!("Average: {}", scheduler.metrics.average());

    let transformed = transform(&tasks);

    println!("Transform size {}", transformed.len());

    let hist = histogram(&tasks);

    println!("Histogram {}", hist.len());

    let ordered = ordered(&tasks);

    println!("Ordered {}", ordered.len());

    let heap = top_priorities(&tasks);

    println!("Heap size {}", heap.len());

    let total = threaded_sum(transformed);

    println!("Thread sum {}", total);

    println!("Counter {}", shared_counter());

    println!("Fib {}", fibonacci(25));

    println!("Pipeline {}", pipeline(15));

    print_any(longest("Rust", "ComplexExample"));

    println!("Execution {:?}", start.elapsed());
}