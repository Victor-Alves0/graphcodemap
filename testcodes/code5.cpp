#include <iostream>
#include <vector>
#include <queue>
#include <stack>
#include <map>
#include <unordered_map>
#include <set>
#include <memory>
#include <thread>
#include <mutex>
#include <future>
#include <optional>
#include <variant>
#include <algorithm>
#include <numeric>
#include <functional>
#include <chrono>
#include <condition_variable>

using namespace std;

//============================================================
// ENUM
//============================================================

enum class TaskState
{
    Created,
    Running,
    Finished,
    Failed
};

//============================================================
// EXCEPTION
//============================================================

class TaskException : public runtime_error
{
public:
    TaskException(const string& msg)
        : runtime_error(msg)
    {}
};

//============================================================
// STRUCT
//============================================================

struct Task
{
    int id;
    string name;
    int priority;
    TaskState state = TaskState::Created;
};

//============================================================
// RAII TIMER
//============================================================

class Timer
{
    chrono::high_resolution_clock::time_point start;

public:

    Timer()
    {
        start = chrono::high_resolution_clock::now();
    }

    ~Timer()
    {
        auto end = chrono::high_resolution_clock::now();

        cout
            << "Execution: "
            << chrono::duration_cast<chrono::milliseconds>(end-start).count()
            << "ms\n";
    }
};

//============================================================
// ABSTRACT CLASS
//============================================================

class Processor
{
public:

    virtual int process(Task&) = 0;

    virtual ~Processor() = default;
};

//============================================================
// IMPLEMENTATION
//============================================================

class ComplexProcessor : public Processor
{
public:

    int process(Task& task) override
    {
        task.state = TaskState::Running;

        if(task.priority % 13 == 0)
        {
            task.state = TaskState::Failed;
            throw TaskException("Invalid task");
        }

        this_thread::sleep_for(chrono::milliseconds(5));

        task.state = TaskState::Finished;

        return task.priority * 3;
    }
};

//============================================================
// TEMPLATE
//============================================================

template<typename T>
T sumVector(const vector<T>& values)
{
    return accumulate(values.begin(), values.end(), T{});
}

//============================================================
// RECURSION
//============================================================

int fibonacci(int n)
{
    if(n<=1)
        return n;

    return fibonacci(n-1)+fibonacci(n-2);
}

//============================================================
// GRAPH
//============================================================

class Graph
{
    unordered_map<int,vector<int>> edges;

public:

    void connect(int a,int b)
    {
        edges[a].push_back(b);
    }

    void print()
    {
        for(auto& [k,v]:edges)
        {
            cout<<k<<" -> ";

            for(auto i:v)
                cout<<i<<" ";

            cout<<"\n";
        }
    }
};

//============================================================
// SCHEDULER
//============================================================

class Scheduler
{
    unique_ptr<Processor> processor;

    vector<Task> tasks;

    mutex m;

public:

    Scheduler()
    {
        processor=make_unique<ComplexProcessor>();
    }

    void add(Task task)
    {
        lock_guard<mutex> lock(m);

        tasks.push_back(task);
    }

    vector<int> execute()
    {
        vector<int> results;

        for(auto& task:tasks)
        {
            try
            {
                results.push_back(
                    processor->process(task)
                );
            }
            catch(...)
            {

            }
        }

        return results;
    }

    vector<Task>& getTasks()
    {
        return tasks;
    }
};

//============================================================
// THREAD POOL (SIMPLE)
//============================================================

class Worker
{
    mutex m;

public:

    void operator()(vector<int>& data)
    {
        lock_guard<mutex> lock(m);

        for(auto& i:data)
            i*=2;
    }
};

//============================================================
// OPTIONAL
//============================================================

optional<Task> findTask(
    vector<Task>& tasks,
    int id)
{
    for(auto& t:tasks)
        if(t.id==id)
            return t;

    return nullopt;
}

//============================================================
// VARIANT
//============================================================

using Data=variant<int,double,string>;

void printData(const Data& d)
{
    visit([](auto&& value)
    {
        cout<<value<<"\n";
    },d);
}

//============================================================
// ALGORITHMS
//============================================================

void sortTasks(vector<Task>& tasks)
{
    sort(
        tasks.begin(),
        tasks.end(),
        [](auto&a,auto&b)
        {
            return a.priority>b.priority;
        });
}

//============================================================
// MAIN
//============================================================

int main()
{
    Timer timer;

    Scheduler scheduler;

    for(int i=0;i<100;i++)
    {
        scheduler.add({
            i,
            "Task-"+to_string(i),
            (i*17)%100+1
        });
    }

    auto results=scheduler.execute();

    cout<<"Results: "<<results.size()<<"\n";

    vector<thread> threads;

    Worker worker;

    for(int i=0;i<4;i++)
        threads.emplace_back(worker,ref(results));

    for(auto& t:threads)
        t.join();

    auto future=async(
        launch::async,
        fibonacci,
        25
    );

    cout<<"Fib: "<<future.get()<<"\n";

    auto total=sumVector(results);

    cout<<"Total "<<total<<"\n";

    auto& tasks=scheduler.getTasks();

    sortTasks(tasks);

    if(auto t=findTask(tasks,20))
    {
        cout<<"Found "<<t->name<<"\n";
    }

    Graph graph;

    for(size_t i=1;i<tasks.size();i++)
    {
        graph.connect(
            tasks[i-1].id,
            tasks[i].id
        );
    }

    graph.print();

    map<int,int> histogram;

    for(auto&t:tasks)
        histogram[t.priority]++;

    for(auto&[k,v]:histogram)
        cout<<k<<" "<<v<<"\n";

    set<int> uniquePriorities;

    for(auto&t:tasks)
        uniquePriorities.insert(t.priority);

    cout<<"Unique "<<uniquePriorities.size()<<"\n";

    printData(42);
    printData(3.1415);
    printData(string("Hello World"));

    vector<int> transformed;

    transform(
        results.begin(),
        results.end(),
        back_inserter(transformed),
        [](int x)
        {
            return x/2;
        });

    int even=
        count_if(
            transformed.begin(),
            transformed.end(),
            [](int x)
            {
                return x%2==0;
            });

    cout<<"Even "<<even<<"\n";

    priority_queue<int> pq;

    for(auto v:transformed)
        pq.push(v);

    cout<<"Highest "<<pq.top()<<"\n";

    return 0;
}