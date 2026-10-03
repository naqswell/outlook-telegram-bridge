/*
 * Executable of OutlookTelegramBridge.app.
 *
 * macOS grants Full Disk Access to the code that runs. With a script as the
 * app's executable the running code would be /bin/sh, and a grant given to
 * the app would not apply. This compiled binary is what holds the grant. It
 * starts Python as a child process rather than exec'ing it, so it stays the
 * responsible process and the child inherits the grant. That holds when
 * launchd starts the app; started from a terminal, the terminal is the
 * responsible process and its permissions apply instead.
 *
 * The Python daemon lives outside the bundle: updating it leaves the app's
 * signature, and with it the grant, untouched.
 */
#include <errno.h>
#include <limits.h>
#include <pwd.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define PYTHON "/usr/bin/python3"
#define SCRIPT "/.local/libexec/outlook-telegram-bridge/outlook_telegram_bridge.py"
#define EX_CONFIG 78

extern char **environ;

static volatile sig_atomic_t child = 0;

static void forward(int sig) {
    if (child > 0) {
        kill((pid_t)child, sig);
    }
}

int main(int argc, char *argv[]) {
    const char *home = getenv("HOME");
    if (home == NULL || home[0] == '\0') {
        struct passwd *pw = getpwuid(getuid());
        home = pw != NULL ? pw->pw_dir : NULL;
    }
    if (home == NULL) {
        fprintf(stderr, "outlook-telegram-bridge: no home directory\n");
        return EX_CONFIG;
    }

    char script[PATH_MAX];
    if (snprintf(script, sizeof script, "%s%s", home, SCRIPT)
            >= (int)sizeof script) {
        fprintf(stderr, "outlook-telegram-bridge: home path too long\n");
        return EX_CONFIG;
    }
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "outlook-telegram-bridge: %s: %s; run install.sh\n",
                script, strerror(errno));
        return EX_CONFIG;
    }

    /* python3, the script, argv[1..], NULL; argc can be 0 */
    char **args = calloc((size_t)argc + 3, sizeof *args);
    if (args == NULL) {
        fprintf(stderr, "outlook-telegram-bridge: out of memory\n");
        return 71; /* EX_OSERR */
    }
    args[0] = PYTHON;
    args[1] = script;
    for (int i = 1; i < argc; i++) {
        args[i + 1] = argv[i];
    }

    /* Lets the daemon stop once this app is gone. */
    setenv("OTB_VIA_APP", "1", 1);

    /*
     * Hold back the signals we pass on until the child exists: one arriving
     * in between would otherwise kill this process and orphan the child.
     */
    sigset_t passed_on, original;
    sigemptyset(&passed_on);
    sigaddset(&passed_on, SIGTERM);
    sigaddset(&passed_on, SIGINT);
    sigaddset(&passed_on, SIGHUP);
    sigprocmask(SIG_BLOCK, &passed_on, &original);

    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_handler = forward;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);

    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr, &original);
    posix_spawnattr_setsigdefault(&attr, &passed_on);
    posix_spawnattr_setflags(&attr,
                             POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);

    pid_t pid;
    int rc = posix_spawn(&pid, PYTHON, NULL, &attr, args, environ);
    posix_spawnattr_destroy(&attr);
    free(args);
    if (rc != 0) {
        fprintf(stderr, "outlook-telegram-bridge: cannot start %s: %s\n",
                PYTHON, strerror(rc));
        return 127;
    }
    child = pid;
    sigprocmask(SIG_SETMASK, &original, NULL);

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("outlook-telegram-bridge: waitpid");
            return 1;
        }
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return WEXITSTATUS(status);
}
