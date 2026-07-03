/* alloc_demo — a libc allocation workload for OpenTrace's ltrace (Library calls)
 * collector. Run it with the "Library calls" collector ON:
 *
 *     ./alloc_demo
 *
 * It (1) churns short-lived buffers (malloc+free), then (2) deliberately LEAKS
 * a batch of blocks that are never freed, plus a calloc/realloc. The Profiling
 * tab shows the malloc/free ledger + library-call hotspots, and the heap_leak
 * anomaly fires on the un-freed blocks. Compile: gcc -O0 alloc_demo.c -o alloc_demo
 */
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    /* (1) churn: allocate and free 200 short-lived 4 KB buffers */
    for (int i = 0; i < 200; i++) {
        char *buf = malloc(4096);
        memset(buf, i & 0xff, 4096);
        free(buf);
    }

    /* (2) leak: 80 blocks of 8 KB each (= 640 KB) that are never freed */
    for (int i = 0; i < 80; i++) {
        char *leak = malloc(8192);
        memset(leak, 1, 64);
        (void) leak; /* intentionally dropped */
    }

    /* a calloc + realloc that IS cleaned up (shows realloc accounting) */
    int *tab = calloc(256, sizeof(int));
    tab = realloc(tab, 4096 * sizeof(int));
    memset(tab, 0, 4096 * sizeof(int));
    free(tab);

    usleep(2000);
    return 0;
}
