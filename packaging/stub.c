/*
 * SesametokBackend.app 실행 스텁.
 *
 * 이 파일이 하는 일은 하나뿐이다: 번들 안의 Resources/target에서 인터프리터 경로와
 * 모듈 이름을 읽어 **자식 프로세스로 spawn**하고, 자신은 부모로 남아 기다린다.
 *
 * exec하지 않는 것이 핵심이다. macOS TCC는 권한을 "책임 프로세스"에 귀속시키는데,
 * 부모가 앱 번들이면 자식이 그 권한을 물려받는다. 그래서:
 *   - 사용자는 /Applications의 앱 하나에만 전체 디스크 접근·손쉬운 사용을 준다.
 *   - 같은 인터프리터를 다른 프로젝트나 셸에서 써도 권한은 0이다(실측 확인).
 *
 * 실행 모드
 *   SesametokBackend                 → python -m <module> start     (더블클릭 = CLI start)
 *   SesametokBackend --managed       → python -m <module> foreground --managed
 *   SesametokBackend <args...>       → python -m <module> <args...>
 */
#include <errno.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <signal.h>
#include <unistd.h>

#define MAX_ARGS 64
/* launchd/Finder의 PATH는 빈약하다. 백엔드가 부르는 도구를 위해 고정한다. */
#define FIXED_PATH "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

/* 자식 pid. launchd의 bootout은 잡의 main 프로세스(이 스텁)에만 SIGTERM을 보내므로,
 * 스텁이 죽으면 python 자식이 고아가 되어 계속 살아남는다. 그래서 시그널을 자식에게
 * 전달하고, 유예 뒤에도 남으면 강제 종료한다. */
static volatile pid_t child_pid = 0;

static void forward_signal(int signum) {
    if (child_pid > 0) {
        kill(child_pid, signum);
    }
}

static char *trim(char *s) {
    size_t n = strlen(s);
    while (n > 0 && (s[n - 1] == '\n' || s[n - 1] == '\r')) {
        s[--n] = '\0';
    }
    return s;
}

/* 번들 안의 Resources/target에서 인터프리터와 모듈 이름을 읽는다. */
static int read_target(char *python, size_t python_size, char *module, size_t module_size) {
    char exe[4096];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) {
        fprintf(stderr, "sesametok: cannot resolve the executable path\n");
        return -1;
    }
    char *slash = strrchr(exe, '/');
    if (slash == NULL) {
        return -1;
    }
    *slash = '\0'; /* .../Contents/MacOS */

    char target[4096];
    snprintf(target, sizeof(target), "%s/../Resources/target", exe);
    FILE *fp = fopen(target, "r");
    if (fp == NULL) {
        fprintf(stderr, "sesametok: cannot read %s\n", target);
        return -1;
    }
    if (fgets(python, (int)python_size, fp) == NULL) {
        fclose(fp);
        return -1;
    }
    if (fgets(module, (int)module_size, fp) == NULL) {
        fclose(fp);
        return -1;
    }
    fclose(fp);
    trim(python);
    trim(module);
    if (python[0] == '\0') {
        return -1;
    }
    if (module[0] == '\0') {
        strncpy(module, "sesametok_backend", module_size - 1);
    }
    return 0;
}

int main(int argc, char *argv[]) {
    char python[4096] = {0};
    char module[256] = {0};
    if (read_target(python, sizeof(python), module, sizeof(module)) != 0) {
        return 2;
    }

    char *args[MAX_ARGS];
    int n = 0;
    args[n++] = python;
    args[n++] = "-m";
    args[n++] = module;

    if (argc <= 1) {
        /* Finder 더블클릭: CLI `start`와 동일하게 launchd 등록/기동만 담당한다. */
        args[n++] = "start";
    } else if (strcmp(argv[1], "--managed") == 0) {
        /* LaunchAgent가 띄운 경우: launchd가 관리하는 포그라운드 모드. */
        args[n++] = "foreground";
        args[n++] = "--managed";
    } else {
        for (int i = 1; i < argc && n < MAX_ARGS - 1; i++) {
            args[n++] = argv[i];
        }
    }
    args[n] = NULL;

    pid_t pid = fork();
    if (pid == 0) {
        setenv("PATH", FIXED_PATH, 1);
        execv(python, args);
        fprintf(stderr, "sesametok: exec failed: %s\n", python);
        _exit(127);
    }
    if (pid < 0) {
        fprintf(stderr, "sesametok: fork failed\n");
        return 4;
    }
    child_pid = pid;
    signal(SIGTERM, forward_signal);
    signal(SIGINT, forward_signal);

    int status = 0;
    if (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
        /* 시그널을 받아 waitpid가 중단됐다. 자식이 스스로 내려가길 잠시 기다린다. */
        for (int i = 0; i < 50; i++) {
            pid_t done = waitpid(pid, &status, WNOHANG);
            if (done == pid) {
                child_pid = 0;
                return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
            }
            usleep(100000); /* 100ms × 50 = 최대 5초 */
        }
        kill(pid, SIGKILL);
        waitpid(pid, &status, 0);
    }
    child_pid = 0;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
