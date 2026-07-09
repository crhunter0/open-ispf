#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#ifdef _WIN32
#include <direct.h>
#define strdup _strdup
#define MKDIR(path) _mkdir(path)
#else
#include <unistd.h>
#define MKDIR(path) mkdir(path, 0755)
#endif

static int ensure_parent_dirs(const char *path) {
    char *tmp = strdup(path);
    if (tmp == NULL) {
        return 8;
    }

    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/' || *p == '\\') {
            char saved = *p;
            *p = '\0';
            if (strlen(tmp) > 0) {
                if (MKDIR(tmp) != 0 && errno != EEXIST) {
                    free(tmp);
                    return 8;
                }
            }
            *p = saved;
        }
    }

    free(tmp);
    return 0;
}

int main(int argc, char **argv) {
    int rc = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--dd") != 0 || (i + 1) >= argc) {
            continue;
        }

        char *payload = argv[++i];
        char *copy = strdup(payload);
        if (copy == NULL) {
            fprintf(stderr, "IEFBR14: memory allocation failure\n");
            return 16;
        }

        char *parts[5] = {0};
        int part_count = 0;
        char *cursor = copy;
        parts[part_count++] = cursor;
        while (*cursor && part_count < 5) {
            if (*cursor == '|') {
                *cursor = '\0';
                parts[part_count++] = cursor + 1;
            }
            cursor++;
        }
        while (part_count < 5) {
            parts[part_count++] = "";
        }

        const char *ddname = part_count > 0 ? parts[0] : "";
        const char *dsn = part_count > 1 ? parts[1] : "";
        const char *disp_primary = part_count > 2 ? parts[2] : "";
        const char *disp_normal = part_count > 3 ? parts[3] : "";
        const char *path = part_count > 4 ? parts[4] : "";

        if (path[0] == '\0') {
            fprintf(stderr, "IEFBR14: %s (%s) missing path\n", ddname, dsn);
            if (rc < 8) rc = 8;
            free(copy);
            continue;
        }

        if (strcmp(disp_primary, "NEW") == 0 || strcmp(disp_primary, "MOD") == 0) {
            int mkrc = ensure_parent_dirs(path);
            if (mkrc != 0) {
                fprintf(stderr, "IEFBR14: unable to create parent directories for %s\n", path);
                if (rc < mkrc) rc = mkrc;
            } else {
                FILE *f = fopen(path, strcmp(disp_primary, "MOD") == 0 ? "ab" : "wb");
                if (f == NULL) {
                    fprintf(stderr, "IEFBR14: fopen failed for %s\n", path);
                    if (rc < 8) rc = 8;
                } else {
                    fclose(f);
                    fprintf(stdout, "IEFBR14: allocated %s\n", path);
                }
            }
        } else if (strcmp(disp_primary, "OLD") == 0 || strcmp(disp_primary, "SHR") == 0) {
            FILE *f = fopen(path, "rb");
            if (f == NULL) {
                fprintf(stderr, "IEFBR14: required data set missing %s\n", path);
                if (rc < 8) rc = 8;
            } else {
                fclose(f);
            }
        }

        if (strcmp(disp_normal, "DELETE") == 0) {
            if (remove(path) == 0) {
                fprintf(stdout, "IEFBR14: deleted %s\n", path);
            } else if (errno != ENOENT) {
                fprintf(stderr, "IEFBR14: delete failed for %s\n", path);
                if (rc < 8) rc = 8;
            }
        }

        free(copy);
    }

    return rc;
}
