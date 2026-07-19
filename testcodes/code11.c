#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <threads.h>

//////////////////////////////////////////////////////////////
// MACROS
//////////////////////////////////////////////////////////////

#define MAX_NAME 64
#define ARRAY_SIZE(x) (sizeof(x)/sizeof(x[0]))

//////////////////////////////////////////////////////////////
// ENUM
//////////////////////////////////////////////////////////////

typedef enum
{
    TASK_CREATED,
    TASK_RUNNING,
    TASK_FINISHED,
    TASK_FAILED

} TaskState;

//////////////////////////////////////////////////////////////
// STRUCTS
//////////////////////////////////////////////////////////////

typedef struct
{
    int id;

    char name[MAX_NAME];

    int priority;

    TaskState state;

} Task;

typedef struct Node
{
    Task task;

    struct Node* next;

} Node;

typedef struct TreeNode
{
    Task task;

    struct TreeNode* left;

    struct TreeNode* right;

} TreeNode;

//////////////////////////////////////////////////////////////
// CALLBACK
//////////////////////////////////////////////////////////////

typedef void (*TaskCallback)(
    Task*
);

//////////////////////////////////////////////////////////////
// GLOBALS
//////////////////////////////////////////////////////////////

static mtx_t mutex;

//////////////////////////////////////////////////////////////
// UTIL
//////////////////////////////////////////////////////////////

inline int max(
    int a,
    int b
){
    return a>b?a:b;
}

//////////////////////////////////////////////////////////////
// LIST
//////////////////////////////////////////////////////////////

Node* list_push(
    Node* head,
    Task task
){

    Node* node=
        malloc(sizeof(Node));

    node->task=task;

    node->next=head;

    return node;

}

void list_foreach(
    Node* head,
    TaskCallback callback
){

    while(head)
    {
        callback(&head->task);

        head=head->next;
    }

}

//////////////////////////////////////////////////////////////
// TREE
//////////////////////////////////////////////////////////////

TreeNode* tree_insert(
    TreeNode* root,
    Task task
){

    if(root==NULL)
    {

        root=
            calloc(
                1,
                sizeof(TreeNode)
            );

        root->task=task;

        return root;

    }

    if(task.priority<
       root->task.priority)

        root->left=
            tree_insert(
                root->left,
                task
            );

    else

        root->right=
            tree_insert(
                root->right,
                task
            );

    return root;

}

void inorder(
    TreeNode* root
){

    if(root==NULL)
        return;

    inorder(root->left);

    printf(
        "%s\n",
        root->task.name
    );

    inorder(root->right);

}

//////////////////////////////////////////////////////////////
// RECURSION
//////////////////////////////////////////////////////////////

int fibonacci(
    int n
){

    if(n<2)
        return n;

    return
        fibonacci(n-1)
        +
        fibonacci(n-2);

}

//////////////////////////////////////////////////////////////
// SORT
//////////////////////////////////////////////////////////////

int compare(
    const void* a,
    const void* b
){

    Task* ta=
        (Task*)a;

    Task* tb=
        (Task*)b;

    return
        tb->priority
        -
        ta->priority;

}

//////////////////////////////////////////////////////////////
// THREAD
//////////////////////////////////////////////////////////////

int worker(
    void* arg
){

    Task* task=
        (Task*)arg;

    mtx_lock(&mutex);

    task->state=
        TASK_RUNNING;

    task->priority*=2;

    task->state=
        TASK_FINISHED;

    mtx_unlock(&mutex);

    return 0;

}

//////////////////////////////////////////////////////////////
// FILE
//////////////////////////////////////////////////////////////

void save(
    Task* tasks,
    size_t count
){

    FILE* f=
        fopen(
            "tasks.txt",
            "w"
        );

    if(!f)
        return;

    for(size_t i=0;
        i<count;
        i++)
    {

        fprintf(
            f,
            "%d %s %d\n",
            tasks[i].id,
            tasks[i].name,
            tasks[i].priority
        );

    }

    fclose(f);

}

//////////////////////////////////////////////////////////////
// CALLBACK
//////////////////////////////////////////////////////////////

void printer(
    Task* task
){

    printf(
        "%d %s\n",
        task->id,
        task->name
    );

}

//////////////////////////////////////////////////////////////
// MAIN
//////////////////////////////////////////////////////////////

int main()
{

    mtx_init(
        &mutex,
        mtx_plain
    );

    Task tasks[100];

    for(int i=0;
        i<100;
        i++)
    {

        tasks[i].id=i;

        sprintf(
            tasks[i].name,
            "Task-%d",
            i
        );

        tasks[i].priority=
            (i*13)%100+1;

        tasks[i].state=
            TASK_CREATED;

    }

    qsort(
        tasks,
        ARRAY_SIZE(tasks),
        sizeof(Task),
        compare
    );

    Node* list=NULL;

    for(size_t i=0;
        i<ARRAY_SIZE(tasks);
        i++)
    {

        list=
            list_push(
                list,
                tasks[i]
            );

    }

    list_foreach(
        list,
        printer
    );

    TreeNode* root=NULL;

    for(size_t i=0;
        i<ARRAY_SIZE(tasks);
        i++)
    {

        root=
            tree_insert(
                root,
                tasks[i]
            );

    }

    inorder(root);

    thrd_t thread;

    thrd_create(
        &thread,
        worker,
        &tasks[0]
    );

    thrd_join(
        thread,
        NULL
    );

    printf(
        "%d\n",
        fibonacci(20)
    );

    save(
        tasks,
        ARRAY_SIZE(tasks)
    );

    Task key=
    {
        .priority=50
    };

    Task* found=
        bsearch(
            &key,
            tasks,
            ARRAY_SIZE(tasks),
            sizeof(Task),
            compare
        );

    if(found)
    {

        printf(
            "%s\n",
            found->name
        );

    }

    mtx_destroy(
        &mutex
    );

    return 0;
}