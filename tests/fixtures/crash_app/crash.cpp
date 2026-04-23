// Deterministic crash fixture for Stackly integration tests.
//
// Argv selects the crash mode:
//   crash_app.exe null   -> null pointer dereference  (EXCEPTION_ACCESS_VIOLATION)
//   crash_app.exe stack  -> infinite recursion        (EXCEPTION_STACK_OVERFLOW)
//   crash_app.exe throw  -> uncaught C++ exception    (EXCEPTION_CPP)
//   crash_app.exe wait   -> block waiting for stdin   (for breakpoint tests)
//
// Built with /Zi /Od /MTd so symbols + locals are reliable for the debugger.
// Every crash path lives in a named function so the stack trace can be asserted.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <process.h>
#include <stdexcept>
#include <string>

#ifdef _MSC_VER
#  define NOINLINE __declspec(noinline)
#else
#  define NOINLINE __attribute__((noinline))
#endif

NOINLINE void crash_null(int depth) {
    int local_counter = 42;
    const char* local_tag = "about-to-deref-null";
    int* bad_pointer = nullptr;
    // Touch locals so the debugger has something visible before the crash.
    (void)local_counter;
    (void)local_tag;
    (void)depth;
    *bad_pointer = 99;  // EXCEPTION_ACCESS_VIOLATION_WRITE here.
}

NOINLINE void crash_stack_overflow(int depth) {
    volatile int padding[64];
    padding[0] = depth;
    crash_stack_overflow(depth + 1);
}

NOINLINE void crash_throw() {
    throw std::runtime_error("boom");
}

NOINLINE void wait_for_stdin() {
    std::printf("waiting for input on pid=%d\n", _getpid());
    (void)std::fflush(stdout);
    char buf[64];
    (void)std::fgets(buf, sizeof(buf), stdin);
}

int main(int argc, char** argv) {
    std::printf("crash_app pid=%d\n", _getpid());
    (void)std::fflush(stdout);

    if (argc < 2) {
        std::printf("usage: crash_app.exe <null|stack|throw|wait>\n");
        return 2;
    }

    const std::string mode = argv[1];
    if (mode == "null") {
        crash_null(0);
    } else if (mode == "stack") {
        crash_stack_overflow(0);
    } else if (mode == "throw") {
        crash_throw();
    } else if (mode == "wait") {
        wait_for_stdin();
    } else {
        std::printf("unknown mode: %s\n", argv[1]);
        return 2;
    }
    return 0;
}
