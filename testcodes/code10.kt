package demo

import kotlin.reflect.full.memberProperties
import kotlinx.coroutines.*
import java.time.LocalDateTime

//////////////////////////////////////////////////////////////
// TYPE ALIAS
//////////////////////////////////////////////////////////////

typealias TaskId = Int

//////////////////////////////////////////////////////////////
// ANNOTATION
//////////////////////////////////////////////////////////////

@Target(AnnotationTarget.CLASS)
annotation class Component(val name: String)

//////////////////////////////////////////////////////////////
// ENUM
//////////////////////////////////////////////////////////////

enum class TaskState {
    CREATED,
    RUNNING,
    FINISHED,
    FAILED
}

//////////////////////////////////////////////////////////////
// SEALED CLASS
//////////////////////////////////////////////////////////////

sealed class ResultData {
    data class Success(val value: Int) : ResultData()
    data class Failure(val reason: String) : ResultData()
}

//////////////////////////////////////////////////////////////
// DATA CLASS
//////////////////////////////////////////////////////////////

data class Task(
    val id: TaskId,
    val name: String,
    val priority: Int,
    var state: TaskState = TaskState.CREATED
)

//////////////////////////////////////////////////////////////
// INTERFACE
//////////////////////////////////////////////////////////////

interface Processor<T> {
    fun process(task: T): Int
}

//////////////////////////////////////////////////////////////
// ABSTRACT
//////////////////////////////////////////////////////////////

abstract class BaseProcessor<T> {

    abstract fun process(task: T): Int

    protected fun validate(priority: Int) {
        require(priority >= 0)
    }

}

//////////////////////////////////////////////////////////////
// OBSERVER
//////////////////////////////////////////////////////////////

fun interface TaskListener {
    fun onProcessed(task: Task)
}

//////////////////////////////////////////////////////////////
// IMPLEMENTATION
//////////////////////////////////////////////////////////////

@Component("Processor")
class ComplexProcessor :
    BaseProcessor<Task>(),
    Processor<Task> {

    private val listeners =
        mutableListOf<TaskListener>()

    override fun process(task: Task): Int {

        validate(task.priority)

        task.state = TaskState.RUNNING

        if (task.priority % 17 == 0) {
            task.state = TaskState.FAILED
            throw IllegalStateException(task.name)
        }

        task.state = TaskState.FINISHED

        listeners.forEach {
            it.onProcessed(task)
        }

        return task.priority * 3
    }

    fun subscribe(listener: TaskListener) {
        listeners += listener
    }

    companion object {

        fun create() =
            ComplexProcessor()

    }

}

//////////////////////////////////////////////////////////////
// SINGLETON
//////////////////////////////////////////////////////////////

object Logger {

    fun info(message: String) {

        println("[${LocalDateTime.now()}] $message")

    }

}

//////////////////////////////////////////////////////////////
// GENERIC REPOSITORY
//////////////////////////////////////////////////////////////

class Repository<T> {

    private val data =
        mutableListOf<T>()

    fun add(item: T) {

        data += item

    }

    fun all(): List<T> =
        data

}

//////////////////////////////////////////////////////////////
// EXTENSION
//////////////////////////////////////////////////////////////

fun List<Task>.averagePriority() =
    map { it.priority }
        .average()

//////////////////////////////////////////////////////////////
// INLINE + REIFIED
//////////////////////////////////////////////////////////////

inline fun <reified T> printType() {

    println(T::class.simpleName)

}

//////////////////////////////////////////////////////////////
// PROPERTY DELEGATE
//////////////////////////////////////////////////////////////

class Config {

    val version by lazy {

        "1.0.0"

    }

}

//////////////////////////////////////////////////////////////
// OPERATOR
//////////////////////////////////////////////////////////////

class Counter(
    val value: Int
) {

    operator fun plus(
        other: Counter
    ) =
        Counter(
            value + other.value
        )

}

//////////////////////////////////////////////////////////////
// FACTORY
//////////////////////////////////////////////////////////////

object ProcessorFactory {

    fun create():
            Processor<Task> {

        return ComplexProcessor()

    }

}

//////////////////////////////////////////////////////////////
// BUILDER DSL
//////////////////////////////////////////////////////////////

class TaskBuilder {

    var id = 0
    var name = ""
    var priority = 0

    fun build() =
        Task(id, name, priority)

}

fun task(
    block: TaskBuilder.() -> Unit
): Task {

    return TaskBuilder()
        .apply(block)
        .build()

}

//////////////////////////////////////////////////////////////
// GENERATOR
//////////////////////////////////////////////////////////////

fun taskSequence() =
    sequence {

        repeat(100) {

            yield(
                Task(
                    it,
                    "Task-$it",
                    (it * 13) % 100 + 1
                )
            )

        }

    }

//////////////////////////////////////////////////////////////
// TAIL RECURSION
//////////////////////////////////////////////////////////////

tailrec fun fibonacci(
    n: Int,
    a: Int = 0,
    b: Int = 1
): Int {

    if (n == 0)
        return a

    return fibonacci(
        n - 1,
        b,
        a + b
    )

}

//////////////////////////////////////////////////////////////
// SCHEDULER
//////////////////////////////////////////////////////////////

class Scheduler(
    private val processor:
    Processor<Task>
) {

    private val queue =
        ArrayDeque<Task>()

    suspend fun execute() {

        while (queue.isNotEmpty()) {

            val task =
                queue.removeFirst()

            delay(5)

            runCatching {

                processor.process(task)

            }.onFailure {

                Logger.info(
                    it.message ?: ""
                )

            }

        }

    }

    fun enqueue(task: Task) {

        queue += task

    }

}

//////////////////////////////////////////////////////////////
// REFLECTION
//////////////////////////////////////////////////////////////

fun inspect(any: Any) {

    any::class.memberProperties
        .forEach {

            println(it.name)

        }

}

//////////////////////////////////////////////////////////////
// MAIN
//////////////////////////////////////////////////////////////

fun main() = runBlocking {

    val repository =
        Repository<Task>()

    val processor =
        ComplexProcessor.create()

    processor.subscribe {

        Logger.info(
            it.name
        )

    }

    val scheduler =
        Scheduler(processor)

    taskSequence()
        .forEach {

            repository.add(it)

            scheduler.enqueue(it)

        }

    scheduler.execute()

    println(
        repository
            .all()
            .averagePriority()
    )

    println(
        fibonacci(20)
    )

    val counter =
        Counter(10) +
                Counter(5)

    println(counter.value)

    val config =
        Config()

    println(config.version)

    printType<Task>()

    inspect(processor)

    val ordered =
        repository
            .all()
            .sortedByDescending {
                it.priority
            }

    ordered
        .take(5)
        .forEach {

            println(it)

        }

    val grouped =
        repository
            .all()
            .groupBy {
                it.priority / 10
            }

    println(grouped.keys)

    val mapped =
        repository
            .all()
            .associateBy {
                it.id
            }

    println(
        mapped[5]
    )

    val built =
        task {

            id = 999

            name = "DSL"

            priority = 50

        }

    println(built)

    val result: ResultData =
        ResultData.Success(123)

    when (result) {

        is ResultData.Success ->
            println(result.value)

        is ResultData.Failure ->
            println(result.reason)

    }

    repository
        .all()
        .asSequence()
        .filter {
            it.priority > 50
        }
        .map {
            it.name
        }
        .take(10)
        .forEach(::println)

}