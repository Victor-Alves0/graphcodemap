;=========================================================
; Complex NASM x86-64 Example
; Linux - System V ABI
;=========================================================

global _start

;=========================================================
; MACROS
;=========================================================

%macro PUSH_ALL 0
    push rax
    push rbx
    push rcx
    push rdx
    push rsi
    push rdi
%endmacro

%macro POP_ALL 0
    pop rdi
    pop rsi
    pop rdx
    pop rcx
    pop rbx
    pop rax
%endmacro

;=========================================================
; DATA
;=========================================================

section .data

numbers dq 1,2,3,4,5,6,7,8,9,10

count dq 10

result dq 0

factorial_result dq 0

fib_result dq 0

;=========================================================
; BSS
;=========================================================

section .bss

buffer resq 128

;=========================================================
; TEXT
;=========================================================

section .text

;---------------------------------------------------------
; ENTRY
;---------------------------------------------------------

_start:

    call initialize

    call sum_array

    mov [result], rax

    mov rdi, 10
    call factorial

    mov [factorial_result], rax

    mov rdi, 15
    call fibonacci

    mov [fib_result], rax

    call process_loop

    call search_value

    call compute_average

    mov rax,60
    xor rdi,rdi
    syscall

;---------------------------------------------------------
; INITIALIZE
;---------------------------------------------------------

initialize:

    push rbp
    mov rbp,rsp

    xor rcx,rcx

.loop:

    cmp rcx,10
    je .done

    mov rax,rcx
    inc rax

    mov [numbers+rcx*8],rax

    inc rcx

    jmp .loop

.done:

    leave
    ret

;---------------------------------------------------------
; SUM ARRAY
;---------------------------------------------------------

sum_array:

    push rbp
    mov rbp,rsp

    xor rax,rax
    xor rcx,rcx

.loop:

    cmp rcx,[count]
    je .finish

    add rax,[numbers+rcx*8]

    inc rcx

    jmp .loop

.finish:

    leave
    ret

;---------------------------------------------------------
; FACTORIAL
;---------------------------------------------------------

factorial:

    push rbp
    mov rbp,rsp

    cmp rdi,1
    jle .base

    push rdi

    dec rdi

    call factorial

    pop rbx

    imul rax,rbx

    leave
    ret

.base:

    mov rax,1

    leave
    ret

;---------------------------------------------------------
; FIBONACCI
;---------------------------------------------------------

fibonacci:

    push rbp
    mov rbp,rsp

    cmp rdi,1
    jle .base

    push rdi

    dec rdi

    call fibonacci

    mov rbx,rax

    pop rdi

    sub rdi,2

    push rbx

    call fibonacci

    pop rbx

    add rax,rbx

    leave
    ret

.base:

    mov rax,rdi

    leave
    ret

;---------------------------------------------------------
; LOOP
;---------------------------------------------------------

process_loop:

    push rbp
    mov rbp,rsp

    xor rcx,rcx

.loop:

    cmp rcx,10
    je .done

    mov rax,[numbers+rcx*8]

    shl rax,1

    mov [numbers+rcx*8],rax

    inc rcx

    jmp .loop

.done:

    leave
    ret

;---------------------------------------------------------
; SEARCH
;---------------------------------------------------------

search_value:

    push rbp
    mov rbp,rsp

    mov rdx,8

    xor rcx,rcx

.loop:

    cmp rcx,10
    je .not_found

    cmp [numbers+rcx*8],rdx

    je .found

    inc rcx

    jmp .loop

.found:

    mov rax,rcx

    leave
    ret

.not_found:

    mov rax,-1

    leave
    ret

;---------------------------------------------------------
; AVERAGE
;---------------------------------------------------------

compute_average:

    push rbp
    mov rbp,rsp

    call sum_array

    mov rbx,[count]

    xor rdx,rdx

    div rbx

    leave
    ret

;---------------------------------------------------------
; SWAP
;---------------------------------------------------------

swap:

    mov rax,[rdi]

    mov rbx,[rsi]

    mov [rdi],rbx

    mov [rsi],rax

    ret

;---------------------------------------------------------
; MAX
;---------------------------------------------------------

max:

    cmp rdi,rsi

    jg .left

    mov rax,rsi

    ret

.left:

    mov rax,rdi

    ret

;---------------------------------------------------------
; MIN
;---------------------------------------------------------

min:

    cmp rdi,rsi

    jl .left

    mov rax,rsi

    ret

.left:

    mov rax,rdi

    ret

;---------------------------------------------------------
; POWER
;---------------------------------------------------------

power:

    push rbp
    mov rbp,rsp

    mov rax,1

.loop:

    cmp rsi,0
    je .finish

    imul rax,rdi

    dec rsi

    jmp .loop

.finish:

    leave
    ret

;---------------------------------------------------------
; COPY ARRAY
;---------------------------------------------------------

copy_array:

    push rbp
    mov rbp,rsp

    xor rcx,rcx

.loop:

    cmp rcx,10
    je .done

    mov rax,[rdi+rcx*8]

    mov [rsi+rcx*8],rax

    inc rcx

    jmp .loop

.done:

    leave
    ret