package main

import (
	"context"
	"container/heap"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"sort"
	"sync"
	"time"
)

//////////////////////////////////////////////////////////////
// ENUM SIMULADO
//////////////////////////////////////////////////////////////

type TaskState int

const (
	Created TaskState = iota
	Running
	Finished
	Failed
)

//////////////////////////////////////////////////////////////
// ERRORS
//////////////////////////////////////////////////////////////

var ErrInvalidPriority = errors.New("invalid priority")

//////////////////////////////////////////////////////////////
// STRUCTS
//////////////////////////////////////////////////////////////

type Task struct {
	ID       int
	Name     string
	Priority int
	State    TaskState
}

type Metrics struct {
	Executed int
	Total    int
}

func (m *Metrics) Average() float64 {
	if m.Executed == 0 {
		return 0
	}
	return float64(m.Total) / float64(m.Executed)
}

//////////////////////////////////////////////////////////////
// INTERFACE
//////////////////////////////////////////////////////////////

type Processor interface {
	Process(*Task) error
}

//////////////////////////////////////////////////////////////
// EMBEDDING
//////////////////////////////////////////////////////////////

type BaseProcessor struct {
	Name string
}

type ComplexProcessor struct {
	BaseProcessor
}

func (p *ComplexProcessor) Process(task *Task) error {

	task.State = Running

	if task.Priority%19 == 0 {
		task.State = Failed
		return ErrInvalidPriority
	}

	time.Sleep(time.Millisecond * 5)

	task.State = Finished

	return nil
}

//////////////////////////////////////////////////////////////
// GENERICS
//////////////////////////////////////////////////////////////

func Map[T any, R any](values []T, mapper func(T) R) []R {

	out := make([]R, 0)

	for _, v := range values {
		out = append(out, mapper(v))
	}

	return out
}

//////////////////////////////////////////////////////////////
// RECURSÃO
//////////////////////////////////////////////////////////////

func Fibonacci(n int) int {

	if n < 2 {
		return n
	}

	return Fibonacci(n-1) + Fibonacci(n-2)
}

//////////////////////////////////////////////////////////////
// CLOSURE
//////////////////////////////////////////////////////////////

func Pipeline(start int, funcs ...func(int) int) int {

	value := start

	for _, f := range funcs {
		value = f(value)
	}

	return value
}

//////////////////////////////////////////////////////////////
// HEAP
//////////////////////////////////////////////////////////////

type PriorityQueue []int

func (p PriorityQueue) Len() int {
	return len(p)
}

func (p PriorityQueue) Less(i, j int) bool {
	return p[i] > p[j]
}

func (p PriorityQueue) Swap(i, j int) {
	p[i], p[j] = p[j], p[i]
}

func (p *PriorityQueue) Push(x interface{}) {
	*p = append(*p, x.(int))
}

func (p *PriorityQueue) Pop() interface{} {

	old := *p

	n := len(old)

	item := old[n-1]

	*p = old[:n-1]

	return item
}

//////////////////////////////////////////////////////////////
// SCHEDULER
//////////////////////////////////////////////////////////////

type Scheduler struct {
	queue   []*Task
	lock    sync.Mutex
	metrics Metrics
	proc    Processor
}

func NewScheduler(p Processor) *Scheduler {

	return &Scheduler{
		proc: p,
	}
}

func (s *Scheduler) Push(task *Task) {

	s.lock.Lock()
	defer s.lock.Unlock()

	s.queue = append(s.queue, task)
}

func (s *Scheduler) Execute() {

	s.lock.Lock()

	queue := s.queue

	s.queue = nil

	s.lock.Unlock()

	for _, task := range queue {

		err := s.proc.Process(task)

		if err == nil {

			s.metrics.Executed++
			s.metrics.Total += task.Priority
		}
	}
}

//////////////////////////////////////////////////////////////
// GOROUTINES
//////////////////////////////////////////////////////////////

func ParallelSum(values []int) int {

	var wg sync.WaitGroup

	ch := make(chan int)

	chunk := len(values)/4 + 1

	for i := 0; i < len(values); i += chunk {

		end := i + chunk

		if end > len(values) {
			end = len(values)
		}

		wg.Add(1)

		go func(part []int) {

			defer wg.Done()

			sum := 0

			for _, v := range part {
				sum += v
			}

			ch <- sum

		}(values[i:end])

	}

	go func() {
		wg.Wait()
		close(ch)
	}()

	total := 0

	for v := range ch {
		total += v
	}

	return total
}

//////////////////////////////////////////////////////////////
// CONTEXT
//////////////////////////////////////////////////////////////

func Worker(ctx context.Context, jobs <-chan *Task, done chan<- int) {

	count := 0

	for {

		select {

		case <-ctx.Done():
			done <- count
			return

		case _, ok := <-jobs:

			if !ok {
				done <- count
				return
			}

			count++
		}
	}
}

//////////////////////////////////////////////////////////////
// REFLECTION
//////////////////////////////////////////////////////////////

func Describe(v any) {

	t := reflect.TypeOf(v)

	fmt.Println("TYPE:", t.Name())

	for i := 0; i < t.NumField(); i++ {

		f := t.Field(i)

		fmt.Println(f.Name, f.Type)
	}
}

//////////////////////////////////////////////////////////////
// JSON
//////////////////////////////////////////////////////////////

func Encode(tasks []*Task) string {

	b, _ := json.MarshalIndent(tasks, "", " ")

	return string(b)
}

//////////////////////////////////////////////////////////////
// MAIN
//////////////////////////////////////////////////////////////

func main() {

	proc := &ComplexProcessor{
		BaseProcessor{
			Name: "Main Processor",
		},
	}

	scheduler := NewScheduler(proc)

	var tasks []*Task

	for i := 0; i < 100; i++ {

		task := &Task{
			ID:       i,
			Name:     fmt.Sprintf("Task-%d", i),
			Priority: (i*17)%100 + 1,
			State:    Created,
		}

		tasks = append(tasks, task)

		scheduler.Push(task)
	}

	scheduler.Execute()

	fmt.Println("Average:", scheduler.metrics.Average())

	priorities := Map(tasks, func(t *Task) int {
		return t.Priority
	})

	fmt.Println("Parallel Sum:", ParallelSum(priorities))

	sort.Ints(priorities)

	fmt.Println("Min:", priorities[0])

	pq := &PriorityQueue{}

	heap.Init(pq)

	for _, p := range priorities {
		heap.Push(pq, p)
	}

	fmt.Println("Top:", heap.Pop(pq))

	fmt.Println("Fib:", Fibonacci(20))

	result := Pipeline(
		10,
		func(x int) int { return x + 5 },
		func(x int) int { return x * 2 },
		func(x int) int { return x - 1 },
	)

	fmt.Println("Pipeline:", result)

	ctx, cancel := context.WithCancel(context.Background())

	jobChan := make(chan *Task)

	done := make(chan int)

	go Worker(ctx, jobChan, done)

	for _, t := range tasks[:25] {
		jobChan <- t
	}

	close(jobChan)

	cancel()

	fmt.Println("Worker processed:", <-done)

	Describe(Task{})

	fmt.Println(Encode(tasks[:3]))

	var cache sync.Map

	cache.Store("average", scheduler.metrics.Average())

	if value, ok := cache.Load("average"); ok {
		fmt.Println(value)
	}
}