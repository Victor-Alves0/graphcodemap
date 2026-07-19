// ============================================================
// MODULE
// ============================================================

const App = (() => {

class TaskError extends Error {
    constructor(message){
        super(message);
        this.name = "TaskError";
    }
}

// ============================================================
// ENUM SIMULATION
// ============================================================

const TaskState = Object.freeze({
    CREATED: Symbol("created"),
    RUNNING: Symbol("running"),
    FINISHED: Symbol("finished"),
    FAILED: Symbol("failed")
});

// ============================================================
// EVENT EMITTER
// ============================================================

class EventEmitter{

    #events = new Map();

    on(name, callback){

        if(!this.#events.has(name))
            this.#events.set(name, []);

        this.#events.get(name).push(callback);
    }

    emit(name, ...args){

        this.#events.get(name)?.forEach(fn => fn(...args));
    }

}

// ============================================================
// MODEL
// ============================================================

class Task{

    #priority;

    constructor(id,name,priority){

        this.id=id;
        this.name=name;

        this.#priority=priority;

        this.state=TaskState.CREATED;
    }

    get priority(){
        return this.#priority;
    }

    set priority(value){
        this.#priority=Math.max(0,value);
    }

}

// ============================================================
// BASE CLASS
// ============================================================

class Processor{

    process(task){
        throw new Error("abstract");
    }

}

// ============================================================
// IMPLEMENTATION
// ============================================================

class ComplexProcessor extends Processor{

    static version="2.0";

    process(task){

        task.state=TaskState.RUNNING;

        if(task.priority%19===0){

            task.state=TaskState.FAILED;

            throw new TaskError(task.name);
        }

        task.state=TaskState.FINISHED;

        return task.priority*3;
    }

}

// ============================================================
// REPOSITORY
// ============================================================

class Repository{

    #items=[];

    add(item){
        this.#items.push(item);
    }

    all(){
        return [...this.#items];
    }

    *generator(){

        for(const item of this.#items)
            yield item;
    }

}

// ============================================================
// SCHEDULER
// ============================================================

class Scheduler extends EventEmitter{

    constructor(processor){

        super();

        this.processor=processor;

        this.queue=[];
    }

    enqueue(task){

        this.queue.push(task);
    }

    async execute(){

        const values=[];

        while(this.queue.length){

            const task=this.queue.shift();

            try{

                await delay(5);

                values.push(
                    this.processor.process(task)
                );

                this.emit("processed",task);

            }catch(e){

                this.emit("error",e);

            }

        }

        return values;

    }

}

// ============================================================
// HELPERS
// ============================================================

const delay=(ms)=>
    new Promise(r=>setTimeout(r,ms));

function fibonacci(n){

    if(n<2)
        return n;

    return fibonacci(n-1)+fibonacci(n-2);

}

function pipeline(value,...funcs){

    return funcs.reduce(
        (v,f)=>f(v),
        value
    );

}

// ============================================================
// PROXY
// ============================================================

function createProxy(target){

    return new Proxy(target,{

        get(obj,key){

            console.log("GET",key);

            return Reflect.get(obj,key);

        },

        set(obj,key,value){

            console.log("SET",key);

            return Reflect.set(obj,key,value);

        }

    });

}

// ============================================================
// WEAKMAP
// ============================================================

const metadata=new WeakMap();

function attachMetadata(task,data){

    metadata.set(task,data);

}

function getMetadata(task){

    return metadata.get(task);

}

// ============================================================
// MAIN
// ============================================================

async function main(){

    const repository=new Repository();

    const scheduler=new Scheduler(
        new ComplexProcessor()
    );

    scheduler.on("processed",
        t=>console.log("OK",t.name));

    scheduler.on("error",
        e=>console.log("ERROR",e.message));

    for(let i=0;i<100;i++){

        const task=new Task(
            i,
            `Task-${i}`,
            (i*17)%100+1
        );

        attachMetadata(task,{
            created:new Date()
        });

        repository.add(task);

        scheduler.enqueue(task);

    }

    const values=await scheduler.execute();

    const total=
        values.reduce(
            (a,b)=>a+b,
            0
        );

    console.log(total);

    const top=
        repository
        .all()
        .filter(x=>x.priority>50)
        .sort((a,b)=>b.priority-a.priority)
        .slice(0,10);

    console.log(top.length);

    const grouped=
        repository
        .all()
        .reduce((acc,item)=>{

            const key=
                Math.floor(item.priority/10);

            (acc[key]??=[]).push(item);

            return acc;

        },{});

    console.log(grouped);

    const set=
        new Set(
            repository
            .all()
            .map(x=>x.priority)
        );

    console.log(set.size);

    const map=
        new Map();

    repository.all().forEach(
        t=>map.set(t.id,t)
    );

    console.log(
        map.get(5)?.name ?? "Not Found"
    );

    console.log(
        pipeline(
            10,
            x=>x+5,
            x=>x*2,
            x=>x-3
        )
    );

    console.log(
        fibonacci(20)
    );

    const proxy=
        createProxy(
            repository.all()[0]
        );

    proxy.priority=80;

    console.log(proxy.priority);

    console.log(
        getMetadata(proxy)
    );

    for(const task of repository.generator()){

        if(task.id>3)
            break;

        console.log(task.name);

    }

}

return{
    main
};

})();

// ============================================================

App.main();