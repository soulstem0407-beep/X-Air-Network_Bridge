/*
 * Thin exec wrapper for release builds.
 * Does not implement transport, OSC, or audio I/O.
 */
#define _POSIX_C_SOURCE 200112L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef XAIR_ROOT
#define XAIR_ROOT "/usr/lib/xair-network-bridge"
#endif

#ifndef XAIR_VERSION
#define XAIR_VERSION "0.0.0"
#endif

#if defined(__GNUC__)
#define XAIR_USED __attribute__((used))
#else
#define XAIR_USED
#endif

static const char xair_release_stamp[] XAIR_USED =
    "XAIR_RELEASE_VERSION=" XAIR_VERSION;

int main(int argc, char **argv)
{
    const char *root = XAIR_ROOT;
    const char *old;
    char path[4096];
    char **nargv;
    int i;
    int n;

    (void)xair_release_stamp;
    old = getenv("PYTHONPATH");
    if (old != NULL && old[0] != '\0') {
        if (snprintf(path, sizeof path, "%s:%s", root, old) >= (int)sizeof path) {
            fputs("xair-network-bridge: PYTHONPATH too long\n", stderr);
            return 127;
        }
    } else {
        if (snprintf(path, sizeof path, "%s", root) >= (int)sizeof path) {
            fputs("xair-network-bridge: root too long\n", stderr);
            return 127;
        }
    }
    if (setenv("PYTHONPATH", path, 1) != 0) {
        perror("setenv");
        return 127;
    }
    if (chdir(root) != 0) {
        perror("chdir");
        return 127;
    }
    n = argc + 2;
    nargv = calloc((size_t)n + 1, sizeof(char *));
    if (nargv == NULL) {
        return 127;
    }
    nargv[0] = "python3";
    nargv[1] = "-m";
    nargv[2] = "src.cli";
    for (i = 1; i < argc; i++) {
        nargv[i + 2] = argv[i];
    }
    execvp("python3", nargv);
    perror("python3");
    return 127;
}
